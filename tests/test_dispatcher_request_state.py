import unittest
from unittest.mock import Mock

import httpx
from starlette.requests import Request

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.request_handler import OperationDispatcher


class OperationDispatcherRequestStateTests(unittest.TestCase):
    def setUp(self):
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="OPENAI_KEY"),
        }
        self.operation_rules = {
            "embeddings": {},
            "rerank": {},
        }
        self.dispatcher = OperationDispatcher(
            self.providers_config,
            self.operation_rules,
            Mock(spec=httpx.AsyncClient),
        )

    def test_set_request_state_populates_dispatcher_fields_on_request_state(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/embeddings",
                "headers": [],
            }
        )
        request.state.llmgateway_gateway_model = "gateway/embed-small"
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
        )

        self.dispatcher.set_request_state(
            request=request,
            operation="embeddings",
            route=route,
            provider_name="openai",
            provider_model="text-embedding-3-small",
        )

        self.assertEqual(request.state.llmgateway_gateway_model, "gateway/embed-small")
        self.assertEqual(request.state.llmgateway_provider, "openai")
        self.assertEqual(request.state.llmgateway_provider_model, "text-embedding-3-small")
        self.assertEqual(request.state.llmgateway_operation, "embeddings")
        self.assertEqual(request.state.llmgateway_target_path, "/embeddings")

        self.assertEqual(getattr(request.state, "llmgateway_gateway_model"), "gateway/embed-small")
        self.assertEqual(getattr(request.state, "llmgateway_provider"), "openai")
        self.assertEqual(getattr(request.state, "llmgateway_provider_model"), "text-embedding-3-small")
        self.assertEqual(getattr(request.state, "llmgateway_operation"), "embeddings")
        self.assertEqual(getattr(request.state, "llmgateway_target_path"), "/embeddings")

    def test_set_request_state_fails_closed_when_gateway_model_is_missing(self):
        request = Request(
            {
                "type": "http",
                "method": "POST",
                "path": "/v1/rerank",
                "headers": [],
            }
        )
        route = OperationRoute(
            provider="openai",
            model="rerank-model",
            target_path="/score",
        )

        with self.assertRaisesRegex(ValueError, "llmgateway_gateway_model"):
            self.dispatcher.set_request_state(
                request=request,
                operation="rerank",
                route=route,
                provider_name="openai",
                provider_model="rerank-model",
            )


if __name__ == "__main__":
    unittest.main()
