from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import socket
import subprocess
import sys
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest
from fastapi import HTTPException

import llm_gateway_core.agents.deep_research_adapter as adapter_module
import llm_gateway_core.services.deep_research_process as process_module
from llm_gateway_core.agents.deep_research_adapter import (
    GPT_RESEARCHER_REQUIRED_VERSION,
    DeepResearchUnavailableError,
    collect_public_result,
    get_gpt_researcher_factory,
)
from llm_gateway_core.services.deep_research_process import (
    DeepResearchAdmissionTimeout,
    DeepResearchCallbacks,
    DeepResearchProcessClosed,
    DeepResearchProcessError,
    DeepResearchProcessRunner,
    DeepResearchRunnerState,
    _recv_async_frame,
)
from llm_gateway_core.services.deep_research_protocol import (
    DEEP_RESEARCH_FRAME_MAX_BYTES,
    DeepResearchCallbackOperation,
    DeepResearchCallbackReportedError,
    DeepResearchCallbackRequest,
    DeepResearchJob,
    DeepResearchProtocolError,
    DeepResearchResult,
    callback_error_message,
    callback_request_message,
    callback_result_message,
    decode_payload,
    encode_frame,
    parse_callback_request,
    parse_callback_response,
)
from tests._async_compat import run_async


FIXTURE_ADAPTER = "tests.deep_research_process_fixture"


def _job(query: str = "topic") -> DeepResearchJob:
    return DeepResearchJob(
        job_id=f"job-{hashlib.sha256(query.encode()).hexdigest()[:16]}",
        query=query,
        fast_model="llmgateway/fast",
        smart_model="llmgateway/smart",
        strategic_model="llmgateway/strategic",
        embedding_model="llmgateway/embedding",
        gateway_base_url="http://127.0.0.1:9000/v1",
        gateway_api_key="child-secret",
    )


