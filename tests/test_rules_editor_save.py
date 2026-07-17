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


class RulesEditorSaveTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.router_rules_path = Path(self.temp_dir.name) / "models_router_rules.json"
        self.model_rules_path = Path(self.temp_dir.name) / "models_model_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text("{}", encoding="utf-8")
        self.fusion_rules_path.write_text("[]", encoding="utf-8")
        self.router_rules_path.write_text("[]", encoding="utf-8")
        self.model_rules_path.write_text("{}", encoding="utf-8")
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
            router_rules_filename=str(self.router_rules_path),
            model_rules_filename=str(self.model_rules_path),
        )
        self.config_loader.load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        with transactional_rules_editor_client(self.config_loader) as result:
            yield result

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

        with self._client() as (client, runtime):
            response = client.post(
                "/v1/config/models-rules",
                content=invalid_rules_text,
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["code"],
            "config_validation_failed",
        )
        self.assertEqual(
            response.json()["detail"]["errors"],
            [
                {
                    "type": "rule_validation",
                    "loc": [],
                    "msg": (
                        "Invalid provider 'missing-provider' used in fallback "
                        "rule for 'gateway-model'. Provider not found in "
                        "configuration."
                    ),
                }
            ],
        )
        self.assertNotIn("DIRECT-KEY", response.text)
        self.assertIs(runtime.initial_snapshot.config_loader, self.config_loader)

    def test_save_providers_returns_400_for_invalid_json(self):
        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/providers",
                content='[{ "provider-a": ',
                headers={
                    "Authorization": "Bearer test-gateway-key",
                    "Content-Type": "text/plain",
                },
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["code"],
            "config_validation_failed",
        )

    def test_get_providers_structured_returns_provider_cards(self):
        with self._client() as (client, _runtime):
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

        with self._client() as (client, runtime):
            response = client.post(
                "/v1/config/providers/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            client.get("/_test/runtime-generation")
            published = runtime.observed_snapshot

        self.assertEqual(response.status_code, 200)
        self.assertEqual(published.generation, 2)
        self.assertEqual(
            published.config_loader.providers_config["devbox"].baseUrl,
            "https://new-devbox.example",
        )
        self.assertEqual(
            self.config_loader.providers_config["devbox"].baseUrl,
            "https://devbox.example",
        )
        saved_text = self.providers_path.read_text(encoding="utf-8")
        self.assertIn('"devbox"', saved_text)
        self.assertIn('"proxy": "http://proxy.example:8080"', saved_text)
        self.assertIn('"pricing"', saved_text)

    def test_get_router_rules_structured_returns_current_router_config(self):
        self.router_rules_path.write_text(
            """
[
  {
    "gateway_model_name": "gateway/router",
    "selector_model": "gateway-model",
    "targets": [
      {"type": "gateway_model", "model": "gateway-model"}
    ]
  }
]
""".strip(),
            encoding="utf-8",
        )
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            router_rules_filename=str(self.router_rules_path),
            model_rules_filename=str(self.model_rules_path),
        ).load_complete()

        with self._client() as (client, _runtime):
            response = client.get(
                "/v1/config/router-rules/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["chat_models"], ["gateway-model"])
        self.assertEqual(payload["fallback_chains"]["gateway-model"][0]["provider"], "devbox")
        self.assertEqual(payload["rules"][0]["gateway_model_name"], "gateway/router")
        self.assertEqual(payload["rules"][0]["selector_model"], "gateway-model")

    def test_save_router_rules_structured_updates_file_and_runtime_config(self):
        payload = {
            "rules": [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway-model",
                    "targets": [
                        {"type": "gateway_model", "model": "gateway-model"},
                        {"type": "fallback_entry", "gateway_model": "gateway-model", "index": 0},
                    ],
                }
            ]
        }

        with self._client() as (client, runtime):
            response = client.post(
                "/v1/config/router-rules/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            client.get("/_test/runtime-generation")
            published = runtime.observed_snapshot

        self.assertEqual(response.status_code, 200)
        self.assertIn(
            "gateway/router",
            published.config_loader.router_rules,
        )
        self.assertNotIn("gateway/router", self.config_loader.router_rules)
        saved_text = self.router_rules_path.read_text(encoding="utf-8")
        self.assertIn('"gateway_model_name": "gateway/router"', saved_text)
        self.assertIn('"type": "fallback_entry"', saved_text)

    def test_save_router_rules_structured_rejects_unknown_selector(self):
        original_file_content = self.router_rules_path.read_text(encoding="utf-8")
        payload = {
            "rules": [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/missing",
                    "targets": [{"type": "gateway_model", "model": "gateway-model"}],
                }
            ]
        }

        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/router-rules/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"]["code"],
            "config_validation_failed",
        )
        self.assertEqual(self.router_rules_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
