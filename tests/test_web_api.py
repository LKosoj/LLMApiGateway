import asyncio
import json
import threading
import tempfile
import time
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import ANY, AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient
from httpx import ASGITransport, AsyncClient as HttpxAsyncClient

import main
from llm_gateway_core.agents import web_research as web_research_agent
from llm_gateway_core.api.v1 import web as web_api
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.utils import zai_mcp as zai_mcp_module
from tests._async_compat import run_async


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

    def get_by_key(self, api_key: str):
        if self.record and api_key == self.record.api_key:
            return self.record
        return None

    def record_spent(self, key_id: int, amount: float) -> None:
        self.spent_calls.append((key_id, amount))


class _FakeDeepResearchManager:
    calls = []
    generated_images_override: list[dict] | None = None

    async def conduct_deep_research(self, **kwargs):
        self.calls.append(kwargs)
        search_results = await kwargs["gateway_search"]("deep topic", 2)
        article = await kwargs["gateway_read"](search_results[0]["url"])
        result = {
            "report": "Deep report",
            "sources": [{"title": article["title"], "url": article["url"]}],
            "source_urls": [article["url"]],
            "context": [article["content"]],
            "research_result": {"status": "ok"},
            "costs": 0.03,
        }
        if self.__class__.generated_images_override is not None:
            result["generated_images"] = self.__class__.generated_images_override
        return result


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
            "http://169.254.169.254/latest/meta-data/",
            "http://localhost:9000/private",
        )

        for url in blocked_urls:
            with self.subTest(url=url), self.assertRaises(HTTPException) as ctx:
                web_api._validate_http_url(url)
            self.assertEqual(ctx.exception.status_code, 400)

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

            with patch("llm_gateway_core.api.v1.web.CLIENT_DISCONNECT_POLL_SECONDS", 0):
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

    def test_deep_research_worker_cancel_stops_inner_event_loop_task(self):
        class BlockingDeepResearchManager:
            started = threading.Event()
            cancelled = threading.Event()

            async def conduct_deep_research(self, **kwargs):
                self.__class__.started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    self.__class__.cancelled.set()
                    raise

        async def scenario():
            cancellation_event = threading.Event()
            worker = web_api._DeepResearchWorker(cancellation_event=cancellation_event, query="topic")
            task = asyncio.create_task(web_api._conduct_deep_research_in_worker(worker))
            await asyncio.wait_for(asyncio.to_thread(BlockingDeepResearchManager.started.wait), timeout=1.0)
            worker.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await asyncio.wait_for(task, timeout=1.0)
            self.assertTrue(cancellation_event.is_set())
            self.assertTrue(BlockingDeepResearchManager.cancelled.is_set())

        with patch(
            "llm_gateway_core.api.v1.web.DeepResearchManager",
            return_value=BlockingDeepResearchManager(),
        ):
            run_async(scenario())

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
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
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
                    "llm_gateway_core.api.v1.web._SEARCH_ADAPTERS",
                    search_adapter_mapping,
                    clear=False,
                )
            )
            stack.enter_context(
                patch.dict(
                    "llm_gateway_core.api.v1.web._READ_ADAPTERS",
                    {"zai": read_adapter},
                    clear=False,
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web._direct_http_fetch",
                    AsyncMock(return_value=direct_fetch_result),
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web._cloakbrowser_fetch",
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
        self.assertIn("returned no text content", response.json()["detail"])
        self.assertEqual(fake_http_client.post.await_count, 1)
        search_adapter.assert_not_awaited()

    def test_web_search_fails_when_no_adapters_configured(self):
        with ExitStack() as stack:
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
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
        self.assertEqual(api_keys_db.spent_calls[0][0], 7)
        self.assertAlmostEqual(api_keys_db.spent_calls[0][1], 0.11)

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
            patch("llm_gateway_core.api.v1.web._generate_queries", side_effect=fake_generate_queries),
            patch("llm_gateway_core.api.v1.web._resolve_fetch_host", return_value=("93.184.216.34",)),
            patch("llm_gateway_core.api.v1.web._direct_http_fetch", new_callable=AsyncMock, return_value=None),
            patch("llm_gateway_core.api.v1.web._cloakbrowser_fetch", new_callable=AsyncMock, return_value=None),
        ):
            with self._client(search_adapter=search_adapter, read_adapter=read_adapter) as (client, _fake_http_client, _, _):
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch(
                "llm_gateway_core.api.v1.web._call_internal_text_model",
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
            patch("llm_gateway_core.api.v1.web._generate_queries", side_effect=fake_generate_queries),
            patch("llm_gateway_core.api.v1.web._resolve_fetch_host", return_value=("93.184.216.34",)),
            patch("llm_gateway_core.api.v1.web._direct_http_fetch", new_callable=AsyncMock, return_value=None),
            patch("llm_gateway_core.api.v1.web._cloakbrowser_fetch", new_callable=AsyncMock, return_value=None),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
        self.assertEqual([item["url"] for item in payload["articles"]], ["https://example.com/one", "https://example.com/two"])
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
            patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model),
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
        with patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])):
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
            with patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model):
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
            with patch("llm_gateway_core.api.v1.web._call_internal_text_model", side_effect=fake_call_internal_text_model):
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
            allowed_models=["llmgateway/web-deep-research", "llmgateway/image-gen"],
        )
        api_keys_db = _FakeApiKeysDB(record)

        with (
            patch("llm_gateway_core.api.v1.web.DeepResearchManager", return_value=_FakeDeepResearchManager()),
            patch("llm_gateway_core.api.v1.web._generate_queries", new_callable=AsyncMock) as generate_queries,
            self._client(api_keys_db) as (client, fake_http_client, _search_adapter, _read_adapter),
        ):
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
        self.assertEqual(payload["usage"]["cost"], 0.03)
        self.assertEqual(_FakeDeepResearchManager.calls[0]["gateway_search"].__name__, "_gateway_search")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["gateway_read"].__name__, "_gateway_read")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["fast_model"], "llmgateway/light_model")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["smart_model"], "llmgateway/light_model")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["strategic_model"], "llmgateway/light_model")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["embedding_model"], "llmgateway/embedding")
        self.assertEqual(_FakeDeepResearchManager.calls[0]["language"], "Chinese")
        self.assertIs(_FakeDeepResearchManager.calls[0]["image_generation_enabled"], False)
        self.assertEqual(api_keys_db.spent_calls, [(9, 0.03)])
        generate_queries.assert_not_awaited()
        fake_http_client.post.assert_not_awaited()

    def test_web_deep_research_does_not_block_ui_requests(self):
        class BlockingDeepResearchManager:
            started = threading.Event()

            async def conduct_deep_research(self, **kwargs):
                self.__class__.started.set()
                time.sleep(0.6)
                return {
                    "report": "Deep report",
                    "sources": [],
                    "source_urls": [],
                    "context": [],
                    "research_result": {"status": "ok"},
                    "costs": 0.01,
                }

        async def scenario(client):
            transport = ASGITransport(app=client.app)
            headers = {"Authorization": "Bearer test-gateway-key"}
            async with HttpxAsyncClient(transport=transport, base_url="http://testserver") as async_client:
                deep_request = asyncio.create_task(
                    async_client.post(
                        "/v1/web/deep-research",
                        json={"model": "llmgateway/web-deep-research", "query": "topic"},
                        headers=headers,
                    )
                )
                await asyncio.wait_for(asyncio.to_thread(BlockingDeepResearchManager.started.wait), timeout=1.0)

                started_at = time.monotonic()
                ui_response = await asyncio.wait_for(
                    async_client.get("/v1/ui/playground", headers=headers),
                    timeout=0.25,
                )
                elapsed = time.monotonic() - started_at

                deep_response = await asyncio.wait_for(deep_request, timeout=1.0)
                return ui_response, deep_response, elapsed

        with (
            patch(
                "llm_gateway_core.api.v1.web.DeepResearchManager",
                return_value=BlockingDeepResearchManager(),
            ),
            self._client() as (client, _fake_http_client, _search_adapter, _read_adapter),
        ):
            ui_response, deep_response, elapsed = run_async(scenario(client))

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
            allowed_models=["llmgateway/web-deep-research", "llmgateway/image-gen"],
        )
        api_keys_db = _FakeApiKeysDB(record)

        with (
            patch("llm_gateway_core.api.v1.web.DeepResearchManager", return_value=_FakeDeepResearchManager()),
            self._client(api_keys_db) as (client, _fake_http_client, _search_adapter, _read_adapter),
        ):
            response = client.post(
                "/v1/web/deep-research",
                json={
                    "model": "llmgateway/web-deep-research",
                    "query": "topic",
                    "image_generation": True,
                },
                headers={"Authorization": "Bearer lgk_deep_images"},
            )

        self.assertEqual(response.status_code, 200)
        call = _FakeDeepResearchManager.calls[0]
        self.assertIs(call["image_generation_enabled"], True)
        self.assertEqual(call["image_generation_model"], "llmgateway/image-gen")
        self.assertEqual(call["image_generation_size"], "1024x1024")
        self.assertEqual(call["image_generation_api_key"], "lgk_deep_images")
        self.assertEqual(call["language"], "Russian")
        self.assertNotIn("image_generation_provider", call)
        self.assertEqual(api_keys_db.spent_calls, [(10, 0.03)])

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

        with self._client(api_keys_db) as (client, _fake_http_client, _search_adapter, _read_adapter):
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
        with self._client() as (client, _fake_http_client, _search_adapter, _read_adapter):
            with patch(
                "llm_gateway_core.api.v1.web.DeepResearchManager",
                return_value=_FakeDeepResearchManager(),
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
                "path": "/tmp/outputs/images/abc/image_deadbeef_0.png",
                "url": "/outputs/images/abc/image_deadbeef_0.png",
                "absolute_url": "/tmp/outputs/images/abc/image_deadbeef_0.png",
                "prompt": "diagram of a cat",
                "alt_text": "Illustration: diagram of a cat",
            },
            {
                "url": "",
                "prompt": "ignored because url is empty",
            },
            "not a dict — must be skipped",
        ]
        try:
            with self._client() as (client, _fake_http_client, _search_adapter, _read_adapter):
                with patch(
                    "llm_gateway_core.api.v1.web.DeepResearchManager",
                    return_value=_FakeDeepResearchManager(),
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

        with self._client() as (client, _fake_http_client, _search_adapter, _read_adapter):
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
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
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
                    "llm_gateway_core.api.v1.web._SEARCH_ADAPTERS",
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

        zai_search_handler = _make_zai_mcp_handler(
            payload_obj=[{"link": "https://example.com/zai"}]
        )
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
        mcp_handler = _make_zai_mcp_handler(
            payload_obj=[{"link": "https://example.com/research", "title": "Research"}]
        )

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
            def list(self, video_id):
                self.video_id = video_id
                return _FakeTranscriptList()

        with patch("youtube_transcript_api.YouTubeTranscriptApi", _FakeYouTubeTranscriptApi):
            result = run_async(
                web_api._direct_http_fetch("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
            )

        self.assertEqual(
            result,
            {
                "url": "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
                "title": "YouTube: dQw4w9WgXcQ (en)",
                "content": "hello world",
            },
        )

    def test_direct_fetch_routes_medium_through_freedium(self):
        class _FakeResponse:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = "<html><head><title>Medium title</title></head><body>body</body></html>"
            content = text.encode("utf-8")

            def raise_for_status(self):
                return None

        class _FakeClient:
            def __init__(self):
                self.requested_urls = []

            async def get(self, url, **_kwargs):
                self.requested_urls.append(url)
                return _FakeResponse()

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Freedium article content")
        fake_client = _FakeClient()

        with (
            patch.object(web_api, "_validate_public_fetch_host", return_value=None),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://medium.com/@user/post", fake_client))

        self.assertEqual(
            fake_client.requested_urls,
            ["https://freedium-mirror.cfd/https://medium.com/@user/post"],
        )
        self.assertEqual(result["url"], "https://medium.com/@user/post")
        self.assertEqual(result["title"], "Medium title")
        self.assertEqual(result["content"], "Freedium article content")

    def test_direct_fetch_falls_back_to_medium_when_freedium_fails(self):
        class _FakeResponse:
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = "<html><head><title>Direct title</title></head><body>body</body></html>"
            content = text.encode("utf-8")

            def __init__(self, *, ok: bool):
                self.status_code = 200 if ok else 503
                self.ok = ok

            def raise_for_status(self):
                if not self.ok:
                    raise web_api.httpx.HTTPError("freedium failed")

        class _FakeClient:
            def __init__(self):
                self.requested_urls = []

            async def get(self, url, **_kwargs):
                self.requested_urls.append(url)
                return _FakeResponse(ok=len(self.requested_urls) > 1)

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Direct Medium content")
        fake_client = _FakeClient()

        with (
            patch.object(web_api, "_validate_public_fetch_host", return_value=None),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://medium.com/@user/post", fake_client))

        self.assertEqual(
            fake_client.requested_urls,
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

        class _FakeResponse:
            status_code = 200
            headers = {"Content-Type": "text/html; charset=utf-8"}
            text = html
            content = html.encode("utf-8")

            def raise_for_status(self):
                return None

        class _FakeClient:
            def __init__(self):
                self.requested_urls = []

            async def get(self, url, **_kwargs):
                self.requested_urls.append(url)
                return _FakeResponse()

        fake_trafilatura = Mock()
        fake_trafilatura.extract = Mock(return_value="Article body")
        fake_client = _FakeClient()

        with (
            patch.object(web_api, "_validate_public_fetch_host", return_value=None),
            patch.dict("sys.modules", {"trafilatura": fake_trafilatura}),
        ):
            result = run_async(web_api._direct_http_fetch("https://example.com/post", fake_client))

        self.assertEqual(fake_client.requested_urls, ["https://example.com/post"])
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
        self.assertEqual(result["content"], "Tavily body")
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
            patch.object(web_api, "zai_mcp_tool_call", fake_call),
        ):
            result = run_async(web_api._read_zai(Mock(), "https://example.com/article"))

        arguments = fake_call.call_args.kwargs["arguments"]
        self.assertEqual(arguments["retain_images"], True)
        self.assertEqual(arguments["with_images_summary"], True)
        self.assertEqual(arguments["keep_img_data_url"], False)
        self.assertEqual(result["images"], [{"url": "https://example.com/diagram.png", "description": "Diagram"}])

    def test_cloakbrowser_extract_prefers_playwright_title_over_extractor_heuristic(self):
        class FakeResult:
            title = "set of rules that assign a property called type"
            content_markdown = "# Type system\n\nbody content"

        fake_module = Mock()
        fake_module.extract = Mock(return_value=FakeResult())
        with patch.dict("sys.modules", {"rs_trafilatura": fake_module}):
            result = web_api._extract_cloakbrowser_markdown(
                "<html><title>Type system - Wikipedia</title></html>",
                "https://en.wikipedia.org/wiki/Type_system",
                "Type system - Wikipedia",
            )
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Type system - Wikipedia")
        self.assertEqual(result["content"], "# Type system\n\nbody content")

    def test_cloakbrowser_extract_falls_back_to_extractor_title_when_page_title_blank(self):
        class FakeResult:
            title = "Some Extracted Title"
            content_markdown = "body"

        fake_module = Mock()
        fake_module.extract = Mock(return_value=FakeResult())
        with patch.dict("sys.modules", {"rs_trafilatura": fake_module}):
            result = web_api._extract_cloakbrowser_markdown("<html></html>", "https://x", "   ")
        self.assertIsNotNone(result)
        self.assertEqual(result["title"], "Some Extracted Title")

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
            fake_http_client = Mock()
            fake_http_client.post = AsyncMock(side_effect=self._fake_post)
            fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
            fake_http_client.aclose = AsyncMock()
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
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
                    "llm_gateway_core.api.v1.web._READ_ADAPTERS",
                    {"tavily": primary_adapter, "zai": secondary_adapter},
                    clear=False,
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web._direct_http_fetch",
                    AsyncMock(return_value=None),
                )
            )
            stack.enter_context(
                patch(
                    "llm_gateway_core.api.v1.web._cloakbrowser_fetch",
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
                "llm_gateway_core.api.v1.web._extract_relevant_article_content",
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
            patch("llm_gateway_core.api.v1.web._generate_queries", AsyncMock(return_value=["topic"])),
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
