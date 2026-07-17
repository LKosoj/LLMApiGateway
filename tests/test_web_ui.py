import base64
import json
import os
from pathlib import Path
import time
from urllib.parse import urlsplit

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
        fallback_rules_path.write_text(
            (
                '[{"gateway_model_name": "llmgateway/light_model", '
                '"fallback_models": [{"provider": "openai", "model": "gpt-4o-mini"}]}]'
            ),
            encoding="utf-8",
        )
        operation_rules_path.write_text(
            (
                '{"embeddings": [{"gateway_model_name": "llmgateway/embedding", '
                '"routes": [{"provider": "openai", "model": "text-embedding-3-small", "target_path": "/embeddings"}]}], '
                '"rerank": [{"gateway_model_name": "llmgateway/rerank", '
                '"routes": [{"provider": "openai", "model": "rerank-model", "target_path": "/score"}]}], '
                '"images_generations": [{"gateway_model_name": "llmgateway/image-gen", '
                '"routes": [{"provider": "openai", "model": "gpt-image-1", "target_path": "/images/generations"}]}], '
                '"images_edits": [{"gateway_model_name": "llmgateway/image-edit", '
                '"routes": [{"provider": "openai", "model": "gpt-image-1", "target_path": "/images/edits"}]}], '
                '"audio_speech": [{"gateway_model_name": "llmgateway/audio-speech", '
                '"routes": [{"provider": "openai", "model": "tts-1", "target_path": "/audio/speech"}]}], '
                '"audio_transcriptions": [{"gateway_model_name": "llmgateway/audio-transcribe", '
                '"routes": [{"provider": "openai", "model": "gpt-4o-mini-transcribe", "target_path": "/audio/transcriptions"}]}], '
                '"pdf_conversions": [{"gateway_model_name": "llmgateway/pdf-convert", '
                '"routes": [{"provider": "openai", "model": "pdf-converter", "target_path": "/pdf"}]}], '
                '"web_search": [], "web_read": [], '
                '"web_research": [], "web_deep_research": []}'
            ),
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


def add_session(page: Page, server: str) -> None:
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])


def route_empty_analytics_dashboard(page: Page, server: str) -> None:
    page.route(
        f"{server}/v1/api/analytics-dashboard*",
        lambda route: route.fulfill(
            json={
                "filters": {"bucket": "day"},
                "totals": {"requests": 0},
                "series": {"usage": []},
                "breakdowns": {"providers": [], "resolved_targets": [], "api_keys": []},
                "reliability": {"fallback": {}, "rejections": {}},
                "recent_records": [],
                "filter_options": {},
            }
        ),
    )


def expand_first_card(page: Page, container_selector: str) -> None:
    card = page.locator(f"{container_selector} .rule-card").first
    card_classes = card.get_attribute("class") or ""
    if "collapsed" in card_classes.split():
        card.locator(".accordion-toggle").click()


def test_web_tab_is_visible(page: Page, server):
    add_session(page, server)

    page.goto(f"{server}/v1/ui/rules-editor")

    web_tab = page.locator("#tabWeb")
    expect(web_tab).to_be_visible()
    expect(web_tab).to_have_text("Web")


