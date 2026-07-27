"""Browser coverage for the Playground Chat provider-model source.

Runs against a real gateway process with ``/v1/chat/completions`` mocked at the
browser network layer, and verifies that:
  * switching "Model type" to provider models reveals the provider and
    provider-model selectors and loads the provider catalog;
  * sending a message in that mode pins the upstream with the
    ``X-LLMGateway-Provider`` header and sends the provider model id verbatim;
  * switching back to gateway models drops the header again.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Route, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "playground-provider-chat-master"
PROVIDER_MODELS = ["direct-model-a", "direct-model-b"]


def _sse_body(text: str) -> str:
    return (
        f"data: {json.dumps({'choices': [{'delta': {'content': text}}]})}\n\n"
        "data: [DONE]\n\n"
    )


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"chat": {"input_rate": 1.0, "output_rate": 2.0}},
                    }
                },
                {
                    "secondary": {
                        "baseUrl": "https://secondary.invalid/v1",
                        "apikey": "test-only-key",
                        # Pinned catalog: the models endpoint answers from config
                        # instead of calling the (unreachable) upstream.
                        "available_models": PROVIDER_MODELS,
                    }
                },
            ]
        ),
        encoding="utf-8",
    )
    sources = {
        "models_fallback_rules.json": json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat-stream",
                    "fallback_models": [
                        {"provider": "primary", "model": "chat"},
                    ],
                    "rotate_models": False,
                }
            ]
        ),
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def provider_chat_server(tmp_path: Path) -> Iterator[str]:
    providers_path = _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
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


def _login(browser: Browser, base_url: str) -> BrowserContext:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/playground"},
    )
    assert response.status == 200
    return context


def _open_chat_tab(page, base_url: str) -> None:
    page.goto(f"{base_url}/v1/ui/playground")
    page.locator('[data-playground-section-tab="chat"]').click()
    expect(page.locator("#simpleChatForm")).to_be_visible()


def _route_completion(page, captured: list[dict]) -> None:
    def handle(route: Route) -> None:
        captured.append(
            {
                "headers": route.request.headers,
                "body": route.request.post_data_json,
            }
        )
        route.fulfill(
            status=200,
            headers={"content-type": "text/event-stream"},
            body=_sse_body("provider reply"),
        )

    page.route("**/v1/chat/completions", handle)


def test_provider_source_reveals_provider_catalog(
    browser: Browser,
    provider_chat_server: str,
) -> None:
    context = _login(browser, provider_chat_server)
    try:
        page = context.new_page()
        _open_chat_tab(page, provider_chat_server)

        # Gateway models are the default: provider controls stay hidden.
        expect(page.locator("#chatProviderGroup")).to_be_hidden()
        expect(page.locator("#chatModel")).to_be_visible()

        page.locator("#chatModelSource").select_option("provider")

        expect(page.locator("#chatGatewayModelGroup")).to_be_hidden()
        expect(page.locator("#chatProviderGroup")).to_be_visible()
        expect(page.locator("#chatProviderHint")).to_be_visible()
        expect(page.locator("#chatProvider")).to_have_value("primary")

        page.locator("#chatProvider").select_option("secondary")
        # The catalog is pinned in providers.json, so it resolves without an
        # upstream call and both models become selectable.
        expect(page.locator("#chatProviderModel")).to_have_value(
            PROVIDER_MODELS[0], timeout=5000
        )
        options = page.locator("#chatProviderModel option").all_inner_texts()
        assert options == PROVIDER_MODELS
    finally:
        context.close()


def test_provider_message_pins_provider_header(
    browser: Browser,
    provider_chat_server: str,
) -> None:
    context = _login(browser, provider_chat_server)
    try:
        page = context.new_page()
        _open_chat_tab(page, provider_chat_server)
        captured: list[dict] = []
        _route_completion(page, captured)

        page.locator("#chatModelSource").select_option("provider")
        page.locator("#chatProvider").select_option("secondary")
        expect(page.locator("#chatProviderModel")).to_have_value(
            PROVIDER_MODELS[0], timeout=5000
        )
        page.locator("#chatProviderModel").select_option(PROVIDER_MODELS[1])

        page.locator("#simpleChatInput").fill("ping provider")
        page.locator("#simpleChatForm .run-button").click()

        expect(page.locator(".chat-message.assistant .chat-content")).to_contain_text(
            "provider reply", timeout=5000
        )
        assert len(captured) == 1
        assert captured[0]["headers"].get("x-llmgateway-provider") == "secondary"
        assert captured[0]["body"]["model"] == PROVIDER_MODELS[1]

        # Back to gateway models: the request must not pin a provider anymore.
        page.locator("#chatModelSource").select_option("gateway")
        expect(page.locator("#chatModel")).to_be_visible()
        page.locator("#simpleChatInput").fill("ping gateway")
        page.locator("#simpleChatForm .run-button").click()

        expect(page.locator(".chat-message.assistant").nth(1)).to_contain_text(
            "provider reply", timeout=5000
        )
        assert len(captured) == 2
        assert "x-llmgateway-provider" not in captured[1]["headers"]
        assert captured[1]["body"]["model"] == "gateway/chat-stream"
    finally:
        context.close()
