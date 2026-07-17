"""Focused contracts for the staged R5.8 literal inventory."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_i18n.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_i18n_r5_8", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _write_inventory_root(
    root: Path,
    *,
    html: str = "<p data-i18n=\"demo:ready\">Ready</p>\n",
    javascript: str = "status.textContent = i18n.t('demo:ready');\n",
    status: str = "complete",
    require_all_complete: bool = False,
    exemptions: list[dict[str, object]] | None = None,
) -> None:
    static = root / "static"
    locale_root = static / "locales"
    scripts = root / "scripts"
    runtime_root = root / "frontend" / "ui-runtime"
    locale_root.mkdir(parents=True, exist_ok=True)
    scripts.mkdir(parents=True, exist_ok=True)
    runtime_root.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(
        ROOT / "frontend" / "ui-runtime" / "scan-i18n-inventory.mjs",
        runtime_root / "scan-i18n-inventory.mjs",
    )
    node_modules = runtime_root / "node_modules"
    if not node_modules.exists():
        node_modules.symlink_to(
            ROOT / "frontend" / "ui-runtime" / "node_modules",
            target_is_directory=True,
        )
    (static / "demo.html").write_text(html, encoding="utf-8")
    (static / "demo.js").write_text(javascript, encoding="utf-8")
    registry = {
        "schemaVersion": 1,
        "defaultLocale": "en",
        "locales": [
            {"code": "en", "nativeLabel": "English", "dir": "ltr"},
            {"code": "ru", "nativeLabel": "Русский", "dir": "ltr"},
        ],
        "namespaces": ["demo"],
        "pageNamespaces": {"demo": ["demo"]},
    }
    (locale_root / "registry.json").write_text(
        json.dumps(registry),
        encoding="utf-8",
    )
    catalog = {
        "label": "Label",
        "ready": "Ready",
        "search": "Search",
        "translated": "Translated",
    }
    for locale in ("en", "ru"):
        locale_dir = locale_root / locale
        locale_dir.mkdir(exist_ok=True)
        (locale_dir / "demo.json").write_text(json.dumps(catalog), encoding="utf-8")
    (scripts / "i18n_inventory.json").write_text(
        json.dumps(
            {
                "schemaVersion": 2,
                "requireAllComplete": require_all_complete,
                "pages": {
                    "demo": {
                        "status": status,
                        "html": ["static/demo.html"],
                        "scripts": ["static/demo.js"],
                    }
                },
                "exemptions": exemptions or [],
            }
        ),
        encoding="utf-8",
    )


def test_inventory_accepts_bound_html_dynamic_js_and_explicit_locale(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html=(
            '<label data-i18n="demo:label">Label</label>\n'
            '<input placeholder="Search" data-i18n-placeholder="demo:search">\n'
        ),
        javascript=(
            "status.textContent = record.name;\n"
            "status.textContent = i18n.t('demo:ready');\n"
            "const count = gatewayI18n.formatNumber(value);\n"
            "const dates = gatewayI18n.formatDate(date);\n"
        ),
    )

    assert checker.validate_inventory(tmp_path) == []


def test_inventory_reports_visible_html_text_and_translatable_attributes(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html='<h1>Usage statistics</h1>\n<input placeholder="Search records">\n',
    )

    errors = checker.validate_inventory(tmp_path)

    assert any("html-text" in error and "Usage statistics" in error for error in errors)
    assert any(
        "html-attribute" in error and "Search records" in error for error in errors
    )


def test_inventory_treats_input_type_as_ascii_case_insensitive(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html='<input type="SUBMIT" value="Send request">\n',
    )

    errors = checker.validate_inventory(tmp_path)

    assert any(
        "html-attribute" in error and "Send request" in error for error in errors
    )


def test_inventory_requires_direct_valid_catalog_bindings(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html=(
            '<div data-i18n="demo:ready">Ready <span>Nested text</span></div>\n'
            '<p data-i18n=":ready">Malformed namespace</p>\n'
            '<p data-i18n="other:ready">Wrong namespace</p>\n'
            '<p data-i18n="demo:missing">Missing key</p>\n'
            '<input placeholder="Missing" data-i18n-placeholder="demo:missing">\n'
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    assert any("bound element must not contain child elements" in error for error in errors)
    assert any("invalid i18n binding: :ready" in error for error in errors)
    assert any("namespace is not registered for demo: other" in error for error in errors)
    assert sum("catalog key does not exist: demo:missing" in error for error in errors) == 2
    assert any("Nested text" in error for error in errors)


def test_inventory_rejects_duplicate_and_structurally_invalid_html(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html=(
            '<p data-i18n="demo:ready" data-i18n="demo:label">Ready</p>\n'
            "<div><span>Misnested</div></span>\n"
            "<section>Unclosed\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    assert any("duplicate HTML attribute: data-i18n" in error for error in errors)
    assert any("misnested closing tag: div" in error for error in errors)
    assert any("unexpected closing tag: span" in error for error in errors)
    assert any("unclosed HTML tag: section" in error for error in errors)


def test_inventory_reports_only_literal_js_user_facing_sinks(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "status.textContent = 'Loading records';\n"
            "status.textContent = record.status;\n"
            "status.textContent = '';\n"
            "button.setAttribute('aria-label', 'Retry request');\n"
            "button.setAttribute('aria-label', translatedLabel);\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    sink_errors = [error for error in errors if "js-sink" in error]
    assert len(sink_errors) == 2
    assert any("Loading records" in error for error in sink_errors)
    assert any("Retry request" in error for error in sink_errors)


def test_inventory_js_ast_ignores_comments_and_string_lookalikes(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "const sample = \"status.textContent = 'Not a sink'\";\n"
            "// renderMessage('error', 'Comment only');\n"
            "/* button.setAttribute('title', 'Comment only'); */\n"
            "status.textContent = record.status;\n"
        ),
    )

    assert checker.validate_inventory(tmp_path) == []


def test_inventory_set_attribute_name_is_ascii_case_insensitive(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript="button.setAttribute('ARIA-LABEL', 'Retry uppercase');\n",
    )

    errors = checker.validate_inventory(tmp_path)

    assert any("js-sink" in error and "Retry uppercase" in error for error in errors)


def test_inventory_js_ast_does_not_treat_bare_t_as_translation(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "function t(value) { return value; }\n"
            "status.textContent = t('Hardcoded user text');\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    assert any("js-sink" in error and "Hardcoded user text" in error for error in errors)


def test_inventory_reports_create_text_node_literal_only(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "document.createTextNode('Visible label');\n"
            "document.createTextNode(dynamicLabel);\n"
            "document.createTextNode(' ');\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    call_errors = [error for error in errors if "js-call-sink" in error]
    assert call_errors == [
        "[demo] static/demo.js:1:25 js-call-sink: Visible label"
    ]


def test_inventory_js_ast_finds_multiline_template_and_untranslated_branch(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "renderMessage(\n"
            "  'error',\n"
            "  failed ? i18n.t('demo:ready') : `Failed \\`request\\`: ${detail}`,\n"
            ");\n"
            "status.textContent = ready\n"
            "  ? i18n.t('demo:ready')\n"
            "  : 'Fallback status';\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    sink_errors = [error for error in errors if "js-" in error]
    assert len(sink_errors) == 2
    assert any("Failed \\`request\\`: ${detail}" in error for error in sink_errors)
    assert any("Fallback status" in error for error in sink_errors)
    assert not any("demo:ready" in error for error in errors)


def test_inventory_js_ast_finds_pure_interpolation_ternary_literals(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript="status.textContent = `${ok ? 'Ready' : 'Failed'}`;\n",
    )

    errors = checker.validate_inventory(tmp_path)

    sink_errors = [error for error in errors if "js-sink" in error]
    assert sink_errors == [
        "[demo] static/demo.js:1:30 js-sink: Ready",
        "[demo] static/demo.js:1:40 js-sink: Failed",
    ]


def test_inventory_js_ast_finds_nested_template_concat_and_logical_literal(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript="status.textContent = `${prefix + `${detail || 'Unknown detail'}`}`;\n",
    )

    errors = checker.validate_inventory(tmp_path)

    sink_errors = [error for error in errors if "js-sink" in error]
    assert len(sink_errors) == 1
    assert "Unknown detail" in sink_errors[0]


def test_inventory_js_ast_ignores_translation_call_in_interpolation(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript="status.textContent = `${i18n.t('demo:translated')}`;\n",
    )

    assert checker.validate_inventory(tmp_path) == []


def test_inventory_js_ast_interpolation_exemption_keeps_exact_location(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript="status.textContent = `${ok ? 'Ready' : 'Failed'}`;\n",
        exemptions=[
            {
                "page": "demo",
                "file": "static/demo.js",
                "kind": "js-sink",
                "line": 1,
                "column": 30,
                "text": "Ready",
                "classification": "sample",
            }
        ],
    )

    errors = checker.validate_inventory(tmp_path)

    assert errors == ["[demo] static/demo.js:1:40 js-sink: Failed"]


def test_inventory_reports_bounded_user_facing_call_sinks(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "renderMessage('error', 'Cannot save configuration');\n"
            "setStatus('audio', 'loading voices', false);\n"
            "setFreeTierStatus(`Catalog unavailable: ${detail}`, 'error');\n"
            "showToast('Pricing saved successfully');\n"
            "renderErrorWithDetails('Cannot load providers', detail);\n"
            "confirm('Delete this API key?');\n"
            "alert(i18n.t('demo:translated'));\n"
            "throw new Error('Internal invariant failed');\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    call_errors = [error for error in errors if "js-call-sink" in error]
    assert len(call_errors) == 6
    for literal in (
        "Cannot save configuration",
        "loading voices",
        "Catalog unavailable: ${detail}",
        "Pricing saved successfully",
        "Cannot load providers",
        "Delete this API key?",
    ):
        assert any(literal in error for error in call_errors)
    assert not any("demo:translated" in error for error in errors)
    assert not any("Internal invariant failed" in error for error in errors)


def test_inventory_rejects_all_direct_intl_and_ignores_comment_lookalikes(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "[].toLocaleString();\n"
            "value.toLocaleString(null);\n"
            "value.toLocaleString('ru');\n"
            "value.toLocaleDateString();\n"
            "value.toLocaleTimeString();\n"
            "value.toLocaleLowerCase('en');\n"
            "value.toLocaleUpperCase();\n"
            "service.toLocaleCache('entry');\n"
            "new Intl.DateTimeFormat([]);\n"
            "new Intl.NumberFormat(null);\n"
            "Intl.Collator('en');\n"
            "// value.toLocaleString(undefined);\n"
            "const sample = 'new Intl.DateTimeFormat(undefined)';\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    intl_errors = [error for error in errors if "js-intl" in error]
    assert len(intl_errors) == 10
    for method in (
        "toLocaleString",
        "toLocaleDateString",
        "toLocaleTimeString",
        "toLocaleLowerCase",
        "toLocaleUpperCase",
    ):
        assert any(method in error for error in intl_errors)
    assert any("Intl.DateTimeFormat" in error for error in intl_errors)
    assert not any("toLocaleCache" in error for error in intl_errors)
    assert not any("static/demo.js:12:" in error for error in intl_errors)


def test_inventory_rejects_locale_branching(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        javascript=(
            "if (currentLocale === 'ru') showRussian();\n"
            "switch (locale) { case 'en': showEnglish(); break; }\n"
            "const technicalLocale = 'ru';\n"
        ),
    )

    errors = checker.validate_inventory(tmp_path)

    branch_errors = [error for error in errors if "js-locale-branch" in error]
    assert len(branch_errors) == 2


def test_inventory_accepts_exact_technical_classifications(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html=(
            "<code>curl /v1/models</code>\n"
            '<pre>{"model":"demo"}</pre>\n'
            "<span>application/json</span>\n"
        ),
        javascript="sample.textContent = '{\"model\":\"demo\"}';\n",
        exemptions=[
            {
                "page": "demo",
                "file": "static/demo.html",
                "kind": "html-code",
                "line": 1,
                "column": 7,
                "text": "curl /v1/models",
                "classification": "code",
            },
            {
                "page": "demo",
                "file": "static/demo.html",
                "kind": "html-pre",
                "line": 2,
                "column": 6,
                "text": '{"model":"demo"}',
                "classification": "pre",
            },
            {
                "page": "demo",
                "file": "static/demo.html",
                "kind": "html-text",
                "line": 3,
                "column": 7,
                "text": "application/json",
                "classification": "protocol",
            },
            {
                "page": "demo",
                "file": "static/demo.js",
                "kind": "js-sink",
                "line": 1,
                "column": 22,
                "text": '{"model":"demo"}',
                "classification": "sample",
            },
        ],
    )

    assert checker.validate_inventory(tmp_path) == []


def test_inventory_exemption_matches_one_same_line_occurrence(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html="<span>Same text</span><span>Same text</span>\n",
        exemptions=[
            {
                "page": "demo",
                "file": "static/demo.html",
                "kind": "html-text",
                "line": 1,
                "column": 7,
                "text": "Same text",
                "classification": "sample",
            }
        ],
    )

    errors = checker.validate_inventory(tmp_path)

    literal_errors = [error for error in errors if "Same text" in error]
    assert literal_errors == [
        "[demo] static/demo.html:1:29 html-text: Same text"
    ]


def test_inventory_rejects_stale_and_nonexact_exemptions(tmp_path: Path) -> None:
    checker = _load_checker()
    stale = {
        "page": "demo",
        "file": "static/demo.html",
        "kind": "html-text",
        "line": 9,
        "column": 1,
        "text": "Missing literal",
        "classification": "sample",
    }
    _write_inventory_root(tmp_path, exemptions=[stale])

    errors = checker.validate_inventory(tmp_path)

    assert any("unused inventory exemption" in error for error in errors)

    stale["pattern"] = ".*"
    _write_inventory_root(tmp_path, exemptions=[stale])
    errors = checker.validate_inventory(tmp_path)
    assert any("inventory exemption 0 has an invalid schema" in error for error in errors)


def test_inventory_rejects_missing_or_undeclared_first_party_sources(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(tmp_path)
    manifest_path = tmp_path / "scripts" / "i18n_inventory.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"]["demo"]["scripts"] = ["static/missing.js"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    errors = checker.validate_inventory(tmp_path)

    assert any("missing inventory source: static/missing.js" in error for error in errors)
    assert any("undeclared first-party source: static/demo.js" in error for error in errors)

    manifest["pages"]["demo"]["scripts"] = ["static/vendor/generated.js"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    errors = checker.validate_inventory(tmp_path)
    assert any(
        "invalid inventory source for demo: static/vendor/generated.js" in error
        for error in errors
    )


def test_pending_pages_are_nonblocking_until_targeted_or_final_integration(
    tmp_path: Path,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html="<h1>Pending migration</h1>\n",
        status="pending",
    )

    assert checker.validate_inventory(tmp_path) == []
    assert any(
        "Pending migration" in error
        for error in checker.validate_inventory(tmp_path, selected_pages={"demo"})
    )

    _write_inventory_root(
        tmp_path,
        html="<h1>Pending migration</h1>\n",
        status="pending",
        require_all_complete=True,
    )
    errors = checker.validate_inventory(tmp_path)
    assert "inventory requires every declared page to be complete: demo" in errors


def test_targeted_pending_page_does_not_disable_completed_pages(tmp_path: Path) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html="<h1>Completed violation</h1>\n",
        status="complete",
    )
    static = tmp_path / "static"
    (static / "pending.html").write_text(
        "<h1>Targeted pending violation</h1>\n",
        encoding="utf-8",
    )
    (static / "pending.js").write_text(
        "status.textContent = i18n.t('demo:ready');\n",
        encoding="utf-8",
    )
    manifest_path = tmp_path / "scripts" / "i18n_inventory.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["pages"]["pending"] = {
        "status": "pending",
        "html": ["static/pending.html"],
        "scripts": ["static/pending.js"],
    }
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    registry_path = static / "locales" / "registry.json"
    registry = json.loads(registry_path.read_text(encoding="utf-8"))
    registry["pageNamespaces"]["pending"] = ["demo"]
    registry_path.write_text(json.dumps(registry), encoding="utf-8")

    errors = checker.validate_inventory(tmp_path, selected_pages={"pending"})

    assert any("Completed violation" in error for error in errors)
    assert any("Targeted pending violation" in error for error in errors)


def test_inventory_implementation_is_split_from_checker() -> None:
    checker_source = CHECKER.read_text(encoding="utf-8")

    assert (ROOT / "scripts" / "i18n_inventory.py").is_file()
    assert "def _scan_javascript" not in checker_source
    assert "class _VisibleHtmlScanner" not in checker_source


def test_main_page_option_enforces_a_pending_page(
    tmp_path: Path,
    monkeypatch,
    capsys,
) -> None:
    checker = _load_checker()
    _write_inventory_root(
        tmp_path,
        html="<h1>Targeted migration</h1>\n",
        status="pending",
    )
    monkeypatch.setattr(checker, "validate_structure", lambda root: [])
    subprocess_calls: list[tuple[tuple[object, ...], dict[str, object]]] = []
    original_run = subprocess.run

    def run(*args, **kwargs):
        command = args[0]
        if str(command[1]).endswith("scan-i18n-inventory.mjs"):
            return original_run(*args, **kwargs)
        subprocess_calls.append((args, kwargs))
        return subprocess.CompletedProcess(
            args=[], returncode=0, stdout="catalogs valid", stderr=""
        )

    monkeypatch.setattr(checker.subprocess, "run", run)

    assert checker.main(["--root", str(tmp_path)]) == 0
    command = subprocess_calls[0][0][0]
    assert command[1] == str(
        tmp_path / "frontend" / "ui-runtime" / "check-catalogs.mjs"
    )
    assert subprocess_calls[0][1]["cwd"] == tmp_path / "frontend" / "ui-runtime"
    capsys.readouterr()
    assert checker.main(["--root", str(tmp_path), "--page", "demo"]) == 1
    assert "Targeted migration" in capsys.readouterr().err
