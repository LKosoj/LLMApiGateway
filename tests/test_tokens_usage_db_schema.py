import sqlite3
import unittest
from datetime import datetime, timezone
from pathlib import Path

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from tests._async_compat import run_async


class TokensUsageDBSchemaTests(unittest.TestCase):
    def test_init_db_adds_request_id_gateway_model_operation_and_duration_columns_for_existing_database(self):
        db_path = Path("db/test_request_id_gateway_model_operation_and_duration_migration.sqlite")
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
                        reasoning_tokens INTEGER DEFAULT 0,
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

            self.assertIn("request_id", column_names)
            self.assertIn("gateway_model", column_names)
            self.assertIn("operation", column_names)
            self.assertIn("duration_ms", column_names)
        finally:
            db_path.unlink(missing_ok=True)

    def test_latest_usage_records_include_request_id_gateway_model_operation_and_duration(self):
        db_path = Path("db/test_request_id_gateway_model_operation_and_duration_records.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "cost": 0.1,
                    "gateway_model": "gateway-model",
                    "operation": "embeddings",
                    "model": "provider-model",
                    "provider": "provider-name",
                    "request_id": "req-456",
                    "duration_ms": 3210,
                }
            )

            latest_record = run_async(db.get_latest_usage_records(limit=1, offset=0))[0]
            created_at = datetime.fromisoformat(latest_record["timestamp"])

            self.assertEqual(latest_record["request_id"], "req-456")
            self.assertEqual(latest_record["gateway_model"], "gateway-model")
            self.assertEqual(latest_record["operation"], "embeddings")
            self.assertEqual(latest_record["provider"], "provider-name")
            self.assertEqual(latest_record["duration_ms"], 3210)
            self.assertIs(created_at.tzinfo, timezone.utc)
        finally:
            db_path.unlink(missing_ok=True)

    def test_aggregated_usage_groups_by_gateway_model_operation_provider_and_model(self):
        db_path = Path("db/test_usage_aggregation.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "gateway_model": "gateway-fast",
                    "operation": "chat",
                    "model": "zai.glm-5",
                    "provider": "devbox",
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 4,
                    "completion_tokens": 5,
                    "total_tokens": 9,
                    "gateway_model": "gateway-fast",
                    "operation": "embeddings",
                    "model": "zai.glm-5",
                    "provider": "devbox",
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 6,
                    "completion_tokens": 7,
                    "total_tokens": 13,
                    "gateway_model": "gateway-smart",
                    "operation": "rerank",
                    "model": "zai.glm-5",
                    "provider": "devbox",
                }
            )

            aggregated_usage = run_async(db.get_aggregated_usage("day"))

            self.assertEqual(len(aggregated_usage), 3)
            self.assertEqual(
                {
                    (row["gateway_model"], row["operation"], row["provider"], row["model"], row["total_tokens"])
                    for row in aggregated_usage
                },
                {
                    ("gateway-fast", "chat", "devbox", "zai.glm-5", 3),
                    ("gateway-fast", "embeddings", "devbox", "zai.glm-5", 9),
                    ("gateway-smart", "rerank", "devbox", "zai.glm-5", 13),
                },
            )
        finally:
            db_path.unlink(missing_ok=True)

    def test_init_db_adds_is_estimated_column_for_existing_database(self):
        db_path = Path("db/test_is_estimated_migration.sqlite")
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
                        reasoning_tokens INTEGER DEFAULT 0,
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

            self.assertIn("is_estimated", column_names)
        finally:
            db_path.unlink(missing_ok=True)

    def test_insert_usage_persists_is_estimated_flag(self):
        db_path = Path("db/test_is_estimated_roundtrip.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "gateway_model": "g",
                    "operation": "chat",
                    "is_estimated": True,
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 20,
                    "completion_tokens": 30,
                    "total_tokens": 50,
                    "gateway_model": "g",
                    "operation": "chat",
                }
            )

            rows = run_async(db.get_latest_usage_records(limit=10, offset=0))

            self.assertEqual(len(rows), 2)
            estimated_values = {r["is_estimated"] for r in rows}
            self.assertEqual(estimated_values, {0, 1})
        finally:
            db_path.unlink(missing_ok=True)

    def test_aggregated_usage_returns_estimated_count(self):
        db_path = Path("db/test_is_estimated_aggregation.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                    "gateway_model": "gw", "operation": "chat",
                    "provider": "p", "model": "m", "is_estimated": True,
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9,
                    "gateway_model": "gw", "operation": "chat",
                    "provider": "p", "model": "m",
                }
            )

            aggregated = run_async(db.get_aggregated_usage("day"))

            self.assertEqual(len(aggregated), 1)
            self.assertEqual(aggregated[0]["estimated_count"], 1)
            self.assertEqual(aggregated[0]["count"], 2)
        finally:
            db_path.unlink(missing_ok=True)

    def test_init_db_adds_cost_saved_column_for_existing_database(self):
        db_path = Path("db/test_cost_saved_migration.sqlite")
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
                        reasoning_tokens INTEGER DEFAULT 0,
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

            self.assertIn("cost_saved", column_names)
        finally:
            db_path.unlink(missing_ok=True)

    def test_insert_usage_persists_cost_saved(self):
        db_path = Path("db/test_cost_saved_roundtrip.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15,
                    "gateway_model": "g", "operation": "chat",
                    "cost": 0.02, "cost_saved": 0.007,
                }
            )

            rows = run_async(db.get_latest_usage_records(limit=1, offset=0))

            self.assertEqual(len(rows), 1)
            self.assertAlmostEqual(rows[0]["cost_saved"], 0.007, places=6)
        finally:
            db_path.unlink(missing_ok=True)

    def test_aggregated_usage_returns_cost_saved_sum(self):
        db_path = Path("db/test_cost_saved_aggregation.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3,
                    "gateway_model": "gw", "operation": "chat",
                    "provider": "p", "model": "m", "cost": 0.01, "cost_saved": 0.003,
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 4, "completion_tokens": 5, "total_tokens": 9,
                    "gateway_model": "gw", "operation": "chat",
                    "provider": "p", "model": "m", "cost": 0.02, "cost_saved": 0.005,
                }
            )

            aggregated = run_async(db.get_aggregated_usage("day"))

            self.assertEqual(len(aggregated), 1)
            self.assertAlmostEqual(aggregated[0]["cost_saved"], 0.008, places=6)
        finally:
            db_path.unlink(missing_ok=True)

    def test_tokens_usage_db_uses_wal_journal_mode(self):
        db_path = Path("db/test_tokens_usage_wal.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            self.assertTrue(db.db_path.exists())

            with sqlite3.connect(db_path) as conn:
                journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

            self.assertEqual(str(journal_mode).lower(), "wal")
        finally:
            db_path.unlink(missing_ok=True)

    def test_dashboard_usage_keeps_incomplete_rows_and_filters_by_key(self):
        db_path = Path("db/test_dashboard_usage.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(
                {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.03,
                    "cost_saved": 0.01,
                    "gateway_model": "gateway-fast",
                    "operation": "chat",
                    "provider": "openrouter",
                    "model": "qwen",
                    "api_key_id": 5,
                    "is_estimated": True,
                    "duration_ms": 120,
                }
            )
            db.insert_usage(
                {
                    "prompt_tokens": 1,
                    "completion_tokens": 2,
                    "total_tokens": 3,
                    "cost": 0.0,
                    "operation": "chat",
                }
            )
            start = datetime.now(timezone.utc).replace(year=2000)
            end = datetime.now(timezone.utc).replace(year=2100)

            all_data = run_async(db.get_dashboard_usage("day", start, end))
            key_data = run_async(
                db.get_dashboard_usage(
                    "day",
                    start,
                    end,
                    api_key_id=5,
                    operation="chat",
                    provider="openrouter",
                    model="qwen",
                )
            )

            self.assertEqual(all_data["summary"]["requests"], 2)
            self.assertEqual(key_data["summary"]["requests"], 1)
            self.assertEqual(key_data["summary"]["estimated_count"], 1)
            gateway_labels = {row["label"] for row in all_data["breakdowns"]["gateway_models"]}
            api_key_labels = {row["label"] for row in all_data["breakdowns"]["api_keys"]}
            self.assertIn("unknown", gateway_labels)
            self.assertIn("unattributed", api_key_labels)
            self.assertIn("5", api_key_labels)
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