def _pid_exists(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def test_protocol_round_trip_is_strict_and_job_repr_hides_credentials() -> None:
    job = _job()
    encoded = encode_frame({"job": job.to_wire()})
    size = int.from_bytes(encoded[:4], "big")
    assert decode_payload(encoded[4:]) == {"job": job.to_wire()}
    assert size == len(encoded) - 4
    assert "child-secret" not in repr(job)

    with pytest.raises(DeepResearchProtocolError):
        decode_payload(b'{"duplicate":1,"duplicate":2}')
    with pytest.raises(DeepResearchProtocolError):
        encode_frame({"payload": "x" * DEEP_RESEARCH_FRAME_MAX_BYTES})
    with pytest.raises(DeepResearchProtocolError):
        DeepResearchJob.from_wire({**job.to_wire(), "unknown": True})
    with pytest.raises(ValueError):
        DeepResearchResult(
            query="topic",
            report="report",
            research_result=None,
            sources=(),
            source_urls=(),
            context=(),
            costs=float("nan"),
        )


def test_callback_protocol_has_exact_job_message_and_operation_identity() -> None:
    request = DeepResearchCallbackRequest(
        job_id="job-1",
        message_id="message-2",
        operation=DeepResearchCallbackOperation.SEARCH,
        arguments={"query": "topic", "max_results": 2},
    )
    assert parse_callback_request(callback_request_message(request)) == request
    result = [
        {"url": "https://example.com", "title": "Example", "snippet": "Body"}
    ]
    assert (
        parse_callback_response(
            callback_result_message(request, result),
            request=request,
        )
        == result
    )
    with pytest.raises(DeepResearchCallbackReportedError) as error:
        parse_callback_response(
            callback_error_message(request, "callback_failed", "RuntimeError"),
            request=request,
        )
    assert error.value.code == "callback_failed"

    invalid = callback_request_message(request)
    invalid["unexpected"] = True
    with pytest.raises(DeepResearchProtocolError):
        parse_callback_request(invalid)
    with pytest.raises(DeepResearchProtocolError):
        parse_callback_response(
            {
                **callback_result_message(request, result),
                "message_id": "wrong-message",
            },
            request=request,
        )
    with pytest.raises(DeepResearchProtocolError, match="reserved"):
        DeepResearchCallbackRequest(
            job_id="job-1",
            message_id="terminal",
            operation=DeepResearchCallbackOperation.SEARCH,
            arguments={"query": "topic", "max_results": 1},
        )


def test_image_metadata_allows_path_word_but_rejects_local_filesystem_urls() -> None:
    image = {
        "url": "/outputs/images/job/image_deadbeef_0.png",
        "prompt": "Show the migration path",
        "alt_text": "Illustration of the migration path",
    }
    result = DeepResearchResult(
        query="topic",
        report="report",
        research_result=None,
        sources=(),
        source_urls=(),
        context=(),
        costs=0.0,
        generated_images=(image,),
    )
    assert result.generated_images == (image,)

    for unsafe_url in ("/tmp/image.png", "file:///tmp/image.png"):
        with pytest.raises(
            DeepResearchProtocolError,
            match="filesystem path",
        ):
            DeepResearchResult(
                query="topic",
                report="report",
                research_result=None,
                sources=(),
                source_urls=(),
                context=(),
                costs=0.0,
                generated_images=({**image, "url": unsafe_url},),
            )


def test_version_pinned_adapter_uses_real_0148_public_api() -> None:
    factory = get_gpt_researcher_factory()
    assert GPT_RESEARCHER_REQUIRED_VERSION == "0.14.8"
    assert factory.__name__ == "GPTResearcher"
    for getter in (
        "get_research_sources",
        "get_source_urls",
        "get_research_context",
        "get_costs",
    ):
        assert callable(getattr(factory, getter, None))

    with (
        patch(
            "llm_gateway_core.agents.deep_research_adapter.metadata.version",
            return_value="0.14.9",
        ),
        pytest.raises(DeepResearchUnavailableError, match="required 0.14.8"),
    ):
        get_gpt_researcher_factory()


def test_adapter_collects_only_public_getters() -> None:
    class Researcher:
        sources = "must not be read"
        source_urls = "must not be read"
        context = "must not be read"

        def get_research_sources(self):
            return [{"title": "public"}]

        def get_source_urls(self):
            return ["https://example.com"]

        def get_research_context(self):
            return ["context"]

        def get_costs(self):
            return 0.1

    result = collect_public_result(
        Researcher(),
        query="topic",
        report="report",
        research_result={"ok": True},
    )
    assert result.sources == ({"title": "public"},)
    assert result.context == ("context",)


def test_adapter_normalizes_joined_public_context_and_rejects_invalid_types() -> None:
    class Researcher:
        context: object = "first\nsecond"

        def get_research_sources(self):
            return []

        def get_source_urls(self):
            return []

        def get_research_context(self):
            return self.context

        def get_costs(self):
            return 0.0

    researcher = Researcher()
    result = collect_public_result(
        researcher,
        query="topic",
        report="report",
        research_result=None,
    )
    assert result.context == ("first\nsecond",)

    for invalid_context in ("", (), None):
        researcher.context = invalid_context
        with pytest.raises(ValueError, match="list or non-empty string"):
            collect_public_result(
                researcher,
                query="topic",
                report="report",
                research_result=None,
            )


def test_adapter_constructs_researcher_inside_gateway_tools_context() -> None:
    state = {"inside_tools": False, "ipc_closed": False}

    class FakeIpcClient:
        def __init__(self, _sock, *, job_id):
            assert job_id == _job().job_id

        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            state["ipc_closed"] = True

        def raise_if_failed(self) -> None:
            return None

    class Researcher:
        def __init__(self, **_kwargs):
            assert state["inside_tools"] is True

        async def conduct_research(self):
            return {"ok": True}

        async def write_report(self):
            return "report"

        def get_research_sources(self):
            return []

        def get_source_urls(self):
            return []

        def get_research_context(self):
            return []

        def get_costs(self):
            return 0.0

    @contextmanager
    def gateway_tools(_ipc):
        state["inside_tools"] = True
        try:
            yield
        finally:
            state["inside_tools"] = False

    async def scenario() -> None:
        with (
            patch.object(adapter_module, "DeepResearchIpcClient", FakeIpcClient),
            patch.object(adapter_module, "get_gpt_researcher_factory", return_value=Researcher),
            patch.object(adapter_module, "_gateway_research_tools", gateway_tools),
        ):
            result = await adapter_module.conduct_deep_research_job(
                _job(),
                object(),  # type: ignore[arg-type]
            )
        assert result.report == "report"
        assert state == {"inside_tools": False, "ipc_closed": True}

    run_async(scenario())


@pytest.mark.parametrize("failure_checkpoint", [1, 2, 3])
def test_adapter_checks_sticky_ipc_failure_before_success(
    failure_checkpoint: int,
) -> None:
    state = {"checks": 0, "write_called": False, "ipc_closed": False}
    failure = DeepResearchCallbackReportedError("callback_failed", "TimeoutError")

    class FakeIpcClient:
        def __init__(self, _sock, *, job_id):
            assert job_id == _job().job_id

        async def start(self) -> None:
            return None

        async def aclose(self) -> None:
            state["ipc_closed"] = True

        def raise_if_failed(self) -> None:
            state["checks"] += 1
            if state["checks"] == failure_checkpoint:
                raise failure

    class Researcher:
        def __init__(self, **_kwargs):
            pass

        async def conduct_research(self):
            return {"ok": True}

        async def write_report(self):
            state["write_called"] = True
            return "report"

        def get_research_sources(self):
            return []

        def get_source_urls(self):
            return []

        def get_research_context(self):
            return []

        def get_costs(self):
            return 0.0

    @contextmanager
    def gateway_tools(_ipc):
        yield

    async def scenario() -> None:
        with (
            patch.object(adapter_module, "DeepResearchIpcClient", FakeIpcClient),
            patch.object(
                adapter_module,
                "get_gpt_researcher_factory",
                return_value=Researcher,
            ),
            patch.object(adapter_module, "_gateway_research_tools", gateway_tools),
        ):
            with pytest.raises(DeepResearchCallbackReportedError) as error:
                await adapter_module.conduct_deep_research_job(
                    _job(),
                    object(),  # type: ignore[arg-type]
                )
        assert error.value is failure
        assert state["checks"] == failure_checkpoint
        assert state["write_called"] is (failure_checkpoint > 1)
        assert state["ipc_closed"] is True

    run_async(scenario())


def test_installed_browse_urls_drops_refused_sources_and_keeps_the_rest() -> None:
    import gpt_researcher.agent as agent_module

    refused = DeepResearchCallbackReportedError("callback_failed", "HTTPException")

    class Ipc:
        async def read(self, url: str):
            if url == "https://blocked.example":
                raise refused
            return {"url": url, "title": "Readable", "content": "body"}

    added: list[list[dict[str, object]]] = []
    researcher = SimpleNamespace(add_research_sources=added.append)

    async def scenario() -> None:
        with adapter_module._gateway_research_tools(Ipc()):  # type: ignore[arg-type]
            manager = agent_module.BrowserManager(researcher)
            pages = await manager.browse_urls(
                ["https://blocked.example", "https://readable.example"]
            )
        assert [page["url"] for page in pages] == ["https://readable.example"]
        assert added == [pages]

    run_async(scenario())


def test_installed_browse_urls_still_fails_on_a_broken_callback_channel() -> None:
    import gpt_researcher.agent as agent_module

    broken = DeepResearchProtocolError("callback response has an unknown message id")

    class Ipc:
        async def read(self, url: str):
            if url == "https://broken.example":
                raise broken
            return {"url": url, "title": "Readable", "content": "body"}

    researcher = SimpleNamespace(add_research_sources=lambda _sources: None)

    async def scenario() -> None:
        with adapter_module._gateway_research_tools(Ipc()):  # type: ignore[arg-type]
            manager = agent_module.BrowserManager(researcher)
            with pytest.raises(DeepResearchProtocolError) as error:
                await manager.browse_urls(
                    ["https://broken.example", "https://readable.example"]
                )
        assert error.value is broken

    run_async(scenario())


def test_refused_read_is_not_sticky_while_search_failure_still_is() -> None:
    async def scenario() -> None:
        parent, child = socket.socketpair()
        try:
            child.setblocking(False)
            ipc = adapter_module.DeepResearchIpcClient(child, job_id=_job().job_id)
            await ipc.start()

            async def refuse_next_request() -> None:
                request = parse_callback_request(await _recv_async_frame(parent))
                await asyncio.get_running_loop().sock_sendall(
                    parent,
                    encode_frame(
                        callback_error_message(
                            request,
                            "callback_failed",
                            "HTTPException",
                        )
                    ),
                )

            try:
                parent.setblocking(False)
                read_call = asyncio.create_task(ipc.read("https://blocked.example"))
                await refuse_next_request()
                with pytest.raises(DeepResearchCallbackReportedError):
                    await read_call
                # The refused read left no sticky failure, so the next call
                # goes out on the wire instead of raising locally.
                ipc.raise_if_failed()

                search_call = asyncio.create_task(ipc.search("still usable", 1))
                await refuse_next_request()
                with pytest.raises(DeepResearchCallbackReportedError):
                    await search_call
                with pytest.raises(DeepResearchCallbackReportedError):
                    ipc.raise_if_failed()
            finally:
                await ipc.aclose()
        finally:
            parent.close()
            child.close()

    run_async(scenario())


def test_spawn_runner_survives_a_refused_read_and_returns_the_report() -> None:
    async def scenario() -> None:
        async def handle(request: DeepResearchCallbackRequest):
            if request.operation is DeepResearchCallbackOperation.READ:
                raise HTTPException(
                    status_code=503,
                    detail="YouTube transcript is unavailable for 'https://blocked'.",
                )
            return [{"url": "https://example.com", "title": "T", "snippet": "S"}]

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            result = await runner.run(
                _job("read-failure"),
                callbacks=DeepResearchCallbacks(handle=handle),
            )
        finally:
            await runner.aclose()
        assert result.report == "report after failed read"
        assert runner.active_process_count == 0

    run_async(scenario())


def test_installed_nested_research_search_uses_parent_ipc_and_restores_method() -> None:
    from gpt_researcher.skills.researcher import ResearchConductor

    calls: list[tuple[str, int]] = []

    class Ipc:
        async def search(self, query: str, max_results: int):
            calls.append((query, max_results))
            return [
                {"url": "https://old.example", "title": "Old", "snippet": "Old"},
                {"url": "https://new.example", "title": "New", "snippet": "New"},
                {"url": "https://new.example", "title": "New", "snippet": "New"},
            ]

    researcher = SimpleNamespace(
        cfg=SimpleNamespace(max_search_results_per_query=7),
        visited_urls={"https://old.example"},
        verbose=False,
    )
    conductor = ResearchConductor(researcher)
    original = ResearchConductor._search_relevant_source_urls

    async def scenario() -> None:
        with adapter_module._gateway_research_tools(Ipc()):  # type: ignore[arg-type]
            urls = await conductor._search_relevant_source_urls(
                "nested query",
                ["example.com"],
            )
            assert urls == ["https://new.example"]
        assert ResearchConductor._search_relevant_source_urls is original

    run_async(scenario())
    assert calls == [("nested query", 7)]
    assert "run_coroutine_threadsafe" not in Path(adapter_module.__file__).read_text(
        encoding="utf-8"
    )


def test_real_shaped_planned_image_is_normalized_without_fallback_generation() -> None:
    planned = {
        "url": "/outputs/images/job/image_deadbeef_0.png",
        "prompt": "Show the migration path",
        "alt_text": "Illustration of the migration path",
        "title": "Migration plan",
        "section_hint": "Architecture",
    }

    class ImageGenerator:
        async def plan_and_generate_images(self, **_kwargs):
            return [planned]

    class Researcher:
        available_images: list[dict[str, str]] = []
        image_generator = ImageGenerator()

        def get_research_context(self):
            return "joined research context"

    class Ipc:
        async def image(self, **_kwargs):
            raise AssertionError("already published planned image must be reused")

    async def scenario() -> None:
        researcher = Researcher()
        images = await adapter_module._ensure_required_images(
            researcher,
            ipc=Ipc(),  # type: ignore[arg-type]
            job=_job(),
        )
        assert images == (
            {
                "url": planned["url"],
                "prompt": planned["prompt"],
                "alt_text": planned["alt_text"],
            },
        )
        assert researcher.available_images == list(images)

    run_async(scenario())


def test_spawn_runner_applies_env_before_adapter_import_without_parent_mutation() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        before = dict(os.environ)
        os.environ["LLMGATEWAY_PARENT_ONLY_SECRET"] = "parent-secret"
        expected_parent = dict(os.environ)
        try:
            result = await runner.run(_job())
            assert result.research_result == {
                "fast_llm": "openai:llmgateway/fast",
                "parent_secret_present": False,
            }
            assert dict(os.environ) == expected_parent
            assert runner.active_process_count == 0
        finally:
            os.environ.clear()
            os.environ.update(before)
            await runner.aclose()
        assert runner.state is DeepResearchRunnerState.CLOSED

    run_async(scenario())


def test_spawn_runner_multiplexes_callbacks_out_of_order() -> None:
    async def scenario() -> None:
        completion_order: list[str] = []

        async def handle(request: DeepResearchCallbackRequest):
            if request.operation is DeepResearchCallbackOperation.SEARCH:
                await asyncio.sleep(0.03)
                completion_order.append("search")
                return [
                    {
                        "url": "https://example.com/article",
                        "title": "Search result",
                        "snippet": "Snippet",
                    }
                ]
            if request.operation is DeepResearchCallbackOperation.READ:
                await asyncio.sleep(0.01)
                completion_order.append("read")
                return {
                    "url": "https://example.com/article",
                    "title": "Article",
                    "content": "Article body",
                }
            completion_order.append("image")
            return [
                {
                    "url": "/outputs/images/job/image_deadbeef_0.png",
                    "prompt": "callback image",
                    "alt_text": "Illustration: callback image",
                }
            ]

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            result = await runner.run(
                _job("callbacks"),
                callbacks=DeepResearchCallbacks(handle=handle),
            )
        finally:
            await runner.aclose()
        assert completion_order == ["image", "read", "search"]
        assert result.report == "callback report"
        assert result.costs == 9.99
        assert result.generated_images == (
            {
                "url": "/outputs/images/job/image_deadbeef_0.png",
                "prompt": "callback image",
                "alt_text": "Illustration: callback image",
            },
        )

    run_async(scenario())


def test_spawn_runner_returns_secret_safe_callback_failure() -> None:
    async def scenario() -> None:
        async def fail(_request: DeepResearchCallbackRequest):
            raise RuntimeError("must-not-cross-the-socket")

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            with pytest.raises(DeepResearchProcessError) as error:
                await runner.run(
                    _job("callbacks"),
                    callbacks=DeepResearchCallbacks(handle=fail),
                )
        finally:
            await runner.aclose()
        assert error.value.code == "callback_failed"
        assert "must-not-cross" not in str(error.value)

    run_async(scenario())


def test_swallowed_child_callback_failure_remains_terminal_failure() -> None:
    async def scenario() -> None:
        async def fail(_request: DeepResearchCallbackRequest):
            raise RuntimeError("must-not-be-swallowed")

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            with pytest.raises(DeepResearchProcessError) as error:
                await runner.run(
                    _job("swallowed-callback"),
                    callbacks=DeepResearchCallbacks(handle=fail),
                )
        finally:
            await runner.aclose()
        assert error.value.code == "callback_failed"
        assert "must-not-be-swallowed" not in str(error.value)
        assert runner.active_process_count == 0

    run_async(scenario())


def test_mixed_callback_error_does_not_poison_delayed_matching_response() -> None:
    async def scenario() -> None:
        completed: list[str] = []

        async def handle(request: DeepResearchCallbackRequest):
            if request.operation is DeepResearchCallbackOperation.SEARCH:
                raise RuntimeError("fast callback failure")
            await asyncio.sleep(0.03)
            completed.append(request.message_id)
            return {
                "url": "https://example.com/mixed",
                "title": "Mixed article",
                "content": "Delayed success",
            }

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            with pytest.raises(DeepResearchProcessError) as error:
                await runner.run(
                    _job("mixed-callback"),
                    callbacks=DeepResearchCallbacks(handle=handle),
                )
        finally:
            await runner.aclose()
        assert error.value.code == "callback_failed"
        assert completed == ["message-2"]
        assert runner.active_process_count == 0

    run_async(scenario())


def test_callback_failure_reason_stays_in_the_parent_log(caplog) -> None:
    async def scenario() -> None:
        async def fail(_request: DeepResearchCallbackRequest):
            raise HTTPException(
                status_code=503,
                detail="Web read 'llmgateway/web-read' failed for 'https://example.com'.",
            )

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            with caplog.at_level(logging.ERROR, logger=process_module.__name__):
                with pytest.raises(DeepResearchProcessError) as error:
                    await runner.run(
                        _job("callbacks"),
                        callbacks=DeepResearchCallbacks(handle=fail),
                    )
        finally:
            await runner.aclose()
        assert error.value.code == "callback_failed"
        assert "llmgateway/web-read" not in str(error.value)
        assert "deep-research callback 'search' failed for job" in caplog.text
        assert "Web read 'llmgateway/web-read' failed" in caplog.text

    run_async(scenario())


def test_hung_callback_times_out_and_releases_process_task_and_permit() -> None:
    with pytest.raises(ValueError, match="callback_timeout_seconds"):
        DeepResearchProcessRunner(callback_timeout_seconds=0)

    async def scenario() -> None:
        never = asyncio.Event()

        async def hang(_request: DeepResearchCallbackRequest):
            await never.wait()

        runner = DeepResearchProcessRunner(
            capacity=1,
            admission_timeout_seconds=0.2,
            callback_timeout_seconds=0.02,
            _adapter_module=FIXTURE_ADAPTER,
        )
        await runner.start()
        job_task = asyncio.create_task(
            runner.run(
                _job("callbacks"),
                callbacks=DeepResearchCallbacks(handle=hang),
            )
        )
        try:
            for _attempt in range(500):
                if runner.active_process_count:
                    break
                await asyncio.sleep(0.001)
            assert runner.active_process_count == 1
            child_pid = runner.active_process_ids[0]

            with pytest.raises(DeepResearchProcessError) as error:
                await job_task
            assert error.value.code == "callback_failed"
            assert runner.active_process_count == 0
            assert not _pid_exists(child_pid)

            result = await runner.run(_job("permit-reused"))
            assert result.report == "fixture report"
            task_prefixes = (
                "deep-research-admission-",
                "deep-research-callback-",
                "deep-research-cleanup-",
                "deep-research-reap-",
                "deep-research-receive-",
            )
            assert not [
                task.get_name()
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith(task_prefixes)
            ]
        finally:
            if not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
            await runner.aclose()

    run_async(scenario())


def test_terminal_before_callback_drain_is_protocol_error_and_cleans_up() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        cancelled = asyncio.Event()
        completed: list[str] = []
        never = asyncio.Event()

        async def handle(request: DeepResearchCallbackRequest):
            started.set()
            try:
                await never.wait()
                completed.append(request.message_id)
            finally:
                cancelled.set()

        runner = DeepResearchProcessRunner(
            admission_timeout_seconds=0.2,
            callback_timeout_seconds=1.0,
            _adapter_module=FIXTURE_ADAPTER,
        )
        await runner.start()
        job_task = asyncio.create_task(
            runner.run(
                _job("terminal-before-callback"),
                callbacks=DeepResearchCallbacks(handle=handle),
            )
        )
        try:
            for _attempt in range(500):
                if runner.active_process_count:
                    break
                await asyncio.sleep(0.001)
            assert runner.active_process_count == 1
            child_pid = runner.active_process_ids[0]

            with pytest.raises(DeepResearchProcessError) as error:
                await job_task
            assert error.value.code == "protocol_error"
            assert started.is_set()
            assert cancelled.is_set()
            assert completed == []
            assert runner.active_process_count == 0
            assert not _pid_exists(child_pid)
            assert not [
                task.get_name()
                for task in asyncio.all_tasks()
                if task is not asyncio.current_task()
                and task.get_name().startswith("deep-research-callback-")
            ]
        finally:
            if not job_task.done():
                job_task.cancel()
                await asyncio.gather(job_task, return_exceptions=True)
            await runner.aclose()

    run_async(scenario())


def test_capacity_timeout_crash_and_cancel_reap_children() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            capacity=1,
            admission_timeout_seconds=0.05,
            _adapter_module=FIXTURE_ADAPTER,
        )
        await runner.start()
        blocked: asyncio.Task[DeepResearchResult] | None = None
        try:
            blocked = asyncio.create_task(runner.run(_job("block")))
            for _attempt in range(500):
                if runner.active_process_count:
                    break
                await asyncio.sleep(0.001)
            assert runner.active_process_count == 1
            blocked_pid = runner.active_process_ids[0]
            with pytest.raises(DeepResearchAdmissionTimeout):
                await runner.run(_job("second"))
            blocked.cancel()
            with pytest.raises(asyncio.CancelledError):
                await blocked
            assert runner.active_process_count == 0
            assert not _pid_exists(blocked_pid)

            with pytest.raises(DeepResearchProcessError) as crashed:
                await runner.run(_job("crash"))
            assert crashed.value.code == "child_crashed"
            assert crashed.value.exit_code == 23
            assert runner.active_process_count == 0
        finally:
            if blocked is not None and not blocked.done():
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
            await runner.aclose()

    run_async(scenario())


