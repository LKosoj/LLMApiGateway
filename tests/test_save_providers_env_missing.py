import os
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.middleware.auth import ROLE_USER
from tests.rules_editor_test_support import (
    editor_router,
    transactional_rules_editor_client,
)


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


class SaveProvidersEnvMissingTests(unittest.TestCase):
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
        ).load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        with transactional_rules_editor_client(self.config_loader) as result:
            yield result

    def _providers_payload(self, devbox_api_key: str) -> str:
        return f"""
        [
          {{
            "openrouter": {{
              "baseUrl": "https://openrouter.example",
              "apikey": "DIRECT-KEY"
            }}
          }},
          {{
            "devbox": {{
              "baseUrl": "https://devbox.example",
              "apikey": "{devbox_api_key}"
            }}
          }}
        ]
        """.strip()

    def test_save_providers_returns_400_for_missing_env_reference(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("MISSING_ENV", None)
            with self._client() as (client, _runtime):
                response = client.post(
                    "/v1/ui/providers-config",
                    content=self._providers_payload("${MISSING_ENV}"),
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
        self.assertNotIn("MISSING_ENV", response.text)
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)

    def test_save_providers_accepts_existing_env_reference(self):
        with patch.dict(os.environ, {"EXISTING_ENV": "secret-value"}, clear=False):
            with self._client() as (client, runtime):
                response = client.post(
                    "/v1/ui/providers-config",
                    content=self._providers_payload("${EXISTING_ENV}"),
                    headers={
                        "Authorization": "Bearer test-gateway-key",
                        "Content-Type": "text/plain",
                    },
                )
                client.get("/_test/runtime-generation")
                published = runtime.observed_snapshot

        self.assertEqual(response.status_code, 200)
        self.assertEqual(published.generation, 2)
        self.assertEqual(
            published.config_loader.providers_config["devbox"].apikey,
            "${EXISTING_ENV}",
        )
        self.assertEqual(
            self.providers_path.read_text(encoding="utf-8"),
            self._providers_payload("${EXISTING_ENV}"),
        )

    def test_legacy_providers_config_route_requires_master_in_handler(self):
        original_file_content = self.providers_path.read_text(encoding="utf-8")
        app = FastAPI()

        @app.middleware("http")
        async def set_user_role(request, call_next):
            request.state.api_key_role = ROLE_USER
            return await call_next(request)

        app.include_router(editor_router, prefix="/v1")

        with TestClient(app) as client:
            response = client.post(
                "/v1/ui/providers-config",
                content=VALID_PROVIDERS_TEXT,
                headers={"Content-Type": "text/plain"},
            )

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )
        self.assertEqual(self.providers_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
