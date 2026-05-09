import unittest

from pydantic import ValidationError

from llm_gateway_core.config.loader import ConfigLoader, FallbackModelRule, ProviderDetails


class FallbackRuleProvidersOrderTests(unittest.TestCase):
    def test_duplicate_providers_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="a",
                model="model-a",
                providers_order=["a", "a"],
            )

    def test_empty_provider_name_in_providers_order_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="a",
                model="model-a",
                providers_order=[""],
            )

    def test_empty_providers_order_list_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="a",
                model="model-a",
                providers_order=[],
            )

    def test_openrouter_subprovider_names_in_providers_order_are_accepted(self):
        config_loader = ConfigLoader()
        providers_config = {
            "a": ProviderDetails(baseUrl="https://a.example", apikey="DIRECT-KEY"),
        }
        fallback_rules = {
            "gateway/model": {
                "fallback_models": [
                    {
                        "provider": "a",
                        "model": "model-a",
                        "providers_order": ["Chutes", "Targon"],
                    }
                ]
            }
        }

        config_loader.validate_fallback_rules_mapping(
            fallback_rules,
            providers_config=providers_config,
        )

    def test_known_providers_order_is_accepted(self):
        config_loader = ConfigLoader()
        providers_config = {
            "a": ProviderDetails(baseUrl="https://a.example", apikey="DIRECT-KEY"),
            "b": ProviderDetails(baseUrl="https://b.example", apikey="DIRECT-KEY"),
        }
        fallback_rules = {
            "gateway/model": {
                "fallback_models": [
                    {
                        "provider": "a",
                        "model": "model-a",
                        "providers_order": ["b", "a"],
                    }
                ]
            }
        }

        config_loader.validate_fallback_rules_mapping(
            fallback_rules,
            providers_config=providers_config,
        )


if __name__ == "__main__":
    unittest.main()
