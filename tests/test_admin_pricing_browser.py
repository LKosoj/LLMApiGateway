from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "pricing-browser-master"


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {
                            "chat": {
                                "context_length": 131072,
                                "input_rate": 1.0,
                                "output_rate": 2.0,
                            }
                        },
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    sources = {
        "models_fallback_rules.json": "[]\n",
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def pricing_server(tmp_path: Path) -> Iterator[tuple[str, Path]]:
    providers_path = _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
            "FALLBACK_RULES_FILENAME": str(
                tmp_path / "models_fallback_rules.json"
            ),
            "MODEL_RULES_FILENAME": str(tmp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(
                tmp_path / "models_operation_rules.json"
            ),
            "FUSION_RULES_FILENAME": str(tmp_path / "models_fusion_rules.json"),
            "ROUTER_RULES_FILENAME": str(tmp_path / "models_router_rules.json"),
            "SESSION_COOKIE_SECURE": "false",
            "VERIFY_MODELS_ON_STARTUP": "off",
            "LOG_LEVEL": "WARNING",
        }
    )
    base_url = f"http://127.0.0.1:{port}"

    with isolated_gateway_process(env=env, temp_path=tmp_path) as process:
        wait_for_gateway(base_url, process)
        yield base_url, providers_path


def _login(context: BrowserContext, base_url: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/pricing"},
    )
    assert response.status == 200


def _open_pricing(context: BrowserContext, base_url: str) -> Page:
    _login(context, base_url)
    page = context.new_page()
    page.goto(f"{base_url}/v1/ui/pricing")
    expect(page.locator("#pricingTableBody tr")).to_have_count(1)
    expect(page.locator("#addRowBtn")).to_be_enabled()
    return page


def _input_rate(page: Page):
    return page.locator("#pricingTableBody tr input").nth(2)


def _dispatch_beforeunload(page: Page) -> dict[str, bool]:
    return page.evaluate(
        """
        () => {
            const event = new Event('beforeunload', {cancelable: true});
            const dispatchResult = window.dispatchEvent(event);
            return {defaultPrevented: event.defaultPrevented, dispatchResult};
        }
        """
    )


def test_pricing_conflict_late_get_and_delete_contract(
    browser: Browser,
    pricing_server: tuple[str, Path],
) -> None:
    base_url, providers_path = pricing_server
    context_a = browser.new_context(locale="en-US")
    context_b = browser.new_context(locale="en-US")
    try:
        page_a = _open_pricing(context_a, base_url)
        page_b = _open_pricing(context_b, base_url)

        _input_rate(page_a).fill("10")
        page_a.locator("#saveBtn").click()
        expect(page_a.locator("#pricingTableBody tr")).not_to_have_class(
            "dirty"
        )
        expect(_input_rate(page_a)).to_have_value("10")

        put_if_matches: list[str | None] = []
        page_b.on(
            "request",
            lambda request: (
                put_if_matches.append(request.headers.get("if-match"))
                if request.url.endswith("/v1/admin/pricing")
                and request.method == "PUT"
                else None
            ),
        )
        _input_rate(page_b).fill("11")
        page_b.locator("#saveBtn").click()
        conflict = page_b.locator("#pricingConflictState")
        expect(conflict).to_be_visible()
        expect(conflict).to_be_focused()
        expect(_input_rate(page_b)).to_have_value("11")
        expect(page_b.locator("#pricingTableBody tr")).to_have_class("dirty")
        assert _dispatch_beforeunload(page_b) == {
            "defaultPrevented": True,
            "dispatchResult": False,
        }

        with page_b.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as repeated_conflict:
            page_b.locator("#saveBtn").click()
        assert repeated_conflict.value.status == 409
        assert len(put_if_matches) == 2
        assert put_if_matches[0] is not None
        assert put_if_matches[1] == put_if_matches[0]
        expect(_input_rate(page_b)).to_have_value("11")
        expect(page_b.locator("#pricingTableBody tr")).to_have_class("dirty")
        assert _dispatch_beforeunload(page_b) == {
            "defaultPrevented": True,
            "dispatchResult": False,
        }

        page_b.evaluate(
            """
            () => {
                const originalFetch = window.fetch.bind(window);
                let delayed = false;
                window.fetch = (input, options = {}) => {
                    const url = typeof input === 'string' ? input : input.url;
                    const method = String(options.method || 'GET').toUpperCase();
                    if (!delayed && url.endsWith('/v1/admin/pricing') && method === 'GET') {
                        delayed = true;
                        return new Promise((resolve, reject) => {
                            window.__releasePricingGet = () => {
                                originalFetch(input, options).then(resolve, reject);
                            };
                        });
                    }
                    return originalFetch(input, options);
                };
            }
            """
        )
        page_b.locator("#reloadPricingBtn").click()
        page_b.wait_for_function("typeof window.__releasePricingGet === 'function'")
        expect(_input_rate(page_b)).to_be_disabled()
        page_b.evaluate(
            """
            () => {
                const input = document.querySelectorAll('#pricingTableBody tr input')[2];
                input.value = '13';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                window.__releasePricingGet();
            }
            """
        )
        expect(_input_rate(page_b)).to_be_enabled()
        expect(_input_rate(page_b)).to_have_value("13")
        expect(page_b.locator("#pricingTableBody tr")).to_have_class("dirty")
        expect(conflict).to_be_visible()

        page_b.locator("#reloadPricingBtn").click()
        expect(conflict).to_be_hidden()
        expect(_input_rate(page_b)).to_have_value("10")
        expect(page_b.locator("#pricingTableBody tr")).not_to_have_class(
            "dirty"
        )

        page_a.locator(".btn-delete").click()
        with page_a.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as delete_response:
            page_a.locator("#saveBtn").click()
        assert delete_response.value.status == 200
        expect(page_a.locator("#pricingTableBody tr")).to_have_count(0)
        expect(page_a.locator("#pricingEmptyState")).to_be_visible()
    finally:
        context_b.close()
        context_a.close()

    written = json.loads(providers_path.read_text(encoding="utf-8"))
    chat_metadata = written[0]["primary"]["models"]["chat"]
    assert chat_metadata == {"context_length": 131072}


