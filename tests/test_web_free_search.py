from __future__ import annotations

import asyncio
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import web_adapters, web_free_engines
from llm_gateway_core.api.v1.web_engine_errors import BotWallError
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.config.settings import settings
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async
from tests.web_accounting_test_support import install_web_accounting_passthrough

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
  ]
}
""".strip()


class _FakeDownstreamResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)
        self.headers = {}

    def json(self):
        return self._payload

    def raise_for_status(self):
        return None


class _FakeApiKeysDB:
    @property
    def db_path(self) -> Path:
        return main.resolve_db_dir() / "tokens_usage.db"

    def get_by_key(self, api_key: str):
        return None

    def record_spent(self, key_id: int, amount: float) -> None:
        return None


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


def _item(url: str, title: str = "topic guide", snippet: str = "about topic") -> dict:
    return {"url": url, "title": title, "snippet": snippet}


def _free_engine(name: str, urls: list[str]):
    """Stub engine returning query-relevant items; records every query it saw."""
    calls: list[str] = []

    async def engine(client, query, max_results, *, language="en", **kwargs):
        calls.append(query)
        return [_item(url, title=f"{query} guide", snippet=f"{query} {name}") for url in urls]

    engine.calls = calls
    return engine


class WebFreeSearchTests(unittest.TestCase):
    def setUp(self):
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openai")
        self.fallback_provider_patcher.start()
        self.addCleanup(self.fallback_provider_patcher.stop)
        self.temp_dir = tempfile.TemporaryDirectory()
        providers_path = Path(self.temp_dir.name) / "providers.json"
        rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.config_loader = ConfigLoader(
            providers_filename=str(providers_path),
            fallback_rules_filename=str(rules_path),
            operation_rules_filename=str(operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        coordinator_patcher = patch("main.ConfigUpdateCoordinator", return_value=config_update_coordinator)
        coordinator_patcher.start()
        self.addCleanup(coordinator_patcher.stop)
        web_adapters.invalidate_web_search_cache()
        self.addCleanup(web_adapters.invalidate_web_search_cache)

    def tearDown(self):
        self.temp_dir.cleanup()

    def _fake_post(self, url, *, headers=None, json=None, **kwargs):
        # Query expansion returns the original query text, so the generated query
        # deduplicates against it and each request runs exactly one search query.
        return _FakeDownstreamResponse(
            {
                "choices": [{"message": {"content": "topic"}}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5, "cost": 0.01},
            }
        )

    @contextmanager
    def _client(
        self,
        free_engines: dict,
        paid_adapters: dict,
        *,
        free_enabled: bool = True,
        cache_ttl: float = 0.0,
        post_side_effect=None,
    ):
        fake_http_client = Mock()
        fake_http_client.post = AsyncMock(side_effect=post_side_effect or self._fake_post)
        fake_http_client.get = AsyncMock(return_value=_FakeDownstreamResponse({"data": []}))
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            install_web_accounting_passthrough(stack)
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
            stack.enter_context(
                patch.object(main.settings, "tavily_api_key", "dummy-tavily" if "tavily" in paid_adapters else None)
            )
            stack.enter_context(
                patch.object(main.settings, "jina_api_key", "dummy-jina" if "jina" in paid_adapters else None)
            )
            stack.enter_context(
                patch.object(main.settings, "zai_api_key", "dummy-zai" if "zai" in paid_adapters else None)
            )
            stack.enter_context(patch.object(main.settings, "web_free_search_enabled", free_enabled))
            stack.enter_context(patch.object(main.settings, "web_free_search_engines", ",".join(free_engines)))
            stack.enter_context(patch.object(main.settings, "web_search_cache_ttl_seconds", cache_ttl))
            stack.enter_context(patch.dict(web_adapters.FREE_ENGINES, free_engines, clear=True))
            stack.enter_context(patch.dict(web_adapters._SEARCH_ADAPTERS, paid_adapters, clear=False))
            with TestClient(main.app) as client:
                yield client

    def _search(self, client, query: str = "topic", **extra):
        return client.post(
            "/v1/web/search",
            json={"model": "llmgateway/web-search", "query": query, "max_results": 5, **extra},
            headers={"Authorization": "Bearer test-gateway-key"},
        )

    def test_two_free_engines_merge_and_skip_paid(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])
        anysearch = _free_engine("anysearch", ["https://beta.example/2"])
        tavily = AsyncMock(return_value=[_item("https://paid.example/3")])
        real_merge = web_adapters.merge_free_results
        with (
            self._client({"bing": bing, "anysearch": anysearch}, {"tavily": tavily}) as client,
            patch.object(web_adapters, "merge_free_results", wraps=real_merge) as merge_spy,
        ):
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        urls = [item["url"] for item in response.json()["data"]]
        self.assertCountEqual(urls, ["https://alpha.example/1", "https://beta.example/2"])
        merge_spy.assert_called_once()
        self.assertEqual(set(merge_spy.call_args.args[0]), {"bing", "anysearch"})
        tavily.assert_not_awaited()

    def test_single_free_engine_adds_one_paid_without_ranking(self):
        bing = _free_engine("bing", ["https://free.example/1"])
        anysearch = _free_engine("anysearch", [])
        tavily = AsyncMock(
            return_value=[
                _item("https://paid.example/zzz", title="unrelated", snippet="nothing"),
                _item("https://paid.example/aaa", title="topic topic topic", snippet="topic"),
                _item("https://free.example/1?utm_source=newsletter"),
            ]
        )
        jina = AsyncMock(return_value=[_item("https://jina.example/4")])
        with self._client({"bing": bing, "anysearch": anysearch}, {"tavily": tavily, "jina": jina}) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        urls = [item["url"] for item in response.json()["data"]]
        # Free first; paid keep their original order (no ranking); tracking-dup dropped.
        self.assertEqual(
            urls,
            ["https://free.example/1", "https://paid.example/zzz", "https://paid.example/aaa"],
        )
        tavily.assert_awaited()
        jina.assert_not_awaited()

    def test_zero_free_engines_keeps_paid_chain_semantics(self):
        bing = _free_engine("bing", [])
        anysearch = _free_engine("anysearch", [])
        tavily = AsyncMock(return_value=[])
        jina = AsyncMock(return_value=[_item("https://jina.example/4", title="Jina", snippet="as is")])
        with self._client({"bing": bing, "anysearch": anysearch}, {"tavily": tavily, "jina": jina}) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        data = response.json()["data"]
        self.assertEqual([item["url"] for item in data], ["https://jina.example/4"])
        self.assertEqual(data[0]["title"], "Jina")
        tavily.assert_awaited()
        jina.assert_awaited()

    def test_free_disabled_keeps_legacy_behavior(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])
        zai = AsyncMock(return_value=[_item("https://paid.example/3")])
        with self._client({"bing": bing}, {"zai": zai}, free_enabled=False) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["url"] for item in response.json()["data"]], ["https://paid.example/3"])
        self.assertEqual(bing.calls, [])
        zai.assert_awaited()

    def test_free_enabled_without_paid_keys_does_not_raise_503(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])
        with self._client({"bing": bing}, {}) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["url"] for item in response.json()["data"]], ["https://alpha.example/1"])

    def test_cache_hit_miss_and_empty_not_cached(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])
        anysearch = _free_engine("anysearch", ["https://beta.example/2"])
        with self._client({"bing": bing, "anysearch": anysearch}, {}, cache_ttl=120.0) as client:
            first = self._search(client)
            second = self._search(client)
            self.assertEqual(first.json()["data"], second.json()["data"])
            self.assertEqual(bing.calls, ["topic"])
            self.assertEqual(anysearch.calls, ["topic"])

            miss = self._search(client, query="alpha")
            self.assertEqual(miss.status_code, 200)
            # A different query is a miss: the whole body reruns (cache is per request,
            # not per expanded query), so the pool sees "alpha" and "topic" again.
            self.assertIn("alpha", bing.calls)
            self.assertEqual(bing.calls.count("topic"), 2)

    def test_empty_result_is_not_cached(self):
        bing = _free_engine("bing", [])
        with self._client({"bing": bing}, {}, cache_ttl=120.0) as client:
            first = self._search(client)
            second = self._search(client)

        self.assertEqual(first.json()["data"], [])
        self.assertEqual(second.json()["data"], [])
        self.assertEqual(bing.calls, ["topic", "topic"])

    def test_cache_ttl_zero_disables_cache(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])
        with self._client({"bing": bing}, {}, cache_ttl=0.0) as client:
            self._search(client)
            self._search(client)

        self.assertEqual(bing.calls, ["topic", "topic"])

    def test_unknown_engine_name_is_ignored_with_warning(self):
        with (
            patch.object(settings, "web_free_search_engines", "bing,nonexistent,bing"),
            patch.dict(web_adapters.FREE_ENGINES, {"bing": _free_engine("bing", [])}, clear=True),
            self.assertLogs("llm_gateway_core.api.v1.web_adapters", level="WARNING") as logs,
        ):
            names = web_adapters._free_search_engines()

        self.assertEqual(names, ["bing"])
        self.assertTrue(any("nonexistent" in line for line in logs.output))

    def test_cooldown_skips_failed_engine_on_next_pool_run(self):
        state = UpstreamRoutingState()
        calls: list[str] = []

        async def failing(client, query, max_results, *, language="en"):
            calls.append(query)
            raise BotWallError("bing: anti-bot challenge")

        anysearch = _free_engine("anysearch", ["https://beta.example/2"])
        with patch.dict(web_adapters.FREE_ENGINES, {"bing": failing, "anysearch": anysearch}, clear=True):
            first = run_async(
                web_adapters._run_free_pool(
                    state,
                    Mock(),
                    ["bing", "anysearch"],
                    "topic",
                    language="en",
                    max_results=5,
                    include_domains=[],
                    exclude_domains=[],
                )
            )
            second = run_async(
                web_adapters._run_free_pool(
                    state,
                    Mock(),
                    ["bing", "anysearch"],
                    "topic",
                    language="en",
                    max_results=5,
                    include_domains=[],
                    exclude_domains=[],
                )
            )

        self.assertEqual(set(first), {"anysearch"})
        self.assertEqual(set(second), {"anysearch"})
        self.assertEqual(len(calls), 1)  # bot-wall cooldown skips the engine, no second call
        self.assertEqual(anysearch.calls, ["topic", "topic"])

    def test_budget_timeout_cancels_slow_engine_and_keeps_fast_results(self):
        cancelled = asyncio.Event()

        async def slow(client, query, max_results, *, language="en"):
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.set()
                raise
            return [_item("https://slow.example/1")]

        anysearch = _free_engine("anysearch", ["https://beta.example/2"])
        with (
            patch.dict(web_adapters.FREE_ENGINES, {"bing": slow, "anysearch": anysearch}, clear=True),
            patch.object(settings, "web_free_search_budget_seconds", 0.05),
        ):
            results = run_async(
                web_adapters._run_free_pool(
                    UpstreamRoutingState(),
                    Mock(),
                    ["bing", "anysearch"],
                    "topic",
                    language="en",
                    max_results=5,
                    include_domains=[],
                    exclude_domains=[],
                )
            )

        self.assertEqual(set(results), {"anysearch"})
        self.assertTrue(cancelled.is_set())

    def test_irrelevant_engine_results_count_as_zero(self):
        bing = _free_engine("bing", ["https://alpha.example/1"])

        async def irrelevant(client, query, max_results, *, language="en"):
            return [_item("https://spam.example/x", title="casino pills", snippet="totally unrelated")]

        zai = AsyncMock(return_value=[_item("https://paid.example/3")])
        with self._client({"bing": bing, "anysearch": irrelevant}, {"zai": zai}) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        urls = [item["url"] for item in response.json()["data"]]
        self.assertEqual(urls, ["https://alpha.example/1", "https://paid.example/3"])
        zai.assert_awaited()

    def test_searxng_receives_instances_and_bearer_token_from_settings(self):
        seen: dict = {}

        async def searxng(client, query, max_results, *, language="en", instances=None, bearer_token=None):
            seen["instances"] = instances
            seen["bearer_token"] = bearer_token
            return [_item("https://beta.example/2", title=f"{query} guide", snippet=query)]

        with (
            patch.dict(web_adapters.FREE_ENGINES, {"searxng": searxng}, clear=True),
            patch.object(settings, "searxng_instances", "https://one.example, https://two.example"),
            patch.object(settings, "searxng_bearer_token", "secret-token"),
        ):
            results = run_async(
                web_adapters._run_free_pool(
                    UpstreamRoutingState(),
                    Mock(),
                    ["searxng"],
                    "topic",
                    language="en",
                    max_results=5,
                    include_domains=[],
                    exclude_domains=[],
                )
            )

        self.assertEqual(set(results), {"searxng"})
        self.assertEqual(seen["instances"], ["https://one.example", "https://two.example"])
        self.assertEqual(seen["bearer_token"], "secret-token")

    def test_tavily_keyless_free_engine_works_without_api_key(self):
        calls: list[dict] = []

        def post_side_effect(url, *, headers=None, json=None, **kwargs):
            if "api.tavily.com" in url:
                calls.append({"headers": headers or {}, "json": json or {}})
                return _FakeDownstreamResponse(
                    {"results": [{"url": "https://free.example/1", "title": "topic guide", "content": "about topic"}]}
                )
            return self._fake_post(url, headers=headers, json=json, **kwargs)

        with (
            patch.object(main.settings, "web_search_tavily_keyless", True),
            self._client(
                {"tavily_keyless": web_free_engines.search_tavily_keyless},
                {},
                post_side_effect=post_side_effect,
            ) as client,
        ):
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["url"] for item in response.json()["data"]], ["https://free.example/1"])
        self.assertEqual(calls[0]["headers"]["x-tavily-access-mode"], "keyless")
        self.assertNotIn("api_key", calls[0]["json"])
        # The paid tavily adapter stays keyed-only even with the keyless flag on.
        self.assertFalse(web_adapters._search_adapter_enabled("tavily"))

    def test_tavily_keyless_and_keyed_modes_coexist_with_api_key(self):
        calls: list[dict] = []

        def post_side_effect(url, *, headers=None, json=None, **kwargs):
            if "api.tavily.com" in url:
                calls.append({"headers": headers or {}, "json": json or {}})
                if "api_key" in (json or {}):
                    return _FakeDownstreamResponse(
                        {"results": [{"url": "https://paid.example/2", "title": "topic paid", "content": "paid"}]}
                    )
                return _FakeDownstreamResponse(
                    {"results": [{"url": "https://free.example/1", "title": "topic guide", "content": "about topic"}]}
                )
            return self._fake_post(url, headers=headers, json=json, **kwargs)

        with (
            patch.object(main.settings, "web_search_tavily_keyless", True),
            self._client(
                {"tavily_keyless": web_free_engines.search_tavily_keyless},
                {"tavily": web_adapters._search_tavily},
                post_side_effect=post_side_effect,
            ) as client,
        ):
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        # One free success -> exactly one paid adapter added; free first, paid as-is.
        self.assertEqual(
            [item["url"] for item in response.json()["data"]],
            ["https://free.example/1", "https://paid.example/2"],
        )
        keyless_calls = [call for call in calls if call["headers"].get("x-tavily-access-mode") == "keyless"]
        keyed_calls = [call for call in calls if "api_key" in call["json"]]
        self.assertEqual(len(keyless_calls), 1)
        self.assertNotIn("api_key", keyless_calls[0]["json"])
        self.assertEqual(len(keyed_calls), 1)
        self.assertEqual(keyed_calls[0]["json"]["api_key"], "dummy-tavily")
        self.assertNotIn("x-tavily-access-mode", keyed_calls[0]["headers"])

    def test_tavily_keyless_flag_off_excludes_engine_but_paid_keyed_works(self):
        keyless_engine = _free_engine("tavily_keyless", ["https://free.example/1"])
        tavily = AsyncMock(return_value=[_item("https://paid.example/2")])
        with (
            patch.object(main.settings, "web_search_tavily_keyless", False),
            patch.object(main.settings, "web_free_search_engines", "tavily_keyless"),
            patch.dict(web_adapters.FREE_ENGINES, {"tavily_keyless": keyless_engine}, clear=True),
        ):
            self.assertEqual(web_adapters._free_search_engines(), [])
        with (
            patch.object(main.settings, "web_search_tavily_keyless", False),
            self._client({"tavily_keyless": keyless_engine}, {"tavily": tavily}) as client,
        ):
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["url"] for item in response.json()["data"]], ["https://paid.example/2"])
        self.assertEqual(keyless_engine.calls, [])
        tavily.assert_awaited()

    def test_domain_filter_cutting_all_free_results_falls_back_to_paid(self):
        bing = _free_engine("bing", ["https://wrong.example/1"])
        anysearch = _free_engine("anysearch", ["https://wrong.example/2"])
        zai = AsyncMock(return_value=[_item("https://wanted.example/3")])
        with self._client({"bing": bing, "anysearch": anysearch}, {"zai": zai}) as client:
            response = self._search(client, include_domains=["wanted.example"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual([item["url"] for item in response.json()["data"]], ["https://wanted.example/3"])
        zai.assert_awaited()

    def test_domain_filter_leaving_one_free_engine_adds_one_paid(self):
        bing = _free_engine("bing", ["https://wanted.example/1"])
        anysearch = _free_engine("anysearch", ["https://wrong.example/2"])
        zai = AsyncMock(return_value=[_item("https://wanted.example/3")])
        with self._client({"bing": bing, "anysearch": anysearch}, {"zai": zai}) as client:
            response = self._search(client, include_domains=["wanted.example"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["url"] for item in response.json()["data"]],
            ["https://wanted.example/1", "https://wanted.example/3"],
        )
        zai.assert_awaited()

    def test_broken_urls_in_free_results_do_not_fail_request(self):
        broken = ["https://example.com:bad/path", "http://[oops/x", "https://x.example:99999/"]
        bing = _free_engine("bing", [broken[0], "https://free.example/1"])
        anysearch = _free_engine("anysearch", [])
        tavily = AsyncMock(
            return_value=[
                _item("https://paid.example/3", title="topic paid"),
                _item(broken[1]),
                _item(broken[2]),
            ]
        )
        with self._client({"bing": bing, "anysearch": anysearch}, {"tavily": tavily}) as client:
            response = self._search(client)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["url"] for item in response.json()["data"]],
            [broken[0], "https://free.example/1", "https://paid.example/3", broken[1], broken[2]],
        )
        # Helper-level: malformed ports/IPv6 fall back to the raw URL key instead of raising.
        for url in broken:
            self.assertEqual(web_adapters._search_result_url_key(url), url.rstrip("/"))

    def test_broken_url_with_domain_filter_does_not_fail(self):
        bing = _free_engine("bing", ["http://[oops/x", "https://wanted.example/1"])
        anysearch = _free_engine("anysearch", ["https://x.example:99999/"])
        zai = AsyncMock(return_value=[_item("https://wanted.example/3")])
        with self._client({"bing": bing, "anysearch": anysearch}, {"zai": zai}) as client:
            response = self._search(client, include_domains=["wanted.example"])

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [item["url"] for item in response.json()["data"]],
            ["https://wanted.example/1", "https://wanted.example/3"],
        )
        self.assertFalse(web_adapters._domain_matches("http://[oops/x", ["oops", "x"]))

    def test_tavily_keyless_flag_off_keeps_other_engines_in_pool(self):
        with (
            patch.object(settings, "web_search_tavily_keyless", False),
            patch.object(settings, "web_free_search_engines", "bing,tavily_keyless"),
            patch.dict(
                web_adapters.FREE_ENGINES,
                {"bing": _free_engine("bing", []), "tavily_keyless": _free_engine("tavily_keyless", [])},
                clear=True,
            ),
        ):
            self.assertEqual(web_adapters._free_search_engines(), ["bing"])

    def test_search_result_url_key_strips_tracking_params(self):
        self.assertEqual(
            web_adapters._search_result_url_key("https://example.com/page?utm_source=x&id=7&fbclid=abc"),
            web_adapters._search_result_url_key("https://example.com/page?id=7"),
        )
        self.assertNotEqual(
            web_adapters._search_result_url_key("https://example.com/page?id=7"),
            web_adapters._search_result_url_key("https://example.com/page?id=8"),
        )


if __name__ == "__main__":
    unittest.main()
