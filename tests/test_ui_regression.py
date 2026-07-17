import json
import os
import threading
import tempfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Page, expect

from llm_gateway_core.version import __version__
from tests.ui_server_helpers import (
    create_authenticated_session,
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)

pytestmark = pytest.mark.browser
FALLBACK_ETAG = f'"fallback_rules:sha256:{"a" * 64}"'
NEXT_FALLBACK_ETAG = f'"fallback_rules:sha256:{"b" * 64}"'
PROVIDERS_ETAG = f'"providers:sha256:{"c" * 64}"'
NEXT_PROVIDERS_ETAG = f'"providers:sha256:{"d" * 64}"'
EDITOR_SOURCE = Path("frontend/editor/src")


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


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
    content = _editor_source()

    assert "function isCurrentEditorDirty()" in content
    assert "window.addEventListener('beforeunload'" in content
    assert "event.preventDefault()" in content
    assert "event.returnValue = ''" in content


def test_rules_editor_ignores_stale_provider_model_loads():
    content = _editor_source()

    assert "providerCatalogGeneration" in content
    assert "rowGeneration !== loadGeneration" in content
    assert "providerCatalogGeneration !== pageGeneration" in content
    assert "!row.isConnected" in content
    assert "providerSelect.value.trim() !== provider" in content
    assert "invalidateProviderCatalogRows();" in content


def test_rules_editor_stops_eval_polling_when_switching_tabs():
    content = _editor_source()

    assert "function stopOpenRouterFreePolling()" in content
    assert "function stopFallbackEvalPolling()" in content
    assert "isRulesTabContextCurrent('openrouter-free', context)" in content
    assert "isRulesTabContextCurrent('fallback-eval', context)" in content
    assert "context.signal.aborted" in content
    assert "context.isCurrent()" in content
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
    assert "action.href = '/v1/ui/api-keys'" in js_content
    assert ".quota-empty a" in css_content


def test_translator_debug_has_no_inline_event_handlers():
    html_content = Path("static/translator-debug.html").read_text(encoding="utf-8")
    js_content = Path("static/translator-debug.js").read_text(encoding="utf-8")

    assert "onclick=" not in html_content
    assert "onclick=" not in js_content
    assert 'addEventListener("click"' in js_content
    assert "copyStep(copyButton)" in js_content


