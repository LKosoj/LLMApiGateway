"""Tests for UpstreamSubscriptionQuotaService."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

import httpx

from llm_gateway_core.services.upstream_subscription_quota import (
    UpstreamSubscriptionQuotaService,
)
from tests._async_compat import run_async


def _mock_response(status_code: int, json_data: dict | None = None) -> httpx.Response:
    if json_data is not None:
        import json
        content = json.dumps(json_data).encode()
        headers = {"content-type": "application/json"}
    else:
        content = b""
        headers = {}
    return httpx.Response(status_code=status_code, content=content, headers=headers)


class _FakeTransport(httpx.AsyncBaseTransport):
    def __init__(self, response: httpx.Response) -> None:
        self._response = response

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        return self._response


def _make_client(response: httpx.Response) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=_FakeTransport(response))


PAID_RESPONSE = {
    "copilot_plan": "business",
    "quota_reset_date": "2026-06-01",
    "quota_snapshots": {
        "chat": {"entitlement": 300, "remaining": 250, "unlimited": False},
        "completions": {"entitlement": 2000, "remaining": 1500, "unlimited": False},
        "premium_interactions": {"entitlement": 50, "remaining": 10, "unlimited": False},
    },
}

FREE_RESPONSE = {
    "monthly_quotas": {"chat": 50, "completions": 2000},
    "limited_user_quotas": {"chat": 30, "completions": 900},
    "limited_user_reset_date": "2026-06-01",
}


class TestGithubCopilotPaidFormat(unittest.TestCase):
    def test_github_copilot_paid_format(self):
        client = _make_client(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_github_copilot(copilot_token="tok"))

        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.kind, "github_copilot")
        self.assertEqual(snapshot.plan, "business")
        self.assertEqual(snapshot.reset_date, "2026-06-01")

        chat = snapshot.categories["chat"]
        self.assertEqual(chat.used, 50)
        self.assertEqual(chat.total, 300)
        self.assertEqual(chat.remaining, 250)
        self.assertFalse(chat.unlimited)

        comp = snapshot.categories["completions"]
        self.assertEqual(comp.used, 500)
        self.assertEqual(comp.total, 2000)

        prem = snapshot.categories["premium_interactions"]
        self.assertEqual(prem.used, 40)
        self.assertEqual(prem.total, 50)


class TestGithubCopilotFreeFormat(unittest.TestCase):
    def test_github_copilot_free_format(self):
        client = _make_client(_mock_response(200, FREE_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_github_copilot(copilot_token="tok"))

        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.plan, "free")
        self.assertEqual(snapshot.reset_date, "2026-06-01")

        chat = snapshot.categories["chat"]
        self.assertEqual(chat.used, 30)
        self.assertEqual(chat.total, 50)
        self.assertEqual(chat.remaining, 20)

        comp = snapshot.categories["completions"]
        self.assertEqual(comp.used, 900)
        self.assertEqual(comp.total, 2000)
        self.assertEqual(comp.remaining, 1100)


class TestGithubCopilotUnknownFormat(unittest.TestCase):
    def test_github_copilot_unknown_format(self):
        client = _make_client(_mock_response(200, {}))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_github_copilot(copilot_token="tok"))

        self.assertIsNotNone(snapshot.error)
        self.assertIn("Unknown response format", snapshot.error)
        self.assertEqual(snapshot.categories, {})


class TestGithubCopilotHttp500(unittest.TestCase):
    def test_github_copilot_http_500(self):
        client = _make_client(_mock_response(500))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_github_copilot(copilot_token="tok"))

        self.assertIsNotNone(snapshot.error)
        self.assertIn("HTTP 500", snapshot.error)
        self.assertEqual(snapshot.categories, {})


class TestGeminiCliSuccess(unittest.TestCase):
    def test_gemini_cli_success(self):
        client = _make_client(_mock_response(200, {"projects": []}))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_gemini_cli(access_token="tok"))

        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.kind, "gemini_cli")
        self.assertEqual(snapshot.plan, "google_cloud")
        self.assertEqual(snapshot.categories, {})


class TestGeminiCliHttp403(unittest.TestCase):
    def test_gemini_cli_http_403(self):
        client = _make_client(_mock_response(403))
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_gemini_cli(access_token="tok"))

        self.assertIsNotNone(snapshot.error)
        self.assertIn("HTTP 403", snapshot.error)


class TestAntigravityStub(unittest.TestCase):
    def test_antigravity_stub(self):
        client = httpx.AsyncClient()
        service = UpstreamSubscriptionQuotaService(http_client=client)
        snapshot = run_async(service.fetch_antigravity(access_token="tok"))

        self.assertIsNone(snapshot.error)
        self.assertEqual(snapshot.kind, "antigravity")
        self.assertEqual(snapshot.categories, {})


class TestTtlCacheHits(unittest.TestCase):
    def test_ttl_cache_hits(self):
        call_count = 0
        original_response = _mock_response(200, PAID_RESPONSE)

        class CountingTransport(httpx.AsyncBaseTransport):
            async def handle_async_request(self, request):
                nonlocal call_count
                call_count += 1
                return original_response

        client = httpx.AsyncClient(transport=CountingTransport())
        service = UpstreamSubscriptionQuotaService(http_client=client, ttl_seconds=60.0)

        # First call — should hit network
        run_async(service.fetch_github_copilot(copilot_token="tok"))
        # Second call — should hit cache
        run_async(service.fetch_github_copilot(copilot_token="tok"))

        self.assertEqual(call_count, 1)


class _FakeProviderDetails:
    def __init__(self, quota_cfg=None):
        self.subscription_quota = quota_cfg


class _FakeQuotaCfg:
    def __init__(self, kind: str, token_env: str):
        self.kind = kind
        self.token_env = token_env


class TestFetchAllNoQuotaBlock(unittest.TestCase):
    def test_fetch_all_with_no_quota_block(self):
        client = _make_client(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)

        providers = {
            "my_provider": _FakeProviderDetails(quota_cfg=None),
        }
        snapshots = run_async(service.fetch_all(providers_config=providers))
        self.assertEqual(len(snapshots), 0)


class TestFetchAllAggregatesResults(unittest.TestCase):
    def test_fetch_all_aggregates_results(self):
        client = _make_client(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)

        providers = {
            "copilot_prov": _FakeProviderDetails(
                quota_cfg=_FakeQuotaCfg("github_copilot", "COPILOT_TOKEN")
            ),
            "gemini_prov": _FakeProviderDetails(
                quota_cfg=_FakeQuotaCfg("gemini_cli", "GEMINI_TOKEN")
            ),
            "no_quota_prov": _FakeProviderDetails(quota_cfg=None),
        }

        with patch.dict(
            "os.environ",
            {"COPILOT_TOKEN": "copilot-tok", "GEMINI_TOKEN": "gemini-tok"},
        ):
            snapshots = run_async(service.fetch_all(providers_config=providers))

        self.assertEqual(len(snapshots), 2)
        kinds = {s.kind for s in snapshots}
        self.assertIn("github_copilot", kinds)
        self.assertIn("gemini_cli", kinds)

        providers_found = {s.provider for s in snapshots}
        self.assertIn("copilot_prov", providers_found)
        self.assertIn("gemini_prov", providers_found)


class TestFetchAllMissingEnvVar(unittest.TestCase):
    def test_fetch_all_missing_env_var_returns_error_snapshot(self):
        client = httpx.AsyncClient()
        service = UpstreamSubscriptionQuotaService(http_client=client)

        providers = {
            "copilot_prov": _FakeProviderDetails(
                quota_cfg=_FakeQuotaCfg("github_copilot", "NONEXISTENT_TOKEN_ENV_XYZ")
            ),
        }

        os.environ.pop("NONEXISTENT_TOKEN_ENV_XYZ", None)
        snapshots = run_async(service.fetch_all(providers_config=providers))

        self.assertEqual(len(snapshots), 1)
        self.assertIsNotNone(snapshots[0].error)
        self.assertIn("NONEXISTENT_TOKEN_ENV_XYZ", snapshots[0].error)


if __name__ == "__main__":
    unittest.main()
