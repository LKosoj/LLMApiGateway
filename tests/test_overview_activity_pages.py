"""Focused browser tests for the P1-2 Overview and Activity pages.

These cover behavior specific to the new pages (KPI/empty-state rendering,
role scoping of the request journal, the disclosed no-data-source state for
5xx, and the placeholder detail drawer). Route-level auth/reachability is
already covered by the generic authenticated-page tests; this file exercises
the pages' own JS rendering logic against mocked API responses.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.test_r5_2_ui_acceptance_browser import (
    MASTER_KEY,
    _create_virtual_key,
    _login,
    r5_2_server,
)

__all__ = ["r5_2_server"]
pytestmark = pytest.mark.browser


def _route_json(page: Page, url_pattern: str, payload: dict) -> None:
    page.route(url_pattern, lambda route: route.fulfill(json=payload))


def _empty_dashboard() -> dict:
    return {
        "filters": {"bucket": "day"},
        "totals": {"requests": 0},
        "series": {"usage": []},
        "breakdowns": {"providers": [], "resolved_targets": [], "api_keys": []},
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }


def _populated_dashboard() -> dict:
    return {
        "filters": {"bucket": "day"},
        "totals": {
            "requests": 42,
            "cost": 1.2345,
            "duration_p95_ms": 850,
            "active_requests": 2,
            "fallback_errors": 1,
            "rejections": 3,
        },
        "series": {
            "usage": [
                {"time_period": "2026-07-18", "requests": 20, "cost": 0.5},
                {"time_period": "2026-07-19", "requests": 22, "cost": 0.73},
            ]
        },
        "breakdowns": {
            "providers": [],
            "resolved_targets": [
                {"label": "llmgateway/light_model", "requests": 30, "cost": 0.9},
            ],
            "api_keys": [],
        },
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }


def test_overview_shows_empty_state_with_reason_when_no_data(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _empty_dashboard())
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator("#overviewEmpty")).to_be_visible()
    expect(page.locator("#overviewEmpty")).to_contain_text("No usage was recorded")
    expect(page.locator("#overviewContent")).to_be_hidden()


def test_overview_renders_kpis_trend_and_top_models_from_dashboard_payload(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _populated_dashboard())
    page.route(
        f"{r5_2_server}/health",
        lambda route: route.fulfill(json={"status": "ok", "phase": "ready", "version": "9.9.9"}),
    )

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator("#overviewContent")).to_be_visible()
    expect(page.locator("#overviewEmpty")).to_be_hidden()
    expect(page.locator("#kpiRequests")).to_have_text("42")
    expect(page.locator("#kpiRejections")).to_have_text("3")
    expect(page.locator("#topModelsBody")).to_contain_text("llmgateway/light_model")
    expect(page.locator("#healthStatus")).to_contain_text("OK")
    expect(page.locator("#healthVersion")).to_contain_text("9.9.9")


def test_overview_health_banner_shows_degraded_status_from_a_failed_health_report(page: Page, r5_2_server: str):
    # The real /health endpoint always returns a well-formed body (status/phase/
    # version/checks) even when unhealthy - it replies 503 with report.ok == False,
    # not a missing/malformed body. So a 503 with a valid JSON payload is rendered
    # as a disclosed "degraded" status, not the generic "unavailable" fallback.
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _empty_dashboard())
    page.route(
        f"{r5_2_server}/health",
        lambda route: route.fulfill(
            status=503, json={"status": "error", "phase": "failed", "version": "9.9.9"}
        ),
    )

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator("#healthStatus")).to_contain_text("Degraded")
    expect(page.locator("#healthStatus")).to_contain_text("Failed")


def test_overview_health_banner_shows_unavailable_when_health_endpoint_is_unreachable(
    page: Page, r5_2_server: str
):
    # True unreachability (network failure, no response at all) is the scenario the
    # "unavailable" fallback is meant for - unlike a 503 with a valid body.
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _empty_dashboard())
    page.route(f"{r5_2_server}/health", lambda route: route.abort())

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator("#healthStatus")).to_contain_text("unavailable")


def _route_single_usage_record(page: Page, r5_2_server: str, record: dict) -> None:
    page.route(
        f"{r5_2_server}/v1/api/usage-records*",
        lambda route: route.fulfill(json={"records": [record], "total_records": 1}),
    )


def test_activity_opens_request_drawer_with_details_and_closes_on_escape(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_single_usage_record(
        page,
        r5_2_server,
        {
            "request_id": "req-abc-123",
            "timestamp": "2026-07-19T10:00:00Z",
            "api_key_id": 7,
            "operation": "chat",
            "gateway_model": "llmgateway/light_model",
            "model": "gpt-4o-mini",
            "provider": "openai",
            "status": "completed",
            "duration_ms": 500,
            "cost": 0.01,
            "prompt_tokens": 120,
            "completion_tokens": 40,
        },
    )
    page.route(
        f"{r5_2_server}/v1/admin/api-keys*",
        lambda route: route.fulfill(json={"keys": [{"id": 7, "name": "Team Backend"}]}),
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")

    row = page.locator("#actTableBody tr").first
    expect(row).to_contain_text("req-abc-123")

    row.click()
    drawer = page.locator(".req-drawer-overlay")
    expect(drawer).to_be_visible()
    expect(page.locator("#requestDrawerTitle")).to_have_text("Request details")
    expect(drawer.locator("code")).to_have_text("req-abc-123")
    expect(drawer).to_contain_text("gpt-4o-mini")
    expect(drawer).to_contain_text("llmgateway/light_model")
    expect(drawer).to_contain_text("openai")
    expect(drawer).to_contain_text("120")
    expect(drawer).to_contain_text("40")
    # Master-only virtual key field resolves and shows the key's name.
    expect(drawer.locator("[data-master-only]")).to_contain_text("#7")
    expect(drawer.locator("[data-master-only]")).to_contain_text("Team Backend")

    page.keyboard.press("Escape")
    expect(drawer).to_be_hidden()
    # Focus returns to the row that opened the drawer.
    expect(row).to_be_focused()


def test_activity_drawer_copy_button_copies_request_id(page: Page, r5_2_server: str):
    page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_single_usage_record(
        page,
        r5_2_server,
        {
            "request_id": "req-copy-me",
            "timestamp": "2026-07-19T10:00:00Z",
            "api_key_id": None,
            "operation": "chat",
            "model": "llmgateway/light_model",
            "provider": "openai",
            "status": "completed",
            "duration_ms": 500,
            "cost": 0.01,
        },
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")
    page.locator("#actTableBody tr").first.click()

    copy_btn = page.locator(".req-drawer-copy-btn")
    expect(copy_btn).to_be_visible()
    copy_btn.click()
    expect(copy_btn).to_have_text("Copied!")
    clipboard_text = page.evaluate("navigator.clipboard.readText()")
    assert clipboard_text == "req-copy-me"


def test_activity_drawer_hides_master_only_fields_for_user_role(page: Page, r5_2_server: str):
    virtual_key = _create_virtual_key(page.context, r5_2_server)
    _login(page.context, r5_2_server, virtual_key)
    _route_single_usage_record(
        page,
        r5_2_server,
        {
            "request_id": "req-user-drawer",
            "timestamp": "2026-07-19T10:00:00Z",
            "api_key_id": 3,
            "operation": "chat",
            "model": "llmgateway/light_model",
            "provider": "openai",
            "status": "completed",
            "duration_ms": 200,
            "cost": 0.001,
        },
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")
    page.wait_for_function("window.gatewayAuth && window.gatewayAuth.state === 'user'")
    page.locator("#actTableBody tr").first.click()

    drawer = page.locator(".req-drawer-overlay")
    expect(drawer).to_be_visible()
    expect(drawer.locator("code")).to_have_text("req-user-drawer")
    expect(drawer.locator("[data-master-only]")).to_be_hidden()


def test_activity_server_error_mode_shows_disclosed_empty_state_without_fetching(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    page.route(
        f"{r5_2_server}/v1/api/usage-records*",
        lambda route: route.fulfill(json={"records": [], "total_records": 0}),
    )
    seen_rejection_requests = []
    page.route(
        f"{r5_2_server}/v1/admin/rejections*",
        lambda route: (seen_rejection_requests.append(route.request.url), route.fulfill(json={"items": [], "total": 0}))[-1],
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")
    page.select_option("#filterStatus", "server_error")

    expect(page.locator("#serverErrorEmpty")).to_be_visible()
    expect(page.locator("#serverErrorEmpty")).to_contain_text("No data source for 5xx")
    expect(page.locator("#actListing")).to_be_hidden()
    assert seen_rejection_requests == []


def test_activity_rejected_filter_and_key_column_are_master_only(page: Page, r5_2_server: str):
    virtual_key = _create_virtual_key(page.context, r5_2_server)
    _login(page.context, r5_2_server, virtual_key)
    page.route(
        f"{r5_2_server}/v1/api/usage-records*",
        lambda route: route.fulfill(
            json={
                "records": [
                    {
                        "request_id": "req-user-1",
                        "timestamp": "2026-07-19T10:00:00Z",
                        "api_key_id": 3,
                        "operation": "chat",
                        "model": "llmgateway/light_model",
                        "provider": "openai",
                        "status": "completed",
                        "duration_ms": 200,
                        "cost": 0.001,
                    }
                ],
                "total_records": 1,
            }
        ),
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")
    page.wait_for_function("window.gatewayAuth && window.gatewayAuth.state === 'user'")

    expect(page.locator('#filterStatus option[value="rejected"]')).to_be_hidden()
    expect(page.locator("th", has_text="Key")).to_be_hidden()
    key_cell = page.locator("#actTableBody tr").first.locator("td").nth(2)
    expect(key_cell).to_be_hidden()
