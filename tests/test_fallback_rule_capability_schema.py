import unittest

from pydantic import ValidationError

from llm_gateway_core.config.loader import FallbackModelRule


class FallbackRuleCapabilitySchemaTests(unittest.TestCase):
    def test_context_window_must_be_positive(self):
        with self.assertRaises(ValidationError) as ctx:
            FallbackModelRule(provider="a", model="model-a", context_window=0)
        self.assertIn("'context_window' must be greater than 0.", str(ctx.exception))

        with self.assertRaises(ValidationError):
            FallbackModelRule(provider="a", model="model-a", context_window=-1)

    def test_context_window_none_is_accepted(self):
        rule = FallbackModelRule(provider="a", model="model-a", context_window=None)
        self.assertIsNone(rule.context_window)

        rule_without_field = FallbackModelRule(provider="a", model="model-a")
        self.assertIsNone(rule_without_field.context_window)

    def test_supports_vision_and_tools_default_to_none(self):
        rule = FallbackModelRule(provider="a", model="model-a")
        self.assertIsNone(rule.supports_vision)
        self.assertIsNone(rule.supports_tools)

        rule_with_values = FallbackModelRule(
            provider="a",
            model="model-a",
            supports_vision=True,
            supports_tools=False,
            context_window=128000,
        )
        self.assertTrue(rule_with_values.supports_vision)
        self.assertFalse(rule_with_values.supports_tools)
        self.assertEqual(rule_with_values.context_window, 128000)

    def test_capabilities_autofilled_accepts_only_known_field_names(self):
        rule = FallbackModelRule(
            provider="a",
            model="model-a",
            capabilities_autofilled=["supports_vision", "context_window"],
        )
        self.assertEqual(rule.capabilities_autofilled, ["supports_vision", "context_window"])

        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="a",
                model="model-a",
                capabilities_autofilled=["not_a_real_field"],
            )

    def test_capabilities_autofilled_none_round_trips_excluded(self):
        rule = FallbackModelRule(provider="a", model="model-a")
        self.assertIsNone(rule.capabilities_autofilled)
        dumped = rule.model_dump(exclude_none=True)
        self.assertNotIn("capabilities_autofilled", dumped)

    def test_capabilities_autofilled_with_values_round_trips_exactly(self):
        rule = FallbackModelRule(
            provider="a",
            model="model-a",
            supports_tools=True,
            context_window=32000,
            capabilities_autofilled=["supports_tools", "context_window"],
        )
        dumped = rule.model_dump(exclude_none=True)
        self.assertEqual(dumped["capabilities_autofilled"], ["supports_tools", "context_window"])

    def test_legacy_config_without_capability_fields_round_trips_unchanged(self):
        legacy_item = {
            "provider": "openrouter",
            "model": "some/model",
            "use_provider_order_as_fallback": False,
        }

        dumped = FallbackModelRule(**legacy_item).model_dump(exclude_none=True)

        self.assertNotIn("supports_vision", dumped)
        self.assertNotIn("supports_tools", dumped)
        self.assertNotIn("context_window", dumped)
        self.assertEqual(
            dumped,
            {
                "provider": "openrouter",
                "model": "some/model",
                "use_provider_order_as_fallback": False,
                "custom_body_params": {},
                "custom_headers": {},
            },
        )


if __name__ == "__main__":
    unittest.main()
