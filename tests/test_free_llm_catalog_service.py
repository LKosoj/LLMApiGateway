"""Tests for the free-tier LLM catalog fetch service (no real network).

Covers:
- `parse_monthly_token_budget_floor` against every observed source format.
- `build_snapshot` filtering (disabled, below threshold, unknown budget,
  a provider left without survivors) and deterministic sorting.
- `FreeLlmCatalogService.refresh_once` success/failure paths (HTTP status
  errors, malformed JSON, connect/timeout errors) via `httpx.MockTransport`.
- `get_status()` contract shape.
"""
from __future__ import annotations

import asyncio
import unittest

import httpx

from llm_gateway_core.services.free_llm_catalog import (
    FREE_LLM_CATALOG_SOURCE_URL,
    FreeLlmCatalogService,
    build_snapshot,
    parse_monthly_token_budget_floor,
)
from tests._async_compat import run_async


class ParseMonthlyTokenBudgetFloorTests(unittest.TestCase):
    def test_parses_every_observed_token_budget_format(self):
        cases = {
            "~6M": 6_000_000,
            "~50-100M": 50_000_000,
            "~1-2M": 1_000_000,
            "~2M (60-100/hr)": 2_000_000,
            "~2-3M (200/hr)": 2_000_000,
            "~100M": 100_000_000,
            "~18-45M": 18_000_000,
        }
        for raw, expected in cases.items():
            with self.subTest(raw=raw):
                self.assertEqual(parse_monthly_token_budget_floor(raw), expected)

    def test_returns_none_for_non_token_budgets_and_bad_input(self):
        cases = [
            "free · 40 RPM",
            "free · 2/min per IP",
            "free · 200/hr per IP",
            "free · promo $0/token",
            "promo (trial)",
            "~? (anon)",
            "",
            None,
            123,
        ]
        for raw in cases:
            with self.subTest(raw=raw):
                self.assertIsNone(parse_monthly_token_budget_floor(raw))


def _platform(platform_id: str, name: str) -> dict:
    return {"id": platform_id, "name": name}


def _model(
    *,
    platform: str,
    model_id: str,
    display_name: str,
    enabled: bool = True,
    monthly_token_budget: str = "~40M",
    limits: dict | None = None,
    context_window: int | None = 131072,
    supports_vision: bool = False,
    supports_tools: bool = True,
    intelligence_rank: int | None = 5,
    speed_rank: int | None = 5,
    size_label: str | None = "Medium",
    quirks: list[dict] | None = None,
) -> dict:
    return {
        "platform": platform,
        "modelId": model_id,
        "displayName": display_name,
        "intelligenceRank": intelligence_rank,
        "speedRank": speed_rank,
        "sizeLabel": size_label,
        "limits": limits or {"rpm": None, "rpd": None, "tpm": None, "tpd": None},
        "monthlyTokenBudget": monthly_token_budget,
        "contextWindow": context_window,
        "enabled": enabled,
        "supportsVision": supports_vision,
        "supportsTools": supports_tools,
        "quirks": quirks or [],
    }


