import json
import unittest
from pathlib import Path
from types import SimpleNamespace

from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware.auth import ROLE_MASTER
from tests._async_compat import run_async


def _unlink_sqlite_files(db_path: Path) -> None:
    db_path.unlink(missing_ok=True)
    db_path.with_name(db_path.name + "-wal").unlink(missing_ok=True)
    db_path.with_name(db_path.name + "-shm").unlink(missing_ok=True)


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
        stats_module._usage_stats_cache.invalidate()

    def test_aggregated_usage_excludes_null_model_provider_and_gateway_model(self):
        db_path = Path("db/test_stats_null_rows.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)
        _unlink_sqlite_files(db_path)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
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
        finally:
            _unlink_sqlite_files(db_path)

    def test_stats_handler_does_not_return_null_bucket_items(self):
        fake_db = _StatsNullRowsDB()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(tokens_usage_db=fake_db)),
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