def test_gateway_docs_page_renders_catalog_and_connection_sections(page: Page, server):
    add_session(page, server)
    page.set_viewport_size({"width": 1024, "height": 900})
    api_base = f"{server}/v1"
    page.add_init_script("localStorage.setItem('llmgateway:locale', 'ru')")

    page.goto(f"{server}/v1/ui/docs")

    expect(page.locator("h1")).to_contain_text("Документация по подключению моделей")
    expect(page.locator("#catalogStatus")).to_contain_text("Загружено gateway-моделей")
    expect(page.locator("#modelCatalog .model-chip", has_text="llmgateway/light_model")).to_have_count(1)
    expect(page.locator("#modelCatalog .model-chip", has_text="llmgateway/rerank")).to_have_count(1)
    expect(page.locator("[data-docs-api-base]")).to_have_count(9)
    assert page.locator("[data-docs-api-base]").all_inner_texts() == [api_base] * 9
    expect(page.locator("#insecureTransportWarning")).to_be_hidden()
    expect(page.locator("#text .service-card")).to_have_count(3)
    expect(page.locator("#text")).to_contain_text("OpenAI-compatible chat completion")
    expect(page.locator("#rerank")).to_contain_text("POST /v1/rerank")
    expect(page.locator("#rerank")).to_contain_text("Массив строк с документами-кандидатами")
    expect(page.locator("#embeddings")).to_contain_text("Желаемая размерность embedding")
    expect(page.locator("#images")).to_contain_text("Описание изображения, которое нужно сгенерировать")
    expect(page.locator("#audio")).to_contain_text("GET /v1/audio/voices")
    expect(page.locator("#audio")).to_contain_text("timestamp_granularities[]")
    expect(page.locator("#pdf")).to_contain_text("POST /v1/pdf/jobs")
    expect(page.locator("#pdf")).to_contain_text("output")
    expect(page.locator("#pdf")).to_contain_text("both")
    expect(page.locator("#pdf")).to_contain_text("target_language")
    expect(page.locator("#pdf")).to_contain_text("math_ocr_provider")
    expect(page.locator("#pdf")).to_contain_text("formulas_max_pages")
    expect(page.locator("#pdf")).to_contain_text("max_pages")
    expect(page.locator("#pdf")).to_contain_text("password")
    expect(page.locator("#pdf")).to_contain_text("ocr_preprocess_save")
    expect(page.locator("#pdf")).to_contain_text("preprocessed-pdf")
    expect(page.locator("#pdf")).to_contain_text(f"curl -s {api_base}/pdf/jobs")
    expect(page.locator("#web")).to_contain_text("/v1/web/deep-research")
    expect(page.locator("#web")).to_contain_text("/v1/tavily/search")
    expect(page.locator("#web")).to_contain_text("/v1/tavily/extract")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/web/search")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/web/read")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/web/research")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/web/deep-research")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/tavily/search")
    expect(page.locator("#web")).to_contain_text(f"curl -s {api_base}/tavily/extract")
    expect(page.locator("#web")).to_contain_text('"image_generation": true')
    expect(page.locator("#web")).to_contain_text("include_raw_content")
    expect(page.locator("#web")).to_contain_text("failed_results[]")
    expect(page.locator("#web")).to_contain_text("raw_content")
    expect(page.locator("#web")).to_contain_text("Сколько поисковых результатов вернуть")
    expect(page.locator("#web")).to_contain_text("Сколько найденных материалов читать и использовать в отчёте")
    expect(page.locator("#web")).to_contain_text("Ограничивает длину итогового отчёта")
    expect(page.locator("#web")).to_contain_text("Сколько операций выполнять параллельно. Диапазон: 1-10, по умолчанию 6.")
    expect(page.locator("#web")).to_contain_text('"concurrency": 6')
    expect(page.locator("#playground")).to_contain_text("output=both|docx|md")
    expect(page.locator("#playground")).to_contain_text("target_language")
    expect(page.locator("#playground")).to_contain_text("ocr_preprocess_save")
    expect(page.locator("#web")).to_contain_text("deep research дополнительно запрашивает изображения")
    expect(page.locator("#web")).not_to_contain_text("max_results 1-20, default 10; num_queries")
    expect(page.locator(".endpoint-row")).to_have_count(0)
    expect(page.locator("a[href='/v1/ui/rules-editor']")).to_be_visible()
    service_cards = page.locator("#web .service-grid .service-card")
    expect(service_cards).to_have_count(6)
    positions = service_cards.evaluate_all(
        """
        cards => cards.map(card => {
            const rect = card.getBoundingClientRect();
            return {
                left: Math.round(rect.left),
                top: Math.round(rect.top)
            };
        })
        """
    )
    assert abs(positions[0]["top"] - positions[1]["top"]) <= 2
    assert abs(positions[2]["top"] - positions[3]["top"]) <= 2
    assert abs(positions[4]["top"] - positions[5]["top"]) <= 2
    assert positions[2]["top"] > positions[0]["top"]
    assert positions[4]["top"] > positions[2]["top"]
    assert positions[0]["left"] < positions[1]["left"]
    assert positions[2]["left"] < positions[3]["left"]
    assert positions[4]["left"] < positions[5]["left"]
    assert abs(positions[0]["left"] - positions[2]["left"]) <= 2
    assert abs(positions[0]["left"] - positions[4]["left"]) <= 2
    all_service_cards = page.locator(".docs-section .service-card")
    expect(all_service_cards).to_have_count(18)
    overflowing_elements = all_service_cards.evaluate_all(
        """
        cards => cards.flatMap(card => {
            const cardRect = card.getBoundingClientRect();
            return [...card.querySelectorAll('table, th, td, code, .param-label')]
                .filter(element => {
                    const rect = element.getBoundingClientRect();
                    return rect.right > cardRect.right + 1 || rect.left < cardRect.left - 1;
                })
                .map(element => element.textContent.trim());
        })
        """
    )
    assert overflowing_elements == []

    expect(page.get_by_role("tab", name="Free-tier провайдеры")).to_be_visible()
    page.get_by_role("tab", name="Free-tier провайдеры").click()
    expect(page.locator("#freeTierMarkdown")).to_contain_text("Каталог free-tier провайдеров")
    expect(page.locator("#freeTierDocStatus")).to_be_hidden()
    expect(page.locator("#freeTierMarkdown table")).to_have_count(1)
    expect(page.locator("#freeTierMarkdown pre code")).to_contain_text("upstream_limits")
    expect(page.locator("#freeTierMarkdown a[href='https://openrouter.ai/collections/free-models']")).to_have_count(1)


def test_gateway_docs_warns_on_public_http_origin(page: Page, server):
    port = urlsplit(server).port
    assert port is not None
    origins = [
        (f"http://gateway.example:{port}", True),
        (f"http://docs.localhost:{port}", False),
        (f"http://127.23.45.67:{port}", False),
        (f"http://[::1]:{port}", False),
        (f"https://secure-gateway.example:{port}", False),
    ]
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies(
        [
            {"name": "llmgateway_session", "value": session, "url": origin}
            for origin, _ in origins
        ]
    )

    def route_docs_origin(route):
        request_url = urlsplit(route.request.url)
        request_origin = f"{request_url.scheme}://{request_url.netloc}"
        mapped_url = route.request.url.replace(request_origin, server, 1)
        route.fulfill(response=route.fetch(url=mapped_url))

    for origin, should_warn in origins:
        page.route(f"{origin}/**", route_docs_origin)
        page.goto(f"{origin}/v1/ui/docs")
        expect(page.locator("html")).to_have_attribute("data-i18n-state", "ready")
        assert page.evaluate("window.gatewayI18n.currentLocale") == "en"

        expect(page.locator("[data-docs-api-base]")).to_have_count(9)
        assert page.locator("[data-docs-api-base]").all_inner_texts() == [f"{origin}/v1"] * 9
        warning = page.locator("#insecureTransportWarning")
        expect(warning).to_have_attribute("role", "note")
        if should_warn:
            expect(warning).to_contain_text("This page uses public HTTP")
            expect(warning).to_be_visible()
        else:
            expect(warning).to_be_hidden()
            expect(warning).to_have_text("")


