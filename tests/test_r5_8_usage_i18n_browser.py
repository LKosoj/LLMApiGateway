from __future__ import annotations

import json
import os
import time
from collections import Counter
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, expect

from tests.ui_server_helpers import get_free_port, isolated_gateway_process, wait_for_gateway


pytestmark = pytest.mark.browser
MASTER_KEY = "r5-8-usage-master"


def _write_config(root: Path) -> Path:
    providers = root / "providers.json"
    providers.write_text(
        json.dumps(
            [
                {
                    "provider-a": {
                        "baseUrl": "https://provider.invalid/v1",
                        "apikey": "test-only-key",
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    for filename, content in {
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
    }.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers


@pytest.fixture(scope="module")
def usage_i18n_server(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    temp_path = tmp_path_factory.mktemp("r5-8-usage")
    providers = _write_config(temp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "provider-a",
            "PROVIDERS_FILENAME": str(providers),
            "FALLBACK_RULES_FILENAME": str(temp_path / "models_fallback_rules.json"),
            "MODEL_RULES_FILENAME": str(temp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(temp_path / "models_operation_rules.json"),
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


def _login(context: BrowserContext, base_url: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/usage-stats"},
    )
    assert response.status == 200, response.text()


def _analytics_payload() -> dict:
    recent_record = {
        "gateway_model": "gateway/model-a",
        "operation": "chat",
        "x_title": "raw-title",
        "provider": "provider-a",
        "model": "model-a",
        "total_tokens": 1234,
        "cost": 1.25,
    }
    return {
        "filters": {"bucket": "day"},
        "totals": {
            "requests": 2,
            "total_tokens": 1234,
            "cost": 1.25,
            "cost_saved": 0.5,
            "cost_per_million_tokens": 1012.5,
            "avg_duration_ms": 1500,
            "active_requests": 1,
            "fallback_errors": 1,
            "rejections": 0,
            "estimated_count": 1,
        },
        "series": {
            "usage": [
                {"time_period": "2026-07-14", "total_tokens": 100},
                {"time_period": "2026-07-15", "total_tokens": 200},
            ]
        },
        "breakdowns": {
            "providers": [{"label": "provider-a", "cost": 1.25, "requests": 2}],
            "resolved_targets": [
                {
                    "label": "provider-a/model-a",
                    "requests": 2,
                    "total_tokens": 1234,
                    "cost": 1.25,
                    "cost_saved": 0.5,
                    "estimated_count": 1,
                    "avg_duration_ms": 1500,
                }
            ],
            "x_titles": [],
            "api_keys": [],
        },
        "reliability": {
            "fallback": {"summary": {"attempts": 2, "errors": 1, "success_rate": 50}},
            "rejections": {"summary": {"rejections": 0}, "categories": []},
        },
        "recent_records": [
            {**recent_record, "timestamp": "2026-07-15T10:30:00+00:00", "status": "completed"},
            {**recent_record, "timestamp": "2026-07-15T10:29:00+00:00", "status": "running"},
            {**recent_record, "timestamp": "2026-07-15T10:28:00+00:00", "status": "in_progress"},
            {**recent_record, "timestamp": "2026-07-15T10:27:00+00:00", "status": "failed"},
            {
                **recent_record,
                "timestamp": "2026-07-15T10:26:00+00:00",
                "status": "usage:values.completedLabel",
            },
        ],
        "filter_options": {
            "operations": ["chat"],
            "gateway_models": ["gateway/model-a"],
            "providers": ["provider-a"],
            "models": ["model-a"],
            "x_titles": ["raw-title"],
            "upstream_keys": ["upstream-key-a"],
        },
    }


def _wait_for_count(counter: Counter, key: str, count: int, page: Page) -> None:
    deadline = time.monotonic() + 5
    while counter[key] < count and time.monotonic() < deadline:
        page.wait_for_timeout(25)
    assert counter[key] >= count


def _wait_for_app_settle(page: Page) -> None:
    page.evaluate(
        "() => new Promise(resolve => requestAnimationFrame(() => requestAnimationFrame(resolve)))"
    )


def test_usage_analytics_topology_locale_rerender_is_snapshot_only_and_state_safe(
    browser: Browser,
    usage_i18n_server: str,
) -> None:
    context = browser.new_context(viewport={"width": 1440, "height": 900})
    counters: Counter = Counter()
    held_routes = {}
    held_once = set()
    try:
        _login(context, usage_i18n_server)
        page = context.new_page()
        page.add_init_script(
            "localStorage.setItem('llmgateway:locale', 'en');"
            "localStorage.setItem('llmgateway:theme', 'light');"
        )

        def fulfill_or_hold_once(key, route, payload):
            if key not in held_once:
                held_once.add(key)
                held_routes[key] = (route, payload)
                return
            route.fulfill(json=payload)

        def assert_loading_rerenders_without_api_fetch(
            selector: str,
            english: str,
            russian: str,
        ) -> None:
            expect(page.locator(selector)).to_have_text(english)
            before_locale = dict(counters)
            page.evaluate("async () => gatewayI18n.changeLanguage('ru')")
            expect(page.locator(selector)).to_have_text(russian)
            assert dict(counters) == before_locale
            page.evaluate("async () => gatewayI18n.changeLanguage('en')")
            expect(page.locator(selector)).to_have_text(english)
            assert dict(counters) == before_locale

        def analytics_route(route):
            counters["analytics"] += 1
            route.fulfill(json=_analytics_payload())

        def stats_route(route):
            counters["stats"] += 1
            period = route.request.url.rsplit("/", 1)[-1]
            payload = [
                {
                    "time_period": "2026-W28" if period == "week" else "2026-07-15",
                    "gateway_model": "gateway/model-a",
                    "operation": "chat",
                    "provider": "provider-a",
                    "model": "model-a",
                    "prompt_tokens": 1000,
                    "completion_tokens": 234,
                    "reasoning_tokens": 0,
                    "total_tokens": 1234,
                    "cached_tokens": 12,
                    "count": 2,
                    "cost": 1.25,
                    "cost_saved": 0.5,
                }
            ]
            if period == "day":
                fulfill_or_hold_once("stats", route, payload)
                return
            route.fulfill(json=payload)

        def records_route(route):
            counters["records"] += 1
            query = parse_qs(urlparse(route.request.url).query)
            offset = int(query.get("offset", ["0"])[0])
            page_number = offset // 25 + 1
            payload = {
                "records": [
                    {
                        "id": page_number,
                        "timestamp": "2026-07-15T10:30:00.123456",
                        "duration_ms": 1500,
                        "gateway_model": "gateway/model-a",
                        "operation": "chat",
                        "provider": "provider-a",
                        "model": f"model-page-{page_number}",
                        "x_title": "raw-title",
                        "prompt_tokens": 1000,
                        "completion_tokens": 234,
                        "reasoning_tokens": 0,
                        "total_tokens": 1234,
                        "cached_tokens": 12,
                        "cost": 1.25,
                        "cost_saved": 0.5,
                        "is_estimated": False,
                    }
                ],
                "total_records": 50,
            }
            fulfill_or_hold_once("records", route, payload)

        def fallback_stats_route(route):
            counters["fallback_stats"] += 1
            payload = [
                {
                    "time_period": "2026-07-15",
                    "gateway_model": "gateway/model-a",
                    "provider": "provider-a",
                    "model": "model-a",
                    "error_type": "http_429",
                    "count": 1,
                    "avg_duration_ms": 250,
                }
            ]
            fulfill_or_hold_once("fallback_stats", route, payload)

        def fallback_records_route(route):
            counters["fallback_records"] += 1
            payload = {
                "records": [
                    {
                        "request_id": "request-raw-1",
                        "timestamp": "2026-07-15T10:30:00",
                        "gateway_model": "gateway/model-a",
                        "x_title": "raw-title",
                        "total_attempts": 2,
                        "total_duration_ms": 1750,
                        "success": True,
                        "attempts": [
                            {
                                "attempt_number": 1,
                                "provider": "provider-a",
                                "model": "model-a",
                                "success": False,
                                "error_type": "http_429",
                                "error_message": "<b>raw upstream detail</b>",
                                "duration_ms": 250,
                            },
                            {
                                "attempt_number": 2,
                                "provider": "provider-a",
                                "model": "model-b",
                                "success": True,
                                "error_type": None,
                                "error_message": None,
                                "duration_ms": 1500,
                            },
                        ],
                    }
                ],
                "total_records": 1,
            }
            fulfill_or_hold_once("fallback_records", route, payload)

        def upstream_route(route):
            counters["upstream"] += 1
            payload = [
                {
                    "time_period": "2026-07-15",
                    "gateway_model": "gateway/model-a",
                    "provider": "provider-a",
                    "model": "model-a",
                    "operation": "chat",
                    "upstream_key_fingerprint": "raw-key-fingerprint",
                    "attempts": 2,
                    "successes": 1,
                    "errors": 1,
                    "success_rate": 50,
                    "avg_duration_ms": 250,
                    "max_duration_ms": 1500,
                }
            ]
            fulfill_or_hold_once("upstream", route, payload)

        def topology_route(route):
            counters["topology"] += 1
            route.fulfill(
                json={
                    "nodes": [
                        {"id": "gateway", "type": "central", "label": "LLM Gateway", "data": {}},
                        {
                            "id": "model:gateway/model-a",
                            "type": "model",
                            "label": "gateway/model-a",
                            "data": {
                                "active_requests": 1,
                                "max_total_attempts": 3,
                                "flags": {"rotate_models": True},
                                "chain": [
                                    {
                                        "priority": 1,
                                        "provider": "provider-a",
                                        "model": "model-a",
                                        "retry_count": 1,
                                        "retry_delay": 250,
                                    }
                                ],
                            },
                        },
                        {
                            "id": "provider:provider-a",
                            "type": "provider",
                            "label": "provider-a",
                            "data": {
                                "health": "ok",
                                "configured": True,
                                "active_requests": 1,
                                "penalty": 0.25,
                                "models": ["model-a"],
                            },
                        },
                    ],
                    "edges": [
                        {
                            "id": "gateway-model",
                            "source": "gateway",
                            "target": "model:gateway/model-a",
                            "type": "alias",
                            "data": {},
                        },
                        {
                            "id": "model-provider",
                            "source": "model:gateway/model-a",
                            "target": "provider:provider-a",
                            "type": "fallback",
                            "data": {"steps": [1], "priority": 1},
                        },
                    ],
                }
            )

        page.route(f"{usage_i18n_server}/v1/api/analytics-dashboard*", analytics_route)
        page.route(f"{usage_i18n_server}/v1/api/usage-stats/*", stats_route)
        page.route(f"{usage_i18n_server}/v1/api/usage-records*", records_route)
        page.route(f"{usage_i18n_server}/v1/api/fallback-stats/*", fallback_stats_route)
        page.route(f"{usage_i18n_server}/v1/api/fallback-records*", fallback_records_route)
        page.route(f"{usage_i18n_server}/v1/api/upstream-stats/*", upstream_route)
        page.route(f"{usage_i18n_server}/v1/topology", topology_route)

        page.goto(f"{usage_i18n_server}/v1/ui/usage-stats")
        expect(page.locator("#analyticsDashboard")).to_be_visible()
        recent_status_cells = page.locator("#analyticsRecentTable tbody tr td:nth-child(2)")
        expect(recent_status_cells).to_have_count(5)
        assert recent_status_cells.all_inner_texts() == [
            "Completed",
            "Running",
            "In progress",
            "Failed",
            "usage:values.completedLabel",
        ]
        expect(recent_status_cells.nth(4)).to_have_attribute("lang", "und")
        expect(recent_status_cells.nth(4)).to_have_attribute("dir", "auto")
        page.set_viewport_size({"width": 390, "height": 900})
        page.select_option("#analyticsProvider", "provider-a")
        _wait_for_count(counters, "analytics", 2, page)
        expect(recent_status_cells).to_have_count(5)
        analytics_scroll_left = page.locator("#analyticsRecentTable").evaluate(
            "element => { element.scrollLeft = 160; return element.scrollLeft; }"
        )
        assert analytics_scroll_left > 0

        page.locator('[data-tab="stats"]').click()
        _wait_for_count(counters, "stats", 1, page)
        assert_loading_rerenders_without_api_fetch(
            "#statsArea",
            "Loading statistics…",
            "Загрузка статистики…",
        )
        page.select_option("#periodSelector", "week")
        _wait_for_count(counters, "stats", 2, page)
        expect(page.locator("#statsArea")).to_contain_text("2026-W28")
        held_route, held_payload = held_routes.pop("stats")
        held_route.fulfill(json=held_payload)
        _wait_for_app_settle(page)
        expect(page.locator("#statsArea")).to_contain_text("2026-W28")

        page.locator('[data-tab="records"]').click()
        _wait_for_count(counters, "records", 1, page)
        assert_loading_rerenders_without_api_fetch(
            "#recordsArea",
            "Loading usage records…",
            "Загрузка записей использования…",
        )
        held_route, held_payload = held_routes.pop("records")
        held_route.fulfill(json=held_payload)
        expect(page.locator("#recordsArea")).to_contain_text("model-page-1")
        page.locator("#nextPage").click()
        expect(page.locator("#recordsArea")).to_contain_text("model-page-2")
        records_scroll_left = page.locator("#recordsArea").evaluate(
            "element => { element.scrollLeft = 240; return element.scrollLeft; }"
        )
        assert records_scroll_left > 0

        page.locator('[data-tab="fallback"]').click()
        _wait_for_count(counters, "fallback_stats", 1, page)
        assert_loading_rerenders_without_api_fetch(
            "#fallbackStatsArea",
            "Loading fallback statistics…",
            "Загрузка статистики резервных маршрутов…",
        )
        held_route, held_payload = held_routes.pop("fallback_stats")
        held_route.fulfill(json=held_payload)
        expect(page.locator("#fallbackStatsArea")).to_contain_text("provider-a")
        page.locator('[data-subtab="chains"]').click()
        _wait_for_count(counters, "fallback_records", 1, page)
        assert_loading_rerenders_without_api_fetch(
            "#fallbackChainsArea",
            "Loading fallback chains…",
            "Загрузка цепочек резервных маршрутов…",
        )
        held_route, held_payload = held_routes.pop("fallback_records")
        held_route.fulfill(json=held_payload)
        chain_header = page.locator("#fallbackChainsArea .chain-header")
        chain_header.click()
        expect(chain_header).to_have_attribute("aria-expanded", "true")
        raw_chain_error = page.locator("#fallbackChainsArea .attempt-error-message")
        expect(raw_chain_error).to_have_text("<b>raw upstream detail</b>")
        expect(raw_chain_error).to_have_attribute("lang", "und")
        expect(raw_chain_error).to_have_attribute("dir", "auto")
        assert page.locator("#fallbackChainsArea b").count() == 0
        page.locator('[data-subtab="upstream"]').click()
        _wait_for_count(counters, "upstream", 1, page)
        assert_loading_rerenders_without_api_fetch(
            "#upstreamStatsArea",
            "Loading upstream analytics…",
            "Загрузка upstream-аналитики…",
        )
        held_route, held_payload = held_routes.pop("upstream")
        held_route.fulfill(json=held_payload)
        expect(page.locator("#upstreamStatsArea")).to_contain_text("raw-key-fingerprint")

        page.locator('[data-tab="topology"]').click()
        expect(page.locator(".react-flow")).to_be_visible()
        page.uncheck("#topologyAutoRefresh")
        model_node = page.locator('.react-flow__node[data-id="model:gateway/model-a"]')
        model_node.click()
        expect(page.locator(".topology-detail")).to_be_visible()
        page.locator(".react-flow__controls-zoomin").click()
        topology_state = page.evaluate(
            """
            () => {
                window.__r58TopologyMount = document.querySelector('#topology-container > div');
                return document.querySelector('.react-flow__viewport').getAttribute('style');
            }
            """
        )

        page.locator('[data-tab="fallback"]').click()
        page.locator('[data-subtab="chains"]').click()
        chain_header = page.locator("#fallbackChainsArea .chain-header")
        expect(chain_header).to_have_attribute("aria-expanded", "true")
        chain_header.focus()
        assert chain_header.evaluate("element => document.activeElement === element")
        page.set_viewport_size({"width": 390, "height": 240})
        page.evaluate("document.scrollingElement.scrollTop = 80")
        page.wait_for_function("window.scrollY === 80")

        scroll_before_ru = page.evaluate(
            """() => {
                return {
                    windowY: window.scrollY,
                };
            }"""
        )
        assert scroll_before_ru["windowY"] > 0

        before_locale = dict(counters)
        page.evaluate("async () => gatewayI18n.changeLanguage('ru')")
        expect(page.locator("html")).to_have_attribute("lang", "ru")
        expect(page.locator("h1")).to_contain_text("статистика использования")
        expect(page.locator('[data-tab="fallback"]')).to_have_attribute("aria-selected", "true")
        expect(page.locator('[data-subtab="chains"]')).to_have_attribute("aria-selected", "true")
        expect(page.locator("#pageInfo")).to_have_text("Страница 2 из 2")
        expect(page.locator("#fallbackChainsArea .chain-header")).to_have_attribute("aria-expanded", "true")
        assert page.locator("#fallbackChainsArea .chain-header").evaluate(
            "element => document.activeElement === element"
        )
        page.wait_for_function(
            "expected => window.scrollY === expected.windowY",
            arg=scroll_before_ru,
        )
        assert dict(counters) == before_locale
        expect(page.locator("#analyticsProvider")).to_have_value("provider-a")
        expect(page.locator("#analyticsKpis")).to_contain_text("Всего токенов")
        assert recent_status_cells.all_inner_texts() == [
            "Завершено",
            "Выполняется",
            "В процессе",
            "Ошибка",
            "usage:values.completedLabel",
        ]
        expect(recent_status_cells.nth(4)).to_have_attribute("lang", "und")
        expect(recent_status_cells.nth(4)).to_have_attribute("dir", "auto")
        expect(page.locator("#analyticsLineChart svg")).to_have_attribute(
            "aria-label", "Линейный график динамики токенов"
        )
        expect(page.locator(".topology-detail")).to_contain_text("Активные запросы")
        expect(page.locator(".react-flow__controls-zoomin")).to_have_attribute(
            "title", "Увеличить масштаб"
        )
        assert page.evaluate(
            "window.__r58TopologyMount === document.querySelector('#topology-container > div')"
        )
        assert page.locator(".react-flow__viewport").get_attribute("style") == topology_state
        assert not page.is_checked("#topologyAutoRefresh")

        page.locator('[data-tab="analytics"]').click()
        expect(page.locator("#analyticsDashboard")).to_be_visible()
        analytics_scroll_left = page.locator("#analyticsRecentTable").evaluate(
            "element => { element.scrollLeft = 160; return element.scrollLeft; }"
        )
        assert analytics_scroll_left > 0
        page.evaluate("document.scrollingElement.scrollTop = 80")
        page.wait_for_function("window.scrollY === 80")
        scroll_before_en = page.evaluate(
            """() => ({
                windowY: window.scrollY,
                analyticsLeft: document.getElementById('analyticsRecentTable').scrollLeft,
            })"""
        )
        before_english = dict(counters)
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")
        expect(page.locator("html")).to_have_attribute("lang", "en")
        expect(page.locator("#analyticsKpis")).to_contain_text("Total Tokens")
        page.wait_for_function(
            """expected => window.scrollY === expected.windowY
                && document.getElementById('analyticsRecentTable').scrollLeft === expected.analyticsLeft""",
            arg=scroll_before_en,
        )
        assert dict(counters) == before_english
        assert recent_status_cells.all_inner_texts() == [
            "Completed",
            "Running",
            "In progress",
            "Failed",
            "usage:values.completedLabel",
        ]

        malicious = "<img src=x onerror=window.__r58Injected=true>"
        page.route(
            f"{usage_i18n_server}/v1/api/analytics-dashboard*",
            lambda route: route.fulfill(
                status=503,
                headers={"X-Request-ID": "request-id-raw"},
                json={"detail": malicious},
            ),
        )
        page.locator("#analyticsRefreshButton").click()
        expect(page.locator("#analyticsStatus")).to_contain_text("Request failed (HTTP 503)")
        expect(page.locator("#analyticsRawDetail")).to_have_text(malicious)
        expect(page.locator("#analyticsRawDetail")).to_have_attribute("lang", "und")
        expect(page.locator("#analyticsRawDetail")).to_have_attribute("dir", "auto")
        expect(page.locator("#analyticsRequestId")).to_contain_text("request-id-raw")
        assert page.locator("#analyticsRawDetail img").count() == 0
        assert page.evaluate("window.__r58Injected !== true")

        before_error_locale = dict(counters)
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(page.locator("html")).to_have_attribute("lang", "ru")
        expect(page.locator("#analyticsStatus")).to_contain_text("HTTP 503")
        assert dict(counters) == before_error_locale
        expect(page.locator("#analyticsRawDetail")).to_have_text(malicious)
        expect(page.locator("#analyticsProvider")).to_have_value("provider-a")

        for width in (390, 768, 1440):
            page.set_viewport_size({"width": width, "height": 900})
            assert page.evaluate(
                "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
            )
    finally:
        context.close()
