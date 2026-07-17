"""R5.8 contracts and browser regression for Playground i18n."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _leaf_keys(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return set()
    result: set[str] = set()
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, str):
            result.add(path)
        else:
            result.update(_leaf_keys(child, path))
    return result


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"Unclosed JavaScript function: {signature}")


def test_playground_catalogs_and_html_cover_all_forms_and_actions() -> None:
    english = json.loads(
        (ROOT / "static/locales/en/playground.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (ROOT / "static/locales/ru/playground.json").read_text(encoding="utf-8")
    )
    required = {
        "page.documentTitle",
        "page.heading",
        "page.adminTesting",
        "tabs.web",
        "tabs.chat",
        "tabs.audioTranscription",
        "tabs.audioSpeech",
        "tabs.imageGeneration",
        "tabs.imageEdit",
        "tabs.pdfConversion",
        "actions.sendMessage",
        "actions.resetChat",
        "actions.runSearch",
        "actions.fetchRead",
        "actions.runTavilySearch",
        "actions.runTavilyExtract",
        "actions.runResearch",
        "actions.runDeepResearch",
        "actions.generateSpeech",
        "actions.transcribeAudio",
        "actions.generateImages",
        "actions.editImages",
        "actions.startPdfConversion",
        "status.running",
        "status.done",
        "status.requestFailed",
        "results.rawJson",
        "results.usage",
        "results.usagePrompt",
        "results.usageCompletion",
        "results.usageTotal",
        "results.usageCost",
        "results.usageCredits",
        "results.searchTitle",
        "results.pdfTitle",
    }

    assert _leaf_keys(english) == _leaf_keys(russian)
    assert required <= _leaf_keys(english)

    html = (ROOT / "static/web-playground.html").read_text(encoding="utf-8")
    css = (ROOT / "static/web-playground.css").read_text(encoding="utf-8")
    assert html.count('class="playground-form"') == 12
    assert html.count("run-button") == 12
    assert 'id="resetSimpleChatButton"' in html
    assert html.count('data-i18n="playground:') >= 70
    assert 'data-i18n="playground:actions.startPdfConversion"' in html
    assert 'data-i18n-aria-label="common:theme.switchToDark"' in html
    assert "Toggle dark mode" not in html
    intro_block = css.split(".playground-intro {", 1)[1].split("}", 1)[0]
    assert "color: var(--text);" in intro_block
    assert "opacity" not in intro_block


def test_playground_locale_rerender_is_in_place_and_does_not_restart_work() -> None:
    source = (ROOT / "static/web-playground.js").read_text(encoding="utf-8")
    locale_body = _function_body(source, "function rerenderLocale()")

    assert "i18n.subscribe(rerenderLocale)" in source
    assert "captureLocaleUiState()" in locale_body
    assert "updateDynamicTranslations()" in locale_body
    assert "updateRuntimeFormats()" in locale_body
    assert "updateModelOptionTranslations()" in locale_body
    assert "rerenderStatuses()" in locale_body
    assert "restoreLocaleUiState(" in locale_body

    for forbidden in (
        "loadModels(",
        "refreshAudioVoiceSelect(",
        "populateSelect(",
        "populateVoiceSelect(",
        "submitRequest(",
        "submitSimpleChatRequest(",
        "submitMultipartRequest(",
        "submitPdfJobRequest(",
        "fetchPdfJobJson(",
        "apiFetch(",
        "fetch(",
        "setTimeout(",
        ".innerHTML",
        "replaceChildren(",
    ):
        assert forbidden not in locale_body


def test_playground_localizes_runtime_text_and_keeps_raw_details_text_only() -> None:
    source = (ROOT / "static/web-playground.js").read_text(encoding="utf-8")

    assert "i18n.formatNumber" in source
    assert "i18n.formatCurrency" in source
    assert ".toLocaleString(" not in source
    assert "cost.toFixed(6)" not in source
    assert "prompt=" not in source
    assert "completion=" not in source
    assert "total=" not in source
    assert "credits=" not in source
    assert "data-playground-i18n" in source
    assert "data-playground-number" in source
    assert "data-playground-duration" in source
    assert "throw createLocalizedValidationError(KEYS.tooManyFiles" in source
    assert "error.statusKey ? null" in source
    assert "rawDetail.textContent" in source
    assert 'rawDetail.lang = "und"' in source
    assert 'rawDetail.dir = "auto"' in source
    assert "rawDetail.innerHTML" not in source
