import tempfile
import unittest
import os
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.rules_editor import _write_text_atomically
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


class RulesEditorValidationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
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

    def test_invalid_providers_payload_returns_400_and_keeps_disk_file(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")

        with self._client() as client:
            response = client.post(
                "/v1/config/providers",
                content='[{ "devbox": ',
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)

    def test_unknown_provider_in_rules_returns_400_and_keeps_disk_file(self):
        original_file_content = self.rules_path.read_text(encoding="utf-8")
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
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_file_content)

    def test_empty_fallback_list_returns_400_and_keeps_disk_file(self):
        original_file_content = self.rules_path.read_text(encoding="utf-8")
        invalid_rules_text = """
        [
          {
            "gateway_model_name": "gateway-model",
            "fallback_models": [],
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
        self.assertIn("at least one fallback model", response.json()["detail"])
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_file_content)

    def test_duplicate_provider_returns_400_and_keeps_disk_and_runtime_config(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")
        original_provider_base_url = self.config_loader.providers_config["openrouter"].baseUrl
        duplicate_providers_text = """
        [
          {
            "openrouter": {
              "baseUrl": "https://openrouter.example",
              "apikey": "DIRECT-KEY"
            }
          },
          {
            "openrouter": {
              "baseUrl": "https://duplicate.example",
              "apikey": "DIRECT-KEY"
            }
          }
        ]
        """.strip()

        with self._client() as client:
            response = client.post(
                "/v1/config/providers",
                content=duplicate_providers_text,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate provider 'openrouter'", response.json()["detail"])
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)
        self.assertEqual(
            self.config_loader.providers_config["openrouter"].baseUrl,
            original_provider_base_url,
        )

    def test_duplicate_gateway_model_name_returns_400_and_keeps_disk_and_runtime_config(self):
        original_file_content = self.rules_path.read_text(encoding="utf-8")
        original_runtime_rule = self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"]
        duplicate_rules_text = """
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
          },
          {
            "gateway_model_name": "gateway-model",
            "fallback_models": [
              {
                "provider": "openrouter",
                "model": "provider-model-duplicate"
              }
            ],
            "rotate_models": false
          }
        ]
        """.strip()

        with self._client() as client:
            response = client.post(
                "/v1/config/models-rules",
                content=duplicate_rules_text,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Duplicate gateway_model_name 'gateway-model'", response.json()["detail"])
        self.assertEqual(self.rules_path.read_text(encoding="utf-8"), original_file_content)
        self.assertEqual(
            self.config_loader.fallback_rules["gateway-model"]["fallback_models"][0]["model"],
            original_runtime_rule,
        )


class RulesEditorAtomicWriteTests(unittest.TestCase):
    def test_write_text_atomically_preserves_existing_file_mode(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "providers.json"
            target_path.write_text('[{"old": true}]\n', encoding="utf-8")
            os.chmod(target_path, 0o640)

            _write_text_atomically(target_path, '[{"new": true}]\n')

            self.assertEqual(target_path.read_text(encoding="utf-8"), '[{"new": true}]\n')
            self.assertEqual(target_path.stat().st_mode & 0o777, 0o640)

    def test_write_text_atomically_removes_temp_file_after_fsync_error(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            target_path = Path(temp_dir) / "providers.json"
            target_path.write_text('[{"old": true}]\n', encoding="utf-8")

            with patch("llm_gateway_core.api.v1.rules_editor.os.fsync", side_effect=OSError("boom")):
                with self.assertRaises(OSError):
                    _write_text_atomically(target_path, '[{"new": true}]\n')

            self.assertEqual(target_path.read_text(encoding="utf-8"), '[{"old": true}]\n')
            self.assertEqual([path.name for path in Path(temp_dir).iterdir()], ["providers.json"])


if __name__ == "__main__":
    unittest.main()