def test_gateway_docs_free_tier_catalog_tracks_locale_without_fallback(page: Page, server):
    add_session(page, server)
    page.add_init_script("localStorage.setItem('llmgateway:locale', 'ru')")
    requests_by_locale = {"en": 0, "ru": 0}
    pending_ru_routes = []

    def route_free_tier_doc(route):
        locale = "ru" if "/ru/" in route.request.url else "en"
        requests_by_locale[locale] += 1
        if locale == "ru" and requests_by_locale[locale] == 1:
            pending_ru_routes.append(route)
            return
        if locale == "ru":
            route.abort("failed")
            return
        route.fulfill(
            status=200,
            content_type="text/markdown; charset=utf-8",
            body="# Free-tier provider catalog\n\nENGLISH MARKER",
        )

    page.route("**/static/locales/*/free-tier-providers.md", route_free_tier_doc)
    page.goto(f"{server}/v1/ui/docs")

    assert requests_by_locale == {"en": 0, "ru": 0}
    page.locator("#localeSelect").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.locator("#localeSelect").select_option("ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    assert requests_by_locale == {"en": 0, "ru": 0}

    page.get_by_role("tab", name="Free-tier провайдеры").click()
    expect(page.locator("#freeTierDocStatus")).to_contain_text("Загрузка каталога free-tier")
    assert len(pending_ru_routes) == 1
    page.get_by_role("tab", name="Free-tier провайдеры").click()
    assert requests_by_locale == {"en": 0, "ru": 1}

    page.locator("#localeSelect").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#freeTierMarkdown")).to_contain_text("ENGLISH MARKER")
    expect(page.locator("#freeTierMarkdown")).not_to_contain_text("РУССКИЙ МАРКЕР")
    expect(page.locator("#freeTierDocStatus")).to_be_hidden()

    page.locator("#localeSelect").select_option("ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator("#freeTierDocStatus")).to_contain_text("Каталог free-tier недоступен")
    expect(page.locator("#freeTierMarkdown")).to_contain_text("Не удалось загрузить русскую версию каталога")
    expect(page.locator("#freeTierMarkdown")).not_to_contain_text("ENGLISH MARKER")

    page.locator("#localeSelect").select_option("en")
    expect(page.locator("#freeTierMarkdown")).to_contain_text("ENGLISH MARKER")
    assert requests_by_locale == {"en": 1, "ru": 2}

    page.evaluate(
        """() => {
            const originalFetch = window.fetch.bind(window);
            window.__staleFreeTierFetches = 0;
            window.__resolveStaleFreeTier = null;
            let resolveStaleProcessed;
            window.__staleFreeTierProcessed = new Promise((resolve) => {
                resolveStaleProcessed = resolve;
            });
            window.fetch = (url, options) => {
                if (String(url).includes('/ru/free-tier-providers.md')) {
                    window.__staleFreeTierFetches += 1;
                    return new Promise((resolve) => {
                        window.__resolveStaleFreeTier = () => {
                            resolve(new Response(
                                '# Каталог free-tier провайдеров\\n\\nSTALE RUSSIAN MARKER',
                                {status: 200, headers: {'Content-Type': 'text/markdown'}},
                            ));
                            requestAnimationFrame(() => resolveStaleProcessed());
                        };
                    });
                }
                return originalFetch(url, options);
            };
        }"""
    )
    page.locator("#localeSelect").select_option("ru")
    expect(page.locator("#freeTierDocStatus")).to_contain_text("Загрузка каталога free-tier")
    assert page.evaluate("window.__staleFreeTierFetches") == 1
    page.locator("#localeSelect").select_option("en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    expect(page.locator("#freeTierMarkdown")).to_contain_text("ENGLISH MARKER")
    page.evaluate("window.__resolveStaleFreeTier()")
    page.evaluate("window.__staleFreeTierProcessed")
    expect(page.locator("#freeTierMarkdown")).to_contain_text("ENGLISH MARKER")
    expect(page.locator("#freeTierMarkdown")).not_to_contain_text("STALE RUSSIAN MARKER")

    page_errors = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.add_init_script(
        """window.__unhandledRejections = [];
        window.addEventListener('unhandledrejection', (event) => {
            window.__unhandledRejections.push(String(event.reason));
        });"""
    )
    markdown_requests_before = sum(requests_by_locale.values())
    page.route("**/static/locales/*/docs.json", lambda route: route.abort("failed"))
    page.goto(f"{server}/v1/ui/docs")
    expect(page.locator("[data-i18n-bootstrap-error]")).to_be_visible()
    free_tier_tab = page.get_by_role("tab", name="Free-tier Providers")
    free_tier_tab.click()
    expect(free_tier_tab).to_have_attribute("aria-selected", "true")
    page.evaluate("window.gatewayI18n.ready.catch(() => undefined)")
    assert sum(requests_by_locale.values()) == markdown_requests_before
    assert page_errors == []
    assert page.evaluate("window.__unhandledRejections") == []


def test_web_playground_does_not_render_unsafe_result_urls_as_links(page: Page, server):
    add_session(page, server)
    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"web_search":["llmgateway/web-search"],"web_read":["llmgateway/web-read"],'
            '"web_research":[],"web_deep_research":[],"audio_speech":[],"audio_transcriptions":[],'
            '"images_generations":[],"images_edits":[],"pdf_conversions":[]}',
        ),
    )
    page.route(
        f"{server}/v1/web/search",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"object":"web_search","model":"llmgateway/web-search","query":"x",'
                '"data":[{"url":"javascript:alert(1)","title":"Unsafe","snippet":"bad"}],'
                '"usage":{}}'
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/playground")
    page.fill("#searchQuery", "x")
    page.click("#searchForm .run-button")

    expect(page.locator('[data-result-for="search"] .search-hit')).to_contain_text("Unsafe")
    expect(page.locator('[data-result-for="search"] .search-hit a')).to_have_count(0)


def test_playground_simple_chat_preserves_limited_context_and_resets(page: Page, server):
    add_session(page, server)
    captured_messages = []

    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"chat":["llmgateway/light_model"],"web_search":[],"web_read":[],'
                '"web_research":[],"web_deep_research":[],"audio_speech":[],"audio_transcriptions":[],'
                '"images_generations":[],"images_edits":[],"pdf_conversions":[]}'
            ),
        ),
    )

    def handle_chat(route):
        body = json.loads(route.request.post_data or "{}")
        messages = body.get("messages") or []
        captured_messages.append(messages)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": f"chatcmpl-{len(captured_messages)}",
                    "object": "chat.completion",
                    "model": body.get("model"),
                    "choices": [
                        {
                            "index": 0,
                            "message": {
                                "role": "assistant",
                                "content": f"reply {len(captured_messages)}",
                            },
                            "finish_reason": "stop",
                        }
                    ],
                    "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
                }
            ),
        )

    page.route(f"{server}/v1/chat/completions", handle_chat)
    page.goto(f"{server}/v1/ui/playground")
    page.click("[data-playground-section-tab='chat']")

    for index in range(1, 12):
        page.fill("#simpleChatInput", f"message {index}")
        page.click("#simpleChatForm .run-button")
        expect(page.locator("#simpleChatTranscript")).to_contain_text(f"reply {index}")

    assert captured_messages[0] == [{"role": "user", "content": "message 1"}]
    assert captured_messages[1] == [
        {"role": "user", "content": "message 1"},
        {"role": "assistant", "content": "reply 1"},
        {"role": "user", "content": "message 2"},
    ]
    assert all(len(messages) <= 20 for messages in captured_messages)
    expect(page.locator("#simpleChatTranscript .chat-message")).to_have_count(20)
    transcript_items = page.eval_on_selector_all(
        "#simpleChatTranscript .chat-content",
        "elements => elements.map(element => element.textContent.trim())",
    )
    assert "message 1" not in transcript_items
    assert "message 2" in transcript_items
    expect(page.locator('[data-status-for="chat"]')).to_contain_text("context 20/20")

    page.click("#resetSimpleChatButton")
    expect(page.locator("#simpleChatTranscript")).to_be_hidden()
    expect(page.locator("#simpleChatTranscript .chat-message")).to_have_count(0)
    expect(page.locator('[data-status-for="chat"]')).to_contain_text("Chat reset.")


