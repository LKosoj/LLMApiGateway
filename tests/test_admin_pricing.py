"""Tests for admin pricing endpoints."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_pricing import admin_pricing_router
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeysDB
import llm_gateway_core.db.api_keys_db as api_keys_db_module
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.utils.html_cache import clear_cache

# Import the settings object used by the loader module so we can patch it in
# tests without affecting the global application settings.
from llm_gateway_core.config.loader import settings as _loader_settings


MASTER_KEY = "master-test-key"
# Test providers always use "test_provider" as the name; we must patch
# FALLBACK_PROVIDER both for load_providers() and reload_providers_config().
PATCHED_FALLBACK = "test_provider"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _providers_json_with_models(tmp_path: Path) -> Path:
    """Write a providers.json with a models block and return its path."""
    data = [
        {
            "test_provider": {
                "baseUrl": "https://example.com/v1",
                "apikey": "dummy",
                "models": {
                    "model_x": {
                        "input_rate": 1.5,
                        "output_rate": 3.0,
                    }
                },
            }
        }
    ]
    p = tmp_path / "providers.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _providers_json_no_models(tmp_path: Path) -> Path:
    """Write a providers.json without any models block."""
    data = [
        {
            "test_provider": {
                "baseUrl": "https://example.com/v1",
                "apikey": "dummy",
            }
        }
    ]
    p = tmp_path / "providers.json"
    p.write_text(json.dumps(data), encoding="utf-8")
    return p


def _build_app(providers_path: Path) -> tuple[FastAPI, ConfigLoader]:
    """Build a minimal FastAPI app with the pricing router.

    The loader settings are patched so that FALLBACK_PROVIDER validation
    passes with our test provider.
    """
    loader = ConfigLoader(providers_filename=str(providers_path))
    loader.load_providers()  # Called with settings already patched by caller

    app = FastAPI()
    app.state.config_loader = loader
    app.state.rate_limiter = RateLimiter()
    app.state.usd_budget_ledger = UsdBudgetLedger()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(admin_pricing_router, prefix="/v1")
    return app, loader


def _master_headers() -> dict:
    return {"Authorization": f"Bearer {MASTER_KEY}"}


# ---------------------------------------------------------------------------
# Base test class that patches both the master key and the fallback provider
# for the entire test lifetime.
# ---------------------------------------------------------------------------


class _PricingTestBase(unittest.TestCase):
    """Patches GATEWAY_API_KEY and FALLBACK_PROVIDER for all pricing tests."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        p1 = patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY)
        p2 = patch.object(_loader_settings, "fallback_provider", PATCHED_FALLBACK)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestGetPricing(_PricingTestBase):
    def test_get_pricing_returns_current_rates(self):
        providers_path = _providers_json_with_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        resp = client.get("/v1/admin/pricing", headers=_master_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIn("items", body)
        self.assertEqual(len(body["items"]), 1)
        item = body["items"][0]
        self.assertEqual(item["provider"], "test_provider")
        self.assertEqual(item["model"], "model_x")
        self.assertAlmostEqual(item["input_rate"], 1.5)
        self.assertAlmostEqual(item["output_rate"], 3.0)

    def test_get_pricing_empty_when_no_models_block(self):
        providers_path = _providers_json_no_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        resp = client.get("/v1/admin/pricing", headers=_master_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["items"], [])


class TestPutPricing(_PricingTestBase):
    def test_put_pricing_writes_to_providers_json(self):
        providers_path = _providers_json_no_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        payload = {
            "items": [
                {
                    "provider": "test_provider",
                    "model": "gpt-4o",
                    "input_rate": 2.5,
                    "output_rate": 10.0,
                }
            ]
        }
        resp = client.put("/v1/admin/pricing", json=payload, headers=_master_headers())
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(len(body["items"]), 1)
        self.assertAlmostEqual(body["items"][0]["input_rate"], 2.5)
        self.assertAlmostEqual(body["items"][0]["output_rate"], 10.0)

        # Verify the file was updated on disk
        written = json.loads(providers_path.read_text(encoding="utf-8"))
        models = written[0]["test_provider"]["models"]
        self.assertIn("gpt-4o", models)
        self.assertAlmostEqual(models["gpt-4o"]["input_rate"], 2.5)

    def test_put_pricing_preserves_json5_comments(self):
        """After PUT the original file is backed up (comments detected), main file updated."""
        content = (
            '[\n'
            '  {\n'
            '    "test_provider": {\n'
            '      // a comment\n'
            '      "baseUrl": "https://example.com/v1",\n'
            '      "apikey": "dummy"\n'
            '    }\n'
            '  }\n'
            ']\n'
        )
        providers_path = self.tmp_path / "providers.json"
        providers_path.write_text(content, encoding="utf-8")

        app, _ = _build_app(providers_path)
        client = TestClient(app)

        payload = {
            "items": [
                {
                    "provider": "test_provider",
                    "model": "model_a",
                    "input_rate": 1.0,
                    "output_rate": 2.0,
                }
            ]
        }
        resp = client.put("/v1/admin/pricing", json=payload, headers=_master_headers())
        self.assertEqual(resp.status_code, 200)

        # A backup file must have been created (name format: providers.json.bak.<timestamp>)
        backups = list(self.tmp_path.glob("providers.json.bak.*"))
        self.assertGreater(len(backups), 0, "Expected a comment backup to be created")

        # The main file is now valid JSON (no comment), containing new models
        updated = json.loads(providers_path.read_text(encoding="utf-8"))
        models = updated[0]["test_provider"]["models"]
        self.assertIn("model_a", models)

    def test_put_pricing_atomic_write(self):
        """If write fails, the main file is untouched."""
        providers_path = _providers_json_no_models(self.tmp_path)
        original_content = providers_path.read_text(encoding="utf-8")

        app, _ = _build_app(providers_path)
        client = TestClient(app)

        # Patch _write_text_atomically to raise an error
        with patch(
            "llm_gateway_core.api.v1.admin_pricing._write_text_atomically",
            side_effect=OSError("disk full"),
        ):
            payload = {
                "items": [
                    {
                        "provider": "test_provider",
                        "model": "fail_model",
                        "input_rate": 9.9,
                        "output_rate": 9.9,
                    }
                ]
            }
            resp = client.put("/v1/admin/pricing", json=payload, headers=_master_headers())
            self.assertIn(resp.status_code, (500, 422))

        # Main file must remain unchanged
        self.assertEqual(providers_path.read_text(encoding="utf-8"), original_content)

    def test_put_pricing_invalid_negative_rate_422(self):
        providers_path = _providers_json_no_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        payload = {
            "items": [
                {
                    "provider": "test_provider",
                    "model": "some-model",
                    "input_rate": -1.0,
                    "output_rate": 1.0,
                }
            ]
        }
        resp = client.put("/v1/admin/pricing", json=payload, headers=_master_headers())
        self.assertEqual(resp.status_code, 422)


class TestCalculate(_PricingTestBase):
    def test_calculate_returns_correct_cost(self):
        """1M prompt tokens @$1.0 + 0.5M completion @$2.0 = $2.0"""
        data = [
            {
                "test_provider": {
                    "baseUrl": "https://example.com/v1",
                    "apikey": "dummy",
                    "models": {
                        "model_x": {"input_rate": 1.0, "output_rate": 2.0}
                    },
                }
            }
        ]
        providers_path = self.tmp_path / "providers.json"
        providers_path.write_text(json.dumps(data), encoding="utf-8")
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        resp = client.post(
            "/v1/admin/pricing/calculate",
            json={
                "provider": "test_provider",
                "model": "model_x",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 500_000,
            },
            headers=_master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertAlmostEqual(body["cost_usd"], 2.0, places=6)
        self.assertAlmostEqual(body["input_rate"], 1.0)
        self.assertAlmostEqual(body["output_rate"], 2.0)

    def test_calculate_model_not_found_404(self):
        providers_path = _providers_json_no_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        resp = client.post(
            "/v1/admin/pricing/calculate",
            json={
                "provider": "test_provider",
                "model": "nonexistent",
                "prompt_tokens": 100,
                "completion_tokens": 100,
            },
            headers=_master_headers(),
        )
        self.assertEqual(resp.status_code, 404)


class TestAuthEnforcement(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp_path = Path(self._tmp.name)

        p1 = patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY)
        p2 = patch.object(_loader_settings, "fallback_provider", PATCHED_FALLBACK)
        p1.start()
        p2.start()
        self.addCleanup(p1.stop)
        self.addCleanup(p2.stop)

        # We need a real ApiKeysDB for virtual key creation
        os.makedirs(self.tmp_path / "db", exist_ok=True)
        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(self.tmp_path / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        providers_path = _providers_json_no_models(self.tmp_path)
        loader = ConfigLoader(providers_filename=str(providers_path))
        loader.load_providers()

        self.db = ApiKeysDB(db_filename="test_pricing_auth.db")

        self.app = FastAPI()
        self.app.state.config_loader = loader
        self.app.state.api_keys_db = self.db
        self.app.state.rate_limiter = RateLimiter()
        self.app.state.usd_budget_ledger = UsdBudgetLedger()
        self.app.middleware("http")(api_key_auth)
        self.app.include_router(auth_router)
        self.app.include_router(admin_pricing_router, prefix="/v1")
        self.client = TestClient(self.app)

    def test_endpoint_master_only_403_for_virtual_key(self):
        record = self.db.create(name="virtual-user")
        resp = self.client.get(
            "/v1/admin/pricing",
            headers={"Authorization": f"Bearer {record.api_key}"},
        )
        self.assertEqual(resp.status_code, 403)

    def test_endpoint_401_no_auth(self):
        resp = self.client.get("/v1/admin/pricing")
        self.assertEqual(resp.status_code, 401)


class TestHotReload(_PricingTestBase):
    def test_hot_reload_registry_after_put(self):
        """After PUT the new rates are immediately visible via GET without restart."""
        providers_path = _providers_json_no_models(self.tmp_path)
        app, loader = _build_app(providers_path)
        client = TestClient(app)

        # Initially empty
        resp = client.get("/v1/admin/pricing", headers=_master_headers())
        self.assertEqual(resp.json()["items"], [])

        # PUT a new rate
        payload = {
            "items": [
                {
                    "provider": "test_provider",
                    "model": "new_model",
                    "input_rate": 5.0,
                    "output_rate": 15.0,
                }
            ]
        }
        put_resp = client.put("/v1/admin/pricing", json=payload, headers=_master_headers())
        self.assertEqual(put_resp.status_code, 200)

        # GET should now reflect the new rate (hot-reload via reload_providers_config)
        get_resp = client.get("/v1/admin/pricing", headers=_master_headers())
        self.assertEqual(get_resp.status_code, 200)
        items = get_resp.json()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["model"], "new_model")
        self.assertAlmostEqual(items[0]["input_rate"], 5.0)


class TestUiPage(_PricingTestBase):
    def setUp(self):
        super().setUp()
        clear_cache()
        self.addCleanup(clear_cache)

    def test_ui_page_loads(self):
        from llm_gateway_core.config.paths import STATIC_DIR

        html_path = STATIC_DIR / "pricing.html"
        self.assertTrue(html_path.exists(), f"pricing.html not found at {html_path}")

        providers_path = _providers_json_no_models(self.tmp_path)
        app, _ = _build_app(providers_path)
        client = TestClient(app)

        resp = client.get("/v1/ui/pricing", headers=_master_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers.get("content-type", ""))
        self.assertIn('<script src="/static/pricing.js"', resp.text)


if __name__ == "__main__":
    unittest.main()
