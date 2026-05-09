import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.rules_editor import MAX_COMMENT_BACKUPS
from llm_gateway_core.config.loader import ConfigLoader


VALID_PROVIDERS_TEXT_WITH_COMMENTS = """
// Providers config
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


def fallback_rules_with_comment(version: int) -> str:
    return f"""
// Fallback rules version {version}
[
  {{
    "gateway_model_name": "gateway-model",
    "fallback_models": [
      {{"provider": "devbox", "model": "provider-model"}}
    ],
    "rotate_models": false
  }}
]
""".strip()


class RulesEditorBackupVersioningTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT_WITH_COMMENTS, encoding="utf-8")
        self.rules_path.write_text(fallback_rules_with_comment(0), encoding="utf-8")
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

    def test_structured_save_rotates_versioned_backups(self):
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
        timestamps = [
            f"2026-04-23T00:00:{index:02d}.000000Z"
            for index in range(MAX_COMMENT_BACKUPS + 1)
        ]

        with self._client() as client, patch(
            "llm_gateway_core.api.v1.rules_editor._backup_timestamp_utc",
            side_effect=timestamps,
        ):
            for index in range(MAX_COMMENT_BACKUPS + 1):
                self.rules_path.write_text(fallback_rules_with_comment(index), encoding="utf-8")
                response = client.post(
                    "/v1/config/models-rules/structured",
                    json=payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                self.assertEqual(response.status_code, 200, response.text)

        backup_names = sorted(
            path.name
            for path in self.rules_path.parent.iterdir()
            if path.name.startswith("models_fallback_rules.json.bak.")
        )
        expected_names = [
            f"models_fallback_rules.json.bak.{timestamp}"
            for timestamp in timestamps[1:]
        ]
        self.assertEqual(backup_names, expected_names)
        self.assertEqual(len(backup_names), MAX_COMMENT_BACKUPS)
        self.assertEqual(len(set(backup_names)), MAX_COMMENT_BACKUPS)


if __name__ == "__main__":
    unittest.main()
