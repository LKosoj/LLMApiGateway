"""Final browser acceptance matrix for the completed R5.8 UI migration."""

from __future__ import annotations

import json
from collections.abc import Iterable

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.test_r5_2_ui_acceptance_browser import (
    MASTER_KEY,
    MASTER_PAGES,
    USER_PAGES,
    _assert_axe_no_high_impact,
    _assert_current_link_is_visible,
    _assert_manual_tab_keyboard,
    _create_virtual_key,
    _login,
    _wait_for_navigation,
    r5_2_server,
)


__all__ = ["r5_2_server"]
pytestmark = pytest.mark.browser

Profile = tuple[str, str, int]


def _profile_context(
    browser: Browser,
    *,
    locale: str,
    theme: str,
    width: int,
) -> BrowserContext:
    context = browser.new_context(viewport={"width": width, "height": 900})
    context.add_init_script(
        "localStorage.setItem('llmgateway:locale', "
        f"{json.dumps(locale)});"
        "localStorage.setItem('llmgateway:theme', "
        f"{json.dumps(theme)});"
    )
    return context


def _observe_errors(page: Page) -> tuple[list[str], list[str]]:
    console_errors: list[str] = []
    page_errors: list[str] = []
    page.on(
        "console",
        lambda message: console_errors.append(
            f"{message.text} @ {message.location.get('url', '')}"
        )
        if message.type == "error"
        else None,
    )
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    return console_errors, page_errors


def _assert_profile_page(
    page: Page,
    *,
    base_url: str,
    path: str,
    locale: str,
    theme: str,
    role: str,
    expected_links: int | None,
    console_errors: list[str],
    page_errors: list[str],
) -> None:
    console_start = len(console_errors)
    page_error_start = len(page_errors)
    page.goto(f"{base_url}{path}")
    page.evaluate("() => window.gatewayI18n.ready")
    expect(page.locator("html")).to_have_attribute("data-i18n-state", "ready")
    expect(page.locator("html")).to_have_attribute("lang", locale)
    assert page.evaluate(
        "expected => localStorage.getItem('llmgateway:locale') === expected",
        locale,
    )
    assert page.evaluate(
        "expected => localStorage.getItem('llmgateway:theme') === expected",
        theme,
    )
    assert page.locator("body").evaluate(
        "(body, dark) => body.classList.contains('dark-mode') === dark",
        theme == "dark",
    )

    if expected_links is not None:
        _wait_for_navigation(page)
        viewport = page.viewport_size
        if viewport is not None and viewport["width"] < 1024:
            page.locator("[data-gateway-nav-toggle]").click()
        expect(page.locator("[data-gateway-nav] a")).to_have_count(expected_links)
        _assert_current_link_is_visible(page)

    assert page.evaluate(
        "document.documentElement.scrollWidth "
        "<= document.documentElement.clientWidth + 1"
    ), f"horizontal overflow: {role} {locale}/{theme} {path}"
    _assert_axe_no_high_impact(page, "document")
    assert console_errors[console_start:] == [], (
        f"console errors: {role} {locale}/{theme} {path}"
    )
    assert page_errors[page_error_start:] == [], (
        f"page errors: {role} {locale}/{theme} {path}"
    )


def _run_pairwise_profiles(
    browser: Browser,
    base_url: str,
    profiles: Iterable[Profile],
) -> None:
    setup = browser.new_context()
    try:
        virtual_key = _create_virtual_key(setup, base_url)
    finally:
        setup.close()

    for locale, theme, width in profiles:
        login_context = _profile_context(
            browser,
            locale=locale,
            theme=theme,
            width=width,
        )
        master_context = _profile_context(
            browser,
            locale=locale,
            theme=theme,
            width=width,
        )
        virtual_context = _profile_context(
            browser,
            locale=locale,
            theme=theme,
            width=width,
        )
        try:
            _login(master_context, base_url, MASTER_KEY)
            _login(virtual_context, base_url, virtual_key)

            login_page = login_context.new_page()
            login_console, login_errors = _observe_errors(login_page)
            _assert_profile_page(
                login_page,
                base_url=base_url,
                path="/auth/login?next=/v1/ui/docs",
                locale=locale,
                theme=theme,
                role="login",
                expected_links=None,
                console_errors=login_console,
                page_errors=login_errors,
            )

            master_page = master_context.new_page()
            master_console, master_errors = _observe_errors(master_page)
            for path in MASTER_PAGES:
                _assert_profile_page(
                    master_page,
                    base_url=base_url,
                    path=path,
                    locale=locale,
                    theme=theme,
                    role="master",
                    expected_links=len(MASTER_PAGES),
                    console_errors=master_console,
                    page_errors=master_errors,
                )

            virtual_page = virtual_context.new_page()
            virtual_console, virtual_errors = _observe_errors(virtual_page)
            for path in USER_PAGES:
                _assert_profile_page(
                    virtual_page,
                    base_url=base_url,
                    path=path,
                    locale=locale,
                    theme=theme,
                    role="virtual",
                    expected_links=len(USER_PAGES),
                    console_errors=virtual_console,
                    page_errors=virtual_errors,
                )
        finally:
            login_context.close()
            master_context.close()
            virtual_context.close()


def test_r5_8_desktop_pairwise_strings_accessibility_and_console_are_clean(
    browser: Browser,
    r5_2_server: str,
) -> None:
    _run_pairwise_profiles(
        browser,
        r5_2_server,
        (("en", "light", 1440), ("ru", "dark", 1440)),
    )


