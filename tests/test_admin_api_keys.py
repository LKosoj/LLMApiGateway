"""Integration tests for Admin API keys endpoints."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_api_keys import admin_api_keys_router
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    app.state.api_keys_db = db
    app.state.rate_limiter = RateLimiter()
    app.state.usd_budget_ledger = UsdBudgetLedger()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(admin_api_keys_router, prefix="/v1")
    return app


class AdminApiKeysHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(Path(self._tmp.name) / "db", exist_ok=True)

        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(Path(self._tmp.name) / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.db = ApiKeysDB(db_filename="test_admin.db")

        master_key = "master-test-token"
        self.master_key = master_key
        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        self.app = _build_app(self.db)
        self.client = TestClient(self.app)

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def test_master_can_create_list_update_delete(self):
        # Create
        create_resp = self.client.post(
            "/v1/admin/api-keys",
            json={
                "name": "team-b",
                "budget_usd": 10.0,
                "rpm": 30,
                "allowed_models": ["gpt-4o"],
            },
            headers=self._master_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        body = create_resp.json()
        self.assertEqual(body["name"], "team-b")
        self.assertTrue(body["api_key"].startswith("lgk_"))
        key_id = body["id"]

        # List
        list_resp = self.client.get("/v1/admin/api-keys", headers=self._master_headers())
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()["keys"]), 1)

        # Update
        update_resp = self.client.patch(
            f"/v1/admin/api-keys/{key_id}",
            json={"disabled": True},
            headers=self._master_headers(),
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertTrue(update_resp.json()["disabled"])

        # Delete
        delete_resp = self.client.delete(
            f"/v1/admin/api-keys/{key_id}",
            headers=self._master_headers(),
        )
        self.assertEqual(delete_resp.status_code, 200)
        self.assertTrue(delete_resp.json()["deleted"])

    def test_create_rejects_empty_name(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "   "},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_virtual_key_cannot_access_admin_api(self):
        # Seed a virtual key; use it for auth against admin endpoints.
        record = self.db.create(name="team-c")
        resp = self.client.get(
            "/v1/admin/api-keys",
            headers={"Authorization": f"Bearer {record.api_key}"},
        )
        # Middleware blocks the path before routing → 403.
        self.assertEqual(resp.status_code, 403)

    def test_missing_auth_returns_401(self):
        resp = self.client.get("/v1/admin/api-keys")
        self.assertEqual(resp.status_code, 401)

    def test_get_404_for_missing_id(self):
        resp = self.client.get("/v1/admin/api-keys/999", headers=self._master_headers())
        self.assertEqual(resp.status_code, 404)

    def test_update_can_clear_nullable_limits_and_metadata(self):
        record = self.db.create(
            name="team-d",
            budget_usd=10.0,
            rpm=30,
            tpm=300,
            metadata={"owner": "alice"},
        )

        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={
                "budget_usd": None,
                "rpm": None,
                "tpm": None,
                "metadata": None,
            },
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["budget_usd"])
        self.assertIsNone(body["rpm"])
        self.assertIsNone(body["tpm"])
        self.assertEqual(body["metadata"], {})

    def test_create_with_budget_period_is_serialized(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "team-period", "budget_usd": 10.0, "budget_period": "daily"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["budget_period"], "daily")
        self.assertIsNotNone(body["budget_reset_at"])

    def test_create_rejects_invalid_budget_period(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "team-bad-period", "budget_period": "weekly"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_budget_period_recomputes_reset(self):
        record = self.db.create(name="team-up", budget_usd=10.0)
        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"budget_period": "monthly"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["budget_period"], "monthly")
        self.assertIsNotNone(body["budget_reset_at"])

    def test_reset_spent_refreshes_usd_budget_ledger(self):
        record = self.db.create(name="team-e", budget_usd=10.0)
        ledger = self.app.state.usd_budget_ledger
        ledger.sync_record(record.id, budget_usd=10.0, spent_usd=10.0)
        self.assertFalse(ledger.reserve(record.id, 5.0))

        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"reset_spent": True},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["spent_usd"], 0.0)
        self.assertTrue(ledger.reserve(record.id, 5.0))


if __name__ == "__main__":
    unittest.main()
