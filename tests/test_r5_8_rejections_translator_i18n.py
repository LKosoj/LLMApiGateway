"""R5.8 contracts for the Rejections and Translator i18n migration."""

from __future__ import annotations

import importlib.util
import json
import os
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from playwright.sync_api import Browser, Page, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "scripts" / "check_i18n.py"
MASTER_KEY = "r5-8-rejections-translator-master"
EXPECTED_STEP_NAMES = (
    "client_request",
    "detected_format",
    "intermediate_openai_request",
    "target_request",
    "provider_response_mock",
    "intermediate_openai_response",
    "client_response",
)
EXEMPTION_FIELDS = (
    "page",
    "file",
    "kind",
    "line",
    "column",
    "text",
    "classification",
)
LANE_3_PAGES = {"rejections", "translator"}
EXPECTED_LANE_3_EXEMPTIONS = (
    ("rejections", "static/rejections.html", "html-text", 38, 50, "auth_invalid", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 39, 50, "key_disabled", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 40, 55, "model_not_allowed", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 41, 54, "budget_exhausted", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 42, 50, "rate_limited", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 43, 49, "master_only", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 44, 50, "unauthorized", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 45, 48, "ip_blocked", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 59, 40, "25", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 60, 49, "50", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 61, 41, "100", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 62, 41, "200", "protocol"),
    ("rejections", "static/rejections.html", "html-text", 83, 29, "X-Title", "protocol"),
    ("translator", "static/translator-debug.html", "html-text", 39, 44, "OpenAI", "protocol"),
    ("translator", "static/translator-debug.html", "html-text", 40, 47, "Anthropic", "protocol"),
    ("translator", "static/translator-debug.html", "html-text", 45, 47, "Anthropic", "protocol"),
    ("translator", "static/translator-debug.html", "html-text", 46, 44, "OpenAI", "protocol"),
    (
        "translator",
        "static/translator-debug.html",
        "html-attribute",
        50,
        87,
        '{"model":"gpt-4o","messages":[{"role":"user","content":"Hello"}]}',
        "sample",
    ),
)


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_i18n_lane_3", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _lane_3_exemptions(exemptions: list[dict]) -> Counter[tuple[object, ...]]:
    return Counter(
        tuple(exemption[field] for field in EXEMPTION_FIELDS)
        for exemption in exemptions
        if exemption.get("page") in LANE_3_PAGES
    )


def test_rejections_and_translator_have_complete_strict_inventory() -> None:
    inventory = json.loads(
        (ROOT / "scripts" / "i18n_inventory.json").read_text(encoding="utf-8")
    )
    assert inventory["pages"]["rejections"]["status"] == "complete"
    assert inventory["pages"]["translator"]["status"] == "complete"
    assert _lane_3_exemptions(inventory["exemptions"]) == Counter(
        EXPECTED_LANE_3_EXEMPTIONS
    )

    checker = _load_checker()
    assert checker.validate_inventory(
        ROOT,
        selected_pages={"rejections", "translator"},
    ) == []


def test_lane_3_exemption_contract_rejects_exact_record_drift() -> None:
    expected = Counter(EXPECTED_LANE_3_EXEMPTIONS)
    records = [
        dict(zip(EXEMPTION_FIELDS, record, strict=True))
        for record in EXPECTED_LANE_3_EXEMPTIONS
    ]
    pricing = {
        "page": "pricing",
        "file": "static/pricing.js",
        "kind": "js-sink",
        "line": 384,
        "column": 13,
        "text": "USD",
        "classification": "protocol",
    }

    assert _lane_3_exemptions(records[1:]) != expected
    assert _lane_3_exemptions([*records, {**records[0], "text": "unexpected"}]) != expected
    assert _lane_3_exemptions([{**records[0], "column": 51}, *records[1:]]) != expected
    assert _lane_3_exemptions([*records, pricing]) == expected


