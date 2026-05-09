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


if __name__ == "__main__":
    unittest.main()
