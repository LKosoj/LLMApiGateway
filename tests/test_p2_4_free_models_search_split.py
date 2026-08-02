"""Browser-level coverage for P2-4: search + Configured/External split on the
Free LLM Catalog page (/v1/ui/free-models).

Both backend responses the page depends on are injected via `page.route(...)`
so the real `api.freellmapi.co` fetch and the real `providers.json` never run
(the process fixture also defaults `FREE_LLM_CATALOG_ENABLED=false`, see
`tests/ui_server_helpers.py`):

  * `GET /v1/api/free-models` -- the free-tier catalog snapshot.
  * `GET /v1/config/providers/structured` -- the configured-provider list the
    page uses to compute which catalog models are already configured.
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
MASTER_KEY = "p2-4-free-models-search-split-master"

# 2 models configured in providers.json ("gpt-oss-120b", "llama-3.3-70b"),
# 3 external-only models known only to the freellmapi.co catalog.
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
                    "limits": {"rpm": 30, "rpd": None, "tpm": 60000, "tpd": None},
                    "monthlyTokenBudget": "~50M",
                    "contextWindow": 65536,
                    "supportsVision": False,
                    "supportsTools": True,
                    "quirks": [],
                },
                {
                    "modelId": "llama-3.3-70b",
                    "displayName": "Llama 3.3 70B",
                    "limits": {"rpm": 30, "rpd": 14_400, "tpm": 60_000, "tpd": 1_000_000},
                    "monthlyTokenBudget": "~30M",
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
                    "modelId": "gpt-4o-mini-free",
                    "displayName": "GPT-4o Mini Free",
                    "limits": {"rpm": 30, "rpd": 14_400, "tpm": None, "tpd": None},
                    "monthlyTokenBudget": "~40M",
                    "contextWindow": 131072,
                    "supportsVision": False,
                    "supportsTools": False,
                    "quirks": [],
                },
                {
                    "modelId": "mixtral-8x7b",
                    "displayName": "Mixtral 8x7B",
                    "limits": {"rpm": 30, "rpd": None, "tpm": None, "tpd": None},
                    "monthlyTokenBudget": "~35M",
                    "contextWindow": 32768,
                    "supportsVision": False,
                    "supportsTools": False,
                    "quirks": [],
                },
                {
                    "modelId": "qwen-2.5-72b",
                    "displayName": "Qwen 2.5 72B",
                    "limits": {"rpm": 30, "rpd": None, "tpm": None, "tpd": None},
                    "monthlyTokenBudget": "~35M",
                    "contextWindow": 32768,
                    "supportsVision": False,
                    "supportsTools": False,
                    "quirks": [],
                },
            ],
        },
    ],
}

STRUCTURED_PROVIDERS_RESPONSE = {
    "providers": [
        {
            "name": "primary",
            "baseUrl": "https://primary.invalid/v1",
            "models": {
                "gpt-oss-120b": {"input_rate": 0, "output_rate": 0},
                "llama-3.3-70b": {"input_rate": 0, "output_rate": 0},
            },
        },
    ],
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


def _open_page(context: BrowserContext, base_url: str) -> Page:
    _login(context, base_url)
    page = context.new_page()
    page.route("**/v1/api/free-models", lambda route: route.fulfill(json=FAKE_SNAPSHOT))
    page.route(
        "**/v1/config/providers/structured",
        lambda route: route.fulfill(json=STRUCTURED_PROVIDERS_RESPONSE),
    )
    with page.expect_response(
        lambda response: response.url.endswith("/v1/api/free-models")
    ) as loaded:
        page.goto(f"{base_url}/v1/ui/free-models")
    assert loaded.value.status == 200
    return page


def test_catalog_splits_into_configured_and_external_sections_with_counts(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server)

        configured_section = page.locator('[data-free-models-section="configured"]')
        external_section = page.locator('[data-free-models-section="external"]')
        expect(configured_section).to_be_visible()
        expect(external_section).to_be_visible()
        expect(configured_section.locator(".free-models-section-heading")).to_have_text(
            "Configured (2)"
        )
        expect(external_section.locator(".free-models-section-heading")).to_have_text(
            "External (3)"
        )
        expect(configured_section.locator(".free-models-model")).to_have_count(2)
        expect(external_section.locator(".free-models-model")).to_have_count(3)

        # Each rendered model row carries the matching configured/external badge.
        expect(
            configured_section.locator('[data-free-models-model-id="gpt-oss-120b"] .badge-configured')
        ).to_have_count(1)
        expect(
            external_section.locator('[data-free-models-model-id="mixtral-8x7b"] .badge-external')
        ).to_have_count(1)
    finally:
        context.close()


def test_search_filters_visible_model_cards_across_both_sections(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server)

        page.locator("#free-models-search").fill("gpt")
        # The search is debounced (200ms); wait for the filtered result.
        expect(page.locator(".free-models-model")).to_have_count(2)
        expect(page.locator('[data-free-models-model-id="gpt-oss-120b"]')).to_be_visible()
        expect(page.locator('[data-free-models-model-id="gpt-4o-mini-free"]')).to_be_visible()
        expect(page.locator('[data-free-models-model-id="llama-3.3-70b"]')).to_have_count(0)

        expect(page.locator('[data-free-models-section="configured"] .free-models-section-heading')).to_have_text(
            "Configured (1)"
        )
        expect(page.locator('[data-free-models-section="external"] .free-models-section-heading')).to_have_text(
            "External (1)"
        )

        # Clearing the search restores the full catalog.
        page.locator("#free-models-search-clear").click()
        expect(page.locator(".free-models-model")).to_have_count(5)
    finally:
        context.close()


def test_status_filter_radio_shows_only_configured_models(
    browser: Browser,
    free_models_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        page = _open_page(context, free_models_server)

        page.locator('input[name="free-models-status-filter"][value="configured"]').check()

        expect(page.locator('[data-free-models-section="configured"]')).to_be_visible()
        expect(page.locator('[data-free-models-section="external"]')).to_have_count(0)
        expect(page.locator(".free-models-model")).to_have_count(2)

        page.locator('input[name="free-models-status-filter"][value="external"]').check()
        expect(page.locator('[data-free-models-section="configured"]')).to_have_count(0)
        expect(page.locator('[data-free-models-section="external"]')).to_be_visible()
        expect(page.locator(".free-models-model")).to_have_count(3)

        page.locator('input[name="free-models-status-filter"][value="all"]').check()
        expect(page.locator(".free-models-model")).to_have_count(5)
    finally:
        context.close()


def test_virtual_key_session_never_requests_master_only_provider_config(
    browser: Browser,
    free_models_server: str,
) -> None:
    """`/v1/config/*` is master-only: a virtual-key session must not ask for it.

    The request would answer 403, which the browser reports as a console error
    even though the page treats the unknown result as "render one unsplit list".
    """
    master_context = browser.new_context(locale="en-US")
    try:
        _login(master_context, free_models_server)
        created = master_context.request.post(
            f"{free_models_server}/v1/admin/api-keys",
            data={"name": "p2-4-free-models-virtual"},
        )
        assert created.status == 201, created.text()
        virtual_key = str(created.json()["api_key"])
    finally:
        master_context.close()

    context = browser.new_context(locale="en-US")
    try:
        response = context.request.post(
            f"{free_models_server}/auth/login",
            data={"api_key": virtual_key, "next": "/v1/ui/free-models"},
        )
        assert response.status == 200

        page = context.new_page()
        console_errors: list[str] = []
        page.on(
            "console",
            lambda message: console_errors.append(message.text)
            if message.type == "error"
            else None,
        )
        config_requests: list[str] = []
        page.on(
            "request",
            lambda request: config_requests.append(request.url)
            if "/v1/config/" in request.url
            else None,
        )
        page.route("**/v1/api/free-models", lambda route: route.fulfill(json=FAKE_SNAPSHOT))

        with page.expect_response(
            lambda response: response.url.endswith("/v1/api/free-models")
        ) as loaded:
            page.goto(f"{free_models_server}/v1/ui/free-models")
        assert loaded.value.status == 200

        # Without the configured-model list the catalog renders unsplit.
        expect(page.locator(".free-models-model")).to_have_count(5)
        expect(page.locator('[data-free-models-section="configured"]')).to_have_count(0)
        assert config_requests == []
        assert console_errors == []
    finally:
        context.close()
