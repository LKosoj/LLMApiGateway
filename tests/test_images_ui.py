import os

import pytest
from playwright.sync_api import Page, expect

from tests.ui_server_helpers import (
    create_authenticated_session,
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser


@pytest.fixture(scope="function")
def server():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"
        fusion_rules_path = temp_path / "models_fusion_rules.json"

        providers_path.write_text('[{"openai": {"baseUrl": "http://api.openai.com", "apikey": "key"}}]', encoding="utf-8")
        fallback_rules_path.write_text("[]", encoding="utf-8")
        operation_rules_path.write_text(
            '{"embeddings": [], "rerank": [], "images_generations": [], "images_edits": []}',
            encoding="utf-8",
        )
        fusion_rules_path.write_text("[]", encoding="utf-8")

        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = "test-key"
        env["FALLBACK_PROVIDER"] = "openai"
        env["PROVIDERS_FILENAME"] = str(providers_path)
        env["FALLBACK_RULES_FILENAME"] = str(fallback_rules_path)
        env["OPERATION_RULES_FILENAME"] = str(operation_rules_path)
        env["FUSION_RULES_FILENAME"] = str(fusion_rules_path)
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        env["LOG_LEVEL"] = "DEBUG"
        base_url = f"http://localhost:{port}"

        with isolated_gateway_process(env=env, temp_path=temp_path) as proc:
            wait_for_gateway(base_url, proc)
            yield base_url


def expand_first_card(page: Page, container_selector: str) -> None:
    card = page.locator(f"{container_selector} .rule-card").first
    card_classes = card.get_attribute("class") or ""
    if "collapsed" in card_classes.split():
        card.locator(".accordion-toggle").click()


def test_images_tab_is_visible(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")

    images_tab = page.locator("#tabImages")
    expect(images_tab).to_be_visible()
    expect(images_tab).to_have_text("Images")


def test_create_and_save_image_generation_and_edit_routes(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabImages")
    expect(page.locator("#messageArea")).to_contain_text("Images Routes loaded successfully")

    page.click("#addImageGenerationButton")
    page.fill("#imageGenerationList .gateway-model-input", "my-image-generation-model")
    page.select_option("#imageGenerationList .provider-select", "openai")
    page.fill("#imageGenerationList .model-input", "gpt-image-1")
    page.locator("#imageGenerationList details.advanced-options summary").click()
    page.select_option("#imageGenerationList .request-format-select", "openai_images")
    page.select_option("#imageGenerationList .response-format-select", "openai_images")
    page.fill("#imageGenerationList .request-mapping-input", '{"fields": {"prompt": "prompt"}}')
    page.fill("#imageGenerationList .response-mapping-input", '{"artifacts_path": "artifacts"}')

    page.click("#addImageEditButton")
    page.fill("#imageEditList .gateway-model-input", "my-image-edit-model")
    page.select_option("#imageEditList .provider-select", "openai")
    page.fill("#imageEditList .model-input", "gpt-image-1")
    page.locator("#imageEditList details.advanced-options summary").click()
    expect(page.locator("#imageEditList .request-format-select option[value='openai_images_multipart']")).to_have_count(1)
    page.select_option("#imageEditList .request-format-select", "nvidia_genai_json")
    page.select_option("#imageEditList .response-format-select", "nvidia_artifacts")
    page.fill("#imageEditList .request-mapping-input", '{"fields": {"prompt": "prompt", "image": {"from": "images[0]", "transform": "to_data_url"}}}')
    page.fill("#imageEditList .response-mapping-input", '{"artifacts_path": "artifacts", "base64_field": "base64"}')

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabImages")
    expect(page.locator("#messageArea")).to_contain_text("Images Routes loaded successfully")

    assert "collapsed" in (page.locator("#imageGenerationList .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#imageGenerationList")
    expect(page.locator("#imageGenerationList .gateway-model-input")).to_have_value("my-image-generation-model")
    expect(page.locator("#imageGenerationList .target-path-input")).to_have_value("/images/generations")
    page.locator("#imageGenerationList details.advanced-options summary").click()
    expect(page.locator("#imageGenerationList .request-format-select")).to_have_value("openai_images")
    expect(page.locator("#imageGenerationList .response-format-select")).to_have_value("openai_images")
    expect(page.locator("#imageGenerationList .request-mapping-input")).to_have_value('{\n  "fields": {\n    "prompt": "prompt"\n  }\n}')

    assert "collapsed" in (page.locator("#imageEditList .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#imageEditList")
    expect(page.locator("#imageEditList .gateway-model-input")).to_have_value("my-image-edit-model")
    expect(page.locator("#imageEditList .target-path-input")).to_have_value("/images/edits")
    page.locator("#imageEditList details.advanced-options summary").click()
    expect(page.locator("#imageEditList .request-format-select")).to_have_value("nvidia_genai_json")
    expect(page.locator("#imageEditList .response-format-select")).to_have_value("nvidia_artifacts")
    expect(page.locator("#imageEditList .response-mapping-input")).to_have_value(
        '{\n  "artifacts_path": "artifacts",\n  "base64_field": "base64"\n}'
    )
