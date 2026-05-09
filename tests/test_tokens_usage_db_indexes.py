"""Composite index and per-connection PRAGMA split.

``get_aggregated_usage`` filters by ``timestamp`` and groups by
``gateway_model`` — a composite ``(timestamp, gateway_model)`` index lets
SQLite satisfy the range scan + group prefix in one seek.

Also pins that ``journal_mode=WAL`` is persistent (so runtime connections
no longer need to re-issue it, which is the whole point of the
``PRAGMAS`` → ``RUNTIME_PRAGMAS`` split).
"""
import sqlite3
import unittest
from pathlib import Path

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.write_batcher import PRAGMAS, RUNTIME_PRAGMAS


class TokensUsageDBIndexesTests(unittest.TestCase):
    def test_composite_index_on_timestamp_and_gateway_model_is_created(self):
        db_path = Path("db/test_tokens_usage_composite_index.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            TokensUsageDB(db_filename=db_path.name)

            with sqlite3.connect(db_path) as conn:
                rows = conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tokens_usage'"
                ).fetchall()

            index_names = {row[0] for row in rows}
            self.assertIn("idx_tokens_usage_timestamp_gateway_model", index_names)
        finally:
            db_path.unlink(missing_ok=True)

    def test_composite_index_columns_match_query_pattern(self):
        db_path = Path("db/test_tokens_usage_composite_index_columns.sqlite")
        db_path.parent.mkdir(parents=True, exist_ok=True)

        try:
            TokensUsageDB(db_filename=db_path.name)

            with sqlite3.connect(db_path) as conn:
                info = conn.execute(
                    "PRAGMA index_info('idx_tokens_usage_timestamp_gateway_model')"
                ).fetchall()

            column_names = [row[2] for row in info]
            self.assertEqual(column_names, ["timestamp", "gateway_model"])
        finally:
            db_path.unlink(missing_ok=True)


class RuntimePragmasTests(unittest.TestCase):
    def test_runtime_pragmas_exclude_journal_mode(self):
        joined = " ".join(RUNTIME_PRAGMAS).lower()
        self.assertNotIn("journal_mode", joined)

    def test_runtime_pragmas_include_synchronous_and_busy_timeout(self):
        joined = " ".join(RUNTIME_PRAGMAS).lower()
        self.assertIn("synchronous", joined)
        self.assertIn("busy_timeout", joined)

    def test_legacy_pragmas_still_available_for_backwards_compatibility(self):
        # PRAGMAS is still exported so external tooling/tests don't break.
        joined = " ".join(PRAGMAS).lower()
        self.assertIn("journal_mode", joined)


if __name__ == "__main__":
    unittest.main()
