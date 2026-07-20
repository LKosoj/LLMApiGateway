"""Browser coverage for P1-7: explicit allowed-models UX, If-Match conflicts,
and the keys-table search/sort controls.

Three UX problems this closes:
  1. An empty ``allowed_models`` list used to implicitly mean "all configured
     models" - confusing and easy to misread as "no models allowed". The
     create/edit form now has an explicit "All configured models" /
     "Only selected models" radio toggle.
  2. ``PATCH`` used to apply unconditionally, so two tabs editing the same key
     could silently clobber each other. The client now sends the ``ETag``
     read on ``GET`` as ``If-Match``; a stale save renders a conflict alert
     inside the dialog instead of losing the draft.
  3. The keys table had no way to find or order rows once there were more
     than a handful of keys.
"""

from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (
    provider_mock,
    server,
)
from tests.ui_server_helpers import create_authenticated_session


__all__ = ["provider_mock", "server"]
pytestmark = pytest.mark.browser


def _setup_session(page: Page, server: str) -> None:
    page.add_init_script(
        "if (localStorage.getItem('llmgateway:locale') === null) "
        "localStorage.setItem('llmgateway:locale', 'en');"
        "if (localStorage.getItem('llmgateway:theme') === null) "
        "localStorage.setItem('llmgateway:theme', 'light');"
    )
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )


def test_create_with_all_models_mode_sends_null_allowed_models(
    page: Page, server: str
) -> None:
    captured: dict = {}

    def api_keys_route(route) -> None:
        request = route.request
        if request.method == "POST":
            body = request.post_data_json
            captured["post_body"] = body
            route.fulfill(
                status=201,
                json={
                    "id": 101,
                    "name": body["name"],
                    "api_key": "lgk_created_secret",
                    "budget_usd": body.get("budget_usd"),
                    "spent_usd": 0.0,
                    "budget_period": body.get("budget_period"),
                    "rpm": body.get("rpm"),
                    "tpm": body.get("tpm"),
                    "allowed_models": body.get("allowed_models") or [],
                    "disabled": False,
                    "metadata": body.get("metadata"),
                    "created_at": "2026-07-20T00:00:00Z",
                    "last_used_at": None,
                },
            )
            return
        route.fulfill(status=200, json={"keys": []})

    _setup_session(page, server)
    page.route(f"{server}/v1/admin/api-keys**", api_keys_route)
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator(".api-keys-empty")).to_be_visible()

    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()
    expect(page.locator("#allowedModelsModeAll")).to_be_checked()
    expect(page.locator("#allowedModelsList")).to_be_hidden()

    page.locator("#fieldName").fill("team-all")
    page.locator("#saveKeyBtn").click()

    expect(page.locator("#newKeyNotice")).to_be_visible()
    assert captured["post_body"]["allowed_models"] is None


def test_create_with_subset_mode_sends_selected_models(
    page: Page, server: str
) -> None:
    captured: dict = {}

    def api_keys_route(route) -> None:
        request = route.request
        if request.method == "POST":
            body = request.post_data_json
            captured["post_body"] = body
            route.fulfill(
                status=201,
                json={
                    "id": 102,
                    "name": body["name"],
                    "api_key": "lgk_created_secret",
                    "budget_usd": body.get("budget_usd"),
                    "spent_usd": 0.0,
                    "budget_period": body.get("budget_period"),
                    "rpm": body.get("rpm"),
                    "tpm": body.get("tpm"),
                    "allowed_models": body.get("allowed_models") or [],
                    "disabled": False,
                    "metadata": body.get("metadata"),
                    "created_at": "2026-07-20T00:00:00Z",
                    "last_used_at": None,
                },
            )
            return
        route.fulfill(status=200, json={"keys": []})

    _setup_session(page, server)
    page.route(f"{server}/v1/admin/api-keys**", api_keys_route)
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(
            status=200, json={"data": [{"id": "model-a"}, {"id": "model-b"}]}
        ),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator(".api-keys-empty")).to_be_visible()

    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()
    page.locator("#allowedModelsModeSubset").check()
    expect(page.locator("#allowedModelsList")).to_be_visible()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    page.locator('#allowedModelsList input[value="model-a"]').check()

    page.locator("#fieldName").fill("team-subset")
    page.locator("#saveKeyBtn").click()

    expect(page.locator("#newKeyNotice")).to_be_visible()
    assert captured["post_body"]["allowed_models"] == ["model-a"]


