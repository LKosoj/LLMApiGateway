import asyncio
import base64
import json
import os
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import ANY, AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

import main
from llm_gateway_core.agents import web_research as web_research_agent
from llm_gateway_core.api.v1 import web as web_api
from llm_gateway_core.api.v1 import web_adapters as web_adapters_owner
from llm_gateway_core.api.v1 import web_extraction as web_extraction_owner
from llm_gateway_core.api.v1 import web_research_orchestration as web_research_owner
from llm_gateway_core.api.v1 import web_safe_fetch as web_safe_fetch_owner
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.config.settings import settings
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.accounting_admission import (
    take_accounting_request_context,
)
from llm_gateway_core.services.accounting import (
    DEFAULT_OPERATION_COST_USD,
    AccountingReservation,
    AccountingUsage,
)
from llm_gateway_core.services.deep_research_accounting import (
    DeepResearchContextTokenCodec,
)
from llm_gateway_core.services.deep_research_process import DeepResearchProcessRunner
from llm_gateway_core.services.deep_research_protocol import (
    DeepResearchCallbackOperation,
    DeepResearchCallbackRequest,
    DeepResearchJob,
    DeepResearchResult,
)
from llm_gateway_core.utils import zai_mcp as zai_mcp_module
from tests._async_compat import run_async
from tests.web_accounting_test_support import install_web_accounting_passthrough


class _FakeMCPResponse:
    """Lightweight stand-in for httpx.Response used by Z.AI MCP fakes."""

    def __init__(self, *, status_code: int = 200, headers: dict | None = None, text: str = ""):
        self.status_code = status_code
        self.headers = headers or {}
        self.text = text

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.text) if self.text else {}


def _make_zai_mcp_handler(*, payload_obj):
    """Return a coroutine handler that fakes the 3-step Z.AI MCP protocol.

    `payload_obj` is the value the tool would return; it gets JSON-encoded
    twice (once for the SSE envelope, once for the inner text content) to
    match what the real Z.AI MCP servers send.
    """
    session_counter = {"n": 0}

    async def handler(url, *, headers=None, json=None, **_kwargs):  # noqa: A002 - matches httpx api
        method = (json or {}).get("method", "")
        if method == "initialize":
            session_counter["n"] += 1
            envelope = {
                "jsonrpc": "2.0",
                "id": (json or {}).get("id"),
                "result": {
                    "protocolVersion": "2024-11-05",
                    "capabilities": {"tools": {"listChanged": True}},
                    "serverInfo": {"name": "fake", "version": "0.0"},
                },
            }
            return _FakeMCPResponse(
                headers={"mcp-session-id": f"sid-{session_counter['n']}"},
                text=f"data: {jsonlib_dumps(envelope)}\n",
            )
        if method == "notifications/initialized":
            return _FakeMCPResponse(text="")
        if method == "tools/call":
            # Real Z.AI MCP servers double-encode the payload: the `text`
            # field is a JSON string whose decoded value is itself another
            # JSON string. Emulate that here so tests catch parsing bugs.
            if isinstance(payload_obj, str):
                inner_text = payload_obj
            else:
                inner_text = jsonlib_dumps(jsonlib_dumps(payload_obj))
            envelope = {
                "jsonrpc": "2.0",
                "id": (json or {}).get("id"),
                "result": {"content": [{"type": "text", "text": inner_text}]},
            }
            return _FakeMCPResponse(text=f"data: {jsonlib_dumps(envelope)}\n")
        raise AssertionError(f"Unexpected MCP method: {method!r}")

    return handler


def jsonlib_dumps(obj) -> str:
    return json.dumps(obj)


_json_dumps = json.dumps

_APPLIED_EVIDENCE_PLAN = {
    "mode": "applied",
    "task_type": "vendor_selection",
    "candidate_type": "design studio",
    "requirements": [
        {
            "id": "specialization",
            "label": "Specialization",
            "description": "Candidate has relevant specialization",
            "required": True,
            "min_sources": 1,
        }
    ],
}

_STUDIO_A_EVIDENCE = {
    "candidates": [
        {
            "name": "Studio A",
            "aliases": ["A"],
            "evidence": [
                {
                    "criterion_id": "specialization",
                    "status": "supports",
                    "claim": "Studio A designs offices.",
                    "quote": "Studio A designs offices.",
                    "confidence": 0.9,
                }
            ],
        }
    ]
}


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()


VALID_FALLBACK_RULES_TEXT = """
[
  {
    "gateway_model_name": "llmgateway/light_model",
    "fallback_models": [
      {
        "provider": "openai",
        "model": "gpt-4o-mini"
      }
    ],
    "rotate_models": false
  }
]
""".strip()


VALID_OPERATION_RULES_TEXT = """
{
  "embeddings": [
    {
      "gateway_model_name": "llmgateway/embedding",
      "routes": [
        {
          "provider": "openai",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "llmgateway/rerank",
      "routes": [
        {
          "provider": "openai",
          "model": "rerank-model",
          "target_path": "/score"
        }
      ]
    }
  ],
  "images_generations": [
    {
      "gateway_model_name": "llmgateway/image-gen",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-image-1",
          "target_path": "/images/generations"
        }
      ]
    }
  ],
  "images_edits": [],
  "web_search": [
    {
      "gateway_model_name": "llmgateway/web-search",
      "query_model": "llmgateway/light_model"
    }
  ],
  "web_read": [
    {
      "gateway_model_name": "llmgateway/web-read"
    }
  ],
  "web_research": [
    {
      "gateway_model_name": "llmgateway/web-research",
      "search_model": "llmgateway/web-search",
      "read_model": "llmgateway/web-read",
      "rerank_model": "llmgateway/rerank",
      "analysis_model": "llmgateway/light_model"
    }
  ],
  "web_deep_research": [
    {
      "gateway_model_name": "llmgateway/web-deep-research",
      "search_model": "llmgateway/web-search",
      "read_model": "llmgateway/web-read",
      "fast_model": "llmgateway/light_model",
      "smart_model": "llmgateway/light_model",
      "strategic_model": "llmgateway/light_model",
      "embedding_model": "llmgateway/embedding",
      "image_generation_model": "llmgateway/image-gen",
      "image_generation_size": "1024x1024"
    }
  ]
}
""".strip()


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FakeDownstreamResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload


class _FakeApiKeysDB:
    def __init__(self, record: ApiKeyRecord | None = None) -> None:
        self.record = record
        self.spent_calls = []

    @property
    def db_path(self) -> Path:
        return main.resolve_db_dir() / "tokens_usage.db"

    def get_by_key(self, api_key: str):
        if self.record and api_key == self.record.api_key:
            return self.record
        return None

    def record_spent(self, key_id: int, amount: float) -> None:
        self.spent_calls.append((key_id, amount))


class _FakeDeepResearchManager:
    calls = []
    generated_images_override: list[dict] | None = None

    async def run(self, _runner, job, callbacks):
        call = {"job": job, "callbacks": callbacks, "callback_images": ()}
        self.calls.append(call)
        search_results = await callbacks.handle(
            DeepResearchCallbackRequest(
                job_id=job.job_id,
                message_id="search-1",
                operation=DeepResearchCallbackOperation.SEARCH,
                arguments={"query": "deep topic", "max_results": 2},
            )
        )
        article = await callbacks.handle(
            DeepResearchCallbackRequest(
                job_id=job.job_id,
                message_id="read-1",
                operation=DeepResearchCallbackOperation.READ,
                arguments={"url": search_results[0]["url"]},
            )
        )
        if self.__class__.generated_images_override is not None:
            generated_images = tuple(self.__class__.generated_images_override)
        elif job.image_generation_enabled:
            generated_images = tuple(
                await callbacks.handle(
                    DeepResearchCallbackRequest(
                        job_id=job.job_id,
                        message_id="image-1",
                        operation=DeepResearchCallbackOperation.IMAGE,
                        arguments={
                            "prompt": "diagram of a cat",
                            "context": "research context",
                            "research_id": job.job_id,
                            "aspect_ratio": "1:1",
                            "num_images": 1,
                            "style": "dark",
                        },
                    )
                )
            )
        else:
            generated_images = ()
        call["callback_images"] = generated_images
        return DeepResearchResult(
            query=job.query,
            report="Deep report",
            sources=({"title": article["title"], "url": article["url"]},),
            source_urls=(article["url"],),
            context=(article["content"],),
            research_result={"status": "ok"},
            costs=0.03,
            generated_images=generated_images,
        )


class _DeepResearchAccountingPassthrough:
    def __init__(self) -> None:
        _handle, self.token = DeepResearchContextTokenCodec.create_process_local().issue_parent(
            reservation=AccountingReservation(
                reservation_id="deep-compat-reservation",
                request_id="deep-compat-request",
                api_key_id=None,
                reserved_usd=1.0,
            ),
            gateway_model="llmgateway/web-deep-research",
            expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
        )
        self.child_routes: list[str] = []
        self.rollup_cost_usd: float | None = None
        self._ready = False
        self._closed = False

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_ready(self) -> bool:
        return self._ready

    def begin(self, _gateway_model: str) -> str:
        return self.token

    async def run_flat_operation_child(self, *, route_template, work, **_kwargs):
        self.child_routes.append(route_template)
        return await work()

    async def seal_for_response(self):
        self.rollup_cost_usd = 0.0
        self._ready = True
        return SimpleNamespace(
            aggregate_usage=AccountingUsage(cost=len(self.child_routes) * DEFAULT_OPERATION_COST_USD)
        )

    async def release_if_open(self, *, primary_error=None) -> None:
        self._closed = True


@contextmanager
def _deep_research_accounting_passthrough():
    owner = _DeepResearchAccountingPassthrough()

    async def reserve(**kwargs):
        return AccountingReservation(
            reservation_id=f"deep-compat-{kwargs['request_id']}",
            request_id=kwargs["request_id"],
            api_key_id=kwargs["api_key_id"],
            reserved_usd=kwargs["estimate_usd"],
        )

    def take_owner(request):
        take_accounting_request_context(request.scope)
        return owner

    accounting_service = main.app.state.services.accounting_service
    with (
        patch.object(accounting_service.reserve, "side_effect", reserve),
        patch.object(accounting_service.release, "return_value", True),
        patch.object(
            web_api,
            "take_deep_research_terminal_owner",
            side_effect=take_owner,
        ),
        patch.object(web_api, "_deep_research_terminal_owner", return_value=owner),
    ):
        yield owner


class WebApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openai")
        self.fallback_provider_patcher.start()
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        coordinator_patcher = patch(
            "main.ConfigUpdateCoordinator",
            return_value=config_update_coordinator,
        )
        coordinator_patcher.start()
        self.addCleanup(coordinator_patcher.stop)
        self.generated_query_text = "optimized search query"
        self.generated_query_text_by_model = {}
        _FakeDeepResearchManager.calls = []
        _FakeDeepResearchManager.generated_images_override = None

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    def test_web_read_url_validation_blocks_private_and_link_local_hosts(self):
        blocked_urls = (
            "http://127.0.0.1:9000/private",
            "http://10.0.0.5/private",
            "http://100.64.0.1/private",
            "http://100.127.255.254/private",
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:9000/private",
        )

        for url in blocked_urls:
            with self.subTest(url=url), self.assertRaises(HTTPException) as ctx:
                web_api._validate_http_url(url)
            self.assertEqual(ctx.exception.status_code, 400)

    def test_fetch_host_resolution_is_not_cached(self):
        calls = []

        async def fake_getaddrinfo(hostname, port, *, type=None):  # noqa: A002 - mirrors socket API
            calls.append((hostname, port, type))
            return [(None, None, None, None, (f"93.184.216.{len(calls)}", port))]

        async def scenario():
            loop = asyncio.get_running_loop()
            with patch.object(loop, "getaddrinfo", side_effect=fake_getaddrinfo):
                first = await web_api._resolve_fetch_host("example.com", 443)
                second = await web_api._resolve_fetch_host("example.com", 443)
            return first, second

        first, second = run_async(scenario())

        self.assertEqual(first, ("93.184.216.1",))
        self.assertEqual(second, ("93.184.216.2",))
        self.assertEqual(len(calls), 2)

    def test_web_read_url_validation_blocks_mixed_public_and_private_dns(self):
        with (
            patch.object(
                web_safe_fetch_owner,
                "_resolve_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34", "127.0.0.1"),
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            run_async(web_api._validated_fetch_url("https://example.com/article"))

        self.assertEqual(ctx.exception.status_code, 400)

    def test_public_redirects_revalidate_each_hop_and_block_private_target(self):
        # _get_pinned_public_url always builds its own pinned httpx.AsyncClient
        # rather than accepting one from a caller, so the test swaps only the
        # transport it uses (via a patched _PinnedHostAsyncHTTPTransport) and
        # lets the real pinned-fetch code path run end to end.
        def handler(request):
            return web_safe_fetch_owner.httpx.Response(
                302,
                headers={"Location": "http://127.0.0.1/private"},
                content=b"",
                request=request,
            )

        with (
            patch.object(
                web_safe_fetch_owner,
                "_resolve_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch.object(
                web_safe_fetch_owner,
                "_PinnedHostAsyncHTTPTransport",
                lambda **_kwargs: web_safe_fetch_owner.httpx.MockTransport(handler),
            ),
            self.assertRaises(HTTPException) as ctx,
        ):
            run_async(web_api._get_with_public_redirects("https://example.com/article"))

        self.assertEqual(ctx.exception.status_code, 400)

    def test_pinned_backend_connects_to_validated_ip_not_original_host(self):
        calls = []

        class _FakeBackend:
            async def connect_tcp(self, host, port, **_kwargs):
                calls.append((host, port))
                return object()

            async def sleep(self, _seconds):
                return None

        backend = web_api._PinnedHostNetworkBackend(
            pinned_host="example.com",
            pinned_port=443,
            connect_ip="93.184.216.34",
        )
        backend._backend = _FakeBackend()

        result = run_async(backend.connect_tcp("example.com", 443))

        self.assertIsNotNone(result)
        self.assertEqual(calls, [("93.184.216.34", 443)])
        with self.assertRaises(web_safe_fetch_owner.httpcore.ConnectError):
            run_async(backend.connect_tcp("other.example", 443))

    def test_cloakbrowser_fetch_is_disabled_unless_explicitly_enabled(self):
        with (
            patch.object(web_api.settings, "web_read_cloakbrowser_enabled", False),
            patch.object(web_extraction_owner, "_cloakbrowser_render_sync") as render_mock,
        ):
            result = run_async(web_api._cloakbrowser_fetch("https://example.com/article"))

        self.assertIsNone(result)
        render_mock.assert_not_called()

    def test_cloakbrowser_no_sandbox_is_explicit_opt_in(self):
        with patch.object(web_api.settings, "web_read_cloakbrowser_no_sandbox", False):
            self.assertNotIn("--no-sandbox", web_api._cloakbrowser_launch_args())

        with patch.object(web_api.settings, "web_read_cloakbrowser_no_sandbox", True):
            self.assertIn("--no-sandbox", web_api._cloakbrowser_launch_args())

    def test_client_disconnect_cancels_running_web_work(self):
        class DisconnectingRequest:
            def __init__(self):
                self.calls = 0

            async def is_disconnected(self):
                self.calls += 1
                return self.calls >= 2

        async def scenario():
            cancelled = asyncio.Event()
            on_cancel = Mock()

            async def long_work():
                try:
                    await asyncio.Event().wait()
                finally:
                    cancelled.set()

            with patch("llm_gateway_core.api.v1.web_research_orchestration.CLIENT_DISCONNECT_POLL_SECONDS", 0):
                with self.assertRaises(HTTPException) as raised:
                    await web_api._run_with_client_disconnect_cancellation(
                        DisconnectingRequest(),
                        web_api.WEB_RESEARCH_OPERATION,
                        long_work,
                        on_cancel=on_cancel,
                    )

            self.assertEqual(raised.exception.status_code, web_api.CLIENT_CLOSED_REQUEST_STATUS_CODE)
            self.assertTrue(cancelled.is_set())
            on_cancel.assert_called_once_with()

        run_async(scenario())

    def test_outer_cancellation_waits_for_owned_task_cleanup(self):
        class ConnectedRequest:
            async def is_disconnected(self):
                return False

        async def scenario():
            work_started = asyncio.Event()
            cleanup_started = asyncio.Event()
            cleanup_gate = asyncio.Event()
            on_cancel = Mock()

            async def work():
                work_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await cleanup_gate.wait()

            with patch.object(web_research_owner, "CLIENT_DISCONNECT_POLL_SECONDS", 0):
                outer = asyncio.create_task(
                    web_api._run_with_client_disconnect_cancellation(
                        ConnectedRequest(),
                        web_api.WEB_DEEP_RESEARCH_OPERATION,
                        work,
                        on_cancel=on_cancel,
                    )
                )
                await work_started.wait()
                outer.cancel()
                await cleanup_started.wait()
                await asyncio.sleep(0)
                self.assertFalse(outer.done())
                outer.cancel()
                await asyncio.sleep(0)
                self.assertFalse(outer.done())
                cleanup_gate.set()
                with self.assertRaises(asyncio.CancelledError):
                    await outer

            on_cancel.assert_called_once_with()
            self.assertFalse(
                any(
                    task.get_name().startswith("web-client-disconnect-")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )

        run_async(scenario())

    def test_success_stops_cancellation_swallowing_disconnect_watcher(self):
        class CancellationSwallowingRequest:
            def __init__(self):
                self.calls = 0
                self.entered = asyncio.Event()
                self.release = asyncio.Event()

            async def is_disconnected(self):
                self.calls += 1
                if self.calls == 1:
                    return False
                self.entered.set()
                try:
                    await self.release.wait()
                    await asyncio.sleep(0)
                except asyncio.CancelledError:
                    return False
                return False

        async def scenario():
            request = CancellationSwallowingRequest()

            async def immediate_work():
                await request.entered.wait()
                request.release.set()
                return "done"

            with patch.object(web_research_owner, "CLIENT_DISCONNECT_POLL_SECONDS", 0):
                result = await asyncio.wait_for(
                    web_api._run_with_client_disconnect_cancellation(
                        request,
                        web_api.WEB_DEEP_RESEARCH_OPERATION,
                        immediate_work,
                    ),
                    timeout=0.5,
                )

            self.assertEqual(result, "done")
            self.assertGreaterEqual(request.calls, 2)
            self.assertFalse(
                any(
                    task.get_name().startswith("web-client-disconnect-")
                    for task in asyncio.all_tasks()
                    if task is not asyncio.current_task()
                )
            )

        run_async(scenario())

    def test_outer_cancellation_waits_for_real_runner_and_reuses_permit(self):
        class ConnectedRequest:
            async def is_disconnected(self):
                return False

        def job(query: str) -> DeepResearchJob:
            return DeepResearchJob(
                job_id=f"web-cancel-{query}",
                query=query,
                fast_model="llmgateway/fast",
                smart_model="llmgateway/smart",
                strategic_model="llmgateway/strategic",
                embedding_model=None,
                gateway_base_url="http://127.0.0.1:9000/v1",
                gateway_api_key="child-secret",
            )

        async def scenario():
            runner = DeepResearchProcessRunner(
                capacity=1,
                admission_timeout_seconds=0.2,
                _adapter_module="tests.deep_research_process_fixture",
            )
            await runner.start()
            outer = asyncio.create_task(
                web_api._run_with_client_disconnect_cancellation(
                    ConnectedRequest(),
                    web_api.WEB_DEEP_RESEARCH_OPERATION,
                    lambda: runner.run(job("block")),
                )
            )
            try:
                for _attempt in range(500):
                    if runner.active_process_count:
                        break
                    await asyncio.sleep(0.001)
                self.assertEqual(runner.active_process_count, 1)
                child_pid = runner.active_process_ids[0]

                outer.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await outer
                self.assertEqual(runner.active_process_count, 0)
                with self.assertRaises(ProcessLookupError):
                    os.kill(child_pid, 0)

                result = await runner.run(job("permit-reused"))
                self.assertEqual(result.report, "fixture report")
                self.assertFalse(
                    any(
                        task.get_name().startswith("web-client-disconnect-")
                        for task in asyncio.all_tasks()
                        if task is not asyncio.current_task()
                    )
                )
            finally:
                if not outer.done():
                    outer.cancel()
                    await asyncio.gather(outer, return_exceptions=True)
                await runner.aclose()

        run_async(scenario())

    def test_deep_research_endpoint_has_no_worker_thread_bridge(self):
        source = Path(web_api.__file__).read_text(encoding="utf-8")
        self.assertNotIn("_DeepResearchWorker", source)
        self.assertNotIn("run_coroutine_threadsafe", source)
        self.assertNotIn("_conduct_deep_research_in_worker", source)

    def _fake_post(self, url, *, headers=None, json=None, **kwargs):
        if "text_1" in json and "text_2" in json:
            documents = json.get("text_2") or []
            return _FakeDownstreamResponse(
                {
                    "data": [
                        {"index": index, "score": float(len(documents) - index)}
                        for index, _document in enumerate(documents)
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 0, "total_tokens": 1, "cost": 0.01},
                }
            )

        # Chat LLM used for query expansion and analysis.
        last_message = (json.get("messages") or [{}])[-1].get("content", "")
        if "Определи, нужно ли включать evidence matrix" in last_message:
            content = _json_dumps(
                {
                    "mode": "not_applicable",
                    "task_type": "general_research",
                    "candidate_type": "",
                    "requirements": [],
                }
            )
        elif "Извлеки evidence matrix" in last_message:
            content = _json_dumps({"candidates": []})
        elif "Собери итоговый исследовательский ответ строго по evidence matrix" in last_message:
            content = "Synthesized evidence answer with citations."
        elif "Проанализируй источник" in last_message:
            content = "- Relevant fact (https://example.com/article)"
        elif "Собери единый связный исследовательский ответ" in last_message:
            content = "Synthesized research answer with citations."
        elif json.get("model") in self.generated_query_text_by_model:
            content = self.generated_query_text_by_model[json.get("model")]
        else:
            content = self.generated_query_text
        return _FakeDownstreamResponse(
            {
                "choices": [{"message": {"content": content}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": 0.01},
            }
        )

    @contextmanager
    def _client(
        self,
        api_keys_db: _FakeApiKeysDB | None = None,
        *,
        post_side_effect=None,
        search_adapter: AsyncMock | None = None,
        search_adapters: dict[str, AsyncMock] | None = None,
        read_adapter: AsyncMock | None = None,
        direct_fetch_result=None,
        cloakbrowser_fetch_result=None,
    ):
        fake_http_client = Mock()
        fake_http_client.post = AsyncMock(side_effect=post_side_effect or self._fake_post)
        fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
        fake_http_client.aclose = AsyncMock()

        # Default adapters: return the canned "example.com/article" result so
        # existing tests don't have to wire a stub for every case.
        if search_adapter is None:
            search_adapter = AsyncMock(
                return_value=[
                    {
                        "url": "https://example.com/article",
                        "title": "Example Article",
                        "snippet": "Short snippet",
                    }
                ]
            )
        search_adapter_mapping = search_adapters or {"zai": search_adapter}
        if read_adapter is None:
            read_adapter = AsyncMock(
                return_value={
                    "url": "https://example.com/article",
                    "title": "Reader Title",
                    "content": "Downloaded article content",
                }
            )

        with ExitStack() as stack:
            install_web_accounting_passthrough(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient", return_value=fake_http_client)
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.ApiKeysDB", return_value=api_keys_db or _FakeApiKeysDB()))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
            stack.enter_context(
                patch.object(
                    main.settings,
                    "proxy_url",
                    "http://proxy.example" if "proxy" in search_adapter_mapping else None,
                )
            )
            stack.enter_context(
                patch.object(
                    main.settings,
                    "tavily_api_key",
                    "dummy-tavily" if "tavily" in search_adapter_mapping else None,
                )
            )
            stack.enter_context(
                patch.object(
                    main.settings,
                    "jina_api_key",
                    "dummy-jina" if "jina" in search_adapter_mapping else None,
                )
            )
            stack.enter_context(
                patch.object(
                    main.settings,
                    "zai_api_key",
                    "dummy-zai" if "zai" in search_adapter_mapping else None,
                )
            )
            stack.enter_context(
                patch.dict(
                    "llm_gateway_core.api.v1.web_adapters._SEARCH_ADAPTERS",
                    search_adapter_mapping,
                    clear=False,
                )
            )
            stack.enter_context(
                patch.dict(
                    "llm_gateway_core.api.v1.web_adapters._READ_ADAPTERS",
                    {"zai": read_adapter},
                    clear=False,
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web_adapters._direct_http_fetch",
                    AsyncMock(return_value=direct_fetch_result),
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web_adapters._cloakbrowser_fetch",
                    AsyncMock(return_value=cloakbrowser_fetch_result),
                )
            )

            with TestClient(main.app) as client:
                yield client, fake_http_client, search_adapter, read_adapter

    def test_web_search_uses_external_service_model(self):
        with self._client() as (client, fake_http_client, search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/search",
                json={"model": "llmgateway/web-search", "query": "topic", "max_results": 3},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "web_search")
        self.assertEqual(payload["model"], "llmgateway/web-search")
        self.assertEqual(payload["data"][0]["url"], "https://example.com/article")
        search_adapter.assert_awaited()
        # The query-expansion chat call went through the fake http client.
        self.assertGreaterEqual(fake_http_client.post.await_count, 1)

    def test_web_search_include_raw_content_uses_read_pipeline(self):
        with self._client() as (client, _fake_http_client, _search_adapter, read_adapter):
            response = client.post(
                "/v1/web/search",
                json={
                    "model": "llmgateway/web-search",
                    "read_model": "llmgateway/web-read",
                    "query": "topic",
                    "include_raw_content": True,
                    "include_images": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"][0]["raw_content"], "Downloaded article content")
        self.assertEqual(payload["data"][0]["images"], [])
        read_adapter.assert_awaited_once_with(ANY, "https://example.com/article")

    def test_web_search_include_raw_content_text_returns_plain_text(self):
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/article",
                "title": "Reader Title",
                "content": "# Heading\n\n[Visible link](https://example.com)\n\n**bold** text",
            }
        )
        with self._client(read_adapter=read_adapter) as (client, _fake_http_client, _search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/search",
                json={
                    "model": "llmgateway/web-search",
                    "read_model": "llmgateway/web-read",
                    "query": "topic",
                    "include_raw_content": "text",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"][0]["raw_content"], "Heading\n\nVisible link\n\nbold text")

    def test_web_search_domain_filters_try_next_adapter_when_first_has_no_matches(self):
        proxy_adapter = AsyncMock(
            return_value=[
                {
                    "url": "https://irrelevant.example/article",
                    "title": "Irrelevant",
                    "snippet": "Wrong domain",
                }
            ]
        )
        zai_adapter = AsyncMock(
            return_value=[
                {
                    "url": "https://wanted.example/article",
                    "title": "Wanted",
                    "snippet": "Right domain",
                }
            ]
        )
        with self._client(search_adapters={"proxy": proxy_adapter, "zai": zai_adapter}) as (
            client,
            _fake_http_client,
            _search_adapter,
            _read_adapter,
        ):
            response = client.post(
                "/v1/web/search",
                json={
                    "model": "llmgateway/web-search",
                    "query": "topic",
                    "include_domains": ["wanted.example"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual([item["url"] for item in payload["data"]], ["https://wanted.example/article"])
        proxy_adapter.assert_awaited()
        zai_adapter.assert_awaited()

    def test_web_read_format_text_returns_plain_text(self):
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/article",
                "title": "Reader Title",
                "content": "## Reader Title\n\n[Source](https://example.com) with *emphasis*",
            }
        )
        with self._client(read_adapter=read_adapter) as (client, _fake_http_client, _search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/read",
                json={
                    "model": "llmgateway/web-read",
                    "url": "https://example.com/article",
                    "format": "text",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"], "Reader Title\n\nSource with emphasis")

    def test_tavily_search_endpoint_returns_tavily_format(self):
        with self._client() as (client, fake_http_client, search_adapter, read_adapter):
            response = client.post(
                "/v1/tavily/search",
                json={
                    "model": "llmgateway/web-search",
                    "read_model": "llmgateway/web-read",
                    "query": "topic",
                    "max_results": 3,
                    "include_raw_content": "markdown",
                    "include_images": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertNotIn("data", payload)
        self.assertEqual(payload["query"], "topic")
        self.assertEqual(payload["answer"], None)
        self.assertEqual(payload["images"], [])
        self.assertEqual(payload["failed_results"], [])
        self.assertIsInstance(payload["request_id"], str)
        self.assertGreaterEqual(payload["response_time"], 0)
        self.assertEqual(payload["results"][0]["url"], "https://example.com/article")
        self.assertEqual(payload["results"][0]["content"], "Short snippet")
        self.assertEqual(payload["results"][0]["raw_content"], "Downloaded article content")
        self.assertEqual(payload["results"][0]["images"], [])
        self.assertIsInstance(payload["results"][0]["score"], float)
        search_adapter.assert_awaited_once_with(fake_http_client, "topic", 3, include_images=True)
        read_adapter.assert_awaited_once_with(fake_http_client, "https://example.com/article")
        fake_http_client.post.assert_not_awaited()

    def test_tavily_extract_endpoint_reads_urls(self):
        with self._client() as (client, _fake_http_client, _search_adapter, read_adapter):
            response = client.post(
                "/v1/tavily/extract",
                json={
                    "model": "llmgateway/web-read",
                    "urls": ["https://example.com/article"],
                    "include_images": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["results"][0]["url"], "https://example.com/article")
        self.assertEqual(payload["results"][0]["raw_content"], "Downloaded article content")
        self.assertEqual(payload["results"][0]["images"], [])
        self.assertEqual(payload["failed_results"], [])
        self.assertIsInstance(payload["request_id"], str)
        read_adapter.assert_awaited_once_with(ANY, "https://example.com/article")

    def test_web_search_query_model_falls_back_after_empty_text_content(self):
        self.rules_path.write_text(
            json.dumps(
                [
                    {
                        "gateway_model_name": "llmgateway/light_model",
                        "fallback_models": [
                            {"provider": "openai", "model": "empty-query-model"},
                            {"provider": "openai", "model": "working-query-model"},
                        ],
                        "rotate_models": False,
                    }
                ]
            ),
            encoding="utf-8",
        )
        self.generated_query_text_by_model = {
            "empty-query-model": "",
            "working-query-model": "fallback optimized query",
        }

        with self._client() as (client, fake_http_client, search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/search",
                json={"model": "llmgateway/web-search", "query": "topic", "max_results": 3},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_http_client.post.await_count, 2)
        search_adapter.assert_awaited_once_with(ANY, "fallback optimized query", 3, include_images=False)

    def test_web_search_reports_internal_query_model_error_when_fallbacks_return_empty_text(self):
        self.generated_query_text = ""

        with self._client() as (client, fake_http_client, search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/search",
                json={"model": "llmgateway/web-search", "query": "topic", "max_results": 3},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertIn("Internal gateway model 'llmgateway/light_model' failed", response.json()["detail"])
        self.assertIn(
            "Model returned an empty completion with no tool call",
            response.json()["detail"],
        )
        self.assertEqual(fake_http_client.post.await_count, 1)
        search_adapter.assert_not_awaited()

    def test_web_search_fails_when_no_adapters_configured(self):
        with ExitStack() as stack:
            install_web_accounting_passthrough(stack)
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient", return_value=fake_http_client)
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.ApiKeysDB", return_value=_FakeApiKeysDB()))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
            stack.enter_context(patch.object(main.settings, "proxy_url", None))
            stack.enter_context(patch.object(main.settings, "tavily_api_key", None))
            stack.enter_context(patch.object(main.settings, "jina_api_key", None))
            stack.enter_context(patch.object(main.settings, "zai_api_key", None))

            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/web/search",
                    json={"model": "llmgateway/web-search", "query": "topic"},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 503)
        self.assertIn("no enabled adapters", response.json()["detail"])

    def test_web_read_downloads_content_by_url(self):
        with self._client() as (client, _fake_http_client, _search_adapter, read_adapter):
            response = client.post(
                "/v1/web/read",
                json={
                    "model": "llmgateway/web-read",
                    "url": "https://example.com/article",
                    "include_images": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "web_read")
        self.assertEqual(payload["title"], "Reader Title")
        self.assertEqual(payload["content"], "Downloaded article content")
        self.assertEqual(payload["images"], [])
        read_adapter.assert_awaited_once_with(ANY, "https://example.com/article")

    def test_web_research_virtual_key_checks_only_external_model(self):
        record = ApiKeyRecord(
            id=7,
            name="restricted",
            api_key="lgk_test",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=["llmgateway/web-research"],
        )
        api_keys_db = _FakeApiKeysDB(record)

        with self._client(api_keys_db) as (client, _fake_http_client, _search_adapter, _read_adapter):
            response = client.post(
                "/v1/web/research",
                json={"model": "llmgateway/web-research", "query": "topic", "max_articles": 1},
                headers={"Authorization": "Bearer lgk_test"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "web_research")
        self.assertEqual(payload["model"], "llmgateway/web-research")
        self.assertEqual(payload["output_language"], "русском")
        self.assertIn("Synthesized research answer", payload["output"])
        self.assertEqual(api_keys_db.spent_calls, [])

    def test_web_research_searches_ru_en_zh_and_reads_language_limited_sources(self):
        generate_calls = []

        async def fake_generate_queries(
            _request,
            _config_loader,
            _http_client,
            *,
            query_model,
            query,
            language,
            num_queries,
            usage_accumulator,
        ):
            generate_calls.append((query_model, query, language, num_queries))
            return [f"{language}-query-{index}" for index in range(num_queries)]

        async def fake_search(_client, query: str, max_results: int, *, include_images: bool = False):
            self.assertFalse(include_images)
            language = query.split("-", 1)[0]
            return [
                {
                    "url": f"https://{language}.example/{query}/{index}",
                    "title": f"{language} article {index}",
                    "snippet": "snippet",
                }
                for index in range(max_results)
            ]

        async def fake_read(_client, url: str):
            language = url.split("://", 1)[1].split(".", 1)[0]
            return {
                "url": url,
                "title": f"{language} title",
                "content": f"{language} downloaded content",
            }

        search_adapter = AsyncMock(side_effect=fake_search)
        read_adapter = AsyncMock(side_effect=fake_read)
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", side_effect=fake_generate_queries),
            patch(
                "llm_gateway_core.api.v1.web_safe_fetch._resolve_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch("llm_gateway_core.api.v1.web_adapters._direct_http_fetch", new_callable=AsyncMock, return_value=None),
            patch(
                "llm_gateway_core.api.v1.web_adapters._cloakbrowser_fetch", new_callable=AsyncMock, return_value=None
            ),
        ):
            with self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                _fake_http_client,
                _,
                _,
            ):
                response = client.post(
                    "/v1/web/research",
                    json={
                        "model": "llmgateway/web-research",
                        "query": "topic",
                        "max_results_per_lang": 10,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [(language, num_queries) for _model, _query, language, num_queries in generate_calls],
            [("ru", 2), ("en", 3), ("zh", 3)],
        )
        self.assertEqual(search_adapter.await_count, 8)
        self.assertEqual(read_adapter.await_count, 30)
        source_languages = [item["language"] for item in payload["sources"]]
        self.assertEqual(source_languages.count("ru"), 8)
        self.assertEqual(source_languages.count("en"), 8)
        self.assertEqual(source_languages.count("zh"), 8)
        article_languages = [item["language"] for item in payload["articles"]]
        self.assertEqual(article_languages.count("ru"), 8)
        self.assertEqual(article_languages.count("en"), 8)
        self.assertEqual(article_languages.count("zh"), 8)

    def test_web_research_rerank_document_truncates_content_to_max_chars_and_omits_url(self):
        max_chars = web_api.ARTICLE_RERANK_DOCUMENT_MAX_CHARS
        head = "head-marker "
        tail_marker = " end-marker"
        long_content = head + ("x" * (max_chars + 500)) + tail_marker

        document = web_api._article_rerank_document(
            {
                "url": "https://example.com/full",
                "title": "Full Article",
                "content": long_content,
            }
        )

        self.assertIn("Title: Full Article", document)
        self.assertIn(head.strip(), document)
        self.assertNotIn(tail_marker.strip(), document)
        # The "Content:\n" prefix is fixed, payload after it is capped at max_chars.
        content_section = document.split("Content:\n", 1)[1]
        self.assertEqual(len(content_section), max_chars)
        self.assertNotIn("https://example.com/full", document)

    def test_web_research_rerank_document_keeps_short_content_unchanged(self):
        short_content = "short body"
        document = web_api._article_rerank_document(
            {
                "url": "https://example.com/short",
                "title": "Short",
                "content": short_content,
            }
        )

        self.assertEqual(document, "Title: Short\nContent:\nshort body")

    def test_web_research_refines_long_article_content_before_rerank_and_analysis(self):
        long_content = "irrelevant lead " + ("x" * web_api.ARTICLE_RELEVANCE_THRESHOLD_CHARS)
        long_content += "\nTAIL_DETAIL layoffs impact after acquisition"
        search_adapter = AsyncMock(
            return_value=[
                {
                    "url": "https://example.com/long",
                    "title": "Long Article",
                    "snippet": "Short snippet",
                }
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/long",
                "title": "Long Article",
                "content": long_content,
            }
        )
        relevance_prompts = []
        analysis_prompts = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "not_applicable",
                        "task_type": "general_research",
                        "candidate_type": "",
                        "requirements": [],
                    }
                )
            if "Оставь только текст статьи" in prompt:
                relevance_prompts.append(prompt)
                article_text = prompt.split("Текст статьи:\n", 1)[1]
                self.assertEqual(article_text, long_content.strip())
                self.assertIn("TAIL_DETAIL layoffs impact after acquisition", article_text)
                self.assertEqual(max_tokens, web_api.ARTICLE_RELEVANCE_MAX_TOKENS)
                return "TAIL_DETAIL layoffs impact after acquisition"
            if "Проанализируй источник" in prompt:
                analysis_prompts.append(prompt)
                self.assertIn("TAIL_DETAIL layoffs impact after acquisition", prompt)
                self.assertNotIn("x" * 1000, prompt)
                return "- Tail fact with source"
            if "Собери единый связный исследовательский ответ" in prompt:
                return "Synthesized tail answer."
            return ""

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "layoffs impact",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(relevance_prompts), 1)
        self.assertEqual(len(analysis_prompts), 1)
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(len(rerank_payloads), 1)
        self.assertEqual(
            rerank_payloads[0]["text_2"],
            ["Title: Long Article\nContent:\nTAIL_DETAIL layoffs impact after acquisition"],
        )
        payload = response.json()
        self.assertEqual(payload["articles"][0]["content"], "TAIL_DETAIL layoffs impact after acquisition")
        self.assertEqual(payload["output"], "Synthesized tail answer.")

    def test_web_research_request_parameters_override_language_query_and_article_defaults(self):
        generate_calls = []

        async def fake_generate_queries(
            _request,
            _config_loader,
            _http_client,
            *,
            query_model,
            query,
            language,
            num_queries,
            usage_accumulator,
        ):
            generate_calls.append((query_model, query, language, num_queries))
            return [f"{language}-query-{index}" for index in range(num_queries)]

        async def fake_search(_client, query: str, max_results: int, *, include_images: bool = False):
            self.assertFalse(include_images)
            language = query.split("-", 1)[0]
            return [
                {
                    "url": f"https://{language}.example/{query}/{index}",
                    "title": f"{language} article {index}",
                    "snippet": "snippet",
                }
                for index in range(max_results)
            ]

        async def fake_read(_client, url: str):
            language = url.split("://", 1)[1].split(".", 1)[0]
            return {
                "url": url,
                "title": f"{language} title",
                "content": f"{language} downloaded content",
            }

        search_adapter = AsyncMock(side_effect=fake_search)
        read_adapter = AsyncMock(side_effect=fake_read)
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", side_effect=fake_generate_queries),
            patch(
                "llm_gateway_core.api.v1.web_safe_fetch._resolve_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch("llm_gateway_core.api.v1.web_adapters._direct_http_fetch", new_callable=AsyncMock, return_value=None),
            patch(
                "llm_gateway_core.api.v1.web_adapters._cloakbrowser_fetch", new_callable=AsyncMock, return_value=None
            ),
        ):
            with self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _,
                _,
            ):
                response = client.post(
                    "/v1/web/research",
                    json={
                        "model": "llmgateway/web-research",
                        "query": "topic",
                        "max_results": 5,
                        "max_articles": 2,
                        "num_queries": 1,
                        "language": "en",
                        "output_language": "en",
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            [(language, num_queries) for _model, _query, language, num_queries in generate_calls],
            [("en", 1)],
        )
        self.assertEqual(search_adapter.await_count, 1)
        self.assertEqual(read_adapter.await_count, 5)
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(len(rerank_payloads), 1)
        rerank_documents = rerank_payloads[0]["text_2"]
        self.assertEqual(len(rerank_documents), 5)
        self.assertIn("Title: en title", rerank_documents[0])
        self.assertIn("Content:\nen downloaded content", rerank_documents[0])
        self.assertNotIn("https://", "\n".join(rerank_documents))
        self.assertEqual(payload["output_language"], "English")
        self.assertEqual([item["language"] for item in payload["sources"]], ["en", "en"])
        self.assertEqual([item["language"] for item in payload["articles"]], ["en", "en"])

    def test_web_research_evidence_matrix_not_applicable_preserves_current_flow(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "not_applicable",
                        "task_type": "general_research",
                        "candidate_type": "",
                        "requirements": [],
                    }
                )
            if "Проанализируй источник" in prompt:
                return "- Relevant fact with source"
            if "Собери единый связный исследовательский ответ" in prompt:
                return "Synthesized answer."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/one", "title": "One", "snippet": "s"},
                {"url": "https://example.com/two", "title": "Two", "snippet": "s"},
            ]
        )

        async def fake_read(_client, url: str):
            return {
                "url": url,
                "title": f"Title {url.rsplit('/', 1)[-1]}",
                "content": f"Content for {url}",
            }

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=AsyncMock(side_effect=fake_read)) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "what happened",
                    "max_results": 2,
                    "max_articles": 2,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"], "Synthesized answer.")
        self.assertNotIn("evidence_matrix", payload)
        self.assertEqual(
            [item["url"] for item in payload["articles"]], ["https://example.com/one", "https://example.com/two"]
        )
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(len(rerank_payloads), 1)
        self.assertEqual(len(rerank_payloads[0]["text_2"]), 2)

    def test_web_research_evidence_matrix_applied_keeps_candidate_with_required_evidence(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "specialization",
                                "label": "Specialization",
                                "description": "Candidate has relevant specialization",
                                "required": True,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "candidates": [
                            {
                                "name": "Studio A",
                                "aliases": ["A"],
                                "evidence": [
                                    {
                                        "criterion_id": "specialization",
                                        "status": "supports",
                                        "claim": "Studio A designs offices.",
                                        "quote": "Studio A designs offices.",
                                        "confidence": 0.9,
                                    }
                                ],
                            }
                        ]
                    }
                )
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                self.assertIn("Studio A", prompt)
                return "Studio A is supported."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": "Studio A designs offices.",
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"], "Studio A is supported.")
        self.assertEqual([item["url"] for item in payload["articles"]], ["https://example.com/studio-a"])
        self.assertEqual(payload["evidence_matrix"]["mode"], "applied")
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], ["Studio A"])
        self.assertEqual(payload["evidence_matrix"]["candidates"][0]["status"], "passed")
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(len(rerank_payloads), 1)
        self.assertEqual(len(rerank_payloads[0]["text_2"]), 1)

    def test_web_research_evidence_matrix_applied_drops_candidate_missing_required_criterion(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "specialization",
                                "label": "Specialization",
                                "description": "Candidate has relevant specialization",
                                "required": True,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            if "Извлеки evidence matrix" in prompt and "https://example.com/supported" in prompt:
                return _json_dumps(
                    {
                        "candidates": [
                            {
                                "name": "Studio A",
                                "aliases": [],
                                "evidence": [
                                    {
                                        "criterion_id": "specialization",
                                        "status": "supports",
                                        "claim": "Studio A designs offices.",
                                        "quote": "Studio A designs offices.",
                                        "confidence": 0.9,
                                    }
                                ],
                            }
                        ]
                    }
                )
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "candidates": [
                            {
                                "name": "Studio B",
                                "aliases": [],
                                "evidence": [
                                    {
                                        "criterion_id": "specialization",
                                        "status": "unclear",
                                        "claim": "",
                                        "quote": "",
                                        "confidence": 0.1,
                                    }
                                ],
                            }
                        ]
                    }
                )
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                self.assertIn("Studio A", prompt)
                self.assertIn("Studio B", prompt)
                return "Studio A is the only supported candidate."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/supported", "title": "Supported", "snippet": "s"},
                {"url": "https://example.com/missing", "title": "Missing", "snippet": "s"},
            ]
        )

        async def fake_read(_client, url: str):
            if url.endswith("/supported"):
                content = "Studio A designs offices."
            else:
                content = "Studio B is mentioned."
            return {"url": url, "title": url.rsplit("/", 1)[-1], "content": content}

        read_adapter = AsyncMock(side_effect=fake_read)
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 2,
                    "max_articles": 2,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(read_adapter.await_count, 2)
        self.assertEqual([item["url"] for item in payload["articles"]], ["https://example.com/supported"])
        self.assertEqual([item["url"] for item in payload["sources"]], ["https://example.com/supported"])
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], ["Studio A"])
        self.assertEqual(payload["evidence_matrix"]["rejected_candidates"], ["Studio B"])
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(len(rerank_payloads), 1)
        self.assertEqual(len(rerank_payloads[0]["text_2"]), 1)

    def test_web_research_evidence_matrix_invalid_analysis_json_fails_explicitly(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return "not json"
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/article", "title": "Article", "snippet": "s"},
            ]
        )
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose a vendor",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("evidence_matrix", detail)
        self.assertIn("analysis_model", detail)
        self.assertIn("invalid JSON", detail)
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_evidence_matrix_malformed_not_applicable_plan_fails_explicitly(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "not_applicable",
                        "task_type": "general_research",
                        "candidate_type": 123,
                        "requirements": [],
                    }
                )
            return ""

        search_adapter = AsyncMock(return_value=[{"url": "https://example.com/a", "title": "A", "snippet": "s"}])
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "what happened",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("candidate_type must be a string", response.json()["detail"])
        search_adapter.assert_not_awaited()
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_evidence_matrix_optional_requirement_fails_explicitly(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "nice_to_have",
                                "label": "Nice to have",
                                "description": "Optional criterion",
                                "required": False,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            return ""

        search_adapter = AsyncMock(return_value=[{"url": "https://example.com/a", "title": "A", "snippet": "s"}])
        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("requirements must be required", response.json()["detail"])
        search_adapter.assert_not_awaited()
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_evidence_matrix_relevance_failure_fails_explicitly(self):
        long_content = "Studio A designs offices. " + ("x" * web_api.ARTICLE_RELEVANCE_THRESHOLD_CHARS)

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "specialization",
                                "label": "Specialization",
                                "description": "Candidate has relevant specialization",
                                "required": True,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            if "Оставь только текст статьи" in prompt:
                raise RuntimeError("relevance model unavailable")
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/long", "title": "Long", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/long",
                "title": "Long",
                "content": long_content,
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("Article relevance preparation failed", response.json()["detail"])
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_evidence_matrix_invalid_extraction_structure_fails_explicitly(self):
        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "specialization",
                                "label": "Specialization",
                                "description": "Candidate has relevant specialization",
                                "required": True,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "candidates": [
                            {
                                "name": "Studio A",
                                "aliases": [],
                                "evidence": "not a list",
                            }
                        ]
                    }
                )
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": "Studio A designs offices.",
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        detail = response.json()["detail"]
        self.assertIn("extraction", detail)
        self.assertIn("evidence must be a list", detail)
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_evidence_matrix_planning_retries_invalid_structure(self):
        plan_prompts = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                plan_prompts.append(prompt)
                if len(plan_prompts) == 1:
                    return _json_dumps(
                        {
                            "mode": "applied",
                            "task_type": "vendor_selection",
                            "candidate_type": 123,
                            "requirements": [],
                        }
                    )
                return _json_dumps(_APPLIED_EVIDENCE_PLAN)
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(_STUDIO_A_EVIDENCE)
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                return "Studio A is supported."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": "Studio A designs offices.",
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"], "Studio A is supported.")
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], ["Studio A"])
        self.assertEqual(len(plan_prompts), 2)
        self.assertIn("Предыдущий ответ отклонён валидатором", plan_prompts[1])

    def test_web_research_evidence_matrix_extraction_retries_invalid_structure(self):
        extraction_prompts = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(_APPLIED_EVIDENCE_PLAN)
            if "Извлеки evidence matrix" in prompt:
                extraction_prompts.append(prompt)
                if len(extraction_prompts) == 1:
                    return _json_dumps(
                        {"candidates": [{"name": "Studio A", "aliases": [], "evidence": "not a list"}]}
                    )
                return _json_dumps(_STUDIO_A_EVIDENCE)
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                return "Studio A is supported."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": "Studio A designs offices.",
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"], "Studio A is supported.")
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], ["Studio A"])
        self.assertEqual(len(extraction_prompts), 2)
        self.assertIn("Предыдущий ответ отклонён валидатором", extraction_prompts[1])

    def test_web_research_evidence_matrix_skips_source_with_permanently_invalid_extraction(self):
        extraction_urls = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(_APPLIED_EVIDENCE_PLAN)
            if "Извлеки evidence matrix" in prompt:
                if "studio-b" in prompt:
                    extraction_urls.append("studio-b")
                    return _json_dumps(
                        {"candidates": [{"name": "Studio B", "aliases": [], "evidence": "not a list"}]}
                    )
                extraction_urls.append("studio-a")
                return _json_dumps(_STUDIO_A_EVIDENCE)
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                return "Studio A is supported."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
                {"url": "https://example.com/studio-b", "title": "Studio B", "snippet": "s"},
            ]
        )

        async def fake_read(_client, url: str):
            return {
                "url": url,
                "title": url.rsplit("/", 1)[-1],
                "content": "Studio A designs offices.",
            }

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=AsyncMock(side_effect=fake_read)) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 2,
                    "max_articles": 2,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["output"], "Studio A is supported.")
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], ["Studio A"])
        self.assertEqual([item["url"] for item in payload["articles"]], ["https://example.com/studio-a"])
        self.assertEqual(
            extraction_urls.count("studio-b"),
            web_research_owner.EVIDENCE_EXTRACTION_MAX_ATTEMPTS,
        )
        self.assertEqual(extraction_urls.count("studio-a"), 1)

    def test_web_research_evidence_matrix_answer_retries_after_upstream_failure(self):
        synthesis_calls = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(_APPLIED_EVIDENCE_PLAN)
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(_STUDIO_A_EVIDENCE)
            if "Собери итоговый исследовательский ответ строго по evidence matrix" in prompt:
                synthesis_calls.append(prompt)
                if len(synthesis_calls) == 1:
                    raise HTTPException(status_code=503, detail="Internal gateway model failed: upstream down")
                return "Studio A is supported."
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": "Studio A designs offices.",
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Studio A is supported.")
        self.assertEqual(len(synthesis_calls), 2)

    def test_web_research_answer_retries_after_upstream_failure(self):
        synthesis_calls = []

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "not_applicable",
                        "task_type": "general_research",
                        "candidate_type": "",
                        "requirements": [],
                    }
                )
            if "Проанализируй источник" in prompt:
                return "- Relevant fact with source"
            if "Собери единый связный исследовательский ответ" in prompt:
                synthesis_calls.append(prompt)
                if len(synthesis_calls) == 1:
                    raise HTTPException(status_code=503, detail="Internal gateway model failed: upstream down")
                return "Synthesized answer."
            return ""

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client() as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "what happened",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["output"], "Synthesized answer.")
        self.assertEqual(len(synthesis_calls), 2)

    def test_web_research_evidence_matrix_checks_quotes_against_original_article_content(self):
        original_content = "Studio A designs offices. " + ("x" * web_api.ARTICLE_RELEVANCE_THRESHOLD_CHARS)
        prepared_content = "Studio A has certified office practice."

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            prompt = messages[-1]["content"]
            if "Определи, нужно ли включать evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "mode": "applied",
                        "task_type": "vendor_selection",
                        "candidate_type": "design studio",
                        "requirements": [
                            {
                                "id": "specialization",
                                "label": "Specialization",
                                "description": "Candidate has relevant specialization",
                                "required": True,
                                "min_sources": 1,
                            }
                        ],
                    }
                )
            if "Оставь только текст статьи" in prompt:
                return prepared_content
            if "Извлеки evidence matrix" in prompt:
                return _json_dumps(
                    {
                        "candidates": [
                            {
                                "name": "Studio A",
                                "aliases": [],
                                "evidence": [
                                    {
                                        "criterion_id": "specialization",
                                        "status": "supports",
                                        "claim": prepared_content,
                                        "quote": prepared_content,
                                        "confidence": 0.9,
                                    }
                                ],
                            }
                        ]
                    }
                )
            return ""

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/studio-a", "title": "Studio A", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/studio-a",
                "title": "Studio A",
                "content": original_content,
            }
        )

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "choose an office design studio",
                    "max_results": 1,
                    "max_articles": 1,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["evidence_matrix"]["passed_candidates"], [])
        self.assertEqual(payload["articles"], [])
        self.assertIn("Insufficient evidence", payload["output"])
        rerank_payloads = [
            call.kwargs["json"]
            for call in fake_http_client.post.await_args_list
            if "text_2" in call.kwargs.get("json", {})
        ]
        self.assertEqual(rerank_payloads, [])

    def test_web_research_rerank_uses_next_route_after_downstream_failure(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["rerank"][0]["routes"].append(
            {
                "provider": "openai",
                "model": "fallback-rerank-model",
                "target_path": "/fallback-score",
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")
        self.config_loader.load_operation_rules()

        rerank_calls = []

        def fake_post(url, *, json=None, **kwargs):
            if isinstance(json, dict) and "text_1" in json and "text_2" in json:
                rerank_calls.append((url, dict(json)))
                if len(rerank_calls) == 1:
                    return _FakeDownstreamResponse({"error": {"message": "primary-rerank-down"}}, status_code=503)
                return _FakeDownstreamResponse(
                    {
                        "data": [{"index": 0, "score": 0.9}],
                        "usage": {"prompt_tokens": 2, "completion_tokens": 0, "total_tokens": 2, "cost": 0.02},
                    }
                )
            return self._fake_post(url, json=json, **kwargs)

        search_adapter = AsyncMock(
            return_value=[
                {
                    "url": "https://example.com/article",
                    "title": "Example Article",
                    "snippet": "Short snippet",
                }
            ]
        )
        with patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])):
            with self._client(post_side_effect=fake_post, search_adapter=search_adapter) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ):
                response = client.post(
                    "/v1/web/research",
                    json={
                        "model": "llmgateway/web-research",
                        "query": "topic",
                        "max_results": 1,
                        "max_articles": 1,
                        "num_queries": 1,
                        "language": "en",
                        "output_language": "en",
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(rerank_calls), 2)
        self.assertEqual(rerank_calls[0][0], "https://openai.example/v1/score")
        self.assertEqual(rerank_calls[1][0], "https://openai.example/v1/fallback-score")
        self.assertEqual(rerank_calls[0][1]["model"], "rerank-model")
        self.assertEqual(rerank_calls[1][1]["model"], "fallback-rerank-model")
        self.assertEqual(response.json()["articles"][0]["rerank_score"], 0.9)

    def test_web_research_article_analysis_runs_parallel_and_synthesizes(self):
        running = 0
        max_running = 0

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            nonlocal running, max_running
            prompt = messages[-1]["content"]
            if "Проанализируй источник" in prompt:
                running += 1
                max_running = max(max_running, running)
                await asyncio.sleep(0.01)
                running -= 1
                return "- Relevant fact with source"
            if "Собери единый связный исследовательский ответ" in prompt:
                self.assertIn("языке: English", prompt)
                return "Single synthesized answer."
            return ""

        async def run_analysis():
            with patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ):
                return await web_api._analyze_articles(
                    Mock(),
                    Mock(),
                    Mock(),
                    analysis_model="llmgateway/light_model",
                    query="topic",
                    output_language="English",
                    articles=[
                        {"url": "https://example.com/1", "title": "One", "content": "one"},
                        {"url": "https://example.com/2", "title": "Two", "content": "two"},
                        {"url": "https://example.com/3", "title": "Three", "content": "three"},
                    ],
                    usage_accumulator=web_api._UsageAccumulator(),
                )

        output = run_async(run_analysis())

        self.assertEqual(output, "Single synthesized answer.")
        self.assertGreater(max_running, 1)

    def test_web_research_article_analysis_does_not_truncate_prepared_content(self):
        tail_marker = "TAIL_MARKER relevant detail beyond old limit"
        long_content = ("x" * web_api.ARTICLE_RELEVANCE_THRESHOLD_CHARS) + tail_marker
        seen_analysis_prompt = None

        async def fake_call_internal_text_model(
            _request,
            _config_loader,
            _http_client,
            *,
            model,
            messages,
            temperature,
            max_tokens,
            usage_accumulator,
        ):
            nonlocal seen_analysis_prompt
            prompt = messages[-1]["content"]
            if "Проанализируй источник" in prompt:
                seen_analysis_prompt = prompt
                return "- Tail fact with source"
            if "Собери единый связный исследовательский ответ" in prompt:
                return "Single synthesized answer."
            return ""

        async def run_analysis():
            with patch(
                "llm_gateway_core.api.v1.web_research_orchestration._call_internal_text_model",
                side_effect=fake_call_internal_text_model,
            ):
                return await web_api._analyze_articles(
                    Mock(),
                    Mock(),
                    Mock(),
                    analysis_model="llmgateway/light_model",
                    query="topic",
                    output_language="English",
                    articles=[
                        {"url": "https://example.com/long", "title": "Long", "content": long_content},
                    ],
                    usage_accumulator=web_api._UsageAccumulator(),
                )

        output = run_async(run_analysis())

        self.assertEqual(output, "Single synthesized answer.")
        self.assertIsNotNone(seen_analysis_prompt)
        self.assertIn(tail_marker, seen_analysis_prompt)

    def test_web_read_rejects_virtual_key_without_service_model(self):
        record = ApiKeyRecord(
            id=8,
            name="restricted",
            api_key="lgk_test",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=["llmgateway/web-search"],
        )

        with self._client(_FakeApiKeysDB(record)) as (client, fake_http_client, _search_adapter, read_adapter):
            response = client.post(
                "/v1/web/read",
                json={"model": "llmgateway/web-read", "url": "https://example.com/article"},
                headers={"Authorization": "Bearer lgk_test"},
            )

        self.assertEqual(response.status_code, 403)
        fake_http_client.post.assert_not_awaited()
        read_adapter.assert_not_awaited()

    def test_web_deep_research_uses_gpt_researcher_service_model(self):
        record = ApiKeyRecord(
            id=9,
            name="restricted",
            api_key="lgk_deep",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=[
                "llmgateway/web-deep-research",
                "llmgateway/web-search",
                "llmgateway/web-read",
                "llmgateway/image-gen",
            ],
        )
        api_keys_db = _FakeApiKeysDB(record)
        fake_research = _FakeDeepResearchManager()

        with (
            patch.object(
                web_api,
                "_run_deep_research_process",
                side_effect=fake_research.run,
            ),
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", new_callable=AsyncMock) as generate_queries,
            self._client(api_keys_db) as (client, fake_http_client, _search_adapter, _read_adapter),
            _deep_research_accounting_passthrough() as accounting,
        ):
            process_image_storage = client.app.state.services.image_storage
            response = client.post(
                "/v1/web/deep-research",
                json={
                    "model": "llmgateway/web-deep-research",
                    "query": "topic",
                    "max_words": 1200,
                    "language": "zh",
                },
                headers={"Authorization": "Bearer lgk_deep"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["object"], "web_deep_research")
        self.assertEqual(payload["model"], "llmgateway/web-deep-research")
        self.assertEqual(payload["output"], "Deep report")
        self.assertEqual(payload["source_urls"], ["https://example.com/article"])
        self.assertEqual(payload["usage"]["cost"], 0.2)
        job = _FakeDeepResearchManager.calls[0]["job"]
        self.assertTrue(job.gateway_api_key.startswith("dr1."))
        self.assertNotEqual(job.gateway_api_key, "lgk_deep")
        self.assertEqual(job.fast_model, "llmgateway/light_model")
        self.assertEqual(job.smart_model, "llmgateway/light_model")
        self.assertEqual(job.strategic_model, "llmgateway/light_model")
        self.assertEqual(job.embedding_model, "llmgateway/embedding")
        self.assertEqual(job.concurrency, 6)
        self.assertEqual(job.language, "Chinese")
        self.assertIs(job.image_generation_enabled, False)
        callback_context = _FakeDeepResearchManager.calls[0]["callbacks"].handle.__self__
        self.assertIs(callback_context.services.image_storage, process_image_storage)
        self.assertEqual(accounting.child_routes, ["/v1/web/search", "/v1/web/read"])
        self.assertEqual(accounting.rollup_cost_usd, 0.0)
        self.assertEqual(api_keys_db.spent_calls, [])
        generate_queries.assert_not_awaited()
        fake_http_client.post.assert_not_awaited()

    def test_web_deep_research_does_not_block_ui_requests(self):
        async def scenario(client):
            started = asyncio.Event()

            async def slow_process(_runner, job, _callbacks):
                started.set()
                await asyncio.sleep(0.6)
                return DeepResearchResult(
                    query=job.query,
                    report="Deep report",
                    sources=(),
                    source_urls=(),
                    context=(),
                    research_result={"status": "ok"},
                    costs=0.01,
                )

            transport = ASGITransport(app=client.app)
            headers = {"Authorization": "Bearer test-gateway-key"}
            with patch.object(
                web_api,
                "_run_deep_research_process",
                side_effect=slow_process,
            ):
                async with HttpxAsyncClient(
                    transport=transport,
                    base_url="http://testserver",
                ) as async_client:
                    deep_request = asyncio.create_task(
                        async_client.post(
                            "/v1/web/deep-research",
                            json={"model": "llmgateway/web-deep-research", "query": "topic"},
                            headers=headers,
                        )
                    )
                    await asyncio.wait_for(started.wait(), timeout=1.0)

                    started_at = time.monotonic()
                    ui_response = await asyncio.wait_for(
                        async_client.get("/v1/ui/playground", headers=headers),
                        timeout=0.25,
                    )
                    elapsed = time.monotonic() - started_at

                    deep_response = await asyncio.wait_for(deep_request, timeout=1.0)
                    return ui_response, deep_response, elapsed

        with (
            self._client() as (client, _fake_http_client, _search_adapter, _read_adapter),
            _deep_research_accounting_passthrough(),
        ):
            ui_response, deep_response, elapsed = client.portal.call(scenario, client)

        self.assertEqual(ui_response.status_code, 200)
        self.assertIn("Playground", ui_response.text)
        self.assertLess(elapsed, 0.25)
        self.assertEqual(deep_response.status_code, 200)

    def test_web_deep_research_can_enable_configured_image_generation(self):
        record = ApiKeyRecord(
            id=10,
            name="restricted",
            api_key="lgk_deep_images",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=[
                "llmgateway/web-deep-research",
                "llmgateway/web-search",
                "llmgateway/web-read",
                "llmgateway/image-gen",
            ],
        )
        api_keys_db = _FakeApiKeysDB(record)
        fake_research = _FakeDeepResearchManager()

        async def post(url, **kwargs):
            if url.endswith("/v1/images/generations"):
                return _FakeDownstreamResponse({"data": [{"b64_json": base64.b64encode(b"png-bytes").decode()}]})
            return await self._fake_post(url, **kwargs)

        with (
            patch.object(
                web_api,
                "_run_deep_research_process",
                side_effect=fake_research.run,
            ),
            self._client(api_keys_db, post_side_effect=post) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
            _deep_research_accounting_passthrough() as accounting,
        ):
            image_storage = client.app.state.services.image_storage
            publish = Mock(side_effect=image_storage.publish_png)
            with patch.object(image_storage, "publish_png", publish):
                response = client.post(
                    "/v1/web/deep-research",
                    json={
                        "model": "llmgateway/web-deep-research",
                        "query": "topic",
                        "image_generation": True,
                    },
                    headers={"Authorization": "Bearer lgk_deep_images"},
                )

        self.assertEqual(response.status_code, 200, response.json())
        call = _FakeDeepResearchManager.calls[0]
        job = call["job"]
        self.assertIs(job.image_generation_enabled, True)
        self.assertEqual(job.image_generation_model, "llmgateway/image-gen")
        self.assertEqual(job.image_generation_size, "1024x1024")
        self.assertTrue(job.gateway_api_key.startswith("dr1."))
        self.assertNotEqual(job.gateway_api_key, "lgk_deep_images")
        self.assertEqual(job.language, "Russian")
        self.assertEqual(set(call["callback_images"][0]), {"url", "prompt", "alt_text"})
        self.assertNotIn("path", call["callback_images"][0])
        self.assertNotIn("absolute_url", call["callback_images"][0])
        publish.assert_called_once()
        self.assertEqual(response.json()["images"], list(call["callback_images"]))
        self.assertEqual(accounting.rollup_cost_usd, 0.0)
        self.assertEqual(api_keys_db.spent_calls, [])

    def test_web_deep_research_rejects_image_generation_when_image_model_disallowed(self):
        record = ApiKeyRecord(
            id=11,
            name="restricted",
            api_key="lgk_deep_only",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=["llmgateway/web-deep-research"],
        )
        api_keys_db = _FakeApiKeysDB(record)

        with (
            self._client(api_keys_db) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
            _deep_research_accounting_passthrough(),
        ):
            response = client.post(
                "/v1/web/deep-research",
                json={
                    "model": "llmgateway/web-deep-research",
                    "query": "topic",
                    "image_generation": True,
                },
                headers={"Authorization": "Bearer lgk_deep_only"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertIn("llmgateway/image-gen", response.json()["detail"])

    def test_web_deep_research_returns_images_field_empty_by_default(self):
        with (
            patch.object(
                web_api,
                "_run_deep_research_process",
                side_effect=_FakeDeepResearchManager().run,
            ),
            self._client() as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
            _deep_research_accounting_passthrough(),
        ):
            response = client.post(
                "/v1/web/deep-research",
                json={"model": "llmgateway/web-deep-research", "query": "topic"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertIn("images", payload)
        self.assertEqual(payload["images"], [])

    def test_web_deep_research_exposes_generated_images_in_response(self):
        _FakeDeepResearchManager.generated_images_override = [
            {
                "url": "/outputs/images/abc/image_deadbeef_0.png",
                "prompt": "diagram of a cat",
                "alt_text": "Illustration: diagram of a cat",
            }
        ]
        try:
            with (
                patch.object(
                    web_api,
                    "_run_deep_research_process",
                    side_effect=_FakeDeepResearchManager().run,
                ),
                self._client() as (
                    client,
                    _fake_http_client,
                    _search_adapter,
                    _read_adapter,
                ),
                _deep_research_accounting_passthrough(),
            ):
                response = client.post(
                    "/v1/web/deep-research",
                    json={"model": "llmgateway/web-deep-research", "query": "topic"},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
        finally:
            _FakeDeepResearchManager.generated_images_override = None

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(
            payload["images"],
            [
                {
                    "url": "/outputs/images/abc/image_deadbeef_0.png",
                    "prompt": "diagram of a cat",
                    "alt_text": "Illustration: diagram of a cat",
                }
            ],
        )

    def test_web_deep_research_rejects_image_generation_without_configured_model(self):
        self.operation_rules_path.write_text(
            VALID_OPERATION_RULES_TEXT.replace(
                '      "image_generation_model": "llmgateway/image-gen",\n',
                "",
            ),
            encoding="utf-8",
        )
        self.config_loader.load_operation_rules()

        with (
            self._client() as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
            _deep_research_accounting_passthrough(),
        ):
            response = client.post(
                "/v1/web/deep-research",
                json={
                    "model": "llmgateway/web-deep-research",
                    "query": "topic",
                    "image_generation": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 500)
        self.assertIn("image_generation_model", response.json()["detail"])

    def test_web_search_falls_back_to_next_adapter_when_first_fails(self):
        primary_adapter = AsyncMock(side_effect=RuntimeError("primary search unavailable"))
        secondary_adapter = AsyncMock(
            return_value=[
                {
                    "url": "https://example.com/fallback",
                    "title": "Fallback Article",
                    "snippet": "From secondary",
                }
            ]
        )

        with ExitStack() as stack:
            install_web_accounting_passthrough(stack)
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient", return_value=fake_http_client)
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.ApiKeysDB", return_value=_FakeApiKeysDB()))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
            # Enable two adapters — Tavily (first in order after Proxy is off) and Z.AI.
            stack.enter_context(patch.object(main.settings, "proxy_url", None))
            stack.enter_context(patch.object(main.settings, "tavily_api_key", "dummy"))
            stack.enter_context(patch.object(main.settings, "jina_api_key", None))
            stack.enter_context(patch.object(main.settings, "zai_api_key", "dummy"))
            stack.enter_context(
                patch.dict(
                    "llm_gateway_core.api.v1.web_adapters._SEARCH_ADAPTERS",
                    {"tavily": primary_adapter, "zai": secondary_adapter},
                    clear=False,
                )
            )

            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/web/search",
                    json={"model": "llmgateway/web-search", "query": "topic", "max_results": 3},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["data"][0]["url"], "https://example.com/fallback")
        primary_adapter.assert_awaited()
        secondary_adapter.assert_awaited()

    def test_zai_web_search_uses_mcp_streamable_http(self):
        calls = []
        mcp_handler = _make_zai_mcp_handler(
            payload_obj=[
                {
                    "link": "https://example.com/zai",
                    "title": "Z.AI result",
                    "content": "Search result content",
                }
            ]
        )

        class FakeAsyncClient:
            async def post(self, url, *, headers=None, json=None, **kwargs):
                calls.append({"url": url, "headers": headers, "json": json})
                return await mcp_handler(url, headers=headers, json=json, **kwargs)

        with patch.object(web_api.settings, "zai_api_key", "dummy-zai-key"):
            results = run_async(web_api._search_zai(FakeAsyncClient(), "topic", 1))

        self.assertEqual(calls[0]["url"], "https://api.z.ai/api/mcp/web_search_prime/mcp")
        self.assertEqual(calls[0]["json"]["method"], "initialize")
        tool_call = next(c for c in calls if (c["json"] or {}).get("method") == "tools/call")
        self.assertEqual(tool_call["json"]["params"]["name"], "web_search_prime")
        self.assertEqual(tool_call["json"]["params"]["arguments"]["search_query"], "topic")
        self.assertEqual(tool_call["json"]["params"]["arguments"]["location"], "us")
        self.assertEqual(results[0]["url"], "https://example.com/zai")

    def test_builtin_web_adapters_use_round_robin_key_from_comma_separated_settings(self):
        calls = []

        class FakeResponse:
            def __init__(self, payload):
                self._payload = payload

            def raise_for_status(self):
                return None

            def json(self):
                return self._payload

        zai_search_handler = _make_zai_mcp_handler(payload_obj=[{"link": "https://example.com/zai"}])
        zai_read_handler = _make_zai_mcp_handler(
            payload_obj={
                "title": "Z.AI title",
                "content": "Z.AI content",
            }
        )

        class FakeAsyncClient:
            async def post(self, url, *, headers=None, json=None, **kwargs):
                calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json or {}})
                if url.endswith("/search"):
                    return FakeResponse({"results": [{"url": "https://example.com/tavily"}]})
                if url.endswith("/extract"):
                    return FakeResponse({"results": [{"raw_content": "Tavily content"}]})
                if "/web_search_prime/" in url:
                    return await zai_search_handler(url, headers=headers, json=json, **kwargs)
                if "/web_reader/" in url:
                    return await zai_read_handler(url, headers=headers, json=json, **kwargs)
                raise AssertionError(f"Unexpected POST URL: {url}")

            async def get(self, url, *, headers=None, **kwargs):
                calls.append({"method": "GET", "url": url, "headers": headers or {}, "json": {}})
                if "s.jina.ai" in url:
                    return FakeResponse({"code": 200, "data": [{"url": "https://example.com/jina"}]})
                if "r.jina.ai" in url:
                    return FakeResponse({"data": {"content": "Jina content"}})
                raise AssertionError(f"Unexpected GET URL: {url}")

        fake_client = FakeAsyncClient()
        with (
            patch.object(web_api.settings, "tavily_api_key", "tavily-one, tavily-two"),
            patch.object(web_api.settings, "jina_api_key", "jina-one, jina-two"),
            patch.object(web_api.settings, "zai_api_key", "zai-one, zai-two"),
        ):
            run_async(web_api._search_tavily(fake_client, "topic", 1))
            run_async(web_api._read_tavily(fake_client, "https://example.com/article"))
            run_async(web_api._search_jina(fake_client, "topic", 1))
            run_async(web_api._read_jina(fake_client, "https://example.com/article"))
            run_async(web_api._search_zai(fake_client, "topic", 1))
            run_async(web_api._read_zai(fake_client, "https://example.com/article"))

        tavily_payloads = [call["json"] for call in calls if "tavily.com" in call["url"]]
        self.assertEqual([payload["api_key"] for payload in tavily_payloads], ["tavily-one", "tavily-two"])
        jina_headers = [call["headers"]["Authorization"] for call in calls if "jina.ai" in call["url"]]
        self.assertEqual(jina_headers, ["Bearer jina-one", "Bearer jina-two"])
        zai_headers = [call["headers"]["Authorization"] for call in calls if "z.ai" in call["url"]]
        deduped = [h for i, h in enumerate(zai_headers) if i == 0 or zai_headers[i] != zai_headers[i - 1]]
        self.assertEqual(deduped, ["Bearer zai-one", "Bearer zai-two"])

    def test_web_adapter_enabled_ignores_empty_comma_separated_keys(self):
        with (
            patch.object(web_api.settings, "tavily_api_key", " , "),
            patch.object(web_api.settings, "jina_api_key", " , "),
            patch.object(web_api.settings, "zai_api_key", " , "),
        ):
            self.assertFalse(web_api._search_adapter_enabled("tavily"))
            self.assertFalse(web_api._search_adapter_enabled("jina"))
            self.assertFalse(web_api._search_adapter_enabled("zai"))

    def test_web_research_zai_search_uses_mcp_streamable_http(self):
        calls = []
        mcp_handler = _make_zai_mcp_handler(payload_obj=[{"link": "https://example.com/research", "title": "Research"}])

        class FakeAsyncClient:
            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, traceback):
                return None

            async def post(self, url, *, headers=None, json=None, **kwargs):
                calls.append({"url": url, "headers": headers, "json": json})
                return await mcp_handler(url, headers=headers, json=json, **kwargs)

        client = web_research_agent.WebResearchClient(
            research_model="llmgateway/light_model",
            zai_api_key="first-zai-key, second-zai-key",
        )

        with patch.object(web_research_agent.httpx, "AsyncClient", return_value=FakeAsyncClient()):
            links = run_async(client._zai_search("topic", 1))

        self.assertEqual(calls[0]["url"], "https://api.z.ai/api/mcp/web_search_prime/mcp")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer first-zai-key")
        tool_call = next(c for c in calls if (c["json"] or {}).get("method") == "tools/call")
        self.assertEqual(tool_call["json"]["params"]["name"], "web_search_prime")
        self.assertEqual(tool_call["json"]["params"]["arguments"]["search_query"], "topic")
        self.assertEqual(links, ["https://example.com/research"])

    def test_zai_mcp_tool_call_handles_three_step_protocol_and_decodes_payload(self):
        calls = []
        handler = _make_zai_mcp_handler(payload_obj={"title": "T", "content": "C"})

        class FakeAsyncClient:
            async def post(self, url, *, headers=None, json=None, **kwargs):
                calls.append({"url": url, "headers": headers, "json": json})
                return await handler(url, headers=headers, json=json, **kwargs)

        result = run_async(
            zai_mcp_module.zai_mcp_tool_call(
                FakeAsyncClient(),
                api_key="key-X",
                server_path="web_reader",
                tool_name="webReader",
                arguments={"url": "https://example.com"},
            )
        )

        self.assertEqual(result, {"title": "T", "content": "C"})
        methods = [(c["json"] or {}).get("method") for c in calls]
        self.assertEqual(methods, ["initialize", "notifications/initialized", "tools/call"])
        self.assertEqual(calls[0]["url"], "https://api.z.ai/api/mcp/web_reader/mcp")
        self.assertEqual(calls[0]["headers"]["Authorization"], "Bearer key-X")
        self.assertEqual(calls[1]["headers"]["Mcp-Session-Id"], "sid-1")
        self.assertEqual(calls[2]["headers"]["Mcp-Session-Id"], "sid-1")

    def test_zai_mcp_tool_call_raises_on_tool_error_envelope(self):
        async def handler(url, *, headers=None, json=None, **kwargs):
            method = (json or {}).get("method", "")
            if method == "initialize":
                envelope = {"jsonrpc": "2.0", "id": (json or {}).get("id"), "result": {}}
                return _FakeMCPResponse(
                    headers={"mcp-session-id": "sid-err"},
                    text=f"data: {jsonlib_dumps(envelope)}\n",
                )
            if method == "notifications/initialized":
                return _FakeMCPResponse(text="")
            envelope = {
                "jsonrpc": "2.0",
                "id": (json or {}).get("id"),
                "error": {"code": -32603, "message": "Tool not found: webReader"},
            }
            return _FakeMCPResponse(text=f"data: {jsonlib_dumps(envelope)}\n")

        class FakeAsyncClient:
            async def post(self, url, *, headers=None, json=None, **kwargs):
                return await handler(url, headers=headers, json=json, **kwargs)

        with self.assertRaises(RuntimeError) as ctx:
            run_async(
                zai_mcp_module.zai_mcp_tool_call(
                    FakeAsyncClient(),
                    api_key="k",
                    server_path="web_reader",
                    tool_name="webReader",
                    arguments={"url": "https://example.com"},
                )
            )
        self.assertIn("Tool not found", str(ctx.exception))

    def test_zai_mcp_search_location_autodetect(self):
        self.assertEqual(zai_mcp_module.detect_zai_search_location("hello world"), "us")
        self.assertEqual(zai_mcp_module.detect_zai_search_location("привет мир"), "us")
        self.assertEqual(zai_mcp_module.detect_zai_search_location("北京天气"), "cn")

    def test_web_read_prefers_direct_fetch_before_adapters(self):
        direct_payload = {
            "url": "https://example.com/article",
            "title": "Direct Title",
            "content": "Directly fetched content",
        }

        with self._client(direct_fetch_result=direct_payload) as (
            client,
            fake_http_client,
            _search_adapter,
            read_adapter,
        ):
            response = client.post(
                "/v1/web/read",
                json={"model": "llmgateway/web-read", "url": "https://example.com/article"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["title"], "Direct Title")
        self.assertEqual(payload["content"], "Directly fetched content")
        read_adapter.assert_not_awaited()

    def test_direct_fetch_returns_youtube_transcript_when_available(self):
        class _FakeTranscript:
            language = "en"

            def fetch(self):
                return [{"text": "hello"}, {"text": "world"}]

        class _FakeTranscriptList:
            def find_transcript(self, languages):
                self.languages = languages
                return _FakeTranscript()

        class _FakeYouTubeTranscriptApi:
            # The gateway passes proxy_config when YOUTUBE_PROXY_URL is set, so
            # the fake accepts kwargs and the proxy stays pinned off here.
            def __init__(self, **kwargs):
                self.kwargs = kwargs

            def list(self, video_id):
                self.video_id = video_id
                return _FakeTranscriptList()

        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
        ):
            result = run_async(web_api._direct_http_fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))

        self.assertEqual(
            result,
            {
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "title": "YouTube: dQw4w9WgXcQ (en)",
                "content": "hello world",
            },
        )

    def test_direct_fetch_routes_medium_through_freedium(self):
        # _get_pinned_public_url always builds its own pinned httpx.AsyncClient
        # rather than accepting one from a caller, so the test swaps only the
        # transport it uses (via a patched _PinnedHostAsyncHTTPTransport) and
        # lets the real pinned-fetch code path run end to end.
        requested_urls = []

        def handler(request):
            requested_urls.append(str(request.url))
            return web_safe_fetch_owner.httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=b"<html><head><title>Medium title</title></head><body>body</body></html>",
                request=request,
            )

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Freedium article content")

        with (
            patch.object(
                web_safe_fetch_owner,
                "_validate_public_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch.object(
                web_safe_fetch_owner,
                "_PinnedHostAsyncHTTPTransport",
                lambda **_kwargs: web_safe_fetch_owner.httpx.MockTransport(handler),
            ),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://medium.com/@user/post"))

        self.assertEqual(
            requested_urls,
            ["https://freedium-mirror.cfd/https://medium.com/@user/post"],
        )
        self.assertEqual(result["url"], "https://medium.com/@user/post")
        self.assertEqual(result["title"], "Medium title")
        self.assertEqual(result["content"], "Freedium article content")

    def test_direct_fetch_falls_back_to_medium_when_freedium_fails(self):
        # _get_pinned_public_url always builds its own pinned httpx.AsyncClient
        # rather than accepting one from a caller, so the test swaps only the
        # transport it uses (via a patched _PinnedHostAsyncHTTPTransport) and
        # lets the real pinned-fetch code path (including the real
        # raise_for_status()) run end to end.
        requested_urls = []

        def handler(request):
            requested_urls.append(str(request.url))
            status_code = 503 if len(requested_urls) == 1 else 200
            return web_safe_fetch_owner.httpx.Response(
                status_code,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=b"<html><head><title>Direct title</title></head><body>body</body></html>",
                request=request,
            )

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Direct Medium content")

        with (
            patch.object(
                web_safe_fetch_owner,
                "_validate_public_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch.object(
                web_safe_fetch_owner,
                "_PinnedHostAsyncHTTPTransport",
                lambda **_kwargs: web_safe_fetch_owner.httpx.MockTransport(handler),
            ),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://medium.com/@user/post"))

        self.assertEqual(
            requested_urls,
            [
                "https://freedium-mirror.cfd/https://medium.com/@user/post",
                "https://medium.com/@user/post",
            ],
        )
        self.assertEqual(result["url"], "https://medium.com/@user/post")
        self.assertEqual(result["title"], "Direct title")
        self.assertEqual(result["content"], "Direct Medium content")

    def test_direct_fetch_returns_best_effort_images_without_retrying(self):
        html = """
        <html>
          <head>
            <title>Article title</title>
            <meta property="og:image" content="/og.jpg" />
          </head>
          <body>
            <article>
              <p>Body</p>
              <img src="/hero.jpg" alt="Hero" />
              <img srcset="/small.jpg 320w, /large.jpg 1280w" alt="Large" />
              <img src="/img/avatars/user.png" alt="avatar" />
            </article>
          </body>
        </html>
        """

        # _get_pinned_public_url always builds its own pinned httpx.AsyncClient
        # rather than accepting one from a caller, so the test swaps only the
        # transport it uses (via a patched _PinnedHostAsyncHTTPTransport) and
        # lets the real pinned-fetch code path run end to end.
        requested_urls = []

        def handler(request):
            requested_urls.append(str(request.url))
            return web_safe_fetch_owner.httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=html.encode("utf-8"),
                request=request,
            )

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Article body")

        with (
            patch.object(
                web_safe_fetch_owner,
                "_validate_public_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch.object(
                web_safe_fetch_owner,
                "_PinnedHostAsyncHTTPTransport",
                lambda **_kwargs: web_safe_fetch_owner.httpx.MockTransport(handler),
            ),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://example.com/post"))

        self.assertEqual(requested_urls, ["https://example.com/post"])
        fake_trafilatura.extract.assert_called_once()
        self.assertTrue(fake_trafilatura.extract.call_args.kwargs["include_links"])
        self.assertTrue(fake_trafilatura.extract.call_args.kwargs["include_images"])
        self.assertEqual(fake_trafilatura.extract.call_args.kwargs["url"], "https://example.com/post")
        self.assertEqual(result["content"], "Article body")
        self.assertNotIn("https://example.com/hero.jpg", result["content"])
        self.assertNotIn("https://example.com/large.jpg", result["content"])
        self.assertNotIn("https://example.com/og.jpg", result["content"])
        self.assertNotIn("avatars", result["content"])
        self.assertEqual(
            result["images"],
            [
                {"url": "https://example.com/hero.jpg", "description": "Hero"},
                {"url": "https://example.com/large.jpg", "description": "Large"},
                {"url": "https://example.com/og.jpg", "description": ""},
            ],
        )

    def test_direct_fetch_keeps_inline_image_links(self):
        # Regression guard: web read must return image links inline in the content. This runs
        # the REAL trafilatura extractor (no mock) — if the trafilatura dependency is missing,
        # the direct pipeline silently degrades to text-only and this assertion fails.
        html = """
        <html><head><title>Article title</title></head>
          <body><article>
            <h1>Заголовок статьи</h1>
            <p>Достаточно длинный первый абзац статьи, чтобы извлекатель уверенно
               распознал основное тело статьи и сохранил вложенные иллюстрации.</p>
            <figure>
              <img src="https://example.com/hero.jpg" alt="Главное фото"/>
              <figcaption>Подпись к фото</figcaption>
            </figure>
            <p>Второй абзац статьи после иллюстрации, продолжающий мысль автора.</p>
          </article></body>
        </html>
        """

        # _get_pinned_public_url always builds its own pinned httpx.AsyncClient
        # rather than accepting one from a caller, so the test swaps only the
        # transport it uses (via a patched _PinnedHostAsyncHTTPTransport) and
        # lets the real pinned-fetch code path run end to end.
        def handler(request):
            return web_safe_fetch_owner.httpx.Response(
                200,
                headers={"Content-Type": "text/html; charset=utf-8"},
                content=html.encode("utf-8"),
                request=request,
            )

        with (
            patch.object(
                web_safe_fetch_owner,
                "_validate_public_fetch_host",
                new_callable=AsyncMock,
                return_value=("93.184.216.34",),
            ),
            patch.object(
                web_safe_fetch_owner,
                "_PinnedHostAsyncHTTPTransport",
                lambda **_kwargs: web_safe_fetch_owner.httpx.MockTransport(handler),
            ),
        ):
            result = run_async(web_api._direct_http_fetch("https://example.com/post"))

        self.assertIsNotNone(result)
        self.assertIn("![", result["content"])
        self.assertIn("https://example.com/hero.jpg", result["content"])

    def test_append_images_to_markdown_is_idempotent_for_inlined_images(self):
        content = "body\n\n![A](https://example.com/a.png)"
        images = [
            {"url": "https://example.com/a.png", "description": "A"},
            {"url": "https://example.com/b.png", "description": "B"},
        ]
        out = web_api._append_images_to_markdown(content, images)
        # An already-inlined image is not duplicated; a missing one is appended.
        self.assertEqual(out.count("https://example.com/a.png"), 1)
        self.assertIn("![B](https://example.com/b.png)", out)

    def test_read_tavily_requests_and_returns_images(self):
        calls = []

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "results": [
                        {
                            "title": "Tavily title",
                            "raw_content": "Tavily body",
                            "images": ["https://example.com/tavily.jpg"],
                        }
                    ]
                }

        class _FakeClient:
            async def post(self, url, *, json=None, **_kwargs):
                calls.append({"url": url, "json": json})
                return _FakeResponse()

        with patch.object(web_api.settings, "tavily_api_key", "tavily-key"):
            result = run_async(web_api._read_tavily(_FakeClient(), "https://example.com/article"))

        self.assertEqual(calls[0]["json"]["include_images"], True)
        self.assertEqual(calls[0]["json"]["format"], "markdown")
        # Tavily returns images as a separate list; the adapter inlines them into the markdown.
        self.assertEqual(result["content"], "Tavily body\n\n![](https://example.com/tavily.jpg)")
        self.assertEqual(result["images"], [{"url": "https://example.com/tavily.jpg", "description": ""}])

    def test_read_jina_extracts_images_from_markdown(self):
        calls = []

        class _FakeResponse:
            def raise_for_status(self):
                return None

            def json(self):
                return {
                    "data": {
                        "title": "Jina title",
                        "content": "Jina body\n\n![Chart](https://example.com/chart.png)",
                    }
                }

        class _FakeClient:
            async def get(self, url, *, headers=None, **_kwargs):
                calls.append({"url": url, "headers": headers or {}})
                return _FakeResponse()

        with patch.object(web_api.settings, "jina_api_key", "jina-key"):
            result = run_async(web_api._read_jina(_FakeClient(), "https://example.com/article"))

        self.assertEqual(calls[0]["headers"]["X-Retain-Images"], "all")
        self.assertEqual(calls[0]["headers"]["X-With-Images-Summary"], "true")
        self.assertEqual(calls[0]["headers"]["X-With-Generated-Alt"], "true")
        # Jina keeps the image link inline in the markdown body; it must survive intact.
        self.assertIn("![Chart](https://example.com/chart.png)", result["content"])
        self.assertEqual(result["images"], [{"url": "https://example.com/chart.png", "description": "Chart"}])

    def test_read_zai_requests_image_retention(self):
        fake_call = AsyncMock(
            return_value={
                "title": "Z.AI title",
                "content": "Z.AI body\n\n![Diagram](https://example.com/diagram.png)",
            }
        )

        with (
            patch.object(web_api.settings, "zai_api_key", "zai-key"),
            patch.object(web_adapters_owner, "zai_mcp_tool_call", fake_call),
        ):
            result = run_async(web_api._read_zai(Mock(), "https://example.com/article"))

        arguments = fake_call.call_args.kwargs["arguments"]
        self.assertEqual(arguments["retain_images"], True)
        self.assertEqual(arguments["with_images_summary"], True)
        self.assertEqual(arguments["keep_img_data_url"], False)
        self.assertEqual(result["images"], [{"url": "https://example.com/diagram.png", "description": "Diagram"}])

    def test_cloakbrowser_extract_prefers_playwright_title_over_html_title(self):
        fake_module = Mock()
        fake_module.extract = Mock(return_value="# Type system\n\nbody content")
        with patch.dict("sys.modules", {"trafilatura": fake_module}):
            result = web_api._extract_cloakbrowser_markdown(
                "<html><title>Type system - Wikipedia</title></html>",
                "https://en.wikipedia.org/wiki/Type_system",
                "Type system - Wikipedia",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Type system - Wikipedia")
        self.assertEqual(result["content"], "# Type system\n\nbody content")
        self.assertTrue(fake_module.extract.call_args.kwargs["include_images"])
        self.assertEqual(fake_module.extract.call_args.kwargs["output_format"], "markdown")

    def test_cloakbrowser_extract_keeps_inline_image_links(self):
        fake_module = Mock()
        fake_module.extract = Mock(return_value="text\n\n![Hero](https://example.com/hero.jpg)")
        with patch.dict("sys.modules", {"trafilatura": fake_module}):
            result = web_api._extract_cloakbrowser_markdown(
                "<html><title>T</title></html>", "https://example.com/a", "T"
            )
        self.assertIsNotNone(result)
        self.assertIn("![Hero](https://example.com/hero.jpg)", result["content"])

    def test_cloakbrowser_extract_falls_back_to_html_title_when_page_title_blank(self):
        fake_module = Mock()
        fake_module.extract = Mock(return_value="body")
        with patch.dict("sys.modules", {"trafilatura": fake_module}):
            result = web_api._extract_cloakbrowser_markdown(
                "<html><head><title>HTML Title</title></head><body>body</body></html>",
                "https://x",
                "   ",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "HTML Title")

    def test_web_read_tries_cloakbrowser_before_paid_adapters(self):
        rendered_payload = {
            "url": "https://example.com/rendered",
            "title": "Rendered Title",
            "content": "Rendered page content",
        }

        with self._client(cloakbrowser_fetch_result=rendered_payload) as (
            client,
            _fake_http_client,
            _search_adapter,
            read_adapter,
        ):
            response = client.post(
                "/v1/web/read",
                json={"model": "llmgateway/web-read", "url": "https://example.com/article"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["title"], "Rendered Title")
        self.assertEqual(payload["content"], "Rendered page content")
        read_adapter.assert_not_awaited()

    def test_web_read_falls_back_to_next_adapter_when_first_fails(self):
        primary_adapter = AsyncMock(side_effect=RuntimeError("primary reader unavailable"))
        secondary_adapter = AsyncMock(
            return_value={
                "url": "https://example.com/article",
                "title": "Secondary Title",
                "content": "From secondary reader",
            }
        )

        with ExitStack() as stack:
            install_web_accounting_passthrough(stack)
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient", return_value=fake_http_client)
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.ApiKeysDB", return_value=_FakeApiKeysDB()))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
            stack.enter_context(patch.object(main.settings, "proxy_url", None))
            stack.enter_context(patch.object(main.settings, "tavily_api_key", "dummy"))
            stack.enter_context(patch.object(main.settings, "jina_api_key", None))
            stack.enter_context(patch.object(main.settings, "zai_api_key", "dummy"))
            stack.enter_context(
                patch.dict(
                    "llm_gateway_core.api.v1.web_adapters._READ_ADAPTERS",
                    {"tavily": primary_adapter, "zai": secondary_adapter},
                    clear=False,
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web_adapters._direct_http_fetch",
                    AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web_adapters._cloakbrowser_fetch",
                    AsyncMock(return_value=None),
                )
            )

            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/web/read",
                    json={"model": "llmgateway/web-read", "url": "https://example.com/article"},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["title"], "Secondary Title")
        self.assertEqual(payload["content"], "From secondary reader")
        primary_adapter.assert_awaited_once_with(ANY, "https://example.com/article")
        secondary_adapter.assert_awaited_once_with(ANY, "https://example.com/article")

    def test_extract_text_with_selectolax_strips_noise_and_returns_main_text(self):
        html = (
            "<html><body>"
            "<script>alert(1)</script>"
            "<style>body{}</style>"
            "<nav>menu</nav>"
            "<footer>bottom</footer>"
            "<main><p>Hello world</p><p>Second paragraph.</p></main>"
            "</body></html>"
        )

        text = web_api._extract_text_with_selectolax(html)

        self.assertIn("Hello world", text)
        self.assertIn("Second paragraph.", text)
        self.assertNotIn("alert", text)
        self.assertNotIn("body{}", text)
        self.assertNotIn("menu", text)
        self.assertNotIn("bottom", text)

    def test_prepare_relevant_articles_skips_failed_relevance_calls(self):
        articles = [
            {"url": "https://example.com/ok", "title": "OK", "content": "x" * 20_000},
            {"url": "https://example.com/fail", "title": "Fail", "content": "y" * 20_000},
        ]

        async def fake_extract(_req, _cfg, _hc, *, relevance_model, query, article, usage_accumulator):
            if article["url"] == "https://example.com/fail":
                raise RuntimeError("relevance llm boom")
            return "trimmed content"

        async def scenario():
            with patch(
                "llm_gateway_core.api.v1.web_research_orchestration._extract_relevant_article_content",
                side_effect=fake_extract,
            ):
                return await web_api._prepare_relevant_articles(
                    Mock(),
                    Mock(),
                    Mock(),
                    relevance_model="llmgateway/light_model",
                    query="topic",
                    articles=articles,
                    usage_accumulator=web_api._UsageAccumulator(),
                )

        prepared = run_async(scenario())
        prepared_urls = [a["url"] for a in prepared]
        self.assertEqual(prepared_urls, ["https://example.com/ok"])

    def test_web_research_continues_when_one_url_read_raises(self):
        async def fake_read(_client, url: str):
            if url.endswith("/bad"):
                raise RuntimeError("read crashed")
            return {
                "url": url,
                "title": "Reader Title",
                "content": "Downloaded article content",
            }

        search_adapter = AsyncMock(
            return_value=[
                {"url": "https://example.com/bad", "title": "Bad", "snippet": "s"},
                {"url": "https://example.com/good", "title": "Good", "snippet": "s"},
            ]
        )
        read_adapter = AsyncMock(side_effect=fake_read)

        with (
            patch("llm_gateway_core.api.v1.web_adapters._generate_queries", AsyncMock(return_value=["topic"])),
            self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (
                client,
                _fake_http_client,
                _search_adapter,
                _read_adapter,
            ),
        ):
            response = client.post(
                "/v1/web/research",
                json={
                    "model": "llmgateway/web-research",
                    "query": "topic",
                    "max_results": 2,
                    "max_articles": 5,
                    "num_queries": 1,
                    "language": "en",
                    "output_language": "en",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        urls = [item["url"] for item in payload["sources"]]
        self.assertIn("https://example.com/good", urls)
        self.assertNotIn("https://example.com/bad", urls)


if __name__ == "__main__":
    unittest.main()
