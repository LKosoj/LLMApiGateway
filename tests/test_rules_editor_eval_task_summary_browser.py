from __future__ import annotations

import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (  # noqa: F401
    create_authenticated_session,
    provider_mock,
    server,
)


pytestmark = pytest.mark.browser


def _eval_payload() -> dict:
    return {
        "running": False,
        "lastCheckedAt": "2026-08-01T10:20:30Z",
        "snapshot": {
            "configuredCount": 2,
            "evaluatedCount": 2,
            "updatedAt": "2026-08-01T10:20:30Z",
            "models": [
                {
                    "rank": 1,
                    "name": "strong-model",
                    "provider": "fixture-provider",
                    "model": "fixture/strong",
                    "score": 900,
                    "healthStatus": "passed",
                    "evalSummary": {
                        "status": "completed",
                        "tasks": [
                            {
                                "id": "instruction_following_lite",
                                "points": 200,
                                "maxPoints": 200,
                                "status": "passed",
                                "details": {
                                    "jsonLineValid": True,
                                    "rawOutput": "STATUS: READY",
                                },
                            },
                            {
                                "id": "grounded_qa_lite",
                                "points": 25,
                                "maxPoints": 50,
                                "status": "failed",
                                "details": {
                                    "groundedCorrect": True,
                                    "refusedUnknown": False,
                                    "rawOutput": "44\n61",
                                },
                            },
                        ],
                    },
                },
                {
                    "rank": 2,
                    "name": "weak-model",
                    "provider": "fixture-provider",
                    "model": "fixture/weak",
                    "score": 300,
                    "healthStatus": "passed",
                    "evalSummary": {
                        "status": "completed",
                        "tasks": [
                            {
                                "id": "instruction_following_lite",
                                "points": 120,
                                "maxPoints": 200,
                                "status": "failed",
                                "details": {
                                    "jsonLineValid": False,
                                    "rawOutput": "here is your answer:\nSTATUS: READY",
                                },
                            },
                            {
                                "id": "grounded_qa_lite",
                                "points": 25,
                                "maxPoints": 50,
                                "status": "failed",
                                "details": {
                                    "groundedCorrect": True,
                                    "refusedUnknown": False,
                                    "rawOutput": "44\n77",
                                },
                            },
                        ],
                    },
                },
            ],
        },
    }


def test_fallback_eval_tab_shows_task_summary_and_raw_outputs(
    page: Page,
    request: pytest.FixtureRequest,
) -> None:
    server_url = request.getfixturevalue("server")
    session = create_authenticated_session(server_url, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server_url}]
    )
    page.add_init_script("localStorage.setItem('llmgateway:locale', 'en');")
    page_errors: list[str] = []
    page.on("pageerror", lambda error: page_errors.append(str(error)))
    page.route(
        "**/v1/fallback-model-evals",
        lambda route: route.fulfill(json=_eval_payload()),
    )

    page.goto(f"{server_url}/v1/ui/rules-editor")
    page.locator('[data-entity-target="fallback-eval"]').click()

    summary = page.locator("#fallbackEvalModels .eval-task-summary")
    expect(summary).to_be_visible()
    expect(summary).to_contain_text("Task summary")

    rows = summary.locator(".eval-task-summary-row")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("instruction_following_lite")
    expect(rows.nth(0)).to_contain_text("passed: 1 of 2")
    expect(rows.nth(0)).to_contain_text("jsonLineValid ×1")
    expect(rows.nth(1)).to_contain_text("grounded_qa_lite")
    expect(rows.nth(1)).to_contain_text("passed: 0 of 2")
    # Обе модели галлюцинируют на отсутствующем факте — это и есть сигнал,
    # который сводка обязана показать поперёк моделей.
    expect(rows.nth(1)).to_contain_text("refusedUnknown ×2")

    details = page.locator("#fallbackEvalModels .eval-task-details").first
    expect(details.locator("summary")).to_contain_text("Model answers per task")
    details.locator("summary").click()
    expect(details.locator(".eval-task-raw-output").first).to_contain_text(
        "STATUS: READY"
    )
    expect(details.locator(".eval-task-detail-checks").first).to_contain_text(
        "refusedUnknown"
    )

    assert page_errors == []


def test_fallback_eval_task_summary_is_localized(
    page: Page,
    request: pytest.FixtureRequest,
) -> None:
    server_url = request.getfixturevalue("server")
    session = create_authenticated_session(server_url, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server_url}]
    )
    page.add_init_script("localStorage.setItem('llmgateway:locale', 'en');")
    page.route(
        "**/v1/fallback-model-evals",
        lambda route: route.fulfill(json=_eval_payload()),
    )

    page.goto(f"{server_url}/v1/ui/rules-editor")
    page.locator('[data-entity-target="fallback-eval"]').click()
    expect(page.locator("#fallbackEvalModels .eval-task-summary")).to_contain_text(
        "Task summary"
    )

    page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")

    summary = page.locator("#fallbackEvalModels .eval-task-summary")
    expect(summary).to_contain_text("Сводка по тестам")
    expect(summary).to_contain_text("прошли: 1 из 2")
    expect(page.locator("#fallbackEvalModels .eval-task-details summary").first).to_contain_text(
        "Ответы моделей по тестам"
    )
