"""Provider saves publish a complete fresh runtime generation."""
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.config.loader import ConfigLoader
from tests.rules_editor_test_support import transactional_rules_editor_client


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

UPDATED_PROXY_PROVIDERS_TEXT = UPDATED_PROVIDERS_TEXT.replace(
    '"apikey": "DIRECT-KEY"\n    }\n  }\n]',
    '"apikey": "DIRECT-KEY",\n      "proxy": "http://proxy.example:8080"\n    }\n  }\n]',
)

INVALID_PROXY_PROVIDERS_TEXT = """
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
      "apikey": "DIRECT-KEY",
      "proxy": "http://user:super-secret@"
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
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openrouter",
        )
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            model_rules_filename=str(Path(self.temp_dir.name) / "models_model_rules.json"),
            router_rules_filename=str(Path(self.temp_dir.name) / "models_router_rules.json"),
        )
        self.config_loader.load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        with transactional_rules_editor_client(self.config_loader) as result:
            yield result

    def test_successful_provider_save_rebuilds_operation_dispatcher(self):
        with self._client() as (client, runtime):
            original_dispatcher = runtime.initial_snapshot.operation_dispatcher

            response = client.post(
                "/v1/config/providers",
                content=UPDATED_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(response.status_code, 200)

            generation_response = client.get("/_test/runtime-generation")
            self.assertEqual(generation_response.json(), {"generation": 2})
            published = runtime.observed_snapshot
            new_dispatcher = published.operation_dispatcher
            self.assertIsNot(new_dispatcher, original_dispatcher)
            self.assertIs(
                new_dispatcher._providers_config,
                published.config_loader.providers_config,
            )
            self.assertEqual(
                new_dispatcher._providers_config["devbox"].baseUrl,
                "https://new-devbox.example",
            )
            self.assertEqual(
                runtime.initial_snapshot.config_loader.providers_config[
                    "devbox"
                ].baseUrl,
                "https://devbox.example",
            )

    def test_successful_provider_save_rebuilds_proxy_clients_dict(self):
        """proxy_http_clients must be rebuilt and the reference swapped on app.state."""
        with self._client() as (client, runtime):
            original_proxy_clients = runtime.initial_snapshot.proxy_http_clients
            response = client.post(
                "/v1/config/providers",
                content=UPDATED_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )
            self.assertEqual(response.status_code, 200)

            client.get("/_test/runtime-generation")
            self.assertIsNot(
                runtime.observed_snapshot.proxy_http_clients,
                original_proxy_clients,
            )

    def test_raw_save_rejects_invalid_proxy_without_changing_disk_or_runtime(self):
        original_bytes = self.providers_path.read_bytes()

        with self._client() as (client, runtime):
            original_config = self.config_loader.providers_config
            original_snapshot = runtime.initial_snapshot
            response = client.post(
                "/v1/config/providers",
                content=INVALID_PROXY_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("super-secret", response.text)
            self.assertEqual(
                response.json()["detail"]["code"],
                "config_validation_failed",
            )
            self.assertEqual(self.providers_path.read_bytes(), original_bytes)
            self.assertIs(self.config_loader.providers_config, original_config)
            generation_response = client.get("/_test/runtime-generation")
            self.assertEqual(generation_response.json(), {"generation": 1})
            self.assertIs(runtime.observed_snapshot, original_snapshot)

    def test_structured_save_rejects_invalid_proxy_without_changing_disk_or_runtime(self):
        original_bytes = self.providers_path.read_bytes()

        with self._client() as (client, runtime):
            original_config = self.config_loader.providers_config
            original_snapshot = runtime.initial_snapshot
            response = client.post(
                "/v1/config/providers/structured",
                json={
                    "providers": [
                        {
                            "name": "openrouter",
                            "baseUrl": "https://openrouter.example",
                            "apikey": "DIRECT-KEY",
                        },
                        {
                            "name": "devbox",
                            "baseUrl": "https://new-devbox.example",
                            "apikey": "DIRECT-KEY",
                            "proxy": "http://user:super-secret@",
                        },
                    ]
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

            self.assertEqual(response.status_code, 400, response.text)
            self.assertNotIn("super-secret", response.text)
            self.assertEqual(
                response.json()["detail"]["code"],
                "config_validation_failed",
            )
            self.assertEqual(self.providers_path.read_bytes(), original_bytes)
            self.assertIs(self.config_loader.providers_config, original_config)
            generation_response = client.get("/_test/runtime-generation")
            self.assertEqual(generation_response.json(), {"generation": 1})
            self.assertIs(runtime.observed_snapshot, original_snapshot)

    def test_successful_save_publishes_prebuilt_proxy_candidate(self):
        with self._client() as (client, runtime):
            response = client.post(
                "/v1/config/providers",
                content=UPDATED_PROXY_PROVIDERS_TEXT,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

            self.assertEqual(response.status_code, 200, response.text)
            client.get("/_test/runtime-generation")
            candidate_client = runtime.observed_snapshot.proxy_http_clients[
                "devbox"
            ]
            self.assertFalse(candidate_client.is_closed)

        self.assertTrue(candidate_client.is_closed)


if __name__ == "__main__":
    unittest.main()
