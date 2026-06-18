import unittest
import tempfile
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

import main
from tests._async_compat import run_async
from llm_gateway_core.config.loader import ConfigError, ConfigLoader


class StartupFailClosedTests(unittest.TestCase):
    @patch.object(main.settings, "gateway_api_key", "")
    def test_startup_raises_when_gateway_api_key_is_empty(self):
        with self.assertRaises(RuntimeError) as context:
            with TestClient(main.app):
                pass

        self.assertIn("GATEWAY_API_KEY", str(context.exception))

    @patch.object(main.settings, "gateway_api_key", "your-secure-api-key")
    def test_startup_raises_when_gateway_api_key_is_placeholder(self):
        with self.assertRaises(RuntimeError) as context:
            with TestClient(main.app):
                pass

        self.assertIn("placeholder", str(context.exception))

    @patch.object(main.settings, "gateway_api_key", "test-gateway-key")
    @patch.object(main.settings, "fallback_provider", "openrouter")
    def test_startup_raises_when_fallback_provider_is_missing_from_providers(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            rules_path = Path(temp_dir) / "models_fallback_rules.json"
            providers_path.write_text(
                """
                [
                  {
                    "devbox": {
                      "baseUrl": "https://devbox.example",
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

            with patch("main.ConfigLoader", return_value=config_loader):
                with self.assertLogs(level="ERROR") as captured_logs:
                    with self.assertRaises(ConfigError):
                        run_async(main.lifespan(main.app).__aenter__())

        combined_logs = "\n".join(captured_logs.output)
        self.assertIn("FALLBACK_PROVIDER", combined_logs)
        self.assertIn("openrouter", combined_logs)
        self.assertIn("Available providers: devbox", combined_logs)

    @patch.object(main.settings, "fallback_provider", "openrouter")
    def test_provider_literal_placeholder_api_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.ai/api/v1",
                      "apikey": "your-openrouter-api-key"
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(providers_filename=str(providers_path))

            with self.assertRaises(ConfigError) as context:
                config_loader.load_providers()

        self.assertIn("placeholder", str(context.exception))

    @patch.object(main.settings, "fallback_provider", "openrouter")
    @patch.dict("os.environ", {"OPENROUTER_API_KEY": "your-openrouter-api-key"})
    def test_provider_env_placeholder_api_key_is_rejected(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            providers_path = Path(temp_dir) / "providers.json"
            providers_path.write_text(
                """
                [
                  {
                    "openrouter": {
                      "baseUrl": "https://openrouter.ai/api/v1",
                      "apikey": "${OPENROUTER_API_KEY}"
                    }
                  }
                ]
                """.strip(),
                encoding="utf-8",
            )

            config_loader = ConfigLoader(providers_filename=str(providers_path))

            with self.assertRaises(ConfigError) as context:
                config_loader.load_providers()

        self.assertIn("OPENROUTER_API_KEY", str(context.exception))
        self.assertIn("placeholder", str(context.exception))


if __name__ == "__main__":
    unittest.main()
