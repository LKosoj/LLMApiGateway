"""Tests for startup model availability verification."""

import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock

from llm_gateway_core.services.model_availability import (
    ModelAvailabilityReport,
    ProviderCheckResult,
    _collect_provider_model_pairs,
    log_availability_report,
    run_startup_model_verification,
    verify_configured_models,
)
from tests._async_compat import run_async


class CollectProviderModelPairsTests(unittest.TestCase):
    def test_collects_from_fallback_models(self):
        fallback_rules = {
            "gw-1": {
                "fallback_models": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p2", "model": "m2"},
                    {"provider": "p1", "model": "m3"},
                ],
            },
        }
        pairs = _collect_provider_model_pairs(fallback_rules, None)
        self.assertEqual(pairs, {"p1": {"m1", "m3"}, "p2": {"m2"}})

    def test_includes_context_overflow_fallback(self):
        fallback_rules = {
            "gw-1": {
                "fallback_models": [{"provider": "p1", "model": "m1"}],
                "context_overflow_fallback": {"provider": "p-big", "model": "m-big"},
            },
        }
        pairs = _collect_provider_model_pairs(fallback_rules, None)
        self.assertEqual(pairs, {"p1": {"m1"}, "p-big": {"m-big"}})

    def test_includes_operation_rules(self):
        operation_rules = {
            "embeddings": {
                "gw-embed": {"routes": [{"provider": "p-emb", "model": "m-emb"}]},
            },
            "rerank": {},
        }
        pairs = _collect_provider_model_pairs(None, operation_rules)
        self.assertEqual(pairs, {"p-emb": {"m-emb"}})

    def test_includes_legacy_flat_operation_rules(self):
        operation_rules = {
            "embeddings": {
                "gw-embed": {"provider": "p-emb", "model": "m-emb"},
            },
        }
        pairs = _collect_provider_model_pairs(None, operation_rules)
        self.assertEqual(pairs, {"p-emb": {"m-emb"}})

    def test_ignores_non_string_entries(self):
        fallback_rules = {
            "gw-1": {
                "fallback_models": [
                    {"provider": "p1", "model": None},
                    {"provider": None, "model": "m1"},
                    "not-a-dict",
                    {"provider": "p1", "model": "m1"},
                ],
            },
        }
        pairs = _collect_provider_model_pairs(fallback_rules, None)
        self.assertEqual(pairs, {"p1": {"m1"}})

    def test_handles_empty_inputs(self):
        self.assertEqual(_collect_provider_model_pairs(None, None), {})
        self.assertEqual(_collect_provider_model_pairs({}, {}), {})


class _FakeService:
    def __init__(self, responses):
        # responses: provider -> either list[str] or Exception
        self._responses = responses
        self.calls: list[str] = []

    async def get_models(self, provider_name, provider_config, http_client):
        self.calls.append(provider_name)
        value = self._responses[provider_name]
        if isinstance(value, Exception):
            raise value
        return value


