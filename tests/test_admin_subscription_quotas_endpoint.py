"""Tests for the /v1/admin/upstream-quotas endpoint."""
from __future__ import annotations

import os
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_subscription_quotas import router as upstream_quota_router
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.upstream_subscription_quota import (
    SubscriptionQuotaSnapshot,
)


MASTER_KEY = "test-master-key-upstream"


class _FakeConfigLoader:
    def __init__(self):
        self.providers_config = {}


class _FakeQuotaService:
    def __init__(self, snapshots):
        self._snapshots = snapshots

    async def fetch_all(self, *, providers_config):
        return self._snapshots


def _build_app(quota_service=None, config_loader=None) -> FastAPI:
    app = FastAPI()
    app.state.upstream_subscription_quota_service = quota_service or _FakeQuotaService([])
    app.state.config_loader = config_loader or _FakeConfigLoader()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(upstream_quota_router, prefix="/v1")
    return app


class TestUpstreamQuotasEndpointMaster200(unittest.TestCase):
    def setUp(self):
        self.patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_endpoint_master_200(self):
        snapshot = SubscriptionQuotaSnapshot(
            provider="copilot",
            kind="github_copilot",
            plan="business",
            reset_date="2026-06-01",
            categories={},
            fetched_at=1234567890.0,
            error=None,
        )
        app = _build_app(quota_service=_FakeQuotaService([snapshot]))
        client = TestClient(app)

        resp = client.get(
            "/v1/admin/upstream-quotas",
            headers={"Authorization": f"Bearer {MASTER_KEY}"},
        )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("snapshots", data)
        self.assertEqual(len(data["snapshots"]), 1)
        self.assertEqual(data["snapshots"][0]["provider"], "copilot")
        self.assertEqual(data["snapshots"][0]["kind"], "github_copilot")


class TestUpstreamQuotasEndpointVirtualKey403(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from llm_gateway_core.db.api_keys_db import ApiKeysDB
        import llm_gateway_core.db.api_keys_db as api_keys_db_module

        self._tmp = tempfile.TemporaryDirectory()
        os.makedirs(Path(self._tmp.name) / "db", exist_ok=True)

        self._path_patcher = patch.object(
            api_keys_db_module,
            "__file__",
            str(Path(self._tmp.name) / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        self._path_patcher.start()

        self.db = ApiKeysDB(db_filename="test_upstream_quota.db")
        self.rec = self.db.create(name="virtual-user")

        self.key_patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.key_patcher.start()

    def tearDown(self):
        self.key_patcher.stop()
        self._path_patcher.stop()
        self._tmp.cleanup()

    def test_endpoint_virtual_key_403(self):
        app = _build_app()
        app.state.api_keys_db = self.db
        client = TestClient(app)

        resp = client.get(
            "/v1/admin/upstream-quotas",
            headers={"Authorization": f"Bearer {self.rec.api_key}"},
        )
        self.assertEqual(resp.status_code, 403)


class TestUpstreamQuotasEndpointNoAuth401(unittest.TestCase):
    def setUp(self):
        self.patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_endpoint_no_auth_401(self):
        app = _build_app()
        client = TestClient(app)

        resp = client.get("/v1/admin/upstream-quotas")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