def test_admin_navigation_is_consistent_across_ui_pages(page: Page, server):
    add_session(page, server)
    route_empty_analytics_dashboard(page, server)

    expected_labels = ["Docs", "Usage Statistics", "Quota", "Rules Editor", "Playground", "API Keys", "Rejections", "Translator Debug", "Pricing"]
    pages = [
        ("/v1/ui/docs", "Docs"),
        ("/v1/ui/usage-stats", "Usage Statistics"),
        ("/v1/ui/rules-editor", "Rules Editor"),
        ("/v1/ui/playground", "Playground"),
        ("/v1/ui/api-keys", "API Keys"),
    ]

    for path, active_label in pages:
        page.goto(f"{server}{path}")
        nav_buttons = page.locator(".top-nav-content .nav-button")
        expect(nav_buttons).to_have_count(len(expected_labels))
        assert [" ".join(label.split()) for label in nav_buttons.all_text_contents()] == expected_labels
        expect(page.locator(".top-nav-content .nav-button.active")).to_have_text(active_label)
        current_page_button = page.locator('.top-nav-content .nav-button[aria-current="page"]')
        expect(current_page_button).to_have_count(1)
        expect(current_page_button).to_have_text(active_label)


def test_playground_does_not_load_audio_voices_on_web_bootstrap(page: Page, server):
    add_session(page, server)
    voice_requests = []

    def handle_voices(route):
        voice_requests.append(route.request.url)
        route.fulfill(status=200, json={"data": []})

    page.route(f"{server}/v1/audio/voices*", handle_voices)
    page.goto(f"{server}/v1/ui/playground")

    expect(page.locator('[data-playground-section-panel="web"]')).to_be_visible()
    page.wait_for_timeout(300)
    assert voice_requests == []

    page.get_by_role("tab", name="Text to Speech").click()
    expect(page.locator('[data-playground-section-panel="audio-speech"]')).to_be_visible()
    deadline = time.time() + 3
    while len(voice_requests) < 1 and time.time() < deadline:
        page.wait_for_timeout(100)
    assert len(voice_requests) == 1


