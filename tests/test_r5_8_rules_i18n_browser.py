from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (  # noqa: F401
    create_authenticated_session,
    provider_mock,
    server,
)


pytestmark = pytest.mark.browser


def test_rules_locale_change_preserves_editor_and_eval_state_without_requests(
    page: Page,
    request: pytest.FixtureRequest,
) -> None:
    server_url = request.getfixturevalue("server")
    session = create_authenticated_session(server_url, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server_url}]
    )
    page.add_init_script(
        "localStorage.setItem('llmgateway:locale', 'en');"
        "localStorage.setItem('llmgateway:theme', 'dark');"
    )

    relevant_requests: list[tuple[str, str]] = []
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))

    def record_request(browser_request) -> None:
        path = browser_request.url.split("?", 1)[0]
        if any(
            marker in path
            for marker in (
                "/v1/config/",
                "/v1/models",
                "/v1/openrouter/free-models",
                "/v1/fallback-model-evals",
            )
        ):
            relevant_requests.append((browser_request.method, path))

    page.on("request", record_request)
    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{server_url}/v1/ui/rules-editor")
    page.wait_for_timeout(250)
    assert page_errors == []
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    page.locator("#previewRulesButton").click()
    expect(page.locator("#rulesPreviewArea strong")).to_have_text(
        "Fallback Rules Preview"
    )
    page.locator('[data-entity-target="images"]').click()
    expect(page.locator("#messageArea")).to_contain_text(
        "Images Routes loaded successfully"
    )
    page.locator("#addImageGenerationButton").click()

    card = page.locator("#imageGenerationList .rule-card").last
    gateway_input = card.locator(".gateway-model-input")
    rate_input = card.locator(".cost-calculator-rate-input")
    gateway_input.fill("llmgateway/localized-image")
    rate_input.fill("0.42")
    card.locator(".provider-select").select_option("openai")
    card.locator(".model-input").fill("fixture-image-model")
    page.evaluate(
        """() => {
            document.getElementById('addFusionButton').click();
            document.getElementById('addAudioSpeechButton').click();
            document.getElementById('addWebSearchButton').click();
        }"""
    )
    gateway_input.evaluate(
        "element => { element.dataset.identityMarker = 'gateway-input'; "
        "element.setSelectionRange(4, 11, 'forward'); element.focus(); }"
    )
    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
    scroll_y = page.evaluate("window.scrollY")
    request_snapshot = list(relevant_requests)

    page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator('[data-entity-target="images"]')).to_have_text("Изображения")
    expect(page.locator("#saveButton")).to_contain_text("Сохранить")
    expect(page.locator('[data-entity-target="images"]')).to_have_attribute("aria-current", "true")
    expect(page.locator("#saveButton")).to_have_attribute(
        "data-editor-dirty", "true"
    )
    expect(rate_input).to_have_value("0.42")
    expect(rate_input).to_have_attribute("placeholder", "0.1")
    expect(gateway_input).to_have_attribute(
        "data-identity-marker", "gateway-input"
    )
    assert page.evaluate("document.activeElement.dataset.identityMarker") == "gateway-input"
    assert page.evaluate("document.activeElement.selectionStart") == 4
    assert page.evaluate("document.activeElement.selectionEnd") == 11
    assert page.evaluate("window.scrollY") == scroll_y
    expect(page.locator("body")).to_have_class("dark-mode")
    expect(page.locator("#rulesPreviewArea strong")).to_have_text(
        "Предпросмотр правил фолбэка"
    )
    expect(page.locator("#fusionList .fusion-section-heading").first).to_have_text(
        "Основная модель (формирует итоговый ответ)"
    )
    expect(
        page.locator("#audioSpeechList .add-fallback-button")
    ).to_have_text("Добавить маршрут")
    expect(
        page.locator("#audioSpeechList .fallback-row .danger-button")
    ).to_have_text("Удалить маршрут")
    expect(
        page.locator("#webSearchList .rule-card-header .danger-button")
    ).to_have_text("Удалить маршрут")
    expect(page.locator("#webSearchList .field-hint").last).to_contain_text(
        "разворачивает запрос пользователя"
    )
    assert relevant_requests == request_snapshot
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )

    rate_input.fill("-1")
    page.locator("#saveButton").click()
    expect(page.locator("#messageArea")).to_have_text(
        "Стоимость успешного запроса должна быть конечным неотрицательным числом."
    )
    expect(page.locator("#messageRawDetail")).to_be_hidden()
    rate_input.fill("0.42")

    page.evaluate(
        """() => {
            document.getElementById('addRuleButton').click();
            document.getElementById('previewRulesButton').click();
        }"""
    )
    expect(page.locator("#messageArea")).to_have_text(
        "В каждом правиле должно быть указано имя модели шлюза."
    )
    expect(page.locator("#messageRawDetail")).to_be_hidden()
    assert relevant_requests == request_snapshot

    eval_page = page.context.new_page()
    eval_page.add_init_script(
        "localStorage.setItem('llmgateway:locale', 'en');"
    )
    eval_requests: list[str] = []
    eval_errors: list[str] = []
    eval_page.on("pageerror", lambda error: eval_errors.append(str(error)))

    def serve_eval(route) -> None:
        eval_requests.append(route.request.url)
        route.fulfill(
            json={
                "running": False,
                "lastCheckedAt": "2026-07-15T10:20:30Z",
                "snapshot": {
                    "configuredCount": 1234,
                    "evaluatedCount": 1,
                    "updatedAt": "2026-07-15T10:20:30Z",
                    "models": [
                        {
                            "rank": 1,
                            "name": "fixture-model",
                            "provider": "fixture-provider",
                            "model": "fixture/model",
                            "score": 1234.5,
                            "gatewayModels": ["llmgateway/fixture"],
                            "healthStatus": "timeout",
                        }
                    ],
                },
            }
        )

    eval_page.route("**/v1/fallback-model-evals", serve_eval)
    eval_page.goto(f"{server_url}/v1/ui/rules-editor")
    eval_page.locator('[data-entity-target="fallback-eval"]').click()
    expect(eval_page.locator("#fallbackEvalModels .openrouter-free-card")).to_have_count(1)
    eval_card = eval_page.locator("#fallbackEvalModels .openrouter-free-card")
    eval_card.evaluate("element => { element.dataset.identityMarker = 'eval-card'; }")
    eval_snapshot = list(eval_requests)
    eval_page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
    expect(eval_page.locator("#fallbackEvalStatus")).to_contain_text("Статус")
    expect(eval_page.locator("#fallbackEvalModels")).to_contain_text("тайм-аут")
    expect(eval_card).to_have_attribute("data-identity-marker", "eval-card")
    assert eval_requests == eval_snapshot
    assert eval_errors == []
    eval_page.close()

    openrouter_page = page.context.new_page()
    openrouter_page.add_init_script(
        "localStorage.setItem('llmgateway:locale', 'en');"
    )
    openrouter_requests: list[str] = []
    openrouter_errors: list[str] = []
    openrouter_page.on(
        "pageerror", lambda error: openrouter_errors.append(str(error))
    )

    def serve_openrouter(route) -> None:
        openrouter_requests.append(route.request.url)
        route.fulfill(
            json={
                "configured": True,
                "manualRefreshRunning": False,
                "nextRefreshAt": "2026-07-15T11:20:30Z",
                "snapshot": {
                    "refreshMode": "fullEval",
                    "catalogCount": 1234,
                    "eligibleCount": 1,
                    "evaluatedCount": 1,
                    "updatedAt": "2026-07-15T10:20:30Z",
                    "models": [
                        {
                            "rank": 1234,
                            "name": "fixture-openrouter-model",
                            "id": "fixture/model:free",
                            "score": 1234.5,
                            "healthStatus": "not_probed",
                        }
                    ],
                },
            }
        )

    openrouter_page.route("**/v1/openrouter/free-models", serve_openrouter)
    openrouter_page.goto(f"{server_url}/v1/ui/rules-editor")
    expect(openrouter_page.locator('[data-entity-target="openrouter-free"]')).to_be_visible()
    openrouter_page.locator('[data-entity-target="openrouter-free"]').click()
    openrouter_card = openrouter_page.locator(
        "#openRouterFreeModels .openrouter-free-card"
    )
    expect(openrouter_card).to_have_count(1)
    expect(openrouter_page.locator("#openRouterFreeStatus")).to_contain_text(
        "Full evaluation"
    )
    expect(openrouter_card).to_contain_text("not probed")
    expect(openrouter_card.locator(".openrouter-free-rank")).to_have_text("#1,234")
    expect(openrouter_card.locator(".openrouter-free-score")).to_have_text("1,234.5")
    openrouter_card.evaluate(
        "element => { element.dataset.identityMarker = 'openrouter-card'; }"
    )
    openrouter_snapshot = list(openrouter_requests)

    openrouter_page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
    expect(openrouter_page.locator("#openRouterFreeStatus")).to_contain_text(
        "Полная оценка"
    )
    expect(openrouter_card).to_contain_text("не проверено")
    expect(openrouter_card.locator(".openrouter-free-rank")).to_have_text(
        "#1 234"
    )
    expect(openrouter_card.locator(".openrouter-free-score")).to_have_text(
        "1 234,5"
    )
    expect(openrouter_card).to_have_attribute(
        "data-identity-marker", "openrouter-card"
    )
    assert openrouter_requests == openrouter_snapshot
    assert openrouter_errors == []
    openrouter_page.close()
