import unittest
from types import MappingProxyType, SimpleNamespace

from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.utils.usage_tracking import (
    ModelCostRates,
    RATE_BASED_COST_SKIP_KEY,
    UPSTREAM_COST_PRESENT_KEY,
    _estimate_cost_saved,
    build_model_cost_rate_registry,
    enrich_tokens_usage,
    extract_tokens_usage,
)


class CostSavingsRatesTests(unittest.TestCase):
    def test_estimate_cost_saved_uses_separate_input_output_rates(self):
        saved = _estimate_cost_saved(
            prompt_tokens=100,
            completion_tokens=1000,
            primary_rate=ModelCostRates(input_rate=10, output_rate=30),
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )

        self.assertAlmostEqual(saved, 0.0279, places=6)

    def test_estimate_cost_saved_returns_none_for_unknown_rate(self):
        saved = _estimate_cost_saved(
            prompt_tokens=100,
            completion_tokens=1000,
            primary_rate=None,
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )

        self.assertIsNone(saved)

    def test_extract_tokens_usage_uses_provider_model_rate_registry(self):
        registry = build_model_cost_rate_registry({
            "primary": ProviderDetails(
                baseUrl="https://primary.example",
                apikey="PRIMARY_KEY",
                models={
                    "primary-model": {
                        "input_rate": 10,
                        "output_rate": 30,
                    },
                },
            ),
            "fallback": ProviderDetails(
                baseUrl="https://fallback.example",
                apikey="FALLBACK_KEY",
                models={
                    "fallback-model": {
                        "input_rate": 1,
                        "output_rate": 3,
                    },
                },
            ),
        })
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 1000,
                "total_tokens": 1100,
            },
            "provider": "fallback",
            "model": "fallback-model",
        }

        result = extract_tokens_usage(
            payload,
            cost_rate_registry=registry,
            primary_provider="primary",
            primary_model="primary-model",
            fallback_provider="fallback",
            fallback_model="fallback-model",
        )

        self.assertAlmostEqual(result["cost_saved"], 0.0279, places=6)

    def test_extract_tokens_usage_marks_unknown_rate_as_none(self):
        registry = build_model_cost_rate_registry({
            "fallback": ProviderDetails(
                baseUrl="https://fallback.example",
                apikey="FALLBACK_KEY",
                models={
                    "fallback-model": {
                        "input_rate": 1,
                        "output_rate": 3,
                    },
                },
            ),
        })
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 1000,
                "total_tokens": 1100,
            },
            "provider": "fallback",
            "model": "fallback-model",
        }

        result = extract_tokens_usage(
            payload,
            cost_rate_registry=registry,
            primary_provider="primary",
            primary_model="unknown-model",
        )

        self.assertIsNone(result["cost_saved"])

    def test_enrich_tokens_usage_uses_explicit_runtime_dependencies(self):
        config_loader = SimpleNamespace(
            fallback_rules={
                "gateway-model": {
                    "fallback_models": [
                        {
                            "provider": "primary",
                            "model": "primary-model",
                        },
                        {
                            "provider": "fallback",
                            "model": "fallback-model",
                        },
                    ],
                },
            },
        )
        registry = MappingProxyType(
            {
                ("primary", "primary-model"): ModelCostRates(10, 30),
                ("fallback", "fallback-model"): ModelCostRates(1, 3),
            }
        )
        request = SimpleNamespace(
            state=SimpleNamespace(
                llmgateway_provider="fallback",
                llmgateway_provider_model="fallback-model",
                llmgateway_gateway_model="gateway-model",
            ),
        )
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 1000,
            "cost_saved": 0,
        }

        result = enrich_tokens_usage(
            usage,
            request,
            config_loader=config_loader,
            cost_rate_registry=registry,
        )

        self.assertAlmostEqual(result["cost_saved"], 0.0279, places=6)
        self.assertAlmostEqual(result["cost"], 0.0031, places=6)

    def test_enrichment_preserves_valid_upstream_positive_and_zero_cost(self):
        config_loader = SimpleNamespace(fallback_rules={})
        registry = MappingProxyType(
            {("trusted", "trusted-model"): ModelCostRates(1000, 2000)}
        )
        request = SimpleNamespace(
            state=SimpleNamespace(
                llmgateway_provider="trusted",
                llmgateway_provider_model="trusted-model",
            )
        )

        for upstream_cost in (0, 0.125):
            with self.subTest(upstream_cost=upstream_cost):
                extracted = extract_tokens_usage(
                    {
                        "usage": {
                            "prompt_tokens": 2,
                            "completion_tokens": 3,
                            "cost": upstream_cost,
                        }
                    }
                )
                result = enrich_tokens_usage(
                    extracted,
                    request,
                    config_loader=config_loader,
                    cost_rate_registry=registry,
                )

                self.assertEqual(result["cost"], upstream_cost)
                self.assertTrue(result[UPSTREAM_COST_PRESENT_KEY])

    def test_invalid_present_upstream_cost_does_not_trigger_local_pricing(self):
        config_loader = SimpleNamespace(fallback_rules={})
        registry = MappingProxyType(
            {("trusted", "trusted-model"): ModelCostRates(1000, 2000)}
        )
        request = SimpleNamespace(
            state=SimpleNamespace(
                llmgateway_provider="trusted",
                llmgateway_provider_model="trusted-model",
            )
        )
        extracted = extract_tokens_usage(
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "cost": "invalid",
                }
            }
        )

        result = enrich_tokens_usage(
            extracted,
            request,
            config_loader=config_loader,
            cost_rate_registry=registry,
        )

        self.assertEqual(result["cost"], 0)
        self.assertTrue(result[RATE_BASED_COST_SKIP_KEY])
        self.assertNotIn(UPSTREAM_COST_PRESENT_KEY, result)

    def test_payload_provider_and_model_cannot_select_local_rates(self):
        config_loader = SimpleNamespace(fallback_rules={})
        registry = MappingProxyType(
            {
                ("trusted", "trusted-model"): ModelCostRates(1000, 2000),
                ("spoofed", "expensive-model"): ModelCostRates(900_000, 900_000),
            }
        )
        request = SimpleNamespace(
            state=SimpleNamespace(
                llmgateway_provider="trusted",
                llmgateway_provider_model="trusted-model",
            )
        )
        extracted = extract_tokens_usage(
            {
                "usage": {"prompt_tokens": 2, "completion_tokens": 3},
                "provider": "spoofed",
                "model": "expensive-model",
            }
        )

        result = enrich_tokens_usage(
            extracted,
            request,
            config_loader=config_loader,
            cost_rate_registry=registry,
        )

        self.assertEqual(result["provider"], "trusted")
        self.assertEqual(result["model"], "trusted-model")
        self.assertAlmostEqual(result["cost"], 0.008, places=6)


if __name__ == "__main__":
    unittest.main()
