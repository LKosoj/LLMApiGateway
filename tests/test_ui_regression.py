import json
import hashlib
import hmac
import os
import secrets
import subprocess
import threading
import time
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.ui_server_helpers import get_free_port, wait_for_gateway

def build_session_signature(issued_at: int, expires_at: int, nonce: str, gateway_api_key: str) -> str:
    secret = gateway_api_key.encode("utf-8")
    payload = f"{issued_at}.{expires_at}.{nonce}.master.".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()

def create_authenticated_session(gateway_api_key: str) -> str:
    issued_at = int(time.time())
    expires_at = issued_at + 365 * 24 * 60 * 60
    nonce = secrets.token_urlsafe(24)
    signature = build_session_signature(issued_at, expires_at, nonce, gateway_api_key)
    return f"{issued_at}.{expires_at}.{nonce}.master..{signature}"


def _empty_analytics_dashboard():
    return {
        "filters": {"bucket": "day"},
        "totals": {"requests": 0},
        "series": {"usage": []},
        "breakdowns": {"providers": [], "resolved_targets": [], "api_keys": []},
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }


def _route_empty_analytics_dashboard(page: Page, server: str):
    page.route(
        f"{server}/v1/api/analytics-dashboard*",
        lambda route: route.fulfill(json=_empty_analytics_dashboard()),
    )


def test_rules_editor_guards_dirty_state_before_unload():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "function isCurrentEditorDirty()" in content
    assert "window.addEventListener('beforeunload'" in content
    assert "event.preventDefault()" in content
    assert "event.returnValue = ''" in content


def test_rules_editor_ignores_stale_provider_model_loads():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "fallbackRowModelRequestSeq" in content
    assert "fallbackRow.dataset.modelsRequestToken = loadToken" in content
    assert "fallbackRow.dataset.modelsRequestToken !== loadToken" in content


def test_rules_editor_stops_eval_polling_when_switching_tabs():
    content = Path("static/editor.js").read_text(encoding="utf-8")

    assert "function stopOpenRouterFreePolling()" in content
    assert "function stopFallbackEvalPolling()" in content
    assert "if (activeEditor !== 'openrouter-free')" in content
    assert "if (activeEditor !== 'fallback-eval')" in content
    assert "previousEditor === 'openrouter-free'" in content
    assert "previousEditor === 'fallback-eval'" in content


def test_model_rules_raw_textarea_has_stable_rows():
    content = Path("static/rules-editor.html").read_text(encoding="utf-8")

    assert 'id="modelRulesRawInput"' in content
    assert 'rows="18"' in content


def test_quota_rerender_clears_countdown_timers_and_has_empty_cta():
    js_content = Path("static/quota.js").read_text(encoding="utf-8")
    css_content = Path("static/quota.css").read_text(encoding="utf-8")

    assert "function clearCountdownTimers()" in js_content
    assert "clearCountdownTimers();" in js_content
    assert 'href="/v1/ui/api-keys"' in js_content
    assert ".quota-empty a" in css_content


def test_translator_debug_has_no_inline_event_handlers():
    html_content = Path("static/translator-debug.html").read_text(encoding="utf-8")
    js_content = Path("static/translator-debug.js").read_text(encoding="utf-8")

    assert "onclick=" not in html_content
    assert "onclick=" not in js_content
    assert 'addEventListener("click"' in js_content
    assert "copyStep(copyButton)" in js_content


def test_web_playground_mobile_layout_and_labels_are_clean():
    html_content = Path("static/web-playground.html").read_text(encoding="utf-8")
    css_content = Path("static/web-playground.css").read_text(encoding="utf-8")

    assert "Include images[]" not in html_content
    assert 'class="primary-button run-button">Run search</button>' in html_content
    assert "@media (max-width: 480px)" in css_content
    assert "min-width: 620px" not in css_content


def test_usage_stats_empty_state_is_not_duplicated_by_status_message():
    content = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "showMessage('No data available for the selected period.'" not in content


def test_api_keys_modal_has_escape_and_focus_trap():
    content = Path("static/api-keys.js").read_text(encoding="utf-8")

    assert 'event.key === "Escape"' in content
    assert 'event.key !== "Tab"' in content
    assert "getModalFocusableElements" in content


class MockProviderHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/models":
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            response = {
                "data": [
                    {"id": "gpt-4o"},
                    {"id": "text-embedding-3-small"},
                    {"id": "rerank-v3.5"},
                    {"id": "gpt-image-1"}
                ]
            }
            self.wfile.write(json.dumps(response).encode("utf-8"))
        else:
            self.send_response(404)
            self.end_headers()
    def log_message(self, format, *args):
        return

@pytest.fixture(scope="function")
def provider_mock():
    server = HTTPServer(("localhost", 0), MockProviderHandler)
    port = server.server_address[1]
    thread = threading.Thread(target=server.serve_forever)
    thread.daemon = True
    thread.start()
    yield f"http://localhost:{port}"
    server.shutdown()

def _serve_gateway(provider_mock, fallback_rules_content="[]"):
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"
        fusion_rules_path = temp_path / "models_fusion_rules.json"
        router_rules_path = temp_path / "models_router_rules.json"
        
        provider_config = json.dumps([{"openai": {"baseUrl": provider_mock, "apikey": "key"}}])
        providers_path.write_text(provider_config, encoding="utf-8")
        fallback_rules_path.write_text("[]", encoding="utf-8")
        operation_rules_path.write_text(
            '{"embeddings": [], "rerank": [], "images_generations": [], "images_edits": []}',
            encoding="utf-8",
        )
        fusion_rules_path.write_text("[]", encoding="utf-8")
        router_rules_path.write_text("[]", encoding="utf-8")
        
        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = "test-key"
        env["FALLBACK_PROVIDER"] = "openai"
        env["PROVIDERS_FILENAME"] = str(providers_path)
        env["FALLBACK_RULES_FILENAME"] = str(fallback_rules_path)
        env["OPERATION_RULES_FILENAME"] = str(operation_rules_path)
        env["FUSION_RULES_FILENAME"] = str(fusion_rules_path)
        env["ROUTER_RULES_FILENAME"] = str(router_rules_path)
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        env["LOG_LEVEL"] = "DEBUG"
        base_url = f"http://localhost:{port}"

        proc = subprocess.Popen(
            ["./.venv/bin/python", "main.py"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True
        )

        wait_for_gateway(base_url, proc)

        yield base_url
        
        proc.terminate()
        proc.wait()

@pytest.fixture(scope="function")
def server(provider_mock):
    yield from _serve_gateway(provider_mock)

def test_rules_editor_tabs_display(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    
    expect(page.locator("#tabRules")).to_be_visible()
    expect(page.locator("#tabEmbeddings")).to_be_visible()
    expect(page.locator("#tabRerank")).to_be_visible()
    expect(page.locator("#tabImages")).to_be_visible()
    expect(page.locator("#tabAudio")).to_be_visible()
    expect(page.locator("#tabRouter")).to_be_visible()
    expect(page.locator("#tabProviders")).to_be_visible()

def test_fallback_rules_still_work(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    
    # Wait for initial load
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    
    # Add a new rule
    page.click("#addRuleButton")
    
    # Fill gateway model name
    page.fill(".gateway-model-input", "my-gateway-model")
    
    # Select provider
    page.select_option(".provider-select", "openai")
    
    # Wait for models to load and select model
    # The models are loaded from our provider_mock
    page.wait_for_selector(".model-select:not([disabled])")
    page.select_option(".model-select", "gpt-4o")
    
    # Save
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")
    
    # Reload and verify
    page.reload()
    expect(page.locator(".gateway-model-input")).to_have_value("my-gateway-model")


def test_providers_editor_structured_ui_roundtrip(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabProviders")

    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    expect(page.locator("#providersList .CodeMirror")).to_have_count(0)

    page.click("#addProviderButton")
    provider_card = page.locator("#providersList .provider-card").last
    provider_card.locator(".provider-name-input").fill("anthropic_local")
    provider_card.locator(".provider-base-url-input").fill("https://anthropic.local")
    expect(provider_card.locator(".provider-api-key-input")).to_have_attribute("type", "password")
    provider_card.locator(".provider-api-key-input").fill("DIRECT-KEY")
    provider_card.locator(".provider-api-key-field button", has_text="Show").click()
    expect(provider_card.locator(".provider-api-key-input")).to_have_attribute("type", "text")
    provider_card.locator(".provider-api-key-field button", has_text="Hide").click()
    expect(provider_card.locator(".provider-api-key-input")).to_have_attribute("type", "password")
    provider_card.locator(".provider-type-select").select_option("anthropic")
    provider_card.locator(".provider-proxy-input").fill("http://proxy.local:8080")
    expect(provider_card.locator(".provider-api-key-field .field-tooltip-button")).to_have_count(1)
    expect(provider_card.locator(".provider-api-key-field .field-tooltip-popover")).to_contain_text("${VAR_NAME}")
    provider_card.locator("details.advanced-options summary").click()
    expect(provider_card.locator(".upstream-limits-section")).to_have_count(1)
    expect(provider_card.locator(".upstream-limits-empty")).to_be_visible()
    provider_card.locator(".upstream-limit-add").click()
    upstream_row = provider_card.locator(".upstream-limit-row").first
    upstream_row.locator(".upstream-limit-model").fill("deepseek/deepseek-r1:free")
    upstream_row.locator(".upstream-limit-rpm").fill("20")
    upstream_row.locator(".upstream-limit-rpd").fill("200")
    upstream_row.locator(".upstream-limit-tpm").fill("60000")
    upstream_row.locator(".upstream-limit-tpd").fill("1000000")
    provider_card.locator(".provider-models-input").fill('{"deepseek/deepseek-r1:free":{"pricing":{"input":0.1}}}')

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabProviders")
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#providersList .provider-card")).to_have_count(2)
    saved_provider = page.locator("#providersList .provider-card").nth(1)
    expect(saved_provider.locator(".provider-name-input")).to_have_value("anthropic_local")
    expect(saved_provider.locator(".provider-base-url-input")).to_have_value("https://anthropic.local")
    expect(saved_provider.locator(".provider-type-select")).to_have_value("anthropic")
    expect(saved_provider.locator(".provider-proxy-input")).to_have_value("http://proxy.local:8080")
    saved_provider.locator(".accordion-toggle").first.click()
    saved_provider.locator("details.advanced-options summary").click()
    saved_row = saved_provider.locator(".upstream-limit-row").first
    expect(saved_row.locator(".upstream-limit-model")).to_have_value("deepseek/deepseek-r1:free")
    expect(saved_row.locator(".upstream-limit-rpm")).to_have_value("20")
    expect(saved_row.locator(".upstream-limit-rpd")).to_have_value("200")
    expect(saved_row.locator(".upstream-limit-tpm")).to_have_value("60000")
    expect(saved_row.locator(".upstream-limit-tpd")).to_have_value("1000000")
    expect(saved_provider.locator(".provider-models-input")).to_have_value(
        '{\n  "deepseek/deepseek-r1:free": {\n    "pricing": {\n      "input": 0.1\n    }\n  }\n}'
    )


def test_providers_editor_load_failure_disables_save_and_add_until_retry(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    provider_get_count = 0
    provider_post_count = 0

    def handle_structured_providers(route):
        nonlocal provider_get_count, provider_post_count
        if route.request.method == "POST":
            provider_post_count += 1
            route.fulfill(status=500, json={"detail": "unexpected save"})
            return

        provider_get_count += 1
        if provider_get_count == 1:
            route.fulfill(status=500, json={"detail": "structured providers failed"})
            return

        route.fulfill(
            json={
                "providers": [
                    {
                        "name": "openai",
                        "baseUrl": "http://api.openai.test",
                        "apikey": "key",
                        "type": "openai",
                    }
                ]
            }
        )

    page.route("**/v1/config/providers/structured", handle_structured_providers)

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabProviders")

    expect(page.locator("#messageArea")).to_contain_text("Error loading Providers")
    expect(page.locator("#saveButton")).to_be_disabled()
    expect(page.locator("#addProviderButton")).to_be_disabled()
    expect(page.locator("#providersList .provider-card")).to_have_count(0)

    page.locator("#saveButton").dispatch_event("click")
    expect(page.locator("#messageArea")).to_contain_text("Cannot save Providers")
    page.wait_for_timeout(100)
    assert provider_post_count == 0

    page.locator("#addProviderButton").dispatch_event("click")
    expect(page.locator("#messageArea")).to_contain_text("Cannot add Provider")
    expect(page.locator("#providersList .provider-card")).to_have_count(0)

    page.click("#tabProviders")

    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    assert provider_get_count == 2
    assert provider_post_count == 0


def test_async_providers_load_does_not_enable_save_on_other_tab(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    delayed_provider_route = {}
    rules_save_route = {}

    def handle_structured_providers(route):
        if route.request.method == "GET" and not delayed_provider_route:
            delayed_provider_route["route"] = route
            return
        route.continue_()

    def handle_rules(route):
        if route.request.method == "POST":
            rules_save_route["route"] = route
            return
        route.continue_()

    page.route("**/v1/config/providers/structured", handle_structured_providers)
    page.route("**/v1/config/models-rules/structured", handle_rules)

    page.goto(f"{server}/v1/ui/rules-editor")
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    page.click("#tabRules")
    expect(page.locator("#saveButton")).to_be_enabled()

    page.click("#saveButton")
    expect(page.locator("#saveButton")).to_be_disabled()
    assert "route" in rules_save_route

    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    page.click("#tabRules")
    expect(page.locator("#saveButton")).to_be_disabled()

    delayed_provider_route["route"].fulfill(
        json={
            "providers": [
                {
                    "name": "openai",
                    "baseUrl": "http://api.openai.test",
                    "apikey": "key",
                    "type": "openai",
                }
            ]
        }
    )
    page.wait_for_timeout(100)
    expect(page.locator("#saveButton")).to_be_disabled()

    rules_save_route["route"].fulfill(json={"message": "Fallback Rules updated successfully."})
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")
    expect(page.locator("#saveButton")).to_be_enabled()


def test_providers_editor_ignores_stale_load_responses(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    provider_get_routes = []

    def handle_structured_providers(route):
        if route.request.method == "GET":
            provider_get_routes.append(route)
            return
        route.continue_()

    page.route("**/v1/config/providers/structured", handle_structured_providers)

    page.goto(f"{server}/v1/ui/rules-editor")
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")

    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    page.click("#tabProviders")
    page.wait_for_timeout(100)
    assert len(provider_get_routes) == 1

    page.click("#tabRules")
    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    assert len(provider_get_routes) == 2

    provider_get_routes[1].fulfill(
        json={
            "providers": [
                {
                    "name": "openai",
                    "baseUrl": "http://api.openai.test",
                    "apikey": "key",
                    "type": "openai",
                }
            ]
        }
    )
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()

    provider_get_routes[0].fulfill(status=500, json={"detail": "stale providers failure"})
    page.wait_for_timeout(100)

    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#messageArea")).not_to_contain_text("stale providers failure")
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()


def _build_master_session_cookie_value(gateway_api_key: str) -> str:
    """Builds a 6-part master session cookie matching the current server-side
    HMAC payload. The legacy 4-part helper above does not include role/key_id
    in the signature payload and is incompatible with the current auth
    middleware (`_build_session_signature` mixes role + key_id into payload).
    """
    issued_at = int(time.time())
    expires_at = issued_at + 365 * 24 * 60 * 60
    nonce = secrets.token_urlsafe(24)
    role = "master"
    key_id_token = ""
    secret = gateway_api_key.encode("utf-8")
    payload = f"{issued_at}.{expires_at}.{nonce}.{role}.{key_id_token}".encode("utf-8")
    signature = hmac.new(secret, payload, hashlib.sha256).hexdigest()
    return f"{issued_at}.{expires_at}.{nonce}.{role}.{key_id_token}.{signature}"


def test_fallback_rules_max_total_attempts_and_use_provider_order_persist(page: Page, server):
    """End-to-end UI round-trip: enter max_total_attempts (rule-level chain
    budget) and use_provider_order_as_fallback (per-fallback toggle) in the
    structured editor, save, reload — both fields must come back preserved.
    Regression for H7: _build_structured_rules_response previously dropped
    max_total_attempts, so a UI round-trip silently zeroed the chain budget."""
    session = _build_master_session_cookie_value("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#addRuleButton")
    page.fill(".gateway-model-input", "chain-budget-model")
    page.fill(".max-total-attempts-input", "5")
    page.select_option(".provider-select", "openai")
    page.wait_for_selector(".model-select:not([disabled])")
    page.select_option(".model-select", "gpt-4o")
    page.locator(".fallback-row details.advanced-options summary").first.click()
    page.check(".use-provider-order-checkbox")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    rule_card = page.locator(".rule-card").first
    if "collapsed" in (rule_card.get_attribute("class") or "").split():
        rule_card.locator(".accordion-toggle").click()
    expect(page.locator(".gateway-model-input")).to_have_value("chain-budget-model")
    expect(page.locator(".max-total-attempts-input")).to_have_value("5")
    page.locator(".fallback-row details.advanced-options summary").first.click()
    expect(page.locator(".use-provider-order-checkbox")).to_be_checked()


def test_fallback_rules_dynamic_penalty_toggle_persists(page: Page, server):
    """Round-trip for dynamic_penalty: the toggle must come back checked after
    save+reload. Regression for a path where _build_fallback_rules_config
    dropped dynamic_penalty from the in-memory rule_config, so the GET endpoint
    re-served False even when the file on disk had true."""
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#addRuleButton")
    page.fill(".gateway-model-input", "dynamic-penalty-model")
    page.select_option(".provider-select", "openai")
    page.wait_for_selector(".model-select:not([disabled])")
    page.select_option(".model-select", "gpt-4o")
    page.check(".dynamic-penalty-checkbox")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    expect(page.locator(".gateway-model-input")).to_have_value("dynamic-penalty-model")
    expect(page.locator(".dynamic-penalty-checkbox")).to_be_checked()


def test_fallback_rules_strip_think_tags_toggle_persists(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#addRuleButton")
    page.fill(".gateway-model-input", "my-thinkless-model")
    page.select_option(".provider-select", "openai")
    page.wait_for_selector(".model-select:not([disabled])")
    page.select_option(".model-select", "gpt-4o")
    page.check(".strip-think-tags-checkbox")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    expect(page.locator(".gateway-model-input")).to_have_value("my-thinkless-model")
    expect(page.locator(".strip-think-tags-checkbox")).to_be_checked()

def test_fallback_rules_loads_with_unavailable_models_highlighted(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )

    page.route(
        "**/v1/config/models-rules/structured",
        lambda route: route.fulfill(
            json={
                "rules": [
                    {
                        "gateway_model_name": "my-gateway-model",
                        "rotate_models": False,
                        "fallback_models": [
                            {"provider": "openai", "model": "missing-model-1"},
                            {"provider": "openai", "model": "missing-model-2"},
                        ],
                    }
                ],
                "providers": ["openai"],
            }
        ),
    )
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "gpt-4o"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded with warnings")
    expect(page.locator("#messageArea")).to_contain_text("Unavailable fallback models")
    # Unavailable fallbacks are grouped under their gateway model as provider.model.
    expect(page.locator("#messageArea")).to_contain_text("gateway model 'my-gateway-model'")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-1")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-2")
    expect(page.locator(".gateway-model-input")).to_have_value("my-gateway-model")
    expect(page.locator(".model-status").first).to_contain_text("missing-model-1")
    expect(page.locator(".model-status").nth(1)).to_contain_text("missing-model-2")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("Cannot save Fallback Rules")
    expect(page.locator("#messageArea")).to_contain_text("gateway model 'my-gateway-model'")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-1")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-2")


def test_fallback_rules_unavailable_model_can_be_replaced(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )

    def handle_rules(route):
        if route.request.method == "POST":
            route.fulfill(json={"message": "Fallback Rules updated successfully."})
        else:
            route.fulfill(
                json={
                    "rules": [
                        {
                            "gateway_model_name": "my-gateway-model",
                            "rotate_models": False,
                            "fallback_models": [
                                {"provider": "openai", "model": "missing-model-1"},
                            ],
                        }
                    ],
                    "providers": ["openai"],
                }
            )

    page.route("**/v1/config/models-rules/structured", handle_rules)
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "gpt-4o"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded with warnings")
    expect(page.locator(".model-status").first).to_contain_text("is not available")

    # Loaded rule cards are collapsed by default; expand to reach the select.
    page.click(".accordion-toggle")

    # Picking an available model must clear the stale "unavailable" error...
    page.select_option(".model-select", "gpt-4o")
    expect(page.locator(".model-status").first).to_contain_text("selected")
    expect(page.locator(".model-status").first).not_to_contain_text("is not available")

    # ...so the save is no longer blocked and succeeds.
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")


def test_fallback_rules_unavailable_models_grouped_by_gateway_model(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )

    # The same unavailable provider/model is referenced by two different gateway
    # models; the warning must attribute it to each gateway model instead of
    # repeating "z.ai.glm-5.1" without saying where it comes from.
    page.route(
        "**/v1/config/models-rules/structured",
        lambda route: route.fulfill(
            json={
                "rules": [
                    {
                        "gateway_model_name": "llmgateway/high",
                        "rotate_models": False,
                        "fallback_models": [
                            {"provider": "z.ai", "model": "glm-5.1"},
                        ],
                    },
                    {
                        "gateway_model_name": "llmgateway/low",
                        "rotate_models": False,
                        "fallback_models": [
                            {"provider": "z.ai", "model": "glm-5.1"},
                        ],
                    },
                ],
                "providers": ["z.ai"],
            }
        ),
    )
    page.route(
        "**/v1/config/providers/z.ai/models",
        lambda route: route.fulfill(
            json={"provider": "z.ai", "models": [{"id": "glm-4.6"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")

    message = page.locator("#messageArea")
    expect(message).to_contain_text("Fallback Rules loaded with warnings")
    expect(message).to_contain_text("gateway model 'llmgateway/high': z.ai.glm-5.1")
    expect(message).to_contain_text("gateway model 'llmgateway/low': z.ai.glm-5.1")


def test_usage_stats_page_loads(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    
    page.goto(f"{server}/v1/ui/usage-stats")
    
    expect(page.locator("h1")).to_contain_text("Usage Statistics")
    expect(page.locator("#analyticsTabContent")).to_be_visible()
    expect(page.locator("#analyticsDashboard")).to_be_visible()

def test_usage_stats_page_renders_operation_column(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/usage-stats/*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                [
                    {
                        "time_period": "2026-03-17",
                        "gateway_model": "gateway/embed-small",
                        "operation": "embeddings",
                        "provider": "openai",
                        "model": "text-embedding-3-small",
                        "prompt_tokens": 2,
                        "completion_tokens": 0,
                        "reasoning_tokens": 0,
                        "total_tokens": 2,
                        "cached_tokens": 0,
                        "count": 1,
                        "cost": 0.0,
                    }
                ]
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/usage-stats")
    page.get_by_role("tab", name="Usage Statistics").click()

    expect(page.locator("#statsArea thead")).to_contain_text("Operation")
    expect(page.locator("#statsArea tbody")).to_contain_text("embeddings")


def test_usage_records_show_time_like_fallback_chains(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/usage-records*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "records": [
                        {
                            "id": 1,
                            "timestamp": "2026-03-21T02:59:18.987654",
                            "duration_ms": 57193,
                            "gateway_model": "llmgateway/qwen3.5",
                            "operation": "chat",
                            "model": "mm.MiniMax-M2.7",
                            "provider": "devbox",
                            "prompt_tokens": 42,
                            "completion_tokens": 11,
                            "reasoning_tokens": 0,
                            "total_tokens": 53,
                            "cached_tokens": 0,
                            "cost": 0.0123,
                        }
                    ],
                    "total_records": 1,
                }
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/usage-stats")
    page.click('button[data-tab="records"]')

    expect(page.locator("#recordsArea thead")).to_contain_text("Duration (ms)")
    tbody_text = page.locator("#recordsArea tbody").text_content() or ""
    assert "2026-03-21T02:59:18" in tbody_text
    assert "57,193" in tbody_text or "57193" in tbody_text
    assert "2026-03-21T02:59:18.987654" not in tbody_text


def test_usage_records_highlight_running_requests(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/usage-records*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "records": [
                        {
                            "id": "active:req-1",
                            "status": "running",
                            "timestamp": "2026-03-21T03:00:00.123456",
                            "duration_ms": 2500,
                            "gateway_model": "llmgateway/qwen3.5",
                            "operation": "chat",
                            "model": "qwen/qwen3",
                            "provider": "openrouter",
                            "prompt_tokens": 0,
                            "completion_tokens": 0,
                            "reasoning_tokens": 0,
                            "total_tokens": 0,
                            "cached_tokens": 0,
                            "cost": 0.0,
                        },
                        {
                            "id": 2,
                            "status": "completed",
                            "timestamp": "2026-03-21T02:59:18.987654",
                            "duration_ms": 57193,
                            "gateway_model": "llmgateway/qwen3.5",
                            "operation": "chat",
                            "model": "mm.MiniMax-M2.7",
                            "provider": "devbox",
                            "prompt_tokens": 42,
                            "completion_tokens": 11,
                            "reasoning_tokens": 0,
                            "total_tokens": 53,
                            "cached_tokens": 0,
                            "cost": 0.0123,
                        },
                    ],
                    "total_records": 2,
                }
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/usage-stats")
    page.click('button[data-tab="records"]')

    thead_text = page.locator("#recordsArea thead").text_content() or ""
    assert "Status" not in thead_text
    running_row = page.locator("#recordsArea tbody tr.usage-record-running")
    expect(running_row).to_be_visible()
    expect(running_row).to_contain_text("llmgateway/qwen3.5")
    tbody_text = page.locator("#recordsArea tbody").text_content() or ""
    assert "Running" not in tbody_text
    assert "Completed" not in tbody_text
    background = running_row.evaluate("element => getComputedStyle(element).backgroundColor")
    assert background not in {"rgba(0, 0, 0, 0)", "transparent"}


def test_api_keys_page_masks_list_but_shows_full_key_in_edit(page: Page, server):
    session = _build_master_session_cookie_value("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    page.route(
        f"{server}/v1/models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"data": []}),
        ),
    )
    page.route(
        f"{server}/v1/admin/api-keys",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "keys": [
                        {
                            "id": 12,
                            "name": "team-key",
                            "api_key": "lgk_test",
                            "budget_usd": 10.0,
                            "spent_usd": 0.0123,
                            "rpm": None,
                            "tpm": None,
                            "allowed_models": [],
                            "disabled": False,
                            "metadata": {},
                            "created_at": "2026-04-24T01:00:00",
                            "last_used_at": "2026-04-24T02:00:00",
                        }
                    ]
                }
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/api-keys")

    expect(page.locator("#keysArea")).not_to_contain_text("lgk_test")
    expect(page.locator("#keysArea")).to_contain_text("••••")
    page.locator('button[data-action="edit"]').click()
    expect(page.locator("#keyModal")).to_contain_text("lgk_test")
    expect(page.locator('button[data-action="usage"]')).to_have_count(0)
    expect(page.locator("#keyUsagePanel")).to_have_count(0)


def test_fallback_chains_show_detailed_error_message(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/fallback-stats/*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps([]),
        ),
    )
    page.route(
        f"{server}/v1/api/fallback-records*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "records": [
                        {
                            "request_id": "req-1",
                            "timestamp": "2026-03-21T02:59:18",
                            "gateway_model": "llmgateway/qwen3.5",
                            "total_attempts": 2,
                            "total_duration_ms": 57193,
                            "success": True,
                            "final_provider": "devbox",
                            "final_model": "mm.MiniMax-M2.7",
                            "x_title": "tgBot",
                            "attempts": [
                                {
                                    "attempt_number": 1,
                                    "provider": "devbox",
                                    "model": "qwc.coder-model",
                                    "success": False,
                                    "error_type": "http_400",
                                    "error_message": (
                                        "Request failed with status code 400. Request summary: "
                                        "model=qwc.coder-model, response_format.type=json_object, "
                                        "stream=false, max_tokens=4000, temperature=0.1."
                                    ),
                                    "duration_ms": 1756,
                                },
                                {
                                    "attempt_number": 2,
                                    "provider": "devbox",
                                    "model": "mm.MiniMax-M2.7",
                                    "success": True,
                                    "error_type": None,
                                    "error_message": None,
                                    "duration_ms": 55437,
                                },
                            ],
                        }
                    ],
                    "total_records": 1,
                }
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/usage-stats")
    page.click('button[data-tab="fallback"]')
    page.click('button[data-subtab="chains"]')

    expect(page.locator("#fallbackChainsArea .chain-card")).to_be_visible()
    expect(page.locator("#fallbackChainsArea .chain-card")).to_contain_text("X-Title: tgBot")
    page.click("#fallbackChainsArea .chain-header")

    expect(page.locator("#fallbackChainsArea .error-badge")).to_contain_text("400 Bad Request")
    expect(page.locator("#fallbackChainsArea .attempt-error-message")).to_contain_text(
        "Request failed with status code 400"
    )
    expect(page.locator("#fallbackChainsArea .attempt-error-message")).to_contain_text(
        "response_format.type=json_object"
    )
    expect(page.locator("#fallbackChainsArea .attempt-error-message")).to_contain_text(
        "max_tokens=4000"
    )

def test_auth_login_flow(page: Page, server):
    _route_empty_analytics_dashboard(page, server)

    # Go to root, should redirect to login with next=/v1/ui/usage-stats
    page.goto(server)
    # The actual redirect is to /auth/login?next=/v1/ui/usage-stats because of root_redirect
    # Note: Browser might normalize encoded slashes back to /
    expect(page).to_have_url(f"{server}/auth/login?next=/v1/ui/usage-stats")
    
    # Enter invalid key
    page.fill('#apiKeyInput', "wrong-key")
    page.click('#submitButton')
    expect(page.locator("#errorBox")).to_be_visible()
    
    # Enter valid key
    page.fill('#apiKeyInput', "test-key")
    page.click('#submitButton')
    
    # Should be redirected to usage-stats
    expect(page).to_have_url(f"{server}/v1/ui/usage-stats")

def test_editor_save_and_reload(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")
    
    # 1. Fallback Rules
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#addRuleButton")
    page.fill("#editor-container-rules .gateway-model-input", "model-chat")
    page.select_option("#editor-container-rules .provider-select", "openai")
    page.wait_for_selector("#editor-container-rules .model-select:not([disabled])")
    page.select_option("#editor-container-rules .model-select", "gpt-4o")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    # 2. Router
    page.click("#tabRouter")
    expect(page.locator("#messageArea")).to_contain_text("Router Models loaded successfully")
    page.click("#addRouterButton")
    page.fill("#editor-container-router .gateway-model-input", "model-router")
    page.select_option("#editor-container-router .router-selector-model-select", "model-chat")
    page.select_option("#editor-container-router .router-target-type-select", "gateway_model")
    page.select_option("#editor-container-router .router-gateway-target-select", "model-chat")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    # 3. Embeddings
    page.click("#tabEmbeddings")
    expect(page.locator("#messageArea")).to_contain_text("Embeddings Routes loaded successfully")
    page.click("#addEmbeddingButton")
    page.fill("#editor-container-embeddings .gateway-model-input", "model-emb")
    page.select_option("#editor-container-embeddings .provider-select", "openai")
    page.fill("#editor-container-embeddings .model-input", "text-embedding-3-small")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    # 4. Rerank
    page.click("#tabRerank")
    expect(page.locator("#messageArea")).to_contain_text("Rerank Routes loaded successfully")
    page.click("#addRerankButton")
    page.fill("#editor-container-rerank .gateway-model-input", "model-rerank")
    page.select_option("#editor-container-rerank .provider-select", "openai")
    page.fill("#editor-container-rerank .model-input", "rerank-v3.5")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    # 5. Images
    page.click("#tabImages")
    expect(page.locator("#messageArea")).to_contain_text("Images Routes loaded successfully")
    page.click("#addImageGenerationButton")
    page.fill("#imageGenerationList .gateway-model-input", "model-image-gen")
    page.select_option("#imageGenerationList .provider-select", "openai")
    page.fill("#imageGenerationList .model-input", "gpt-image-1")
    page.click("#addImageEditButton")
    page.fill("#imageEditList .gateway-model-input", "model-image-edit")
    page.select_option("#imageEditList .provider-select", "openai")
    page.fill("#imageEditList .model-input", "gpt-image-1")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    # 6. Providers
    page.click("#tabProviders")
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    
    # Reload and check all
    page.reload()
    
    # Check Fallback
    expect(page.locator("#editor-container-rules .gateway-model-input")).to_have_value("model-chat")

    # Check Router
    page.click("#tabRouter")
    expect(page.locator("#editor-container-router .gateway-model-input")).to_have_value("model-router")
    expect(page.locator("#editor-container-router .router-selector-model-select")).to_have_value("model-chat")
    expect(page.locator("#editor-container-router .router-gateway-target-select")).to_have_value("model-chat")
    
    # Check Embeddings
    page.click("#tabEmbeddings")
    expect(page.locator("#editor-container-embeddings .gateway-model-input")).to_have_value("model-emb")
    
    # Check Rerank
    page.click("#tabRerank")
    expect(page.locator("#editor-container-rerank .gateway-model-input")).to_have_value("model-rerank")

    # Check Images
    page.click("#tabImages")
    expect(page.locator("#imageGenerationList .gateway-model-input")).to_have_value("model-image-gen")
    expect(page.locator("#imageEditList .gateway-model-input")).to_have_value("model-image-edit")
