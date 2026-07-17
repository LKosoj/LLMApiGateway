"""Minimal serial integration contract for the R5.8 API Keys and Quota lane."""

from __future__ import annotations

from collections import Counter

import pytest
from playwright.sync_api import Page, expect

from tests.test_ui_regression import (
    provider_mock,
    server,
)
from tests.ui_server_helpers import create_authenticated_session


__all__ = ["provider_mock", "server"]
pytestmark = pytest.mark.browser


def test_api_keys_and_quota_locale_switch_is_snapshot_only(
    page: Page,
    server: str,
) -> None:
    counters: Counter = Counter()
    raw_upstream_detail = "<img src=x onerror=window.__quotaInjected=true>"
    record = {
        "id": 7,
        "name": "team-key",
        "api_key": "lgk_integration_secret",
        "budget_usd": 10.0,
        "spent_usd": 0.125,
        "budget_period": "monthly",
        "rpm": 20,
        "tpm": 1000,
        "allowed_models": ["catalog-model", "saved-only-model"],
        "disabled": False,
        "metadata": {"owner": "team-a"},
        "created_at": "2026-07-15T10:00:00Z",
        "last_used_at": "2026-07-15T11:00:00Z",
    }

    def api_keys_route(route) -> None:
        counters["api_keys"] += 1
        route.fulfill(status=200, json={"keys": [record]})

    def models_route(route) -> None:
        counters["models"] += 1
        route.fulfill(status=200, json={"data": [{"id": "catalog-model"}]})

    def quota_route(route) -> None:
        counters["quota"] += 1
        if counters["quota"] > 1:
            route.fulfill(status=500, json={"detail": "failed"})
            return
        route.fulfill(
            status=200,
            json=[
                {
                    "key_id": 7,
                    "name": "team-key",
                    "disabled": False,
                    "rpm": {"current": 2, "limit": 20, "reset_in_seconds": 60},
                    "tpm": {"current": 125, "limit": 1000, "reset_in_seconds": 60},
                    "budget": {"spent_usd": 0.125, "limit_usd": 10.0},
                    "fallback_events_24h": 0,
                }
            ],
        )

    def upstream_route(route) -> None:
        counters["upstream"] += 1
        route.fulfill(
            status=200,
            json={
                "snapshots": [
                    {
                        "provider": "provider-error",
                        "kind": "github_copilot",
                        "plan": None,
                        "reset_date": None,
                        "categories": {},
                        "fetched_at": 0,
                        "error": raw_upstream_detail,
                    }
                ]
            },
        )

    page.add_init_script(
        "if (localStorage.getItem('llmgateway:locale') === null) "
        "localStorage.setItem('llmgateway:locale', 'en');"
        "if (localStorage.getItem('llmgateway:theme') === null) "
        "localStorage.setItem('llmgateway:theme', 'light');"
    )
    session = create_authenticated_session(server, "test-key")
    page.context.add_cookies(
        [{"name": "llmgateway_session", "value": session, "url": server}]
    )
    page.route(f"{server}/v1/admin/api-keys**", api_keys_route)
    page.route(f"{server}/v1/models", models_route)
    page.route(f"{server}/v1/api/quota/keys", quota_route)
    page.route(f"{server}/v1/admin/upstream-quotas", upstream_route)
    page.clock.install()

    page.set_viewport_size({"width": 390, "height": 844})
    page.goto(f"{server}/v1/ui/api-keys")
    expect(page.locator('tr[data-key-id="7"]')).to_be_visible()
    page.locator('button[data-action="edit"]').click()
    expect(page.locator("#allowedModelsList")).to_have_attribute(
        "data-model-catalog-state", "ready"
    )
    catalog_checkbox = page.locator(
        '#allowedModelsList input[value="catalog-model"]'
    )
    missing_checkbox = page.locator(
        '#allowedModelsList input[value="saved-only-model"]'
    )
    expect(catalog_checkbox).to_be_checked()
    expect(missing_checkbox).to_be_checked()
    page.locator("#fieldName").fill("team-alpha")
    page.evaluate(
        """() => {
            window.__r58KeysRow = document.querySelector('tr[data-key-id="7"]');
            window.__r58KeysField = document.getElementById('fieldName');
            window.__r58CatalogCheckbox = document.querySelector(
                '#allowedModelsList input[value="catalog-model"]'
            );
            window.__r58MissingCheckbox = document.querySelector(
                '#allowedModelsList input[value="saved-only-model"]'
            );
        }"""
    )
    before_keys_locale = dict(counters)

    page.select_option("#localeSelect", "ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    expect(page.locator('[data-key-field="status"]')).to_have_text("Активен")
    expect(page.locator("#modalTitle")).to_contain_text("Изменить")
    assert dict(counters) == before_keys_locale
    page.select_option("#localeSelect", "en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    assert dict(counters) == before_keys_locale
    assert page.evaluate(
        """() => window.__r58KeysRow === document.querySelector('tr[data-key-id="7"]')
            && window.__r58KeysField === document.getElementById('fieldName')
            && window.__r58CatalogCheckbox === document.querySelector(
                '#allowedModelsList input[value="catalog-model"]'
            )
            && window.__r58MissingCheckbox === document.querySelector(
                '#allowedModelsList input[value="saved-only-model"]'
            )"""
    )
    expect(page.locator("#fieldName")).to_have_value("team-alpha")
    expect(catalog_checkbox).to_be_checked()
    expect(missing_checkbox).to_be_checked()

    page.locator("#cancelKeyBtn").click()
    expect(page.locator("#keyModal")).to_be_hidden()
    expect(page.locator("body")).not_to_have_class("dark-mode")
    page.locator("#darkModeToggle").click()
    expect(page.locator("body")).to_have_class("dark-mode")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )

    page.goto(f"{server}/v1/ui/quota")
    expect(page.locator("#quota-grid")).to_have_attribute(
        "data-quota-state", "ready"
    )
    expect(page.locator(".quota-card")).to_have_count(1)
    expect(page.locator(".upstream-card")).to_have_count(1)
    expect(page.locator(".upstream-card-error-summary")).to_have_text(
        "Could not load provider quota."
    )
    raw_detail = page.locator(".upstream-card-error-detail")
    expect(raw_detail).to_have_text(raw_upstream_detail)
    expect(raw_detail).to_have_attribute("lang", "und")
    expect(raw_detail).to_have_attribute("dir", "auto")
    assert page.locator(".upstream-card-error-detail img").count() == 0
    assert page.evaluate("window.__quotaInjected !== true")

    page.clock.run_for(2000)
    expect(page.locator(".countdown-badge").first).to_have_text("00:58")
    state_before = page.evaluate(
        """() => {
            const card = document.querySelector('.quota-card');
            const metric = document.querySelector('.quota-metric-count');
            const spacer = document.createElement('span');
            spacer.dataset.testSpacer = 'true';
            spacer.style.cssText = 'display:block;width:900px;height:1px';
            card.style.cssText += ';overflow:auto;max-width:220px';
            card.appendChild(spacer);
            card.scrollLeft = 70;
            metric.tabIndex = 0;
            metric.focus({preventScroll: true});
            const pageSpacer = document.createElement('div');
            pageSpacer.style.height = '1200px';
            document.body.appendChild(pageSpacer);
            void document.body.offsetHeight;
            window.scrollTo({top: 80, left: 0, behavior: 'instant'});
            window.__r58QuotaCard = card;
            window.__r58QuotaMetric = metric;
            window.__r58QuotaCountdown = document.querySelector('.countdown-badge');
            window.__r58UpstreamCard = document.querySelector('.upstream-card');
            return {windowY: window.scrollY, cardLeft: card.scrollLeft};
        }"""
    )
    assert state_before["windowY"] > 0
    assert state_before["cardLeft"] > 0
    before_quota_locale = dict(counters)

    page.select_option("#localeSelect", "ru")
    expect(page.locator("html")).to_have_attribute("lang", "ru")
    page.clock.run_for(20)
    expect(page.locator("#quota-status")).to_contain_text("Обновлено")
    expect(page.locator(".upstream-card-error-summary")).to_have_text(
        "Не удалось загрузить квоту провайдера."
    )
    expect(raw_detail).to_have_text(raw_upstream_detail)
    assert dict(counters) == before_quota_locale
    page.select_option("#localeSelect", "en")
    expect(page.locator("html")).to_have_attribute("lang", "en")
    page.clock.run_for(20)
    expect(page.locator("#quota-status")).to_contain_text("Updated")
    assert dict(counters) == before_quota_locale
    assert page.evaluate(
        """() => window.__r58QuotaCard === document.querySelector('.quota-card')
            && window.__r58QuotaMetric === document.querySelector('.quota-metric-count')
            && window.__r58QuotaCountdown === document.querySelector('.countdown-badge')
            && window.__r58UpstreamCard === document.querySelector('.upstream-card')
            && window.__r58QuotaMetric === document.activeElement
            && document.querySelector('[data-test-spacer="true"]') !== null"""
    )
    assert page.evaluate("window.scrollY") == state_before["windowY"]
    assert page.locator(".quota-card").evaluate("card => card.scrollLeft") == state_before[
        "cardLeft"
    ]
    expect(page.locator(".countdown-badge").first).to_have_text("00:58")
    page.clock.run_for(1000)
    expect(page.locator(".countdown-badge").first).to_have_text("00:57")
    assert dict(counters) == before_quota_locale

    expected_stale_counters = dict(before_quota_locale)
    expected_stale_counters["quota"] += 1
    page.clock.run_for(2000)
    expect(page.locator("#quota-grid")).to_have_attribute(
        "data-quota-state", "stale"
    )
    expect(page.locator("#quota-status")).to_contain_text("outdated")
    assert dict(counters) == expected_stale_counters
    assert page.evaluate(
        """() => window.__r58QuotaCard === document.querySelector('.quota-card')
            && window.__r58QuotaMetric === document.querySelector('.quota-metric-count')
            && window.__r58UpstreamCard === document.querySelector('.upstream-card')"""
    )
    expect(page.locator("body")).to_have_class("dark-mode")
    assert page.evaluate(
        "document.documentElement.scrollWidth <= document.documentElement.clientWidth"
    )
