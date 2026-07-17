"""R5.8 contracts for the Gateway Docs i18n migration."""

from __future__ import annotations

from hashlib import sha256
import json
from pathlib import Path
import re


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


def test_docs_catalogs_and_html_cover_page_prose() -> None:
    english = json.loads(
        (ROOT / "static/locales/en/docs.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (ROOT / "static/locales/ru/docs.json").read_text(encoding="utf-8")
    )
    required = {
        "page.documentTitle",
        "page.heading",
        "page.leadBeforeModel",
        "page.leadAfterModel",
        "tabs.api",
        "tabs.freeTier",
        "labels.required",
        "labels.optional",
        "labels.response",
        "catalog.loading",
        "catalog.ready",
        "catalog.unavailable",
        "catalog.errorHint",
        "catalog.empty",
        "catalog.groups.chat.title",
        "catalog.groups.chat.hint",
        "catalog.groups.webDeepResearch.title",
        "catalog.groups.webDeepResearch.hint",
        "freeTier.loading",
        "freeTier.unavailable",
        "freeTier.errorHint",
    }

    assert _leaf_keys(english) == _leaf_keys(russian)
    assert required <= _leaf_keys(english)

    html = (ROOT / "static/gateway-docs.html").read_text(encoding="utf-8")
    assert html.count("data-docs-api-base") == 9
    assert html.count('data-i18n="docs:') >= 120
    assert 'data-i18n="docs:page.heading"' in html
    assert 'data-i18n-aria-label="docs:tabs.ariaLabel"' in html
    assert 'id="catalogStatusDetail"' in html
    assert 'id="freeTierStatusDetail"' in html
    assert html.count("data-i18n-raw-detail") == 2


def test_docs_locale_rerender_uses_snapshot_without_refetching_or_rebuilding() -> None:
    source = (ROOT / "static/gateway-docs.js").read_text(encoding="utf-8")
    locale_body = _function_body(source, "function rerenderLocale()")
    catalog_locale_body = _function_body(source, "function renderCatalogLocale()")

    assert source.count('apiFetch("/v1/ui/docs/models")') == 1
    assert "const catalogState =" in source
    assert "i18n.subscribe(rerenderLocale)" in source
    assert "renderCatalogLocale()" in locale_body
    assert "renderTransportWarning()" in locale_body
    assert "loadFreeTierDoc(null, uiState)" in locale_body
    assert "dataset.catalogGroup" in source

    for forbidden in (
        "loadCatalog(",
        "apiFetch(",
        "renderCatalog(",
        "replaceChildren(",
        ".innerHTML",
    ):
        assert forbidden not in locale_body + catalog_locale_body


def test_docs_preserve_technical_examples_and_separate_raw_errors() -> None:
    html = (ROOT / "static/gateway-docs.html").read_text(encoding="utf-8")
    source = (ROOT / "static/gateway-docs.js").read_text(encoding="utf-8")
    blocks = re.findall(
        r'<pre tabindex="0"><code>(.*?)</code></pre>',
        html,
        re.DOTALL,
    )

    assert len(blocks) == 8
    assert html.count('<pre tabindex="0"><code>') == len(blocks)
    assert (
        sha256("\n\0\n".join(blocks).encode()).hexdigest()
        == "d5502763ab60f44bd5824950b9f72b1dbb0d3911d3c2dd814af74552e121a561"
    )
    assert "rawDetailElement: catalogStatusDetail" in source
    assert "rawDetailElement: freeTierStatusDetail" in source
    assert 'i18n.t("docs:freeTier.unavailable")' in source
    assert 'i18n.t("docs:freeTier.unavailable", { error:' not in source


def test_docs_optional_parameter_labels_use_accessible_text_color() -> None:
    css = (ROOT / "static/gateway-docs.css").read_text(encoding="utf-8")

    assert ".param-label {" in css
    assert "color: var(--text);" in css
    assert "body.dark-mode .param-label {\n    color: var(--text);\n}" in css
