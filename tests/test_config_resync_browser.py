"""Browser coverage for the out-of-sync notice and its resync button.

An out-of-band edit to any of the six configuration files leaves the running
process with a snapshot the disk no longer matches, and every writer then
answers 409 ``config_sources_out_of_sync``. Reloading the page does not help —
the GET serves the same in-memory snapshot — so the conflict block has to offer
a resync instead, and the button has to actually adopt the disk.
"""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "resync-browser-master"


def _write_config(root: Path) -> tuple[Path, Path]:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {
                            "chat": {
                                "context_length": 131072,
                                "input_rate": 1.0,
                                "output_rate": 2.0,
                            }
                        },
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    fallback_path = root / "models_fallback_rules.json"
    fallback_path.write_text("[]\n", encoding="utf-8")
    for filename, content in (
        ("models_model_rules.json", "{}\n"),
        ("models_operation_rules.json", "{}\n"),
        ("models_fusion_rules.json", "[]\n"),
        ("models_router_rules.json", "[]\n"),
    ):
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path, fallback_path


@pytest.fixture
def resync_server(tmp_path: Path) -> Iterator[tuple[str, Path, Path]]:
    providers_path, fallback_path = _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
            "FALLBACK_RULES_FILENAME": str(fallback_path),
            "MODEL_RULES_FILENAME": str(tmp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(
                tmp_path / "models_operation_rules.json"
            ),
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
        yield base_url, providers_path, fallback_path


def _login(context: BrowserContext, base_url: str, next_path: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": next_path},
    )
    assert response.status == 200


def _drift_fallback_rules(fallback_path: Path) -> None:
    """Rewrite a non-target source out of band with valid but different bytes."""
    fallback_path.write_text(
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat",
                    "fallback_models": [{"provider": "primary", "model": "chat"}],
                }
            ]
        ),
        encoding="utf-8",
    )


def _open_pricing(context: BrowserContext, base_url: str) -> Page:
    _login(context, base_url, "/v1/ui/pricing")
    page = context.new_page()
    page.goto(f"{base_url}/v1/ui/pricing")
    expect(page.locator("#pricingTableBody tr")).to_have_count(1)
    expect(page.locator("#addRowBtn")).to_be_enabled()
    return page


def test_pricing_out_of_sync_notice_resyncs_and_then_saves(
    browser: Browser,
    resync_server: tuple[str, Path, Path],
) -> None:
    base_url, providers_path, fallback_path = resync_server
    context = browser.new_context(locale="en-US")
    try:
        page = _open_pricing(context, base_url)
        _drift_fallback_rules(fallback_path)

        rate_input = page.locator("#pricingTableBody tr input").nth(2)
        rate_input.fill("5")
        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as blocked_save:
            page.locator("#saveBtn").click()
        assert blocked_save.value.status == 409
        assert blocked_save.value.json()["detail"]["code"] == (
            "config_sources_out_of_sync"
        )

        conflict = page.locator("#pricingConflictState")
        expect(conflict).to_be_visible()
        expect(page.locator("#pricingConflictTitle")).to_have_text(
            "Configuration on disk changed"
        )
        expect(page.locator("#reloadPricingBtn")).to_have_text(
            "Reload configuration from disk"
        )

        with page.expect_response(
            lambda response: response.url.endswith("/v1/config/resync")
        ) as resync_response:
            page.locator("#reloadPricingBtn").click()
        assert resync_response.value.status == 200
        expect(conflict).to_be_hidden()

        rate_input.fill("6")
        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as saved:
            page.locator("#saveBtn").click()
        assert saved.value.status == 200
    finally:
        context.close()

    written = json.loads(providers_path.read_text(encoding="utf-8"))
    assert written[0]["primary"]["models"]["chat"]["input_rate"] == 6.0
    # The resync only republishes the runtime; the drifted file stays as the
    # out-of-band editor wrote it.
    assert json.loads(fallback_path.read_text(encoding="utf-8"))[0][
        "gateway_model_name"
    ] == "gateway/chat"