def test_rejections_and_translator_sources_use_locale_state_contracts() -> None:
    rejections = (ROOT / "static" / "rejections.js").read_text(encoding="utf-8")
    rejections_css = (ROOT / "static" / "rejections.css").read_text(
        encoding="utf-8"
    )
    rejections_html = (ROOT / "static" / "rejections.html").read_text(
        encoding="utf-8"
    )
    translator = (ROOT / "static" / "translator-debug.js").read_text(
        encoding="utf-8"
    )
    translator_html = (ROOT / "static" / "translator-debug.html").read_text(
        encoding="utf-8"
    )
    translator_css = (ROOT / "static" / "translator-debug.css").read_text(
        encoding="utf-8"
    )

    for source in (rejections, translator):
        assert "gatewayI18n" in source
        assert ".subscribe(" in source
        assert "createStatus" in source
        assert "toLocaleString" not in source
        assert "toLocaleTimeString" not in source
        assert "new Intl." not in source

    assert "requestGeneration" in rejections
    assert "stepsSnapshot" in translator
    assert "EXPECTED_STEP_NAMES" in translator
    assert ".rej-table th {\n    color: var(--text);" in rejections_css
    assert '<div class="rej-table-wrap" tabindex="0">' in rejections_html
    mock_hint = next(
        line
        for line in translator_html.splitlines()
        if 'data-i18n="translator:input.mockHint"' in line
    )
    empty_hint = translator_html.split(
        'data-i18n="translator:steps.emptyBefore"', 1
    )[0].splitlines()[-1]
    assert "opacity" not in mock_hint
    assert "opacity" not in empty_hint
    assert "@import" not in translator_css

    for namespace in ("rejections", "translator"):
        english = json.loads(
            (ROOT / "static" / "locales" / "en" / f"{namespace}.json").read_text(
                encoding="utf-8"
            )
        )
        russian = json.loads(
            (ROOT / "static" / "locales" / "ru" / f"{namespace}.json").read_text(
                encoding="utf-8"
            )
        )
        assert _leaf_keys(english) == _leaf_keys(russian)


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


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"chat": {"input_rate": 1.0, "output_rate": 2.0}},
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    for filename, content in {
        "models_fallback_rules.json": "[]\n",
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture(scope="module")
def lane_3_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    temp_path = tmp_path_factory.mktemp("r5-8-lane-3")
    providers_path = _write_config(temp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
            "FALLBACK_RULES_FILENAME": str(temp_path / "models_fallback_rules.json"),
            "MODEL_RULES_FILENAME": str(temp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(temp_path / "models_operation_rules.json"),
            "FUSION_RULES_FILENAME": str(temp_path / "models_fusion_rules.json"),
            "ROUTER_RULES_FILENAME": str(temp_path / "models_router_rules.json"),
            "SESSION_COOKIE_SECURE": "false",
            "VERIFY_MODELS_ON_STARTUP": "off",
            "LOG_LEVEL": "WARNING",
        }
    )
    base_url = f"http://127.0.0.1:{port}"
    with isolated_gateway_process(env=env, temp_path=temp_path) as process:
        wait_for_gateway(base_url, process)
        yield base_url


def _login(browser: Browser, base_url: str):
    context = browser.new_context(permissions=["clipboard-read", "clipboard-write"])
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/rejections"},
    )
    assert response.status == 200
    return context


def _rejection_items(prefix: str, count: int, *, start: int = 0) -> list[dict]:
    return [
        {
            "timestamp": "2026-07-15T08:30:00+00:00",
            "category": "rate_limited",
            "status_code": 429,
            "method": "POST",
            "path": f"/v1/raw/{prefix}/{index + start}",
            "api_key_id": 7,
            "x_title": "Raw Client",
            "client_ip": "192.0.2.15",
            "reason": f"RAW_REASON_{prefix}_{index + start}",
            "request_id": f"req-{prefix}-{index + start}",
        }
        for index in range(count)
    ]


def _translation_steps() -> list[dict]:
    return [
        {
            "step": index,
            "name": name,
            "title": f"BACKEND TITLE {index}",
            "payload": {
                "marker": f"RAW_PAYLOAD_{index}",
                "content": "x" * 400,
            },
            "notes": f"BACKEND NOTES {index}",
        }
        for index, name in enumerate(EXPECTED_STEP_NAMES, start=1)
    ]


def _change_locale_without_focus(page: Page, locale: str) -> None:
    page.evaluate("() => window.gatewayNav.ready")
    page.locator("#localeSelect").evaluate(
        """(select, value) => {
            select.value = value;
            select.dispatchEvent(new Event('change', {bubbles: true}));
        }""",
        locale,
    )
    expect(page.locator("html")).to_have_attribute("lang", locale)


