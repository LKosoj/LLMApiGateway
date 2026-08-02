import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (  # noqa: F401
    create_authenticated_session,
    provider_mock,
    server,
)


pytestmark = pytest.mark.browser


def test_eval_tabs_recover_pending_requests_without_stale_writes(
    page: Page,
    request: pytest.FixtureRequest,
) -> None:
    server_url = request.getfixturevalue("server")
    openrouter_requests = []
    openrouter_run_requests = []
    fallback_requests = []
    fallback_run_requests = []
    openrouter_request_count = 0
    initial_rules_request_count = 0

    def route_openrouter(route) -> None:
        nonlocal openrouter_request_count
        openrouter_request_count += 1
        if openrouter_request_count == 1:
            route.fulfill(json={"configured": True})
            return
        openrouter_requests.append(route)

    def count_initial_rules_request(route) -> None:
        nonlocal initial_rules_request_count
        initial_rules_request_count += 1
        route.continue_()

    page.route("**/v1/openrouter/free-models", route_openrouter)
    page.route(
        "**/v1/openrouter/free-models/run",
        lambda route: openrouter_run_requests.append(route),
    )
    page.route("**/v1/config/models-rules/structured", count_initial_rules_request)
    page.route(
        "**/v1/fallback-model-evals",
        lambda route: fallback_requests.append(route),
    )
    page.route(
        "**/v1/fallback-model-evals/run",
        lambda route: fallback_run_requests.append(route),
    )
    session = create_authenticated_session(server_url, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server_url}]
    )
    page.goto(f"{server_url}/v1/ui/rules-editor")
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    assert initial_rules_request_count == 1
    expect(page.locator('[data-entity-target="openrouter-free"]')).to_be_visible()

    page.click('[data-entity-target="openrouter-free"]')
    _wait_for_routes(page, openrouter_requests, 1)
    page.click('[data-entity-target="openrouter-free"]')
    _wait_for_routes(page, openrouter_requests, 2)
    openrouter_requests[0].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": False,
            "snapshot": {"models": [{"name": "stale-model"}]},
        }
    )
    openrouter_requests[1].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": False,
            "snapshot": {"models": [{"name": "fresh-model"}]},
        }
    )
    expect(page.locator("#openRouterFreeModels")).to_contain_text("fresh-model")
    expect(page.locator("#openRouterFreeModels")).not_to_contain_text("stale-model")

    page.click("#runOpenRouterFreeEvalButton")
    _wait_for_routes(page, openrouter_run_requests, 1)
    page.click('[data-entity-target="openrouter-free"]')
    openrouter_run_requests[0].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": True,
            "snapshot": {"models": [{"name": "running-model"}]},
        }
    )
    _wait_for_routes(page, openrouter_requests, 3, timeout_ms=4_000)
    openrouter_requests[2].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": False,
            "snapshot": {"models": [{"name": "completed-model"}]},
        }
    )
    expect(page.locator("#openRouterFreeModels")).to_contain_text("completed-model")
    expect(page.locator("#runOpenRouterFreeEvalButton")).to_be_enabled()

    page.click("#runOpenRouterFreeEvalButton")
    _wait_for_routes(page, openrouter_run_requests, 2)
    page.click('[data-entity-target="rules"]')
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    openrouter_count_before_leave_response = len(openrouter_requests)
    openrouter_run_requests[1].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": True,
            "snapshot": {"models": [{"name": "stale-leave-openrouter"}]},
        }
    )
    page.wait_for_timeout(3_200)
    assert len(openrouter_requests) == openrouter_count_before_leave_response
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    expect(page.locator("#openRouterFreeModels")).not_to_contain_text(
        "stale-leave-openrouter"
    )
    page.click('[data-entity-target="openrouter-free"]')
    _wait_for_routes(page, openrouter_requests, 4)
    openrouter_requests[3].fulfill(
        json={
            "configured": True,
            "manualRefreshRunning": False,
            "snapshot": {"models": [{"name": "recovered-openrouter"}]},
        }
    )
    expect(page.locator("#openRouterFreeModels")).to_contain_text("recovered-openrouter")
    expect(page.locator("#runOpenRouterFreeEvalButton")).to_be_enabled()

    page.click('[data-entity-target="fallback-eval"]')
    _wait_for_routes(page, fallback_requests, 1)
    page.click('[data-entity-target="fallback-eval"]')
    _wait_for_routes(page, fallback_requests, 2)
    fallback_requests[0].fulfill(
        json={"lastCheckedAt": None, "lastError": "stale-error", "running": False, "snapshot": None}
    )
    fallback_requests[1].fulfill(
        json={"lastCheckedAt": None, "lastError": "fresh-error", "running": False, "snapshot": None}
    )
    expect(page.locator("#fallbackEvalStatus")).to_contain_text("fresh-error")
    expect(page.locator("#fallbackEvalStatus")).not_to_contain_text("stale-error")

    page.click("#runFallbackEvalButton")
    _wait_for_routes(page, fallback_run_requests, 1)
    page.click('[data-entity-target="fallback-eval"]')
    fallback_run_requests[0].fulfill(
        json={"lastCheckedAt": None, "lastError": None, "running": True, "snapshot": None}
    )
    _wait_for_routes(page, fallback_requests, 3, timeout_ms=4_000)
    fallback_requests[2].fulfill(
        json={
            "lastCheckedAt": None,
            "lastError": "completed-eval",
            "running": False,
            "snapshot": None,
        }
    )
    expect(page.locator("#fallbackEvalStatus")).to_contain_text("completed-eval")
    expect(page.locator("#runFallbackEvalButton")).to_be_enabled()

    page.click("#runFallbackEvalButton")
    _wait_for_routes(page, fallback_run_requests, 2)
    page.click('[data-entity-target="rules"]')
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    fallback_count_before_leave_response = len(fallback_requests)
    fallback_run_requests[1].fulfill(
        json={
            "lastCheckedAt": None,
            "lastError": "stale-leave-fallback",
            "running": True,
            "snapshot": None,
        }
    )
    page.wait_for_timeout(3_200)
    assert len(fallback_requests) == fallback_count_before_leave_response
    expect(page.locator("#messageArea")).to_contain_text(
        "Fallback Rules loaded successfully"
    )
    expect(page.locator("#fallbackEvalStatus")).not_to_contain_text(
        "stale-leave-fallback"
    )
    page.click('[data-entity-target="fallback-eval"]')
    _wait_for_routes(page, fallback_requests, 4)
    fallback_requests[3].fulfill(
        json={
            "lastCheckedAt": None,
            "lastError": "recovered-eval",
            "running": False,
            "snapshot": None,
        }
    )
    expect(page.locator("#fallbackEvalStatus")).to_contain_text("recovered-eval")
    expect(page.locator("#runFallbackEvalButton")).to_be_enabled()


def _wait_for_routes(
    page: Page,
    routes: list,
    expected_count: int,
    *,
    timeout_ms: int = 1_000,
) -> None:
    for _ in range(max(1, timeout_ms // 20)):
        if len(routes) >= expected_count:
            return
        page.wait_for_timeout(20)
    assert len(routes) >= expected_count
