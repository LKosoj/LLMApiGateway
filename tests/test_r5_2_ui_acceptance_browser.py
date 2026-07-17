from __future__ import annotations

import json
import os
import time
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

MASTER_KEY = "r5-2-browser-master"
AXE_PATH = (
    Path(__file__).resolve().parents[1]
    / "frontend"
    / "ui-runtime"
    / "node_modules"
    / "axe-core"
    / "axe.min.js"
)
MASTER_PAGES = (
    "/v1/ui/docs",
    "/v1/ui/usage-stats",
    "/v1/ui/quota",
    "/v1/ui/rules-editor",
    "/v1/ui/playground",
    "/v1/ui/api-keys",
    "/v1/ui/rejections",
    "/v1/ui/translator-debug",
    "/v1/ui/pricing",
)
USER_PAGES = MASTER_PAGES[:3]


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
        "models_operation_rules.json": json.dumps(
            {
                "embeddings": [],
                "rerank": [],
                "images_generations": [],
                "images_edits": [],
            }
        ),
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture(scope="module")
def r5_2_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    temp_path = tmp_path_factory.mktemp("r5-2-ui")
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
            "FALLBACK_RULES_FILENAME": str(
                temp_path / "models_fallback_rules.json"
            ),
            "MODEL_RULES_FILENAME": str(temp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(
                temp_path / "models_operation_rules.json"
            ),
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


def _login(context: BrowserContext, base_url: str, api_key: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": api_key, "next": "/v1/ui/docs"},
    )
    assert response.status == 200, response.text()


def _create_virtual_key(context: BrowserContext, base_url: str) -> str:
    _login(context, base_url, MASTER_KEY)
    response = context.request.post(
        f"{base_url}/v1/admin/api-keys",
        data={"name": "r5-2-browser-user"},
    )
    assert response.status == 201, response.text()
    return str(response.json()["api_key"])


def _wait_for_navigation(page: Page) -> None:
    page.wait_for_function(
        "window.gatewayNav && window.gatewayAuth && "
        "window.gatewayAuth.state !== 'pending'"
    )
    page.evaluate("() => window.gatewayNav.ready")


def _assert_current_link_is_visible(page: Page) -> None:
    scroller = page.locator(".gateway-nav-scroller")
    current = page.locator('[data-gateway-nav] [aria-current="page"]')
    expect(current).to_have_count(1)
    bounds = page.evaluate(
        """
        () => {
            const viewport = document.querySelector('.gateway-nav-scroller')
                .getBoundingClientRect();
            const current = document.querySelector(
                '[data-gateway-nav] [aria-current="page"]'
            ).getBoundingClientRect();
            return {
                viewportLeft: viewport.left,
                viewportRight: viewport.right,
                currentLeft: current.left,
                currentRight: current.right,
            };
        }
        """
    )
    assert bounds["currentLeft"] >= bounds["viewportLeft"] - 1
    assert bounds["currentRight"] <= bounds["viewportRight"] + 1
    expect(scroller).to_be_visible()


def _assert_axe_no_high_impact(page: Page, selector: str) -> None:
    page.add_script_tag(path=str(AXE_PATH))
    violations = page.evaluate(
        """
        async (selector) => {
            const context = selector === 'document'
                ? document
                : document.querySelector(selector);
            if (!context) throw new Error(`Missing axe context: ${selector}`);
            const result = await axe.run(context, {
                runOnly: {type: 'tag', values: ['wcag2a', 'wcag2aa']},
                resultTypes: ['violations'],
            });
            return result.violations
                .filter((item) => item.impact === 'critical' || item.impact === 'serious')
                .map((item) => ({
                    id: item.id,
                    impact: item.impact,
                    nodes: item.nodes.map((node) => ({
                        target: node.target,
                        summary: node.failureSummary,
                    })),
                }));
        }
        """,
        selector,
    )
    assert violations == []


def _assert_manual_tab_keyboard(page: Page, tablist_selector: str) -> None:
    tabs = page.locator(f"{tablist_selector} > [role=tab]:visible")
    assert tabs.count() >= 2
    selected = page.locator(
        f'{tablist_selector} > [role=tab][aria-selected="true"]:visible'
    )
    expect(selected).to_have_count(1)
    selected_id = selected.get_attribute("id")
    selected.focus()
    page.keyboard.press("ArrowRight")
    focused = page.locator(f"{tablist_selector} > [role=tab]:focus")
    expect(focused).to_have_count(1)
    focused_id = focused.get_attribute("id")
    assert focused_id != selected_id
    expect(selected).to_have_attribute("aria-selected", "true")
    page.keyboard.press("Enter")
    activated = page.locator(f"#{focused_id}")
    expect(activated).to_have_attribute("aria-selected", "true")
    expect(activated).to_have_attribute("tabindex", "0")
    _assert_axe_no_high_impact(page, tablist_selector)


def test_master_and_user_navigation_matrix_has_exact_role_scope_and_visible_active_link(
    browser: Browser,
    r5_2_server: str,
) -> None:
    master = browser.new_context(viewport={"width": 390, "height": 844})
    user = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        virtual_key = _create_virtual_key(master, r5_2_server)
        _login(user, r5_2_server, virtual_key)

        for context, pages, expected_links in (
            (master, MASTER_PAGES, 9),
            (user, USER_PAGES, 3),
        ):
            page = context.new_page()
            for index, path in enumerate(pages):
                width = (390, 768, 1440)[index % 3]
                page.set_viewport_size({"width": width, "height": 900})
                page.goto(f"{r5_2_server}{path}")
                _wait_for_navigation(page)
                expect(page.locator("[data-gateway-nav]")).to_be_visible()
                expect(page.locator("[data-gateway-nav] a")).to_have_count(expected_links)
                _assert_current_link_is_visible(page)
                _assert_axe_no_high_impact(page, ".top-nav")
            page.close()
    finally:
        master.close()
        user.close()


def test_delayed_and_unknown_identity_are_single_fetch_and_fail_closed(
    browser: Browser,
    r5_2_server: str,
) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844})
    try:
        _login(context, r5_2_server, MASTER_KEY)
        page = context.new_page()
        page.add_init_script(
            "localStorage.setItem('llmgateway:locale', 'en');"
            "localStorage.setItem('llmgateway:theme', 'light');"
        )
        page.add_init_script(
            """
            (() => {
                const nativeFetch = window.fetch.bind(window);
                let releaseLocaleCatalog;
                let localeCatalogReleased = false;
                const localeCatalogGate = new Promise((resolve) => {
                    releaseLocaleCatalog = resolve;
                });
                window.__authStartedBeforeLocaleCatalogRelease = false;
                window.__releaseLocaleCatalog = () => {
                    localeCatalogReleased = true;
                    releaseLocaleCatalog();
                };
                window.fetch = async (input, init) => {
                    const rawUrl = typeof input === 'string' ? input : input.url;
                    const url = new URL(rawUrl, window.location.href);
                    if (url.pathname === '/auth/me') {
                        window.__authStartedBeforeLocaleCatalogRelease =
                            !localeCatalogReleased;
                    }
                    const response = await nativeFetch(input, init);
                    if (url.pathname === '/static/locales/en/common.json') {
                        await localeCatalogGate;
                    }
                    return response;
                };
            })();
            """
        )
        page.add_init_script(
            """
            window.__pendingRoleLeaks = [];
            document.addEventListener('DOMContentLoaded', () => {
                const check = () => {
                    if (window.gatewayAuth?.state !== 'pending') return;
                    const nav = document.querySelector('[data-gateway-nav]');
                    const gated = [...document.querySelectorAll(
                        '[data-master-only], [data-user-only]'
                    )];
                    if ((nav && !nav.hidden) || gated.some((node) => !node.hidden)) {
                        window.__pendingRoleLeaks.push('visible-before-auth');
                    }
                };
                new MutationObserver(check).observe(document.body, {
                    attributes: true,
                    subtree: true,
                    attributeFilter: ['hidden'],
                });
                check();
            }, {once: true});
            """
        )
        request_count = 0
        health_request_count = 0

        def delayed_identity(route) -> None:
            nonlocal request_count
            request_count += 1
            time.sleep(0.25)
            route.fulfill(status=200, json={"role": "master", "key_id": None})

        def count_health(route) -> None:
            nonlocal health_request_count
            health_request_count += 1
            route.continue_()

        page.route(f"{r5_2_server}/auth/me", delayed_identity)
        page.route(f"{r5_2_server}/health", count_health)
        page.goto(f"{r5_2_server}/v1/ui/rules-editor", wait_until="commit")
        page.wait_for_function(
            "window.__authStartedBeforeLocaleCatalogRelease === true"
        )
        page.evaluate("window.__releaseLocaleCatalog()")
        page.wait_for_load_state("load")
        _wait_for_navigation(page)
        assert request_count == 1
        assert health_request_count == 1
        assert page.evaluate("window.__pendingRoleLeaks") == []

        theme_toggle = page.locator("#darkModeToggle")
        version = page.locator("[data-product-version]")
        expect(theme_toggle).to_have_attribute("aria-label", "Switch to dark mode")
        expect(theme_toggle).to_have_attribute("title", "Switch to dark mode")
        expect(version).to_have_attribute("data-product-version-state", "ready")
        version_text = version.inner_text()
        assert version_text.startswith("(v")
        page.evaluate(
            """
            () => {
                window.__f2ThemeToggle = document.querySelector('#darkModeToggle');
                window.__f2Version = document.querySelector('[data-product-version]');
                window.__f2ThemeToggle.focus();
            }
            """
        )

        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(theme_toggle).to_have_attribute(
            "aria-label", "Переключить на тёмную тему"
        )
        expect(theme_toggle).to_have_attribute(
            "title", "Переключить на тёмную тему"
        )
        expect(version).to_have_text(version_text)
        expect(theme_toggle).to_be_focused()
        assert page.evaluate(
            """
            () => (
                window.__f2ThemeToggle === document.querySelector('#darkModeToggle')
                && window.__f2Version === document.querySelector('[data-product-version]')
                && window.Theme.get().mode === 'light'
                && localStorage.getItem('llmgateway:theme') === 'light'
            )
            """
        )
        assert request_count == 1
        assert health_request_count == 1

        page.evaluate("() => window.Theme.cycle()")
        expect(theme_toggle).to_have_attribute(
            "aria-label", "Использовать системную тему"
        )
        assert page.evaluate(
            "window.Theme.get().mode === 'dark' && "
            "localStorage.getItem('llmgateway:theme') === 'dark'"
        )
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
        expect(theme_toggle).to_have_attribute("aria-label", "Switch to system mode")
        expect(version).to_have_text(version_text)
        assert request_count == 1
        assert health_request_count == 1

        page.unroute(f"{r5_2_server}/auth/me", delayed_identity)
        page.route(
            f"{r5_2_server}/auth/me",
            lambda route: route.fulfill(status=500, json={"detail": "unavailable"}),
        )
        page.goto(f"{r5_2_server}/v1/ui/rules-editor")
        page.wait_for_function("window.gatewayAuth?.state === 'unknown'")
        expect(page.locator("[data-gateway-nav]")).to_be_hidden()
        assert page.locator(
            "[data-master-only]:not([hidden]), [data-user-only]:not([hidden])"
        ).count() == 0
    finally:
        context.close()


