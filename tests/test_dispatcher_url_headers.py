import unittest
from unittest.mock import Mock

import httpx

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.request_handler import OperationDispatcher


class OperationDispatcherUrlHeadersTests(unittest.TestCase):
    def setUp(self):
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="OPENAI_KEY"),
        }
        self.mock_http_client = Mock(spec=httpx.AsyncClient)
        self.dispatcher = OperationDispatcher(
            self.providers_config,
            {"embeddings": {}, "rerank": {}, "images_generations": {}, "images_edits": {}},
            self.mock_http_client,
        )

    def test_build_target_url_returns_correct_url_with_normalized_slashes(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
        )

        for base_url, expected_url in (
            ("https://openai.example", "https://openai.example/embeddings"),
            ("https://openai.example/", "https://openai.example/embeddings"),
            ("https://openai.example/v1", "https://openai.example/v1/embeddings"),
            ("https://openai.example/v1/", "https://openai.example/v1/embeddings"),
        ):
            with self.subTest(base_url=base_url):
                provider_config = ProviderDetails(baseUrl=base_url, apikey="OPENAI_KEY")
                self.assertEqual(
                    self.dispatcher.build_target_url(route, provider_config),
                    expected_url,
                )

    def test_build_target_url_returns_absolute_target_path_as_is(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
        )

        provider_config = ProviderDetails(baseUrl="https://openai.example/v1", apikey="OPENAI_KEY")

        self.assertEqual(
            self.dispatcher.build_target_url(route, provider_config),
            "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
        )

    def test_build_headers_includes_authorization_when_api_key_present(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
        )

        headers = self.dispatcher.build_headers(route, "provider-secret")

        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(headers["Authorization"], "Bearer provider-secret")

    def test_build_headers_merges_custom_headers_with_route_priority(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
            custom_headers={
                "Content-Type": "application/vnd.api+json",
                "X-Request-ID": "dispatcher-test",
            },
        )

        headers = self.dispatcher.build_headers(route, "provider-secret")

        self.assertEqual(headers["Content-Type"], "application/vnd.api+json")
        self.assertEqual(headers["Authorization"], "Bearer provider-secret")
        self.assertEqual(headers["X-Request-ID"], "dispatcher-test")


if __name__ == "__main__":
    unittest.main()
