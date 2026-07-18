import asyncio
import copy
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.services import http_client_factory
from llm_gateway_core.agents.deep_research import DeepResearchManager
from llm_gateway_core.services.request_handler import make_llm_request
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from tests._async_compat import run_async
from tests.test_lifespan_app_services import _lifespan_environment


class _FakeResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = ""

    def json(self):
        return self._payload


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200):
        self._chunks = chunks
        self.status_code = status_code

    async def aread(self):
        return b""

    async def aiter_raw(self, *, chunk_size=None):
        for chunk in self._chunks:
            if chunk_size is None:
                yield chunk
                continue
            for offset in range(0, len(chunk), chunk_size):
                yield chunk[offset:offset + chunk_size]


class _FakeStreamContextManager:
    def __init__(self, response: _FakeStreamResponse):
        self._response = response
        self.exited = False

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        self.exited = True
        return False


async def _collect_streaming_response_body(response: StreamingResponse) -> bytes:
    body = []
    async for chunk in response.body_iterator:
        body.append(chunk)
    return b"".join(body)


async def _wait_for_mock_call(mock: Mock) -> None:
    for _attempt in range(20):
        if mock.called:
            return
        await asyncio.sleep(0)
    raise AssertionError(f"{mock!r} was not called")


async def _make_stream_request_and_collect(
    fake_client: Mock,
    request_payload: dict,
) -> tuple[StreamingResponse | None, str | None, bytes | None]:
    capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)
    response_data, error_detail = await make_llm_request(
        fake_client,
        "https://example.com/chat/completions",
        {"Content-Type": "application/json"},
        request_payload,
        True,
        stream_observation_capacity=capacity,
        stream_event_max_bytes=1024,
    )

    collected = None
    if isinstance(response_data, StreamingResponse):
        collected = await _collect_streaming_response_body(response_data)

    return response_data, error_detail, collected


