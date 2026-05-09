import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader


VALID_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "devbox": {
      "baseUrl": "https://devbox.example",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

VALID_RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway-model",
    "fallback_models": [
      {
        "provider": "devbox",
        "model": "provider-model"
      }
    ],
    "rotate_models": false
  }
]
""".strip()


class RulesEditorSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
                yield client

    def test_save_models_rules_returns_400_for_unknown_provider(self):
        invalid_rules_text = """
        [
          {
            "gateway_model_name": "gateway-model",
            "fallback_models": [
              {
                "provider": "missing-provider",
                "model": "provider-model"
              }
            ],
            "rotate_models": false
          }
        ]
        """.strip()

        with self._client() as client:
            response = client.post(
                "/v1/config/models-rules",
                content=invalid_rules_text,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("missing-provider", response.json()["detail"])

    def test_save_providers_returns_400_for_invalid_json(self):
        with self._client() as client:
            response = client.post(
                "/v1/config/providers",
                content='[{ "provider-a": ',
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)

    def test_get_providers_structured_returns_provider_cards(self):
        with self._client() as client:
            response = client.get(
                "/v1/config/providers/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        providers = response.json()["providers"]
        self.assertEqual([provider["name"] for provider in providers], ["openrouter", "devbox"])
        self.assertEqual(providers[0]["type"], "openai")
        self.assertEqual(providers[1]["baseUrl"], "https://devbox.example")

    def test_save_providers_structured_updates_file_and_runtime_config(self):
        payload = {
            "providers": [
                {
                    "name": "openrouter",
                    "baseUrl": "https://openrouter.example",
                    "apikey": "DIRECT-KEY",
                    "type": "openai",
                },
                {
                    "name": "devbox",
                    "baseUrl": "https://new-devbox.example",
                    "apikey": "DIRECT-KEY",
                    "type": "openai",
                    "proxy": "http://proxy.example:8080",
                    "models": {"pricing": {"input": 0.1}},
                },
            ]
        }

        with self._client() as client:
            response = client.post(
                "/v1/config/providers/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            self.config_loader.providers_config["devbox"].baseUrl,
            "https://new-devbox.example",
        )
        saved_text = self.providers_path.read_text(encoding="utf-8")
        self.assertIn('"devbox"', saved_text)
        self.assertIn('"proxy": "http://proxy.example:8080"', saved_text)
        self.assertIn('"pricing"', saved_text)

    def test_save_models_rules_does_not_overwrite_file_when_replace_fails(self):
        updated_rules_text = VALID_RULES_TEXT.replace("provider-model", "provider-model-v2")
        original_file_content = self.rules_path.read_text(encoding="utf-8")

        with self._client() as client:
            with patch("llm_gateway_core.api.v1.rules_editor.os.replace", side_effect=OSError("disk full")):
                response = client.post(
                    "/v1/config/models-rules",
                    content=updated_rules_text,
                    headers={
                        "Authorization": "Bearer test-gateway-key",
                        "Content-Type": "text/plain",
                    },
                )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
