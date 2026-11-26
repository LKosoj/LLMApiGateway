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


def _rich_dashboard(role: str = "master", include_lifetime: bool = True) -> dict:
    payload = {
        "scope": {"role": role},
        "filters": {"bucket": "day"},
        "totals": {
            "requests": 120,
            "total_tokens": 54000,
            "cost": 2.4,
            "cost_saved": 0.6,
            "tokens_per_second": 48.5,
            "ttft_p50_ms": 220,
            "ttft_p95_ms": 1400,
            "duration_p95_ms": 900,
            "active_requests": 3,
            "fallback_errors": 2,
            "rejections": 5,
        },
        "previous": {
            "totals": {
                "requests": 100,
                "total_tokens": 45000,
                "cost": 2.0,
                "cost_saved": 0.4,
                "tokens_per_second": 40.0,
                "duration_p95_ms": 1000,
            },
            "fallback": {
                "summary": {"attempts": 50, "errors": 4, "success_rate": 92.0},
            },
        },
        "series": {
            "usage": [
                {"time_period": f"2026-07-{day:02d}", "requests": 10 + day, "total_tokens": 1000 * day, "cost": 0.1 * day}
                for day in range(1, 10)
            ]
        },
        "breakdowns": {
            "resolved_targets": [
                {
                    "label": "openai / gpt-4o-mini",
                    "requests": 80,
                    "total_tokens": 32000,
                    "cost": 1.5,
                    "cost_saved": 0.4,
                    "tokens_per_second": 52.3,
                    "fallback_success_rate": 98.5,
                }
            ],
            "providers": [
                {
                    "label": "openai",
                    "requests": 90,
                    "total_tokens": 40000,
                    "cost": 1.8,
                    "cost_saved": 0.5,
                    "tokens_per_second": 50.0,
                    "ttft_avg_ms": 210,
                    "duration_p95_ms": 880,
                    "fallback_success_rate": 97.2,
                }
            ],
            "operations": [
                {
                    "label": "chat",
                    "requests": 110,
                    "total_tokens": 50000,
                    "cost": 2.2,
                    "cost_saved": 0.6,
                    "tokens_per_second": 49.1,
                    "avg_duration_ms": 620,
                }
            ],
            "api_keys": [
                {
                    "label": "7",
                    "api_key_name": "Team Backend",
                    "requests": 70,
                    "total_tokens": 28000,
                    "cost": 1.1,
                    "cost_saved": 0.2,
                    "tokens_per_second": 46.0,
                }
            ]
            if role == "master"
            else [],
            "x_titles": [
                {
                    "label": "tgBot",
                    "requests": 60,
                    "total_tokens": 21000,
                    "cost": 0.9,
                    "cost_saved": 0.1,
                    "tokens_per_second": 44.2,
                }
            ],
        },
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }
    if include_lifetime:
        payload["lifetime"] = {
            "requests": 9876,
            "total_tokens": 1234567,
            "cost": 42.5,
            "cost_saved": 7.25,
            "first_event_at": "2025-01-15T10:00:00Z",
        }
    return payload


def test_overview_renders_extended_kpis_deltas_and_lifetime_with_compare_previous(
    page: Page, r5_2_server: str
):
    _login(page.context, r5_2_server, MASTER_KEY)
    seen_urls: list[str] = []

    def handle_dashboard(route):
        seen_urls.append(route.request.url)
        route.fulfill(json=_rich_dashboard("master", include_lifetime=True))

    page.route(f"{r5_2_server}/v1/api/analytics-dashboard*", handle_dashboard)
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator("#kpiRequests")).to_have_text("120")
    expect(page.locator("#kpiTokens")).to_have_text("54,000")
    expect(page.locator("#kpiCostSaved")).to_contain_text("0.6000")
    expect(page.locator("#kpiThroughput")).to_have_text("48.5 tok/s")
    expect(page.locator("#kpiTtft")).to_have_text("220 ms / 1.4 s")

    # Deltas and semantic tones
    expect(page.locator("#kpiRequestsDelta")).to_have_attribute("data-tone", "neutral")
    expect(page.locator("#kpiRequestsDelta")).to_contain_text("+20%")
    expect(page.locator("#kpiCostDelta")).to_have_attribute("data-tone", "bad")
    expect(page.locator("#kpiCostSavedDelta")).to_have_attribute("data-tone", "ok")
    expect(page.locator("#kpiLatencyDelta")).to_have_attribute("data-tone", "ok")
    expect(page.locator("#kpiErrorsDelta")).to_have_attribute("data-tone", "ok")

    # Lifetime row
    expect(page.locator("#overviewLifetime")).to_be_visible()
    expect(page.locator("#overviewLifetime")).to_contain_text("All-time")
    expect(page.locator("#overviewLifetime")).to_contain_text("9,876")
    expect(page.locator("#overviewLifetime")).to_contain_text("2025")

    assert any("compare=previous" in url for url in seen_urls)


