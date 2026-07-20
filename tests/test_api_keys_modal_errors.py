"""Browser coverage for P0-1: API Keys modal-local field/server errors.

``static/api-keys.js::saveKey()`` used to route field validation errors and
save-request errors to the page-level ``#messageArea``. That element sits
behind the ``inert`` background while the create/edit dialog is open, so the
message was present in the DOM but invisible and unreachable to the user.
These errors must render inside the dialog instead: field errors next to the
offending input (with ``aria-invalid``/``aria-describedby`` and focus moved
to the field), and save-request errors in a ``role="alert"`` banner inside
the dialog.
"""

from __future__ import annotations

from collections import Counter

import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (
    provider_mock,
    server,
)
from tests.ui_server_helpers import create_authenticated_session


__all__ = ["provider_mock", "server"]
pytestmark = pytest.mark.browser


def _open_create_modal(page: Page, server: str) -> None:
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
    page.route(
        f"{server}/v1/admin/api-keys**",
        lambda route: route.fulfill(status=200, json={"keys": []}),
    )
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator(".api-keys-empty")).to_be_visible()
    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()


def test_field_validation_error_renders_inside_dialog_and_focuses_field(
    page: Page, server: str
) -> None:
    _open_create_modal(page, server)

    page.locator("#saveKeyBtn").click()

    name_error = page.locator("#fieldNameError")
    expect(name_error).to_have_text("Name is required.")
    expect(page.locator("#fieldName")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#fieldName")).to_have_attribute(
        "aria-describedby", "fieldNameError"
    )
    expect(page.locator("#fieldName")).to_be_focused()
    # The global message area sits behind the inert page background while
    # the modal is open, so it must stay untouched.
    expect(page.locator("#messageArea")).to_have_text("")

    # Fix the name and trigger a second, different field error: the first
    # field's error state must be cleared, not just visually overwritten.
    # (RPM/TPM are native <input type="number">, which browsers refuse to
    # fill with non-numeric text, so metadata is used to exercise a second,
    # independent field-error target.)
    page.locator("#fieldName").fill("team-a")
    page.locator("#fieldMetadata").fill("{not valid json")
    page.locator("#saveKeyBtn").click()

    # The raw JSON.parse() message is appended in parentheses and is engine
    # dependent, so only assert the stable, translated prefix plus the fact
    # that a raw detail was appended at all (regression guard: this detail
    # used to render in the page-level messageDetail node and must not be
    # silently dropped now that errors render inside the dialog).
    metadata_error = page.locator("#fieldMetadataError")
    expect(metadata_error).to_contain_text("Metadata must contain valid JSON.")
    expect(metadata_error).to_contain_text("(")
    expect(page.locator("#fieldMetadata")).to_have_attribute("aria-invalid", "true")
    expect(page.locator("#fieldMetadata")).to_have_attribute(
        "aria-describedby", "fieldMetadataError"
    )
    expect(page.locator("#fieldMetadata")).to_be_focused()
    expect(page.locator("#fieldName")).not_to_have_attribute("aria-invalid", "true")
    expect(name_error).to_be_hidden()


def test_server_rejection_renders_as_alert_inside_dialog(
    page: Page, server: str
) -> None:
    counters: Counter = Counter()

    def api_keys_route(route) -> None:
        if route.request.method == "POST":
            counters["post"] += 1
            route.fulfill(status=400, json={"detail": "duplicate name"})
            return
        route.fulfill(status=200, json={"keys": []})

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
    page.route(f"{server}/v1/admin/api-keys**", api_keys_route)
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(status=200, json={"data": []}),
    )
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator(".api-keys-empty")).to_be_visible()
    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()

    page.locator("#fieldName").fill("team-a")
    page.locator("#saveKeyBtn").click()

    modal_error = page.locator("#modalError")
    expect(modal_error).to_contain_text("Request failed (HTTP 400).")
    expect(modal_error).to_have_attribute("role", "alert")
    expect(page.locator("#modalErrorDetail")).to_have_text("duplicate name")
    expect(page.locator("#modalErrorDetail")).to_have_attribute("lang", "und")
    expect(page.locator("#modalErrorDetail")).to_have_attribute("dir", "auto")
    expect(page.locator("#messageArea")).to_have_text("")
    expect(page.locator("#keyModal")).to_be_visible()
    assert counters["post"] == 1


def test_reopening_modal_clears_stale_field_error(page: Page, server: str) -> None:
    _open_create_modal(page, server)

    page.locator("#saveKeyBtn").click()
    expect(page.locator("#fieldNameError")).to_have_text("Name is required.")

    page.locator("#cancelKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_hidden()

    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()
    expect(page.locator("#fieldNameError")).to_be_hidden()
    expect(page.locator("#fieldName")).not_to_have_attribute("aria-invalid", "true")
