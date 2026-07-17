"""Tests for UpstreamSubscriptionQuotaService."""
from __future__ import annotations

import asyncio
import logging
import os
import re
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


class _RecordingClient:
    def __init__(
        self,
        response: httpx.Response,
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._response = response
        self._started = started
        self._release = release
        self.calls: list[dict] = []

    async def get(self, url, *, headers):
        self.calls.append({"url": url, "headers": dict(headers)})
        if self._started is not None and len(self.calls) == 1:
            self._started.set()
            assert self._release is not None
            await self._release.wait()
        return self._response


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

    def test_changed_token_does_not_reuse_cached_snapshot(self):
        client = _RecordingClient(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)

        run_async(service.fetch_github_copilot(copilot_token="token-a"))
        run_async(service.fetch_github_copilot(copilot_token="token-b"))

        self.assertEqual(len(client.calls), 2)


def test_transport_exception_does_not_expose_credentials(caplog):
    secret = "quota-secret-in-authorization"

    class ExplodingClient:
        async def get(self, url, *, headers):  # noqa: ARG002
            raise RuntimeError(f"Authorization: Bearer {secret}")

    service = UpstreamSubscriptionQuotaService(http_client=ExplodingClient())
    target = service._target(
        provider="copilot",
        kind="github_copilot",
        token_env="COPILOT_TOKEN",
        token=secret,
    )

    with caplog.at_level(
        logging.ERROR,
        logger="llm_gateway_core.services.upstream_subscription_quota",
    ):
        snapshot = run_async(
            service.fetch_github_copilot(
                copilot_token=secret,
                provider="copilot",
            )
        )

    assert snapshot.error == "Request failed."
    assert "RuntimeError" in caplog.text
    assert secret not in caplog.text
    assert secret not in repr(target)
    assert secret not in repr(service._cache)
    assert service._cache
    for cache_key in service._cache:
        assert len(cache_key) == 2
        assert all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in cache_key)


class _FakeProviderDetails:
    def __init__(self, quota_cfg=None, *, metadata=None):
        self.subscription_quota = quota_cfg
        self.metadata = metadata


class _FakeQuotaCfg:
    def __init__(self, kind: str, token_env: str):
        self.kind = kind
        self.token_env = token_env


class TestFetchAllCacheIdentity(unittest.TestCase):
    def test_cache_identity_tracks_only_quota_scope_and_credential(self):
        client = _RecordingClient(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client)

        with patch.dict(
            "os.environ",
            {
                "TOKEN_A": "same-token",
                "TOKEN_B": "same-token",
            },
        ):
            first = {
                "provider": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "TOKEN_A"),
                    metadata={"generation": 1},
                )
            }
            same_scope = {
                "provider": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "TOKEN_A"),
                    metadata={"generation": 2},
                )
            }
            changed_env = {
                "provider": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "TOKEN_B")
                )
            }
            changed_provider = {
                "provider-renamed": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "TOKEN_B")
                )
            }
            changed_kind = {
                "provider-renamed": _FakeProviderDetails(
                    _FakeQuotaCfg("gemini_cli", "TOKEN_B")
                )
            }

            run_async(service.fetch_all(providers_config=first))
            run_async(service.fetch_all(providers_config=same_scope))
            self.assertEqual(len(client.calls), 1)

            run_async(service.fetch_all(providers_config=changed_env))
            run_async(service.fetch_all(providers_config=changed_provider))
            run_async(service.fetch_all(providers_config=changed_kind))

        self.assertEqual(len(client.calls), 4)
        self.assertEqual(len(service._cache), 4)
        for cache_key in service._cache:
            self.assertEqual(len(cache_key), 2)
            self.assertTrue(
                all(re.fullmatch(r"[0-9a-f]{64}", digest) for digest in cache_key)
            )

    def test_fetch_all_materializes_every_target_before_network_await(self):
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            client_n = _RecordingClient(
                _mock_response(200, PAID_RESPONSE),
                started=started,
                release=release,
            )
            client_n1 = _RecordingClient(_mock_response(200, PAID_RESPONSE))
            service = UpstreamSubscriptionQuotaService(http_client=client_n)
            providers_n = {
                "first": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "FIRST_N")
                ),
                "second": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "SECOND_N")
                ),
            }
            providers_n1 = {
                "second": _FakeProviderDetails(
                    _FakeQuotaCfg("github_copilot", "SECOND_N1")
                )
            }

            with patch.dict(
                "os.environ",
                {
                    "FIRST_N": "first-token-n",
                    "SECOND_N": "second-token-n",
                    "SECOND_N1": "second-token-n1",
                },
            ):
                task_n = asyncio.create_task(
                    service.fetch_all(providers_config=providers_n)
                )
                await started.wait()

                providers_n["second"].subscription_quota.kind = "mutated-kind"
                providers_n["second"].subscription_quota.token_env = "SECOND_N1"
                os.environ["SECOND_N"] = "mutated-token"
                service._client = client_n1

                snapshots_n1 = await service.fetch_all(providers_config=providers_n1)
                release.set()
                snapshots_n = await task_n

                calls_before_cache_hit = len(client_n1.calls)
                cached_n1 = await service.fetch_all(providers_config=providers_n1)

            self.assertEqual(len(snapshots_n), 2)
            self.assertEqual(len(snapshots_n1), 1)
            self.assertEqual(cached_n1, snapshots_n1)
            self.assertEqual(len(client_n.calls), 2)
            self.assertEqual(
                client_n.calls[1]["headers"]["Authorization"],
                "Bearer second-token-n",
            )
            self.assertEqual(len(client_n1.calls), calls_before_cache_hit)
            self.assertEqual(
                client_n1.calls[0]["headers"]["Authorization"],
                "Bearer second-token-n1",
            )

        run_async(scenario())

    def test_expired_entries_are_pruned_before_cache_insert(self):
        client = _RecordingClient(_mock_response(200, PAID_RESPONSE))
        service = UpstreamSubscriptionQuotaService(http_client=client, ttl_seconds=10.0)

        with patch(
            "llm_gateway_core.services.upstream_subscription_quota.time.monotonic",
            return_value=100.0,
        ):
            run_async(
                service.fetch_github_copilot(
                    copilot_token="token-a",
                    provider="provider-a",
                )
            )
        with patch(
            "llm_gateway_core.services.upstream_subscription_quota.time.monotonic",
            return_value=111.0,
        ):
            run_async(
                service.fetch_github_copilot(
                    copilot_token="token-b",
                    provider="provider-b",
                )
            )

        self.assertEqual(len(service._cache), 1)
        self.assertEqual(len(client.calls), 2)


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