def test_runner_reaps_descendant_process_groups_on_success_and_crash(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        try:
            success_pid_path = tmp_path / "success.pid"
            result = await runner.run(_job(f"grandchild-success:{success_pid_path}"))
            success_pid = int(success_pid_path.read_text(encoding="utf-8"))
            assert result.research_result["grandchild_pid"] == success_pid
            assert not _pid_exists(success_pid)

            crash_pid_path = tmp_path / "crash.pid"
            with pytest.raises(DeepResearchProcessError) as crashed:
                await runner.run(_job(f"grandchild-crash:{crash_pid_path}"))
            assert crashed.value.code == "child_crashed"
            crash_pid = int(crash_pid_path.read_text(encoding="utf-8"))
            assert not _pid_exists(crash_pid)
        finally:
            await runner.aclose()

    run_async(scenario())


def test_invalid_result_truncated_frame_and_active_shutdown_fail_closed() -> None:
    async def scenario() -> None:
        reader, writer = socket.socketpair()
        reader.setblocking(False)
        try:
            writer.sendall((8).to_bytes(4, "big") + b"{}")
            writer.close()
            with pytest.raises(DeepResearchProtocolError, match="unexpected EOF"):
                await _recv_async_frame(reader)
        finally:
            reader.close()
            writer.close()

        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        with pytest.raises(DeepResearchProcessError) as invalid:
            await runner.run(_job("invalid-result"))
        assert invalid.value.code == "invalid_result"

        blocked = asyncio.create_task(runner.run(_job("block")))
        for _attempt in range(500):
            if runner.active_process_count:
                break
            await asyncio.sleep(0.001)
        assert runner.active_process_count == 1
        blocked_pid = runner.active_process_ids[0]
        await runner.aclose()
        result = await asyncio.gather(blocked, return_exceptions=True)
        assert isinstance(result[0], DeepResearchProcessError)
        assert runner.state is DeepResearchRunnerState.CLOSED
        assert runner.active_process_count == 0
        assert not _pid_exists(blocked_pid)

    run_async(scenario())


def test_shutdown_closes_post_admission_race_without_spawning() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        admitted = asyncio.Event()
        release = asyncio.Event()
        original_acquire = runner._acquire_slot

        async def paused_acquire() -> None:
            await original_acquire()
            admitted.set()
            await release.wait()

        with patch.object(runner, "_acquire_slot", new=paused_acquire):
            run_task = asyncio.create_task(runner.run(_job("race")))
            await asyncio.wait_for(admitted.wait(), timeout=1)
            close_task = asyncio.create_task(runner.aclose())
            while runner.state is DeepResearchRunnerState.RUNNING:
                await asyncio.sleep(0)
            assert runner.active_process_count == 0
            release.set()
            with pytest.raises(DeepResearchProcessClosed):
                await run_task
            await close_task
        assert runner.state is DeepResearchRunnerState.CLOSED
        assert runner.active_process_count == 0

    run_async(scenario())


def test_cancellation_after_admission_completion_releases_slot() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        run_task: asyncio.Task[DeepResearchResult] | None = None
        original_acquire = runner._acquire_slot

        async def acquire_then_cancel_caller() -> None:
            await original_acquire()
            asyncio.get_running_loop().call_soon(
                lambda: run_task.cancel() if run_task is not None else None
            )

        try:
            with patch.object(
                runner,
                "_acquire_slot",
                new=acquire_then_cancel_caller,
            ):
                run_task = asyncio.create_task(runner.run(_job("cancel-race")))
                with pytest.raises(asyncio.CancelledError):
                    await run_task
            assert runner.active_process_count == 0
            assert (await runner.run(_job())).report == "fixture report"
        finally:
            await runner.aclose()

    run_async(scenario())


def test_repeated_cancellation_cannot_interrupt_kill_and_reap(tmp_path: Path) -> None:
    async def scenario() -> None:
        marker = tmp_path / "ignore-term.pid"
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        blocked = asyncio.create_task(runner.run(_job(f"ignore-term:{marker}")))
        try:
            for _attempt in range(1000):
                if marker.exists():
                    break
                await asyncio.sleep(0.001)
            assert marker.exists()
            child_pid = int(marker.read_text(encoding="utf-8"))
            with patch.object(
                process_module,
                "DEEP_RESEARCH_TERMINATE_GRACE_SECONDS",
                0.2,
            ):
                blocked.cancel()
                await asyncio.sleep(0.01)
                blocked.cancel()
                with pytest.raises(asyncio.CancelledError):
                    await blocked
            assert runner.active_process_count == 0
            assert not _pid_exists(child_pid)
            assert (await runner.run(_job())).report == "fixture report"
        finally:
            if not blocked.done():
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
            await runner.aclose()

    run_async(scenario())


def test_parent_import_graph_and_runner_source_have_no_thread_fallback() -> None:
    project_root = Path(__file__).resolve().parents[1]
    completed = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import main; import llm_gateway_core.api.v1.web; "
                "assert not any(n == 'gpt_researcher' or n.startswith('gpt_researcher.') "
                "for n in sys.modules)"
            ),
        ],
        cwd=project_root,
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    assert completed.returncode == 0, completed.stderr

    source = (
        project_root / "llm_gateway_core/services/deep_research_process.py"
    ).read_text(encoding="utf-8")
    for forbidden in (
        "to_thread",
        "run_in_executor",
        "ThreadPool",
        "ProcessPool",
        "multiprocessing.Queue",
    ):
        assert forbidden not in source


