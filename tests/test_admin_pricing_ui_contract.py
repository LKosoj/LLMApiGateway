from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PRICING_JS = ROOT / "static" / "pricing.js"
PRICING_HTML = ROOT / "static" / "pricing.html"
PRICING_CSS = ROOT / "static" / "pricing.css"


def test_pricing_javascript_is_valid_and_free_of_unsafe_html() -> None:
    result = subprocess.run(
        ["node", "--check", str(PRICING_JS)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    source = PRICING_JS.read_text(encoding="utf-8")
    css = PRICING_CSS.read_text(encoding="utf-8")
    assert "innerHTML" not in source
    assert "Number.isFinite" in source
    assert "'If-Match': baseEtag" in source
    assert "resp.status === 409" in source
    assert "window.gatewayUi.describeApiError" in source
    assert "rawDetailElement: rawDetail" in source
    assert "i18n.formatCurrency" in source
    assert "i18n.formatNumber" in source
    assert ".toFixed(" not in source
    assert ".toLocale" not in source
    assert "new Intl." not in source
    assert "calcResult.replaceChildren" not in source
    assert "input.setAttribute('data-i18n-aria-label', labelKey)" in source
    assert "input.setAttribute('aria-label', i18n.t(labelKey))" in source
    table_heading = css.split(".pricing-table th {", 1)[1].split("}", 1)[0]
    delete_button = css.split(".pricing-table .btn-delete {", 1)[1].split("}", 1)[0]
    assert "color: var(--text);" in table_heading
    assert "opacity" not in table_heading
    assert "color: var(--error);" in delete_button


def test_pricing_late_get_cannot_replace_a_newer_local_edit() -> None:
    source = PRICING_JS.read_text(encoding="utf-8")

    capture = source.index("const requestEditVersion = editVersion;")
    disable = source.index("loading = true;", capture)
    guard = source.index("if (editVersion !== requestEditVersion)", disable)
    assignment = source.index("state = loaded.items.map", guard)
    assert capture < disable < guard < assignment
    assert "tableBody.querySelectorAll('input, button')" in source
    assert "control.disabled = disabled" in source


def test_pricing_locale_handler_only_rerenders_localized_copy() -> None:
    source = PRICING_JS.read_text(encoding="utf-8")

    match = re.search(
        r"function renderLocalizedState\(\) \{(?P<body>.*?)\n    \}",
        source,
        re.DOTALL,
    )
    assert match is not None
    body = match.group("body")
    assert "toastStatus.rerender()" in body
    assert "renderCalculationResult()" in body
    for forbidden in (
        "renderTable(",
        "rebuildCalcSelect(",
        "loadPricing(",
        "saveBtn.click(",
        "calcBtn.click(",
        "apiFetch(",
    ):
        assert forbidden not in body
    assert "i18n.subscribe(renderLocalizedState)" in source


def test_pricing_conflict_ui_uses_strict_paired_i18n_catalogs() -> None:
    html = PRICING_HTML.read_text(encoding="utf-8")
    assert 'data-i18n-page="pricing"' in html
    assert '/static/vendor/ui-runtime.bundle.js' in html
    assert 'id="pricingConflictState"' in html
    assert 'role="alert"' in html
    assert 'tabindex="-1"' in html
    assert 'id="pricingRawDetail"' in html
    assert 'data-i18n-raw-detail' in html

    for binding in (
        'data-i18n="pricing:pageTitle"',
        'data-i18n="pricing:heading"',
        'data-i18n-aria-label="pricing:productVersionLabel"',
        'data-i18n="pricing:editor.addRow"',
        'data-i18n="pricing:editor.save"',
        'data-i18n="pricing:calculator.selectModel"',
        'data-i18n="pricing:calculator.result.model"',
    ):
        assert binding in html
    source = PRICING_JS.read_text(encoding="utf-8")
    assert "delBtn.setAttribute('data-i18n', 'pricing:editor.table.delete')" in source

    catalogs = {
        locale: json.loads(
            (ROOT / "static" / "locales" / locale / "pricing.json").read_text(
                encoding="utf-8"
            )
        )
        for locale in ("en", "ru")
    }
    assert catalogs["en"].keys() == catalogs["ru"].keys()
    assert catalogs["en"]["conflict"].keys() == catalogs["ru"]["conflict"].keys()
    assert set(catalogs["en"]["conflict"]) == {"title", "message", "reload"}

    def flatten(value: object, prefix: str = "") -> set[str]:
        assert isinstance(value, dict)
        result: set[str] = set()
        for key, child in value.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(child, dict):
                result.update(flatten(child, path))
            else:
                assert isinstance(child, str) and child
                result.add(path)
        return result

    expected_keys = {
        "pageTitle",
        "heading",
        "productVersionLabel",
        "editor.title",
        "editor.addRow",
        "editor.save",
        "editor.hintPrefix",
        "editor.hintUnit",
        "editor.hintMiddle",
        "editor.providersFile",
        "editor.hintSuffix",
        "editor.empty",
        "editor.table.provider",
        "editor.table.model",
        "editor.table.inputRate",
        "editor.table.outputRate",
        "editor.table.delete",
        "calculator.title",
        "calculator.model",
        "calculator.selectModel",
        "calculator.promptTokens",
        "calculator.completionTokens",
        "calculator.calculate",
        "calculator.result.model",
        "calculator.result.input",
        "calculator.result.output",
        "calculator.result.ratePerMillion",
        "validation.providerRequired",
        "validation.modelRequired",
        "validation.inputRateInvalid",
        "validation.outputRateInvalid",
        "status.mustLoad",
        "status.saved",
        "status.selectModel",
        "contract.missingEtag",
        "contract.invalidShape",
        "conflict.title",
        "conflict.message",
        "conflict.reload",
    }
    assert flatten(catalogs["en"]) == expected_keys
    assert flatten(catalogs["ru"]) == expected_keys

    inventory = json.loads(
        (ROOT / "scripts" / "i18n_inventory.json").read_text(encoding="utf-8")
    )
    pricing_exemptions = [
        exemption
        for exemption in inventory["exemptions"]
        if exemption["page"] == "pricing"
    ]
    assert pricing_exemptions == [
        {
            "page": "pricing",
            "file": "static/pricing.js",
            "kind": "js-sink",
            "line": 384,
            "column": 13,
            "text": "USD",
            "classification": "protocol",
        }
    ]