def test_overview_drilldowns_and_keys_tab_are_master_only(page: Page, r5_2_server: str):
    # Master sees all 3 drill-down links and Keys tab
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _rich_dashboard("master"))
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")

    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/live"]')).to_be_visible()
    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/failures"]')).to_be_visible()
    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/rejections"]')).to_be_visible()
    expect(page.locator('#breakdownTabs [data-tab="api_keys"]')).to_be_visible()

    # User sees no drill-down links, no Keys tab, and no lifetime row when omitted
    page.route(
        f"{r5_2_server}/auth/me",
        lambda route: route.fulfill(json={"role": "user", "key_id": 7, "name": "Team Backend"}),
    )
    _route_json(
        page,
        f"{r5_2_server}/v1/api/analytics-dashboard*",
        _rich_dashboard("user", include_lifetime=False),
    )

    page.goto(f"{r5_2_server}/v1/ui/overview")
    page.wait_for_function("window.gatewayAuth && window.gatewayAuth.state === 'user'")

    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/live"]')).to_be_hidden()
    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/failures"]')).to_be_hidden()
    expect(page.locator('a.ov-kpi-drill[href="/v1/ui/rejections"]')).to_be_hidden()
    expect(page.locator('#breakdownTabs [data-tab="api_keys"]')).to_be_hidden()
    expect(page.locator("#overviewLifetime")).to_be_hidden()


def test_overview_trend_metric_switch_and_interactive_tooltip(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _rich_dashboard("master"))
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")
    expect(page.locator("#trendChart svg")).to_be_visible()

    # Intermediate X-axis labels (more than just first and last)
    labels = page.locator("#trendChart svg text.ov-chart-label")
    assert labels.count() >= 5

    # Switch to total_tokens metric
    page.select_option("#trendMetric", "total_tokens")
    hit = page.locator("#trendChart svg circle.ov-hit").nth(2)
    hit.focus()
    tooltip = page.locator("#trendChart .ov-chart-tooltip")
    expect(tooltip).to_be_visible()
    expect(tooltip).to_contain_text("2026-07-03")
    expect(tooltip).to_contain_text("3,000")

    # Tooltip hides on blur
    hit.blur()
    expect(tooltip).to_be_hidden()

    # Tooltip shows on mouseenter and hides on mouseleave
    hit.dispatch_event("mouseenter")
    expect(tooltip).to_be_visible()
    hit.dispatch_event("mouseleave")
    expect(tooltip).to_be_hidden()


