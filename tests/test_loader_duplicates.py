import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main
from llm_gateway_core.config.loader import ConfigError, ConfigLoader


class LoaderDuplicateValidationTests(unittest.TestCase):
    def test_load_providers_rejects_duplicate_provider_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            rules_path = Path(temp_dir) / "models_fallback_rules.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter-1.example",
                      "apikey": "DIRECT-KEY"
                    }
                  },
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter-2.example",
                      "apikey": "DIRECT-KEY"
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            rules_path.write_text("[]", encoding="utf-8")

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(rules_path),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with self.assertLogs(level="ERROR") as captured_logs:
                    with self.assertRaises(ConfigError):
                        config_loader.load_providers()

        combined_logs = "\n".join(captured_logs.output)
        self.assertIn("Duplicate provider 'openrouter'", combined_logs)
        self.assertEqual(config_loader.providers_config, {})

    def test_load_fallback_rules_rejects_duplicate_gateway_model_names(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            rules_path = Path(temp_dir) / "models_fallback_rules.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.example",
                      "apikey": "DIRECT-KEY"
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )
            rules_path.write_text(
                """
                [
                  {
                    "gateway_model_name": "gateway-model",
                    "fallback_models": [
                      {
                        "provider": "openrouter",
                        "model": "provider-model-1"
                      }
                    ],
                    "rotate_models": false
                  },
                  {
                    "gateway_model_name": "gateway-model",
                    "fallback_models": [
                      {
                        "provider": "openrouter",
                        "model": "provider-model-2"
                      }
                    ],
                    "rotate_models": false
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(
                providers_filename=str(providers_path),
                fallback_rules_filename=str(rules_path),
            )

            with patch.object(main.settings, "fallback_provider", "openrouter"):
                config_loader.load_providers()
                with self.assertLogs(level="ERROR") as captured_logs:
                    with self.assertRaises(ConfigError):
                        config_loader.load_fallback_rules()

        combined_logs = "\n".join(captured_logs.output)
        self.assertIn("Duplicate gateway_model_name 'gateway-model'", combined_logs)
        self.assertEqual(config_loader.fallback_rules, {})


if __name__ == "__main__":
    unittest.main()