def test_edit_conflict_shows_alert_and_preserves_draft(
    page: Page, server: str
) -> None:
    record = {
        "id": 7,
        "name": "team-conflict",
        "api_key": "lgk_conflict_secret",
        "budget_usd": None,
        "spent_usd": 0.0,
        "budget_period": "none",
        "rpm": None,
        "tpm": None,
        "allowed_models": [],
        "disabled": False,
        "metadata": None,
        "created_at": "2026-07-15T10:00:00Z",
        "last_used_at": None,
    }

    def api_keys_route(route) -> None:
        request = route.request
        if request.method == "PATCH":
            route.fulfill(
                status=412,
                json={
                    "detail": "API key was modified by another request; reload and retry",
                    "error": {
                        "message": "API key was modified by another request; reload and retry",
                        "type": "invalid_request_error",
                        "code": "api_key_conflict",
                    },
                },
            )
            return
        if request.url.rstrip("/").endswith("/v1/admin/api-keys"):
            route.fulfill(status=200, json={"keys": [record]})
            return
        # GET single record (revealFullApiKey), carries the ETag used as
        # If-Match on save.
        route.fulfill(status=200, json=record, headers={"ETag": '"etag-1"'})

    _setup_session(page, server)
    page.route(f"{server}/v1/admin/api-keys**", api_keys_route)
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator('tr[data-key-id="7"]')).to_be_visible()

    page.locator('button[data-action="edit"]').click()
    expect(page.locator("#keyModal")).to_be_visible()
    # Wait for revealFullApiKey() to resolve so currentRecordEtag is captured
    # before the save below evaluates the If-Match precondition.
    expect(page.locator("#newKeyValue")).to_have_text("lgk_conflict_secret")

    page.locator("#fieldName").fill("team-conflict-renamed")
    page.locator("#saveKeyBtn").click()

    expect(page.locator("#modalError")).to_contain_text(
        "This API key was changed elsewhere while you were editing it."
    )
    expect(page.locator("#modalError")).to_have_attribute("role", "alert")
    # The draft must survive: the dialog stays open with the typed value.
    expect(page.locator("#keyModal")).to_be_visible()
    expect(page.locator("#fieldName")).to_have_value("team-conflict-renamed")
    # Page-level message area sits behind the inert background while the
    # modal is open, so it must stay untouched.
    expect(page.locator("#messageArea")).to_have_text("")


def test_search_and_sort_the_keys_table(page: Page, server: str) -> None:
    def _record(key_id: int, name: str, created_at: str, last_used_at: str) -> dict:
        return {
            "id": key_id,
            "name": name,
            "api_key": f"lgk_{key_id}",
            "budget_usd": None,
            "spent_usd": 0.0,
            "budget_period": "none",
            "rpm": None,
            "tpm": None,
            "allowed_models": [],
            "disabled": False,
            "metadata": None,
            "created_at": created_at,
            "last_used_at": last_used_at,
        }

    records = [
        _record(1, "Zebra", "2026-07-01T00:00:00Z", "2026-07-10T00:00:00Z"),
        _record(2, "Alpha", "2026-07-05T00:00:00Z", "2026-07-01T00:00:00Z"),
        _record(3, "Middle", "2026-07-03T00:00:00Z", "2026-07-15T00:00:00Z"),
    ]

    _setup_session(page, server)
    page.route(
        f"{server}/v1/admin/api-keys**",
        lambda route: route.fulfill(status=200, json={"keys": records}),
    )
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator("tr[data-key-id]")).to_have_count(3)

    def row_order() -> list[str]:
        return page.locator("tr[data-key-id]").evaluate_all(
            "els => els.map(el => el.dataset.keyId)"
        )

    # Default sort: name ascending.
    expect(page.locator("#sortByName")).to_have_attribute("data-sort-direction", "asc")
    assert row_order() == ["2", "3", "1"]

    page.locator("#keysSearchInput").fill("mid")
    expect(page.locator("tr[data-key-id]")).to_have_count(1)
    assert row_order() == ["3"]

    page.locator("#keysSearchInput").fill("")
    expect(page.locator("tr[data-key-id]")).to_have_count(3)

    page.locator("#sortByCreated").click()
    expect(page.locator("#sortByCreated")).to_have_attribute("data-sort-direction", "asc")
    assert row_order() == ["1", "3", "2"]

    page.locator("#sortByCreated").click()
    expect(page.locator("#sortByCreated")).to_have_attribute("data-sort-direction", "desc")
    assert row_order() == ["2", "3", "1"]

    page.locator("#sortByLastUsed").click()
    expect(page.locator("#sortByLastUsed")).to_have_attribute("data-sort-direction", "asc")
    assert row_order() == ["2", "1", "3"]
