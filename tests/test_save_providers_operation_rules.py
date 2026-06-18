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
        "provider": "openrouter",
        "model": "provider-model"
      }
    ],
    "rotate_models": false
  }
]
""".strip()

OPERATION_RULES_TEXT = """
{
  "embeddings": [
    {
      "gateway_model_name": "gateway/embed",
      "routes": [
        {
          "provider": "devbox",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [],
  "images_generations": [],
  "images_edits": [],
  "audio_transcriptions": []
}
""".strip()


class SaveProvidersOperationRulesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(OPERATION_RULES_TEXT, encoding="utf-8")
        self.fusion_rules_path.write_text("[]", encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()

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

    def _save_providers(self, client: TestClient, payload_text: str):
        return client.post(
            "/v1/config/providers",
            content=payload_text,
            headers={
                "Authorization": "Bearer test-gateway-key",
                "Content-Type": "text/plain",
            },
        )

    def test_save_providers_rejects_provider_rename_used_by_operation_rules(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")
        renamed_providers_text = VALID_PROVIDERS_TEXT.replace('"devbox"', '"devbox_renamed"')

        with self._client() as client:
            response = self._save_providers(client, renamed_providers_text)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid operation rules provider references", response.json()["detail"])
        self.assertIn("devbox", response.json()["detail"])
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)

    def test_save_providers_rejects_provider_delete_used_by_operation_rules(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")
        deleted_provider_text = """
        [
          {
            "openrouter": {
              "baseUrl": "https://openrouter.example",
              "apikey": "DIRECT-KEY"
            }
          }
        ]
        """.strip()

        with self._client() as client:
            response = self._save_providers(client, deleted_provider_text)

        self.assertEqual(response.status_code, 400)
        self.assertIn("Invalid operation rules provider references", response.json()["detail"])
        self.assertIn("devbox", response.json()["detail"])
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
