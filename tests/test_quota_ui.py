"""Tests for the Quota Dashboard UI endpoint.

Uses TestClient for structural checks and Playwright for browser-level smoke tests:
- The HTML page is served correctly
- The page contains expected markup
- Playwright: page loads without JS errors
- Playwright: quota-grid becomes visible after polling
- Playwright: countdown ticks down
"""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import create_autospec, patch

import pytest
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests.runtime_test_support import bind_app_services
from tests.ui_server_helpers import (
    create_authenticated_session,
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)

pytestmark = pytest.mark.browser


if TYPE_CHECKING:
    from playwright.sync_api import Page

_playwright_available = importlib.util.find_spec("playwright.sync_api") is not None
requires_playwright = pytest.mark.skipif(
    not _playwright_available, reason="playwright not installed"
)


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    fallback_events_db = create_autospec(FallbackEventsDB, instance=True, spec_set=True)
    fallback_events_db.get_events_count_last_24h.return_value = {}
    bind_app_services(
        app,
        api_keys_db=db,
        fallback_events_db=fallback_events_db,
        rate_limiter=RateLimiter(),
        usd_budget_ledger=UsdBudgetLedger(),
    )
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
        fusion_rules_path = temp_path / "models_fusion_rules.json"

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
        fusion_rules_path.write_text("[]", encoding="utf-8")

        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = "quota-test-key"
        env["FALLBACK_PROVIDER"] = "openai"
        env["PROVIDERS_FILENAME"] = str(providers_path)
        env["FALLBACK_RULES_FILENAME"] = str(fallback_rules_path)
        env["OPERATION_RULES_FILENAME"] = str(operation_rules_path)
        env["FUSION_RULES_FILENAME"] = str(fusion_rules_path)
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        env["LOG_LEVEL"] = "WARNING"
        base_url = f"http://localhost:{port}"

        with isolated_gateway_process(env=env, temp_path=temp_path) as proc:
            wait_for_gateway(base_url, proc)
            yield base_url


