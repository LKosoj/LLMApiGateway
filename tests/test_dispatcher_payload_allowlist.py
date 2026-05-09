import unittest
from unittest.mock import Mock

import httpx

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.request_handler import OperationDispatcher


class OperationDispatcherPayloadAllowlistTests(unittest.TestCase):
    def setUp(self):
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="OPENAI_KEY"),
            "cohere": ProviderDetails(baseUrl="https://cohere.example", apikey="COHERE_KEY"),
        }
        self.mock_http_client = Mock(spec=httpx.AsyncClient)
        self.dispatcher = OperationDispatcher(
            self.providers_config,
            {"embeddings": {}, "rerank": {}, "images_generations": {}, "images_edits": {}},
            self.mock_http_client,
        )

    def test_build_payload_replaces_model_with_route_model(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
        )

        payload = self.dispatcher.build_payload(
            {"model": "gateway/embed-small", "input": "hello"},
            route,
            "embeddings",
        )

        self.assertEqual(payload["model"], "text-embedding-3-small")

    def test_build_payload_applies_embeddings_allowlist_and_removes_forbidden_keys(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
        )
        route.custom_body_params = {
            "dimensions": 1024,
            "encoding_format": "float",
            "user": "user-123",
            "top_n": 5,
            "stream": True,
            "messages": [{"role": "user", "content": "hi"}],
            "model": "should-not-win",
        }

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

        self.assertEqual(payload["model"], "text-embedding-3-small")
        self.assertEqual(payload["dimensions"], 1024)
        self.assertEqual(payload["encoding_format"], "float")
        self.assertEqual(payload["user"], "user-123")
        self.assertEqual(payload["input"], "hello")
        self.assertNotIn("top_n", payload)
        self.assertNotIn("stream", payload)
        self.assertNotIn("messages", payload)
        self.assertNotIn("tools", payload)

    def test_build_payload_applies_rerank_allowlist_without_overriding_model(self):
        route = OperationRoute(
            provider="cohere",
            model="rerank-v3.5",
            target_path="/rerank",
        )
        route.custom_body_params = {
            "top_n": 3,
            "return_documents": True,
            "max_chunks_per_doc": 2,
            "dimensions": 768,
            "model": "should-not-win",
            "tool_choice": "none",
        }

        payload = self.dispatcher.build_payload(
            {
                "model": "gateway/rerank-v1",
                "query": "hello",
                "documents": ["doc-1", "doc-2"],
                "messages": [{"role": "user", "content": "hi"}],
            },
            route,
            "rerank",
        )

        self.assertEqual(payload["model"], "rerank-v3.5")
        self.assertEqual(payload["top_n"], 3)
        self.assertTrue(payload["return_documents"])
        self.assertEqual(payload["max_chunks_per_doc"], 2)
        self.assertEqual(payload["query"], "hello")
        self.assertEqual(payload["documents"], ["doc-1", "doc-2"])
        self.assertNotIn("dimensions", payload)
        self.assertNotIn("messages", payload)
        self.assertNotIn("tool_choice", payload)

    def test_filter_custom_headers_blocks_security_sensitive_headers(self):
        filtered_headers = self.dispatcher.filter_custom_headers(
            {
                "Authorization": "Bearer should-be-blocked",
                "Cookie": "session=blocked",
                "X-Api-Key": "blocked",
                "X-Allowed": "keep-me",
                "X-Not-Allowed": "drop-me",
            },
            ["Authorization", "Cookie", "X-Api-Key", "X-Allowed"],
        )

        self.assertEqual(filtered_headers, {"X-Allowed": "keep-me"})

    def test_build_payload_with_route_overrides_keeps_prompt_and_merges_non_reserved_params(self):
        route = OperationRoute(
            provider="openai",
            model="gpt-image-1",
            target_path="/images/generations",
        )
        route.custom_body_params = {
            "quality": "high",
            "size": "1024x1024",
            "n": 2,
            "stream": True,
            "prompt": "should-not-override",
        }

        payload = self.dispatcher.build_payload_with_route_overrides(
            {
                "model": "gateway/image-v1",
                "prompt": "draw a fox",
                "stream": False,
            },
            route,
        )

        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertEqual(payload["prompt"], "draw a fox")
        self.assertEqual(payload["quality"], "high")
        self.assertEqual(payload["size"], "1024x1024")
        self.assertEqual(payload["n"], 2)
        self.assertNotIn("stream", payload)

    def test_build_payload_with_route_overrides_keeps_images_and_merges_edit_params(self):
        route = OperationRoute(
            provider="openai",
            model="gpt-image-1",
            target_path="/images/edits",
        )
        route.custom_body_params = {
            "input_fidelity": "high",
            "output_format": "png",
            "images": ["should-not-override"],
            "model": "should-not-win",
        }

        payload = self.dispatcher.build_payload_with_route_overrides(
            {
                "model": "gateway/image-edit-v1",
                "prompt": "remove the background",
                "images": [{"image_url": "https://example.com/image.png"}],
            },
            route,
        )

        self.assertEqual(payload["model"], "gpt-image-1")
        self.assertEqual(payload["prompt"], "remove the background")
        self.assertEqual(payload["images"], [{"image_url": "https://example.com/image.png"}])
        self.assertEqual(payload["input_fidelity"], "high")
        self.assertEqual(payload["output_format"], "png")


if __name__ == "__main__":
    unittest.main()
