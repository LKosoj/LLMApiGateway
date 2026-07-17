from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITOR_JS = ROOT / "static" / "editor.js"
EDITOR_SOURCE = ROOT / "frontend" / "editor" / "src"
EDITOR_HTML = ROOT / "static" / "rules-editor.html"
EDITOR_CSS = ROOT / "static" / "editor.css"


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


def test_rules_editor_uses_in_place_locale_rendering_without_request_or_state_resets() -> None:
    source = _editor_source()

    assert "function bindLocalizedText" in source
    assert "function bindLocalizedAttribute" in source
    assert "function rerenderLocale" in source
    assert "gatewayI18n.subscribe(ctx.rerenderLocale)" in source
    assert "gatewayI18n.getCollator" in source
    assert "gatewayI18n.formatNumber" in source
    assert "gatewayI18n.formatDate" in source
    assert "class LocalizedUiError extends ConfigUiError" in source
    assert "const KNOWN_CONFIG_ERROR_KEYS = new Map" in source
    assert "showClientValidationError(error)" in source
    assert "new Intl.Collator(undefined" not in source
    assert ".toLocaleString(" not in source

    rerender_source = source.split("function rerenderLocale", 1)[1].split(
        "\n    }", 1
    )[0]
    for forbidden in (
        "apiFetch(",
        "loadRulesEditor",
        "loadOperationRulesPayload",
        "loadOpenRouterFreeModels",
        "loadFallbackModelEvals",
        "rulesTabsController.activate",
        "rulesTabsController.repair",
        "providerModelsCache.clear",
        "providerModelsCacheEpoch",
        "providerCatalogGeneration",
        "setTimeout",
        "clearTimeout",
    ):
        assert forbidden not in rerender_source


def test_rules_editor_catalogs_are_strictly_paired_and_cover_dynamic_copy() -> None:
    catalogs = {
        locale: json.loads(
            (ROOT / "static" / "locales" / locale / "editor.json").read_text(
                encoding="utf-8"
            )
        )
        for locale in ("en", "ru")
    }

    assert catalogs["en"].keys() == catalogs["ru"].keys()
    assert {
        "actions",
        "catalog",
        "errors",
        "eval",
        "fields",
        "messages",
        "page",
        "preview",
        "sections",
        "tabs",
        "tooltips",
    } <= catalogs["en"].keys()
    assert catalogs["en"]["messages"].keys() == catalogs["ru"]["messages"].keys()
    assert catalogs["en"]["errors"].keys() == catalogs["ru"]["errors"].keys()
    assert catalogs["en"]["eval"].keys() == catalogs["ru"]["eval"].keys()
    assert catalogs["en"]["tooltips"].keys() == catalogs["ru"]["tooltips"].keys()

    source = _editor_source()
    for key in (
        "editor:errors.costRate",
        "editor:errors.fieldJson",
        "editor:errors.unknownProviders",
        "editor:errors.chooseProvider",
        "editor:errors.chooseAvailableModel",
        "editor:errors.gatewayName",
        "editor:errors.fallbackRequired",
        "editor:messages.loadedWithWarnings",
        "editor:messages.unavailableModels",
        "editor:eval.stateNotProbed",
        "editor:eval.refreshFullEval",
        "editor:eval.refreshLatencyOnly",
        "editor:eval.refreshManualEval",
    ):
        assert key in source

    assert "bindLocalizedText(heading, titleKey)" in source
    assert "buildFusionSectionHeading('editor:sections.fusion.mainHeading')" in source
    assert "buildFusionSectionHeading('editor:sections.router.targetsHeading')" in source
    assert "bindKnownActionText(addRouteButton, options.addRouteButtonText)" in source
    assert "bindKnownActionText(removeButton, options.removeButtonText)" in source
    assert "bindKnownActionText(removeButton, removeLabel)" in source
    assert "bindLocalizedText(hint, hintKey)" in source
    assert "bindLocalizedAttribute(button, 'title', tooltipKey)" in source
    assert "bindLocalizedText(popover, tooltipKey)" in source
    assert "bindLocalizedValue(score, () => ctx.formatNumber(model.score))" in source
    assert "formatUnavailableFallbackModelsMessage" not in source


def test_rules_editor_keeps_raw_detail_and_technical_samples_separate() -> None:
    html = EDITOR_HTML.read_text(encoding="utf-8")
    source = _editor_source()

    assert 'id="messageRawDetail"' in html
    assert "data-i18n-raw-detail" in html
    assert "rawDetailElement.textContent" in source
    assert "rawDetailElement.setAttribute('lang', 'und')" in source
    assert "rawDetailElement.setAttribute('dir', 'auto')" in source
    assert "rawDetailElement.innerHTML" not in source

    for sample in (
        "0.1",
        '{"temperature": 0.2}',
        '{"X-Provider": "value"}',
        "/images/generations",
        "${APIKEY_PROVIDER}",
    ):
        assert sample in source


def test_rules_editor_static_copy_uses_catalog_bindings() -> None:
    html = EDITOR_HTML.read_text(encoding="utf-8")
    css = EDITOR_CSS.read_text(encoding="utf-8")

    assert 'data-i18n="editor:page.title"' in html
    assert 'data-i18n="editor:tabs.rules"' in html
    assert 'data-i18n="editor:tabs.providers"' in html
    assert 'data-i18n="editor:sections.fallback.title"' in html
    assert 'data-i18n="editor:actions.saveConfiguration"' in html
    assert 'data-i18n-aria-label="editor:eval.openrouterGuideLabel"' in html
    format_tag = next(
        line
        for line in html.splitlines()
        if 'data-i18n="editor:sections.fallback.format"' in line
    )
    assert "opacity" not in format_tag
    assert ".rules-toolbar-copy span {\n    color: var(--text);" in css
    empty_state = css.split(".rules-empty-state {", 1)[1].split("}", 1)[0]
    assert "color: var(--text);" in empty_state
    assert "#messageArea.success {\n    background-color: var(--success-soft);\n    color: var(--text);" in css
