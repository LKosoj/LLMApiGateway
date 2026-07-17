"""R5.8 contracts for the API Keys i18n migration."""

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


def test_api_keys_catalogs_and_html_cover_the_complete_page() -> None:
    english = json.loads(
        (ROOT / "static/locales/en/api_keys.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (ROOT / "static/locales/ru/api_keys.json").read_text(encoding="utf-8")
    )
    required = {
        "page.documentTitle",
        "page.heading",
        "page.description",
        "toolbar.refresh",
        "toolbar.create",
        "table.empty",
        "table.columns.name",
        "table.columns.key",
        "table.columns.status",
        "table.columns.spentBudget",
        "table.columns.reset",
        "table.columns.rateLimits",
        "table.columns.allowedModels",
        "table.columns.lastUsed",
        "table.status.active",
        "table.status.disabled",
        "table.actions.edit",
        "table.actions.delete",
        "table.scrollHint",
        "modal.createTitle",
        "modal.editTitle",
        "modal.fields.name",
        "modal.fields.budget",
        "modal.fields.budgetPeriod",
        "modal.fields.allowedModels",
        "modal.fields.metadata",
        "modal.actions.close",
        "modal.actions.save",
        "confirm.delete",
        "validation.nameRequired",
        "validation.invalidNumber",
        "validation.invalidInteger",
        "validation.metadataObject",
        "validation.invalidMetadata",
        "status.created",
        "status.updated",
        "status.deleted",
        "modelCatalogLoading",
        "modelCatalogEmpty",
        "modelCatalogError",
        "modelCatalogRetry",
    }
    assert _leaf_keys(english) == _leaf_keys(russian)
    assert required <= _leaf_keys(english)

    html = (ROOT / "static/api-keys.html").read_text(encoding="utf-8")
    assert 'data-i18n="api_keys:page.heading"' in html
    assert 'data-i18n-placeholder="api_keys:modal.placeholders.name"' in html
    assert 'id="messageDetail"' in html
    assert "Loading API keys..." not in html
    assert 'data-i18n="api_keys:toolbar.create"' in html


def test_locale_update_is_in_place_and_preserves_r5_4_state_contracts() -> None:
    source = (ROOT / "static/api-keys.js").read_text(encoding="utf-8")
    locale_body = _function_body(source, "function rerenderLocale()")
    table_body = _function_body(source, "function updateKeysTableLocale()")

    assert "i18n.subscribe(rerenderLocale)" in source
    assert "rerenderModelCatalogCopy()" in locale_body
    assert "messageStatus.rerender()" in locale_body
    assert "updateKeysTableLocale()" in locale_body
    assert "querySelectorAll" in table_body
    for forbidden in (
        "loadKeys(",
        "loadAvailableModels(",
        "renderKeysTable(",
        'apiFetch(',
        'fetch(',
        'method: "POST"',
        'method: "PATCH"',
        'method: "DELETE"',
        "replaceChildren(",
        ".innerHTML",
    ):
        assert forbidden not in locale_body + table_body

    assert "currentKeys" in source
    assert "selectedAllowedModels" in source
    assert "modelCatalogPromise" in source


def test_api_keys_use_runtime_formatting_safe_errors_and_localized_scroll_hint() -> None:
    source = (ROOT / "static/api-keys.js").read_text(encoding="utf-8")
    css = (ROOT / "static/api-keys.css").read_text(encoding="utf-8")

    assert "i18n.formatNumber" in source
    assert "i18n.formatCurrency" in source
    assert "i18n.formatDate" in source
    assert ".toFixed(" not in source
    assert ".toLocaleString(" not in source
    assert "rawDetailElement: messageDetail" in source
    assert "window.gatewayUi.describeApiError" in source
    assert 'content: attr(data-scroll-hint)' in css
    assert 'content: "Scroll horizontally"' not in css
    assert ".keys-table th {\n    color: var(--text);\n}" in css
    assert ".badge-active {\n    color: var(--text);" in css