def test_cleanup_failure_after_success_still_releases_slot_and_bookkeeping(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            capacity=1,
            admission_timeout_seconds=0.2,
            _adapter_module=FIXTURE_ADAPTER,
        )
        await runner.start()
        real_finish_child = process_module._finish_child

        async def reap_timeout_after_real_cleanup(owner):
            await real_finish_child(owner)
            owner.closed = False
            raise DeepResearchProcessError("reap_timeout")

        try:
            caplog.set_level("ERROR")
            with patch.object(
                process_module,
                "_finish_child",
                new=reap_timeout_after_real_cleanup,
            ):
                with pytest.raises(DeepResearchProcessError) as error:
                    await runner.run(_job())
            assert error.value.code == "reap_timeout"
            assert runner.active_process_count == 0
            assert runner._owners == set()
            assert runner._active_runs == 0
            assert runner._active_runs_drained.is_set()
            assert any(
                record.levelname == "ERROR" and "cleanup failed" in record.getMessage()
                for record in caplog.records
            )

            result = await runner.run(_job("after-cleanup-failure"))
            assert result.report == "fixture report"
        finally:
            await runner.aclose()

    run_async(scenario())


def test_aclose_reaches_closed_even_if_owner_cleanup_fails_during_shutdown() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        real_finish_child = process_module._finish_child

        async def reap_timeout_after_real_cleanup(owner):
            await real_finish_child(owner)
            owner.closed = False
            raise DeepResearchProcessError("reap_timeout")

        blocked = asyncio.create_task(runner.run(_job("block")))
        try:
            for _attempt in range(500):
                if runner.active_process_count:
                    break
                await asyncio.sleep(0.001)
            assert runner.active_process_count == 1
            blocked_pid = runner.active_process_ids[0]

            with patch.object(
                process_module,
                "_finish_child",
                new=reap_timeout_after_real_cleanup,
            ):
                await runner.aclose()

            assert runner.state is DeepResearchRunnerState.CLOSED
            assert runner.active_process_count == 0
            assert not _pid_exists(blocked_pid)

            result = await asyncio.gather(blocked, return_exceptions=True)
            assert isinstance(result[0], DeepResearchProcessError)
        finally:
            if not blocked.done():
                blocked.cancel()
                await asyncio.gather(blocked, return_exceptions=True)
            await runner.aclose()

    run_async(scenario())


