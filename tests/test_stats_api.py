import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager, contextmanager
from datetime import timezone
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.middleware.auth import (
    ROLE_USER,
    SESSION_COOKIE_NAME,
    ApiKeyAuthMiddleware,
    create_authenticated_session,
)
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from tests.runtime_test_support import installed_runtime


class _FakeTokensUsageDB:
    def __init__(self):
        self.calls: list[tuple[int, int, object]] = []
        self.count_calls: list[object] = []
        self.aggregated_calls: list[tuple[str, object, object, object]] = []
        self.dashboard_calls: list[dict] = []
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

    async def get_dashboard_usage(self, period: str, start_date, end_date, **kwargs):
        self.dashboard_calls.append(
            {"period": period, "start_date": start_date, "end_date": end_date, **kwargs}
        )
        return {
            "summary": {
                "requests": 2,
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "reasoning_tokens": 3,
                "cached_tokens": 4,
                "cost": 0.06,
                "cost_saved": 0.01,
                "estimated_count": 1,
                "avg_duration_ms": 150,
                "max_duration_ms": 250,
                "duration_p50_ms": 140,
                "duration_p95_ms": 240,
                "ttft_avg_ms": 80,
                "ttft_max_ms": 120,
                "ttft_p50_ms": 75,
                "ttft_p95_ms": 115,
            },
            "series": [{"time_period": "2026-05-31", "requests": 2, "total_tokens": 30, "cost": 0.06}],
            "breakdowns": {
                "operations": [{"label": "chat", "requests": 2, "total_tokens": 30, "cost": 0.06}],
                "gateway_models": [{"label": "gateway-model", "requests": 2, "total_tokens": 30, "cost": 0.06}],
                "providers": [{"label": "openrouter", "requests": 2, "total_tokens": 30, "cost": 0.06}],
                "resolved_targets": [{"label": "openrouter / qwen", "provider": "openrouter", "model": "qwen", "requests": 2}],
                "x_titles": [{"label": "tgBot", "requests": 2, "total_tokens": 30, "cost": 0.06}],
                "api_keys": [{"label": str(kwargs.get("api_key_id") or "unattributed"), "requests": 2}],
                "upstream_keys": [{"label": "fp-1", "requests": 2}],
            },
            "recent_records": [
                {
                    "id": 42,
                    "timestamp": "2026-05-31T00:00:00+00:00",
                    "model": "qwen",
                    "x_title": "tgBot",
                    "upstream_key_fingerprint": "fp-1",
                }
            ],
        }

    async def get_dashboard_filter_options(self, start_date, end_date, **kwargs):
        return {
            "operations": ["chat"],
            "gateway_models": ["gateway-model"],
            "providers": ["openrouter"],
            "models": ["qwen"],
            "upstream_keys": ["fp-1"],
            "usage_sources": ["provider"],
            "x_titles": ["tgBot"],
        }

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class _FakeFallbackEventsDB:
    def __init__(self):
        self.upstream_calls: list[tuple[str, object, object, object]] = []
        self.dashboard_calls: list[dict] = []
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

    async def get_dashboard_fallback(self, period: str, start_date, end_date, **kwargs):
        self.dashboard_calls.append(
            {"period": period, "start_date": start_date, "end_date": end_date, **kwargs}
        )
        return {
            "summary": {
                "attempts": 3,
                "successes": 2,
                "errors": 1,
                "success_rate": 66.67,
                "avg_duration_ms": 120,
                "max_duration_ms": 300,
            },
            "series": [{"time_period": "2026-05-31", "attempts": 3, "successes": 2, "errors": 1}],
            "error_types": [{"label": "http_429", "errors": 1}],
            "upstream": [{"label": "openrouter / qwen", "provider": "openrouter", "model": "qwen", "upstream_key_fingerprint": "fp-1", "attempts": 3}],
        }

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class _FakeApiKeysDB:
    def __init__(self, valid_key_id: int):
        self.valid_key_id = valid_key_id

    def get_by_id(self, key_id: int):
        if key_id == self.valid_key_id:
            return SimpleNamespace(
                id=key_id,
                name=f"key-{key_id}",
                disabled=False,
                budget_usd=None,
                spent_usd=0.0,
                budget_period="none",
                last_used_at=None,
            )
        return None

    def list_all(self):
        record = self.get_by_id(self.valid_key_id)
        return [record] if record is not None else []


