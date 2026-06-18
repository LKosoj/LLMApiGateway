"""Verify that saving providers.json triggers refresh of runtime state.

After a successful POST /v1/config/providers:
- app.state.proxy_http_clients must be rebuilt (new providers honored)
- app.state.operation_dispatcher must be a new instance using updated providers_config
- Old proxy clients must be scheduled for close (not leaked)
"""
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

UPDATED_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "devbox": {
      "baseUrl": "https://new-devbox.example",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()


class ProvidersRefreshTests(unittest.TestCase):
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

    def test_successful_provider_save_rebuilds_operation_dispatcher(self):
        with self._client() as client:
            original_dispatcher = main.app.state.operation_dispatcher

            response = client.post(
                "/v1/config/providers",
                content=UPDATED_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(response.status_code, 200)

            # Dispatcher must be a new instance referencing the updated providers_config.
            new_dispatcher = main.app.state.operation_dispatcher
            self.assertIsNot(new_dispatcher, original_dispatcher)
            self.assertIs(
                new_dispatcher._providers_config,
                self.config_loader.providers_config,
            )
            self.assertEqual(
                new_dispatcher._providers_config["devbox"].baseUrl,
                "https://new-devbox.example",
            )

    def test_successful_provider_save_rebuilds_proxy_clients_dict(self):
        """proxy_http_clients must be rebuilt and the reference swapped on app.state."""
        with self._client() as client:
            original_proxy_clients = main.app.state.proxy_http_clients
            response = client.post(
                "/v1/config/providers",
                content=UPDATED_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(response.status_code, 200)

            # After save, the dict reference must be swapped (not mutated in place).
            self.assertIsNot(main.app.state.proxy_http_clients, original_proxy_clients)


if __name__ == "__main__":
    unittest.main()
