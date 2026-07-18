"""Browser-level tests for the free-tier LLM catalog page (/v1/ui/free-models).

The backend (`llm_gateway_core/services/free_llm_catalog.py`) is exercised
elsewhere; these tests only cover client-side rendering of a snapshot served
by `GET /v1/api/free-models`. The response is injected via `page.route(...)`
so the real `api.freellmapi.co` fetch never runs (also guarded by
`isolated_gateway_process` defaulting `FREE_LLM_CATALOG_ENABLED=false`).
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
MASTER_KEY = "free-models-browser-master"

FAKE_SNAPSHOT = {
    "updatedAt": "2026-07-19T08:00:00Z",
    "sourceVersion": "2026.07.18",
    "sourceGeneratedAt": "2026-07-18T07:03:02.234Z",
    "lastError": None,
    "minMonthlyTokens": 30_000_000,
    "providers": [
        {
            "id": "cerebras",
            "name": "Cerebras",
            "models": [
                {
                    "modelId": "gpt-oss-120b",
                    "displayName": "GPT-OSS 120B",
                    "intelligenceRank": 5,
                    "speedRank": 1,
                    "sizeLabel": "Large",
                    "limits": {"rpm": 30, "rpd": None, "tpm": 60000, "tpd": None},
                    "monthlyTokenBudget": "~50M",
                    "monthlyTokenBudgetFloor": 50_000_000,
                    "contextWindow": 65536,
                    "supportsVision": False,
                    "supportsTools": True,
                    "quirks": [
                        {
                            "slug": "no-system-prompt",
                            "title": "No system prompt support",
                            "body": "This model ignores the system role; fold instructions into the user turn.",
                            "severity": "warning",
                        },
                    ],
                },
                {
                    "modelId": "llama-3.3-70b",
                    "displayName": "Llama 3.3 70B",
                    "intelligenceRank": 4,
                    "speedRank": 2,
                    "sizeLabel": "Large",
                    "limits": {"rpm": 30, "rpd": 14_400, "tpm": 60_000, "tpd": 1_000_000},
                    "monthlyTokenBudget": "~30M",
                    "monthlyTokenBudgetFloor": 30_000_000,
                    "contextWindow": 131072,
                    "supportsVision": True,
                    "supportsTools": False,
                    "quirks": [],
                },
            ],
        },
        {
            "id": "groq",
            "name": "Groq",
            "models": [
                {
                    "modelId": "llama-3.1-8b-instant",
                    "displayName": "Llama 3.1 8B Instant",
                    "intelligenceRank": 2,
                    "speedRank": 5,
                    "sizeLabel": "Small",
                    "limits": {"rpm": 30, "rpd": 14_400, "tpm": None, "tpd": None},
                    "monthlyTokenBudget": "~40M",
                    "monthlyTokenBudgetFloor": 40_000_000,
                    "contextWindow": 131072,
                    "supportsVision": False,
                    "supportsTools": False,
                    "quirks": [],
                },
            ],
        },
    ],
}

UNAVAILABLE_SNAPSHOT = {
    "updatedAt": None,
    "sourceVersion": None,
    "sourceGeneratedAt": None,
    "lastError": "freellmapi.co request timed out after 10s",
    "minMonthlyTokens": 30_000_000,
    "providers": [],
}

STALE_SNAPSHOT = {
    **FAKE_SNAPSHOT,
    "lastError": "freellmapi.co returned HTTP 503",
}

EMPTY_SNAPSHOT = {
    **FAKE_SNAPSHOT,
    "providers": [],
}


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps([{"primary": {"baseUrl": "https://primary.invalid/v1", "apikey": "test-only-key"}}]),
        encoding="utf-8",
    )
    sources = {
        "models_fallback_rules.json": "[]\n",
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def free_models_server(tmp_path: Path) -> Iterator[str]:
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


def _login(context: BrowserContext, base_url: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/free-models"},
    )
    assert response.status == 200


def _open_page(context: BrowserContext, base_url: str, payload: dict) -> Page:
    _login(context, base_url)
    page = context.new_page()
    page.route("**/v1/api/free-models", lambda route: route.fulfill(json=payload))
    with page.expect_response(
        lambda response: response.url.endswith("/v1/api/free-models")
    ) as loaded:
        page.goto(f"{base_url}/v1/ui/free-models")
    assert loaded.value.status == 200
    return page


def test_providers_and_models_render_in_payload_order(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, FAKE_SNAPSHOT)
        expect(page.locator("#free-models-list")).to_have_attribute(
            "data-free-models-state", "ready"
        )
        expect(page.locator(".free-models-provider-name")).to_have_text(["Cerebras", "Groq"])
        first_provider_models = page.locator(".free-models-provider").nth(0).locator(
            ".free-models-model-name"
        )
        expect(first_provider_models).to_have_text(["GPT-OSS 120B", "Llama 3.3 70B"])
    finally:
        context.close()


def test_only_populated_limits_are_rendered(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, FAKE_SNAPSHOT)

        rpm_tpm_only = page.locator('[data-free-models-model-id="gpt-oss-120b"] .free-models-limits')
        rpm_tpm_text = rpm_tpm_only.inner_text()
        assert "RPM" in rpm_tpm_text
        assert "TPM" in rpm_tpm_text
        assert "RPD" not in rpm_tpm_text
        assert "TPD" not in rpm_tpm_text

        rpm_rpd_only = page.locator(
            '[data-free-models-model-id="llama-3.1-8b-instant"] .free-models-limits'
        )
        rpm_rpd_text = rpm_rpd_only.inner_text()
        assert "RPM" in rpm_rpd_text
        assert "RPD" in rpm_rpd_text
        assert "TPM" not in rpm_rpd_text
        assert "TPD" not in rpm_rpd_text
    finally:
        context.close()


def test_quirks_details_toggle_open_and_closed(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, FAKE_SNAPSHOT)
        details = page.locator('[data-free-models-model-id="gpt-oss-120b"] .free-models-quirks')

        assert details.get_attribute("open") is None
        details.locator("summary").click()
        assert details.get_attribute("open") is not None
        details.locator("summary").click()
        assert details.get_attribute("open") is None
    finally:
        context.close()


def test_vision_and_tools_badges_render_only_for_supported_models(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, FAKE_SNAPSHOT)

        vision_only = page.locator('[data-free-models-model-id="llama-3.3-70b"] .free-models-badge')
        expect(vision_only).to_have_text(["Vision"])

        tools_only = page.locator('[data-free-models-model-id="gpt-oss-120b"] .free-models-badge')
        expect(tools_only).to_have_text(["Tools"])

        neither = page.locator('[data-free-models-model-id="llama-3.1-8b-instant"] .free-models-badge')
        expect(neither).to_have_count(0)
    finally:
        context.close()


def test_unavailable_state_shows_message_and_error_without_rendering_list(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, UNAVAILABLE_SNAPSHOT)

        expect(page.locator("#free-models-list")).to_have_attribute(
            "data-free-models-state", "unavailable"
        )
        expect(page.locator('[data-free-models-field="state-message"]')).to_have_text(
            "Free-tier catalog data is unavailable."
        )
        expect(page.locator(".free-models-provider")).to_have_count(0)
        expect(page.locator("#free-models-error-detail")).to_have_text(
            UNAVAILABLE_SNAPSHOT["lastError"]
        )
    finally:
        context.close()


def test_empty_state_shows_threshold_message_without_error(
    browser: Browser,
    free_models_server: str,
) -> None:
    # A successful snapshot with no surviving models (e.g. an overly high
    # FREE_LLM_CATALOG_MIN_MONTHLY_TOKENS) is its own state: no providers,
    # no error, no retry button.
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, EMPTY_SNAPSHOT)

        expect(page.locator("#free-models-list")).to_have_attribute(
            "data-free-models-state", "empty"
        )
        expect(page.locator('[data-free-models-field="state-message"]')).to_have_text(
            "No free-tier models currently clear the monthly token threshold."
        )
        expect(page.locator(".free-models-provider")).to_have_count(0)
        expect(page.locator("#free-models-error-detail")).to_have_text("")
        expect(page.locator("#free-models-retry")).to_be_hidden()
    finally:
        context.close()


def test_stale_state_shows_data_with_as_of_marker_and_error(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server, STALE_SNAPSHOT)

        expect(page.locator("#free-models-list")).to_have_attribute(
            "data-free-models-state", "stale"
        )
        expect(page.locator(".free-models-provider")).to_have_count(
            len(STALE_SNAPSHOT["providers"])
        )
        expect(page.locator("#free-models-status")).to_contain_text("as of")
        expect(page.locator("#free-models-error-detail")).to_have_text(
            STALE_SNAPSHOT["lastError"]
        )
    finally:
        context.close()
