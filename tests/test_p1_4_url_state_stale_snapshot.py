"""P1-4: URL state for Rejections/Activity filters + stale-snapshot fallback.

These browser tests exercise:
- Filters (category/key_id/since/limit/page for Rejections; status/since/
  provider/key_id/request_id/limit/page for Activity) are serialized into
  ``location.search`` on every change, and restored from the URL on reload.
- When a request fails after a successful load (network/500), the page keeps
  showing the previous snapshot and surfaces a localized "stale" badge
  instead of wiping the table.
- The last successful snapshot is cached in ``sessionStorage`` so a page
  reload that immediately fails still has something to fall back to.
- A first-ever load that fails (no snapshot yet) shows the plain error
  banner, exactly like before this change.
"""

from __future__ import annotations

import urllib.parse

import pytest
from playwright.sync_api import Page, expect

from tests.test_r5_2_ui_acceptance_browser import MASTER_KEY, _login, r5_2_server


__all__ = ["r5_2_server"]
pytestmark = pytest.mark.browser


def _rejection_items(count: int, *, prefix: str = "item") -> list[dict]:
    return [
        {
            "timestamp": "2026-07-15T08:30:00+00:00",
            "category": "rate_limited",
            "status_code": 429,
            "method": "POST",
            "path": f"/v1/raw/{prefix}/{index}",
            "api_key_id": 7,
            "x_title": "Raw Client",
            "client_ip": "192.0.2.15",
            "reason": f"REASON_{prefix}_{index}",
            "request_id": f"req-{prefix}-{index}",
        }
        for index in range(count)
    ]


def _url_query(page: Page) -> dict[str, list[str]]:
    return urllib.parse.parse_qs(urllib.parse.urlparse(page.url).query)