def test_finish_close_tolerates_owner_closed_by_its_own_run_completion() -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            capacity=1,
            admission_timeout_seconds=0.2,
            _adapter_module=FIXTURE_ADAPTER,
        )
        await runner.start()
        captured: list = []
        real_spawn_owner = DeepResearchProcessRunner._spawn_owner

        def spy_spawn_owner(self, job):
            owner = real_spawn_owner(self, job)
            captured.append(owner)
            return owner

        try:
            with patch.object(
                DeepResearchProcessRunner,
                "_spawn_owner",
                new=spy_spawn_owner,
            ):
                result = await runner.run(_job())
            assert result.report == "fixture report"
            assert len(captured) == 1
            stale_owner = captured[0]
            # The owner's own run() call already closed and discarded it.
            assert stale_owner.closed is True
            assert stale_owner not in runner._owners

            # Reproduces the aclose()/run() race: aclose() snapshots
            # self._owners before the owner's own run() finishes closing it,
            # so _finish_close() can still observe a stale, already-closed
            # reference. Reading .pid on it must not raise ValueError.
            await runner._finish_close((stale_owner,))
            assert runner._state is DeepResearchRunnerState.CLOSED
        finally:
            await runner.aclose()

    run_async(scenario())


def test_finish_close_logs_cancelled_error_outcome_from_owner_finish(
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        runner = DeepResearchProcessRunner(
            _adapter_module=FIXTURE_ADAPTER,
            admission_timeout_seconds=0.2,
        )
        await runner.start()
        owner = runner._spawn_owner(_job("cancelled-outcome"))
        runner._owners.add(owner)
        real_finish_child = process_module._finish_child

        async def cancelled_after_real_cleanup(owner):
            await real_finish_child(owner)
            raise asyncio.CancelledError()

        try:
            caplog.set_level("ERROR")
            with patch.object(
                process_module,
                "_finish_child",
                new=cancelled_after_real_cleanup,
            ):
                # gather(..., return_exceptions=True) hands a self-raised
                # CancelledError back as a plain result value (not a
                # propagated cancellation), so _finish_close must recognize
                # it as BaseException to log it instead of swallowing it.
                await runner._finish_close((owner,))
            assert runner._state is DeepResearchRunnerState.CLOSED
            assert any(
                record.levelname == "ERROR" and "cleanup failed" in record.getMessage()
                for record in caplog.records
            )
        finally:
            await runner.aclose()

    run_async(scenario())
