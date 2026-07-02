import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import router as api_v1_router
from llm_gateway_core.config.loader import ConfigLoader


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "cohere": {
      "baseUrl": "https://cohere.example/v2",
      "apikey": "COHERE-KEY"
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


class EmbeddingsEndpointTests(unittest.TestCase):
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
        if isinstance(downstream_response, list):
            fake_http_client.post = AsyncMock(side_effect=downstream_response)
        else:
            fake_http_client.post = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_post_embeddings_with_valid_body_returns_downstream_json_unchanged(self):
        downstream_payload = {
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
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
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://openai.example/v1/embeddings")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["headers"],
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer DIRECT-KEY",
                "X-Route-Header": "embed-route",
            },
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "text-embedding-3-small",
                "input": ["hello"],
                "encoding_format": "float",
                "dimensions": 256,
                "user": "operation-user",
            },
        )

    def test_post_embeddings_without_model_returns_400(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={"input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_post_embeddings_without_input_returns_400(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'input' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_post_embeddings_with_unknown_model_returns_404(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={"model": "unknown-model", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No embeddings route configured for model 'unknown-model'.")
        fake_http_client.post.assert_not_awaited()

    def test_post_embeddings_preserves_non_retryable_downstream_4xx(self):
        for status_code in (400, 500):
            with self.subTest(status_code=status_code):
                with self._client(
                    _FakeDownstreamResponse(
                        {"error": {"message": f"downstream-{status_code}"}},
                        status_code=status_code,
                    )
                ) as (client, fake_http_client):
                    response = client.post(
                        "/v1/embeddings",
                        json={"model": "gateway/embed-small", "input": ["hello"]},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                self.assertEqual(response.status_code, 400 if status_code == 400 else 503)
                self.assertEqual(response.json()["detail"], f"Downstream request failed with status {status_code}.")
                self.assertNotIn(f"downstream-{status_code}", response.text)
                fake_http_client.post.assert_awaited_once()

    def test_post_embeddings_retries_same_route_when_operation_route_configures_retry(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["embeddings"][0]["routes"][0]["retry_count"] = 1
        operation_rules["embeddings"][0]["routes"][0]["retry_delay"] = 0
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        downstream_payload = {
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "temporary-unavailable"}}, status_code=503),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        ) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)

    def test_post_embeddings_falls_back_to_next_route_after_downstream_failure(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["embeddings"][0]["routes"].append(
            {
                "provider": "cohere",
                "model": "embed-english-v3.0",
                "target_path": "/embed",
                "custom_body_params": {
                    "encoding_format": "float",
                },
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        downstream_payload = {
            "object": "list",
            "data": [{"object": "embedding", "embedding": [0.3, 0.4], "index": 0}],
            "model": "embed-english-v3.0",
            "usage": {"prompt_tokens": 2, "total_tokens": 2},
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "primary-down"}}, status_code=503),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        ) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={
                    "model": "gateway/embed-small",
                    "input": ["hello"],
                    "encoding_format": "base64",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        self.assertEqual(fake_http_client.post.await_args_list[0].args[0], "https://openai.example/v1/embeddings")
        self.assertEqual(fake_http_client.post.await_args_list[1].args[0], "https://cohere.example/v2/embed")
        self.assertEqual(fake_http_client.post.await_args_list[1].kwargs["headers"]["Authorization"], "Bearer COHERE-KEY")
        self.assertEqual(
            fake_http_client.post.await_args_list[1].kwargs["json"],
            {
                "model": "embed-english-v3.0",
                "input": ["hello"],
                "encoding_format": "float",
            },
        )

    def test_post_embeddings_does_not_fallback_after_non_retryable_downstream_4xx(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["embeddings"][0]["routes"].append(
            {
                "provider": "cohere",
                "model": "embed-english-v3.0",
                "target_path": "/embed",
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "bad request"}}, status_code=400),
                _FakeDownstreamResponse({"object": "list", "data": []}, status_code=200),
            ]
        ) as (client, fake_http_client):
            response = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Downstream request failed with status 400.")
        self.assertNotIn("bad request", response.text)
        self.assertEqual(fake_http_client.post.await_count, 1)

    def test_api_v1_router_registers_embeddings_router(self):
        embeddings_routes = [
            route
            for route in api_v1_router.routes
            if getattr(route, "path", None) == "/embeddings" and "Embeddings V1" in getattr(route, "tags", [])
        ]
        self.assertEqual(len(embeddings_routes), 1)


if __name__ == "__main__":
    unittest.main()