def test_rejections_apply_filter_updates_url_and_restores_after_reload(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    page.route(
        "**/v1/admin/rejections*",
        lambda route: route.fulfill(json={"items": _rejection_items(3), "total": 3}),
    )

    page.goto(f"{r5_2_server}/v1/ui/rejections")
    expect(page.locator("#rejTableBody tr")).to_have_count(3)
    assert _url_query(page) == {}

    page.locator("#filterCategory").select_option("rate_limited")
    page.locator("#filterKeyId").fill("7")
    page.locator("#filterSince").fill("2026-07-01T10:30")
    with page.expect_request("**/v1/admin/rejections*"):
        page.locator("#applyFiltersBtn").click()

    query = _url_query(page)
    assert query["category"] == ["rate_limited"]
    assert query["key_id"] == ["7"]
    assert query["since"] == ["2026-07-01T10:30"]
    assert query["limit"] == ["50"]
    assert "page" not in query

    page.reload()
    expect(page.locator("#rejTableBody tr")).to_have_count(3)
    expect(page.locator("#filterCategory")).to_have_value("rate_limited")
    expect(page.locator("#filterKeyId")).to_have_value("7")
    expect(page.locator("#filterSince")).to_have_value("2026-07-01T10:30")


def test_rejections_pagination_updates_page_query_param(page: Page, r5_2_server: str) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    page.route(
        "**/v1/admin/rejections*",
        lambda route: route.fulfill(json={"items": _rejection_items(50), "total": 120}),
    )

    page.goto(f"{r5_2_server}/v1/ui/rejections")
    expect(page.locator("#rejTableBody tr")).to_have_count(50)

    with page.expect_request("**/v1/admin/rejections*"):
        page.locator("#nextPageBtn").click()

    query = _url_query(page)
    assert query["page"] == ["2"]

    with page.expect_request(
        lambda request: "/v1/admin/rejections" in request.url and "offset=50" in request.url
    ):
        page.reload()

    assert _url_query(page)["page"] == ["2"]


def test_rejections_renders_stale_snapshot_and_badge_on_server_error(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    responses = iter(
        [
            lambda route: route.fulfill(json={"items": _rejection_items(2), "total": 2}),
            lambda route: route.fulfill(status=500, json={"detail": "boom"}),
        ]
    )
    page.route("**/v1/admin/rejections*", lambda route: next(responses)(route))

    page.goto(f"{r5_2_server}/v1/ui/rejections")
    expect(page.locator("#rejTableBody tr")).to_have_count(2)
    expect(page.locator("#rejStaleBadge")).to_be_hidden()

    with page.expect_response("**/v1/admin/rejections*"):
        page.locator("#refreshBtn").click()

    # The previous snapshot stays on screen instead of being wiped, and the
    # stale badge with a localized relative-time hint becomes visible.
    expect(page.locator("#rejTableBody tr")).to_have_count(2)
    expect(page.locator("#rejStaleBadge")).to_be_visible()
    expect(page.locator("#rejStaleBadge")).to_contain_text("Stale")
    expect(page.locator("#messageArea")).to_be_visible()


def test_rejections_stale_snapshot_survives_reload_via_session_storage(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    should_fail = {"value": False}

    def handler(route):
        if should_fail["value"]:
            route.fulfill(status=500, json={"detail": "boom"})
        else:
            route.fulfill(json={"items": _rejection_items(2), "total": 2})

    page.route("**/v1/admin/rejections*", handler)

    page.goto(f"{r5_2_server}/v1/ui/rejections")
    expect(page.locator("#rejTableBody tr")).to_have_count(2)
    stored = page.evaluate("() => sessionStorage.getItem('rejections:last-snapshot')")
    assert stored is not None

    should_fail["value"] = True
    page.reload()

    # Even though this session's first fetch after reload fails, the snapshot
    # persisted in sessionStorage before the reload is shown immediately.
    expect(page.locator("#rejTableBody tr")).to_have_count(2)
    expect(page.locator("#rejStaleBadge")).to_be_visible()


def test_rejections_first_load_failure_without_snapshot_shows_plain_error(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    page.route(
        "**/v1/admin/rejections*",
        lambda route: route.fulfill(status=500, json={"detail": "boom"}),
    )

    page.goto(f"{r5_2_server}/v1/ui/rejections")

    expect(page.locator("#messageArea")).to_be_visible()
    expect(page.locator("#rejStaleBadge")).to_be_hidden()


def test_activity_filters_sync_to_url_and_restore_after_reload(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    page.route(
        "**/v1/api/usage-records*",
        lambda route: route.fulfill(json={"records": [], "total_records": 0}),
    )
    page.route(
        "**/v1/admin/rejections*",
        lambda route: route.fulfill(json={"items": [], "total": 0}),
    )

    page.goto(f"{r5_2_server}/v1/ui/activity")
    page.locator("#filterProvider").fill("openai")
    with page.expect_request("**/v1/api/usage-records*"):
        page.locator("#applyFiltersBtn").click()

    query = _url_query(page)
    assert query["provider"] == ["openai"]
    assert query["limit"] == ["50"]

    page.reload()
    expect(page.locator("#filterProvider")).to_have_value("openai")


def test_activity_renders_stale_snapshot_and_badge_on_server_error(
    page: Page, r5_2_server: str
) -> None:
    _login(page.context, r5_2_server, MASTER_KEY)
    responses = iter(
        [
            lambda route: route.fulfill(
                json={
                    "records": [
                        {
                            "request_id": "req-stale-1",
                            "timestamp": "2026-07-19T10:00:00Z",
                            "api_key_id": 7,
                            "operation": "chat",
                            "model": "llmgateway/light_model",
                            "provider": "openai",
                            "status": "completed",
                            "duration_ms": 500,
                            "cost": 0.01,
                        }
                    ],
                    "total_records": 1,
                }
            ),
            lambda route: route.fulfill(status=500, json={"detail": "boom"}),
        ]
    )
    page.route("**/v1/api/usage-records*", lambda route: next(responses)(route))

    page.goto(f"{r5_2_server}/v1/ui/activity")
    expect(page.locator("#actTableBody tr")).to_contain_text("req-stale-1")
    expect(page.locator("#actStaleBadge")).to_be_hidden()

    with page.expect_response("**/v1/api/usage-records*"):
        page.locator("#refreshBtn").click()

    expect(page.locator("#actTableBody tr")).to_contain_text("req-stale-1")
    expect(page.locator("#actStaleBadge")).to_be_visible()
    expect(page.locator("#actStaleBadge")).to_contain_text("Stale")
