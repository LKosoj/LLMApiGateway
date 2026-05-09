import ast
import inspect
import textwrap
import unittest
from unittest.mock import Mock

import httpx

from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.request_handler import OperationDispatcher


class OperationDispatcherNoFallbackRotationTests(unittest.TestCase):
    def setUp(self):
        self.dispatcher = OperationDispatcher(
            providers_config={
                "openai": ProviderDetails(baseUrl="https://openai.example", apikey="OPENAI_KEY"),
                "cohere": ProviderDetails(baseUrl="https://cohere.example", apikey="COHERE_KEY"),
            },
            operation_rules={
                "embeddings": {
                    "gateway/embed-small": {
                        "routes": [
                            {
                                "provider": "openai",
                                "model": "text-embedding-3-small",
                                "target_path": "/embeddings",
                            },
                            {
                                "provider": "cohere",
                                "model": "embed-english-v3.0",
                                "target_path": "/v2/embed",
                            },
                        ]
                    }
                },
                "rerank": {
                    "gateway/rerank-v1": {
                        "routes": [
                            {
                                "provider": "cohere",
                                "model": "rerank-v3.5",
                                "target_path": "/v2/rerank",
                            },
                            {
                                "provider": "openai",
                                "model": "rerank-fallback",
                                "target_path": "/score",
                            },
                        ]
                    }
                },
            },
            http_client=Mock(spec=httpx.AsyncClient),
        )

    def test_lookup_route_returns_only_the_first_route_without_fallback_iteration(self):
        embeddings_route = self.dispatcher.lookup_route("embeddings", "gateway/embed-small")
        rerank_route = self.dispatcher.lookup_route("rerank", "gateway/rerank-v1")

        self.assertEqual(embeddings_route.provider, "openai")
        self.assertEqual(embeddings_route.model, "text-embedding-3-small")
        self.assertEqual(rerank_route.provider, "cohere")
        self.assertEqual(rerank_route.model, "rerank-v3.5")

    def test_dispatcher_source_contains_no_fallback_rotation_or_retry_logic(self):
        dispatcher_source = inspect.getsource(OperationDispatcher)
        lookup_route_source = inspect.getsource(OperationDispatcher.lookup_route)
        lookup_route_tree = ast.parse(textwrap.dedent(lookup_route_source))

        self.assertIn("routes[0]", lookup_route_source)
        self.assertFalse(any(isinstance(node, (ast.For, ast.While)) for node in ast.walk(lookup_route_tree)))

        runtime_names = {
            node.id
            for node in ast.walk(ast.parse(textwrap.dedent(dispatcher_source)))
            if isinstance(node, ast.Name)
        }
        runtime_names.update(
            node.attr
            for node in ast.walk(ast.parse(textwrap.dedent(dispatcher_source)))
            if isinstance(node, ast.Attribute)
        )

        forbidden_markers = (
            "fallback_models",
            "model_rotation_db",
            "rotate_models",
            "retry_count",
            "retry_delay",
        )
        for marker in forbidden_markers:
            with self.subTest(marker=marker):
                self.assertNotIn(marker, runtime_names)


if __name__ == "__main__":
    unittest.main()
