"""Tests for the free-tier LLM catalog page and JSON API endpoints.

Modeled on tests/test_quota_endpoint.py: builds a minimal FastAPI app with
the real auth middleware and the free_llm_catalog_router, and exercises it
through TestClient. No real HTTP call to freellmapi.co is made anywhere --
the service under test is fed status directly or via httpx.MockTransport in
tests/test_free_llm_catalog_service.py.
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

import llm_gateway_core.api.v1.free_llm_catalog as free_llm_catalog_module
from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.free_llm_catalog import free_llm_catalog_router
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.free_llm_catalog import FreeLlmCatalogService
from tests.runtime_test_support import bind_app_services


def _build_app(db: ApiKeysDB, catalog_service: FreeLlmCatalogService | None = None) -> FastAPI:
    app = FastAPI()
    bind_app_services(
        app,
        api_keys_db=db,
        free_llm_catalog_service=catalog_service or FreeLlmCatalogService(),
    )
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(free_llm_catalog_router, prefix="/v1")
    return app


class FreeLlmCatalogApiTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_free_llm_catalog.db")
        self.master_key = "master-free-models-test"

        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", self.master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    # -- /v1/api/free-models -------------------------------------------------

    def test_api_free_models_json_shape(self):
        from llm_gateway_core.services.free_llm_catalog import build_snapshot

        payload = {
            "version": "2026.07.18",
            "generatedAt": "2026-07-18T07:03:02.234Z",
            "platforms": [{"id": "cerebras", "name": "Cerebras"}],
            "models": [
                {
                    "platform": "cerebras",
                    "modelId": "gpt-oss-120b",
                    "displayName": "GPT-OSS 120B",
                    "intelligenceRank": 5,
                    "speedRank": 1,
                    "sizeLabel": "Large",
                    "limits": {"rpm": 30, "rpd": None, "tpm": 60000, "tpd": None},
                    "monthlyTokenBudget": "~50M",
                    "contextWindow": 65536,
                    "enabled": True,
                    "supportsVision": False,
                    "supportsTools": True,
                    "quirks": [],
                }
            ],
        }
        snapshot = build_snapshot(
            payload, min_monthly_tokens=30_000_000, now_iso="2026-07-19T00:00:00Z"
        )
        service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)
        service._snapshot = snapshot
        service._last_error = None

        app = _build_app(self.db, service)
        client = TestClient(app)
        resp = client.get("/v1/api/free-models", headers=self._master_headers())

        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(
            set(data.keys()),
            {
                "updatedAt",
                "sourceVersion",
                "sourceGeneratedAt",
                "lastError",
                "minMonthlyTokens",
                "providers",
            },
        )
        self.assertEqual(data["updatedAt"], "2026-07-19T00:00:00Z")
        self.assertEqual(data["sourceVersion"], "2026.07.18")
        self.assertIsNone(data["lastError"])
        self.assertEqual(data["minMonthlyTokens"], 30_000_000)
        self.assertEqual(len(data["providers"]), 1)
        provider = data["providers"][0]
        self.assertEqual(provider["id"], "cerebras")
        model = provider["models"][0]
        self.assertEqual(model["modelId"], "gpt-oss-120b")
        self.assertEqual(model["monthlyTokenBudgetFloor"], 50_000_000)

    def test_api_free_models_empty_snapshot_shape(self):
        app = _build_app(self.db, FreeLlmCatalogService(min_monthly_tokens=30_000_000))
        client = TestClient(app)
        resp = client.get("/v1/api/free-models", headers=self._master_headers())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "updatedAt": None,
                "sourceVersion": None,
                "sourceGeneratedAt": None,
                "lastError": None,
                "minMonthlyTokens": 30_000_000,
                "providers": [],
            },
        )

    def test_api_free_models_master_role_access(self):
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/api/free-models", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)

    def test_api_free_models_user_role_access(self):
        record = self.db.create(name="virtual-user")
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get(
            "/v1/api/free-models",
            headers={"Authorization": f"Bearer {record.api_key}"},
        )
        self.assertEqual(resp.status_code, 200)

    def test_api_free_models_requires_auth(self):
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/api/free-models")
        self.assertEqual(resp.status_code, 401)

    # -- /v1/ui/free-models ---------------------------------------------------

    def test_ui_free_models_page_returns_404_when_html_file_is_absent(self):
        # The endpoint must fail honestly instead of serving a stale/blank page.
        tmp_static = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_static.cleanup)
        static_dir_patch = patch.object(
            free_llm_catalog_module, "STATIC_DIR", Path(tmp_static.name)
        )
        static_dir_patch.start()
        self.addCleanup(static_dir_patch.stop)

        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/ui/free-models", headers=self._master_headers())
        self.assertEqual(resp.status_code, 404)

    def test_ui_free_models_page_serves_html_when_file_is_present(self):
        tmp_static = tempfile.TemporaryDirectory()
        self.addCleanup(tmp_static.cleanup)
        html_path = Path(tmp_static.name) / "free-models.html"
        html_path.write_text("<html><body>free models</body></html>", encoding="utf-8")

        static_dir_patch = patch.object(
            free_llm_catalog_module, "STATIC_DIR", Path(tmp_static.name)
        )
        static_dir_patch.start()
        self.addCleanup(static_dir_patch.stop)

        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/ui/free-models", headers=self._master_headers())

        self.assertEqual(resp.status_code, 200)
        self.assertIn("free models", resp.text)

    def test_ui_free_models_page_requires_auth(self):
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/ui/free-models")
        self.assertEqual(resp.status_code, 401)


if __name__ == "__main__":
    unittest.main()