class BuildSnapshotTests(unittest.TestCase):
    def test_drops_disabled_below_threshold_unknown_budget_and_empty_providers(self):
        payload = {
            "version": "2026.07.18",
            "generatedAt": "2026-07-18T07:03:02.234Z",
            "platforms": [
                _platform("alpha", "Alpha Provider"),
                _platform("gamma", "Gamma Provider"),
                _platform("delta", "Delta Provider"),
            ],
            "models": [
                _model(
                    platform="alpha",
                    model_id="alpha-ok",
                    display_name="Alpha OK",
                    monthly_token_budget="~40M",
                ),
                _model(
                    platform="gamma",
                    model_id="gamma-disabled",
                    display_name="Gamma Disabled",
                    enabled=False,
                    monthly_token_budget="~100M",
                ),
                _model(
                    platform="gamma",
                    model_id="gamma-unknown-budget",
                    display_name="Gamma Unknown Budget",
                    monthly_token_budget="free · 40 RPM",
                ),
                _model(
                    platform="delta",
                    model_id="delta-below-threshold",
                    display_name="Delta Below Threshold",
                    monthly_token_budget="~20M",
                ),
            ],
        }

        snapshot = build_snapshot(
            payload, min_monthly_tokens=30_000_000, now_iso="2026-07-19T00:00:00Z"
        )

        provider_ids = [provider.id for provider in snapshot.providers]
        self.assertEqual(provider_ids, ["alpha"])
        self.assertEqual(len(snapshot.providers[0].models), 1)
        self.assertEqual(snapshot.providers[0].models[0].model_id, "alpha-ok")

    def test_sorts_providers_by_max_floor_then_name_and_models_by_floor_then_name(self):
        payload = {
            "version": "v1",
            "generatedAt": "2026-07-18T00:00:00Z",
            "platforms": [
                _platform("alpha", "Alpha Provider"),
                _platform("beta", "Beta Provider"),
                _platform("same-a", "Same Floor A"),
                _platform("same-b", "Same Floor B"),
            ],
            "models": [
                # alpha: two models tied at 40M -> tie-break by display name.
                _model(
                    platform="alpha",
                    model_id="zeta-1",
                    display_name="Zeta Model",
                    monthly_token_budget="~40M",
                ),
                _model(
                    platform="alpha",
                    model_id="alpha-1",
                    display_name="Alpha Model",
                    monthly_token_budget="~40M",
                ),
                # beta: single model at 90M -> highest max floor, sorts first.
                _model(
                    platform="beta",
                    model_id="beta-1",
                    display_name="Beta Model",
                    monthly_token_budget="~90M",
                ),
                # same-a/same-b: tied provider max floor -> tie-break by name.
                _model(
                    platform="same-b",
                    model_id="same-b-1",
                    display_name="Same B Model",
                    monthly_token_budget="~50M",
                ),
                _model(
                    platform="same-a",
                    model_id="same-a-1",
                    display_name="Same A Model",
                    monthly_token_budget="~50M",
                ),
            ],
        }

        snapshot = build_snapshot(
            payload, min_monthly_tokens=30_000_000, now_iso="2026-07-19T00:00:00Z"
        )

        provider_ids = [provider.id for provider in snapshot.providers]
        self.assertEqual(provider_ids, ["beta", "same-a", "same-b", "alpha"])

        alpha_provider = next(p for p in snapshot.providers if p.id == "alpha")
        alpha_model_ids = [model.model_id for model in alpha_provider.models]
        self.assertEqual(alpha_model_ids, ["alpha-1", "zeta-1"])

    def test_carries_through_all_model_fields(self):
        payload = {
            "version": "v1",
            "generatedAt": "2026-07-18T00:00:00Z",
            "platforms": [_platform("cloudflare", "Cloudflare Workers AI")],
            "models": [
                _model(
                    platform="cloudflare",
                    model_id="@cf/openai/gpt-oss-120b",
                    display_name="GPT-OSS 120B (CF)",
                    monthly_token_budget="~18-45M",
                    limits={"rpm": 5, "rpd": None, "tpm": 30000, "tpd": 1000000},
                    context_window=131072,
                    supports_vision=False,
                    supports_tools=True,
                    intelligence_rank=6,
                    speed_rank=11,
                    size_label="Large",
                    quirks=[
                        {
                            "slug": "cloudflare-key-format",
                            "title": "Key is account_id:token",
                            "body": "Combined credential.",
                            "severity": "info",
                        }
                    ],
                )
            ],
        }

        snapshot = build_snapshot(
            payload, min_monthly_tokens=18_000_000, now_iso="2026-07-19T00:00:00Z"
        )

        model = snapshot.providers[0].models[0]
        self.assertEqual(model.monthly_token_budget, "~18-45M")
        self.assertEqual(model.monthly_token_budget_floor, 18_000_000)
        self.assertEqual(model.limits.rpm, 5)
        self.assertEqual(model.limits.tpm, 30000)
        self.assertIsNone(model.limits.rpd)
        self.assertEqual(model.context_window, 131072)
        self.assertFalse(model.supports_vision)
        self.assertTrue(model.supports_tools)
        self.assertEqual(model.intelligence_rank, 6)
        self.assertEqual(model.speed_rank, 11)
        self.assertEqual(model.size_label, "Large")
        self.assertEqual(len(model.quirks), 1)
        self.assertEqual(model.quirks[0].slug, "cloudflare-key-format")
        self.assertEqual(model.quirks[0].severity, "info")

        as_dict = snapshot.to_dict()
        self.assertEqual(as_dict["minMonthlyTokens"], 18_000_000)
        self.assertEqual(as_dict["sourceVersion"], "v1")
        provider_dict = as_dict["providers"][0]
        self.assertEqual(provider_dict["id"], "cloudflare")
        model_dict = provider_dict["models"][0]
        self.assertEqual(model_dict["modelId"], "@cf/openai/gpt-oss-120b")
        self.assertEqual(model_dict["monthlyTokenBudgetFloor"], 18_000_000)

    def test_drops_models_whose_enabled_flag_is_truthy_but_not_true(self):
        # The source is external JSON: only a literal `true` may enable a
        # model; truthy junk (1, "true", ...) must not slip through.
        for truthy in (1, "true", "yes", [1]):
            with self.subTest(truthy=truthy):
                truthy_model = _model(
                    platform="alpha",
                    model_id="alpha-truthy",
                    display_name="Alpha Truthy",
                    monthly_token_budget="~100M",
                )
                truthy_model["enabled"] = truthy
                payload = {
                    "version": "v1",
                    "generatedAt": "2026-07-18T00:00:00Z",
                    "platforms": [_platform("alpha", "Alpha Provider")],
                    "models": [
                        _model(
                            platform="alpha",
                            model_id="alpha-strict-true",
                            display_name="Alpha Strict True",
                            monthly_token_budget="~40M",
                        ),
                        truthy_model,
                    ],
                }

                snapshot = build_snapshot(
                    payload,
                    min_monthly_tokens=30_000_000,
                    now_iso="2026-07-19T00:00:00Z",
                )

                model_ids = [
                    model.model_id
                    for provider in snapshot.providers
                    for model in provider.models
                ]
                self.assertEqual(model_ids, ["alpha-strict-true"])

    def test_raises_value_error_on_malformed_payload_shape(self):
        bad_payloads = [
            None,
            [],
            "not a dict",
            {"platforms": [], "models": "not a list"},
            {"platforms": "not a list", "models": []},
            {"models": []},
            {"platforms": []},
        ]
        for payload in bad_payloads:
            with self.subTest(payload=payload):
                with self.assertRaises(ValueError):
                    build_snapshot(
                        payload, min_monthly_tokens=30_000_000, now_iso="2026-07-19T00:00:00Z"
                    )


