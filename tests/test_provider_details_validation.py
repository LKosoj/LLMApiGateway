import unittest

from pydantic import ValidationError

from llm_gateway_core.config.loader import ProviderDetails


class ProviderDetailsValidationTests(unittest.TestCase):
    def test_empty_base_url_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderDetails(baseUrl="", apikey="DIRECT-KEY")

    def test_empty_apikey_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderDetails(baseUrl="http://provider.example", apikey="")

    def test_base_url_without_http_scheme_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderDetails(baseUrl="provider.example", apikey="DIRECT-KEY")

    def test_valid_provider_details_are_accepted(self):
        details = ProviderDetails(baseUrl="https://provider.example", apikey="DIRECT-KEY")

        self.assertEqual(details.baseUrl, "https://provider.example")
        self.assertEqual(details.apikey, "DIRECT-KEY")
        self.assertEqual(details.usage_cost_currency, "USD")
        self.assertIsNone(details.switch_on_leading_ideograph)

    def test_provider_usage_cost_currency_requires_three_uppercase_letters(self):
        provider = {"baseUrl": "https://provider.example", "apikey": "DIRECT-KEY"}
        self.assertEqual(
            ProviderDetails(**provider, usage_cost_currency="RUB", usage_cost_units_per_usd=80).usage_cost_currency, "RUB"
        )
        with self.assertRaises(ValidationError):
            ProviderDetails(**provider, usage_cost_currency="RUB")
        with self.assertRaises(ValidationError):
            ProviderDetails(**provider, usage_cost_currency="rubles")

    def test_switch_on_leading_ideograph_accepts_only_real_booleans(self):
        enabled = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            switch_on_leading_ideograph=True,
        )
        disabled = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            switch_on_leading_ideograph=False,
        )

        self.assertIs(enabled.switch_on_leading_ideograph, True)
        self.assertIsNone(disabled.switch_on_leading_ideograph)
        with self.assertRaises(ValidationError):
            ProviderDetails(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
                switch_on_leading_ideograph="yes",
            )

    def test_custom_headers_user_agent_is_accepted(self):
        details = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            custom_headers={"User-Agent": "Cline/1.0"},
        )

        self.assertEqual(details.custom_headers, {"User-Agent": "Cline/1.0"})

    def test_custom_headers_authorization_is_rejected(self):
        with self.assertRaises(ValidationError):
            ProviderDetails(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
                custom_headers={"Authorization": "Bearer leak"},
            )


if __name__ == "__main__":
    unittest.main()
