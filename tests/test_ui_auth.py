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
    assert "global.location.assign(payload.redirect_to || '/v1/ui/usage-stats')" in script


def test_six_tab_groups_expose_semantic_tab_roles():
    rules = read_static("rules-editor.html")
    usage = read_static("usage-stats.html")
    playground = read_static("web-playground.html")
    docs = read_static("gateway-docs.html")

    assert '<div class="tabs" role="tablist">' in rules
    assert '<div class="tabs" role="tablist">' in usage
    assert '<div class="fallback-sub-tabs" role="tablist">' in usage
    assert '<div class="tabs playground-section-tabs" role="tablist">' in playground
    assert '<div class="tabs web-operation-tabs" role="tablist">' in playground
    assert '<div class="docs-tabs" role="tablist"' in docs
    for content in (rules, usage, playground, docs):
        assert 'role="tab"' in content
        assert 'role="tabpanel"' in content
    for content, selector in (
        (rules, "data-tab="),
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
