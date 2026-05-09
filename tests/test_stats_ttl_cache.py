"""Integration tests: /api/usage-stats and /api/fallback-stats respect TTL cache.

Verifies that repeated calls within the TTL window hit the cache and avoid
re-querying the database, while different periods do not interfere.
"""
import unittest
from contextlib import ExitStack, contextmanager
from datetime import timezone
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import stats as stats_module


class _CountingTokensUsageDB:
    def __init__(self):
        self.aggregated_calls: list[tuple[str, object, object, object]] = []

    async def get_aggregated_usage(self, period: str, start_date=None, end_date=None, *, api_key_id=None):
        self.aggregated_calls.append((period, start_date, end_date, api_key_id))
        return [
            {
                "time_period": period,
                "gateway_model": "gateway",
                "operation": "chat",
                "provider": "provider",
                "model": "model",
                "total_tokens": 1,
            }
        ]

    async def get_latest_usage_records(self, limit: int = 25, offset: int = 0, *, api_key_id=None):
        return []

    async def get_total_records_count(self, *, api_key_id=None):
        return 0

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class _CountingFallbackEventsDB:
    def __init__(self):
        self.aggregated_calls: list[tuple[str, object, object]] = []

    async def get_aggregated_stats(self, period: str, start_date=None, end_date=None):
        self.aggregated_calls.append((period, start_date, end_date))
        return [{"time_period": period, "failures": 2}]

    async def get_fallback_records(self, limit: int = 25, offset: int = 0):
        return [], 0

    def cleanup_old_records(self, retention_days: int = 180):
        return None


class StatsTtlCacheTests(unittest.TestCase):
    def setUp(self):
        stats_module._usage_stats_cache.invalidate()
        stats_module._fallback_stats_cache.invalidate()

    @contextmanager
    def _client(self, fake_tokens_db, fake_fallback_db=None):
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        fake_config_loader = Mock()
        fake_config_loader.load_providers.return_value = {}
        fake_config_loader.load_fallback_rules.return_value = {}

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=fake_config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB", return_value=fake_tokens_db))
            if fake_fallback_db is not None:
                stack.enter_context(patch("main.FallbackEventsDB", return_value=fake_fallback_db))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
                yield client

    def test_usage_stats_repeated_calls_within_ttl_hit_cache(self):
        db = _CountingTokensUsageDB()
        with self._client(db) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            r1 = client.get("/v1/api/usage-stats/day", headers=headers)
            r2 = client.get("/v1/api/usage-stats/day", headers=headers)
            r3 = client.get("/v1/api/usage-stats/day", headers=headers)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        self.assertEqual(r2.json(), r3.json())
        self.assertEqual(len(db.aggregated_calls), 1)
        _, start_date, end_date, _ = db.aggregated_calls[0]
        self.assertIs(start_date.tzinfo, timezone.utc)
        self.assertIs(end_date.tzinfo, timezone.utc)

    def test_usage_stats_different_periods_cached_independently(self):
        db = _CountingTokensUsageDB()
        with self._client(db) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            client.get("/v1/api/usage-stats/day", headers=headers)
            client.get("/v1/api/usage-stats/hour", headers=headers)
            client.get("/v1/api/usage-stats/day", headers=headers)
            client.get("/v1/api/usage-stats/hour", headers=headers)

        periods_called = [c[0] for c in db.aggregated_calls]
        self.assertEqual(sorted(periods_called), ["day", "hour"])

    def test_usage_stats_after_cache_invalidation_refetches(self):
        db = _CountingTokensUsageDB()
        with self._client(db) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            client.get("/v1/api/usage-stats/day", headers=headers)
            stats_module._usage_stats_cache.invalidate()
            client.get("/v1/api/usage-stats/day", headers=headers)

        self.assertEqual(len(db.aggregated_calls), 2)

    def test_fallback_stats_repeated_calls_within_ttl_hit_cache(self):
        tokens_db = _CountingTokensUsageDB()
        fallback_db = _CountingFallbackEventsDB()
        with self._client(tokens_db, fallback_db) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            r1 = client.get("/v1/api/fallback-stats/day", headers=headers)
            r2 = client.get("/v1/api/fallback-stats/day", headers=headers)

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r1.json(), r2.json())
        self.assertEqual(len(fallback_db.aggregated_calls), 1)
        _, start_date, end_date = fallback_db.aggregated_calls[0]
        self.assertIs(start_date.tzinfo, timezone.utc)
        self.assertIs(end_date.tzinfo, timezone.utc)


if __name__ == "__main__":
    unittest.main()
