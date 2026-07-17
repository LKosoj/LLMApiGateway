import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware.auth import ROLE_MASTER
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services


class _StatsNullRowsDB:
    def __init__(self):
        self.calls = []

    async def get_aggregated_usage(self, period: str, start_date=None, end_date=None, *, api_key_id=None):
        self.calls.append((period, start_date, end_date, api_key_id))
        return [
            {
                "time_period": "2026-04-22",
                "gateway_model": "gateway",
                "operation": "chat",
                "provider": "provider",
                "model": "model",
                "total_tokens": 10,
            },
            {
                "time_period": "2026-04-22",
                "gateway_model": "gateway",
                "operation": "chat",
                "provider": "provider",
                "model": None,
                "total_tokens": 20,
            },
        ]


class StatsNullRowsTests(unittest.TestCase):
    def setUp(self):
        self._clear_caches()
        self.addCleanup(self._clear_caches)

    @staticmethod
    def _clear_caches() -> None:
        stats_module._usage_stats_cache.invalidate()
        stats_module._fallback_stats_cache.invalidate()
        stats_module._upstream_stats_cache.invalidate()

    def test_aggregated_usage_excludes_null_model_provider_and_gateway_model(self):
        with tempfile.TemporaryDirectory() as temp_root, patch.dict(
            os.environ,
            {"GATEWAY_DB_DIR": temp_root},
        ):
            root = Path(temp_root).resolve()
            db = TokensUsageDB(db_filename="test_stats_null_rows.sqlite")
            self.assertTrue(db.db_path.resolve().is_relative_to(root))
            db.insert_usage(
                {
                    "prompt_tokens": 4,
                    "completion_tokens": 6,
                    "total_tokens": 10,
                    "gateway_model": "gateway",
                    "operation": "chat",
                    "provider": "provider",
                    "model": "model",
                }
            )
            db.insert_usage(
                {
                    "total_tokens": 20,
                    "gateway_model": "gateway",
                    "operation": "chat",
                    "provider": "provider",
                    "model": None,
                }
            )
            db.insert_usage(
                {
                    "total_tokens": 30,
                    "gateway_model": "gateway",
                    "operation": "chat",
                    "provider": None,
                    "model": "model",
                }
            )
            db.insert_usage(
                {
                    "total_tokens": 40,
                    "gateway_model": None,
                    "operation": "chat",
                    "provider": "provider",
                    "model": "model",
                }
            )

            rows = run_async(db.get_aggregated_usage("day"))

            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["gateway_model"], "gateway")
            self.assertEqual(rows[0]["provider"], "provider")
            self.assertEqual(rows[0]["model"], "model")
            self.assertEqual(rows[0]["total_tokens"], 10)

    def test_stats_handler_does_not_return_null_bucket_items(self):
        fake_db = _StatsNullRowsDB()
        services = make_app_services(tokens_usage_db=fake_db)
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    services=services,
                    tokens_usage_db=Mock(),
                )
            ),
            state=SimpleNamespace(api_key_role=ROLE_MASTER),
        )

        response = run_async(stats_module.get_aggregated_stats(request, "day"))
        payload = json.loads(response.body)

        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0]["model"], "model")
        self.assertEqual(payload[0]["total_tokens"], 10)
        self.assertEqual(len(fake_db.calls), 1)


if __name__ == "__main__":
    unittest.main()
