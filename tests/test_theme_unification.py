"""Tests for unified theme system (theme.js / theme.css).

Checks:
- All 6 main UI pages include theme.js and theme.css.
- theme.js and theme.css are served correctly and contain the required API.
- Legacy inline theme functions are removed from JS files.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_api_keys import admin_api_keys_router
from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.api.v1.stats import stats_router
from llm_gateway_core.config.paths import STATIC_DIR
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
    app.include_router(editor_router, prefix="/v1")
    app.include_router(stats_router, prefix="/v1")
    app.include_router(quota_router, prefix="/v1")
    app.include_router(admin_api_keys_router, prefix="/v1")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


class ThemeUnificationTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_theme_unification.db")
        self.master_key = "master-theme-test"

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

    # ── Static asset existence ─────────────────────────────────────────────

    def test_theme_js_is_served(self):
        """theme.js is served with status 200."""
        resp = self.client.get("/static/theme.js")
        self.assertEqual(resp.status_code, 200)

    def test_theme_js_contains_required_api(self):
        """theme.js exposes Theme.init, Theme.set, and Theme.attachToggle."""
        resp = self.client.get("/static/theme.js")
        self.assertIn("Theme.init", resp.text)
        self.assertIn("Theme.set", resp.text)
        self.assertIn("Theme.attachToggle", resp.text)

    def test_theme_css_is_served(self):
        """theme.css is served with status 200."""
        resp = self.client.get("/static/theme.css")
        self.assertEqual(resp.status_code, 200)

    def test_theme_css_contains_dark_mode_selector(self):
        """theme.css defines body.dark-mode block."""
        resp = self.client.get("/static/theme.css")
        self.assertIn("body.dark-mode {", resp.text)

    # ── HTML pages include unified theme files ─────────────────────────────

    def _assert_page_has_theme(self, url: str, description: str) -> None:
        resp = self.client.get(url, headers=self._master_headers())
        self.assertEqual(resp.status_code, 200, f"{description}: expected 200")
        self.assertIn(
            '<script src="/static/theme.js"',
            resp.text,
            f"{description}: missing theme.js script tag",
        )
        self.assertIn(
            '<link rel="stylesheet" href="/static/theme.css">',
            resp.text,
            f"{description}: missing theme.css link tag",
        )

    def test_rules_editor_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/rules-editor", "rules-editor")

    def test_usage_stats_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/usage-stats", "usage-stats")

    def test_api_keys_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/api-keys", "api-keys")

    def test_gateway_docs_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/docs", "gateway-docs")

    def test_web_playground_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/playground", "web-playground")

    def test_quota_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/quota", "quota")

    # ── Legacy theme functions removed from JS files ───────────────────────

    def test_editor_js_no_legacy_theme(self):
        """editor.js no longer contains the old setDarkMode function."""
        content = (STATIC_DIR / "editor.js").read_text(encoding="utf-8")
        self.assertNotIn("function setDarkMode", content)
        self.assertNotIn("localStorage.setItem('darkMode'", content)

    def test_usage_stats_js_no_legacy_theme(self):
        """usage-stats.js no longer contains the old applyTheme function."""
        content = (STATIC_DIR / "usage-stats.js").read_text(encoding="utf-8")
        self.assertNotIn("const applyTheme", content)
        self.assertNotIn("localStorage.setItem('theme'", content)

    def test_api_keys_js_no_legacy_theme(self):
        """api-keys.js no longer contains the old applyDarkMode function."""
        content = (STATIC_DIR / "api-keys.js").read_text(encoding="utf-8")
        self.assertNotIn("function applyDarkMode", content)

    def test_gateway_docs_js_no_legacy_theme(self):
        """gateway-docs.js no longer contains the old setupThemeToggle function."""
        content = (STATIC_DIR / "gateway-docs.js").read_text(encoding="utf-8")
        self.assertNotIn("function setupThemeToggle", content)

    def test_quota_js_no_legacy_theme(self):
        """quota.js no longer contains the old applyTheme function."""
        content = (STATIC_DIR / "quota.js").read_text(encoding="utf-8")
        self.assertNotIn("const applyTheme", content)
        self.assertNotIn("localStorage.setItem('theme'", content)

    def test_web_playground_html_no_legacy_theme_inline_script(self):
        """web-playground.html no longer has the legacy inline theme script."""
        content = (STATIC_DIR / "web-playground.html").read_text(encoding="utf-8")
        # The old inline script set localStorage.setItem("darkMode", ...)
        self.assertNotIn('localStorage.setItem("darkMode"', content)

    # ── theme.js migration and API details ─────────────────────────────────

    def test_theme_js_legacy_key_migration(self):
        """theme.js contains migration logic for legacy localStorage keys."""
        content = (STATIC_DIR / "theme.js").read_text(encoding="utf-8")
        self.assertIn("llmgateway:theme", content)
        # Migration reads both legacy key names
        self.assertIn("darkMode", content)
        self.assertIn("theme", content)

    def test_theme_js_system_mode(self):
        """theme.js supports 'system' mode with matchMedia listener."""
        content = (STATIC_DIR / "theme.js").read_text(encoding="utf-8")
        self.assertIn("system", content)
        self.assertIn("matchMedia", content)
        self.assertIn("prefers-color-scheme", content)

    def test_theme_css_defines_unified_variables(self):
        """theme.css defines the required unified CSS custom properties."""
        content = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")
        for var in ("--bg", "--bg-elevated", "--text", "--border", "--accent"):
            self.assertIn(var, content, f"theme.css missing variable {var}")


if __name__ == "__main__":
    unittest.main()
