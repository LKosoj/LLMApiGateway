import json
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
FALLBACK_ETAG = f'"fallback_rules:sha256:{"a" * 64}"'

@pytest.fixture(scope="function")
def server():
    # Start the server in a separate process
    # Use a temporary directory for config files to avoid side effects
    import tempfile
    from pathlib import Path
    
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"
        fusion_rules_path = temp_path / "models_fusion_rules.json"
        
        providers_path.write_text('[{"openai": {"baseUrl": "http://api.openai.com", "apikey": "key"}}, {"anthropic": {"baseUrl": "http://api.anthropic.com", "apikey": "key"}}]', encoding="utf-8")
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

def test_embeddings_tab_is_visible(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    
    # Check if Embeddings tab exists
    embeddings_tab = page.locator('[data-entity-target="embeddings"]')
    expect(embeddings_tab).to_be_visible()
    expect(embeddings_tab).to_have_text("Embeddings")

def test_create_and_save_embedding_route(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    # Mock providers response to have some providers
    page.route("**/v1/config/models-rules/structured", lambda route: route.fulfill(
        json={"rules": [], "providers": ["openai", "anthropic"]},
        headers={"ETag": FALLBACK_ETAG},
    ))
    # Mock models response for openai
    page.route("**/v1/config/providers/openai/models", lambda route: route.fulfill(
        json={"provider": "openai", "models": [{"id": "text-embedding-3-small"}]}
    ))
    
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')
    
    # Wait for loading to finish
    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")
    
    # Click Add Embedding Model
    page.click("#addEmbeddingButton")
    expect(page.locator("#editor-container-embeddings .add-fallback-button")).to_have_text("Add Fallback Route")
    
    # Fill gateway model name
    page.fill("#editor-container-embeddings .gateway-model-input", "my-emb-model")
    
    # Select provider
    page.select_option("#editor-container-embeddings .provider-select", "openai")
    
    # Wait for models to load and select model
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-small")
    
    # Save
    page.click("#saveButton")
    
    # Check success message
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

def test_validation_error_display(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')
    
    # Wait for loading to finish
    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")
    
    # Add a rule with empty name
    page.click("#addEmbeddingButton")
    page.fill("#editor-container-embeddings .gateway-model-input", "")
    
    # Save
    page.click("#saveButton")
    
    # Check error message (client side validation)
    expect(page.locator("#messageArea")).to_have_class("error")
    expect(page.locator("#messageArea")).to_contain_text("The configuration is invalid")
    expect(page.locator("#messageRawDetail")).to_contain_text("gateway model name")

def test_delete_embedding_route(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')
    
    # Add a route
    page.click("#addEmbeddingButton")
    expect(page.locator("#embeddingsList .rule-card")).to_have_count(1)
    
    # Delete it
    page.click("text=Remove Model")
    expect(page.locator("#embeddingsList .rule-card")).to_have_count(0)


def test_edit_embedding_route(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')

    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")

    page.click("#addEmbeddingButton")
    page.fill("#editor-container-embeddings .gateway-model-input", "my-emb-model")
    page.select_option("#editor-container-embeddings .provider-select", "openai")
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-small")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.fill("#editor-container-embeddings .gateway-model-input", "my-emb-model-edited")
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-large")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click('[data-entity-target="embeddings"]')
    assert "collapsed" in (page.locator("#editor-container-embeddings .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-embeddings")
    expect(page.locator("#editor-container-embeddings .gateway-model-input")).to_have_value("my-emb-model-edited")
    expect(page.locator("#editor-container-embeddings .model-input")).to_have_value("text-embedding-3-large")


def test_create_and_save_embedding_route_with_retry_settings(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')
    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")

    page.click("#addEmbeddingButton")
    page.fill("#editor-container-embeddings .gateway-model-input", "my-emb-retry-model")
    page.select_option("#editor-container-embeddings .provider-select", "openai")
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-small")

    page.locator("#editor-container-embeddings details.advanced-options summary").click()
    page.fill("#editor-container-embeddings .retry-delay-input", "0.5")
    page.fill("#editor-container-embeddings .retry-count-input", "2")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click('[data-entity-target="embeddings"]')
    assert "collapsed" in (page.locator("#editor-container-embeddings .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#editor-container-embeddings")
    page.locator("#editor-container-embeddings details.advanced-options summary").click()
    expect(page.locator("#editor-container-embeddings .retry-delay-input")).to_have_value("0.5")
    expect(page.locator("#editor-container-embeddings .retry-count-input")).to_have_value("2")


def test_saving_embeddings_uses_loaded_operation_base_and_if_match(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.route("**/v1/config/models-rules/structured", lambda route: route.fulfill(
        json={"rules": [], "providers": ["openai"]},
        headers={"ETag": FALLBACK_ETAG},
    ))
    page.route("**/v1/config/providers/openai/models", lambda route: route.fulfill(
        json={"provider": "openai", "models": [{"id": "text-embedding-3-small"}]}
    ))

    stale_payload = {
        "embeddings": [],
        "rerank": [{"gateway_model_name": "old-rerank", "routes": [{"provider": "openai", "model": "old"}]}],
        "images_generations": [],
        "images_edits": [],
    }
    request_count = {"get": 0}
    posted_payload = {}
    posted_if_match = {"value": None}
    base_etag = f'"operation_rules:sha256:{"b" * 64}"'
    next_etag = f'"operation_rules:sha256:{"c" * 64}"'

    def handle_operation_rules(route):
        if route.request.method == "GET":
            request_count["get"] += 1
            route.fulfill(json=stale_payload, headers={"ETag": base_etag})
            return
        posted_payload.update(json.loads(route.request.post_data or "{}"))
        posted_if_match["value"] = route.request.headers.get("if-match")
        route.fulfill(
            json={"message": "Operation rules updated successfully.", **posted_payload},
            headers={"ETag": next_etag},
        )

    page.route("**/v1/config/model-operations/structured", handle_operation_rules)

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click('[data-entity-target="embeddings"]')
    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")

    page.click("#addEmbeddingButton")
    page.fill("#editor-container-embeddings .gateway-model-input", "my-emb-model")
    page.select_option("#editor-container-embeddings .provider-select", "openai")
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-small")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    assert request_count["get"] == 1
    assert posted_if_match["value"] == base_etag
    assert posted_payload["rerank"][0]["gateway_model_name"] == "old-rerank"
