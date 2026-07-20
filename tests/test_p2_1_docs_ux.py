"""Browser-level coverage for P2-1: Docs quickstart + copy-to-clipboard +
sticky table of contents on the Gateway Docs page.

Verifies:
  * the quickstart section (#docs-quickstart) renders with several cards;
  * clicking a code example's copy button copies its text and switches the
    button's accessible label to the localized "Copied!" state;
  * the section table of contents (`[data-docs-toc]`) becomes a sticky
    side column on desktop viewports (>=1024px) and stays a plain,
    non-sticky block on narrower viewports.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "p2-1-docs-ux-master"


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
                }
            ]
        ),
        encoding="utf-8",
    )
    sources = {
        "models_fallback_rules.json": "[]\n",
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": json.dumps(
            {
                "embeddings": [],
                "rerank": [],
                "images_generations": [],
                "images_edits": [],
            }
        ),
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def docs_server(tmp_path: Path) -> Iterator[str]:
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


def _login(
    browser: Browser,
    base_url: str,
    *,
    viewport: dict[str, int],
) -> BrowserContext:
    context = browser.new_context(viewport=viewport)
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/docs"},
    )
    assert response.status == 200
    return context


def test_docs_quickstart_section_has_multiple_cards(
    browser: Browser,
    docs_server: str,
) -> None:
    context = _login(browser, docs_server, viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(f"{docs_server}/v1/ui/docs")

        quickstart = page.locator("#docs-quickstart")
        expect(quickstart).to_be_visible()
        cards = quickstart.locator(".quickstart-card")
        assert cards.count() >= 3
        for index in range(cards.count()):
            expect(cards.nth(index)).to_be_visible()
    finally:
        context.close()


def test_docs_copy_button_copies_code_and_shows_copied_state(
    browser: Browser,
    docs_server: str,
) -> None:
    context = _login(browser, docs_server, viewport={"width": 1280, "height": 900})
    try:
        context.grant_permissions(["clipboard-read", "clipboard-write"])
        page = context.new_page()
        page.goto(f"{docs_server}/v1/ui/docs")

        copy_button = page.locator(".btn-copy").first
        expect(copy_button).to_be_visible()
        code_element = copy_button.locator(
            "xpath=ancestor::*[contains(@class, 'docs-code-block')][1]//pre/code"
        )
        expected_text = code_element.inner_text()

        copy_button.click()
        expect(copy_button).to_have_attribute("aria-label", "Copied!")
        expect(copy_button).to_have_attribute("title", "Copied!")
        assert copy_button.evaluate("(el) => el.classList.contains('copied')")

        clipboard_text = page.evaluate("navigator.clipboard.readText()")
        assert clipboard_text == expected_text

        # Base attribute state is restored afterwards.
        expect(copy_button).to_have_attribute("aria-label", "Copy", timeout=3000)
        expect(copy_button).to_have_attribute("title", "Copy")
        assert not copy_button.evaluate("(el) => el.classList.contains('copied')")
    finally:
        context.close()


def test_docs_toc_is_sticky_on_desktop_viewport(
    browser: Browser,
    docs_server: str,
) -> None:
    context = _login(browser, docs_server, viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.goto(f"{docs_server}/v1/ui/docs")

        toc = page.locator("[data-docs-toc]")
        expect(toc).to_be_visible()
        position = toc.evaluate("(el) => getComputedStyle(el).position")
        assert position == "sticky"
    finally:
        context.close()


def test_docs_toc_is_not_sticky_on_mobile_viewport(
    browser: Browser,
    docs_server: str,
) -> None:
    context = _login(browser, docs_server, viewport={"width": 390, "height": 844})
    try:
        page = context.new_page()
        page.goto(f"{docs_server}/v1/ui/docs")

        toc = page.locator("[data-docs-toc]")
        expect(toc).to_be_attached()
        position = toc.evaluate("(el) => getComputedStyle(el).position")
        assert position != "sticky"
    finally:
        context.close()