def _add_session(page: "Page", server: str) -> None:
    session = create_authenticated_session(server, "quota-test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])


def _quota_snapshot(*, current: int = 2) -> list[dict]:
    return [
        {
            "key_id": 1,
            "name": "quota-browser-key",
            "disabled": False,
            "rpm": {"current": current, "limit": 10, "reset_in_seconds": 60},
            "tpm": {"current": current * 100, "limit": 1000, "reset_in_seconds": 60},
            "budget": {"spent_usd": 0.1, "limit_usd": 1.0},
            "fallback_events_24h": 1,
        }
    ]


def _install_quota_fetch_harness(
    page: "Page", responses: list[dict], *, hold_auth: bool = False
) -> None:
    script = """
        (() => {
            const responses = __RESPONSES__;
            const holdAuth = __HOLD_AUTH__;
            const nativeFetch = window.fetch.bind(window);
            const planned = responses.slice();
            let authRequestCount = 0;
            let requestCount = 0;
            let upstreamRequestCount = 0;
            let release = null;
            let releaseAuth = null;

            function makeResponse(plannedResponse) {
                return new Response(JSON.stringify(plannedResponse.body), {
                    status: plannedResponse.status,
                    headers: {'Content-Type': 'application/json'},
                });
            }

            window.__quotaHarness = {
                authRequestCount: () => authRequestCount,
                requestCount: () => requestCount,
                upstreamRequestCount: () => upstreamRequestCount,
                release: () => {
                    if (!release) throw new Error('No held quota response');
                    const pending = release;
                    release = null;
                    pending();
                },
                releaseAuth: () => {
                    if (!releaseAuth) throw new Error('No held auth response');
                    const pending = releaseAuth;
                    releaseAuth = null;
                    pending();
                },
            };

            window.fetch = (input, options) => {
                const url = new URL(typeof input === 'string' ? input : input.url, location.href);
                if (url.pathname === '/auth/me' && holdAuth) {
                    authRequestCount += 1;
                    return new Promise((resolve) => {
                        releaseAuth = () => resolve(makeResponse({
                            status: 200,
                            body: {role: 'master', key_id: null},
                        }));
                    });
                }
                if (url.pathname === '/v1/api/quota/keys') {
                    requestCount += 1;
                    const plannedResponse = planned.shift();
                    if (!plannedResponse) {
                        return Promise.reject(new Error('Unexpected quota request'));
                    }
                    if (plannedResponse.hold) {
                        return new Promise((resolve) => {
                            release = () => resolve(makeResponse(plannedResponse));
                        });
                    }
                    return Promise.resolve(makeResponse(plannedResponse));
                }
                if (url.pathname === '/v1/admin/upstream-quotas') {
                    upstreamRequestCount += 1;
                    return Promise.resolve(makeResponse({status: 200, body: {snapshots: []}}));
                }
                return nativeFetch(input, options);
            };
        })();
        """.replace("__RESPONSES__", json.dumps(responses)).replace(
            "__HOLD_AUTH__", json.dumps(hold_auth)
        )
    page.add_init_script(script)


def _quota_request_count(page: "Page") -> int:
    return page.evaluate("() => window.__quotaHarness.requestCount()")


@requires_playwright
def test_quota_page_loads_without_js_errors(page: "Page", quota_server: str) -> None:
    """Virtual-key quota UI skips the master-only upstream request."""
    from playwright.sync_api import expect

    js_errors: list[str] = []
    console_errors: list[str] = []
    upstream_requests: list[str] = []
    page.on("pageerror", lambda exc: js_errors.append(str(exc)))
    page.on(
        "console",
        lambda message: console_errors.append(message.text)
        if message.type == "error"
        else None,
    )
    page.on(
        "request",
        lambda request: upstream_requests.append(request.url)
        if request.url.endswith("/v1/admin/upstream-quotas")
        else None,
    )

    master_login = page.context.request.post(
        f"{quota_server}/auth/login",
        data={"api_key": "quota-test-key", "next": "/v1/ui/quota"},
    )
    assert master_login.status == 200, master_login.text()
    created = page.context.request.post(
        f"{quota_server}/v1/admin/api-keys",
        data={"name": "quota-browser-virtual"},
    )
    assert created.status == 201, created.text()
    virtual_login = page.context.request.post(
        f"{quota_server}/auth/login",
        data={"api_key": created.json()["api_key"], "next": "/v1/ui/quota"},
    )
    assert virtual_login.status == 200, virtual_login.text()
    page.goto(f"{quota_server}/v1/ui/quota")

    expect(page.locator("#quota-grid")).to_be_attached()
    page.wait_for_function("window.gatewayAuth.state === 'user'")

    assert js_errors == [], f"Unexpected JS errors: {js_errors}"
    assert console_errors == [], f"Unexpected console errors: {console_errors}"
    assert upstream_requests == []


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


@requires_playwright
def test_quota_delayed_ready_then_empty_is_responsive(
    page: "Page", quota_server: str, browser
) -> None:
    from playwright.sync_api import expect

    page.clock.install()
    _install_quota_fetch_harness(
        page,
        [
            {"status": 200, "body": _quota_snapshot(), "hold": True},
            {"status": 200, "body": []},
        ],
    )
    _add_session(page, quota_server)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{quota_server}/v1/ui/quota", wait_until="domcontentloaded")

    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "loading")
    page.evaluate("() => window.__quotaHarness.release()")
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "ready")
    expect(page.locator(".quota-card")).to_have_count(1)

    page.set_viewport_size({"width": 1440, "height": 900})
    page.clock.run_for(5_000)
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "empty")
    expect(page.locator(".quota-empty")).to_contain_text("No API keys found")
    assert _quota_request_count(page) == 2

    teardown_context = browser.new_context()
    teardown_page = teardown_context.new_page()
    try:
        _install_quota_fetch_harness(teardown_page, [], hold_auth=True)
        _add_session(teardown_page, quota_server)
        teardown_page.goto(f"{quota_server}/v1/ui/quota", wait_until="domcontentloaded")
        teardown_page.wait_for_function(
            "() => window.__quotaHarness.authRequestCount() === 1"
        )
        teardown_page.evaluate("() => window.dispatchEvent(new Event('beforeunload'))")
        teardown_page.evaluate("() => window.__quotaHarness.releaseAuth()")
        teardown_page.wait_for_function("() => window.gatewayAuth.state === 'master'")
        expect(teardown_page.locator("#quota-status")).to_be_empty()
        assert _quota_request_count(teardown_page) == 0
        assert teardown_page.evaluate(
            "() => window.__quotaHarness.upstreamRequestCount()"
        ) == 0
    finally:
        teardown_context.close()


