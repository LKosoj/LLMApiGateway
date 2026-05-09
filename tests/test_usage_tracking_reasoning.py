import sqlite3
import unittest
from pathlib import Path

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.utils.usage_tracking import extract_tokens_usage
from tests._async_compat import run_async


class UsageTrackingReasoningTests(unittest.TestCase):
    def _record_reasoning_usage(
        self,
        db_name: str,
        *,
        completion_tokens: int,
        reasoning_tokens: int,
    ) -> dict:
        db = TokensUsageDB(db_filename=db_name)
        usage = extract_tokens_usage({
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": completion_tokens,
                "total_tokens": 10 + completion_tokens,
                "completion_tokens_details": {
                    "reasoning_tokens": reasoning_tokens,
                },
            },
            "provider": "provider-name",
            "model": "provider-model",
        })
        usage["gateway_model"] = "gateway-model"
        usage["operation"] = "chat"

        db.insert_usage(usage)

        return run_async(db.get_latest_usage_records(limit=1, offset=0))[0]

    def test_completion_gt_reasoning_keeps_completion_total_in_db(self):
        db_path = Path("db/test_usage_tracking_reasoning_gt.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            row = self._record_reasoning_usage(
                db_path.name,
                completion_tokens=500,
                reasoning_tokens=300,
            )

            self.assertEqual(row["completion_tokens"], 500)
            self.assertEqual(row["reasoning_tokens"], 300)
        finally:
            db_path.unlink(missing_ok=True)

    def test_completion_lt_reasoning_keeps_completion_total_in_db(self):
        db_path = Path("db/test_usage_tracking_reasoning_lt.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            row = self._record_reasoning_usage(
                db_path.name,
                completion_tokens=100,
                reasoning_tokens=300,
            )

            self.assertEqual(row["completion_tokens"], 100)
            self.assertEqual(row["reasoning_tokens"], 300)
        finally:
            db_path.unlink(missing_ok=True)

    def test_init_db_adds_reasoning_tokens_column_for_existing_database(self):
        db_path = Path("db/test_usage_tracking_reasoning_migration.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tokens_usage (
                        id INTEGER PRIMARY KEY AUTOINCREMENT,
                        timestamp DATETIME NOT NULL,
                        prompt_tokens INTEGER DEFAULT 0,
                        completion_tokens INTEGER DEFAULT 0,
                        total_tokens INTEGER DEFAULT 0,
                        cached_tokens INTEGER DEFAULT 0,
                        cost REAL DEFAULT 0.0,
                        model TEXT,
                        provider TEXT
                    )
                    """
                )
                conn.commit()

            TokensUsageDB(db_filename=db_path.name)

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("PRAGMA table_info(tokens_usage)")
                column_names = {row[1] for row in cursor.fetchall()}

            self.assertIn("reasoning_tokens", column_names)
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