def test_r5_8_mobile_pairwise_strings_accessibility_and_console_are_clean(
    browser: Browser,
    r5_2_server: str,
) -> None:
    _run_pairwise_profiles(
        browser,
        r5_2_server,
        (("en", "dark", 390), ("ru", "light", 390)),
    )


def test_r5_8_768_keyboard_rtl_delayed_and_raw_detail_contract(
    browser: Browser,
    r5_2_server: str,
) -> None:
    master = _profile_context(
        browser,
        locale="en",
        theme="light",
        width=768,
    )
    login = _profile_context(
        browser,
        locale="en",
        theme="light",
        width=768,
    )
    try:
        _login(master, r5_2_server, MASTER_KEY)
        page = master.new_page()
        console_errors, page_errors = _observe_errors(page)

        page.goto(f"{r5_2_server}/v1/ui/docs")
        page.evaluate("() => window.gatewayI18n.ready")
        _wait_for_navigation(page)
        _assert_manual_tab_keyboard(page, ".docs-tabs")
        selected = page.locator('.docs-tabs > [role="tab"][aria-selected="true"]')
        expect(selected).to_have_count(1)
        selected.focus()
        page.evaluate("document.documentElement.dir = 'rtl'")
        page.keyboard.press("ArrowRight")
        expect(page.locator('.docs-tabs > [role="tab"]').first).to_be_focused()

        page.goto(f"{r5_2_server}/v1/ui/api-keys")
        page.evaluate("() => window.gatewayI18n.ready")
        _wait_for_navigation(page)
        create_button = page.locator("#createKeyBtn")
        create_button.click()
        expect(page.locator("#keyModal")).to_be_visible()
        expect(page.locator("#fieldName")).to_be_focused()
        page.keyboard.press("Shift+Tab")
        expect(page.locator("#saveKeyBtn")).to_be_focused()
        page.keyboard.press("Escape")
        expect(page.locator("#keyModal")).to_be_hidden()
        expect(create_button).to_be_focused()

        page.goto(f"{r5_2_server}/v1/ui/pricing")
        page.evaluate("() => window.gatewayI18n.ready")
        _wait_for_navigation(page)
        page.evaluate(
            """
            () => {
                window.gatewayNav.destroy();
                document.documentElement.dir = 'rtl';
                // The nav mount now lives inside a drawer overlay that
                // `destroy()` leaves closed; the destroyed dialog controller
                // can no longer open it, so reveal it directly for this
                // manually re-created (mobile-width) navigation instance.
                const overlay = document.querySelector('[data-gateway-nav-overlay]');
                if (overlay) overlay.hidden = false;
                const root = document.querySelector('[data-gateway-nav]');
                window.__r58RtlNavigation = gatewayUi.createNavigation(root, {
                    direction: 'rtl',
                    pathname: '/v1/ui/pricing',
                    role: 'master',
                    translate: (key) => gatewayI18n.t(key),
                });
            }
            """
        )
        page.wait_for_function(
            "window.__r58RtlNavigation "
            "&& document.querySelector('[data-gateway-nav] [aria-current=\"page\"]')"
        )
        _assert_current_link_is_visible(page)
        assert page.evaluate(
            "document.documentElement.scrollWidth "
            "<= document.documentElement.clientWidth + 1"
        )
        assert console_errors == []
        assert page_errors == []

        login_page = login.new_page()
        login_console, login_errors = _observe_errors(login_page)
        held_routes = []

        def hold_login(route) -> None:
            held_routes.append(route)

        login_page.route("**/auth/login", hold_login)
        login_page.add_init_script(
            """
            (() => {
                const nativeFetch = window.fetch.bind(window);
                window.__r58LoginFetches = 0;
                window.fetch = (input, init) => {
                    const raw = typeof input === 'string' ? input : input.url;
                    const url = new URL(raw, location.href);
                    if (url.pathname === '/auth/login') window.__r58LoginFetches += 1;
                    return nativeFetch(input, init);
                };
            })();
            """
        )
        login_page.goto(f"{r5_2_server}/auth/login?next=/v1/ui/docs")
        login_page.evaluate("() => window.gatewayI18n.ready")
        login_page.locator("#apiKeyInput").fill("held-request-key")
        login_page.locator("#submitButton").click()
        login_page.wait_for_function("window.__r58LoginFetches === 1")
        assert len(held_routes) == 1

        login_page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(login_page.locator("html")).to_have_attribute("lang", "ru")
        expect(login_page.locator("#submitButton")).to_have_text("Выполняется вход…")
        raw_detail = "<img src=x onerror=window.__r58Injected=true>"
        held_routes[0].fulfill(
            status=503,
            json={
                "detail": raw_detail,
                "error": {"code": "auth_unavailable", "message": "ignored"},
            },
        )
        expect(login_page.locator("#errorSummary")).to_have_text(
            "Сервис временно недоступен."
        )
        expect(login_page.locator("#errorBox")).to_have_text(raw_detail)
        expect(login_page.locator("#errorBox")).to_have_attribute("lang", "und")
        expect(login_page.locator("#errorBox")).to_have_attribute("dir", "auto")
        expect(login_page.locator("#errorBox img")).to_have_count(0)
        assert login_page.evaluate("window.__r58Injected !== true")
        assert login_page.evaluate("window.__r58LoginFetches") == 1
        assert len(login_console) == 1
        assert "status of 503" in login_console[0]
        assert login_console[0].endswith("/auth/login")
        assert login_errors == []
    finally:
        master.close()
        login.close()
