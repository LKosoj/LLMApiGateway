import unittest
from contextlib import ExitStack, contextmanager
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.middleware.auth import ROLE_USER, SESSION_COOKIE_NAME, create_authenticated_session
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry


class _FakeTokensUsageDB:
    def __init__(self):
        self.calls: list[tuple[int, int, object]] = []
        self.count_calls: list[object] = []
        self.aggregated_calls: list[tuple[str, object, object, object]] = []
        self.records = [
            {"id": 1, "model": "m1", "operation": "chat"},
            {"id": 2, "model": "m2", "operation": "embeddings"},
            {"id": 3, "model": "m3", "operation": "rerank"},
        ]
        self.aggregated = [
            {
                "time_period": "2026-03-15",
                "gateway_model": "gateway-model",
                "operation": "embeddings",
                "provider": "devbox",
                "model": "zai.glm-5",
                "completion_tokens": 30,
                "reasoning_tokens": 7,
                "total_tokens": 42,
            }
        ]

    async def get_latest_usage_records(self, limit: int = 25, offset: int = 0, *, api_key_id=None):
        self.calls.append((limit, offset, api_key_id))
        return self.records[offset:offset + limit]

    async def get_total_records_count(self, *, api_key_id=None):
        self.count_calls.append(api_key_id)
        return len(self.records)

    async def get_aggregated_usage(self, period: str, start_date=None, end_date=None, *, api_key_id=None):
        self.aggregated_calls.append((period, start_date, end_date, api_key_id))
        return self.aggregated

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class _FakeFallbackEventsDB:
    def __init__(self):
        self.upstream_calls: list[tuple[str, object, object, object]] = []
        self.upstream_rows = [
            {
                "time_period": "2026-05-20",
                "gateway_model": "llmgateway/free-stack",
                "operation": "chat",
                "provider": "openrouter",
                "model": "deepseek/deepseek-r1:free",
                "upstream_key_fingerprint": "abc123",
                "attempts": 3,
                "successes": 2,
                "errors": 1,
                "success_rate": 66.6667,
                "avg_duration_ms": 1200,
                "max_duration_ms": 1800,
            }
        ]

    async def get_upstream_stats(self, period: str, start_date=None, end_date=None, *, api_key_id=None):
        self.upstream_calls.append((period, start_date, end_date, api_key_id))
        return self.upstream_rows

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class _FakeApiKeysDB:
    def __init__(self, valid_key_id: int):
        self.valid_key_id = valid_key_id

    def get_by_id(self, key_id: int):
        if key_id == self.valid_key_id:
            return SimpleNamespace(id=key_id, disabled=False)
        return None


