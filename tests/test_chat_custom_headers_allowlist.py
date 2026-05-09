"""Regression: chat fallback rules must not inject protected headers.

Two layers of defense:
1. Loader rejects custom_headers containing Authorization/Cookie/X-Api-Key
   at config load time (Pydantic validator on FallbackModelRule).
2. Runtime filter in chat.py drops them on attempt even if they slip in
   via mutation or a bypass.
"""
import unittest

from pydantic import ValidationError

from llm_gateway_core.config.loader import FallbackModelRule


class FallbackModelRuleCustomHeadersValidationTests(unittest.TestCase):
    def test_plain_custom_header_is_accepted(self):
        rule = FallbackModelRule(
            provider="p",
            model="m",
            custom_headers={"X-Customer-Id": "42"},
        )
        self.assertEqual(rule.custom_headers, {"X-Customer-Id": "42"})

    def test_authorization_header_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="p",
                model="m",
                custom_headers={"Authorization": "Bearer leak"},
            )

    def test_cookie_header_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="p",
                model="m",
                custom_headers={"cookie": "session=x"},
            )

    def test_x_api_key_header_is_rejected(self):
        with self.assertRaises(ValidationError):
            FallbackModelRule(
                provider="p",
                model="m",
                custom_headers={"X-Api-Key": "leak"},
            )

    def test_case_insensitive_match(self):
        for bad_name in ("AUTHORIZATION", "Authorization", "authorization"):
            with self.assertRaises(ValidationError):
                FallbackModelRule(
                    provider="p",
                    model="m",
                    custom_headers={bad_name: "x"},
                )


if __name__ == "__main__":
    unittest.main()