def test_web_playground_mobile_layout_and_labels_are_clean(page: Page, server):
    console_errors = []
    page.on(
        "console",
        lambda message: console_errors.append(message.text) if message.type == "error" else None,
    )
    page.emulate_media(reduced_motion="reduce")
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/playground")
    forms = page.locator(".playground-form")
    actions = page.locator(".playground-actions button")
    expect(forms).to_have_count(12)
    expect(actions).to_have_count(13)
    assert all("Include images[]" not in text for text in page.locator("label").all_text_contents())

    primary = page.locator(".playground-actions button.primary-button")
    secondary = page.locator(".playground-actions button.secondary-button")
    expect(primary).to_have_count(1)
    expect(secondary).to_have_count(12)

    def assert_action_styles(*, dark_mode: bool):
        page.evaluate("darkMode => document.body.classList.toggle('dark-mode', darkMode)", dark_mode)
        page.wait_for_timeout(20)
        styles = page.evaluate(
            """() => {
                const tokenStyle = getComputedStyle(document.body);
                const resolveColor = (value) => {
                    const probe = document.createElement('span');
                    probe.style.color = value;
                    document.body.appendChild(probe);
                    const color = getComputedStyle(probe).color;
                    probe.remove();
                    return color;
                };
                const colorToken = (name) => resolveColor(tokenStyle.getPropertyValue(name).trim());
                const read = (element) => {
                    const style = getComputedStyle(element);
                    return {
                        background: style.backgroundColor,
                        border: style.borderTopColor,
                        color: style.color,
                        cursor: style.cursor,
                        opacity: style.opacity,
                        transform: style.transform,
                    };
                };
                return {
                    tokens: {
                        accent: colorToken('--accent'),
                        accentContrast: colorToken('--accent-contrast'),
                        elevated: colorToken('--bg-elevated'),
                        border: colorToken('--border'),
                        text: colorToken('--text'),
                    },
                    primary: Array.from(document.querySelectorAll('.playground-actions .primary-button'), read),
                    secondary: Array.from(document.querySelectorAll('.playground-actions .secondary-button'), read),
                };
            }"""
        )
        assert all(
            style["background"] == styles["tokens"]["accent"]
            and style["border"] == styles["tokens"]["accent"]
            and style["color"] == styles["tokens"]["accentContrast"]
            for style in styles["primary"]
        )
        assert all(
            style["background"] == styles["tokens"]["elevated"]
            and style["border"] == styles["tokens"]["border"]
            and style["color"] == styles["tokens"]["text"]
            for style in styles["secondary"]
        )

        actions.evaluate_all("buttons => buttons.forEach(button => { button.disabled = true; })")
        disabled = actions.evaluate_all(
            """buttons => buttons.map(button => {
                const style = getComputedStyle(button);
                return {
                    cursor: style.cursor,
                    opacity: style.opacity,
                    transform: style.transform,
                    isRun: button.classList.contains('run-button'),
                };
            })"""
        )
        assert all(style["opacity"] != "1" and style["transform"] == "none" for style in disabled)
        assert all(style["cursor"] == "progress" for style in disabled if style["isRun"])
        assert all(style["cursor"] == "not-allowed" for style in disabled if not style["isRun"])
        actions.evaluate_all("buttons => buttons.forEach(button => { button.disabled = false; })")

    section_forms = {
        "chat": "simpleChatForm",
        "audio-transcription": "audioTranscriptionForm",
        "audio-speech": "audioSpeechForm",
        "image-generation": "imageGenerationForm",
        "image-edit": "imageEditForm",
        "pdf-conversion": "pdfConversionForm",
    }
    web_forms = {
        "search": "searchForm",
        "read": "readForm",
        "tavily-search": "tavilySearchForm",
        "tavily-extract": "tavilyExtractForm",
        "research": "researchForm",
        "deep-research": "deepResearchForm",
    }

    def assert_active_form_layout(form_id: str, viewport_width: int):
        form = page.locator(f"#{form_id}")
        expect(form).to_be_visible()
        assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
        if viewport_width != 390:
            return
        groups = form.locator(".field-row .field-group").evaluate_all(
            """items => items.filter(item => item.getClientRects().length > 0).map(item => ({
                height: item.getBoundingClientRect().height,
                width: item.getBoundingClientRect().width,
                rowWidth: item.parentElement.getBoundingClientRect().width,
            }))"""
        )
        assert all(0 < group["height"] < 160 and abs(group["width"] - group["rowWidth"]) <= 1 for group in groups)

    for viewport_width in (390, 1440):
        page.set_viewport_size({"width": viewport_width, "height": 1000})
        page.locator('[data-playground-section-tab="web"]').click()
        for key, form_id in web_forms.items():
            page.locator(f'[data-web-tab="{key}"]').click()
            assert_active_form_layout(form_id, viewport_width)
        for key, form_id in section_forms.items():
            page.locator(f'[data-playground-section-tab="{key}"]').click()
            assert_active_form_layout(form_id, viewport_width)

        if viewport_width == 390:
            mobile_groups = page.evaluate(
                """() => Array.from(document.querySelectorAll('.field-row .field-group')).map(group => {
                    const style = getComputedStyle(group);
                    return {
                        basis: style.flexBasis,
                        grow: style.flexGrow,
                        shrink: style.flexShrink,
                        minWidth: style.minWidth,
                    };
                })"""
            )
            assert mobile_groups
            mobile_flex = {
                (group["basis"], group["grow"], group["shrink"], group["minWidth"]) for group in mobile_groups
            }
            assert mobile_flex == {("auto", "0", "0", "0px")}

        assert_action_styles(dark_mode=False)
        assert_action_styles(dark_mode=True)

    assert console_errors == []


def test_usage_stats_empty_state_is_not_duplicated_by_status_message():
    content = Path("static/usage-stats.js").read_text(encoding="utf-8")

    assert "showMessage('No data available for the selected period.'" not in content


def test_api_keys_modal_has_escape_and_focus_trap():
    content = Path("static/api-keys.js").read_text(encoding="utf-8")

    assert "window.gatewayUi.createDialog" in content
    assert 'labelledBy: "modalTitle"' in content
    assert 'restoreFocus: () => {' in content
    assert 'button[data-action="edit"]' in content
    assert 'closeModal("submit")' in content
    assert "getModalFocusableElements" not in content
    assert "handleModalKeydown" not in content


