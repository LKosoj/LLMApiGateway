"""Validation tests for Admin API keys endpoints."""

from __future__ import annotations

import json
import math
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
from llm_gateway_core.services.rate_limiter import RateLimiter


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    app.state.api_keys_db = db
    app.state.rate_limiter = RateLimiter()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(admin_api_keys_router, prefix="/v1")
    return app


class AdminApiKeysValidationTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_admin_validation.db")
        self.master_key = "master-test-token"
        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", self.master_key),
        ]
        for patcher in key_patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

        self.client = TestClient(_build_app(self.db))

    def tearDown(self) -> None:
        self.client.close()

    def _master_headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.master_key}",
            "Content-Type": "application/json",
        }

    def _post_json(self, payload: dict) -> object:
        return self.client.post(
            "/v1/admin/api-keys",
            content=json.dumps(payload),
            headers=self._master_headers(),
        )

    def test_rejects_invalid_budget_values_and_large_metadata(self):
        vectors = [
            ("nan", {"name": "nan-budget", "budget_usd": math.nan}, "budget_usd"),
            ("inf", {"name": "inf-budget", "budget_usd": math.inf}, "budget_usd"),
            ("negative", {"name": "negative-budget", "budget_usd": -1.0}, "budget_usd"),
            (
                "large_metadata",
                {"name": "large-metadata", "metadata": {"blob": "x" * (1024 * 1024)}},
                "metadata",
            ),
        ]

        for label, payload, expected_detail in vectors:
            with self.subTest(label=label):
                response = self._post_json(payload)

                self.assertEqual(response.status_code, 400)
                self.assertIn(expected_detail, response.json()["detail"])


if __name__ == "__main__":
    unittest.main()