@pytest.mark.browser
def test_rejections_and_translator_locale_state_and_races(
    browser: Browser,
    lane_3_server: str,
) -> None:
    context = _login(browser, lane_3_server)
    page_errors: list[str] = []
    try:
        rejections = context.new_page()
        rejections.on("pageerror", lambda error: page_errors.append(str(error)))
        rejections.set_viewport_size({"width": 390, "height": 844})
        rejection_routes: list = []
        rejections.route(
            "**/v1/admin/rejections*",
            lambda route: rejection_routes.append(route),
        )
        with rejections.expect_request("**/v1/admin/rejections*"):
            rejections.goto(
                f"{lane_3_server}/v1/ui/rejections",
                wait_until="domcontentloaded",
            )
        rejections.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(rejection_routes) == 1

        _change_locale_without_focus(rejections, "ru")
        assert len(rejection_routes) == 1
        with rejections.expect_request("**/v1/admin/rejections*"):
            rejections.locator("#refreshBtn").click()
        rejections.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(rejection_routes) == 2
        rejection_routes[1].fulfill(
            json={"items": _rejection_items("current", 25), "total": 60}
        )
        expect(rejections.locator("#rejTableBody tr")).to_have_count(25)
        stale_request = rejection_routes[0].request
        with rejections.expect_response(
            lambda response: response.request is stale_request
            and response.url == stale_request.url
        ):
            rejection_routes[0].fulfill(
                json={"items": _rejection_items("stale", 1), "total": 1}
            )
        rejections.evaluate(
            "() => new Promise(resolve => requestAnimationFrame(() => "
            "requestAnimationFrame(resolve)))"
        )
        expect(rejections.locator("#rejTableBody")).to_contain_text(
            "RAW_REASON_current_0"
        )
        expect(rejections.locator("#rejTableBody")).not_to_contain_text(
            "RAW_REASON_stale_0"
        )

        rejections.locator("#filterCategory").select_option("rate_limited")
        rejections.locator("#filterKeyId").fill("7")
        rejections.locator("#filterSince").fill("2026-07-01T10:30")
        with rejections.expect_request("**/v1/admin/rejections*"):
            rejections.locator("#filterLimit").select_option("25")
        rejections.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(rejection_routes) == 3
        rejection_routes[2].fulfill(
            json={"items": _rejection_items("filtered", 25), "total": 60}
        )
        with rejections.expect_request("**/v1/admin/rejections*"):
            rejections.locator("#nextPageBtn").click()
        rejections.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(rejection_routes) == 4
        assert "offset=25" in rejection_routes[3].request.url
        rejection_routes[3].fulfill(
            json={
                "items": _rejection_items("page-two", 25, start=25),
                "total": 60,
            }
        )
        expect(rejections.locator("#pageInfo")).to_contain_text("26")

        state_before = rejections.evaluate(
            """() => {
                const table = document.querySelector('.rej-table-wrap');
                table.scrollLeft = 220;
                window.scrollTo(0, document.body.scrollHeight);
                const key = document.querySelector('#filterKeyId');
                key.focus();
                return {
                    scrollLeft: table.scrollLeft,
                    scrollY: window.scrollY,
                    rawCells: [...document.querySelectorAll('#rejTableBody tr')].map(
                        (row) => [...row.cells].slice(1).map((cell) => cell.textContent)
                    ),
                };
            }"""
        )
        domain_count = len(rejection_routes)
        _change_locale_without_focus(rejections, "en")
        assert len(rejection_routes) == domain_count
        assert rejections.locator("#filterCategory").input_value() == "rate_limited"
        assert rejections.locator("#filterKeyId").input_value() == "7"
        assert rejections.locator("#filterSince").input_value() == "2026-07-01T10:30"
        assert rejections.locator("#filterLimit").input_value() == "25"
        assert rejections.evaluate("document.activeElement.id") == "filterKeyId"
        assert rejections.evaluate(
            "document.querySelector('.rej-table-wrap').scrollLeft"
        ) == state_before["scrollLeft"]
        rejection_scroll = rejections.evaluate(
            """() => ({
                y: window.scrollY,
                max: Math.max(0, document.documentElement.scrollHeight - innerHeight),
            })"""
        )
        expected_scroll = min(state_before["scrollY"], rejection_scroll["max"])
        assert abs(rejection_scroll["y"] - expected_scroll) <= 2
        assert rejections.evaluate(
            """() => [...document.querySelectorAll('#rejTableBody tr')].map(
                (row) => [...row.cells].slice(1).map((cell) => cell.textContent)
            )"""
        ) == state_before["rawCells"]
        expect(rejections.locator("#pageInfo")).to_contain_text("26–50 of 60")

        with rejections.expect_request("**/v1/admin/rejections*"):
            rejections.locator("#refreshBtn").click()
        rejections.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(rejection_routes) == 5
        rejection_routes[4].fulfill(
            status=400,
            content_type="application/json",
            body=json.dumps({"detail": "RAW_BACKEND_FILTER_DETAIL"}),
        )
        expect(rejections.locator("#messageArea")).to_contain_text(
            "Invalid filter values"
        )
        expect(rejections.locator("#messageDetail")).to_have_text(
            "RAW_BACKEND_FILTER_DETAIL"
        )
        expect(rejections.locator("#messageDetail")).to_have_attribute("lang", "und")
        _change_locale_without_focus(rejections, "ru")
        expect(rejections.locator("#messageArea")).to_contain_text(
            "Некорректные значения фильтров"
        )
        assert len(rejection_routes) == 5

        translator = context.new_page()
        translator.on("pageerror", lambda error: page_errors.append(str(error)))
        translator.set_viewport_size({"width": 1440, "height": 900})
        translation_routes: list = []
        translator.route(
            "**/v1/admin/translator/debug",
            lambda route: translation_routes.append(route),
        )
        translator.goto(
            f"{lane_3_server}/v1/ui/translator-debug",
            wait_until="domcontentloaded",
        )
        expect(translator.locator("html")).to_have_attribute("lang", "ru")
        request_text = json.dumps(
            {"model": "gpt-4o", "messages": [{"role": "user", "content": "RAW"}]},
            ensure_ascii=False,
            indent=2,
        )
        mock_text = json.dumps({"id": "raw-mock", "content": "RAW_MOCK"}, indent=2)
        translator.locator("#requestInput").fill(request_text)
        translator.locator("#mockResponse").fill(mock_text)
        with translator.expect_request("**/v1/admin/translator/debug"):
            translator.locator("#runBtn").click()
        translator.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(translation_routes) == 1
        expect(translator.locator("#runBtn")).to_contain_text("Выполняется")
        _change_locale_without_focus(translator, "en")
        expect(translator.locator("#runBtn")).to_contain_text("Running")
        assert len(translation_routes) == 1
        translation_routes[0].fulfill(json={"steps": _translation_steps()})
        expect(translator.locator(".step-card")).to_have_count(7)
        expect(translator.locator(".step-label").first).to_contain_text(
            "Step 1: Client request"
        )
        expect(translator.locator("#stepsContainer")).not_to_contain_text(
            "BACKEND TITLE"
        )
        expect(translator.locator("#stepsContainer")).not_to_contain_text(
            "BACKEND NOTES"
        )

        first_card = translator.locator('.step-card[data-step-name="client_request"]')
        first_card.locator("details").evaluate("element => { element.open = true; }")
        payload = first_card.locator("textarea")
        payload_value = payload.input_value()
        translator.evaluate(
            """() => {
                const card = document.querySelector('.step-card');
                card.dataset.identityMarker = 'preserved';
                const textarea = card.querySelector('textarea');
                textarea.focus();
                textarea.setSelectionRange(5, 20, 'forward');
                textarea.scrollTop = 60;
                textarea.scrollLeft = 10;
                window.scrollTo(0, document.body.scrollHeight);
            }"""
        )
        translator_state = translator.evaluate(
            """() => {
                const textarea = document.querySelector('.step-card textarea');
                return {
                    selectionStart: textarea.selectionStart,
                    selectionEnd: textarea.selectionEnd,
                    scrollTop: textarea.scrollTop,
                    scrollLeft: textarea.scrollLeft,
                    scrollY: window.scrollY,
                };
            }"""
        )
        _change_locale_without_focus(translator, "ru")
        expect(first_card).to_have_attribute("data-identity-marker", "preserved")
        expect(first_card.locator("details")).to_have_attribute("open", "")
        assert payload.input_value() == payload_value
        assert translator.evaluate("document.activeElement === document.querySelector('.step-card textarea')")
        assert translator.evaluate("document.activeElement.selectionStart") == translator_state[
            "selectionStart"
        ]
        assert translator.evaluate("document.activeElement.selectionEnd") == translator_state[
            "selectionEnd"
        ]
        assert translator.evaluate("document.activeElement.scrollTop") == translator_state[
            "scrollTop"
        ]
        assert translator.evaluate("document.activeElement.scrollLeft") == translator_state[
            "scrollLeft"
        ]
        assert (
            abs(translator.evaluate("window.scrollY") - translator_state["scrollY"])
            <= 2
        )
        assert len(translation_routes) == 1

        first_card.locator(".btn-copy").click()
        expect(first_card.locator(".btn-copy")).to_have_text("Скопировано!")
        _change_locale_without_focus(translator, "en")
        expect(first_card.locator(".btn-copy")).to_have_text("Copied!")
        expect(first_card.locator(".btn-copy")).to_have_text("Copy", timeout=3_000)
        assert len(translation_routes) == 1

        with translator.expect_request("**/v1/admin/translator/debug"):
            translator.locator("#runBtn").click()
        translator.evaluate("() => new Promise(requestAnimationFrame)")
        assert len(translation_routes) == 2
        unknown_steps = _translation_steps()
        unknown_steps[-1]["name"] = "unexpected_step"
        translation_routes[1].fulfill(json={"steps": unknown_steps})
        expect(translator.locator("#errorBanner")).to_contain_text(
            "unsupported translation step"
        )
        expect(translator.locator("#errorDetail")).to_have_text("unexpected_step")
        expect(translator.locator("#errorDetail")).to_have_attribute("dir", "auto")
        expect(translator.locator(".step-card")).to_have_count(0)

        translator.locator("#requestInput").fill("{broken-json")
        translator.locator("#runBtn").click()
        expect(translator.locator("#errorBanner")).to_contain_text(
            "Invalid JSON in the request body"
        )
        expect(translator.locator("#errorDetail")).not_to_be_empty()
        assert len(translation_routes) == 2
        assert page_errors == []
    finally:
        context.close()