def test_api_keys_and_pricing_use_shared_status_primitive():
    api_keys = Path("static/api-keys.js").read_text(encoding="utf-8")
    pricing = Path("static/pricing.js").read_text(encoding="utf-8")

    assert "window.gatewayUi.createStatus(messageArea" in api_keys
    assert "window.gatewayUi.createStatus(toast" in pricing
    assert "setTimeout(() =>" not in api_keys


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
    thread_started = False
    try:
        thread.start()
        thread_started = True
        yield f"http://localhost:{port}"
    finally:
        try:
            if thread_started:
                server.shutdown()
        finally:
            try:
                server.server_close()
            finally:
                if thread_started:
                    thread.join(timeout=5)

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

        with isolated_gateway_process(env=env, temp_path=temp_path) as proc:
            wait_for_gateway(base_url, proc)
            yield base_url

@pytest.fixture(scope="function")
def server(provider_mock):
    yield from _serve_gateway(provider_mock)

def test_rules_editor_tabs_display_and_product_version_states(page: Page, server):
    console_errors = []
    page.on("console", lambda message: console_errors.append(message.text) if message.type == "error" else None)
    page.add_init_script(
        "window.addEventListener('gateway:product-version-error', "
        "() => { window.__productVersionErrorSeen = true; });"
    )
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    
    page.goto(f"{server}/v1/ui/rules-editor")

    version = page.locator("[data-product-version]")
    expect(version).to_have_text(f"(v{__version__})")
    expect(version).to_have_attribute("data-product-version-state", "ready")
    
    expect(page.locator("#tabRules")).to_be_visible()
    expect(page.locator("#tabEmbeddings")).to_be_visible()
    expect(page.locator("#tabRerank")).to_be_visible()
    expect(page.locator("#tabImages")).to_be_visible()
    expect(page.locator("#tabAudio")).to_be_visible()
    expect(page.locator("#tabRouter")).to_be_visible()
    expect(page.locator("#tabProviders")).to_be_visible()
    assert console_errors == []

    page.route(
        f"{server}/health",
        lambda route: route.fulfill(status=200, json={"status": "ok"}),
    )
    page.reload()
    expect(version).to_have_text("(version unavailable)")
    expect(version).to_have_attribute("data-product-version-state", "error")
    assert page.evaluate("window.__productVersionErrorSeen === true")
    assert len(console_errors) == 1
    assert console_errors[0].startswith(
        "Failed to load the LLM Gateway product version. "
        "Error: Health endpoint omitted the product version header"
    )

def test_fallback_rules_still_work(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
    expect(provider_card.locator(".provider-api-key-field .field-tooltip-popover")).to_contain_text(
        "environment variable"
    )
    page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
    expect(provider_card.locator(".provider-api-key-field .field-tooltip-button")).to_have_count(1)
    page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
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
    session = create_authenticated_session(server, "test-key")
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
            },
            headers={"ETag": PROVIDERS_ETAG},
        )

    page.route("**/v1/config/providers/structured", handle_structured_providers)

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabProviders")

    expect(page.locator("#messageArea")).to_contain_text("Error loading Providers")
    expect(page.locator("#saveButton")).to_be_disabled()
    expect(page.locator("#addProviderButton")).to_be_disabled()
    expect(page.locator("#providersList .provider-card")).to_have_count(0)

    page.locator("#saveButton").dispatch_event("click")
    expect(page.locator("#messageArea")).to_contain_text(
        "Provider configuration has not loaded successfully"
    )
    page.wait_for_timeout(100)
    assert provider_post_count == 0

    page.locator("#addProviderButton").dispatch_event("click")
    expect(page.locator("#messageArea")).to_contain_text(
        "Provider configuration has not loaded successfully"
    )
    expect(page.locator("#providersList .provider-card")).to_have_count(0)

    page.click("#tabProviders")

    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    assert provider_get_count == 2
    assert provider_post_count == 0


def test_async_providers_load_locks_navigation_until_response(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    delayed_provider_route = {}

    def handle_structured_providers(route):
        if route.request.method == "GET" and not delayed_provider_route:
            delayed_provider_route["route"] = route
            return
        route.continue_()

    page.route("**/v1/config/providers/structured", handle_structured_providers)

    page.goto(f"{server}/v1/ui/rules-editor")
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    expect(page.locator("#tabRules")).to_be_disabled()
    expect(page.locator("#tabProviders")).to_be_disabled()

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
        },
        headers={"ETag": PROVIDERS_ETAG},
    )
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#tabRules")).to_be_enabled()
    expect(page.locator("#tabProviders")).to_be_enabled()
    expect(page.locator("#saveButton")).to_be_enabled()

    page.click("#tabRules")
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    expect(page.locator("#saveButton")).to_be_enabled()


