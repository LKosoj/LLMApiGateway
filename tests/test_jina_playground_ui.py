from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import Page, expect

from tests.test_web_ui import add_session, server


__all__ = ["server"]
pytestmark = pytest.mark.browser


def test_jina_playground_forms_send_get_and_preserve_results_on_locale_change(page: Page, server: str):
    requests = []
    page.add_init_script("localStorage.setItem('llmgateway:locale', 'en')")
    add_session(page, server)
    page.route(
        f"{server}/v1/ui/playground/models",
        lambda route: route.fulfill(json={"chat": [], "web_search": ["gateway/search"], "web_read": ["gateway/read"]}),
    )
    article = {"title": "Jina page", "url": "https://example.com", "description": "Description", "content": "# Full page text"}

    def jina_response(route):
        request = route.request
        assert request.method == "GET"
        assert request.post_data is None
        assert request.headers["accept"] == "application/json"
        assert "content-type" not in request.headers
        parts = urlsplit(request.url)
        requests.append((parts.path, parse_qs(parts.query)))
        route.fulfill(json={"code": 200, "status": 20000, "data": [article] if parts.path.endswith("search") else article})

    page.route(f"{server}/v1/jina/search?*", jina_response)
    page.route(f"{server}/v1/jina/extract?*", jina_response)
    page.goto(f"{server}/v1/ui/playground")
    expect(page.locator("html")).to_have_attribute("data-i18n-state", "ready")
    page.locator("[data-playground-section-tab='web']").click()
    page.locator("[data-web-tab='jina-search']").click()
    page.select_option("#jinaSearchModel", "gateway/search")
    page.select_option("#jinaSearchReadModel", "gateway/read")
    page.fill("#jinaSearchQuery", "Кофе & tea/чай")
    page.fill("#jinaSearchNum", "2")
    page.fill("#jinaSearchLanguage", "ru")
    page.fill("#jinaSearchSite", "example.com, docs.example.com")
    page.select_option("#jinaSearchFormat", "text")
    page.locator("#jinaSearchForm .run-button").click()
    expect(page.locator("[data-result-for='jina-search']")).to_contain_text("Full page text")
    assert requests[-1] == ("/v1/jina/search", {
        "model": ["gateway/search"], "read_model": ["gateway/read"], "q": ["Кофе & tea/чай"],
        "num": ["2"], "hl": ["ru"], "site": ["example.com, docs.example.com"], "respondWith": ["text"],
    })

    page.locator("[data-web-tab='jina-extract']").click()
    page.select_option("#jinaExtractModel", "gateway/read")
    url = "https://example.com/article?token=a%26b&x=%2F"
    page.fill("#jinaExtractUrl", url)
    page.locator("#jinaExtractForm .run-button").click()
    result = page.locator("[data-result-for='jina-extract']")
    expect(result).to_contain_text("Full page text")
    expect(result.locator("details.raw-details")).to_have_count(1)
    assert requests[-1] == ("/v1/jina/extract", {
        "model": ["gateway/read"], "url": [url], "respondWith": ["markdown"],
    })
    request_count = len(requests)
    for locale in ("ru", "en"):
        page.select_option("#localeSelect", locale)
        page.wait_for_function("locale => window.gatewayI18n.currentLocale === locale", arg=locale)
        expect(page.locator("[data-web-panel='jina-extract']")).to_be_visible()
        expect(page.locator("#jinaExtractUrl")).to_have_value(url)
        expect(result).to_contain_text("Full page text")
    assert len(requests) == request_count
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#jinaExtractForm")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth <= document.documentElement.clientWidth")