def test_pricing_success_keeps_a_late_local_edit_and_adopts_new_etag(
    browser: Browser,
    pricing_server: tuple[str, Path],
) -> None:
    base_url, providers_path = pricing_server
    context = browser.new_context(locale="en-US")
    try:
        page = _open_pricing(context, base_url)
        put_if_matches: list[str | None] = []
        page.on(
            "request",
            lambda request: (
                put_if_matches.append(request.headers.get("if-match"))
                if request.url.endswith("/v1/admin/pricing")
                and request.method == "PUT"
                else None
            ),
        )
        page.evaluate(
            """
            () => {
                const originalFetch = window.fetch.bind(window);
                let delayed = false;
                window.fetch = (input, options = {}) => {
                    const url = typeof input === 'string' ? input : input.url;
                    const method = String(options.method || 'GET').toUpperCase();
                    if (!delayed && url.endsWith('/v1/admin/pricing') && method === 'PUT') {
                        delayed = true;
                        return new Promise((resolve, reject) => {
                            window.__releasePricingPut = () => {
                                originalFetch(input, options).then(resolve, reject);
                            };
                        });
                    }
                    return originalFetch(input, options);
                };
            }
            """
        )

        _input_rate(page).fill("7")
        page.locator("#saveBtn").click()
        page.wait_for_function("typeof window.__releasePricingPut === 'function'")
        expect(_input_rate(page)).to_be_disabled()
        page.evaluate(
            """
            () => {
                const input = document.querySelectorAll('#pricingTableBody tr input')[2];
                input.value = '8';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                window.__releasePricingPut();
            }
            """
        )
        expect(_input_rate(page)).to_be_enabled()
        expect(_input_rate(page)).to_have_value("8")
        expect(page.locator("#pricingTableBody tr")).to_have_class("dirty")
        assert _dispatch_beforeunload(page) == {
            "defaultPrevented": True,
            "dispatchResult": False,
        }

        with page.expect_response(
            lambda response: (
                response.url.endswith("/v1/admin/pricing")
                and response.request.method == "PUT"
            )
        ) as second_save:
            page.locator("#saveBtn").click()
        assert second_save.value.status == 200
        assert len(put_if_matches) == 2
        assert put_if_matches[0] is not None
        assert put_if_matches[1] is not None
        assert put_if_matches[1] != put_if_matches[0]
        expect(_input_rate(page)).to_have_value("8")
        expect(page.locator("#pricingTableBody tr")).not_to_have_class("dirty")
        assert _dispatch_beforeunload(page) == {
            "defaultPrevented": False,
            "dispatchResult": True,
        }
    finally:
        context.close()

    written = json.loads(providers_path.read_text(encoding="utf-8"))
    assert written[0]["primary"]["models"]["chat"]["input_rate"] == 8.0


