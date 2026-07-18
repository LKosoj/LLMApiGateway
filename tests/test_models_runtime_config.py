import os
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ANTHROPIC_API_VERSION, ConfigLoader, resolve_provider_api_key
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from tests.chat_accounting_test_support import install_main_chat_accounting_double


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

INITIAL_RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway-model-v1",
    "fallback_models": [
      {
        "provider": "devbox",
        "model": "provider-model-v1"
      }
    ],
    "rotate_models": false
  }
]
""".strip()

UPDATED_RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway-model-v2",
    "fallback_models": [
      {
        "provider": "devbox",
        "model": "provider-model-v2"
      }
    ],
    "rotate_models": false
  }
]
""".strip()


class _FallbackModelsResponse:
    def __init__(
        self,
        payload: dict | None = None,
        status_code: int = 200,
        *,
        text: str = '{"data":[]}',
        json_error: Exception | None = None,
    ):
        self._payload = payload or {"data": []}
        self.status_code = status_code
        self.text = text
        self._json_error = json_error

    def json(self):
        if self._json_error is not None:
            raise self._json_error
        return self._payload


class ModelsRuntimeConfigTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(INITIAL_RULES_TEXT, encoding="utf-8")
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
        def unexpected_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(f"Unexpected HTTP request: {request.method}")

        fake_http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(unexpected_request)
        )
        fake_http_client.get = AsyncMock(return_value=_FallbackModelsResponse())
        self.config_loader.load_fusion_rules = Mock(return_value={})

        with ExitStack() as stack:
            install_main_chat_accounting_double(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch(
                    "main.create_shared_http_client",
                    return_value=fake_http_client,
                )
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_models_endpoint_uses_same_provider_api_key_resolution_as_chat(self):
        headers = {"Authorization": "Bearer test-gateway-key"}

        with self._client() as (client, fake_http_client):
            response = client.get("/v1/models", headers=headers)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            fake_http_client.get.await_args.kwargs["headers"]["Authorization"],
            "Bearer DIRECT-KEY",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    def test_models_and_chat_use_same_fallback_provider(self, make_llm_request_mock):
        headers = {"Authorization": "Bearer test-gateway-key"}
        make_llm_request_mock.return_value = (
            {
                "id": "chat-success",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
            },
            None,
        )

        with self._client() as (client, fake_http_client):
            models_response = client.get("/v1/models", headers=headers)
            chat_response = client.post(
                "/v1/chat/completions",
                json={"model": "unknown-model", "messages": [{"role": "user", "content": "hello"}]},
                headers=headers,
            )

        self.assertEqual(models_response.status_code, 200)
        self.assertEqual(chat_response.status_code, 200)
        self.assertEqual(
            fake_http_client.get.await_args.args[0],
            "https://openrouter.example/models",
        )
        self.assertEqual(
            make_llm_request_mock.await_args.args[1],
            "https://openrouter.example/chat/completions",
        )

    def test_models_endpoint_uses_anthropic_fallback_provider_catalog_contract(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}},
  {"anthropic": {"baseUrl": "https://api.anthropic.example", "apikey": "ANTHROPIC-KEY", "type": "anthropic"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer test-gateway-key"}

        with patch.object(main.settings, "fallback_provider", "anthropic"):
            with self._client() as (client, fake_http_client):
                response = client.get("/v1/models", headers=headers)

        self.assertEqual(response.status_code, 200)
        call = fake_http_client.get.await_args
        self.assertEqual(call.args[0], "https://api.anthropic.example/v1/models")
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "ANTHROPIC-KEY")
        self.assertEqual(call.kwargs["headers"]["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_models_endpoint_sanitizes_downstream_catalog_error_logs(self):
        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock(
                return_value=_FallbackModelsResponse(
                    status_code=503,
                    text="secret-upstream-body https://internal.example/models?token=secret",
                )
            )
            with self.assertLogs("llm_gateway_core.api.v1.models", level="WARNING") as logs:
                response = client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(ids, {"gateway-model-v1"})
        log_output = "\n".join(logs.output)
        self.assertIn("Downstream error 503", log_output)
        self.assertNotIn("secret-upstream-body", log_output)
        self.assertNotIn("internal.example", log_output)
        self.assertNotIn("openrouter.example/models", log_output)

    def test_models_endpoint_sanitizes_invalid_catalog_json_logs(self):
        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock(
                return_value=_FallbackModelsResponse(
                    status_code=200,
                    text="secret-invalid-json-body",
                    json_error=ValueError("secret-json-error"),
                )
            )
            with self.assertLogs("llm_gateway_core.api.v1.models", level="ERROR") as logs:
                response = client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(ids, {"gateway-model-v1"})
        log_output = "\n".join(logs.output)
        self.assertIn("Invalid JSON response", log_output)
        self.assertNotIn("secret-invalid-json-body", log_output)
        self.assertNotIn("secret-json-error", log_output)
        self.assertNotIn("openrouter.example/models", log_output)

    def test_get_model_uses_anthropic_fallback_provider_catalog_contract(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY"}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}},
  {"anthropic": {"baseUrl": "https://api.anthropic.example", "apikey": "ANTHROPIC-KEY", "type": "anthropic"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer test-gateway-key"}

        with patch.object(main.settings, "fallback_provider", "anthropic"):
            with self._client() as (client, fake_http_client):
                fake_http_client.get = AsyncMock(
                    return_value=_FallbackModelsResponse({"id": "claude-haiku"}, status_code=200)
                )
                response = client.get("/v1/models/claude-haiku", headers=headers)

        self.assertEqual(response.status_code, 200)
        call = fake_http_client.get.await_args
        self.assertEqual(call.args[0], "https://api.anthropic.example/v1/models/claude-haiku")
        self.assertEqual(call.kwargs["headers"]["x-api-key"], "ANTHROPIC-KEY")
        self.assertEqual(call.kwargs["headers"]["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("Authorization", call.kwargs["headers"])

    def test_get_model_sanitizes_invalid_downstream_json_logs(self):
        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock(
                return_value=_FallbackModelsResponse(
                    status_code=200,
                    text="secret-invalid-model-json-body",
                    json_error=ValueError("secret-model-json-error"),
                )
            )
            with self.assertLogs("llm_gateway_core.api.v1.models", level="ERROR") as logs:
                response = client.get(
                    "/v1/models/provider-model-v1",
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Model 'provider-model-v1' not found.")
        log_output = "\n".join(logs.output)
        self.assertIn("Invalid JSON response", log_output)
        self.assertNotIn("secret-invalid-model-json-body", log_output)
        self.assertNotIn("secret-model-json-error", log_output)
        self.assertNotIn("openrouter.example/models/provider-model-v1", log_output)

    def test_models_endpoint_uses_explicit_provider_model_list(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY", "available_models": ["pinned-model-a", "pinned-model-b"]}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer test-gateway-key"}

        with self._client() as (client, fake_http_client):
            response = client.get("/v1/models", headers=headers)

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertIn("pinned-model-a", ids)
        self.assertIn("pinned-model-b", ids)
        # The pinned list must short-circuit the live /models lookup for the provider.
        called_urls = [call.args[0] for call in fake_http_client.get.await_args_list if call.args]
        self.assertNotIn("https://openrouter.example/models", called_urls)

    def test_get_model_in_explicit_list_returns_entry_without_live_lookup(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY", "available_models": ["pinned-model-a", "pinned-model-b"]}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer test-gateway-key"}

        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock()
            response = client.get("/v1/models/pinned-model-a", headers=headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "pinned-model-a")
        self.assertEqual(body["object"], "model")
        self.assertEqual(body["owned_by"], "openrouter")
        self.assertEqual(body["source_provider"], "openrouter")
        # A pinned model must short-circuit the per-model upstream lookup.
        self.assertEqual(fake_http_client.get.await_count, 0)

    def test_get_model_in_explicit_list_anthropic_format(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY", "available_models": ["pinned-model-a"]}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {
            "Authorization": "Bearer test-gateway-key",
            "anthropic-version": ANTHROPIC_API_VERSION,
        }

        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock()
            response = client.get("/v1/models/pinned-model-a", headers=headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "pinned-model-a")
        self.assertEqual(body["type"], "model")
        self.assertEqual(body["display_name"], "pinned-model-a")
        self.assertEqual(fake_http_client.get.await_count, 0)

    def test_get_model_not_in_explicit_list_returns_404_without_live_lookup(self):
        self.providers_path.write_text(
            """
[
  {"openrouter": {"baseUrl": "https://openrouter.example", "apikey": "DIRECT-KEY", "available_models": ["pinned-model-a"]}},
  {"devbox": {"baseUrl": "https://devbox.example", "apikey": "DIRECT-KEY"}}
]
""".strip(),
            encoding="utf-8",
        )
        headers = {"Authorization": "Bearer test-gateway-key"}

        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock()
            response = client.get("/v1/models/not-pinned", headers=headers)

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "Model 'not-pinned' not found.")
        self.assertNotIn("pinned-model-a", str(response.json()))
        # An explicit list must not trigger a live per-model lookup for missing ids.
        self.assertEqual(fake_http_client.get.await_count, 0)

    def test_models_list_filtered_by_virtual_key_allowed_models(self):
        virtual_record = ApiKeyRecord(
            id=42,
            name="virtual-key",
            api_key="lgk_virtual",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            allowed_models=["gateway-model-v1"],
        )

        fallback_payload = {
            "data": [
                {"id": "gateway-model-v1"},
                {"id": "extra-fallback-model"},
            ]
        }

        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock(
                return_value=_FallbackModelsResponse(fallback_payload)
            )
            api_keys_db = client.app.state.services.api_keys_db
            with patch.object(
                api_keys_db,
                "get_by_key",
                return_value=virtual_record,
            ) as get_by_key:
                response = client.get(
                    "/v1/models",
                    headers={"Authorization": "Bearer lgk_virtual"},
                )

        self.assertEqual(response.status_code, 200)
        get_by_key.assert_called_once_with("lgk_virtual")
        ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(ids, {"gateway-model-v1"})

    def test_models_list_unfiltered_for_master_key(self):
        fallback_payload = {
            "data": [
                {"id": "extra-fallback-model"},
            ]
        }

        with self._client() as (client, fake_http_client):
            fake_http_client.get = AsyncMock(
                return_value=_FallbackModelsResponse(fallback_payload)
            )
            response = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        ids = {item["id"] for item in response.json()["data"]}
        self.assertEqual(ids, {"gateway-model-v1", "extra-fallback-model"})

    def test_resolve_provider_api_key_uses_explicit_env_reference(self):
        with patch.dict(os.environ, {"DEVBOX_ENV_KEY": "env-key-value"}, clear=False):
            self.assertEqual(resolve_provider_api_key("${DEVBOX_ENV_KEY}"), "env-key-value")

        self.assertEqual(resolve_provider_api_key("DIRECT-KEY"), "DIRECT-KEY")
        self.assertIsNone(resolve_provider_api_key(None))


if __name__ == "__main__":
    unittest.main()
