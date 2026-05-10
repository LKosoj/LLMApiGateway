import os
import unittest
from unittest.mock import patch

from llm_gateway_core.config.loader import (
    ConfigError,
    ConfigLoader,
    resolve_provider_api_key,
    resolve_provider_proxy,
)
from llm_gateway_core.config.settings import settings


class ResolveEnvSyntaxTests(unittest.TestCase):
    def test_api_key_explicit_env_reference_uses_environment_value(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env-value"}, clear=False):
            self.assertEqual(resolve_provider_api_key("${OPENAI_API_KEY}"), "sk-env-value")

    def test_api_key_explicit_env_reference_uses_round_robin_for_comma_separated_values(self):
        with patch.dict(os.environ, {"OPENROUTER_API_KEY": "env-rr-a, ,env-rr-b"}, clear=False):
            self.assertEqual(resolve_provider_api_key("${OPENROUTER_API_KEY}"), "env-rr-a")
            self.assertEqual(resolve_provider_api_key("${OPENROUTER_API_KEY}"), "env-rr-b")
            self.assertEqual(resolve_provider_api_key("${OPENROUTER_API_KEY}"), "env-rr-a")

    def test_api_key_explicit_env_reference_missing_raises_config_error(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("OPENAI_API_KEY", None)

            with self.assertRaisesRegex(ConfigError, "OPENAI_API_KEY"):
                resolve_provider_api_key("${OPENAI_API_KEY}")

    def test_unbraced_env_like_api_key_is_literal_with_warning(self):
        with patch.dict(os.environ, {"OPENAI_API_KEY": "sk-env-value"}, clear=False):
            with self.assertLogs(level="WARNING") as logs:
                resolved = resolve_provider_api_key("OPENAI_API_KEY")

        self.assertEqual(resolved, "OPENAI_API_KEY")
        self.assertIn("${VAR}", "\n".join(logs.output))

    def test_proxy_explicit_env_reference_uses_environment_value(self):
        with patch.dict(os.environ, {"PROVIDER_PROXY_URL": "socks5://resolved@host:1080"}, clear=False):
            self.assertEqual(
                resolve_provider_proxy("${PROVIDER_PROXY_URL}"),
                "socks5://resolved@host:1080",
            )

    def test_provider_semantic_validation_rejects_missing_explicit_env_reference(self):
        payload = """
        [
          {
            "openrouter": {
              "baseUrl": "https://openrouter.example",
              "apikey": "${MISSING_OPENAI_API_KEY}"
            }
          }
        ]
        """.strip()
        config_loader = ConfigLoader()

        with patch.object(settings, "fallback_provider", "openrouter"):
            with patch.dict(os.environ, {}, clear=False):
                os.environ.pop("MISSING_OPENAI_API_KEY", None)
                with self.assertRaisesRegex(ConfigError, "MISSING_OPENAI_API_KEY"):
                    config_loader.parse_and_validate_providers_payload(payload, strict_env=True)


if __name__ == "__main__":
    unittest.main()
