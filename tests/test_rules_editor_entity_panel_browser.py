from __future__ import annotations

import json
import os
import re
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "editor-entity-panel-master"


def _write_config(root: Path) -> None:
    (root / "providers.json").write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"upstream-chat": {}},
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_fallback_rules.json").write_text(
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat",
                    "fallback_models": [
                        {"provider": "primary", "model": "upstream-chat"}
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    for filename, content in (
        ("models_model_rules.json", "{}\n"),
        ("models_operation_rules.json", "{}\n"),
        ("models_fusion_rules.json", "[]\n"),
        ("models_router_rules.json", "[]\n"),
    ):
        (root / filename).write_text(content, encoding="utf-8")


@pytest.fixture
def editor_server(tmp_path: Path) -> Iterator[str]:
    _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(tmp_path / "providers.json"),
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


def _open_editor(browser: Browser, base_url: str) -> Page:
    context = browser.new_context()
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/rules-editor"},
    )
    assert response.status == 200

    page = context.new_page()
    page.goto(f"{base_url}/v1/ui/rules-editor")
    expect(page.locator("#saveButton")).to_be_enabled()
    return page


def test_entity_sidebar_lists_grouped_entities_and_sticky_footer_is_visible(
    browser: Browser,
    editor_server: str,
) -> None:
    page = _open_editor(browser, editor_server)

    # Providers / Fallback / Operation / Fusion / Router / Pricing.
    expect(page.locator(".editor-entity-group")).to_have_count(6)
    expect(
        page.locator('.editor-entity-group[data-entity-group="providers"]')
    ).to_be_visible()
    expect(
        page.locator('.editor-entity-group[data-entity-group="fallback"]')
    ).to_be_visible()

    # The Save button lives inside the sticky footer and is visible without
    # scrolling the (12-panel) editor content.
    expect(page.locator("#editorFooter")).to_be_visible()
    expect(page.locator("#saveButton")).to_be_visible()
    expect(page.locator("#editorFooter")).to_have_css("position", "sticky")

    page.context.close()


def test_entity_sidebar_search_filters_items_and_shows_empty_state(
    browser: Browser,
    editor_server: str,
) -> None:
    page = _open_editor(browser, editor_server)

    search = page.locator("#entitySearchInput")
    fusion_item = page.locator('.editor-entity-item[data-entity-target="fusion"]')
    providers_item = page.locator('.editor-entity-item[data-entity-target="providers"]')

    search.fill("Providers")
    expect(providers_item).to_be_visible()
    expect(fusion_item).to_be_hidden()
    expect(page.locator("#entityListEmptyState")).to_be_hidden()

    search.fill("no such entity anywhere")
    expect(page.locator("#entityListEmptyState")).to_be_visible()

    search.fill("")
    expect(fusion_item).to_be_visible()
    expect(page.locator("#entityListEmptyState")).to_be_hidden()

    page.context.close()


def test_entity_sidebar_selection_drives_the_shared_tabs_controller(
    browser: Browser,
    editor_server: str,
) -> None:
    page = _open_editor(browser, editor_server)

    page.locator('.editor-entity-item[data-entity-target="providers"]').click()

    # The sidebar drives the same rulesTabsController.activate() API the
    # (now-removed) top tab bar used to: the panel becomes the visible one.
    expect(page.locator("#editor-container-providers")).to_be_visible()
    expect(
        page.locator('.editor-entity-item[data-entity-target="providers"]')
    ).to_have_class(re.compile(r"\bactive\b"))
    expect(page.locator("#editorFooterDocument")).to_have_text("Providers")

    page.context.close()


def test_entity_footer_validation_reflects_dirty_state(
    browser: Browser,
    editor_server: str,
) -> None:
    page = _open_editor(browser, editor_server)

    expect(page.locator("#editorFooterValidation")).to_have_text("All changes saved")

    card = page.locator("#rulesList .rule-card").first
    if "collapsed" in (card.get_attribute("class") or "").split():
        card.locator(".accordion-toggle").click()
    card.locator(".upstream-key-pool-input").first.fill("some-pool")

    expect(page.locator("#saveButton")).to_have_attribute("data-editor-dirty", "true")
    expect(page.locator("#editorFooterValidation")).to_have_text("Unsaved changes")

    page.context.close()
