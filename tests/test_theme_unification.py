"""Tests for unified theme system (theme.js / theme.css).

Checks:
- Main UI pages include theme.js and theme.css.
- Main UI pages declare the shared SVG favicon.
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
from llm_gateway_core.api.v1.admin_pricing import admin_pricing_router
from llm_gateway_core.api.v1.admin_rejections import admin_rejections_router
from llm_gateway_core.api.v1.admin_translator_debug import translator_debug_router
from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.api.v1.stats import stats_router
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from tests.runtime_test_support import bind_app_services


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, api_keys_db=db)
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(editor_router, prefix="/v1")
    app.include_router(stats_router, prefix="/v1")
    app.include_router(quota_router, prefix="/v1")
    app.include_router(admin_api_keys_router, prefix="/v1")
    app.include_router(admin_pricing_router, prefix="/v1")
    app.include_router(admin_rejections_router, prefix="/v1")
    app.include_router(translator_debug_router, prefix="/v1")
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
        self.client = self.enterContext(TestClient(self.app))

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

    def test_favicon_is_served(self):
        """Shared SVG favicon is served with status 200."""
        resp = self.client.get("/static/favicon.svg")
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
        self.assertIn(
            '<link rel="icon" type="image/svg+xml" href="/static/favicon.svg">',
            resp.text,
            f"{description}: missing favicon link tag",
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

    def test_pricing_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/pricing", "pricing")

    def test_rejections_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/rejections", "rejections")

    def test_translator_debug_includes_theme_files(self):
        self._assert_page_has_theme("/v1/ui/translator-debug", "translator-debug")

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

    def test_pricing_js_no_legacy_theme(self):
        """pricing.js uses the unified Theme manager, not local dark-mode storage."""
        content = (STATIC_DIR / "pricing.js").read_text(encoding="utf-8")
        self.assertIn("Theme.attachToggle", content)
        self.assertNotIn("localStorage.setItem('darkMode'", content)

    def test_web_playground_html_no_legacy_theme_inline_script(self):
        """web-playground.html no longer has the legacy inline theme script."""
        content = (STATIC_DIR / "web-playground.html").read_text(encoding="utf-8")
        # The old inline script set localStorage.setItem("darkMode", ...)
        self.assertNotIn('localStorage.setItem("darkMode"', content)

    def test_admin_pages_use_external_redesign_css(self):
        """Admin table pages use external page CSS instead of inline stylesheets."""
        api_keys_html = (STATIC_DIR / "api-keys.html").read_text(encoding="utf-8")
        rejections_html = (STATIC_DIR / "rejections.html").read_text(encoding="utf-8")
        self.assertIn('/static/api-keys.css', api_keys_html)
        self.assertIn('/static/rejections.css', rejections_html)
        self.assertNotIn("<style>", api_keys_html)
        self.assertNotIn("<style>", rejections_html)

    def test_responsive_scaffold_keeps_overflow_inside_controls(self):
        """Shared page CSS keeps wide navigation/tables scrollable inside their containers."""
        usage_css = (STATIC_DIR / "usage-stats.css").read_text(encoding="utf-8")
        editor_css = (STATIC_DIR / "editor.css").read_text(encoding="utf-8")
        playground_css = (STATIC_DIR / "web-playground.css").read_text(encoding="utf-8")
        api_keys_css = (STATIC_DIR / "api-keys.css").read_text(encoding="utf-8")
        rejections_css = (STATIC_DIR / "rejections.css").read_text(encoding="utf-8")

        self.assertIn("overflow-x: clip", usage_css)
        self.assertIn("overflow-x: auto", usage_css)
        self.assertIn("#recordsArea {", usage_css)
        self.assertIn("max-height: clamp(320px, 62dvh, 720px)", usage_css)
        self.assertIn("#recordsArea table", usage_css)
        self.assertIn("position: sticky", usage_css)
        self.assertIn("@media (max-width: 600px)", usage_css)
        self.assertIn("overflow-x: clip", editor_css)
        self.assertIn("overflow-x: auto", editor_css)
        self.assertIn(".fallback-list", editor_css)
        self.assertIn("min-width: 680px", editor_css)
        self.assertIn("@media (max-width: 640px)", editor_css)
        self.assertIn(".playground-panel", playground_css)
        self.assertIn("flex-wrap: nowrap", playground_css)
        self.assertIn("min-width: 0", playground_css)
        self.assertNotIn("min-width: 620px", playground_css)
        self.assertIn(".keys-table-wrap", api_keys_css)
        self.assertIn("overflow-x: auto", api_keys_css)
        self.assertIn(".rej-table-wrap", rejections_css)
        self.assertIn("overflow-x: auto", rejections_css)

    def test_theme_uses_indigo_accent_palette(self):
        """Primary and paired accent tokens use the indigo palette."""
        content = (STATIC_DIR / "theme.css").read_text(encoding="utf-8")
        self.assertIn("--accent: #4f46e5;", content)
        self.assertIn("--accent-hover: #4338ca;", content)
        self.assertIn("--accent-soft: rgba(79, 70, 229, 0.11);", content)
        self.assertIn("--accent-text: #4338ca;", content)
        self.assertIn("--accent: #818cf8;", content)
        self.assertIn("--accent-hover: #a5b4fc;", content)
        self.assertIn("--accent-soft: rgba(129, 140, 248, 0.16);", content)
        self.assertIn("--accent-text: #a5b4fc;", content)

    def test_first_party_static_assets_do_not_reference_legacy_teal_accent(self):
        """First-party root static assets no longer hard-code the previous teal accent."""
        legacy_patterns = (
            "deep teal",
            "#0d9488",
            "#0f766e",
            "#2dd4bf",
            "#5eead4",
            "13, 148, 136",
            "45, 212, 191",
        )
        checked_suffixes = {".css", ".html", ".js", ".svg"}
        for path in STATIC_DIR.iterdir():
            if path.suffix not in checked_suffixes:
                continue
            content = path.read_text(encoding="utf-8").lower()
            for pattern in legacy_patterns:
                self.assertNotIn(pattern, content, f"{path.name} still references {pattern}")

    def test_translator_debug_css_uses_shared_scaffold(self):
        """The translator page loads the shared theme before its page styles."""
        content = (STATIC_DIR / "translator-debug.css").read_text(encoding="utf-8")
        html = (STATIC_DIR / "translator-debug.html").read_text(encoding="utf-8")
        self.assertLess(
            html.index('href="/static/theme.css"'),
            html.index('href="/static/translator-debug.css"'),
        )
        self.assertIn("width: min(var(--page-max-width)", content)
        self.assertIn("background: var(--bg-elevated)", content)
        self.assertIn("overflow-x: auto", content)

    # ── theme.js migration and API details ─────────────────────────────────

    def test_theme_js_legacy_key_migration(self):
        """theme.js contains migration logic for legacy localStorage keys."""
        content = (STATIC_DIR / "theme.js").read_text(encoding="utf-8")
        self.assertIn("llmgateway:theme", content)
        # Migration reads both legacy key names
        self.assertIn("darkMode", content)
        self.assertIn("true", content)
        self.assertIn("false", content)
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