class _FakeRejectionsDB:
    def __init__(self):
        self.dashboard_calls: list[dict] = []

    async def get_dashboard_rejections(self, period: str, start_date, end_date, **kwargs):
        self.dashboard_calls.append(
            {"period": period, "start_date": start_date, "end_date": end_date, **kwargs}
        )
        return {
            "summary": {"rejections": 1},
            "series": [{"time_period": "2026-05-31", "rejections": 1}],
            "categories": [{"label": "rate_limited", "rejections": 1}],
            "status_codes": [{"label": "429", "status_code": 429, "rejections": 1}],
            "recent": [],
        }

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class StatsApiPaginationTests(unittest.TestCase):
    def setUp(self):
        self._clear_caches()
        self.addCleanup(self._clear_caches)

    @staticmethod
    def _clear_caches() -> None:
        stats_module._usage_stats_cache.invalidate()
        stats_module._fallback_stats_cache.invalidate()
        stats_module._upstream_stats_cache.invalidate()

    @contextmanager
    def _client(
        self,
        fake_tokens_usage_db: _FakeTokensUsageDB,
        fake_api_keys_db: _FakeApiKeysDB | None = None,
        fake_fallback_events_db: _FakeFallbackEventsDB | None = None,
        fake_rejections_db: _FakeRejectionsDB | None = None,
        active_requests_registry: ActiveRequestsRegistry | None = None,
    ):
        fallback_events_db = fake_fallback_events_db or _FakeFallbackEventsDB()
        service_overrides = {
            "tokens_usage_db": fake_tokens_usage_db,
            "fallback_events_db": fallback_events_db,
            "rejections_db": fake_rejections_db or _FakeRejectionsDB(),
        }
        if fake_api_keys_db is not None:
            service_overrides["api_keys_db"] = fake_api_keys_db
        if active_requests_registry is not None:
            service_overrides["active_requests_registry"] = active_requests_registry

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with installed_runtime(app, **service_overrides):
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(stats_module.stats_router, prefix="/v1")
        app.add_middleware(RuntimeSnapshotMiddleware)
        app.add_middleware(ApiKeyAuthMiddleware)

        with patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "test-gateway-key",
        ):
            with TestClient(app) as client:
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

    def test_usage_records_without_authenticated_role_fails_closed(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with installed_runtime(app, tokens_usage_db=fake_tokens_usage_db):
                yield

        app = FastAPI(lifespan=lifespan)
        app.include_router(stats_module.stats_router, prefix="/v1")

        with TestClient(app) as client:
            response = client.get("/v1/api/usage-records")

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.json()["detail"], "Authenticated API key role is not recognized.")
        self.assertEqual(fake_tokens_usage_db.calls, [])

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
            upstream_key_fingerprint="fp-upstream",
            prompt_tokens=42,
            total_tokens=42,
            is_estimated=True,
        )

        with self._client(
            fake_tokens_usage_db,
            active_requests_registry=active_requests,
        ) as client:
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
        self.assertEqual(payload["records"][0]["upstream_key_fingerprint"], "fp-upstream")
        self.assertEqual(payload["records"][0]["prompt_tokens"], 42)
        self.assertEqual(payload["records"][0]["total_tokens"], 42)
        self.assertTrue(payload["records"][0]["is_estimated"])
        self.assertEqual(payload["records"][1]["status"], "completed")
        self.assertEqual(fake_tokens_usage_db.calls, [(1, 0, None)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [None])

    def test_usage_records_pagination_offsets_past_running_requests(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        active_requests = ActiveRequestsRegistry()
        active_requests.start(
            request_id="req-running",
            path="/v1/embeddings",
            api_key_id=None,
            gateway_model="gateway-embeddings",
            operation="embeddings",
        )

        with self._client(
            fake_tokens_usage_db,
            active_requests_registry=active_requests,
        ) as client:
            response = client.get(
                "/v1/api/usage-records?limit=2&offset=1",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual([record["status"] for record in response.json()["records"]], ["completed", "completed"])
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 0, None)])

    def test_master_usage_records_ignore_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?limit=2&offset=0&api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_tokens_usage_db.calls, [(2, 0, None)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [None])

    def test_master_usage_stats_ignore_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-stats/month?api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(fake_tokens_usage_db.aggregated_calls), 1)
        self.assertIsNone(fake_tokens_usage_db.aggregated_calls[0][3])

    def test_master_usage_filter_ignores_non_positive_api_key_id(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db) as client:
            response = client.get(
                "/v1/api/usage-records?api_key_id=0",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_tokens_usage_db.calls, [(25, 0, None)])
        self.assertEqual(fake_tokens_usage_db.count_calls, [None])

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

    def test_virtual_key_usage_filter_requires_api_key_id(self):
        request = Mock()
        request.state = SimpleNamespace(api_key_role=ROLE_USER)

        with self.assertRaises(HTTPException) as ctx:
            stats_module._effective_api_key_filter(request)

        self.assertEqual(ctx.exception.status_code, 401)

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

    def test_master_upstream_stats_ignore_api_key_id(self):
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
        self.assertIsNone(api_key_id)

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
            upstream_state = client.app.state.services.upstream_routing_state
            client.app.state.upstream_routing_state = Mock()
            upstream_state.mark_health(
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

    def test_master_usage_dashboard_applies_filters_without_raw_keys(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()
        fake_fallback_events_db = _FakeFallbackEventsDB()
        fake_rejections_db = _FakeRejectionsDB()

        active_requests = ActiveRequestsRegistry()
        active_requests.start(
            request_id="req-running",
            path="/v1/chat/completions",
            api_key_id=7,
            gateway_model="gateway-model",
            operation="chat",
            x_title="tgBot",
        )
        active_requests.update("req-running", provider="openrouter", model="qwen")

        with self._client(
            fake_tokens_usage_db,
            _FakeApiKeysDB(valid_key_id=7),
            fake_fallback_events_db=fake_fallback_events_db,
            fake_rejections_db=fake_rejections_db,
            active_requests_registry=active_requests,
        ) as client:
            response = client.get(
                "/v1/api/analytics-dashboard"
                "?range=7d&bucket=day&api_key_id=7&operation=chat"
                "&gateway_model=gateway-model&provider=openrouter&model=qwen"
                "&upstream_key_fingerprint=fp-1&usage_source=provider&x_title=tgBot&estimated=true",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"]["role"], "master")
        self.assertFalse(payload["scope"]["can_filter_keys"])
        self.assertIsNone(payload["filters"]["api_key_id"])
        self.assertEqual(payload["filters"]["api_key_scope"], "all")
        self.assertEqual(payload["filters"]["operation"], "chat")
        self.assertEqual(payload["filters"]["x_title"], "tgBot")
        self.assertEqual(payload["totals"]["requests"], 2)
        self.assertEqual(payload["totals"]["active_requests"], 0)
        self.assertEqual(payload["totals"]["fallback_attempts"], 3)
        self.assertEqual(payload["totals"]["rejections"], 0)
        self.assertEqual(len(payload["warnings"]), 1)
        self.assertEqual(payload["breakdowns"]["api_keys"][0]["api_key_name"], "Unattributed")
        self.assertEqual(payload["breakdowns"]["x_titles"][0]["label"], "tgBot")
        self.assertEqual(payload["recent_records"][0]["x_title"], "tgBot")
        self.assertEqual(payload["filter_options"]["api_keys"][0]["name"], "key-7")
        self.assertEqual(payload["filter_options"]["x_titles"], ["tgBot"])
        self.assertNotIn("api_key", payload["filter_options"]["api_keys"][0])
        self.assertNotIn("raw_api_key", str(payload))

        usage_call = fake_tokens_usage_db.dashboard_calls[0]
        self.assertEqual(usage_call["period"], "day")
        self.assertIsNone(usage_call["api_key_id"])
        self.assertEqual(usage_call["operation"], "chat")
        self.assertEqual(usage_call["upstream_key_fingerprint"], "fp-1")
        self.assertEqual(usage_call["usage_source"], "provider")
        self.assertEqual(usage_call["x_title"], "tgBot")
        self.assertTrue(usage_call["is_estimated"])
        self.assertIsNone(fake_fallback_events_db.dashboard_calls[0]["api_key_id"])
        self.assertEqual(fake_rejections_db.dashboard_calls, [])

    def test_analytics_dashboard_totals_include_ttft_and_percentile_fields(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(
            fake_tokens_usage_db,
            _FakeApiKeysDB(valid_key_id=7),
        ) as client:
            response = client.get(
                "/v1/api/analytics-dashboard?range=7d&bucket=day",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        totals = response.json()["totals"]
        self.assertEqual(totals["duration_p50_ms"], 140)
        self.assertEqual(totals["duration_p95_ms"], 240)
        self.assertEqual(totals["ttft_avg_ms"], 80)
        self.assertEqual(totals["ttft_max_ms"], 120)
        self.assertEqual(totals["ttft_p50_ms"], 75)
        self.assertEqual(totals["ttft_p95_ms"], 115)

    def test_virtual_key_usage_dashboard_forces_own_scope(self):
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
            response = client.get("/v1/api/analytics-dashboard?api_key_id=999&range=24h")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["scope"]["role"], "user")
        self.assertEqual(payload["scope"]["api_key_id"], 11)
        self.assertFalse(payload["scope"]["can_filter_keys"])
        self.assertEqual(payload["filters"]["bucket"], "hour")
        self.assertEqual(payload["breakdowns"]["api_keys"], [])
        self.assertEqual(payload["breakdowns"]["upstream_keys"], [])
        self.assertEqual(payload["filter_options"]["api_keys"], [])
        self.assertEqual(payload["filter_options"]["upstream_keys"], [])
        self.assertEqual(payload["reliability"]["fallback"]["error_types"], [])
        self.assertEqual(payload["reliability"]["fallback"]["upstream"], [])
        self.assertNotIn("upstream_key_fingerprint", payload["recent_records"][0])
        self.assertEqual(fake_tokens_usage_db.dashboard_calls[0]["api_key_id"], 11)
        self.assertEqual(fake_fallback_events_db.dashboard_calls[0]["api_key_id"], 11)

    def test_usage_dashboard_rejects_invalid_scope_and_filters(self):
        fake_tokens_usage_db = _FakeTokensUsageDB()

        with self._client(fake_tokens_usage_db, _FakeApiKeysDB(valid_key_id=7)) as client:
            invalid_scope = client.get(
                "/v1/api/analytics-dashboard?api_key_scope=unknown",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            unattributed_with_ignored_api_key_id = client.get(
                "/v1/api/analytics-dashboard?api_key_scope=unattributed&api_key_id=7",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            invalid_bucket = client.get(
                "/v1/api/analytics-dashboard?bucket=minute",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(invalid_scope.status_code, 400)
        self.assertEqual(unattributed_with_ignored_api_key_id.status_code, 200)
        self.assertEqual(invalid_bucket.status_code, 400)
        self.assertEqual(len(fake_tokens_usage_db.dashboard_calls), 1)
        self.assertTrue(fake_tokens_usage_db.dashboard_calls[0]["api_key_unattributed"])


if __name__ == "__main__":
    unittest.main()