class StatsApiPaginationTests(unittest.TestCase):
    def setUp(self):
        stats_module._usage_stats_cache.invalidate()
        stats_module._fallback_stats_cache.invalidate()
        stats_module._upstream_stats_cache.invalidate()

    @contextmanager
    def _client(
        self,
        fake_tokens_usage_db: _FakeTokensUsageDB,
        fake_api_keys_db: _FakeApiKeysDB | None = None,
        fake_fallback_events_db: _FakeFallbackEventsDB | None = None,
    ):
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        fake_config_loader = Mock()
        fake_config_loader.load_providers.return_value = {}
        fake_config_loader.load_fallback_rules.return_value = {}
        fallback_events_db = fake_fallback_events_db or _FakeFallbackEventsDB()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=fake_config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB", return_value=fake_tokens_usage_db))
            stack.enter_context(patch("main.FallbackEventsDB", return_value=fallback_events_db))
            if fake_api_keys_db is not None:
                stack.enter_context(patch("main.ApiKeysDB", return_value=fake_api_keys_db))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
                yield client

    def test_usage_records_returns_400_for_negative_limit_and_offset(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            negative_limit_response = client.get(
                "/v1/api/usage-records?limit=-1",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            negative_offset_response = client.get(
                "/v1/api/usage-records?offset=-1",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(negative_limit_response.status_code, 400)
        self.assertEqual(negative_offset_response.status_code, 400)
        self.assertEqual(fake_tokens_usage_db.calls, [])

    def test_usage_records_returns_400_when_limit_exceeds_maximum(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?limit=1000",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(fake_tokens_usage_db.calls, [])

    def test_usage_records_accepts_maximum_valid_limit(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?limit=100&offset=0",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["records"],
            [
                {**record, "status": "completed"}
                for record in fake_tokens_usage_db.records
            ],
        )
        self.assertEqual(response.json()["total_records"], 3)
        self.assertEqual(fake_tokens_usage_db.calls, [(100, 0, None)])

    def test_usage_stats_include_gateway_model_in_aggregated_payload(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-stats/day",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_tokens_usage_db.aggregated)
        self.assertEqual(len(fake_tokens_usage_db.aggregated_calls), 1)
        period, start_date, end_date, api_key_id = fake_tokens_usage_db.aggregated_calls[0]
        self.assertEqual(period, "day")
        self.assertIs(start_date.tzinfo, timezone.utc)
        self.assertIs(end_date.tzinfo, timezone.utc)
        self.assertIsNone(api_key_id)
        self.assertEqual(response.json()[0]["operation"], "embeddings")
        self.assertEqual(response.json()[0]["completion_tokens"], 30)
        self.assertEqual(response.json()[0]["reasoning_tokens"], 7)

    def test_usage_records_call_async_db_methods_directly(self):
        """Verify that the handler calls async DB methods (no thread offloading)."""
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?limit=2&offset=1",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 1, None)])

    def test_usage_records_include_running_requests_before_completed_records(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            active_requests = ActiveRequestsRegistry()
            active_requests.start(
                request_id="req-running",
                path="/v1/chat/completions",
                api_key_id=7,
                gateway_model="llmgateway/qwen",
                operation="chat",
            )
            active_requests.update(
                "req-running",
                provider="openrouter",
                model="qwen/qwen3",
            )
            client.app.state.active_requests_registry = active_requests

            response = client.get(
                "/v1/api/usage-records?limit=2&offset=0&api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["total_records"], 4)
        self.assertEqual(payload["records"][0]["id"], "active:req-running")
        self.assertEqual(payload["records"][0]["status"], "running")
        self.assertEqual(payload["records"][0]["gateway_model"], "llmgateway/qwen")
        self.assertEqual(payload["records"][0]["provider"], "openrouter")
        self.assertEqual(payload["records"][0]["model"], "qwen/qwen3")
        self.assertEqual(payload["records"][1]["status"], "completed")
        self.assertEqual(fake_tokens_usage_db.calls, [(1, 0, 7)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [7])

    def test_usage_records_pagination_offsets_past_running_requests(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            active_requests = ActiveRequestsRegistry()
            active_requests.start(
                request_id="req-running",
                path="/v1/embeddings",
                api_key_id=None,
                gateway_model="gateway-embeddings",
                operation="embeddings",
            )
            client.app.state.active_requests_registry = active_requests

            response = client.get(
                "/v1/api/usage-records?limit=2&offset=1",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([record["status"] for record in response.json()["records"]], ["completed", "completed"])
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 0, None)])

    def test_master_usage_records_can_filter_by_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?limit=2&offset=0&api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 0, 7)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [7])

    def test_master_usage_stats_can_filter_by_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-stats/month?api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_tokens_usage_db.aggregated_calls), 1)
        self.assertEqual(fake_tokens_usage_db.aggregated_calls[0][3], 7)

    def test_master_usage_filter_rejects_non_positive_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?api_key_id=0",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "api_key_id must be a positive integer.")
        self.assertEqual(fake_tokens_usage_db.calls, [])
        self.assertEqual(fake_tokens_usage_db.count_calls, [])

    def test_virtual_key_usage_filter_ignores_requested_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db, _FakeApiKeysDB(valid_key_id=11)) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=11),
            )
            records_response = client.get("/v1/api/usage-records?api_key_id=999&limit=2")
            stats_response = client.get("/v1/api/usage-stats/day?api_key_id=999")

        self.assertEqual(records_response.status_code, 200)
        self.assertEqual(stats_response.status_code, 200)
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 0, 11)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [11])
        self.assertEqual(fake_tokens_usage_db.aggregated_calls[0][3], 11)

    def test_usage_stats_call_async_aggregation_directly(self):
        """Verify that the handler calls async DB aggregation method."""
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-stats/day",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_tokens_usage_db.aggregated_calls), 1)
        self.assertEqual(fake_tokens_usage_db.aggregated_calls[0][0], "day")

    def test_master_upstream_stats_can_filter_by_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()
        fake_fallback_events_db = _FakeFallbackEventsDB()

        with self._client(
            fake_tokens_usage_db,
            fake_fallback_events_db=fake_fallback_events_db,
        ) as client:
            response = client.get(
                "/v1/api/upstream-stats/day?api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fake_fallback_events_db.upstream_rows)
        self.assertEqual(len(fake_fallback_events_db.upstream_calls), 1)
        period, start_date, end_date, api_key_id = fake_fallback_events_db.upstream_calls[0]
        self.assertEqual(period, "day")
        self.assertIs(start_date.tzinfo, timezone.utc)
        self.assertIs(end_date.tzinfo, timezone.utc)
        self.assertEqual(api_key_id, 7)

    def test_upstream_stats_are_master_only(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()
        fake_fallback_events_db = _FakeFallbackEventsDB()

        with self._client(
            fake_tokens_usage_db,
            _FakeApiKeysDB(valid_key_id=11),
            fake_fallback_events_db=fake_fallback_events_db,
        ) as client:
            client.cookies.set(
                SESSION_COOKIE_NAME,
                create_authenticated_session(role=ROLE_USER, key_id=11),
            )
            response = client.get("/v1/api/upstream-stats/day")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(fake_fallback_events_db.upstream_calls, [])

    def test_upstream_status_returns_runtime_rows(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            client.app.state.upstream_routing_state.mark_health(
                "openrouter",
                "deepseek/deepseek-r1:free",
                "abc123",
                "healthy",
                None,
            )
            response = client.get(
                "/v1/api/upstream-status",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        rows = response.json()["rows"]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["provider"], "openrouter")
        self.assertEqual(rows[0]["upstream_key_fingerprint"], "abc123")
        self.assertEqual(rows[0]["health_status"], "healthy")


if __name__ == "__main__":
    unittest.main()
