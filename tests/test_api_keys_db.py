"""Unit tests for the virtual API keys database."""

from __future__ import annotations

import os
import math
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import (
    API_KEY_PREFIX,
    ApiKeysDB,
    compute_next_budget_reset,
    generate_api_key,
)


class _ApiKeysDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._root = Path(self._tmp.name)
        os.makedirs(self._root / "db", exist_ok=True)

        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(self._root / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.db = ApiKeysDB(db_filename="test_api_keys.db")


class GenerateApiKeyTests(unittest.TestCase):
    def test_key_has_prefix_and_is_unique(self):
        first = generate_api_key()
        second = generate_api_key()
        self.assertTrue(first.startswith(API_KEY_PREFIX))
        self.assertTrue(second.startswith(API_KEY_PREFIX))
        self.assertNotEqual(first, second)
        self.assertGreater(len(first), len(API_KEY_PREFIX) + 20)


class ApiKeysDBCrudTests(_ApiKeysDBTestBase):
    def test_create_returns_record_with_plaintext_key(self):
        record = self.db.create(
            name="team-a",
            budget_usd=5.0,
            rpm=60,
            tpm=1_000,
            allowed_models=["gpt-4o", "claude-3"],
            metadata={"owner": "alice"},
        )
        self.assertGreater(record.id, 0)
        self.assertEqual(record.name, "team-a")
        self.assertTrue(record.api_key.startswith(API_KEY_PREFIX))
        self.assertEqual(record.budget_usd, 5.0)
        self.assertEqual(record.rpm, 60)
        self.assertEqual(record.tpm, 1_000)
        self.assertEqual(record.allowed_models, ["gpt-4o", "claude-3"])
        self.assertEqual(record.metadata, {"owner": "alice"})
        self.assertEqual(record.spent_usd, 0.0)
        self.assertFalse(record.disabled)

    def test_create_rejects_empty_name(self):
        with self.assertRaises(ValueError):
            self.db.create(name="   ")

    def test_get_by_key_finds_existing_record(self):
        record = self.db.create(name="k1")
        fetched = self.db.get_by_key(record.api_key)
        self.assertIsNotNone(fetched)
        self.assertEqual(fetched.id, record.id)

    def test_get_by_key_returns_none_for_unknown_key(self):
        self.assertIsNone(self.db.get_by_key("unknown"))

    def test_update_patches_specific_fields(self):
        record = self.db.create(name="k1", rpm=10, tpm=100)
        updated = self.db.update(record.id, name="renamed", rpm=0, tpm=999)
        self.assertIsNotNone(updated)
        self.assertEqual(updated.name, "renamed")
        self.assertIsNone(updated.rpm)  # rpm=0 stored as NULL
        self.assertEqual(updated.tpm, 999)

    def test_update_clears_nullable_fields_with_explicit_none(self):
        record = self.db.create(
            name="k1",
            budget_usd=5.0,
            rpm=10,
            tpm=100,
            metadata={"owner": "alice"},
        )
        updated = self.db.update(
            record.id,
            budget_usd=None,
            rpm=None,
            tpm=None,
            metadata=None,
        )
        self.assertIsNotNone(updated)
        self.assertIsNone(updated.budget_usd)
        self.assertIsNone(updated.rpm)
        self.assertIsNone(updated.tpm)
        self.assertEqual(updated.metadata, {})

    def test_update_allows_disabling_and_resetting_spent(self):
        record = self.db.create(name="k1")
        self.db.record_spent(record.id, 2.5)
        updated = self.db.update(record.id, disabled=True, reset_spent=True)
        self.assertTrue(updated.disabled)
        self.assertEqual(updated.spent_usd, 0.0)

    def test_delete_removes_record(self):
        record = self.db.create(name="k1")
        self.assertTrue(self.db.delete(record.id))
        self.assertFalse(self.db.delete(record.id))
        self.assertIsNone(self.db.get_by_id(record.id))

    def test_list_all_returns_records_sorted_by_id(self):
        a = self.db.create(name="a")
        b = self.db.create(name="b")
        all_records = self.db.list_all()
        self.assertEqual([r.id for r in all_records], [a.id, b.id])

    def test_record_spent_increments_counter_without_batcher(self):
        record = self.db.create(name="k1")
        self.db.record_spent(record.id, 0.75)
        self.db.record_spent(record.id, 0.25)
        fresh = self.db.get_by_id(record.id)
        self.assertAlmostEqual(fresh.spent_usd, 1.0, places=6)
        self.assertIsNotNone(fresh.last_used_at)


class ApiKeysDBBatcherMismatchTests(_ApiKeysDBTestBase):
    """Regression test: binding a WriteBatcher whose ``db_path`` points at a
    different database must not silently drop ``spent_usd`` updates.

    Historically ``main.py`` shared the tokens_usage.db batcher with
    ``ApiKeysDB``; the batcher would route ``UPDATE api_keys …`` into
    ``tokens_usage.db`` where the table does not exist, raise inside a
    ``try/except``, and drop the write. The fix is to run ``record_spent``
    synchronously when no matching batcher is bound; this test locks that in
    by calling ``record_spent`` directly and verifying the fresh value hits
    the on-disk database.
    """

    def test_record_spent_persists_to_disk_without_matching_batcher(self):
        record = self.db.create(name="budget-track", budget_usd=1.0)
        self.db.record_spent(record.id, 0.42)
        # Fresh read — mimics the next-request snapshot used by access_control.
        fresh = self.db.get_by_id(record.id)
        self.assertAlmostEqual(fresh.spent_usd, 0.42, places=6)


class BudgetEnforcementTests(_ApiKeysDBTestBase):
    def test_invalid_budget_values_are_rejected(self):
        for budget_usd in (math.nan, math.inf, -1.0):
            with self.subTest(budget_usd=budget_usd):
                with self.assertRaises(ValueError):
                    self.db.create(name="k1", budget_usd=budget_usd)

    def test_null_budget_is_treated_as_unlimited(self):
        record = self.db.create(name="k1", budget_usd=None)
        self.assertFalse(record.budget_enforced())

    def test_zero_budget_exhausts_immediately(self):
        record = self.db.create(name="k1", budget_usd=0.0)
        self.assertTrue(record.budget_enforced())
        self.assertTrue(record.budget_exhausted())

    def test_positive_budget_exhausted_when_spent_reaches_limit(self):
        record = self.db.create(name="k1", budget_usd=1.0)
        self.db.record_spent(record.id, 0.5)
        self.db.record_spent(record.id, 0.5)
        fresh = self.db.get_by_id(record.id)
        self.assertTrue(fresh.budget_exhausted())

    def test_large_metadata_is_rejected(self):
        with self.assertRaises(ValueError):
            self.db.create(name="k1", metadata={"blob": "x" * (1024 * 1024)})


class ComputeNextBudgetResetTests(unittest.TestCase):
    def test_none_period_returns_none(self):
        self.assertIsNone(compute_next_budget_reset("none"))
        self.assertIsNone(compute_next_budget_reset("whatever"))

    def test_daily_returns_next_midnight_utc(self):
        now = datetime(2026, 5, 29, 12, 34, 56, tzinfo=timezone.utc)
        self.assertEqual(
            compute_next_budget_reset("daily", now=now),
            "2026-05-30T00:00:00+00:00",
        )

    def test_monthly_returns_first_of_next_month_utc(self):
        now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            compute_next_budget_reset("monthly", now=now),
            "2026-06-01T00:00:00+00:00",
        )

    def test_monthly_handles_december_rollover(self):
        now = datetime(2026, 12, 15, 8, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(
            compute_next_budget_reset("monthly", now=now),
            "2027-01-01T00:00:00+00:00",
        )


class BudgetPeriodCrudTests(_ApiKeysDBTestBase):
    def test_create_without_period_is_cumulative(self):
        record = self.db.create(name="k1", budget_usd=10.0)
        self.assertEqual(record.budget_period, "none")
        self.assertIsNone(record.budget_reset_at)

    def test_create_with_daily_period_sets_reset_boundary(self):
        record = self.db.create(name="k1", budget_usd=10.0, budget_period="daily")
        self.assertEqual(record.budget_period, "daily")
        self.assertIsNotNone(record.budget_reset_at)
        self.assertTrue(record.budget_reset_at.endswith("T00:00:00+00:00"))

    def test_create_rejects_invalid_period(self):
        with self.assertRaises(ValueError):
            self.db.create(name="k1", budget_period="weekly")

    def test_update_period_recomputes_reset_boundary(self):
        record = self.db.create(name="k1", budget_usd=10.0)
        updated = self.db.update(record.id, budget_period="monthly")
        self.assertEqual(updated.budget_period, "monthly")
        self.assertIsNotNone(updated.budget_reset_at)
        self.assertTrue(updated.budget_reset_at.startswith("20"))

    def test_update_period_to_none_clears_reset_boundary(self):
        record = self.db.create(name="k1", budget_usd=10.0, budget_period="daily")
        updated = self.db.update(record.id, budget_period="none")
        self.assertEqual(updated.budget_period, "none")
        self.assertIsNone(updated.budget_reset_at)

    def test_update_rejects_invalid_period(self):
        record = self.db.create(name="k1")
        with self.assertRaises(ValueError):
            self.db.update(record.id, budget_period="yearly")


class ResetDueBudgetsTests(_ApiKeysDBTestBase):
    def _force_reset_at(self, key_id: int, value: str | None) -> None:
        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET budget_reset_at = ? WHERE id = ?",
                (value, key_id),
            )
            conn.commit()

    def test_resets_only_due_periodic_keys(self):
        due = self.db.create(name="due", budget_usd=10.0, budget_period="daily")
        self.db.record_spent(due.id, 7.5)
        self._force_reset_at(due.id, "2000-01-01T00:00:00+00:00")

        future = self.db.create(name="future", budget_usd=10.0, budget_period="daily")
        self.db.record_spent(future.id, 3.0)  # reset_at stays in the future

        cumulative = self.db.create(name="cumulative", budget_usd=10.0)
        self.db.record_spent(cumulative.id, 4.0)

        now = datetime(2026, 5, 29, 12, 0, 0, tzinfo=timezone.utc)
        reset = self.db.reset_due_budgets(now=now)

        self.assertEqual([r.id for r in reset], [due.id])
        self.assertEqual(reset[0].spent_usd, 0.0)
        self.assertEqual(reset[0].budget_reset_at, "2026-05-30T00:00:00+00:00")

        self.assertEqual(self.db.get_by_id(due.id).spent_usd, 0.0)
        self.assertAlmostEqual(self.db.get_by_id(future.id).spent_usd, 3.0, places=6)
        self.assertAlmostEqual(self.db.get_by_id(cumulative.id).spent_usd, 4.0, places=6)

    def test_no_due_keys_returns_empty(self):
        self.db.create(name="cumulative", budget_usd=10.0)
        self.assertEqual(self.db.reset_due_budgets(), [])

    def test_reset_serializes_against_concurrent_writer(self):
        """The reset must take a write lock, not race a concurrent spend.

        While another connection holds an open write transaction, the reset
        cannot proceed; once that writer commits, the reset completes and
        zeroes the (now-accumulated) counter. This guards the IMMEDIATE
        transaction that makes the SELECT + zeroing UPDATE atomic w.r.t.
        ``record_spent`` running on a separate connection.
        """
        due = self.db.create(name="due", budget_usd=10.0, budget_period="daily")
        self.db.record_spent(due.id, 8.0)
        self._force_reset_at(due.id, "2000-01-01T00:00:00+00:00")

        blocker = sqlite3.connect(self.db.db_path, timeout=30.0)
        self.addCleanup(blocker.close)
        blocker.execute("PRAGMA busy_timeout=30000")
        blocker.execute("BEGIN IMMEDIATE")
        blocker.execute(
            "UPDATE api_keys SET spent_usd = spent_usd + 1.0 WHERE id = ?", (due.id,)
        )  # lock held, not yet committed

        done = threading.Event()
        result: dict = {}

        def worker() -> None:
            try:
                result["records"] = self.db.reset_due_budgets()
            except Exception as exc:  # pragma: no cover - surfaced via assertion
                result["error"] = exc
            finally:
                done.set()

        thread = threading.Thread(target=worker)
        thread.start()
        try:
            # Reset is blocked on the held write lock — it must not finish yet.
            self.assertFalse(done.wait(timeout=0.5))
            blocker.commit()  # release the lock; the +1.0 spend is now persisted
            self.assertTrue(done.wait(timeout=10))
        finally:
            thread.join(timeout=10)

        self.assertNotIn("error", result)
        self.assertEqual([r.id for r in result["records"]], [due.id])
        self.assertEqual(result["records"][0].spent_usd, 0.0)
        self.assertEqual(self.db.get_by_id(due.id).spent_usd, 0.0)
        self.assertNotEqual(
            self.db.get_by_id(due.id).budget_reset_at, "2000-01-01T00:00:00+00:00"
        )


