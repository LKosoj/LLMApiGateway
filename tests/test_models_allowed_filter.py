"""Verify that /v1/models and /v1/models/{id} honour a virtual key's allowed_models."""

from __future__ import annotations

import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from tests.chat_accounting_test_support import install_main_chat_accounting_double


PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway-model-allowed",
    "fallback_models": [
      {"provider": "openrouter", "model": "upstream-a"}
    ],
    "rotate_models": false
  },
  {
    "gateway_model_name": "gateway-model-forbidden",
    "fallback_models": [
      {"provider": "openrouter", "model": "upstream-b"}
    ],
    "rotate_models": false
  }
]
""".strip()


class _EmptyFallbackResponse:
    status_code = 200
    text = '{"data":[]}'

    def json(self):
        return {"data": []}


class ModelsAllowedFilterTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)

        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.model_rules_path = Path(self.temp_dir.name) / "models_model_rules.json"
        self.providers_path.write_text(PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.model_rules_path.write_text("{}\n", encoding="utf-8")

        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(Path(self.temp_dir.name) / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.api_keys_db = ApiKeysDB(db_filename="test_allowed_filter.db")

        fallback_patch = patch.object(main.settings, "fallback_provider", "openrouter")
        fallback_patch.start()
        self.addCleanup(fallback_patch.stop)

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            model_rules_filename=str(self.model_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_model_rules()

    @contextmanager
    def _client(self, fake_http_client: Mock | None = None):
        fake_http_client = fake_http_client or Mock()
        if "get" not in fake_http_client.__dict__:
            fake_http_client.get = AsyncMock(return_value=_EmptyFallbackResponse())
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
            install_main_chat_accounting_double(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("main.create_shared_http_client", return_value=fake_http_client)
            )
            stack.enter_context(
                patch(
                    "main.ConfigUpdateCoordinator",
                    return_value=config_update_coordinator,
                )
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.ApiKeysDB", return_value=self.api_keys_db))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-master"))

            with TestClient(main.app) as client:
                yield client

    def test_virtual_key_sees_only_allowed_models(self):
        record = self.api_keys_db.create(
            name="team-restricted", allowed_models=["gateway-model-allowed"]
        )
        with self._client() as client:
            response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {record.api_key}"},
            )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(ids, {"gateway-model-allowed"})

    def test_virtual_key_without_allowed_models_sees_all(self):
        record = self.api_keys_db.create(name="team-unrestricted")
        with self._client() as client:
            response = client.get(
                "/v1/models",
                headers={"Authorization": f"Bearer {record.api_key}"},
            )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("gateway-model-allowed", ids)
        self.assertIn("gateway-model-forbidden", ids)

    def test_master_key_sees_all_models(self):
        with self._client() as client:
            response = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer test-master"},
            )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("gateway-model-allowed", ids)
        self.assertIn("gateway-model-forbidden", ids)

    def test_get_single_model_rejects_disallowed_for_virtual_key(self):
        record = self.api_keys_db.create(
            name="team-restricted-single", allowed_models=["gateway-model-allowed"]
        )
        with self._client() as client:
            response = client.get(
                "/v1/models/gateway-model-forbidden",
                headers={"Authorization": f"Bearer {record.api_key}"},
            )

        self.assertEqual(response.status_code, 403)

    def test_get_single_model_rejects_excluded_before_fallback_provider_lookup(self):
        self.model_rules_path.write_text(
            '{"excluded_models": ["blocked/*"]}\n',
            encoding="utf-8",
        )
        self.config_loader.load_model_rules()
        fake_http_client = Mock()
        fake_http_client.get = AsyncMock(return_value=_EmptyFallbackResponse())
        fake_http_client.aclose = AsyncMock()

        with self._client(fake_http_client) as client:
            fake_http_client.get.reset_mock()
            response = client.get(
                "/v1/models/blocked/model",
                headers={"Authorization": "Bearer test-master"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("excluded", response.json()["detail"])
        self.assertEqual(fake_http_client.get.await_count, 0)

    def test_anthropic_count_tokens_rejects_disallowed_model_for_virtual_key(self):
        record = self.api_keys_db.create(
            name="team-restricted-count", allowed_models=["gateway-model-allowed"]
        )
        with self._client() as client:
            response = client.post(
                "/v1/messages/count_tokens",
                json={
                    "model": "gateway-model-forbidden",
                    "messages": [{"role": "user", "content": "Count these tokens"}],
                },
                headers={"Authorization": f"Bearer {record.api_key}"},
            )

        self.assertEqual(response.status_code, 403)


if __name__ == "__main__":
    unittest.main()
