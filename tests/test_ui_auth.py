import os


def test_shared_auth_js_redirects_to_login_instead_of_prompting_for_token():
    shared_auth_path = "static/shared-auth.js"

    assert os.path.exists(shared_auth_path), f"{shared_auth_path} does not exist"
    with open(shared_auth_path, "r", encoding="utf-8") as file:
        content = file.read()

    assert "window.location.assign(buildLoginUrl())" in content
    assert "Authentication required" in content
    assert "sessionStorage" not in content
    assert "prompt(" not in content
    assert "Authorization" not in content


def test_shared_auth_fails_closed_for_unknown_identity_and_preserves_403():
    shared_auth_path = "static/shared-auth.js"

    with open(shared_auth_path, "r", encoding="utf-8") as file:
        content = file.read()

    assert "if (response.status === 401)" in content
    assert "response.status === 403" not in content
    assert 'cachedIdentity = {role: "unknown", key_id: null, name: null};' in content
    assert 'const isMaster = Boolean(identity && identity.role === "master");' in content


def test_ui_pages_reference_shared_auth_js():
    html_files = [
        "static/api-keys.html",
        "static/rules-editor.html",
        "static/usage-stats.html",
        "static/gateway-docs.html",
        "static/web-playground.html",
        "static/pricing.html",
        "static/quota.html",
        "static/rejections.html",
        "static/translator-debug.html",
    ]

    for file_path in html_files:
        assert os.path.exists(file_path), f"{file_path} does not exist"
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()

        assert "/static/shared-auth.js" in content, f"{file_path} missing shared auth script include"


def test_ui_pages_reference_shared_core_and_nav_js():
    html_files = [
        "static/api-keys.html",
        "static/rules-editor.html",
        "static/usage-stats.html",
        "static/gateway-docs.html",
        "static/web-playground.html",
        "static/pricing.html",
        "static/quota.html",
        "static/rejections.html",
        "static/translator-debug.html",
    ]

    for file_path in html_files:
        assert os.path.exists(file_path), f"{file_path} does not exist"
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()

        assert "/static/ui-core.js" in content, f"{file_path} missing shared UI core include"
        assert "/static/shared-nav.js" in content, f"{file_path} missing shared nav include"


def test_ui_js_files_use_shared_auth_helper():
    files_to_check = [
        "static/editor.js",
        "static/usage-stats.js",
        "static/gateway-docs.js",
        "static/web-playground.js",
        "static/pricing.js",
    ]

    for file_path in files_to_check:
        assert os.path.exists(file_path), f"{file_path} does not exist"
        with open(file_path, "r", encoding="utf-8") as file:
            content = file.read()

        assert "window.gatewayAuth" in content, f"{file_path} missing shared auth usage"
        assert "const { apiFetch } = window.gatewayAuth;" in content, f"{file_path} missing apiFetch shared helper"


def test_web_playground_uses_self_hosted_markdown_vendor_scripts():
    with open("static/web-playground.html", "r", encoding="utf-8") as file:
        content = file.read()

    assert "cdnjs" not in content
    assert "/static/vendor/marked.min.js" in content
    assert "/static/vendor/purify.min.js" in content


def test_shared_nav_sets_current_page_aria_attribute():
    with open("static/shared-nav.js", "r", encoding="utf-8") as file:
        content = file.read()

    assert "aria-current" in content
    assert "applyCurrentNav" in content


def test_pricing_uses_shared_toast_helper():
    with open("static/pricing.js", "r", encoding="utf-8") as file:
        content = file.read()

    assert "window.gatewayUi.showToast" in content
    assert "let toastTimer" not in content