class VerifyConfiguredModelsTests(unittest.TestCase):
    def _build_providers(self, names):
        return {name: SimpleNamespace(baseUrl=f"https://{name}", apikey="K") for name in names}

    def test_reports_ok_when_all_models_present(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1", "m2"]})
        fallback_rules = {"gw": {"fallback_models": [{"provider": "p1", "model": "m1"}]}}

        report = run_async(
            verify_configured_models(
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertEqual(len(report.results), 1)
        self.assertEqual(report.results[0].provider, "p1")
        self.assertEqual(report.results[0].missing_models, [])
        self.assertFalse(report.has_failures)

    def test_reports_missing_models(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1"]})
        fallback_rules = {
            "gw": {
                "fallback_models": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p1", "model": "missing-model"},
                ]
            }
        }

        report = run_async(
            verify_configured_models(
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertEqual(report.results[0].missing_models, ["missing-model"])
        self.assertTrue(report.has_failures)
        self.assertEqual(report.missing_total, 1)

    def test_reports_error_when_provider_fetch_fails(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ValueError("HTTP 500")})
        fallback_rules = {"gw": {"fallback_models": [{"provider": "p1", "model": "m1"}]}}

        report = run_async(
            verify_configured_models(
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertEqual(report.results[0].error, "HTTP 500")
        self.assertTrue(report.has_failures)

    def test_reports_error_when_provider_not_in_config(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({})
        fallback_rules = {
            "gw": {"fallback_models": [{"provider": "missing-provider", "model": "m1"}]}
        }

        report = run_async(
            verify_configured_models(
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertIsNotNone(report.results[0].error)
        self.assertIn("not found", report.results[0].error)

    def test_empty_report_when_no_rules(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({})

        report = run_async(
            verify_configured_models(
                providers_config=providers,
                fallback_rules=None,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertEqual(report.results, [])
        self.assertFalse(report.has_failures)


class RunStartupModelVerificationTests(unittest.TestCase):
    def _build_providers(self, names):
        return {name: SimpleNamespace(baseUrl=f"https://{name}", apikey="K") for name in names}

    def test_off_mode_returns_none(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1"]})

        result = run_async(
            run_startup_model_verification(
                mode="off",
                providers_config=providers,
                fallback_rules={"gw": {"fallback_models": [{"provider": "p1", "model": "m1"}]}},
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertIsNone(result)
        self.assertEqual(service.calls, [])

    def test_warn_mode_returns_report_without_raising(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1"]})  # configured model 'missing' is absent
        fallback_rules = {
            "gw": {"fallback_models": [{"provider": "p1", "model": "missing"}]}
        }

        result = run_async(
            run_startup_model_verification(
                mode="warn",
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertIsInstance(result, ModelAvailabilityReport)
        self.assertTrue(result.has_failures)

    def test_strict_mode_raises_on_missing(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1"]})
        fallback_rules = {
            "gw": {"fallback_models": [{"provider": "p1", "model": "missing"}]}
        }

        with self.assertRaises(RuntimeError) as ctx:
            run_async(
                run_startup_model_verification(
                    mode="strict",
                    providers_config=providers,
                    fallback_rules=fallback_rules,
                    operation_rules=None,
                    provider_models_service=service,
                    http_client=AsyncMock(),
                )
            )
        self.assertIn("verification failed", str(ctx.exception))

    def test_strict_mode_succeeds_when_all_models_present(self):
        providers = self._build_providers(["p1"])
        service = _FakeService({"p1": ["m1", "m2"]})
        fallback_rules = {
            "gw": {"fallback_models": [{"provider": "p1", "model": "m1"}]}
        }

        result = run_async(
            run_startup_model_verification(
                mode="strict",
                providers_config=providers,
                fallback_rules=fallback_rules,
                operation_rules=None,
                provider_models_service=service,
                http_client=AsyncMock(),
            )
        )

        self.assertFalse(result.has_failures)

    def test_invalid_mode_raises(self):
        with self.assertRaises(ValueError):
            run_async(
                run_startup_model_verification(
                    mode="loud",
                    providers_config={},
                    fallback_rules=None,
                    operation_rules=None,
                    provider_models_service=_FakeService({}),
                    http_client=AsyncMock(),
                )
            )


class LogAvailabilityReportTests(unittest.TestCase):
    def test_handles_empty_report(self):
        # Should not raise.
        log_availability_report(ModelAvailabilityReport(results=[]))

    def test_logs_all_result_types(self):
        report = ModelAvailabilityReport(results=[
            ProviderCheckResult(provider="ok", available_models=["a", "b"]),
            ProviderCheckResult(provider="missing", available_models=["x"], missing_models=["y"]),
            ProviderCheckResult(provider="down", error="HTTP 500"),
        ])
        # Should not raise.
        log_availability_report(report)


if __name__ == "__main__":
    unittest.main()