def test_providers_editor_serializes_explicit_loads(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
    expect(page.locator("#tabProviders")).to_be_disabled()
    expect(page.locator("#tabRules")).to_be_disabled()
    page.wait_for_timeout(100)
    assert len(provider_get_routes) == 1

    provider_get_routes[0].fulfill(
        json={
            "providers": [
                {
                    "name": "openai",
                    "baseUrl": "http://api.openai.test",
                    "apikey": "key",
                    "type": "openai",
                }
            ]
        },
        headers={"ETag": PROVIDERS_ETAG},
    )
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()

    page.click("#tabRules")
    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    page.click("#tabProviders")
    expect(page.locator("#saveButton")).to_be_disabled()
    assert len(provider_get_routes) == 2
    provider_get_routes[1].fulfill(
        json={
            "providers": [
                {
                    "name": "anthropic",
                    "baseUrl": "http://api.anthropic.test",
                    "apikey": "key",
                    "type": "anthropic",
                }
            ]
        },
        headers={"ETag": NEXT_PROVIDERS_ETAG},
    )
    expect(page.locator("#messageArea")).to_contain_text("Providers loaded successfully")
    expect(page.locator("#providersList .provider-card")).to_have_count(1)
    expect(page.locator("#providersList .provider-name-input")).to_have_value("anthropic")
    expect(page.locator("#saveButton")).to_be_enabled()
    expect(page.locator("#addProviderButton")).to_be_enabled()


def test_fallback_rules_max_total_attempts_and_use_provider_order_persist(page: Page, server):
    """End-to-end UI round-trip: enter max_total_attempts (rule-level chain
    budget) and use_provider_order_as_fallback (per-fallback toggle) in the
    structured editor, save, reload — both fields must come back preserved.
    Regression for H7: _build_structured_rules_response previously dropped
    max_total_attempts, so a UI round-trip silently zeroed the chain budget."""
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
            },
            headers={"ETag": FALLBACK_ETAG},
        ),
    )
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "gpt-4o"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    expect(page.locator(".gateway-model-input")).to_have_value("my-gateway-model")
    expect(page.locator(".model-select").first).to_have_value("missing-model-1")
    expect(page.locator(".model-select").nth(1)).to_have_value("missing-model-2")

    page.click(".accordion-toggle")
    expect(page.locator(".model-status").first).to_contain_text("missing-model-1")
    expect(page.locator(".model-status").nth(1)).to_contain_text("missing-model-2")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("Unavailable fallback models")
    expect(page.locator("#messageArea")).to_contain_text("my-gateway-model:")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-1")
    expect(page.locator("#messageArea")).to_contain_text("openai.missing-model-2")


def test_fallback_rules_unavailable_model_can_be_replaced(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )

    posted_payloads = []

    def handle_rules(route):
        if route.request.method == "POST":
            posted = json.loads(route.request.post_data or "{}")
            posted_payloads.append(posted)
            route.fulfill(
                json={"message": "Fallback Rules updated successfully.", **posted},
                headers={"ETag": NEXT_FALLBACK_ETAG},
            )
        else:
            route.fulfill(
                json={
                    "rules": [
                        {
                            "gateway_model_name": "my-gateway-model",
                            "rotate_models": False,
                            "compress_tool_results": True,
                            "fallback_models": [
                                {"provider": "openai", "model": "missing-model-1"},
                            ],
                        }
                    ],
                    "providers": ["openai"],
                },
                headers={"ETag": FALLBACK_ETAG},
            )

    page.route("**/v1/config/models-rules/structured", handle_rules)
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "gpt-4o"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")

    expect(page.locator("#messageArea")).to_contain_text("Fallback Rules loaded successfully")
    compress_tool_results = page.locator(".compress-tool-results-checkbox")
    expect(compress_tool_results).to_be_checked()
    compress_tool_results.uncheck()

    # Loaded rule cards are collapsed by default; expand to reach the select.
    page.click(".accordion-toggle")
    expect(page.locator(".model-status").first).to_contain_text("is unavailable")

    # Reopening the already cached catalog must not clear fail-closed metadata.
    model_select = page.locator(".model-select").first
    model_select.focus()
    model_select.dispatch_event("pointerdown")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("Unavailable fallback models")
    expect(model_select).to_have_value("missing-model-1")

    # Picking an available model must clear the stale "unavailable" error...
    model_select.select_option("gpt-4o")
    expect(page.locator(".model-status").first).to_contain_text("selected")
    expect(page.locator(".model-status").first).not_to_contain_text("is unavailable")

    # ...so the save is no longer blocked and succeeds.
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")
    assert posted_payloads[0]["rules"][0]["compress_tool_results"] is False


