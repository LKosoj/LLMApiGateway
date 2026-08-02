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
    # Start the server in a separate process
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

def test_rerank_tab_is_visible(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    
    # Check if Rerank tab exists
    rerank_tab = page.locator('[data-entity-target="rerank"]')
    expect(rerank_tab).to_be_visible()
    expect(rerank_tab).to_have_text("Rerank")

def test_create_and_save_rerank_route(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="rerank"]')
    
    # Wait for loading to finish
    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")
    
    # Click Add Rerank Model
    page.click("#addRerankButton")
    expect(page.locator("#editor-container-rerank .add-fallback-button")).to_have_text("Add Fallback Route")
    
    # Fill gateway model name
    page.fill("#editor-container-rerank .gateway-model-input", "my-rerank-model")
    
    # Select provider
    page.select_option("#editor-container-rerank .provider-select", "openai")
    
    # Wait for models to load and select model (or manual entry)
    page.fill("#editor-container-rerank .model-input", "bge-reranker-v2-m3")
    
    # Target path should be editable and default to /score
    target_path_input = page.locator("#editor-container-rerank .target-path-input")
    expect(target_path_input).to_have_value("/score")
    page.fill("#editor-container-rerank .target-path-input", "/rerank")
    
    # Save
    page.click("#saveButton")
    
    # Check success message
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")
    
    # Reload page and check if data persists
    page.reload()
    page.click('[data-entity-target="rerank"]')
    assert "collapsed" in (page.locator("#editor-container-rerank .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-rerank")
    expect(page.locator("#editor-container-rerank .gateway-model-input")).to_have_value("my-rerank-model")
    expect(page.locator("#editor-container-rerank .target-path-input")).to_have_value("/rerank")

def test_create_and_save_rerank_route_with_absolute_target_url(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="rerank"]')

    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")

    page.click("#addRerankButton")
    page.fill("#editor-container-rerank .gateway-model-input", "my-absolute-rerank-model")
    page.select_option("#editor-container-rerank .provider-select", "openai")
    page.fill("#editor-container-rerank .model-input", "bge-reranker-v2-m3")

    target_path_input = page.locator("#editor-container-rerank .target-path-input")
    target_path_input.fill("https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking")
    page.locator("#editor-container-rerank details.advanced-options summary").click()
    expect(page.locator("#editor-container-rerank .request-format-select option[value='query_texts']")).to_have_count(1)
    expect(page.locator("#editor-container-rerank .response-format-select option[value='scores']")).to_have_count(1)
    page.select_option("#editor-container-rerank .request-format-select", "query_passages")
    page.select_option("#editor-container-rerank .response-format-select", "rankings_logit")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click('[data-entity-target="rerank"]')
    assert "collapsed" in (page.locator("#editor-container-rerank .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-rerank")
    expect(page.locator("#editor-container-rerank .gateway-model-input")).to_have_value("my-absolute-rerank-model")
    expect(page.locator("#editor-container-rerank .target-path-input")).to_have_value(
        "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking"
    )
    page.locator("#editor-container-rerank details.advanced-options summary").click()
    expect(page.locator("#editor-container-rerank .request-format-select")).to_have_value("query_passages")
    expect(page.locator("#editor-container-rerank .response-format-select")).to_have_value("rankings_logit")

def test_rerank_validation_error_invalid_target_path(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="rerank"]')
    
    # Wait for loading to finish
    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")
    
    page.click("#addRerankButton")
    
    # Fill required fields
    page.fill("#editor-container-rerank .gateway-model-input", "my-rerank-model")
    page.select_option("#editor-container-rerank .provider-select", "openai")
    page.fill("#editor-container-rerank .model-input", "some-model")
    
    # Invalid target path (does not start with /)
    page.fill("#editor-container-rerank .target-path-input", "score")
    
    # Save
    page.click("#saveButton")
    
    # Check error message (client side validation)
    expect(page.locator("#messageArea")).to_have_class("error")
    expect(page.locator("#messageArea")).to_contain_text("The configuration is invalid")
    expect(page.locator("#messageRawDetail")).to_contain_text(
        "Target path must start with / or with http:// or https://"
    )

    # Invalid route must not persist after reload
    page.reload()
    page.click('[data-entity-target="rerank"]')
    expect(page.locator("#rerankList .rule-card")).to_have_count(0)


def test_create_and_save_rerank_route_with_retry_settings(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="rerank"]')
    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")

    page.click("#addRerankButton")
    page.fill("#editor-container-rerank .gateway-model-input", "my-rerank-retry-model")
    page.select_option("#editor-container-rerank .provider-select", "openai")
    page.fill("#editor-container-rerank .model-input", "bge-reranker-v2-m3")

    page.locator("#editor-container-rerank details.advanced-options summary").click()
    page.fill("#editor-container-rerank .retry-delay-input", "4")
    page.fill("#editor-container-rerank .retry-count-input", "1")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click('[data-entity-target="rerank"]')
    assert "collapsed" in (page.locator("#editor-container-rerank .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-rerank")
    page.locator("#editor-container-rerank details.advanced-options summary").click()
    expect(page.locator("#editor-container-rerank .retry-delay-input")).to_have_value("4")
    expect(page.locator("#editor-container-rerank .retry-count-input")).to_have_value("1")


def test_create_and_save_rerank_route_with_jina_output_format(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="rerank"]')
    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")

    page.click("#addRerankButton")
    page.fill("#editor-container-rerank .gateway-model-input", "my-rerank-jina-output-model")
    page.select_option("#editor-container-rerank .provider-select", "openai")
    page.fill("#editor-container-rerank .model-input", "bge-reranker-v2-m3")

    page.locator("#editor-container-rerank details.advanced-options summary").click()
    page.select_option("#editor-container-rerank .response-output-format-select", "jina_results")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click('[data-entity-target="rerank"]')
    assert "collapsed" in (page.locator("#editor-container-rerank .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-rerank")
    page.locator("#editor-container-rerank details.advanced-options summary").click()
    expect(page.locator("#editor-container-rerank .response-output-format-select")).to_have_value("jina_results")