class BudgetPeriodMigrationTests(unittest.TestCase):
    def test_legacy_database_gets_period_columns(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        root = Path(tmp.name)
        db_dir = root / "db"
        os.makedirs(db_dir, exist_ok=True)
        legacy_path = db_dir / "legacy_api_keys.db"

        # Build an old-style table that predates the periodic-budget columns.
        with sqlite3.connect(legacy_path) as conn:
            conn.execute(
                """
                CREATE TABLE api_keys (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    api_key TEXT NOT NULL UNIQUE,
                    budget_usd REAL,
                    spent_usd REAL NOT NULL DEFAULT 0.0,
                    rpm INTEGER,
                    tpm INTEGER,
                    allowed_models TEXT,
                    disabled INTEGER NOT NULL DEFAULT 0,
                    metadata TEXT,
                    created_at DATETIME NOT NULL,
                    last_used_at DATETIME
                )
                """
            )
            conn.execute(
                "INSERT INTO api_keys (name, api_key, spent_usd, disabled, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                ("legacy", "lgk_legacy", 1.0, 0, "2026-01-01T00:00:00"),
            )
            conn.commit()

        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(root / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        db = ApiKeysDB(db_filename="legacy_api_keys.db")
        with sqlite3.connect(db.db_path) as conn:
            columns = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
        self.assertIn("budget_period", columns)
        self.assertIn("budget_reset_at", columns)

        legacy = db.get_by_key("lgk_legacy")
        self.assertIsNotNone(legacy)
        self.assertEqual(legacy.budget_period, "none")
        self.assertIsNone(legacy.budget_reset_at)


class AllowedModelsTests(_ApiKeysDBTestBase):
    def test_empty_list_allows_any_model(self):
        record = self.db.create(name="k1")
        self.assertTrue(record.model_allowed("anything"))

    def test_list_restricts_model_choice(self):
        record = self.db.create(name="k1", allowed_models=["gpt-4o"])
        self.assertTrue(record.model_allowed("gpt-4o"))
        self.assertFalse(record.model_allowed("gpt-3.5"))
        self.assertFalse(record.model_allowed(None))


if __name__ == "__main__":
    unittest.main()
