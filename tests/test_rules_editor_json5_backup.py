"""Structured save backs up exact JSON5 source bytes through the coordinator."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.config.loader import ConfigLoader
from tests.rules_editor_test_support import transactional_rules_editor_client


VALID_PROVIDERS_TEXT_WITH_COMMENTS = """
// Providers config
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY" // production key reference
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

VALID_FALLBACK_RULES_WITH_COMMENTS = """
// Fallback rules
[
  {
    "gateway_model_name": "gateway-model",
    // Primary provider first
    "fallback_models": [
      {"provider": "devbox", "model": "provider-model"}
    ],
    "rotate_models": false
  }
]
""".strip()


class StructuredSaveBackupTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT_WITH_COMMENTS, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_WITH_COMMENTS, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openrouter",
        )
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(Path(self.temp_dir.name) / "models_fusion_rules.json"),
            model_rules_filename=str(Path(self.temp_dir.name) / "models_model_rules.json"),
            router_rules_filename=str(Path(self.temp_dir.name) / "models_router_rules.json"),
        )
        self.config_loader.load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        with transactional_rules_editor_client(self.config_loader) as (
            client,
            _runtime,
        ):
            yield client

    def test_structured_save_creates_backup_when_original_has_comments(self):
        payload = {
            "rules": [
                {
                    "gateway_model_name": "gateway-model",
                    "fallback_models": [
                        {"provider": "devbox", "model": "provider-model"}
                    ],
                    "rotate_models": False,
                }
            ]
        }
        original_text = self.rules_path.read_text(encoding="utf-8")

        with self._client() as client:
            response = client.post(
                "/v1/config/models-rules/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        backup_name = body.get("comments_backup")
        self.assertIsInstance(backup_name, str)
        self.assertTrue(backup_name.startswith("models_fallback_rules.json.bak."))

        backup_path = self.rules_path.parent / backup_name
        self.assertTrue(backup_path.exists())
        self.assertEqual(backup_path.read_text(encoding="utf-8"), original_text)
        self.assertNotIn("//", self.rules_path.read_text(encoding="utf-8").split("\n")[0])


if __name__ == "__main__":
    unittest.main()