def test_pricing_drift_of_the_edited_file_is_not_a_reload_loop(
    browser: Browser,
    resync_server: tuple[str, Path, Path],
) -> None:
    """Editing providers.json out of band must offer a resync, not a reload.

    This is the shape users hit in practice: a deploy or a script rewrites the
    very file the page edits. Reloading re-reads the same in-memory snapshot,
    so the next save is rejected identically — only the resync breaks out.
    """
    base_url, providers_path, _fallback_path = resync_server
    context = browser.new_context(locale="en-US")
    try:
        page = _open_pricing(context, base_url)
        drifted = json.loads(providers_path.read_text(encoding="utf-8"))
        drifted[0]["primary"]["models"]["chat"]["output_rate"] = 9.0
        providers_path.write_text(json.dumps(drifted), encoding="utf-8")

        rate_input = page.locator("#pricingTableBody tr input").nth(2)
        for attempt_value in ("5", "7"):
            rate_input.fill(attempt_value)
            with page.expect_response(
                lambda response: (
                    response.url.endswith("/v1/admin/pricing")
                    and response.request.method == "PUT"
                )
            ) as blocked_save:
                page.locator("#saveBtn").click()
            assert blocked_save.value.status == 409
            assert blocked_save.value.json()["detail"]["code"] == (
                "config_sources_out_of_sync"
            )

        expect(page.locator("#pricingConflictTitle")).to_have_text(
            "Configuration on disk changed"
        )
        with page.expect_response(
            lambda response: response.url.endswith("/v1/config/resync")
        ) as resync_response:
            page.locator("#reloadPricingBtn").click()
        assert resync_response.value.status == 200

        rate_input.fill("8")
        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as saved:
            page.locator("#saveBtn").click()
        assert saved.value.status == 200
    finally:
        context.close()

    written = json.loads(providers_path.read_text(encoding="utf-8"))[0]["primary"]
    assert written["models"]["chat"]["input_rate"] == 8.0
    # The resync adopted the out-of-band bytes, so the save preserves them.
    assert written["models"]["chat"]["output_rate"] == 9.0


def test_editor_out_of_sync_notice_resyncs_and_then_saves(
    browser: Browser,
    resync_server: tuple[str, Path, Path],
) -> None:
    base_url, _providers_path, fallback_path = resync_server
    context = browser.new_context(locale="en-US")
    try:
        _login(context, base_url, "/v1/ui/rules-editor")
        page = context.new_page()
        page.goto(f"{base_url}/v1/ui/rules-editor")
        page.locator('[data-entity-target="providers"]').click()
        expect(page.locator("#providersList .provider-card")).to_have_count(1)

        # The drift is in a neighbour of the document being saved: the editor's
        # own providers revision is still current, so this is the out-of-sync
        # case rather than a plain revision conflict.
        _drift_fallback_rules(fallback_path)

        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/config/providers/structured")
                and response.request.method == "POST"
            )
        ) as blocked_save:
            page.locator("#saveButton").click()
        assert blocked_save.value.status == 409
        assert blocked_save.value.json()["detail"]["code"] == (
            "config_sources_out_of_sync"
        )

        conflict = page.locator("#editorConflictState")
        expect(conflict).to_be_visible()
        expect(page.locator("#editorConflictTitle")).to_have_text(
            "Configuration on disk changed"
        )
        expect(page.locator("#reloadEditorDocumentButton")).to_have_text(
            "Reload configuration from disk"
        )

        with page.expect_response(
            lambda response: response.url.endswith("/v1/config/resync")
        ) as resync_response:
            page.locator("#reloadEditorDocumentButton").click()
        assert resync_response.value.status == 200
        expect(conflict).to_be_hidden()

        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/config/providers/structured")
                and response.request.method == "POST"
            )
        ) as saved:
            page.locator("#saveButton").click()
        assert saved.value.status == 200
    finally:
        context.close()
