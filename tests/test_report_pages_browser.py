"""Browser tests for the report pages: Live now, Model failures, My key.

Route-level access is covered in tests/test_report_pages.py; here the pages'
own rendering runs against mocked API responses.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from playwright.sync_api import Page, expect

from tests.test_r5_2_ui_acceptance_browser import (
    MASTER_KEY,
    _assert_axe_no_high_impact,
    _create_virtual_key,
    _login,
    r5_2_server,
)

__all__ = ["r5_2_server"]
pytestmark = pytest.mark.browser


def _route_json(page: Page, url_pattern: str, payload) -> None:
    page.route(url_pattern, lambda route: route.fulfill(json=payload))


def _current_hour_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:00:00")


def _today_key() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _dashboard(**overrides) -> dict:
    payload = {
        "filters": {"bucket": "hour"},
        "totals": {"requests": 0, "total_tokens": 0, "cost": 0},
        "series": {"usage": []},
        "breakdowns": {"providers": [], "resolved_targets": [], "api_keys": [], "x_titles": []},
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }
    payload.update(overrides)
    return payload


def _status_row(**overrides) -> dict:
    row = {
        "provider": "openrouter",
        "model": "kimi-k2",
        "upstream_key_fingerprint": "abcd1234",
        "health_status": "healthy",
        "last_checked_at": datetime.now(timezone.utc).isoformat(),
        "last_error": None,
        "cooldown_remaining_seconds": 0,
        "cooldown_escalation_level": 0,
        "requests_last_minute": 0,
        "requests_last_day": 0,
        "tokens_last_minute": 0,
        "tokens_last_day": 0,
        "observed_limits": {},
    }
    row.update(overrides)
    return row


def test_live_page_shows_running_requests_and_models_under_load(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _dashboard(
        totals={"requests": 120, "total_tokens": 50000, "cost": 0.42},
        previous={"totals": {"requests": 80, "total_tokens": 50000, "cost": 0}, "fallback": {}},
        series={"usage": [{"time_period": _current_hour_key(), "requests": 30, "total_tokens": 9000, "cost": 0.1}]},
        breakdowns={
            "providers": [],
            "resolved_targets": [{"provider": "openrouter", "model": "kimi-k2", "requests": 80, "cost": 0.3}],
            "api_keys": [{"label": "laptop", "api_key_id": 7, "api_key_name": "laptop", "requests": 90}],
            "x_titles": [],
        },
    ))
    _route_json(page, f"{r5_2_server}/v1/api/usage-records*", {"records": [
        {
            "status": "running", "duration_ms": 45000, "gateway_model": "llmgateway/chat",
            "provider": "openrouter", "model": "kimi-k2", "api_key_id": 7, "x_title": "notes-app",
            "operation": "chat",
        },
        {"status": "completed", "duration_ms": 900, "gateway_model": "llmgateway/chat"},
    ]})
    _route_json(page, f"{r5_2_server}/v1/api/upstream-status", {"rows": [
        _status_row(
            requests_last_minute=6, requests_last_day=80, tokens_last_minute=3000,
            cooldown_remaining_seconds=75,
            observed_limits={"rpm": {"limit": 10, "remaining": 4, "reset_in_seconds": 20}},
        ),
        _status_row(model="idle-model", requests_last_day=0),
    ]})

    page.goto(f"{r5_2_server}/v1/ui/live")

    expect(page.locator("#kpiRunning [data-kpi-value]")).to_have_text("1")
    expect(page.locator("#kpiRunning")).to_have_attribute("data-tone", "warn")
    expect(page.locator("#kpiRpm [data-kpi-value]")).to_have_text("6")
    expect(page.locator("#kpiCost [data-kpi-note]")).to_contain_text("kimi-k2")
    expect(page.locator("#kpiRpm [data-kpi-delta]")).to_have_text("▲ 50% vs the previous 24 hours")
    expect(page.locator("#kpiTpm [data-kpi-delta]")).to_have_text("same as the previous 24 hours")
    expect(page.locator("#kpiCost [data-kpi-delta]")).to_have_text("the previous 24 hours had 0")
    expect(page.locator("#runningBody tr")).to_have_count(1)
    expect(page.locator("#runningBody")).to_contain_text("llmgateway/chat")
    expect(page.locator("#runningBody")).to_contain_text("laptop")
    expect(page.locator("#whoList")).to_contain_text("laptop")
    # Only models that served traffic in 24 h are listed.
    expect(page.locator("#targetsBody tr")).to_have_count(1)
    expect(page.locator("#targetsBody")).to_contain_text("6 of 10")
    expect(page.locator("#targetsBody")).to_contain_text("Cooling down · 1:15")
    _assert_axe_no_high_impact(page, "document")

    page.click("#pauseBtn")
    expect(page.locator("#pauseBtn")).to_have_text("Resume updates")
    expect(page.locator("#pauseBtn")).to_have_attribute("aria-pressed", "true")


def test_failures_page_lists_unavailable_targets_and_resumes_a_pause(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    pauses = [{
        "provider": "groq", "upstream_key_fingerprint": "ffff9999", "model": None,
        "reason": "maintenance", "remaining_seconds": 600,
    }]
    resume_bodies: list[dict] = []

    def handle_pauses(route):
        route.fulfill(json={"targets": [], "pauses": pauses})

    def handle_resume(route):
        resume_bodies.append(json.loads(route.request.post_data or "{}"))
        pauses.clear()
        route.fulfill(json={"pauses": []})

    page.route(f"{r5_2_server}/v1/admin/upstream-pauses", handle_pauses)
    page.route(f"{r5_2_server}/v1/admin/upstream-pauses/resume", handle_resume)
    _route_json(page, f"{r5_2_server}/v1/api/upstream-status", {"rows": [
        _status_row(cooldown_remaining_seconds=120, last_error="HTTP 429", cooldown_escalation_level=2),
        _status_row(model="healthy-model"),
    ]})
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _dashboard(reliability={
        "fallback": {
            "summary": {"attempts": 200, "successes": 190, "errors": 10},
            "requests": {"total": 50, "rescued": 3, "failed": 1},
            "series": [{"time_period": _current_hour_key(), "attempts": 20, "successes": 15, "errors": 5}],
            "error_types": [{"label": "http_429", "errors": 7}, {"label": "http_502", "errors": 3}],
            "upstream": [
                {"provider": "openrouter", "model": "kimi-k2", "upstream_key_fingerprint": "abcd1234",
                 "attempts": 40, "successes": 30, "errors": 10, "success_rate": 75.0,
                 "avg_duration_ms": 1500, "top_error_type": "http_429"},
            ],
        },
        "rejections": {},
    }, previous={"totals": {}, "fallback": {
        "summary": {"attempts": 100, "successes": 100, "errors": 0},
        "requests": {"total": 30, "rescued": 3, "failed": 2},
    }}))
    _route_json(page, f"{r5_2_server}/v1/api/fallback-records*", {"records": [{
        "request_id": "req-1", "timestamp": datetime.now(timezone.utc).isoformat(),
        "gateway_model": "llmgateway/chat", "x_title": "notes-app", "success": True,
        "total_duration_ms": 2500, "attempts": [
            {"provider": "openrouter", "model": "kimi-k2", "success": False, "error_type": "http_429",
             "error_message": "rate limited", "duration_ms": 500},
            {"provider": "groq", "model": "llama", "success": True, "error_type": None, "duration_ms": 2000},
        ],
    }], "total_records": 1})

    page.goto(f"{r5_2_server}/v1/ui/failures")

    expect(page.locator("#kpiUnavailable [data-kpi-value]")).to_have_text("2 of 3")
    expect(page.locator("#kpiNotRescued [data-kpi-value]")).to_have_text("1")
    expect(page.locator("#kpiNotRescued")).to_have_attribute("data-tone", "bad")
    expect(page.locator("#kpiRescued [data-kpi-note]")).to_contain_text("of 4 requests")
    # Fewer unrescued requests is good news, new failed attempts are bad news.
    expect(page.locator("#kpiNotRescued [data-kpi-delta]")).to_have_text("▼ 50% vs the previous period")
    expect(page.locator("#kpiNotRescued [data-kpi-delta]")).to_have_attribute("data-tone", "ok")
    expect(page.locator("#kpiRescued [data-kpi-delta]")).to_have_text("same as the previous period")
    expect(page.locator("#kpiFailedAttempts [data-kpi-delta]")).to_have_text("the previous period had 0")
    expect(page.locator("#kpiFailedAttempts [data-kpi-delta]")).to_have_attribute("data-tone", "bad")
    expect(page.locator("#downList .rp-down-card")).to_have_count(2)
    expect(page.locator("#downList")).to_contain_text("HTTP 429")
    expect(page.locator("#downList")).to_contain_text("cooldown level 2")
    expect(page.locator("#downList")).to_contain_text("all models of the key")
    expect(page.locator("#whyList")).to_contain_text("Rate limit · http_429")
    expect(page.locator("#whyList")).to_contain_text("Provider server error · http_502")
    expect(page.locator("#reliabilityBody")).to_contain_text("75%")
    expect(page.locator("#chainsBody")).to_contain_text("rescued")
    expect(page.locator("#chainsBody .rp-hop")).to_have_count(2)
    _assert_axe_no_high_impact(page, "document")

    page.get_by_role("button", name="Remove pause").click()

    expect(page.locator("#downList .rp-down-card")).to_have_count(1)
    assert resume_bodies == [{"provider": "groq", "upstream_key_fingerprint": "ffff9999", "model": None}]


def _failures_routes(page: Page, base_url: str, *, status_rows: list[dict], targets: list[dict]) -> dict:
    """Mocks the failures page APIs; returns the recorded POST bodies and dashboard URLs."""
    seen: dict = {"health": [], "pause": [], "dashboard": []}

    def handle_pauses(route):
        if route.request.method == "POST":
            seen["pause"].append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"targets": targets, "pauses": []})

    def handle_health(route):
        seen["health"].append(json.loads(route.request.post_data or "{}"))
        route.fulfill(json={"checked": 1, "results": []})

    def handle_dashboard(route):
        seen["dashboard"].append(route.request.url)
        filtered = "model=" in route.request.url
        route.fulfill(json=_dashboard(reliability={"fallback": {
            "summary": {"attempts": 10, "successes": 6, "errors": 4},
            "requests": {"total": 4, "rescued": 3, "failed": 1},
            "series": [{"time_period": _current_hour_key(), "attempts": 10, "successes": 6, "errors": 4}],
            "error_types": [{"label": "read_timeout" if filtered else "http_429", "errors": 4}],
            "upstream": [{
                "provider": "openrouter", "model": "kimi-k2", "upstream_key_fingerprint": "abcd1234",
                "attempts": 10, "successes": 6, "errors": 4, "success_rate": 60.0,
                "avg_duration_ms": 900, "top_error_type": "http_429",
            }],
        }, "rejections": {}}))

    page.route(f"{base_url}/v1/admin/upstream-pauses", handle_pauses)
    page.route(f"{base_url}/v1/api/upstream-health/run", handle_health)
    page.route(f"{base_url}/v1/api/analytics-dashboard*", handle_dashboard)
    _route_json(page, f"{base_url}/v1/api/upstream-status", {"rows": status_rows})
    _route_json(page, f"{base_url}/v1/api/fallback-records*", {"records": [], "total_records": 0})
    return seen


def test_failures_page_checks_one_target_and_pauses_a_model(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    seen = _failures_routes(
        page,
        r5_2_server,
        status_rows=[
            _status_row(health_status="error", last_error="HTTP 502"),
            _status_row(model="glm-4.6"),
            _status_row(provider="groq", model="llama", upstream_key_fingerprint="ffff9999"),
        ],
        targets=[
            {"provider": "openrouter", "upstream_key_fingerprint": "abcd1234"},
            {"provider": "groq", "upstream_key_fingerprint": "ffff9999"},
        ],
    )

    page.goto(f"{r5_2_server}/v1/ui/failures")
    card = page.locator("#downList .rp-down-card")
    expect(card).to_have_count(1)
    assert "compare=previous" in seen["dashboard"][0]

    card.get_by_role("button", name="Check now").click()
    expect(page.locator("#updatedAt")).to_contain_text("Checked 1 model")
    assert seen["health"] == [{"provider": "openrouter", "model": "kimi-k2", "upstream_key_fingerprint": "abcd1234"}]

    # "Pause" on a card opens the form prefilled with that key and model.
    page.locator("#downList .rp-down-card").get_by_role("button", name="Pause").click()
    expect(page.locator("#pauseDetails")).to_have_attribute("open", "")
    expect(page.locator("#pauseKey")).to_have_value("openrouter|abcd1234")
    expect(page.locator("#pauseModel")).to_have_value("kimi-k2")
    expect(page.locator("#pauseReason")).to_be_focused()
    expect(page.locator("#pauseModels option")).to_have_count(2)
    _assert_axe_no_high_impact(page, "document")

    # Another key, the whole key (no model), 90 minutes.
    page.select_option("#pauseKey", "groq|ffff9999")
    expect(page.locator("#pauseModels option")).to_have_count(1)
    page.fill("#pauseModel", "")
    page.fill("#pauseMinutes", "90")
    page.fill("#pauseReason", "provider maintenance")
    page.click("#pauseSubmit")

    expect(page.locator("#pauseDetails")).not_to_have_attribute("open", "")
    assert seen["pause"] == [{
        "provider": "groq", "upstream_key_fingerprint": "ffff9999", "model": None,
        "duration_seconds": 5400, "reason": "provider maintenance",
    }]


def test_failures_page_opens_a_chart_for_one_model(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    seen = _failures_routes(page, r5_2_server, status_rows=[_status_row()], targets=[])

    page.goto(f"{r5_2_server}/v1/ui/failures")
    expect(page.locator("#modelDetail")).to_be_hidden()
    page.locator("#reliabilityBody").get_by_role("button", name="kimi-k2 · openrouter").click()

    expect(page.locator("#modelDetail")).to_be_visible()
    expect(page.locator("#modelDetailTitle")).to_have_text("kimi-k2 · openrouter · key …1234")
    expect(page.locator("#modelDetailTitle")).to_be_focused()
    expect(page.locator("#modelDetailWhy")).to_contain_text("read_timeout")
    expect(page.locator("#modelDetailChart svg")).to_have_count(1)
    filtered = [url for url in seen["dashboard"] if "model=" in url]
    assert filtered and "provider=openrouter" in filtered[-1]
    assert "upstream_key_fingerprint=abcd1234" in filtered[-1]
    _assert_axe_no_high_impact(page, "document")

    # Switching the period reloads the open chart for the new range.
    page.select_option("#rangeSelect", "7d")
    expect(page.locator("#modelDetailHint")).to_have_text("failed attempts per day")
    assert any("range=7d" in url and "model=" in url for url in seen["dashboard"])

    page.click("#modelDetailClose")
    expect(page.locator("#modelDetail")).to_be_hidden()


def test_live_page_opens_a_chart_for_one_model(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    dashboard_urls: list[str] = []

    def handle_dashboard(route):
        dashboard_urls.append(route.request.url)
        requests = 7 if "model=" in route.request.url else 30
        route.fulfill(json=_dashboard(
            totals={"requests": requests, "total_tokens": 100, "cost": 0},
            series={"usage": [{"time_period": _current_hour_key(), "requests": requests}]},
        ))

    page.route(f"{r5_2_server}/v1/api/analytics-dashboard*", handle_dashboard)
    _route_json(page, f"{r5_2_server}/v1/api/usage-records*", {"records": []})
    _route_json(page, f"{r5_2_server}/v1/api/upstream-status", {"rows": [
        _status_row(requests_last_minute=2, requests_last_day=7),
    ]})

    page.goto(f"{r5_2_server}/v1/ui/live")
    # No "previous" block in the payload: no comparison line either.
    expect(page.locator("#kpiRpm [data-kpi-value]")).to_have_text("2")
    expect(page.locator("#kpiRpm [data-kpi-delta]")).to_have_count(0)
    assert "compare=previous" in dashboard_urls[0]

    page.locator("#targetsBody").get_by_role("button", name="kimi-k2 · openrouter").click()

    expect(page.locator("#modelDetail")).to_be_visible()
    expect(page.locator("#modelDetailTitle")).to_have_text("kimi-k2 · openrouter")
    expect(page.locator("#modelDetailTitle")).to_be_focused()
    expect(page.locator("#modelDetailChart svg")).to_have_attribute(
        "aria-label", "Requests to kimi-k2 per hour over 24 hours",
    )
    filtered = [url for url in dashboard_urls if "model=" in url]
    assert filtered and "provider=openrouter" in filtered[-1]
    assert "upstream_key_fingerprint=abcd1234" in filtered[-1]
    _assert_axe_no_high_impact(page, "document")

    page.click("#modelDetailClose")
    expect(page.locator("#modelDetail")).to_be_hidden()


def test_phone_bottom_nav_links_main_sections_and_opens_the_drawer(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    page.set_viewport_size({"width": 390, "height": 844})

    page.goto(f"{r5_2_server}/v1/ui/failures")

    bar = page.locator(".gateway-bottom-nav")
    expect(bar).to_be_visible()
    expect(bar.locator(".gateway-bottom-nav-link")).to_have_text(["Live", "Failures", "Activity", "Menu"])
    expect(bar.locator('[aria-current="page"]')).to_have_text("Failures")
    for entry in bar.locator(".gateway-bottom-nav-link").all():
        box = entry.bounding_box()
        assert box is not None and box["height"] >= 44
    # The page reserves room so the bar never covers the last content.
    padding = page.evaluate("parseFloat(getComputedStyle(document.body).paddingBottom)")
    assert padding >= 56
    _assert_axe_no_high_impact(page, "document")

    bar.get_by_role("button", name="Menu").click()
    expect(page.locator("#gateway-sidebar")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#gateway-sidebar")).to_be_hidden()

    page.set_viewport_size({"width": 1280, "height": 900})
    expect(bar).to_be_hidden()


def test_my_key_page_warns_about_a_low_budget_and_explains_rejections(page: Page, r5_2_server: str):
    virtual_key = _create_virtual_key(page.context, r5_2_server)
    _login(page.context, r5_2_server, virtual_key)
    _route_json(page, f"{r5_2_server}/v1/api/quota/keys", [{
        "key_id": 3, "name": "laptop", "disabled": False,
        "rpm": {"current": 3, "limit": 60, "reset_in_seconds": 30},
        "tpm": {"current": 1200, "limit": None, "reset_in_seconds": 30},
        "budget": {"spent_usd": 4.62, "limit_usd": 5.0, "period": "monthly", "reset_at": "2026-11-01T00:00:00+00:00"},
        "allowed_models": ["llmgateway/chat", "llmgateway/code"],
        "fallback_events_24h": 0,
    }])
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _dashboard(
        series={"usage": [{"time_period": _today_key(), "requests": 148, "cost": 0.7}]},
        reliability={"fallback": {}, "rejections": {
            "summary": {"rejections": 9},
            "recent": [{
                "timestamp": datetime.now(timezone.utc).isoformat(), "category": "rate_limited",
                "reason": "rpm limit reached", "status_code": 429, "path": "/v1/chat/completions",
            }],
        }},
    ))

    page.goto(f"{r5_2_server}/v1/ui/my-key")

    expect(page.locator("#keyHeading")).to_have_text("laptop")
    expect(page.locator("#callout")).to_be_visible()
    # $0.38 left at $0.10/day (0.7 over the last 7 days) lasts about 3 days.
    expect(page.locator("#calloutTitle")).to_contain_text("about 3 days")
    expect(page.locator("#budgetHeading")).to_have_text("Monthly budget")
    expect(page.locator("#factRpm")).to_have_text("3 of 60")
    expect(page.locator("#factTpm")).to_contain_text("no limit")
    expect(page.locator("#factToday")).to_have_text("148")
    expect(page.locator("#modelList")).to_have_text("llmgateway/chat, llmgateway/code")
    expect(page.locator("#baseUrl")).to_have_text(f"{r5_2_server}/v1")
    expect(page.locator("#rejectionsHint")).to_have_text("9 in 30 days")
    expect(page.locator("#rejectionsBody")).to_contain_text("Too many requests")
    expect(page.locator("#rejectionsBody")).to_contain_text("add a pause between requests")
    expect(page.locator("#masterNote")).to_be_hidden()
    _assert_axe_no_high_impact(page, "document")


def test_my_key_page_has_no_spend_chart_without_spending(page: Page, r5_2_server: str):
    virtual_key = _create_virtual_key(page.context, r5_2_server)
    _login(page.context, r5_2_server, virtual_key)
    _route_json(page, f"{r5_2_server}/v1/api/analytics-dashboard*", _dashboard(
        series={"usage": [{"time_period": _today_key(), "requests": 3, "cost": 0}]},
    ))

    page.goto(f"{r5_2_server}/v1/ui/my-key")

    expect(page.locator("#spendChart")).to_have_text("No spending in the last 14 days.")
    expect(page.locator("#spendChart svg")).to_have_count(0)


def test_my_key_page_points_the_master_to_quota(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)

    page.goto(f"{r5_2_server}/v1/ui/my-key")

    expect(page.locator("#masterNote")).to_be_visible()
    expect(page.locator("#masterNote a")).to_have_attribute("href", "/v1/ui/quota")
    expect(page.locator("#keyContent")).to_be_hidden()


def test_admin_pages_drop_the_page_card_and_grey_buttons(page: Page, r5_2_server: str):
    _login(page.context, r5_2_server, MASTER_KEY)
    # A button with no author styles, the grey browser default a page button must not look like.
    browser_default_js = """() => {
        const host = document.createElement('div');
        const probe = document.createElement('button');
        host.attachShadow({mode: 'open'}).appendChild(probe);
        document.body.appendChild(host);
        const bg = getComputedStyle(probe).backgroundColor;
        host.remove();
        return bg;
    }"""
    accent_js = """() => {
        const probe = document.createElement('div');
        probe.style.background = 'var(--accent)';
        document.body.appendChild(probe);
        const bg = getComputedStyle(probe).backgroundColor;
        probe.remove();
        return bg;
    }"""

    page.goto(f"{r5_2_server}/v1/ui/activity")

    expect(page.locator(".container")).to_have_css("box-shadow", "none")
    expect(page.locator(".container")).to_have_css("background-color", "rgba(0, 0, 0, 0)")
    expect(page.locator("h1")).to_have_css("font-size", "26px")
    browser_grey = page.evaluate(browser_default_js)
    for button_id in ("#resetFiltersBtn", "#refreshBtn"):
        background = page.locator(button_id).evaluate("el => getComputedStyle(el).backgroundColor")
        assert background != browser_grey, button_id
    expect(page.locator("#applyFiltersBtn")).to_have_css("background-color", page.evaluate(accent_js))

    # The legacy analytics page keeps its card.
    page.goto(f"{r5_2_server}/v1/ui/usage-stats")
    expect(page.locator(".container").first).not_to_have_css("box-shadow", "none")