class SharedHttpClientTests(unittest.TestCase):
    def test_run_usage_stats_cleanup_loop_deletes_records_older_than_90_days(self):
        fake_tokens_usage_db = Mock()
        fake_fallback_events_db = Mock()
        fake_rejections_db = Mock()

        async def fake_to_thread(func, *args, **kwargs):
            return func(*args, **kwargs)

        async def scenario():
            with patch("asyncio.to_thread", side_effect=fake_to_thread):
                cleanup_task = asyncio.create_task(
                    main.run_usage_stats_cleanup_loop(
                        fake_tokens_usage_db,
                        fake_fallback_events_db,
                        fake_rejections_db,
                        retention_days=90,
                        interval_seconds=3600,
                    )
                )
                await _wait_for_mock_call(fake_rejections_db.cleanup_old_records)
                fake_tokens_usage_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                fake_fallback_events_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                fake_rejections_db.cleanup_old_records.assert_called_once_with(retention_days=90)
                cleanup_task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await cleanup_task

        run_async(scenario())

    def test_run_budget_reset_loop_delegates_to_accounting_service(self):
        accounting_service = Mock()
        accounting_service.reset_due_budgets = AsyncMock()

        async def scenario():
            reset_task = asyncio.create_task(
                main.run_budget_reset_loop(
                    accounting_service,
                    interval_seconds=3600,
                )
            )
            await _wait_for_mock_call(accounting_service.reset_due_budgets)
            accounting_service.reset_due_budgets.assert_awaited_once()
            moment = accounting_service.reset_due_budgets.await_args.kwargs["now"]
            self.assertIsNotNone(moment.tzinfo)
            self.assertEqual(moment.utcoffset().total_seconds(), 0)
            reset_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await reset_task

        run_async(scenario())

    def test_select_capability_autofill_interval_short_polls_on_cold_start(self):
        # Official OpenRouter provider configured, but its capability index has not
        # been populated yet by the (independent) OpenRouter refresh loop: the next
        # materialize() run must not wait a full 8h for the OpenRouter fallback
        # source to become usable.
        self.assertEqual(
            main._select_capability_autofill_interval(
                openrouter_configured=True,
                capability_index_empty=True,
                default_interval_seconds=8 * 60 * 60,
            ),
            main.CAPABILITY_AUTOFILL_COLD_START_INTERVAL_SECONDS,
        )

    def test_run_capability_autofill_loop_survives_a_failing_iteration(self):
        # A raising get_status()/get_capability_index() (or any other iteration
        # failure) must be absorbed and retried on the default cadence instead
        # of killing the supervised task for the rest of the process lifetime
        # (a dead supervised task turns /health into a 503).
        iteration_reached = asyncio.Event()

        async def failing_get_status():
            iteration_reached.set()
            raise RuntimeError("simulated iteration failure")

        openrouter_service = Mock()
        openrouter_service.get_status = failing_get_status
        capability_autofill_service = Mock()
        capability_autofill_service.materialize = AsyncMock()

        async def scenario():
            loop_task = asyncio.create_task(
                main.run_capability_autofill_loop(
                    capability_autofill_service,
                    runtime_manager=Mock(),
                    config_update_coordinator=Mock(),
                    shared_http_client=Mock(),
                    openrouter_free_models_service=openrouter_service,
                )
            )
            await asyncio.wait_for(iteration_reached.wait(), timeout=5)
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(loop_task.done())
            capability_autofill_service.materialize.assert_not_awaited()
            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task

        run_async(scenario())

    def test_select_capability_autofill_interval_uses_default_once_catalog_is_populated(self):
        self.assertEqual(
            main._select_capability_autofill_interval(
                openrouter_configured=True,
                capability_index_empty=False,
                default_interval_seconds=8 * 60 * 60,
            ),
            8 * 60 * 60,
        )

    def test_select_capability_autofill_interval_uses_default_when_openrouter_not_configured(self):
        # An empty index is expected (and permanent) when there is no official
        # OpenRouter provider/key to refresh it from -- short-polling would just
        # spin forever for no benefit.
        self.assertEqual(
            main._select_capability_autofill_interval(
                openrouter_configured=False,
                capability_index_empty=True,
                default_interval_seconds=8 * 60 * 60,
            ),
            8 * 60 * 60,
        )

    def test_select_free_llm_catalog_interval_short_polls_until_first_snapshot(self):
        # No snapshot yet: poll frequently instead of waiting a full 24h for
        # the first successful fetch.
        self.assertEqual(
            main._select_free_llm_catalog_interval(
                snapshot_present=False,
                default_interval_seconds=24 * 60 * 60,
            ),
            main.FREE_LLM_CATALOG_COLD_START_INTERVAL_SECONDS,
        )

    def test_select_free_llm_catalog_interval_uses_default_once_snapshot_exists(self):
        self.assertEqual(
            main._select_free_llm_catalog_interval(
                snapshot_present=True,
                default_interval_seconds=24 * 60 * 60,
            ),
            24 * 60 * 60,
        )

    def test_run_free_llm_catalog_loop_survives_a_failing_iteration(self):
        # A raising refresh_once()/get_status() must be absorbed and retried
        # on the default cadence instead of killing the supervised task for
        # the rest of the process lifetime.
        iteration_reached = asyncio.Event()

        async def failing_refresh_once(_http_client):
            iteration_reached.set()
            raise RuntimeError("simulated iteration failure")

        service = Mock()
        service.refresh_once = failing_refresh_once
        service.get_status = AsyncMock()

        async def scenario():
            loop_task = asyncio.create_task(
                main.run_free_llm_catalog_loop(
                    service,
                    http_client=Mock(),
                )
            )
            await asyncio.wait_for(iteration_reached.wait(), timeout=5)
            for _ in range(10):
                await asyncio.sleep(0)
            self.assertFalse(loop_task.done())
            service.get_status.assert_not_awaited()
            loop_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await loop_task

        # conftest.py defaults FREE_LLM_CATALOG_ENABLED to "false" for tests;
        # this test exercises the enabled fetch path.
        with patch.object(main.settings, "free_llm_catalog_enabled", True):
            run_async(scenario())

    def test_create_shared_http_client_configures_finite_timeouts(self):
        http_client = main.create_shared_http_client()
        self.assertEqual(
            http_client.timeout.connect,
            http_client_factory.HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS,
        )
        self.assertEqual(http_client_factory.HTTP_CLIENT_READ_TIMEOUT_SECONDS, 500.0)
        self.assertEqual(
            http_client.timeout.read,
            http_client_factory.HTTP_CLIENT_READ_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            http_client.timeout.write,
            http_client_factory.HTTP_CLIENT_WRITE_TIMEOUT_SECONDS,
        )
        self.assertEqual(
            http_client.timeout.pool,
            http_client_factory.HTTP_CLIENT_POOL_TIMEOUT_SECONDS,
        )
        run_async(http_client.aclose())

    def test_resolve_write_batcher_db_path_falls_back_to_the_configured_state_dir(self):
        """A db_path that is not a path sends the batcher to GATEWAY_DB_DIR.

        The fallback used to name the checkout's own ``db/`` outright, so a
        deployment that moved its state elsewhere still got its usage writes
        aimed back at the source tree.
        """
        fake_tokens_usage_db = Mock()  # db_path is an auto-created Mock, not a path

        with tempfile.TemporaryDirectory() as state_dir:
            with patch.dict(os.environ, {"GATEWAY_DB_DIR": state_dir}):
                resolved = main.resolve_write_batcher_db_path(fake_tokens_usage_db)

            self.assertEqual(resolved, Path(state_dir) / "tokens_usage.db")

    def test_resolve_write_batcher_db_path_accepts_real_path(self):
        fake_tokens_usage_db = Mock()
        fake_tokens_usage_db.db_path = Path("/tmp/custom-tokens-usage.db")

        resolved = main.resolve_write_batcher_db_path(fake_tokens_usage_db)

        self.assertEqual(resolved, Path("/tmp/custom-tokens-usage.db"))

    def test_lifespan_creates_and_closes_shared_http_client_once(self):
        with _lifespan_environment() as env:
            with TestClient(main.app) as client:
                services = client.app.state.services
                self.assertIs(services.http_client, env.shared_clients[0])
                self.assertEqual(services.task_supervisor.task_count, 5)
                self.assertEqual(services.runtime_manager.current_generation, 1)

        self.assertTrue(services.task_supervisor.closed)
        self.assertEqual(services.task_supervisor.task_count, 0)
        env.shared_clients[0].aclose.assert_awaited_once()

    def test_make_llm_request_uses_injected_client(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(return_value=_FakeResponse({"id": "response-id"}))
        request_payload = {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]}
        original_payload = copy.deepcopy(request_payload)

        with patch("llm_gateway_core.services.request_handler.httpx.AsyncClient", side_effect=AssertionError("AsyncClient constructor must not be used")):
            response_data, error_detail = run_async(
                make_llm_request(
                    fake_client,
                    "https://example.com/chat/completions",
                    {"Content-Type": "application/json"},
                    request_payload,
                    False,
                )
            )

        self.assertEqual(response_data, {"id": "response-id"})
        self.assertIsNone(error_detail)
        self.assertEqual(request_payload, original_payload)
        fake_client.post.assert_awaited_once()
        self.assertEqual(fake_client.post.await_args.args[0], "https://example.com/chat/completions")
        self.assertEqual(fake_client.post.await_args.kwargs["headers"], {"Content-Type": "application/json"})
        self.assertEqual(fake_client.post.await_args.kwargs["json"], request_payload)
        self.assertNotIn("content", fake_client.post.await_args.kwargs)
        self.assertNotIn("timeout", fake_client.post.await_args.kwargs)

    def test_make_llm_request_stream_uses_client_defaults_without_timeout_override(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n']
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))
        request_payload = {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]}
        original_payload = copy.deepcopy(request_payload)

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                request_payload,
                True,
                stream_observation_capacity=StreamObservationCapacity(
                    max_items=4,
                    max_bytes=4096,
                ),
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertEqual(request_payload, original_payload)
        fake_client.stream.assert_called_once()
        self.assertNotIn("timeout", fake_client.stream.call_args.kwargs)

    def test_make_llm_request_stream_without_usage_finishes_cleanly(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertEqual(
            collected,
            b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n'
            b"data: [DONE]\n\n",
        )

    def test_make_llm_request_stream_with_only_done_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([b"data: [DONE]\n\n"])
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_with_only_role_chunk_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"role":"assistant"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_closes_stream_context_on_downstream_stream_error_status(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([], status_code=400)
        stream_response.aread = AsyncMock(return_value=b"boom")
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                True,
                stream_observation_capacity=StreamObservationCapacity(
                    max_items=4,
                    max_bytes=4096,
                ),
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Upstream request failed with HTTP status 400.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_preserves_keepalive_before_first_data(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b": ping\n\n",
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b"data: [DONE]\n\n",
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertTrue(collected.startswith(b": ping\n\n"))
        self.assertIn(b'"content":"hello"', collected)

    def test_make_llm_request_stream_with_only_keepalive_returns_error(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse([b": ping\n\n"])
        stream_context = _FakeStreamContextManager(stream_response)
        fake_client.stream = Mock(return_value=stream_context)

        response_data, error_detail, _ = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream ended before any content chunks were received.")
        self.assertTrue(stream_context.exited)

    def test_make_llm_request_stream_stops_after_error_chunk_without_code(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        stream_response = _FakeStreamResponse(
            [
                b'data: {"id":"chunk-1","choices":[{"delta":{"content":"hello"}}]}\n\n',
                b'data: {"error":{"message":"boom"}}\n\n',
                b'data: {"id":"chunk-2","choices":[{"delta":{"content":"after-error"}}]}\n\n',
            ]
        )
        fake_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response_data, error_detail, collected = run_async(
            _make_stream_request_and_collect(
                fake_client,
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
            )
        )

        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(error_detail)
        self.assertIn(b'"content":"hello"', collected)
        self.assertIn(b'"error":{"message":"boom"}', collected)
        self.assertNotIn(b"after-error", collected)

    def test_make_llm_request_returns_error_on_provider_timeout(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(
            side_effect=httpx.ReadTimeout(
                "provider timed out",
                request=httpx.Request("POST", "https://example.com/chat/completions"),
            )
        )

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                False,
            )
        )

        self.assertIsNone(response_data)
        self.assertIsNotNone(error_detail)
        self.assertIn("ReadTimeout connecting to https://example.com/chat/completions", error_detail)
        self.assertIn("provider timed out", error_detail)

    def test_make_llm_request_preserves_timeout_type_when_exception_message_is_empty(self):
        fake_client = Mock(spec=httpx.AsyncClient)
        fake_client.post = AsyncMock(
            side_effect=httpx.ReadTimeout(
                "",
                request=httpx.Request("POST", "https://example.com/chat/completions"),
            )
        )

        response_data, error_detail = run_async(
            make_llm_request(
                fake_client,
                "https://example.com/chat/completions",
                {"Content-Type": "application/json"},
                {"model": "demo-model", "messages": [{"role": "user", "content": "hello"}]},
                False,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "ReadTimeout connecting to https://example.com/chat/completions",
        )


class DeepResearchWorkerHttpClientTests(unittest.TestCase):
    def test_conduct_deep_research_passes_http_client_to_install_gateway_image_generator(self):
        """The call site in conduct_deep_research must always pass http_client to
        _install_gateway_image_generator when image generation is enabled.
        This ensures GatewayImageGenerator reuses the per-worker client instead of
        creating a new one per image generation call."""
        install_calls: list[dict] = []

        def fake_install(researcher, model, *, http_client=None):
            install_calls.append({"http_client": http_client})

        class _FakeResearcher:
            env_snapshot = {}
            image_provider = None
            context = ["context"]
            available_images = []
            sources = []
            source_urls = []
            costs = None

            def __init__(self, *, query, report_type, verbose):
                self.image_generator = _FakeImageGenerator()

            async def conduct_research(self):
                return "result"

            async def write_report(self):
                return "report"

            def get_costs(self):
                return self.costs

            def get_research_sources(self):
                return self.sources

            def get_source_urls(self):
                return self.source_urls

            def get_research_context(self):
                return self.context

            def _generate_research_id(self):
                return "rid"

        class _FakeImageGenerator:
            image_provider = None

            def is_enabled(self):
                return True

            async def plan_and_generate_images(self, **kwargs):
                return [{"url": "/outputs/images/rid/img.png", "prompt": "p", "alt_text": "a"}]

        class _FakeManager(DeepResearchManager):
            def _get_researcher_factory(self):
                return _FakeResearcher

        with patch("llm_gateway_core.agents.deep_research._install_gateway_image_generator", side_effect=fake_install):
            run_async(
                _FakeManager().conduct_deep_research(
                    query="test",
                    fast_model="model",
                    smart_model="model",
                    strategic_model="model",
                    image_generation_enabled=True,
                    image_generation_model="img-model",
                    image_generation_size="512x512",
                )
            )

        self.assertEqual(len(install_calls), 1, "Expected exactly one call to _install_gateway_image_generator")
        self.assertIsInstance(
            install_calls[0]["http_client"],
            httpx.AsyncClient,
            "_install_gateway_image_generator must receive a live httpx.AsyncClient, not None",
        )


if __name__ == "__main__":
    unittest.main()