def test_create_and_save_web_services(page: Page, server):
    add_session(page, server)

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabWeb")
    expect(page.locator("#messageArea")).to_contain_text("Web Services loaded successfully")

    page.click("#addWebSearchButton")
    page.fill("#webSearchList .gateway-model-input", "llmgateway/web-search-test")
    page.select_option("#webSearchList .query-model-input", "llmgateway/light_model")
    # Built-in adapters are fixed — the card must not expose provider/model/target_path/retry controls.
    expect(page.locator("#webSearchList .provider-select")).to_have_count(0)
    expect(page.locator("#webSearchList .model-input")).to_have_count(0)
    expect(page.locator("#webSearchList .target-path-input")).to_have_count(0)
    expect(page.locator("#webSearchList details.advanced-options")).to_have_count(0)
    expect(page.locator("#webSearchList .request-format-select")).to_have_count(0)
    expect(page.locator("#webSearchList .retry-delay-input")).to_have_count(0)
    expect(page.locator("#webSearchList .retry-count-input")).to_have_count(0)

    page.click("#addWebReadButton")
    page.fill("#webReadList .gateway-model-input", "llmgateway/web-read-test")
    expect(page.locator("#webReadList .provider-select")).to_have_count(0)
    expect(page.locator("#webReadList .model-input")).to_have_count(0)
    expect(page.locator("#webReadList .target-path-input")).to_have_count(0)
    expect(page.locator("#webReadList details.advanced-options")).to_have_count(0)
    expect(page.locator("#webReadList .request-format-select")).to_have_count(0)

    page.click("#addWebResearchButton")
    page.fill("#webResearchList .gateway-model-input", "llmgateway/web-research-test")
    page.select_option("#webResearchList .search-model-input", "llmgateway/web-search-test")
    page.select_option("#webResearchList .read-model-input", "llmgateway/web-read-test")
    page.select_option("#webResearchList .rerank-model-input", "llmgateway/rerank")
    page.select_option("#webResearchList .analysis-model-input", "llmgateway/light_model")

    page.click("#addWebDeepResearchButton")
    page.fill("#webDeepResearchList .gateway-model-input", "llmgateway/web-deep-research-test")
    page.select_option("#webDeepResearchList .search-model-input", "llmgateway/web-search-test")
    page.select_option("#webDeepResearchList .read-model-input", "llmgateway/web-read-test")
    page.select_option("#webDeepResearchList .fast-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .smart-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .strategic-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .embedding-model-input", "llmgateway/embedding")
    page.select_option("#webDeepResearchList .image-generation-model-input", "llmgateway/image-gen")
    page.fill("#webDeepResearchList .image-generation-size-input", "1024x1024")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabWeb")
    expect(page.locator("#messageArea")).to_contain_text("Web Services loaded successfully")

    assert "collapsed" in (page.locator("#webSearchList .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#webSearchList")
    expect(page.locator("#webSearchList .gateway-model-input")).to_have_value("llmgateway/web-search-test")
    expect(page.locator("#webSearchList .query-model-input")).to_have_value("llmgateway/light_model")
    expect(page.locator("#webSearchList .target-path-input")).to_have_count(0)

    expand_first_card(page, "#webReadList")
    expect(page.locator("#webReadList .gateway-model-input")).to_have_value("llmgateway/web-read-test")
    expect(page.locator("#webReadList .target-path-input")).to_have_count(0)

    expand_first_card(page, "#webResearchList")
    expect(page.locator("#webResearchList .gateway-model-input")).to_have_value("llmgateway/web-research-test")
    expect(page.locator("#webResearchList .search-model-input")).to_have_value("llmgateway/web-search-test")
    expect(page.locator("#webResearchList .read-model-input")).to_have_value("llmgateway/web-read-test")
    expect(page.locator("#webResearchList .rerank-model-input")).to_have_value("llmgateway/rerank")
    expect(page.locator("#webResearchList .analysis-model-input")).to_have_value("llmgateway/light_model")

    expand_first_card(page, "#webDeepResearchList")
    expect(page.locator("#webDeepResearchList .gateway-model-input")).to_have_value("llmgateway/web-deep-research-test")
    expect(page.locator("#webDeepResearchList .fast-model-input")).to_have_value("llmgateway/light_model")
    expect(page.locator("#webDeepResearchList .embedding-model-input")).to_have_value("llmgateway/embedding")
    expect(page.locator("#webDeepResearchList .image-generation-model-input")).to_have_value("llmgateway/image-gen")
    expect(page.locator("#webDeepResearchList .image-generation-size-input")).to_have_value("1024x1024")


def test_playground_page_renders_sections_and_populates_model_selects(page: Page, server):
    add_session(page, server)

    # Сначала через rules-editor заведём минимальные web-конфигурации, чтобы
    # /v1/ui/playground/models вернул непустые списки.
    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabWeb")
    expect(page.locator("#messageArea")).to_contain_text("Web Services loaded successfully")

    page.click("#addWebSearchButton")
    page.fill("#webSearchList .gateway-model-input", "llmgateway/web-search-test")
    page.select_option("#webSearchList .query-model-input", "llmgateway/light_model")

    page.click("#addWebReadButton")
    page.fill("#webReadList .gateway-model-input", "llmgateway/web-read-test")

    page.click("#addWebResearchButton")
    page.fill("#webResearchList .gateway-model-input", "llmgateway/web-research-test")
    page.select_option("#webResearchList .search-model-input", "llmgateway/web-search-test")
    page.select_option("#webResearchList .read-model-input", "llmgateway/web-read-test")
    page.select_option("#webResearchList .rerank-model-input", "llmgateway/rerank")
    page.select_option("#webResearchList .analysis-model-input", "llmgateway/light_model")

    page.click("#addWebDeepResearchButton")
    page.fill("#webDeepResearchList .gateway-model-input", "llmgateway/web-deep-research-test")
    page.select_option("#webDeepResearchList .search-model-input", "llmgateway/web-search-test")
    page.select_option("#webDeepResearchList .read-model-input", "llmgateway/web-read-test")
    page.select_option("#webDeepResearchList .fast-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .smart-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .strategic-model-input", "llmgateway/light_model")
    page.select_option("#webDeepResearchList .embedding-model-input", "llmgateway/embedding")

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.route(
        f"{server}/v1/audio/voices?model=*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"object":"audio.voice_list","model":"llmgateway/audio-speech",'
                '"data":[{"id":"aidar","name":"Aidar","language":"ru","gender":"male"},'
                '{"id":"baya","name":"Baya","language":"ru","gender":"female"}]}'
            ),
        ),
    )

    page.goto(f"{server}/v1/ui/playground")
    expect(page.locator("h1")).to_contain_text("Playground")

    # 7 верхнеуровневых разделов, Web активен по умолчанию.
    expect(page.locator("[data-playground-section-tab]")).to_have_count(7)
    expect(page.locator("[data-playground-section-tab='web']")).to_have_class("tab-button active")
    expect(page.locator("[data-playground-section-tab='chat']")).to_be_visible()
    expect(page.locator("[data-playground-section-panel='web']")).to_be_visible()
    expect(page.locator("[data-playground-section-panel='chat']")).to_be_hidden()
    expect(page.locator("[data-playground-section-panel='audio-speech']")).to_be_hidden()

    page.click("[data-playground-section-tab='chat']")
    expect(page.locator("[data-playground-section-panel='chat']")).to_be_visible()
    expect(page.locator("#chatModel option", has_text="llmgateway/light_model")).to_have_count(1)
    expect(page.locator("#simpleChatInput")).to_be_visible()
    expect(page.locator("#resetSimpleChatButton")).to_be_visible()

    page.click("[data-playground-section-tab='web']")
    expect(page.locator("[data-playground-section-panel='web']")).to_be_visible()

    # Внутри Web остаются прежние 6 web-операций.
    expect(page.locator("[data-web-tab]")).to_have_count(6)
    expect(page.locator("[data-web-tab='search']")).to_have_class("tab-button active")
    expect(page.locator("[data-web-panel='search']")).to_be_visible()
    expect(page.locator("[data-web-panel='read']")).to_be_hidden()
    expect(page.locator("[data-web-tab='tavily-search']")).to_be_visible()
    expect(page.locator("[data-web-tab='tavily-extract']")).to_be_visible()

    # Dropdown'ы должны заполниться через /v1/ui/playground/models.
    expect(page.locator("#searchModel option", has_text="llmgateway/web-search-test")).to_have_count(1)
    expect(page.locator("#searchReadModel option", has_text="llmgateway/web-read-test")).to_have_count(1)
    expect(page.locator("#readModel option", has_text="llmgateway/web-read-test")).to_have_count(1)
    expect(page.locator("#tavilySearchModel option", has_text="llmgateway/web-search-test")).to_have_count(1)
    expect(page.locator("#tavilySearchReadModel option", has_text="llmgateway/web-read-test")).to_have_count(1)
    expect(page.locator("#tavilyExtractModel option", has_text="llmgateway/web-read-test")).to_have_count(1)
    expect(page.locator("#researchModel option", has_text="llmgateway/web-research-test")).to_have_count(1)
    expect(page.locator("#deepResearchModel option", has_text="llmgateway/web-deep-research-test")).to_have_count(1)
    expect(page.locator("#searchIncludeRawContent")).to_have_value("")
    expect(page.locator("#searchIncludeImages")).to_be_visible()
    expect(page.locator("#searchIncludeDomains")).to_be_visible()
    expect(page.locator("#searchExcludeDomains")).to_be_visible()

    page.click("[data-web-tab='read']")
    expect(page.locator("[data-web-panel='read']")).to_be_visible()
    expect(page.locator("#readIncludeImages")).to_be_visible()

    page.click("[data-web-tab='tavily-search']")
    expect(page.locator("[data-web-panel='tavily-search']")).to_be_visible()
    expect(page.locator("#tavilySearchIncludeRawContent")).to_have_value("")
    expect(page.locator("#tavilySearchIncludeImages")).to_be_visible()
    expect(page.locator("#tavilySearchIncludeDomains")).to_be_visible()

    page.click("[data-web-tab='tavily-extract']")
    expect(page.locator("[data-web-panel='tavily-extract']")).to_be_visible()
    expect(page.locator("#tavilyExtractUrls")).to_be_visible()
    expect(page.locator("#tavilyExtractIncludeImages")).to_be_visible()

    page.click("[data-web-tab='research']")
    expect(page.locator("[data-web-panel='research']")).to_be_visible()
    expect(page.locator("#researchMaxResults")).to_have_value("10")
    expect(page.locator("#researchMaxArticles")).to_have_value("8")
    expect(page.locator("#researchNumQueries")).to_have_value("")
    expect(page.locator("#researchLanguage")).to_have_value("all")
    expect(page.locator("#researchOutputLanguage")).to_have_value("ru")
    expect(page.locator("[data-web-panel='research'] .field-help")).to_have_count(6)
    expect(page.locator("label[for='researchMaxArticles'] + input + .field-help")).to_contain_text("after content reranking")
    expect(page.locator("label[for='researchLanguage'] + select + .field-help")).to_contain_text("source-search")
    expect(page.locator("label[for='researchOutputLanguage'] + select + .field-help")).to_contain_text("final report")

    # Переключение на Deep Research: виден чекбокс генерации изображений.
    page.click("[data-web-tab='deep-research']")
    expect(page.locator("[data-web-panel='deep-research']")).to_be_visible()
    expect(page.locator("[data-web-panel='search']")).to_be_hidden()
    expect(page.locator("#deepResearchConcurrency")).to_have_value("6")
    expect(page.locator("#deepResearchImageGeneration")).to_be_visible()
    retention_hint = page.locator("[data-generated-images-retention-hint]")
    expect(retention_hint).to_be_visible()
    expect(retention_hint).to_contain_text("Locally published PNGs")
    expect(retention_hint).to_contain_text("removed after 10 days")
    expect(retention_hint).to_contain_text("across restart and recreate")
    expect(retention_hint).to_contain_text("Provider-hosted URLs")

    page.click("[data-playground-section-tab='audio-speech']")
    expect(page.locator("[data-playground-section-panel='audio-speech']")).to_be_visible()
    expect(page.locator("#audioSpeechModel option", has_text="llmgateway/audio-speech")).to_have_count(1)
    expect(page.locator("#audioSpeechInput")).to_be_visible()
    expect(page.locator("#audioSpeechVoice option", has_text="Aidar")).to_have_count(1)
    expect(page.locator("#audioSpeechLanguage")).to_have_value("")

    page.click("[data-playground-section-tab='audio-transcription']")
    expect(page.locator("#audioTranscriptionModel option", has_text="llmgateway/audio-transcribe")).to_have_count(1)
    expect(page.locator("#audioTranscriptionFile")).to_be_visible()
    expect(page.locator("#audioTranscriptionLanguage")).to_have_value("")
    expect(page.locator("#audioTranscriptionFormat")).to_have_value("json")

    page.click("[data-playground-section-tab='image-generation']")
    expect(page.locator("#imageGenerationModel option", has_text="llmgateway/image-gen")).to_have_count(1)
    expect(page.locator("#imageGenerationPrompt")).to_be_visible()
    expect(page.locator("#imageGenerationSize option", has_text="1024x1024")).to_have_count(1)
    expect(page.locator("#imageGenerationSize option", has_text="1536x1024")).to_have_count(1)

    page.click("[data-playground-section-tab='image-edit']")
    expect(page.locator("#imageEditModel option", has_text="llmgateway/image-edit")).to_have_count(1)
    expect(page.locator("#imageEditFiles")).to_have_attribute("data-max-files", "4")
    expect(page.locator("#imageEditSize option", has_text="1024x1024")).to_have_count(1)
    expect(page.locator("#imageEditSize option", has_text="1536x1024")).to_have_count(1)

    page.click("[data-playground-section-tab='pdf-conversion']")
    expect(page.locator("#pdfConversionModel option", has_text="llmgateway/pdf-convert")).to_have_count(1)
    expect(page.locator("#pdfConversionFile")).to_be_visible()
    expect(page.locator("#pdfConversionOutput")).to_have_value("both")
    expect(page.locator("#pdfConversionLanguage")).to_have_value("rus+eng")
    expect(page.locator("#pdfConversionTargetLanguage")).to_have_value("")
    expect(page.locator("#pdfConversionMathOcr")).to_have_value("off")
    expect(page.locator("#pdfConversionFormulaPages")).to_be_visible()
    expect(page.locator("#pdfConversionMaxPages")).to_be_visible()
    expect(page.locator("#pdfConversionPreprocessSave")).to_be_visible()
    expect(page.locator("#pdfConversionFormat")).to_have_count(0)
    expect(page.locator("#pdfConversionMode")).to_have_count(0)


