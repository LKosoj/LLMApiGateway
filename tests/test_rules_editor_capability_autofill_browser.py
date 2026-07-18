from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    expect,
)

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "editor-capability-autofill-master"
FALLBACK_RULES_PATH = "/v1/config/models-rules/structured"
GATEWAY_MODEL_NAME = "llmgateway/test-model"

# Synthetic /v1/capability-autofill status: only field 0.supports_vision has a
# resolved source, matching the real service's contract that an owned field
# can lack a source entry this run (anti-regression path). The badge for
# candidate 0's supports_vision must show the "provider" qualifier; every
# other locked/unlocked field is exercised without relying on this status.
CAPABILITY_AUTOFILL_STATUS = {
    "lastRunAt": "2026-07-18T12:00:00Z",
    "lastError": None,
    "resolutions": {
        GATEWAY_MODEL_NAME: [
            {
                "index": 0,
                "provider": "primary",
                "model": "primary-model",
                "fields": {"supports_vision": {"source": "provider"}},
            },
        ],
    },
}


def _write_config(root: Path) -> None:
    (root / "providers.json").write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        # No network call needed to validate fallback-rule
                        # candidates against the provider's catalog: pinning
                        # available_models short-circuits
                        # ProviderModelsService.get_models() (see
                        # llm_gateway_core/services/provider_models.py).
                        "available_models": ["primary-model", "primary-model-2"],
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
                    "gateway_model_name": GATEWAY_MODEL_NAME,
                    "fallback_models": [
                        {
                            "provider": "primary",
                            "model": "primary-model",
                            "use_provider_order_as_fallback": False,
                            "custom_body_params": {},
                            "custom_headers": {},
                            "supports_vision": True,
                            "capabilities_autofilled": ["supports_vision"],
                        },
                        {
                            "provider": "primary",
                            "model": "primary-model-2",
                            "use_provider_order_as_fallback": False,
                            "custom_body_params": {},
                            "custom_headers": {},
                            "supports_tools": False,
                        },
                    ],
                    "rotate_models": False,
                    "dynamic_penalty": False,
                    "strip_think_tags": False,
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_operation_rules.json").write_text(
        json.dumps(
            {
                "embeddings": [],
                "rerank": [],
                "images_generations": [],
                "images_edits": [],
            }
        ),
        encoding="utf-8",
    )
    for filename, content in (
        ("models_model_rules.json", "{}\n"),
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


def _login(context: BrowserContext, base_url: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/rules-editor"},
    )
    assert response.status == 200


def _open_editor(context: BrowserContext, base_url: str) -> Page:
    page = context.new_page()

    page.route(
        "**/v1/capability-autofill",
        lambda route: route.fulfill(json=CAPABILITY_AUTOFILL_STATUS),
    )
    page.route(
        "**/v1/config/providers/primary/models",
        lambda route: route.fulfill(
            json={
                "provider": "primary",
                "models": [{"id": "primary-model"}, {"id": "primary-model-2"}],
            }
        ),
    )

    with page.expect_response(
        lambda response: response.url.endswith(FALLBACK_RULES_PATH)
        and response.request.method == "GET"
    ) as loaded_response:
        page.goto(f"{base_url}/v1/ui/rules-editor")
    assert loaded_response.value.status == 200
    expect(page.locator("#saveButton")).to_be_enabled()
    return page


def _expand_card(page: Page) -> None:
    card = page.locator("#rulesList .rule-card").first
    if "collapsed" in (card.get_attribute("class") or "").split():
        card.locator(".accordion-toggle").click()


def _open_advanced(row) -> None:
    details = row.locator(".advanced-options")
    if details.get_attribute("open") is None:
        details.locator("summary").click()


def test_capability_autofill_badges_render_edit_and_round_trip(
    browser: Browser,
    editor_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        _login(context, editor_server)
        page = _open_editor(context, editor_server)

        _expand_card(page)
        rows = page.locator("#rulesList .rule-card").first.locator(
            ".fallback-list > .fallback-row"
        )
        expect(rows).to_have_count(2)
        row_vision, row_tools = rows.nth(0), rows.nth(1)
        _open_advanced(row_vision)
        _open_advanced(row_tools)

        # (a) autofilled field renders as a read-only badge, not a select.
        vision_select = row_vision.locator(".supports-vision-select")
        expect(vision_select).to_be_hidden()
        vision_badge = row_vision.locator(
            '.capability-badge[data-capability-field="supports_vision"]'
        )
        expect(vision_badge).to_be_visible()
        expect(vision_badge.locator(".capability-badge-value")).to_have_text("Yes")
        expect(vision_badge.locator(".capability-badge-source")).to_have_text(
            "from provider catalog"
        )

        # (b) manual field stays a normal editable select.
        tools_select = row_tools.locator(".supports-tools-select")
        expect(tools_select).to_be_visible()
        expect(tools_select).to_have_value("false")
        expect(
            row_tools.locator('.capability-badge[data-capability-field="supports_tools"]')
        ).to_have_count(0)

        # (d.1) save without touching anything: capabilities_autofilled round-trips.
        with page.expect_response(
            lambda response: response.url.endswith(FALLBACK_RULES_PATH)
            and response.request.method == "POST"
        ) as first_save:
            page.locator("#saveButton").click()
        assert first_save.value.status == 200
        first_rules = first_save.value.json()["rules"]
        first_candidate = first_rules[0]["fallback_models"][0]
        assert first_candidate["supports_vision"] is True
        assert first_candidate["capabilities_autofilled"] == ["supports_vision"]

        # (c) clicking "Edit" turns the badge back into a control, save re-rendered
        # the rule card so re-locate it.
        _expand_card(page)
        rows = page.locator("#rulesList .rule-card").first.locator(
            ".fallback-list > .fallback-row"
        )
        row_vision = rows.nth(0)
        _open_advanced(row_vision)
        vision_badge = row_vision.locator(
            '.capability-badge[data-capability-field="supports_vision"]'
        )
        expect(vision_badge).to_be_visible()
        vision_badge.locator(".capability-badge-edit").click()
        vision_select = row_vision.locator(".supports-vision-select")
        expect(vision_select).to_be_visible()
        expect(vision_select).to_have_value("true")
        expect(vision_badge).to_be_hidden()

        # (d.2) save after the override: the field is excluded from the list.
        with page.expect_response(
            lambda response: response.url.endswith(FALLBACK_RULES_PATH)
            and response.request.method == "POST"
        ) as second_save:
            page.locator("#saveButton").click()
        assert second_save.value.status == 200
        second_rules = second_save.value.json()["rules"]
        second_candidate = second_rules[0]["fallback_models"][0]
        assert second_candidate["supports_vision"] is True
        assert "capabilities_autofilled" not in second_candidate
    finally:
        context.close()
