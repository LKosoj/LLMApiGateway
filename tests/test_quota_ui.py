"""Tests for the Quota Dashboard UI endpoint.

Uses TestClient for structural checks and Playwright for browser-level smoke tests:
- The HTML page is served correctly
- The page contains expected markup
- Playwright: page loads without JS errors
- Playwright: quota-grid becomes visible after polling
- Playwright: countdown ticks down
"""
from __future__ import annotations

import hashlib
import hmac
import importlib.util
import os
import secrets
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests.ui_server_helpers import get_free_port, wait_for_gateway

if TYPE_CHECKING:
    from playwright.sync_api import Page

_playwright_available = importlib.util.find_spec("playwright.sync_api") is not None
requires_playwright = pytest.mark.skipif(
    not _playwright_available, reason="playwright not installed"
)


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    app.state.api_keys_db = db
    app.state.rate_limiter = RateLimiter()
    app.state.usd_budget_ledger = UsdBudgetLedger()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(quota_router, prefix="/v1")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


class QuotaUiTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_quota_ui.db")
        self.master_key = "master-quota-ui-test"

        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", self.master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        self.app = _build_app(self.db)
        self.client = TestClient(self.app)

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def test_quota_page_returns_200(self):
        """The /v1/ui/quota endpoint serves the HTML page."""
        resp = self.client.get("/v1/ui/quota", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])

    def test_quota_page_contains_quota_grid(self):
        """Quota HTML contains the #quota-grid anchor."""
        resp = self.client.get("/v1/ui/quota", headers=self._master_headers())
        self.assertIn("quota-grid", resp.text)

    def test_quota_page_loads_quota_js(self):
        """Quota HTML references quota.js."""
        resp = self.client.get("/v1/ui/quota", headers=self._master_headers())
        self.assertIn("quota.js", resp.text)

    def test_quota_page_loads_quota_css(self):
        """Quota HTML references quota.css."""
        resp = self.client.get("/v1/ui/quota", headers=self._master_headers())
        self.assertIn("quota.css", resp.text)

    def test_quota_page_contains_shared_auth(self):
        """Quota HTML loads shared-auth.js for role-based UI."""
        resp = self.client.get("/v1/ui/quota", headers=self._master_headers())
        self.assertIn("shared-auth.js", resp.text)

    def test_quota_css_is_served(self):
        """quota.css static file is reachable."""
        resp = self.client.get("/static/quota.css")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("quota-card", resp.text)

    def test_quota_js_is_served(self):
        """quota.js static file is reachable."""
        resp = self.client.get("/static/quota.js")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("fetchQuotaData", resp.text)

    def test_quota_api_returns_json_array(self):
        """The /v1/api/quota/keys endpoint returns a JSON array."""
        self.db.create(name="test-key")
        resp = self.client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertIsInstance(resp.json(), list)

    def test_progress_bars_rendered_client_side(self):
        """The progress-bar CSS classes are present in the stylesheet."""
        resp = self.client.get("/static/quota.css")
        self.assertIn("progress-bar-track", resp.text)
        self.assertIn("progress-bar-fill", resp.text)
        self.assertIn("color-ok", resp.text)
        self.assertIn("color-warn", resp.text)
        self.assertIn("color-danger", resp.text)


if __name__ == "__main__":
    unittest.main()


# ── Playwright browser-level smoke tests ──────────────────────────────────────

def _build_session_signature(issued_at: int, expires_at: int, nonce: str, gateway_api_key: str) -> str:
    secret = gateway_api_key.encode("utf-8")
    payload = f"{issued_at}.{expires_at}.{nonce}.master.".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def _create_authenticated_session(gateway_api_key: str) -> str:
    issued_at = int(time.time())
    expires_at = issued_at + 365 * 24 * 60 * 60
    nonce = secrets.token_urlsafe(24)
    signature = _build_session_signature(issued_at, expires_at, nonce, gateway_api_key)
    return f"{issued_at}.{expires_at}.{nonce}.master..{signature}"


@pytest.fixture(scope="module")
def quota_server():
    """Start a real gateway process for Playwright smoke tests."""
    if not _playwright_available:
        pytest.skip("playwright not installed")
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"

        providers_path.write_text(
            '[{"openai": {"baseUrl": "http://api.openai.com", "apikey": "key"}}]',
            encoding="utf-8",
        )
        fallback_rules_path.write_text(
            '[{"gateway_model_name": "llmgateway/light_model", '
            '"fallback_models": [{"provider": "openai", "model": "gpt-4o-mini"}]}]',
            encoding="utf-8",
        )
        operation_rules_path.write_text(
            '{"embeddings": [], "rerank": [], "images_generations": [], '
            '"images_edits": [], "audio_speech": [], "audio_transcriptions": [], '
            '"pdf_conversions": [], "web_search": [], "web_read": [], '
            '"web_research": [], "web_deep_research": []}',
            encoding="utf-8",
        )

        db_dir = temp_path / "db"
        db_dir.mkdir(parents=True, exist_ok=True)

        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = "quota-test-key"
        env["FALLBACK_PROVIDER"] = "openai"
        env["PROVIDERS_FILENAME"] = str(providers_path)
        env["FALLBACK_RULES_FILENAME"] = str(fallback_rules_path)
        env["OPERATION_RULES_FILENAME"] = str(operation_rules_path)
        env["GATEWAY_DB_DIR"] = str(db_dir)
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        env["LOG_LEVEL"] = "WARNING"
        base_url = f"http://localhost:{port}"

        proc = subprocess.Popen(
            ["./.venv/bin/python", "main.py"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        wait_for_gateway(base_url, proc)
        yield base_url
        proc.terminate()
        proc.wait()


def _add_session(page: "Page", server: str) -> None:
    session = _create_authenticated_session("quota-test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])


@requires_playwright
def test_quota_page_loads_without_js_errors(page: "Page", quota_server: str) -> None:
    """Quota dashboard page loads with no uncaught JS exceptions."""
    from playwright.sync_api import expect

    js_errors: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))

    _add_session(page, quota_server)
    page.goto(f"{quota_server}/v1/ui/quota")

    expect(page.locator("#quota-grid")).to_be_attached()

    assert js_errors == [], f"Unexpected JS errors: {js_errors}"


@requires_playwright
def test_quota_grid_visible_after_load(page: "Page", quota_server: str) -> None:
    """Quota dashboard renders #quota-grid container visible on the page."""
    from playwright.sync_api import expect

    _add_session(page, quota_server)
    page.goto(f"{quota_server}/v1/ui/quota")

    quota_grid = page.locator("#quota-grid")
    expect(quota_grid).to_be_visible()


@requires_playwright
def test_quota_countdown_ticks(page: "Page", quota_server: str) -> None:
    """After a key is created, the polling fetches cards with countdown badges."""
    import requests as _requests
    from playwright.sync_api import expect

    _requests.post(
        f"{quota_server}/v1/admin/api-keys",
        json={"name": "pw-test-key", "rpm": 60},
        headers={"Authorization": "Bearer quota-test-key"},
    )

    _add_session(page, quota_server)
    page.goto(f"{quota_server}/v1/ui/quota")

    card_locator = page.locator(".quota-card").first
    expect(card_locator).to_be_visible(timeout=10_000)

    countdown = card_locator.locator(".countdown-badge").first
    expect(countdown).to_be_visible()
    initial_text = countdown.text_content()
    assert initial_text is not None and ":" in initial_text, (
        f"Expected mm:ss countdown, got: {initial_text!r}"
    )
