import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient
from starlette.requests import Request

import main
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1/",
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
            "Content-Type": "application/vnd.api+json",
            "X-Route-Header": "embed-route"
          },
          "custom_body_params": {
            "dimensions": 256,
            "encoding_format": "float",
            "user": "operation-user"
          }
        },
        {
          "provider": "cohere",
          "model": "embed-english-v3.0",
          "target_path": "/v2/embed"
        }
      ]
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "gateway/rerank-v1",
      "routes": [
        {
          "provider": "cohere",
          "model": "rerank-v3.5",
          "target_path": "/score"
        }
      ]
    }
  ]
}
""".strip()


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class OperationDispatchTests(unittest.TestCase):
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
    def _client(self):
        fake_http_client = Mock(spec=httpx.AsyncClient)
        fake_http_client.post = AsyncMock()
        fake_http_client.aclose = AsyncMock()
        self.config_loader.load_fusion_rules = Mock(return_value={})

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))

            with TestClient(main.app) as client:
                dispatcher = getattr(client.app.state, "operation_dispatcher", None)
                self.assertIsInstance(dispatcher, OperationDispatcher)
                self.assertIs(client.app.state.http_client, fake_http_client)
                yield client, dispatcher, fake_http_client

    def test_dispatch_lookup_returns_route(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertIsNotNone(route)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")

    def test_dispatch_lookup_unknown_model_returns_none(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "unknown-model")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertIsNone(route)

    def test_dispatch_lookup_resolves_model_alias(self):
        self.config_loader.model_rules = {
            "aliases": {"public/embed": "gateway/embed-small"},
        }
        self.config_loader.load_model_rules = Mock(return_value=self.config_loader.model_rules)

        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "public/embed")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertIsNotNone(route)
        self.assertEqual(route.model, "text-embedding-3-small")

    def test_dispatch_build_target_url(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            provider_config = client.app.state.config_loader.providers_config[route.provider]
            target_url = dispatcher.build_target_url(route, provider_config)

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(target_url, "https://openai.example/v1/embeddings")

    def test_dispatch_build_headers(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            headers = dispatcher.build_headers(route, "provider-secret")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(
            headers,
            {
                "Content-Type": "application/vnd.api+json",
                "Authorization": "Bearer provider-secret",
                "X-Route-Header": "embed-route",
            },
        )

    def test_dispatch_build_payload(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            payload = dispatcher.build_payload(
                {
                    "model": "gateway/embed-small",
                    "input": ["hello"],
                    "dimensions": 1024,
                    "encoding_format": "base64",
                },
                route,
                "embeddings",
            )

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(
            payload,
            {
                "model": "text-embedding-3-small",
                "input": ["hello"],
                "dimensions": 256,
                "encoding_format": "float",
                "user": "operation-user",
            },
        )

    def test_dispatch_sets_request_state(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            request = Request(
                {
                    "type": "http",
                    "method": "POST",
                    "path": "/v1/embeddings",
                    "headers": [],
                }
            )
            request.state.llmgateway_gateway_model = "gateway/embed-small"

            dispatcher.set_request_state(
                request=request,
                operation="embeddings",
                route=route,
                provider_name=route.provider,
                provider_model=route.model,
            )

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(request.state.llmgateway_gateway_model, "gateway/embed-small")
        self.assertEqual(request.state.llmgateway_provider, "openai")
        self.assertEqual(request.state.llmgateway_provider_model, "text-embedding-3-small")
        self.assertEqual(request.state.llmgateway_operation, "embeddings")
        self.assertEqual(request.state.llmgateway_target_path, "/embeddings")

    def test_dispatch_uses_shared_http_client(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("rerank", "gateway/rerank-v1")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertIsNotNone(route)

    def test_dispatch_lookup_routes_returns_ordered_fallback_routes(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            routes = dispatcher.lookup_routes("embeddings", "gateway/embed-small")

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")
        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[0].provider, "openai")
        self.assertEqual(routes[0].model, "text-embedding-3-small")
        self.assertEqual(routes[1].provider, "cohere")
        self.assertEqual(routes[1].model, "embed-english-v3.0")

    def test_dispatch_payload_allowlist(self):
        with self._client() as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("embeddings", "gateway/embed-small")
            route.custom_body_params.update(
                {
                    "top_n": 5,
                    "stream": True,
                    "messages": [{"role": "user", "content": "hi"}],
                    "model": "should-not-win",
                }
            )
            payload = dispatcher.build_payload(
                {
                    "model": "gateway/embed-small",
                    "input": "hello",
                    "stream": True,
                    "tools": [{"type": "function"}],
                },
                route,
                "embeddings",
            )

        self.assertIs(dispatcher, client.app.state.operation_dispatcher)
        self.assertIs(dispatcher._http_client, fake_http_client)
        self.assertEqual(payload["model"], "text-embedding-3-small")
        self.assertEqual(payload["dimensions"], 256)
        self.assertEqual(payload["encoding_format"], "float")
        self.assertEqual(payload["user"], "operation-user")
        self.assertEqual(payload["input"], "hello")
        self.assertNotIn("top_n", payload)
        self.assertNotIn("stream", payload)
        self.assertNotIn("messages", payload)
        self.assertNotIn("tools", payload)


if __name__ == "__main__":
    unittest.main()