def test_overview_single_point_trend_and_missing_optional_fields(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    payload = {
        "scope": {"role": "master"},
        "filters": {"bucket": "day"},
        "totals": {
            "requests": 5,
            "total_tokens": 1000,
            "cost": 0.05,
            "cost_saved": 0.0,
            "tokens_per_second": 0,
            "ttft_p50_ms": None,
            "ttft_p95_ms": None,
            "duration_p95_ms": 0,
            "active_requests": 0,
            "fallback_errors": 0,
            "rejections": 0,
        },
        "previous": {
            "totals": {
                "requests": 5,
                "tokens_per_second": 42.0,
                "duration_p95_ms": 500,
            },
        },
        "series": {
            "usage": [
                {"time_period": "2026-07-19", "requests": 5, "total_tokens": 1000, "cost": 0.05},
            ]
        },
        "breakdowns": {
            "resolved_targets": [
                {
                    "label": "openai / gpt-4o-mini",
                    "requests": 5,
                    "total_tokens": 1000,
                    "cost": 0.05,
                    "cost_saved": 0.0,
                    "tokens_per_second": None,
                    "fallback_success_rate": None,
                }
            ],
            "providers": [],
            "operations": [],
            "api_keys": [],
            "x_titles": [],
        },
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", payload)
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")
    expect(page.locator("#trendChart svg")).to_be_visible()

    # Single-point chart skips polygon/polyline and centers its lone X-axis label
    assert page.locator("#trendChart svg polygon.ov-area").count() == 0
    assert page.locator("#trendChart svg polyline.ov-line").count() == 0
    x_label = page.locator("#trendChart svg text.ov-chart-label", has_text="2026-07-19")
    expect(x_label).to_have_attribute("text-anchor", "middle")

    # Zero/null throughput & latency render N/A without bogus -100% deltas
    expect(page.locator("#kpiThroughput")).to_have_text("N/A")
    expect(page.locator("#kpiThroughputDelta")).to_be_hidden()
    expect(page.locator("#kpiTtft")).to_have_text("N/A")
    expect(page.locator("#kpiLatency")).to_have_text("N/A")
    expect(page.locator("#kpiLatencyDelta")).to_be_hidden()
    expect(page.locator("#kpiErrorsDelta")).to_be_hidden()
    expect(page.locator("#topModelsBody")).to_contain_text("N/A")


def test_overview_breakdown_tabs_switch_columns_and_rows_and_ru_locale(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    dashboard_fetches: list[str] = []

    def handle_dashboard(route):
        dashboard_fetches.append(route.request.url)
        route.fulfill(json=_rich_dashboard("master"))

    page.route(f"{r5_2_server}/v1/api/analytics-dashboard*", handle_dashboard)
    page.route(f"{r5_2_server}/health", lambda route: route.fulfill(json={"status": "ok", "phase": "ready"}))

    page.goto(f"{r5_2_server}/v1/ui/overview")

    # Default Models tab
    expect(page.locator("#topModelsBody")).to_contain_text("openai / gpt-4o-mini")
    expect(page.locator("#topModelsBody")).to_contain_text("98.5%")

    # Keyboard navigation (ArrowRight -> Providers, End -> Clients, Home -> Models)
    models_tab = page.locator('#breakdownTabs [data-tab="resolved_targets"]')
    models_tab.focus()
    page.keyboard.press("ArrowRight")
    expect(page.locator('#breakdownTabs [data-tab="providers"]')).to_be_focused()
    expect(page.locator('#breakdownTabs [data-tab="providers"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#breakdownsHead")).to_contain_text("TTFT")
    expect(page.locator("#breakdownsHead")).to_contain_text("P95")
    expect(page.locator("#topModelsBody")).to_contain_text("openai")
    expect(page.locator("#topModelsBody")).to_contain_text("210 ms")

    page.keyboard.press("End")
    expect(page.locator('#breakdownTabs [data-tab="x_titles"]')).to_be_focused()
    expect(page.locator("#topModelsBody")).to_contain_text("tgBot")

    page.keyboard.press("Home")
    expect(models_tab).to_be_focused()

    # Operations tab
    page.locator('#breakdownTabs [data-tab="operations"]').click()
    expect(page.locator("#breakdownsHead")).to_contain_text("Avg duration")
    expect(page.locator("#topModelsBody")).to_contain_text("chat")

    # Keys tab (master)
    page.locator('#breakdownTabs [data-tab="api_keys"]').click()
    expect(page.locator("#topModelsBody")).to_contain_text("Team Backend")

    # Clients tab
    page.locator('#breakdownTabs [data-tab="x_titles"]').click()
    expect(page.locator("#topModelsBody")).to_contain_text("tgBot")

    # Switch to RU locale and verify localized rendering without re-fetching dashboard
    fetches_before_locale = len(dashboard_fetches)
    page.select_option("#localeSelect", "ru")
    expect(page.locator("h1")).to_have_text("Обзор")
    expect(page.locator("#kpiThroughput")).to_contain_text("ток/с")
    expect(page.locator("#overviewLifetime")).to_contain_text("За всё время")
    expect(page.locator('#breakdownTabs [data-tab="x_titles"]')).to_have_text("Клиенты")
    assert len(dashboard_fetches) == fetches_before_locale


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
            "x_title": "tgBot",
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
    expect(drawer).to_contain_text("X-Title")
    expect(drawer).to_contain_text("tgBot")
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
