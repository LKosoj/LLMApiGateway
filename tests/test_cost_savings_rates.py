import unittest
from types import SimpleNamespace

from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.utils.usage_tracking import (
    ModelCostRates,
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

    def test_enrich_tokens_usage_reads_rates_from_config_loader(self):
        request = SimpleNamespace(
            state=SimpleNamespace(
                llmgateway_provider="fallback",
                llmgateway_provider_model="fallback-model",
                llmgateway_gateway_model="gateway-model",
            ),
            app=SimpleNamespace(
                state=SimpleNamespace(
                    config_loader=SimpleNamespace(
                        providers_config={
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
                        },
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
                    ),
                ),
            ),
        )
        usage = {
            "prompt_tokens": 100,
            "completion_tokens": 1000,
            "cost_saved": 0,
        }

        result = enrich_tokens_usage(usage, request)

        self.assertAlmostEqual(result["cost_saved"], 0.0279, places=6)


if __name__ == "__main__":
    unittest.main()
