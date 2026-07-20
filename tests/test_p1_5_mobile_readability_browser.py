"""Browser-level coverage for P1-5: readable tables + mobile detail views +
touch targets, for the Rejections and Pricing admin pages.

Verifies, at a 390x844 mobile viewport:
  * the Rejections/Pricing tables no longer force horizontal scrolling
    (only the highest-priority columns stay visible);
  * clicking a row (Rejections) / the expand button (Pricing) opens a
    bottom-sheet detail view exposing the hidden fields;
  * the controls that remain on screen meet the 44px touch-target minimum.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "p1-5-mobile-readability-master"


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"chat": {"input_rate": 1.0, "output_rate": 2.0}},
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    for filename, content in {
        "models_fallback_rules.json": "[]\n",
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def mobile_server(tmp_path: Path) -> Iterator[str]:
    providers_path = _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
            "FALLBACK_RULES_FILENAME": str(tmp_path / "models_fallback_rules.json"),
            "MODEL_RULES_FILENAME": str(tmp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(tmp_path / "models_operation_rules.json"),
            "FUSION_RULES_FILENAME": str(tmp_path / "models_fusion_rules.json"),
            "ROUTER_RULES_FILENAME": str(tmp_path / "models_router_rules.json"),
            "SESSION_COOKIE_SECURE": "false",
            "VERIFY_MODELS_ON_STARTUP": "off",
            "LOG_LEVEL": "WARNING",
        }
    )
    base_url = f"http://127.0.0.1:{port}"
    with isolated_gateway_process(env=env, temp_path=tmp_path) as process:
        wait_for_gateway(base_url, process)
        yield base_url


def _login(browser: Browser, base_url: str, next_path: str) -> BrowserContext:
    context = browser.new_context(viewport={"width": 390, "height": 844})
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": next_path},
    )
    assert response.status == 200
    return context


@pytest.mark.browser
def test_rejections_mobile_shows_priority_columns_and_detail_sheet(
    browser: Browser,
    mobile_server: str,
) -> None:
    context = _login(browser, mobile_server, "/v1/ui/rejections")
    try:
        page = context.new_page()
        page.route(
            "**/v1/admin/rejections*",
            lambda route: route.fulfill(
                json={
                    "items": [
                        {
                            "timestamp": "2026-07-15T08:30:00+00:00",
                            "category": "rate_limited",
                            "status_code": 429,
                            "method": "POST",
                            "path": "/v1/raw/abc",
                            "api_key_id": 7,
                            "x_title": "Raw Client",
                            "client_ip": "192.0.2.15",
                            "reason": "RAW_REASON_abc",
                            "request_id": "req-abc",
                        }
                    ],
                    "total": 1,
                }
            ),
        )
        page.goto(f"{mobile_server}/v1/ui/rejections", wait_until="networkidle")
        expect(page.locator("#rejTableBody tr")).to_have_count(1)

        # No nested horizontal scroller at 390px.
        overflow = page.locator(".rej-table-wrap").evaluate(
            "(el) => el.scrollWidth - el.clientWidth"
        )
        assert overflow <= 1

        # Only the highest-priority columns are visible in the table.
        visible = page.locator("thead th").evaluate_all(
            "(ths) => ths.map((th) => th.offsetParent !== null)"
        )
        assert visible == [True, True, True, False, False, False, False, False, False]

        # Row click opens the detail sheet exposing the hidden fields.
        page.locator("#rejTableBody tr").first.click()
        sheet = page.locator("#rejDetailSheet")
        expect(sheet).to_be_visible()
        expect(sheet).to_contain_text("/v1/raw/abc")
        expect(sheet).to_contain_text("RAW_REASON_abc")

        close_box = page.locator("#rejDetailClose").bounding_box()
        assert close_box is not None
        assert close_box["height"] >= 44
        assert close_box["width"] >= 44

        page.keyboard.press("Escape")
        expect(sheet).to_be_hidden()

        apply_box = page.locator("#applyFiltersBtn").bounding_box()
        assert apply_box is not None
        assert apply_box["height"] >= 44
    finally:
        context.close()


@pytest.mark.browser
def test_pricing_mobile_shows_priority_columns_and_detail_sheet(
    browser: Browser,
    mobile_server: str,
) -> None:
    context = _login(browser, mobile_server, "/v1/ui/pricing")
    try:
        page = context.new_page()
        page.goto(f"{mobile_server}/v1/ui/pricing")
        expect(page.locator("#pricingTableBody tr")).to_have_count(1)

        overflow = page.locator(".pricing-table-wrap").evaluate(
            "(el) => el.scrollWidth - el.clientWidth"
        )
        assert overflow <= 1

        visible = page.locator("#pricingTable thead th").evaluate_all(
            "(ths) => ths.map((th) => th.offsetParent !== null)"
        )
        # Provider, Model, (blank actions header) visible; Input/Output rate hidden.
        assert visible == [True, True, False, False, True]

        expand_box = page.locator("#pricingTableBody tr .btn-expand").bounding_box()
        assert expand_box is not None
        assert expand_box["height"] >= 44
        assert expand_box["width"] >= 44

        page.locator("#pricingTableBody tr .btn-expand").click()
        sheet = page.locator("#pricingDetailSheet")
        expect(sheet).to_be_visible()
        expect(page.locator("#pricingDetailProvider")).to_have_text("primary")
        expect(page.locator("#pricingDetailModel")).to_have_text("chat")
        expect(page.locator("#pricingDetailInputRate")).to_have_value("1")

        page.locator("#pricingDetailInputRate").fill("5")
        page.locator("#pricingDetailClose").click()
        expect(sheet).to_be_hidden()

        rate_input = page.locator("#pricingTableBody tr input").nth(2)
        expect(rate_input).to_have_value("5")
        expect(page.locator("#pricingTableBody tr")).to_have_class("dirty")
    finally:
        context.close()
