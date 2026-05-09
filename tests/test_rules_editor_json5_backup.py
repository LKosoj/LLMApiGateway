"""Structured save must back up JSON5 files that contain comments.

Structured POST endpoints re-serialize via json.dumps, which cannot keep `//`
or `/* */` comments. To avoid silent data loss, the server snapshots the
previous file as `<name>.bak.<timestamp>` whenever comments are detected.
"""
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.rules_editor import _json5_has_comments
from llm_gateway_core.config.loader import ConfigLoader


class Json5CommentDetectionTests(unittest.TestCase):
    def test_line_comment(self):
        self.assertTrue(_json5_has_comments('{"a": 1} // trailing'))

    def test_block_comment(self):
        self.assertTrue(_json5_has_comments('/* header */ {"a": 1}'))

    def test_no_comment(self):
        self.assertFalse(_json5_has_comments('{"a": 1, "b": [2, 3]}'))

    def test_url_inside_string_is_not_a_comment(self):
        # A `//` inside a JSON string must not be classified as a comment.
        self.assertFalse(_json5_has_comments('{"url": "https://example.com"}'))


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
            stack.enter_context(
                patch("llm_gateway_core.api.v1.rules_editor._validate_provider_models", new=AsyncMock())
            )
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
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