def test_pricing_locale_rerender_preserves_state_and_inflight_requests(
    browser: Browser,
    pricing_server: tuple[str, Path],
) -> None:
    base_url, _providers_path = pricing_server
    context = browser.new_context(locale="en-US", viewport={"width": 390, "height": 844})
    peer_context = browser.new_context(locale="en-US")
    try:
        _login(context, base_url)
        page = context.new_page()
        page.add_init_script(
            """
            (() => {
                const nativeFetch = window.fetch.bind(window);
                const controls = {
                    calls: [],
                    holdGet: true,
                    holdPut: false,
                    holdCalculate: false,
                };
                window.__pricingFetchControls = controls;
                window.fetch = (input, options = {}) => {
                    const rawUrl = typeof input === 'string' ? input : input.url;
                    const url = new URL(rawUrl, window.location.href);
                    const method = String(options.method || 'GET').toUpperCase();
                    let kind = null;
                    if (url.pathname === '/v1/admin/pricing/calculate') kind = 'calculate';
                    else if (url.pathname === '/v1/admin/pricing') kind = method.toLowerCase();
                    if (kind !== null) {
                        const headers = new Headers(options.headers || {});
                        controls.calls.push({
                            kind,
                            method,
                            ifMatch: headers.get('If-Match'),
                        });
                    }
                    const execute = () => nativeFetch(input, options);
                    const shouldHold =
                        (kind === 'get' && controls.holdGet)
                        || (kind === 'put' && controls.holdPut)
                        || (kind === 'calculate' && controls.holdCalculate);
                    if (!shouldHold) return execute();
                    if (kind === 'get') controls.holdGet = false;
                    if (kind === 'put') controls.holdPut = false;
                    if (kind === 'calculate') controls.holdCalculate = false;
                    return new Promise((resolve, reject) => {
                        controls[`release_${kind}`] = () => {
                            delete controls[`release_${kind}`];
                            execute().then(resolve, reject);
                        };
                    });
                };
            })();
            """
        )
        initial_etags: list[str] = []
        page.on(
            "response",
            lambda response: (
                initial_etags.append(response.headers["etag"])
                if response.url.endswith("/v1/admin/pricing")
                and response.request.method == "GET"
                and "etag" in response.headers
                else None
            ),
        )
        page.goto(f"{base_url}/v1/ui/pricing")
        page.wait_for_function(
            "typeof window.__pricingFetchControls.release_get === 'function'"
        )
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(page.locator("html")).to_have_attribute("lang", "ru")
        assert page.evaluate("window.__pricingFetchControls.calls") == [
            {"kind": "get", "method": "GET", "ifMatch": None}
        ]
        page.evaluate("window.__pricingFetchControls.release_get()")
        expect(page.locator("#pricingTableBody tr")).to_have_count(1)
        row_inputs = page.locator("#pricingTableBody tr input")
        for index, label in enumerate(
            (
                "Провайдер",
                "Модель",
                "Входной тариф ($/1 млн)",
                "Выходной тариф ($/1 млн)",
            )
        ):
            expect(row_inputs.nth(index)).to_have_attribute("aria-label", label)
        assert len(initial_etags) == 1

        peer = _open_pricing(peer_context, base_url)
        _input_rate(peer).fill("10")
        peer.locator("#saveBtn").click()
        expect(peer.locator("#pricingTableBody tr")).not_to_have_class("dirty")

        page.route(
            "**/v1/admin/pricing/calculate",
            lambda route: route.fulfill(
                status=503,
                json={
                    "detail": "upstream <trace>",
                    "error": {"code": "auth_unavailable", "message": "ignored"},
                },
            ),
            times=1,
        )
        page.locator("#calcModel").select_option("primary::chat")
        page.locator("#calcBtn").click()
        expect(page.locator("#toast")).to_have_text("Сервис временно недоступен.")
        expect(page.locator("#pricingRawDetail")).to_have_text("upstream <trace>")
        expect(page.locator("#pricingRawDetail")).to_have_attribute("lang", "und")
        expect(page.locator("#pricingRawDetail")).to_have_attribute("dir", "auto")

        page.evaluate("window.__pricingFetchControls.holdCalculate = true")
        page.locator("#calcBtn").click()
        page.wait_for_function(
            "typeof window.__pricingFetchControls.release_calculate === 'function'"
        )
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        for index, label in enumerate(
            ("Provider", "Model", "Input rate ($/1M)", "Output rate ($/1M)")
        ):
            expect(row_inputs.nth(index)).to_have_attribute("aria-label", label)
        page.evaluate("window.__pricingFetchControls.release_calculate()")
        expect(page.locator("#calcResult")).to_be_visible()
        expect(page.locator("#calcCostValue")).to_have_text("$11.000000")

        rate_input = _input_rate(page)
        rate_input.fill("7")
        page.evaluate(
            """
            () => {
                const tableWrap = document.querySelector('.pricing-table-wrap');
                const modelInput = document.querySelectorAll('#pricingTableBody input')[1];
                window.__pricingRefs = {
                    row: document.querySelector('#pricingTableBody tr'),
                    inputs: [...document.querySelectorAll('#pricingTableBody input')],
                    result: document.querySelector('#calcResult'),
                    conflict: document.querySelector('#pricingConflictState'),
                };
                tableWrap.scrollLeft = 120;
                window.scrollTo(0, document.body.scrollHeight);
                modelInput.focus();
                modelInput.setSelectionRange(1, 3);
                window.__pricingView = {
                    tableScroll: tableWrap.scrollLeft,
                    windowScroll: window.scrollY,
                };
            }
            """
        )
        locale_call_count = page.evaluate("window.__pricingFetchControls.calls.length")
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(page.locator("#addRowBtn")).to_have_text("Добавить строку")
        expect(page.locator(".btn-delete")).to_have_text("Удалить")
        expect(page.locator("#calcCostValue")).to_contain_text("11,000000")
        assert page.evaluate("window.__pricingFetchControls.calls.length") == locale_call_count
        assert page.evaluate(
            """
            () => {
                const inputs = [...document.querySelectorAll('#pricingTableBody input')];
                const modelInput = inputs[1];
                const tableWrap = document.querySelector('.pricing-table-wrap');
                return {
                    sameNodes:
                        window.__pricingRefs.row === document.querySelector('#pricingTableBody tr')
                        && window.__pricingRefs.inputs.every((input, index) => input === inputs[index])
                        && window.__pricingRefs.result === document.querySelector('#calcResult')
                        && window.__pricingRefs.conflict === document.querySelector('#pricingConflictState'),
                    focused: document.activeElement === modelInput,
                    selection: [modelInput.selectionStart, modelInput.selectionEnd],
                    tableScroll: tableWrap.scrollLeft,
                    windowScrollStable:
                        Math.abs(window.scrollY - window.__pricingView.windowScroll) <= 4,
                    mode: window.Theme.get().mode,
                };
            }
            """
        ) == {
            "sameNodes": True,
            "focused": True,
            "selection": [1, 3],
            "tableScroll": page.evaluate("window.__pricingView.tableScroll"),
            "windowScrollStable": True,
            "mode": page.evaluate("window.Theme.get().mode"),
        }
        expect(rate_input).to_have_value("7")
        expect(page.locator("#pricingTableBody tr")).to_have_class("dirty")
        expect(page.locator("#calcModel")).to_have_value("primary::chat")
        expect(page.locator("#calcPromptTokens")).to_have_value("1000000")
        expect(page.locator("#calcCompletionTokens")).to_have_value("500000")

        page.evaluate("window.__pricingFetchControls.holdPut = true")
        page.locator("#saveBtn").click()
        page.wait_for_function(
            "typeof window.__pricingFetchControls.release_put === 'function'"
        )
        page.evaluate(
            """
            () => {
                const rateInput = document.querySelectorAll('#pricingTableBody input')[2];
                const modelInput = document.querySelectorAll('#pricingTableBody input')[1];
                rateInput.value = '8';
                rateInput.dispatchEvent(new Event('input', {bubbles: true}));
                modelInput.setSelectionRange(0, 2);
                window.__pricingHeldActive = document.activeElement;
            }
            """
        )
        held_call_count = page.evaluate("window.__pricingFetchControls.calls.length")
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
        assert page.evaluate("window.__pricingFetchControls.calls.length") == held_call_count
        assert page.evaluate(
            """
                () => {
                    const modelInput = document.querySelectorAll('#pricingTableBody input')[1];
                    return document.activeElement === window.__pricingHeldActive
                        && modelInput.selectionStart === 0
                        && modelInput.selectionEnd === 2;
            }
            """
        )
        page.evaluate("window.__pricingFetchControls.release_put()")
        conflict = page.locator("#pricingConflictState")
        expect(conflict).to_be_visible()
        expect(conflict).to_be_focused()
        expect(rate_input).to_have_value("8")
        expect(page.locator("#pricingTableBody tr")).to_have_class("dirty")
        pricing_calls = page.evaluate("window.__pricingFetchControls.calls")
        put_calls = [call for call in pricing_calls if call["kind"] == "put"]
        assert len(put_calls) == 1
        assert put_calls[0]["ifMatch"] == initial_etags[0]

        conflict_call_count = len(pricing_calls)
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(conflict).to_be_focused()
        assert page.evaluate("window.__pricingFetchControls.calls.length") == conflict_call_count
        assert _dispatch_beforeunload(page) == {
            "defaultPrevented": True,
            "dispatchResult": False,
        }
    finally:
        peer_context.close()
        context.close()