@requires_playwright
def test_quota_initial_error_requires_localized_retry(page: "Page", quota_server: str) -> None:
    from playwright.sync_api import expect

    page.clock.install()
    _install_quota_fetch_harness(
        page,
        [
            {"status": 500, "body": {"detail": "failed"}},
            {"status": 200, "body": _quota_snapshot()},
        ],
    )
    _add_session(page, quota_server)
    page.goto(f"{quota_server}/v1/ui/quota")

    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "initial-error")
    expect(page.locator("#quota-retry")).to_be_visible()
    page.clock.run_for(15_000)
    assert _quota_request_count(page) == 1

    page.locator("#localeSelect").select_option("ru")
    expect(page.locator("#quota-retry")).to_have_text("Повторить")
    assert _quota_request_count(page) == 1
    page.locator("#quota-retry").click()
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "ready")
    expect(page.locator(".quota-card")).to_have_count(1)
    expect(page.locator(".quota-empty")).to_have_count(0)
    assert _quota_request_count(page) == 2


@requires_playwright
def test_quota_stale_recovery_and_locale_change_do_not_overlap_requests(
    page: "Page", quota_server: str
) -> None:
    from playwright.sync_api import expect

    page.clock.install()
    _install_quota_fetch_harness(
        page,
        [
            {"status": 200, "body": _quota_snapshot(current=2)},
            {"status": 500, "body": {"detail": "failed"}, "hold": True},
            {"status": 200, "body": _quota_snapshot(current=4)},
        ],
    )
    _add_session(page, quota_server)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{quota_server}/v1/ui/quota")
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "ready")
    expect(page.locator(".quota-metric-count").first).to_have_text("2")
    expect(page.locator(".countdown-badge").first).to_have_text("01:00")
    page.clock.run_for(2_000)
    expect(page.locator(".countdown-badge").first).to_have_text("00:58")

    page.locator("[data-gateway-nav-toggle]").click()
    page.locator("#localeSelect").select_option("ru")
    expect(page.locator("#quota-status")).to_contain_text("Обновлено")
    expect(page.locator(".countdown-badge").first).to_have_text("00:58")
    assert _quota_request_count(page) == 1
    page.set_viewport_size({"width": 1440, "height": 900})
    page.locator("#localeSelect").select_option("en")
    expect(page.locator("#quota-status")).to_contain_text("Updated")
    assert _quota_request_count(page) == 1
    page.locator(".quota-metric-count").first.evaluate(
        "element => { element.dataset.staleIdentity = 'preserved'; }"
    )

    page.clock.run_for(5_000)
    assert _quota_request_count(page) == 2
    page.clock.run_for(15_000)
    assert _quota_request_count(page) == 2
    page.evaluate("() => window.__quotaHarness.release()")
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "stale")
    expect(page.locator(".quota-metric-count").first).to_have_text("2")
    expect(page.locator(".quota-metric-count").first).to_have_attribute(
        "data-stale-identity", "preserved"
    )

    page.clock.run_for(5_000)
    expect(page.locator("#quota-grid")).to_have_attribute("data-quota-state", "ready")
    expect(page.locator(".quota-metric-count").first).to_have_text("4")
    assert _quota_request_count(page) == 3