def test_playground_pdf_conversion_uses_job_api_and_gateway_downloads(page: Page, server, tmp_path):
    add_session(page, server)
    source_pdf = tmp_path / "source.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n1 0 obj\n<<>>\nendobj\n%%EOF\n")
    captured = {"create_body": "", "urls": []}
    poll_count = {"value": 0}

    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"web_search":[],"web_read":[],"web_research":[],"web_deep_research":[],'
                '"audio_speech":[],"audio_transcriptions":[],"images_generations":[],"images_edits":[],'
                '"pdf_conversions":["llmgateway/pdf-convert"]}'
            ),
        ),
    )

    def handle_create_job(route):
        captured["create_body"] = route.request.post_data or ""
        route.fulfill(
            status=202,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "job-123",
                    "filename": "source.pdf",
                    "status": "running",
                    "stage": "preprocess",
                    "message": "Preprocessing PDF",
                    "percent": 30,
                    "elapsed_seconds": 2,
                    "eta_seconds": 5,
                    "progress": [{"stage": "preprocess", "message": "pages", "current": 1, "total": 3, "percent": 30}],
                    "downloads": [],
                    "result_available": False,
                }
            ),
        )

    def handle_job_status(route):
        captured["urls"].append(route.request.url)
        poll_count["value"] += 1
        status = "running" if poll_count["value"] == 1 else "succeeded"
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "id": "job-123",
                    "filename": "source.pdf",
                    "status": status,
                    "stage": "done" if status == "succeeded" else "deepseek_ocr",
                    "message": "Conversion finished" if status == "succeeded" else "DeepSeek OCR",
                    "percent": 100 if status == "succeeded" else 70,
                    "elapsed_seconds": 7,
                    "eta_seconds": None if status == "succeeded" else 3,
                    "progress": [
                        {"stage": "preprocess", "message": "pages", "current": 3, "total": 3, "percent": 30},
                        {"stage": "deepseek_ocr", "message": "pages", "current": 3, "total": 3, "percent": 70},
                    ],
                    "downloads": [
                        {"artifact": "docx", "label": "DOCX", "filename": "source.docx"},
                        {"artifact": "md", "label": "Markdown", "filename": "source.md"},
                        {"artifact": "preprocessed-pdf", "label": "Preprocessed PDF", "filename": "preprocessed.pdf"},
                    ]
                    if status == "succeeded"
                    else [],
                    "result_available": status == "succeeded",
                    "error": None,
                }
            ),
        )

    def handle_job_result(route):
        captured["urls"].append(route.request.url)
        route.fulfill(
            status=200,
            content_type="application/json",
            body=json.dumps(
                {
                    "filename": "source.pdf",
                    "docx_filename": "source.docx",
                    "md_filename": "source.md",
                    "markdown": "# Converted document\n\nBody",
                    "ocr_preview": "OCR preview",
                    "meta": {"pages": 3},
                }
            ),
        )

    page.route(f"{server}/v1/pdf/jobs", handle_create_job)
    page.route(f"{server}/v1/pdf/jobs/job-123?*", handle_job_status)
    page.route(f"{server}/v1/pdf/jobs/job-123/result?*", handle_job_result)

    page.goto(f"{server}/v1/ui/playground")
    page.click("[data-playground-section-tab='pdf-conversion']")
    page.set_input_files("#pdfConversionFile", str(source_pdf))
    page.select_option("#pdfConversionOutput", "both")
    page.fill("#pdfConversionLanguage", "rus+eng")
    page.select_option("#pdfConversionTargetLanguage", "English")
    page.select_option("#pdfConversionMathOcr", "mathpix")
    page.fill("#pdfConversionFormulaPages", "2")
    page.fill("#pdfConversionMaxPages", "47")
    page.fill("#pdfConversionPassword", "secret")
    page.check("#pdfConversionPreprocessSave")
    page.click("#pdfConversionForm .run-button")

    expect(page.locator('[data-result-for="pdf-conversion"] a[download="source.docx"]')).to_have_count(1)
    expect(page.locator('[data-result-for="pdf-conversion"] a[download="source.md"]')).to_have_count(1)
    expect(page.locator('[data-result-for="pdf-conversion"] a[download="preprocessed.pdf"]')).to_have_count(1)
    expect(page.locator('[data-result-for="pdf-conversion"]')).to_contain_text("Converted document")
    expect(page.locator('[data-status-for="pdf-conversion"]')).to_contain_text("downloads: 3")

    multipart_body = captured["create_body"]
    assert 'name="model"' in multipart_body
    assert "llmgateway/pdf-convert" in multipart_body
    assert 'name="output"' in multipart_body
    assert "both" in multipart_body
    assert 'name="language"' in multipart_body
    assert "rus+eng" in multipart_body
    assert 'name="target_language"' in multipart_body
    assert "English" in multipart_body
    assert 'name="math_ocr_provider"' in multipart_body
    assert "mathpix" in multipart_body
    assert 'name="formulas_max_pages"' in multipart_body
    assert "2" in multipart_body
    assert 'name="max_pages"' in multipart_body
    assert "47" in multipart_body
    assert 'name="ocr_preprocess_save"' in multipart_body
    assert "true" in multipart_body
    assert 'name="output_format"' not in multipart_body
    assert all("model=llmgateway%2Fpdf-convert" in url for url in captured["urls"])
    docx_href = page.locator('[data-result-for="pdf-conversion"] a[download="source.docx"]').get_attribute("href")
    assert "/v1/pdf/jobs/job-123/download/docx?model=llmgateway%2Fpdf-convert" in docx_href


