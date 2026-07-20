import json
import re
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
STATIC_DIR = PROJECT_ROOT / "static"
AUTHENTICATED_PAGES = {
    "api-keys.html": "api-keys",
    "rules-editor.html": "rules-editor",
    "usage-stats.html": "usage",
    "gateway-docs.html": "docs",
    "web-playground.html": "playground",
    "pricing.html": "pricing",
    "quota.html": "quota",
    "rejections.html": "rejections",
    "translator-debug.html": "translator",
    "overview.html": "overview",
    "activity.html": "activity",
}


def read_static(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_shared_auth_redirects_only_401_and_uses_one_runtime_identity_controller():
    content = read_static("shared-auth.js")

    assert "ui.createAuthController" in content
    assert "controller.fetchIdentity()" in content
    assert "if (response.status === 401)" in content
    assert "response.status === 403" not in content
    assert "global.location.assign(buildLoginUrl())" in content
    assert "ui.applyRoleVisibility(document, state)" in content
    assert "ui.AUTH_STATES.pending" in content
    assert ".style.display" not in content
    assert "sessionStorage" not in content
    assert "prompt(" not in content
    assert "Authorization" not in content


def test_authenticated_pages_use_the_runtime_auth_nav_shell_in_fixed_order():
    for filename, page in AUTHENTICATED_PAGES.items():
        content = read_static(filename)
        runtime = content.index('/static/vendor/ui-runtime.bundle.js')
        auth = content.index('/static/shared-auth.js')
        nav = content.index('/static/shared-nav.js')

        assert f'data-i18n-page="{page}"' in content
        assert '/static/shared-nav.css' in content
        assert 'data-gateway-nav hidden' in content
        assert 'data-locale-select' in content
        assert 'data-locale-error hidden' in content
        assert runtime < auth < nav
        assert '/static/ui-core.js' not in content
        assert 'href="/v1/ui/docs"' not in content


def test_role_gated_markup_is_hidden_until_identity_is_known():
    for filename in AUTHENTICATED_PAGES:
        content = read_static(filename)
        for line in content.splitlines():
            if "data-master-only" in line or "data-user-only" in line:
                assert "hidden" in line, f"{filename} exposes role-gated markup: {line}"


def test_shared_nav_uses_strict_runtime_navigation_and_locale_contracts():
    content = read_static("shared-nav.js")

    concurrent_bootstrap = re.search(
        r"Promise\.all\(\[\s*(?P<entries>.*?)\s*\]\)",
        content,
        re.DOTALL,
    )
    assert concurrent_bootstrap is not None
    entries = concurrent_bootstrap.group("entries")
    assert content.count("auth.fetchIdentity()") == 1
    assert "auth.fetchIdentity()" in entries
    assert "i18n.ready.then(applyCurrentThemeLabels)" in entries
    assert "ui.createNavigation" in content
    assert "ui.createLocaleSelector" in content
    assert "i18n.t('common:localeChangeFailed')" in content
    assert "i18n.subscribe" in content
    assert "innerHTML" not in content


def test_shared_nav_renders_identity_role_name_and_logout_for_both_roles():
    content = read_static("shared-nav.js")

    assert "data-gateway-identity" in content
    assert "data-gateway-identity-role" in content
    assert "data-gateway-identity-name" in content
    assert "data-gateway-identity-logout" in content
    assert "auth.logout()" in content
    assert "i18n.t(`common:identity.role.${currentIdentity.role}`)" in content
    assert "i18n.t('common:identity.name', {name: currentIdentity.name})" in content
    assert "i18n.t('common:identity.logout')" in content
    assert "currentIdentity.role !== 'master' && currentIdentity.role !== 'user'" in content
    assert "innerHTML" not in content
    # The bootstrap Promise.all must remain the single identity fetch (no duplicate call).
    assert content.count("auth.fetchIdentity()") == 1


def test_shared_nav_builds_sidebar_and_drawer_shell_for_desktop_and_mobile():
    content = read_static("shared-nav.js")

    assert "data-gateway-nav-toggle" in content
    assert "data-gateway-nav-overlay" in content
    assert "data-gateway-sidebar" in content
    assert "data-gateway-nav-close" in content
    assert "data-gateway-sidebar-footer" in content
    assert "ui.createDialog(" in content
    assert "layout: 'list'" in content
    assert "innerHTML" not in content


def test_shared_nav_css_defines_permanent_desktop_sidebar_and_mobile_drawer():
    content = read_static("shared-nav.css")

    for selector in (
        ".gateway-nav-toggle",
        ".gateway-nav-overlay",
        ".gateway-sidebar",
        ".gateway-nav-close",
        ".gateway-nav-list",
        ".gateway-sidebar-footer",
    ):
        assert selector in content
    assert "@media (min-width: 1024px)" in content
    assert "margin-inline-start: 240px" in content


def test_navigation_toggle_close_overlay_locale_keys_exist_for_en_and_ru():
    for locale in ("en", "ru"):
        catalog = json.loads(
            (PROJECT_ROOT / "static" / "locales" / locale / "common.json").read_text(
                encoding="utf-8"
            )
        )
        navigation = catalog["navigation"]
        for key in ("toggle", "close", "overlay"):
            assert isinstance(navigation[key], str) and navigation[key]


def test_identity_locale_keys_exist_for_en_and_ru():
    for locale in ("en", "ru"):
        catalog = json.loads(
            (PROJECT_ROOT / "static" / "locales" / locale / "common.json").read_text(
                encoding="utf-8"
            )
        )
        identity = catalog["identity"]
        assert isinstance(identity["role"]["master"], str) and identity["role"]["master"]
        assert isinstance(identity["role"]["user"], str) and identity["role"]["user"]
        assert isinstance(identity["name"], str) and "{name}" in identity["name"]
        assert isinstance(identity["logout"], str) and identity["logout"]


def test_login_uses_synchronous_submit_guard_and_shared_error_contract():
    html = read_static("login.html")
    script = read_static("login.js")

    assert 'data-i18n-page="login"' in html
    assert html.index('data-locale-select') < html.index('id="loginForm"')
    assert 'data-i18n="auth:intro"' in html
    assert 'data-i18n="auth:accessHint"' in html
    assert '__NEXT__' in html
    assert '__ERROR_MESSAGE__' in html
    assert 'id="errorSummary"' in html
    assert 'id="errorBox"' in html
    assert html.index('id="errorSummary"') < html.index('id="errorBox"')
    assert 'id="errorBox" class="error" data-i18n-raw-detail' in html
    assert '<template id="serverErrorTemplate">__ERROR_MESSAGE__</template>' in html
    assert ".content.textContent" in script
    assert 'id="submitButton" type="submit" data-i18n="auth:submit" disabled' in html
    assert '<script>' not in html
    assert '/static/vendor/ui-runtime.bundle.js' in html
    assert '/static/login.js' in html
    assert script.index("form.addEventListener('submit'") < script.index(
        "await i18n.ready"
    )
    assert "if (!ready || running) return;" in script
    assert "ui.createStatus" in script
    assert "ui.describeApiError" in script
    assert "i18n.subscribe" in script
    assert "innerHTML" not in script
    assert "next: nextInput.value" in script
    assert "global.location.assign(payload.redirect_to || '/v1/ui/overview')" in script


def test_five_tab_groups_expose_semantic_tab_roles():
    rules = read_static("rules-editor.html")
    usage = read_static("usage-stats.html")
    playground = read_static("web-playground.html")
    docs = read_static("gateway-docs.html")

    # rules-editor.html has no top tablist (the sidebar is the only entity
    # navigation), but its panels still carry role="tabpanel".
    assert 'role="tabpanel"' in rules
    assert '<div class="tabs" role="tablist">' in usage
    assert '<div class="fallback-sub-tabs" role="tablist">' in usage
    assert '<div class="tabs playground-section-tabs" role="tablist">' in playground
    assert '<div class="tabs web-operation-tabs" role="tablist">' in playground
    assert '<div class="docs-tabs" role="tablist"' in docs
    for content in (usage, playground, docs):
        assert 'role="tab"' in content
        assert 'role="tabpanel"' in content
    for content, selector in (
        (usage, "data-tab="),
        (usage, "data-subtab="),
        (playground, "data-playground-section-tab="),
        (playground, "data-web-tab="),
        (docs, "data-docs-tab="),
    ):
        for line in content.splitlines():
            if selector in line:
                assert 'role="tab"' in line


def test_large_document_sinks_do_not_announce_whole_rerenders():
    docs = read_static("gateway-docs.html")

    for element_id in ("modelCatalog", "freeTierMarkdown"):
        start = docs.index(f'id="{element_id}"')
        tag_end = docs.index(">", start)
        assert "aria-live" not in docs[start:tag_end]


def test_docs_use_shared_status_semantics():
    docs_script = read_static("gateway-docs.js")

    assert docs_script.count("window.gatewayUi.createStatus") == 2
    assert "catalogStatus.busy(message)" in docs_script
    assert "catalogStatus.polite(message)" in docs_script
    assert "catalogStatus.error(message)" in docs_script
    assert "freeTierStatus.busy(message)" in docs_script
    assert "freeTierStatus.error(message)" in docs_script
    assert "freeTierStatus.clear()" in docs_script


def test_playground_has_no_inline_script():
    playground_html = read_static("web-playground.html")
    playground_script = read_static("web-playground.js")

    assert re.search(r"<script\b(?![^>]*\bsrc=)[^>]*>", playground_html, re.IGNORECASE) is None
    assert 'Theme.attachToggle("darkModeToggle");' in playground_script


def test_web_playground_uses_self_hosted_markdown_vendor_scripts():
    content = read_static("web-playground.html")

    assert "cdnjs" not in content
    assert "/static/vendor/marked.min.js" in content
    assert "/static/vendor/purify.min.js" in content


def test_ui_js_files_use_shared_auth_helper():
    for filename in (
        "editor.js",
        "usage-stats.js",
        "gateway-docs.js",
        "web-playground.js",
        "pricing.js",
    ):
        content = read_static(filename)
        assert "window.gatewayAuth" in content
        assert "const { apiFetch } = window.gatewayAuth;" in content