def test_fallback_rules_unavailable_models_grouped_by_gateway_model(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
            },
            headers={"ETag": FALLBACK_ETAG},
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
    expect(message).to_contain_text("Fallback Rules loaded successfully")
    page.locator(".accordion-toggle").nth(0).click()
    page.locator(".accordion-toggle").nth(1).click()
    page.click("#saveButton")
    expect(message).to_contain_text("Unavailable fallback models")
    expect(message).to_contain_text("llmgateway/high: z.ai.glm-5.1")
    expect(message).to_contain_text("llmgateway/low: z.ai.glm-5.1")


def test_usage_stats_page_loads(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    
    page.goto(f"{server}/v1/ui/usage-stats")
    
    expect(page.locator("h1")).to_contain_text("Usage Statistics")
    expect(page.locator("#analyticsTabContent")).to_be_visible()
    expect(page.locator("#analyticsDashboard")).to_be_visible()

def test_usage_stats_page_renders_operation_column(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
    assert "Mar 21, 2026" in tbody_text
    assert "2:59:18 AM" in tbody_text
    assert "57,193" in tbody_text or "57193" in tbody_text
    assert "2026-03-21T02:59:18.987654" not in tbody_text


def test_usage_records_highlight_running_requests(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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
    expect(page.locator("#keyModal .modal")).to_have_attribute("role", "dialog")
    expect(page.locator("#keyModal .modal")).to_have_attribute("aria-modal", "true")
    expect(page.locator(".container")).to_have_attribute("inert", "")
    expect(page.locator("#fieldName")).to_be_focused()

    page.keyboard.press("Escape")
    expect(page.locator("#keyModal")).to_be_hidden()
    expect(page.locator('button[data-action="edit"]')).to_be_focused()

    page.locator('button[data-action="edit"]').click()
    page.evaluate(
        """
        () => {
            const original = document.querySelector('button[data-action="edit"]');
            original.replaceWith(original.cloneNode(true));
        }
        """
    )
    page.keyboard.press("Escape")
    expect(page.locator('button[data-action="edit"]')).to_be_focused()


def test_api_keys_model_catalog_is_lazy_shared_and_cached(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    pending_model_routes = []
    model_request_count = 0

    def hold_models(route):
        nonlocal model_request_count
        model_request_count += 1
        pending_model_routes.append(route)

    page.route(f"{server}/v1/models", hold_models)
    page.route(
        f"{server}/v1/admin/api-keys",
        lambda route: route.fulfill(status=200, json={"keys": []}),
    )

    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator("#keysArea")).to_contain_text("No API keys yet")
    assert model_request_count == 0

    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "loading"
    )
    assert model_request_count == 1

    page.locator("#cancelKeyBtn").click()
    page.locator("#createKeyBtn").click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "loading"
    )
    assert model_request_count == 1

    pending_model_routes[0].fulfill(
        status=200,
        json={"data": [{"id": "model-b"}, {"id": "model-a"}]},
    )
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    assert page.locator("#allowedModelsList").get_attribute("aria-live") is None
    expect(page.locator('#allowedModelsList input[type="checkbox"]')).to_have_count(2)

    page.locator("#cancelKeyBtn").click()
    page.locator("#createKeyBtn").click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    assert model_request_count == 1


def test_api_keys_model_catalog_error_retry_and_locale_preserve_form(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    model_request_count = 0

    def models_response(route):
        nonlocal model_request_count
        model_request_count += 1
        if model_request_count == 1:
            route.fulfill(status=503, json={"detail": "temporarily unavailable"})
        else:
            route.fulfill(status=200, json={"data": [{"id": "model-a"}]})

    page.route(f"{server}/v1/models", models_response)
    page.route(
        f"{server}/v1/admin/api-keys",
        lambda route: route.fulfill(status=200, json={"keys": []}),
    )

    page.goto(f"{server}/v1/ui/api-keys")
    page.locator("#createKeyBtn").click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "error"
    )
    expect(page.locator("#allowedModelsList")).to_contain_text("Could not load models")
    expect(page.locator("#allowedModelsList .model-catalog-copy")).to_have_attribute(
        "role", "status"
    )
    expect(page.locator("#allowedModelsList .model-catalog-copy")).to_have_attribute(
        "aria-live", "polite"
    )
    expect(page.locator("#retryModelsBtn")).to_have_text("Retry")
    assert model_request_count == 1

    page.locator("#fieldName").fill("team-alpha")
    page.locator("#fieldName").focus()
    page.locator("#fieldName").evaluate("element => element.setSelectionRange(2, 6)")
    page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
    expect(page.locator("#allowedModelsList")).to_contain_text("Не удалось загрузить модели")
    expect(page.locator("#retryModelsBtn")).to_have_text("Повторить")
    expect(page.locator("#fieldName")).to_have_value("team-alpha")
    expect(page.locator("#fieldName")).to_be_focused()
    assert page.locator("#fieldName").evaluate(
        "element => [element.selectionStart, element.selectionEnd]"
    ) == [2, 6]
    assert model_request_count == 1

    page.locator("#retryModelsBtn").click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    checkbox = page.locator('#allowedModelsList input[value="model-a"]')
    checkbox.check()
    page.evaluate(
        """
        () => {
            window.__r54Checkbox = document.querySelector(
                '#allowedModelsList input[value="model-a"]'
            );
            const field = document.querySelector('#fieldName');
            field.focus();
            field.setSelectionRange(1, 4);
        }
        """
    )
    page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
    assert page.evaluate(
        "() => window.__r54Checkbox === document.querySelector('#allowedModelsList input[value=\"model-a\"]')"
    )
    expect(checkbox).to_be_checked()
    expect(page.locator("#fieldName")).to_have_value("team-alpha")
    expect(page.locator("#fieldName")).to_be_focused()
    assert page.locator("#fieldName").evaluate(
        "element => [element.selectionStart, element.selectionEnd]"
    ) == [1, 4]
    assert model_request_count == 2

    failed_page = page.context.new_page()
    failed_page.add_init_script(
        """
        window.__unhandledRejections = [];
        window.addEventListener("unhandledrejection", event => {
            window.__unhandledRejections.push(String(event.reason));
            event.preventDefault();
        });
        """
    )
    failed_page.route(
        f"{server}/static/locales/en/api_keys.json",
        lambda route: route.fulfill(status=503, json={"detail": "catalog unavailable"}),
    )
    failed_page.goto(f"{server}/v1/ui/api-keys")
    expect(failed_page.locator("[data-i18n-bootstrap-error]")).to_contain_text(
        "Localization failed"
    )
    failed_page.wait_for_timeout(50)
    assert failed_page.evaluate("window.__unhandledRejections") == []
    failed_page.close()


def test_api_keys_slow_edit_then_create_does_not_leak_selection(page: Page, server):
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    record = {
        "id": 12,
        "name": "team-key",
        "api_key": "lgk_test",
        "budget_usd": 10.0,
        "spent_usd": 0.0,
        "rpm": None,
        "tpm": None,
        "allowed_models": ["catalog-model", "saved-only-model"],
        "disabled": False,
        "metadata": {},
        "created_at": "2026-04-24T01:00:00",
        "last_used_at": None,
    }
    pending_model_routes = []
    saved_payloads = []

    page.route(
        f"{server}/v1/models",
        lambda route: pending_model_routes.append(route),
    )

    def api_keys_response(route):
        if route.request.method == "PATCH":
            saved_payloads.append(route.request.post_data_json)
            route.fulfill(status=200, json=record)
        else:
            route.fulfill(status=200, json={"keys": [record]})

    page.route(f"{server}/v1/admin/api-keys**", api_keys_response)

    page.goto(f"{server}/v1/ui/api-keys")
    page.locator('button[data-action="edit"]').click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "loading"
    )
    page.locator("#cancelKeyBtn").click()
    page.locator("#createKeyBtn").click()

    pending_model_routes[0].fulfill(
        status=200,
        json={"data": [{"id": "catalog-model"}, {"id": "other-model"}]},
    )
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    expect(page.locator('#allowedModelsList input[type="checkbox"]:checked')).to_have_count(0)
    expect(page.locator('#allowedModelsList input[value="saved-only-model"]')).to_have_count(0)

    page.locator("#cancelKeyBtn").click()
    page.locator('button[data-action="edit"]').click()
    missing_checkbox = page.locator('#allowedModelsList input[value="saved-only-model"]')
    expect(missing_checkbox).to_be_checked()
    expect(page.locator('#allowedModelsList input[value="catalog-model"]')).to_be_checked()

    missing_checkbox.evaluate("element => element.remove()")
    page.locator("#saveKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_hidden()
    assert saved_payloads[0]["allowed_models"] == ["catalog-model", "saved-only-model"]


def test_fallback_chains_show_detailed_error_message(page: Page, server):
    session = create_authenticated_session(server, "test-key")
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
    session = create_authenticated_session(server, "test-key")
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


def test_fallback_chains_pagination_advances_while_chains_subtab_stays_active(page: Page, server):
    """Regression for a dead Next/Prev pagination bug: reselecting the already
    active "chains" sub-tab (which is what the Next/Prev handlers used to do)
    goes through onReselect, which resets fallbackCurrentPage back to 1 before
    the fetch reads it, so page 2+ was unreachable."""
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/fallback-stats/*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps([])),
    )

    requested_offsets = []

    def handle_fallback_records(route):
        query = parse_qs(urlparse(route.request.url).query)
        offset = int(query.get("offset", ["0"])[0])
        requested_offsets.append(offset)
        record = {
            "request_id": f"req-{offset}",
            "timestamp": "2026-03-21T02:59:18",
            "gateway_model": "llmgateway/qwen3.5",
            "total_attempts": 1,
            "total_duration_ms": 100,
            "success": True,
            "x_title": None,
            "attempts": [
                {
                    "attempt_number": 1,
                    "provider": "devbox",
                    "model": "model-a",
                    "success": True,
                    "error_type": None,
                    "error_message": None,
                    "duration_ms": 100,
                }
            ],
        }
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"records": [record], "total_records": 60}),
        )

    page.route(f"{server}/v1/api/fallback-records*", handle_fallback_records)

    page.goto(f"{server}/v1/ui/usage-stats")
    page.click('button[data-tab="fallback"]')
    page.click('button[data-subtab="chains"]')

    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 1 of 3")
    assert requested_offsets[-1] == 0

    page.click("#fallbackNextPage")
    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 2 of 3")
    assert requested_offsets[-1] == 25

    page.click("#fallbackPrevPage")
    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 1 of 3")
    assert requested_offsets[-1] == 0


def test_fallback_chains_next_double_click_does_not_overrun_last_page(page: Page, server):
    """Regression: fallbackNextPage had no synchronous upper-bound check (unlike
    fallbackPrevPage's `if (fallbackCurrentPage > 1)`), so a double click fired
    before the first response updates `disabled` could push fallbackCurrentPage
    past totalPages. The stale-response guard then finds page(4) !== totalPages(3)
    and total_records !== 0, so it never re-disables Next, leaving the UI stuck
    showing "Page 4 of 3" with an empty list and a still-clickable Next button."""
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    _route_empty_analytics_dashboard(page, server)
    page.route(
        f"{server}/v1/api/fallback-stats/*",
        lambda route: route.fulfill(status=200, content_type="application/json", body=json.dumps([])),
    )

    requested_offsets = []

    def handle_fallback_records(route):
        query = parse_qs(urlparse(route.request.url).query)
        offset = int(query.get("offset", ["0"])[0])
        requested_offsets.append(offset)
        total_records = 60
        records = (
            [
                {
                    "request_id": f"req-{offset}",
                    "timestamp": "2026-03-21T02:59:18",
                    "gateway_model": "llmgateway/qwen3.5",
                    "total_attempts": 1,
                    "total_duration_ms": 100,
                    "success": True,
                    "x_title": None,
                    "attempts": [
                        {
                            "attempt_number": 1,
                            "provider": "devbox",
                            "model": "model-a",
                            "success": True,
                            "error_type": None,
                            "error_message": None,
                            "duration_ms": 100,
                        }
                    ],
                }
            ]
            if offset < total_records
            else []
        )
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps({"records": records, "total_records": total_records}),
        )

    page.route(f"{server}/v1/api/fallback-records*", handle_fallback_records)

    page.goto(f"{server}/v1/ui/usage-stats")
    page.click('button[data-tab="fallback"]')
    page.click('button[data-subtab="chains"]')

    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 1 of 3")
    page.click("#fallbackNextPage")
    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 2 of 3")
    assert requested_offsets[-1] == 25

    # Fire two Next clicks in the same tick, before the first response can
    # disable the button or update fallbackCurrentPage's bound check.
    page.evaluate(
        """
        () => {
            const btn = document.querySelector('#fallbackNextPage');
            btn.click();
            btn.click();
        }
        """
    )

    expect(page.locator("#fallbackPageInfo")).to_have_text("Page 3 of 3")
    assert 75 not in requested_offsets


def _analytics_dashboard_payload(requests_count):
    return {
        "filters": {"bucket": "day"},
        "totals": {"requests": requests_count},
        "series": {"usage": []},
        "breakdowns": {"providers": [], "resolved_targets": [], "api_keys": []},
        "reliability": {"fallback": {}, "rejections": {}},
        "recent_records": [],
        "filter_options": {},
    }


def test_usage_analytics_reactivation_retries_after_error_instead_of_showing_stale_success(page: Page, server):
    """Regression: state.loaded is only ever set to true on success and never
    reset, so a failed refresh after a prior success left activate() taking
    the "already loaded" branch and re-rendering the stale success payload
    with a green status instead of retrying."""
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    call_count = 0

    def handle_dashboard(route):
        nonlocal call_count
        call_count += 1
        if call_count == 2:
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return
        requests_count = 11 if call_count == 1 else 22
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(_analytics_dashboard_payload(requests_count)),
        )

    page.route(f"{server}/v1/api/analytics-dashboard*", handle_dashboard)

    page.goto(f"{server}/v1/ui/usage-stats")
    expect(page.locator("#analyticsKpis")).to_contain_text("11")
    assert call_count == 1

    page.select_option("#analyticsRange", "7d")
    expect(page.locator("#analyticsStatus")).not_to_be_empty()
    assert call_count == 2

    page.click('button[data-tab="stats"]')
    page.click('button[data-tab="analytics"]')

    expect(page.locator("#analyticsKpis")).to_contain_text("22")
    assert call_count == 3


def test_api_keys_double_click_save_creates_only_one_key(page: Page, server):
    """Regression: saveKeyBtn was never disabled during the save request, so a
    double click (two clicks before the first response returns) sent two POST
    requests and created two API keys, leaving the first one orphaned."""
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    page.route(f"{server}/v1/models", lambda route: route.fulfill(status=200, json={"data": []}))

    post_count = 0

    def handle_api_keys(route):
        nonlocal post_count
        if route.request.method == "POST":
            post_count += 1
            route.fulfill(
                status=200,
                json={
                    "id": post_count,
                    "name": "double-click-key",
                    "api_key": f"lgk_double_click_{post_count}",
                    "budget_usd": None,
                    "spent_usd": 0.0,
                    "rpm": None,
                    "tpm": None,
                    "allowed_models": [],
                    "disabled": False,
                    "metadata": {},
                    "created_at": "2026-04-24T01:00:00",
                    "last_used_at": None,
                },
            )
        else:
            route.fulfill(status=200, json={"keys": []})

    page.route(f"{server}/v1/admin/api-keys", handle_api_keys)

    page.goto(f"{server}/v1/ui/api-keys")
    page.locator("#createKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_visible()
    page.locator("#fieldName").fill("double-click-key")

    page.evaluate(
        """
        () => {
            const btn = document.querySelector('#saveKeyBtn');
            btn.click();
            btn.click();
        }
        """
    )

    expect(page.locator("#saveKeyBtn")).to_be_enabled()
    assert post_count == 1


def test_playground_audio_speech_voice_select_ignores_stale_model_response(page: Page, server):
    """Regression: the model-select change listener calls
    refreshAudioVoiceSelect() with no activationContext, so its staleness
    guards (which only check activationContext) never trigger. A slow voice
    response for a previously-selected model could overwrite the voice list
    for the model the user has since switched to."""
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])
    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(
            status=200,
            json={"audio_speech": ["model-slow", "model-fast"]},
        ),
    )

    held_slow_routes = []

    def handle_voices(route):
        query = parse_qs(urlparse(route.request.url).query)
        model = query.get("model", [""])[0]
        if model == "model-slow":
            held_slow_routes.append(route)
            return
        route.fulfill(status=200, json={"data": ["fast-voice"]})

    page.route(f"{server}/v1/audio/voices*", handle_voices)

    page.goto(f"{server}/v1/ui/playground")
    page.click('[data-playground-section-tab="audio-speech"]')

    # Arriving on the Audio Speech section triggers an initial (context-aware)
    # voice fetch for the default-selected model, which our route holds open.
    expect(page.locator("#audioSpeechModel")).to_have_value("model-slow")
    expect(page.locator("#audioSpeechVoice option", has_text="Loading voices")).to_have_count(1)
    page.wait_for_timeout(100)
    assert len(held_slow_routes) == 1

    page.select_option("#audioSpeechModel", "model-fast")
    expect(page.locator("#audioSpeechVoice option", has_text="fast-voice")).to_have_count(1)

    # Release the stale held response for the model the user switched away from.
    held_slow_routes[0].fulfill(status=200, json={"data": ["slow-voice"]})
    page.wait_for_timeout(100)

    expect(page.locator("#audioSpeechVoice option", has_text="fast-voice")).to_have_count(1)
    expect(page.locator("#audioSpeechVoice option", has_text="slow-voice")).to_have_count(0)
