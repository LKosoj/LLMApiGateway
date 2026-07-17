from __future__ import annotations

import json
import os
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
MASTER_KEY = "editor-errors-master"
RULES_PATH = "/v1/config/models-rules/structured"


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


def _open_rules_tab(browser: Browser, base_url: str) -> Page:
    context = browser.new_context()
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/rules-editor"},
    )
    assert response.status == 200

    page = context.new_page()
    page.goto(f"{base_url}/v1/ui/rules-editor")
    expect(page.locator("#saveButton")).to_be_enabled()
    page.locator("#tabRules").click()
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    return page


def test_a_rejected_fallback_rule_names_the_gateway_model_it_belongs_to(
    browser: Browser,
    editor_server: str,
) -> None:
    page = _open_rules_tab(browser, editor_server)
    card = page.locator("#rulesList .rule-card").first
    if "collapsed" in (card.get_attribute("class") or "").split():
        card.locator(".accordion-toggle").click()

    card.locator(".upstream-key-pool-input").first.fill("ghost-pool")
    with page.expect_response(
        lambda response: response.url.endswith(RULES_PATH)
        and response.request.method == "POST"
    ) as saved:
        page.locator("#saveButton").click()

    assert saved.value.status == 400
    expect(page.locator("#messageRawDetail")).to_contain_text(
        "Invalid upstream_key_pool 'ghost-pool' used in fallback rule for "
        "'gateway/chat' (provider: primary). Pool not found in provider "
        "configuration."
    )
    page.context.close()
