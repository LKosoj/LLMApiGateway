import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.operation_accounting_test_support import (
    install_embeddings_rerank_accounting_passthrough,
)


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

VALID_FALLBACK_RULES_TEXT = """
[
  {
    "gateway_model_name": "chat-model",
    "fallback_models": [
      {
        "provider": "openai",
        "model": "gpt-4o-mini"
      }
    ],
    "rotate_models": false
  }
]
""".strip()

VALID_OPERATION_RULES_TEXT = """
{
  "embeddings": [
    {
      "gateway_model_name": "gateway/embed-small",
      "routes": [
        {
          "provider": "openai",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings",
          "custom_headers": {
            "X-Route-Header": "embed-route"
          },
          "custom_body_params": {
            "dimensions": 256,
            "user": "operation-user"
          }
        }
      ]
    }
  ],
  "rerank": [],
  "images_generations": [],
  "images_edits": []
}
""".strip()


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FakeDownstreamResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class EmbeddingsApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openai")
        self.fallback_provider_patcher.start()
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self, downstream_response):
        fake_http_client = Mock()
        fake_http_client.post = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
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
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            install_embeddings_rerank_accounting_passthrough(stack)

            with TestClient(main.app) as client:
                services = client.app.state.services
                self.assertIs(services.http_client, fake_http_client)
                lease = client.portal.call(services.runtime_manager.acquire_current)
                try:
                    dispatcher = lease.snapshot.operation_dispatcher
                    self.assertIsInstance(dispatcher, OperationDispatcher)
                    yield client, dispatcher, fake_http_client
                finally:
                    client.portal.call(lease.release)

    def test_embeddings_valid_request(self):
        downstream_payload = {
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            self.assertIsNotNone(route)

            response = client.post(
                "/v1/embeddings",
                json={
                    "model": "gateway/embed-small",
                    "input": ["hello"],
                    "encoding_format": "float",
                    "dimensions": 1024,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        tokens_usage_db.insert_usage.assert_not_called()

    def test_embeddings_missing_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("embeddings", "gateway/embed-small"))

            response = client.post(
                "/v1/embeddings",
                json={"input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_embeddings_missing_input(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("embeddings", "gateway/embed-small"))

            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'input' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_embeddings_unknown_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNone(dispatcher.lookup_route("embeddings", "unknown-model"))

            response = client.post(
                "/v1/embeddings",
                json={"model": "unknown-model", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No embeddings route configured for model 'unknown-model'.")
        fake_http_client.post.assert_not_awaited()

    def test_embeddings_downstream_error(self):
        with self._client(
            _FakeDownstreamResponse(
                {"error": {"message": "downstream-500"}},
                status_code=500,
            )
        ) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("embeddings", "gateway/embed-small"))

            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Downstream request failed with status 500.")
        self.assertNotIn("downstream-500", response.text)
        fake_http_client.post.assert_awaited_once()

    def test_embeddings_response_unchanged(self):
        downstream_payload = {
            "object": "list",
            "data": [
                {
                    "object": "embedding",
                    "embedding": [0.1, 0.2],
                    "index": 0,
                    "metadata": {"source": "downstream"},
                }
            ],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
            "extra_field": {"nested": ["kept", "as-is"]},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            self.assertEqual(dispatcher.lookup_route("embeddings", "gateway/embed-small").model, "text-embedding-3-small")

            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        tokens_usage_db.insert_usage.assert_not_called()


if __name__ == "__main__":
    unittest.main()