def test_playground_renders_download_links_for_media_results(page: Page, server, tmp_path):
    add_session(page, server)
    png_b64 = (
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+/p9sAAAAASUVORK5CYII="
    )
    image_path = tmp_path / "source.png"
    image_path.write_bytes(base64.b64decode(png_b64))

    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=(
                '{"web_search":[],"web_read":[],"web_research":[],"web_deep_research":[],'
                '"audio_speech":["llmgateway/audio-speech"],'
                '"audio_transcriptions":["llmgateway/audio-transcribe"],'
                '"images_generations":["llmgateway/image-gen"],"images_edits":["llmgateway/image-edit"],'
                '"pdf_conversions":[]}'
            ),
        ),
    )
    page.route(
        f"{server}/v1/audio/voices?model=*",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body='{"object":"audio.voice_list","data":[{"id":"aidar","name":"Aidar","language":"ru"}]}',
        ),
    )
    page.route(
        f"{server}/v1/audio/speech",
        lambda route: route.fulfill(status=200, content_type="audio/mpeg", body="audio-bytes"),
    )
    page.route(
        f"{server}/v1/audio/transcriptions",
        lambda route: route.fulfill(status=200, content_type="text/plain", body="hello world"),
    )
    page.route(
        f"{server}/v1/images/generations",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=f'{{"data":[{{"url":"data:image/png;base64,{png_b64}","revised_prompt":"tiny image"}}]}}',
        ),
    )
    page.route(
        f"{server}/v1/images/edits",
        lambda route: route.fulfill(
            status=200,
            content_type="application/json",
            body=f'{{"data":[{{"url":"data:image/png;base64,{png_b64}","revised_prompt":"edited tiny image"}}]}}',
        ),
    )

    page.goto(f"{server}/v1/ui/playground")

    page.click("[data-playground-section-tab='audio-speech']")
    expect(page.locator("#audioSpeechVoice option", has_text="Aidar")).to_have_count(1)
    page.fill("#audioSpeechInput", "hello")
    page.click("#audioSpeechForm .run-button")
    expect(page.locator('[data-result-for="audio-speech"] audio')).to_be_visible()
    expect(page.locator('[data-result-for="audio-speech"] a[download="speech.mp3"]')).to_have_count(1)

    page.click("[data-playground-section-tab='audio-transcription']")
    page.set_input_files("#audioTranscriptionFile", str(Path(__file__).with_name("test.mp3")))
    page.select_option("#audioTranscriptionFormat", "text")
    page.click("#audioTranscriptionForm .run-button")
    expect(page.locator('[data-result-for="audio-transcription"]')).to_contain_text("hello world")
    expect(page.locator('[data-result-for="audio-transcription"] a[download="transcription.txt"]')).to_have_count(1)

    page.click("[data-playground-section-tab='image-generation']")
    page.fill("#imageGenerationPrompt", "small transparent pixel")
    page.select_option("#imageGenerationSize", "1024x1024")
    page.select_option("#imageGenerationFormat", "b64_json")
    page.click("#imageGenerationForm .run-button")
    expect(page.locator('[data-result-for="image-generation"] img')).to_have_count(1)
    expect(page.locator('[data-result-for="image-generation"] a[download="generated-image-1.png"]')).to_have_count(1)
    expect(page.locator('[data-result-for="image-generation"]')).not_to_contain_text("No image payloads in response.")

    page.click("[data-playground-section-tab='image-edit']")
    page.set_input_files("#imageEditFiles", str(image_path))
    page.fill("#imageEditPrompt", "make it brighter")
    page.select_option("#imageEditFormat", "url")
    page.click("#imageEditForm .run-button")
    expect(page.locator('[data-result-for="image-edit"] img')).to_have_count(1)
    expect(page.locator('[data-result-for="image-edit"] a[download="edited-image-1.png"]')).to_have_count(1)
    expect(page.locator('[data-result-for="image-edit"]')).not_to_contain_text("No image payloads in response.")
