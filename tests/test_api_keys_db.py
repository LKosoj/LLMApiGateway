"""Unit tests for the virtual API keys database."""

from __future__ import annotations

import os
import math
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import (
    API_KEY_PREFIX,
    ApiKeysDB,
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
