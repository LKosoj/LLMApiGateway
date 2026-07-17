import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import httpx
from starlette.requests import Request

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.config.settings import settings
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


class OperationDispatchTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp_dir.cleanup)
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        with patch.object(settings, "fallback_provider", "openai"):
            self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()
        self.fake_http_client = Mock(spec=httpx.AsyncClient)
        self.dispatcher = OperationDispatcher(
            self.config_loader.providers_config,
            self.config_loader.operation_rules,
            self.fake_http_client,
            model_rules={},
        )

    def test_dispatch_lookup_returns_route(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertIsNotNone(route)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")

    def test_dispatch_lookup_unknown_model_returns_none(self):
        route = self.dispatcher.lookup_route("embeddings", "unknown-model")

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertIsNone(route)

    def test_dispatch_lookup_resolves_model_alias(self):
        model_rules = {
            "aliases": {"public/embed": "gateway/embed-small"},
        }
        dispatcher = OperationDispatcher(
            self.config_loader.providers_config,
            self.config_loader.operation_rules,
            self.fake_http_client,
            model_rules=model_rules,
        )
        route = dispatcher.lookup_route("embeddings", "public/embed")

        self.assertIs(dispatcher._http_client, self.fake_http_client)
        self.assertIsNotNone(route)
        self.assertEqual(route.model, "text-embedding-3-small")

    def test_dispatch_build_target_url(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        provider_config = self.config_loader.providers_config[route.provider]
        target_url = self.dispatcher.build_target_url(route, provider_config)

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertEqual(target_url, "https://openai.example/v1/embeddings")

    def test_dispatch_build_headers(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        headers = self.dispatcher.build_headers(route, "provider-secret")

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertEqual(
            headers,
            {
                "Content-Type": "application/vnd.api+json",
                "Authorization": "Bearer provider-secret",
                "X-Route-Header": "embed-route",
            },
        )

    def test_dispatch_build_payload(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        payload = self.dispatcher.build_payload(
            {
                "model": "gateway/embed-small",
                "input": ["hello"],
                "dimensions": 1024,
                "encoding_format": "base64",
            },
            route,
            "embeddings",
        )

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
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
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/embeddings",
                "headers": [],
            }
        )
        request.state.llmgateway_gateway_model = "gateway/embed-small"

        self.dispatcher.set_request_state(
            request=request,
            operation="embeddings",
            route=route,
            provider_name=route.provider,
            provider_model=route.model,
        )

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertEqual(request.state.llmgateway_gateway_model, "gateway/embed-small")
        self.assertEqual(request.state.llmgateway_provider, "openai")
        self.assertEqual(request.state.llmgateway_provider_model, "text-embedding-3-small")
        self.assertEqual(request.state.llmgateway_operation, "embeddings")
        self.assertEqual(request.state.llmgateway_target_path, "/embeddings")

    def test_dispatch_uses_shared_http_client(self):
        route = self.dispatcher.lookup_route("rerank", "gateway/rerank-v1")

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertIsNotNone(route)

    def test_dispatch_lookup_routes_returns_ordered_fallback_routes(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        routes = self.dispatcher.lookup_routes("embeddings", "gateway/embed-small")

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")
        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[0].provider, "openai")
        self.assertEqual(routes[0].model, "text-embedding-3-small")
        self.assertEqual(routes[1].provider, "cohere")
        self.assertEqual(routes[1].model, "embed-english-v3.0")

    def test_dispatch_payload_allowlist(self):
        route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        route.custom_body_params.update(
            {
                "top_n": 5,
                "stream": True,
                "messages": [{"role": "user", "content": "hi"}],
                "model": "should-not-win",
            }
        )
        payload = self.dispatcher.build_payload(
            {
                "model": "gateway/embed-small",
                "input": "hello",
                "stream": True,
                "tools": [{"type": "function"}],
            },
            route,
            "embeddings",
        )

        self.assertIs(self.dispatcher._http_client, self.fake_http_client)
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