def test_navigation_overflow_cues_work_at_three_widths_and_in_rtl(
    browser: Browser,
    r5_2_server: str,
) -> None:
    context = browser.new_context()
    try:
        _login(context, r5_2_server, MASTER_KEY)
        page = context.new_page()
        for width in (390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            page.goto(f"{r5_2_server}/v1/ui/pricing")
            _wait_for_navigation(page)
            _assert_current_link_is_visible(page)

        page.set_viewport_size({"width": 390, "height": 844})
        page.evaluate(
            """
            () => {
                window.gatewayNav.destroy();
                document.documentElement.dir = 'rtl';
                const root = document.querySelector('[data-gateway-nav]');
                window.__rtlNavigation = gatewayUi.createNavigation(root, {
                    direction: 'rtl',
                    pathname: '/v1/ui/pricing',
                    role: 'master',
                    translate: (key) => gatewayI18n.t(key),
                });
            }
            """
        )
        page.wait_for_timeout(50)
        _assert_current_link_is_visible(page)
        assert page.locator(
            "[data-gateway-nav][data-overflow-start], "
            "[data-gateway-nav][data-overflow-end]"
        ).count() == 1
    finally:
        context.close()


def test_all_six_page_tablists_use_manual_keyboard_activation(
    browser: Browser,
    r5_2_server: str,
) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    try:
        _login(context, r5_2_server, MASTER_KEY)
        page = context.new_page()

        page.goto(f"{r5_2_server}/v1/ui/docs")
        _wait_for_navigation(page)
        _assert_manual_tab_keyboard(page, ".docs-tabs")

        page.goto(f"{r5_2_server}/v1/ui/usage-stats")
        _wait_for_navigation(page)
        _assert_manual_tab_keyboard(page, ".container > .tabs")
        page.locator('[data-tab="fallback"]').click()
        expect(page.locator(".fallback-sub-tabs")).to_be_visible()
        _assert_manual_tab_keyboard(page, ".fallback-sub-tabs")

        page.goto(f"{r5_2_server}/v1/ui/playground")
        _wait_for_navigation(page)
        _assert_manual_tab_keyboard(page, ".playground-section-tabs")
        page.locator('[data-playground-section-tab="web"]').click()
        expect(page.locator(".web-operation-tabs")).to_be_visible()
        _assert_manual_tab_keyboard(page, ".web-operation-tabs")

        page.goto(f"{r5_2_server}/v1/ui/rules-editor")
        _wait_for_navigation(page)
        _assert_manual_tab_keyboard(page, ".container > .tabs")
    finally:
        context.close()


def test_login_locale_is_available_before_auth_and_is_axe_clean(
    page: Page,
    r5_2_server: str,
) -> None:
    auth_me_requests = 0

    def count_auth_me(route) -> None:
        nonlocal auth_me_requests
        auth_me_requests += 1
        route.continue_()

    page.route(f"{r5_2_server}/auth/me", count_auth_me)
    page.goto(f"{r5_2_server}/auth/login?next=/v1/ui/docs")
    expect(page.locator("[data-locale-select] option")).to_have_count(2)
    page.locator("[data-locale-select]").select_option("ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator('label[for="apiKeyInput"]')).to_have_text("API-ключ шлюза")
    expect(page.locator("#submitButton")).to_have_text("Войти")
    assert auth_me_requests == 0
    _assert_axe_no_high_impact(page, "document")

    _login(page.context, r5_2_server, MASTER_KEY)
    failed_page = page.context.new_page()
    failed_page.add_init_script(
        "localStorage.setItem('llmgateway:locale', 'en');"
        "localStorage.setItem('llmgateway:theme', 'light');"
    )
    common_catalog = json.loads(
        (
            Path(__file__).resolve().parents[1]
            / "static"
            / "locales"
            / "en"
            / "common.json"
        ).read_text(encoding="utf-8")
    )
    del common_catalog["theme"]["switchToDark"]
    failed_auth_requests = 0

    def count_failed_auth(route) -> None:
        nonlocal failed_auth_requests
        failed_auth_requests += 1
        route.continue_()

    failed_page.route(
        f"{r5_2_server}/static/locales/en/common.json",
        lambda route: route.fulfill(status=200, json=common_catalog),
    )
    failed_page.route(f"{r5_2_server}/auth/me", count_failed_auth)
    failed_page.goto(f"{r5_2_server}/v1/ui/docs")
    rejection = failed_page.evaluate(
        """
        () => window.gatewayNav.ready.then(
            () => null,
            error => ({name: error.name, code: error.code, message: error.message}),
        )
        """
    )
    assert rejection["code"] == "TRANSLATION_MISSING"
    assert "common:theme.switchToDark" in rejection["message"]
    assert failed_auth_requests == 1
    failed_page.close()


def test_login_submit_errors_and_locale_changes_preserve_page_state(
    browser: Browser,
    r5_2_server: str,
) -> None:
    context = browser.new_context(viewport={"width": 390, "height": 844})
    page = context.new_page()
    login_routes = []
    auth_requests: list[tuple[str, str]] = []
    page_errors: list[str] = []

    def hold_login_posts(route) -> None:
        if route.request.method == "POST":
            login_routes.append(route)
            return
        route.continue_()

    def record_auth_request(request) -> None:
        path = request.url.split("?", 1)[0]
        if path.endswith("/auth/login") or path.endswith("/auth/me"):
            auth_requests.append((request.method, path.rsplit("/", 2)[-1]))

    def wait_for_login_posts(count: int) -> None:
        for _ in range(500):
            if len(login_routes) >= count:
                return
            page.wait_for_timeout(10)
        assert len(login_routes) == count

    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.on("request", record_auth_request)
    page.route("**/auth/login**", hold_login_posts)
    page.add_init_script(
        """
        (() => {
            localStorage.setItem('llmgateway:locale', 'en');
            localStorage.setItem('llmgateway:theme', 'dark');
            const nativeFetch = window.fetch.bind(window);
            let releaseCatalog;
            const catalogGate = new Promise((resolve) => {
                releaseCatalog = resolve;
            });
            window.__releaseLoginCatalog = releaseCatalog;
            window.__loginFetchCalls = 0;
            window.fetch = async (input, init) => {
                const rawUrl = typeof input === 'string' ? input : input.url;
                const url = new URL(rawUrl, window.location.href);
                if (url.pathname === '/auth/login') {
                    window.__loginFetchCalls += 1;
                }
                const response = await nativeFetch(input, init);
                if (url.pathname === '/static/locales/en/auth.json') {
                    await catalogGate;
                }
                return response;
            };
        })();
        """
    )

    try:
        page.goto(
            f"{r5_2_server}/auth/login?next=/v1/ui/docs%3Ftab%3Dmodels",
            wait_until="domcontentloaded",
        )
        api_key = page.locator("#apiKeyInput")
        api_key.fill("typed-before-bootstrap")
        page.locator("#loginForm").evaluate("form => form.requestSubmit()")
        assert page.evaluate("window.__loginFetchCalls") == 0

        page.evaluate("window.__releaseLoginCatalog()")
        expect(page.locator("[data-locale-select] option")).to_have_count(2)
        assert page.evaluate(
            "document.documentElement.scrollWidth <= innerWidth"
        )
        expect(page.locator("body")).to_have_class("dark-mode")
        assert page.evaluate(
            "localStorage.getItem('llmgateway:theme')"
        ) == "dark"

        api_key.evaluate("element => { element.dataset.identityMarker = 'preserved'; }")
        api_key.focus()
        api_key.evaluate("element => element.setSelectionRange(5, 11, 'forward')")
        next_value = page.locator("#nextInput").input_value()
        api_key.press("Enter")
        page.locator("#loginForm").evaluate("form => form.requestSubmit()")
        wait_for_login_posts(1)
        expect(page.locator("#submitButton")).to_be_disabled()
        expect(page.locator("#submitButton")).to_have_text("Signing in…")

        locale_request_snapshot = list(auth_requests)
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(page.locator("#submitButton")).to_have_text("Выполняется вход…")
        assert auth_requests == locale_request_snapshot
        assert not any(path == "me" for _, path in auth_requests)
        assert api_key.input_value() == "typed-before-bootstrap"
        expect(api_key).to_have_attribute("data-identity-marker", "preserved")
        assert page.locator("#nextInput").input_value() == next_value
        assert page.evaluate("document.activeElement.id") == "apiKeyInput"
        assert page.evaluate("document.activeElement.selectionStart") == 5
        assert page.evaluate("document.activeElement.selectionEnd") == 11
        expect(page.locator("body")).to_have_class("dark-mode")
        assert page.evaluate(
            "localStorage.getItem('llmgateway:theme')"
        ) == "dark"

        login_routes[0].fulfill(
            status=401,
            json={
                "detail": "<b>RAW_INVALID_KEY</b>",
                "error": {
                    "code": "auth_invalid_api_key",
                    "message": "must not replace top-level detail",
                },
            },
        )
        expect(page.locator("#errorSummary")).to_have_text("Неверный API-ключ.")
        expect(page.locator("#errorBox")).to_have_text("<b>RAW_INVALID_KEY</b>")
        expect(page.locator("#errorBox")).to_have_attribute("lang", "und")
        expect(page.locator("#errorBox")).to_have_attribute("dir", "auto")
        expect(page.locator("#errorBox b")).to_have_count(0)
        expect(page.locator("#submitButton")).to_be_enabled()

        error_request_snapshot = list(auth_requests)
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
        expect(page.locator("#errorSummary")).to_have_text("Invalid API key.")
        expect(page.locator("#errorBox")).to_have_text("<b>RAW_INVALID_KEY</b>")
        assert auth_requests == error_request_snapshot

        cases = (
            ("auth_invalid_request", 400, "The request is invalid."),
            ("auth_rate_limited", 429, "Too many requests. Try again later."),
            ("auth_unavailable", 503, "The service is temporarily unavailable."),
            ("auth:submit", 418, "Request failed (HTTP 418)."),
        )
        for index, (code, status, summary) in enumerate(cases, start=2):
            api_key.press("Enter")
            wait_for_login_posts(index)
            login_routes[index - 1].fulfill(
                status=status,
                headers={"Retry-After": "17"} if status == 429 else None,
                json={
                    "detail": f"<img src=x onerror=RAW_{index}>",
                    "error": {"code": code, "message": "ignored nested detail"},
                },
            )
            expect(page.locator("#errorSummary")).to_have_text(summary)
            expect(page.locator("#errorBox")).to_have_text(
                f"<img src=x onerror=RAW_{index}>"
            )
            expect(page.locator("#errorBox img")).to_have_count(0)
            expect(page.locator("#submitButton")).to_be_enabled()

        api_key.press("Enter")
        wait_for_login_posts(6)
        login_routes[5].abort("failed")
        expect(page.locator("#errorSummary")).to_have_text(
            "The service could not be reached."
        )
        expect(page.locator("#errorBox")).not_to_be_empty()
        expect(page.locator("#errorBox")).to_have_attribute("lang", "und")
        expect(page.locator("#errorBox")).to_have_attribute("dir", "auto")
        assert not any(path == "me" for _, path in auth_requests)
        assert page_errors == []
    finally:
        context.close()