def _catalog_payload() -> dict:
    return {
        "version": "2026.07.18",
        "generatedAt": "2026-07-18T07:03:02.234Z",
        "platforms": [_platform("cerebras", "Cerebras")],
        "models": [
            _model(
                platform="cerebras",
                model_id="gpt-oss-120b",
                display_name="GPT-OSS 120B",
                monthly_token_budget="~30M",
            )
        ],
    }


class FreeLlmCatalogServiceRefreshTests(unittest.TestCase):
    @staticmethod
    def _client(handler) -> httpx.AsyncClient:
        return httpx.AsyncClient(transport=httpx.MockTransport(handler))

    def test_refresh_once_success_populates_snapshot_and_clears_last_error(self):
        def handler(request: httpx.Request) -> httpx.Response:
            self.assertEqual(str(request.url), FREE_LLM_CATALOG_SOURCE_URL)
            return httpx.Response(200, json=_catalog_payload())

        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)
            async with self._client(handler) as client:
                await service.refresh_once(client)
            status = await service.get_status()
            self.assertIsNotNone(status["updatedAt"])
            self.assertEqual(status["sourceVersion"], "2026.07.18")
            self.assertIsNone(status["lastError"])
            self.assertEqual(len(status["providers"]), 1)
            self.assertEqual(
                status["providers"][0]["models"][0]["modelId"], "gpt-oss-120b"
            )

        run_async(scenario())

    def test_refresh_once_http_status_error_keeps_old_snapshot_and_records_error(self):
        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)

            def ok_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_catalog_payload())

            async with self._client(ok_handler) as client:
                await service.refresh_once(client)
            baseline = await service.get_status()
            self.assertIsNone(baseline["lastError"])

            def failing_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(503, text="service unavailable")

            async with self._client(failing_handler) as client:
                await service.refresh_once(client)
            status = await service.get_status()

            self.assertIsNotNone(status["lastError"])
            self.assertEqual(status["updatedAt"], baseline["updatedAt"])
            self.assertEqual(status["providers"], baseline["providers"])

        run_async(scenario())

    def test_refresh_once_malformed_json_keeps_old_snapshot_and_records_error(self):
        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)

            def ok_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_catalog_payload())

            async with self._client(ok_handler) as client:
                await service.refresh_once(client)
            baseline = await service.get_status()

            def bad_json_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, content=b"not-json{")

            async with self._client(bad_json_handler) as client:
                await service.refresh_once(client)
            status = await service.get_status()

            self.assertIsNotNone(status["lastError"])
            self.assertEqual(status["updatedAt"], baseline["updatedAt"])

        run_async(scenario())

    def test_refresh_once_connect_timeout_keeps_old_snapshot_and_records_error(self):
        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)

            def ok_handler(request: httpx.Request) -> httpx.Response:
                return httpx.Response(200, json=_catalog_payload())

            async with self._client(ok_handler) as client:
                await service.refresh_once(client)
            baseline = await service.get_status()

            def timeout_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectTimeout("simulated connect timeout")

            async with self._client(timeout_handler) as client:
                await service.refresh_once(client)
            status = await service.get_status()

            self.assertIsNotNone(status["lastError"])
            self.assertEqual(status["updatedAt"], baseline["updatedAt"])

        run_async(scenario())

    def test_refresh_once_connect_error_keeps_old_snapshot_and_records_error(self):
        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)

            def connect_error_handler(request: httpx.Request) -> httpx.Response:
                raise httpx.ConnectError("simulated connect error")

            async with self._client(connect_error_handler) as client:
                await service.refresh_once(client)
            status = await service.get_status()

            self.assertIsNotNone(status["lastError"])
            self.assertIsNone(status["updatedAt"])
            self.assertEqual(status["providers"], [])

        run_async(scenario())

    def test_get_status_is_not_blocked_by_an_in_flight_slow_fetch(self):
        # The fetch can hang for up to the 30s timeout; readers of
        # get_status() (the /v1/api/free-models endpoint) must not wait on it.
        async def scenario():
            fetch_started = asyncio.Event()
            release_fetch = asyncio.Event()

            async def slow_handler(request: httpx.Request) -> httpx.Response:
                fetch_started.set()
                await release_fetch.wait()
                return httpx.Response(200, json=_catalog_payload())

            service = FreeLlmCatalogService(min_monthly_tokens=30_000_000)
            async with self._client(slow_handler) as client:
                refresh_task = asyncio.create_task(service.refresh_once(client))
                await asyncio.wait_for(fetch_started.wait(), timeout=5)

                status = await asyncio.wait_for(service.get_status(), timeout=1)
                self.assertIsNone(status["updatedAt"])

                release_fetch.set()
                await asyncio.wait_for(refresh_task, timeout=5)

            status = await service.get_status()
            self.assertIsNotNone(status["updatedAt"])

        run_async(scenario())

    def test_get_status_shape_before_any_refresh(self):
        async def scenario():
            service = FreeLlmCatalogService(min_monthly_tokens=25_000_000)
            status = await service.get_status()
            self.assertEqual(
                status,
                {
                    "updatedAt": None,
                    "sourceVersion": None,
                    "sourceGeneratedAt": None,
                    "lastError": None,
                    "minMonthlyTokens": 25_000_000,
                    "providers": [],
                },
            )

        run_async(scenario())

    def test_service_constructor_requires_no_arguments(self):
        # tests/runtime_test_support.py's default_factories call the class
        # with no arguments; the constructor must accept that.
        service = FreeLlmCatalogService()
        self.assertEqual(service.min_monthly_tokens, 30_000_000)


if __name__ == "__main__":
    unittest.main()
