"""Serial Docs and Playground locale-preservation contract for R5.8."""

from __future__ import annotations

from collections import Counter

import pytest
from playwright.sync_api import Page, expect

from tests.test_web_ui import add_session, server


__all__ = ["server"]
pytestmark = pytest.mark.browser


def _change_locale(page: Page, locale: str) -> None:
    page.evaluate(
        """(value) => {
            const select = document.querySelector('#localeSelect');
            select.value = value;
            select.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        locale,
    )
    page.wait_for_function(
        "value => document.querySelector('#localeSelect').dataset.localeState === 'ready' "
        "&& window.gatewayI18n.currentLocale === value",
        arg=locale,
    )


def test_docs_and_playground_locale_switch_preserves_snapshots_without_requests(
    page: Page,
    server: str,
    tmp_path,
) -> None:
    counters: Counter = Counter()
    page.set_viewport_size({"width": 390, "height": 844})
    page.add_init_script(
        "localStorage.setItem('llmgateway:locale', 'en');"
        "localStorage.setItem('llmgateway:theme', 'light');"
    )
    add_session(page, server)

    def docs_models_route(route) -> None:
        assert route.request.method == "GET"
        counters["docs_models"] += 1
        route.fulfill(
            json={
                "models": [{"id": "gateway/chat"}],
                "groups": {"chat": ["gateway/chat"]},
            }
        )

    page.route(f"{server}/v1/ui/docs/models", docs_models_route)
    page.goto(f"{server}/v1/ui/docs")

    expect(page.locator("html")).to_have_attribute("data-i18n-state", "ready")
    model_chip = page.locator("#modelCatalog .model-chip", has_text="gateway/chat")
    expect(model_chip).to_have_count(1)
    expect(page.locator("[data-docs-api-base]")).to_have_count(9)
    assert page.locator("[data-docs-api-base]").all_inner_texts() == [
        f"{server}/v1"
    ] * 9
    assert counters["docs_models"] == 1

    docs_scroll = page.evaluate(
        """() => {
            const chip = document.querySelector('#modelCatalog .model-chip');
            const tab = document.querySelector('[data-docs-tab="api"]');
            window.scrollTo({top: 400, left: 0, behavior: 'instant'});
            tab.focus({preventScroll: true});
            window.__r58DocsSnapshot = {chip, tab};
            return window.scrollY;
        }"""
    )
    assert docs_scroll > 0
    docs_requests_before_locale = dict(counters)

    _change_locale(page, "ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator("h1")).to_contain_text("Документация по подключению моделей")
    assert dict(counters) == docs_requests_before_locale
    assert page.evaluate(
        """() => {
            const snapshot = window.__r58DocsSnapshot;
            return snapshot.chip === document.querySelector('#modelCatalog .model-chip')
                && snapshot.tab === document.querySelector('[data-docs-tab="api"]')
                && snapshot.tab === document.activeElement
                && snapshot.tab.getAttribute('aria-selected') === 'true';
        }"""
    )
    assert page.evaluate("window.scrollY") == docs_scroll

    def playground_models_route(route) -> None:
        assert route.request.method == "GET"
        counters["playground_models"] += 1
        route.fulfill(
            json={
                "chat": ["gateway/chat"],
                "fusion": [],
                "web_search": ["gateway/search"],
                "web_read": ["gateway/read"],
                "web_research": ["gateway/research"],
                "web_deep_research": ["gateway/deep-research"],
                "audio_speech": ["gateway/speech"],
                "audio_transcriptions": ["gateway/transcription"],
                "images_generations": ["gateway/image-generation"],
                "images_edits": ["gateway/image-edit"],
                "pdf_conversions": ["gateway/pdf"],
            }
        )

    def voices_route(route) -> None:
        counters["voices"] += 1
        route.fulfill(
            json={
                "data": [
                    {
                        "id": "voice-a",
                        "name": "Voice A",
                        "language": "en",
                    }
                ]
            }
        )

    def audio_speech_route(route) -> None:
        counters["audio_speech"] += 1
        route.fulfill(
            status=200,
            body=b"snapshot-audio",
            headers={"Content-Type": "audio/mpeg"},
        )

    def web_search_route(route) -> None:
        counters["web_search"] += 1
        route.fulfill(
            json={
                "model": "gateway/search",
                "data": [
                    {
                        "title": "Snapshot result",
                        "url": "https://example.com/result",
                        "snippet": "Preserved result",
                        "raw_content": "**preserved raw content**",
                    }
                ],
                "usage": {
                    "prompt_tokens": 1000,
                    "completion_tokens": 250,
                    "total_tokens": 1250,
                    "cost": 0.125,
                },
            }
        )

    def pdf_route(route) -> None:
        counters["pdf"] += 1
        if route.request.method == "POST":
            route.fulfill(
                json={
                    "id": "job-snapshot",
                    "status": "running",
                    "stage": "convert",
                    "percent": 25,
                    "current": 1,
                    "total": 4,
                    "elapsed_seconds": 1,
                    "eta_seconds": 3,
                    "downloads": [],
                    "result_available": False,
                }
            )
            return
        if "/result?" in route.request.url:
            route.fulfill(json={"markdown": "# Snapshot result"})
            return
        route.fulfill(
            json={
                "id": "job-snapshot",
                "status": "completed",
                "stage": "done",
                "percent": 100,
                "current": 4,
                "total": 4,
                "elapsed_seconds": 2,
                "eta_seconds": 0,
                "downloads": [
                    {
                        "artifact": "docx",
                        "label": "DOCX",
                        "filename": "snapshot.docx",
                    }
                ],
                "result_available": True,
            }
        )

    page.route(f"{server}/v1/ui/playground/models", playground_models_route)
    page.route(f"{server}/v1/audio/voices?model=*", voices_route)
    page.route(f"{server}/v1/audio/speech", audio_speech_route)
    page.route(f"{server}/v1/web/search", web_search_route)
    page.route(f"{server}/v1/pdf/jobs**", pdf_route)
    page.goto(f"{server}/v1/ui/playground")

    expect(page.locator("html")).to_have_attribute("data-i18n-state", "ready")
    expect(page.locator("#searchModel option[value='gateway/search']")).to_have_count(1)
    assert counters["playground_models"] == 1

    representative_forms = [
        ("simpleChatForm", "simpleChatInput", "draft chat"),
        ("searchForm", "searchQuery", "snapshot search"),
        ("readForm", "readUrl", "https://example.com/article"),
        ("tavilySearchForm", "tavilySearchQuery", "snapshot tavily"),
        ("tavilyExtractForm", "tavilyExtractUrls", "https://example.com/raw"),
        ("researchForm", "researchQuery", "snapshot research"),
        ("deepResearchForm", "deepResearchQuery", "snapshot deep research"),
        ("audioSpeechForm", "audioSpeechInput", "snapshot speech"),
        ("audioTranscriptionForm", "audioTranscriptionPrompt", "snapshot prompt"),
        ("imageGenerationForm", "imageGenerationPrompt", "snapshot image"),
        ("imageEditForm", "imageEditPrompt", "snapshot edit"),
        ("pdfConversionForm", "pdfConversionPassword", "snapshot password"),
    ]
    page.evaluate(
        """entries => entries.forEach(([, controlId, value]) => {
            const control = document.getElementById(controlId);
            control.value = value;
            control.dispatchEvent(new Event('input', {bubbles: true}));
        })""",
        representative_forms,
    )

    page.locator("[data-playground-section-tab='audio-speech']").click()
    voice_option = page.locator("#audioSpeechVoice option[value='voice-a']")
    expect(voice_option).to_have_count(1)
    page.locator("#audioSpeechVoice").select_option("voice-a")
    page.locator("#audioSpeechForm .run-button").click()
    expect(page.locator('[data-result-for="audio-speech"] audio')).to_have_count(1)
    expect(
        page.locator('[data-result-for="audio-speech"] a.download-link')
    ).to_have_count(1)
    assert counters["voices"] == 1
    assert counters["audio_speech"] == 1

    source_pdf = tmp_path / "snapshot.pdf"
    source_pdf.write_bytes(b"%PDF-1.4\n%%EOF\n")
    page.locator("[data-playground-section-tab='pdf-conversion']").click()
    page.set_input_files("#pdfConversionFile", source_pdf)
    page.locator("#pdfConversionForm .run-button").click()
    pdf_download = page.locator(
        '[data-result-for="pdf-conversion"] a[download="snapshot.docx"]'
    )
    expect(pdf_download).to_have_count(1)
    assert counters["pdf"] == 3

    page.locator("[data-playground-section-tab='web']").click()
    page.locator("[data-web-tab='search']").click()
    page.locator("#searchForm .run-button").click()
    search_result = page.locator('[data-result-for="search"]')
    expect(search_result.locator("details.raw-details")).to_have_count(1)
    expect(search_result).to_contain_text("Preserved result")
    assert counters["web_search"] == 1

    playground_state = page.evaluate(
        """entries => {
            const formIds = entries.map(([formId]) => formId);
            const values = Object.fromEntries(entries.map(
                ([formId, controlId]) => [formId, document.getElementById(controlId).value]
            ));
            const result = document.querySelector('[data-result-for="search"]');
            const spacer = document.createElement('span');
            spacer.dataset.r58ScrollSpacer = 'true';
            spacer.style.cssText = 'display:block;width:900px;height:1px';
            result.style.cssText += ';overflow:auto;max-width:220px';
            result.appendChild(spacer);
            result.scrollLeft = 60;
            const focus = document.getElementById('searchQuery');
            focus.focus({preventScroll: true});
            focus.setSelectionRange(2, 8);
            window.__r58PlaygroundSnapshot = {
                formIds,
                forms: formIds.map((id) => document.getElementById(id)),
                values,
                mainTab: document.querySelector('[data-playground-section-tab="web"]'),
                webTab: document.querySelector('[data-web-tab="search"]'),
                modelOption: document.querySelector('#searchModel option[value="gateway/search"]'),
                voiceOption: document.querySelector('#audioSpeechVoice option[value="voice-a"]'),
                searchResult: result,
                rawDetails: result.querySelector('details.raw-details'),
                audioResult: document.querySelector('[data-result-for="audio-speech"]'),
                audio: document.querySelector('[data-result-for="audio-speech"] audio'),
                audioDownload: document.querySelector('[data-result-for="audio-speech"] a.download-link'),
                pdfResult: document.querySelector('[data-result-for="pdf-conversion"]'),
                pdfDownload: document.querySelector('[data-result-for="pdf-conversion"] a.download-link'),
                focus,
            };
            return {
                resultLeft: result.scrollLeft,
                audioHref: window.__r58PlaygroundSnapshot.audioDownload.href,
                audioSrc: window.__r58PlaygroundSnapshot.audio.src,
                pdfHref: window.__r58PlaygroundSnapshot.pdfDownload.getAttribute('href'),
            };
        }""",
        representative_forms,
    )
    assert playground_state["resultLeft"] > 0
    assert playground_state["audioHref"].startswith("blob:")
    requests_before_locale = dict(counters)

    _change_locale(page, "ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator("#searchForm .run-button")).to_have_text("Запустить поиск")
    expect(search_result.locator("details.raw-details summary")).to_have_text(
        "Исходный JSON-ответ"
    )
    assert dict(counters) == requests_before_locale
    assert page.evaluate(
        """expected => {
            const snapshot = window.__r58PlaygroundSnapshot;
            return snapshot.forms.every(
                (form, index) => form === document.getElementById(snapshot.formIds[index])
            )
                && snapshot.formIds.every((formId) => {
                    const entry = expected.find(([id]) => id === formId);
                    return document.getElementById(entry[1]).value === snapshot.values[formId];
                })
                && snapshot.mainTab === document.querySelector('[data-playground-section-tab="web"]')
                && snapshot.mainTab.getAttribute('aria-selected') === 'true'
                && snapshot.webTab === document.querySelector('[data-web-tab="search"]')
                && snapshot.webTab.getAttribute('aria-selected') === 'true'
                && snapshot.modelOption === document.querySelector('#searchModel option[value="gateway/search"]')
                && snapshot.voiceOption === document.querySelector('#audioSpeechVoice option[value="voice-a"]')
                && snapshot.searchResult === document.querySelector('[data-result-for="search"]')
                && snapshot.rawDetails === snapshot.searchResult.querySelector('details.raw-details')
                && snapshot.audioResult === document.querySelector('[data-result-for="audio-speech"]')
                && snapshot.audio === snapshot.audioResult.querySelector('audio')
                && snapshot.audioDownload === snapshot.audioResult.querySelector('a.download-link')
                && snapshot.pdfResult === document.querySelector('[data-result-for="pdf-conversion"]')
                && snapshot.pdfDownload === snapshot.pdfResult.querySelector('a.download-link')
                && snapshot.focus === document.activeElement
                && snapshot.focus.selectionStart === 2
                && snapshot.focus.selectionEnd === 8;
        }""",
        representative_forms,
    )
    assert search_result.evaluate("result => result.scrollLeft") == playground_state[
        "resultLeft"
    ]
    assert page.locator(
        '[data-result-for="audio-speech"] a.download-link'
    ).get_attribute("href") == playground_state["audioHref"]
    assert page.locator('[data-result-for="audio-speech"] audio').get_attribute(
        "src"
    ) == playground_state["audioSrc"]
    assert pdf_download.get_attribute("href") == playground_state["pdfHref"]
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth + 1"
    )
