import unittest
from unittest.mock import Mock

import httpx

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.request_handler import OperationDispatcher


class OperationDispatcherTests(unittest.TestCase):
    """Tests for OperationDispatcher lookup_route functionality."""

    def setUp(self):
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="OPENAI_KEY"),
            "cohere": ProviderDetails(baseUrl="https://cohere.example", apikey="COHERE_KEY"),
        }
        self.operation_rules = {
            "embeddings": {
                "text-embedding-3-small": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                            "retry_count": 2,
                            "retry_delay": 1,
                            "custom_headers": {"X-Custom": "value"},
                            "custom_body_params": {"encoding_format": "float"},
                        }
                    ]
                },
                "embed-english-v3": {
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "embed-english-v3",
                            "target_path": "/v2/embed",
                        }
                    ]
                },
            },
            "rerank": {
                "rerank-english-v3": {
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-english-v3",
                            "target_path": "/rerank",
                        }
                    ]
                },
            },
            "images_generations": {},
            "images_edits": {},
            "audio_speech": {
                "tts-1": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "tts-1",
                            "target_path": "/audio/speech",
                        }
                    ]
                }
            },
            "audio_transcriptions": {
                "gpt-4o-mini-transcribe": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "gpt-4o-mini-transcribe",
                            "target_path": "/audio/transcriptions",
                        }
                    ]
                }
            },
            "pdf_conversions": {
                "pdf-converter": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "pdf-converter",
                            "target_path": "/api",
                        }
                    ]
                }
            },
        }
        self.mock_http_client = Mock(spec=httpx.AsyncClient)
        self.dispatcher = OperationDispatcher(self.providers_config, self.operation_rules, self.mock_http_client)

    def test_lookup_route_returns_route_for_valid_embeddings_operation_and_model(self):
        """
        Verify that lookup_route returns a valid OperationRoute for embeddings operation
        with an existing gateway model.
        """
        route = self.dispatcher.lookup_route("embeddings", "text-embedding-3-small")

        self.assertIsNotNone(route)
        self.assertIsInstance(route, OperationRoute)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")
        self.assertEqual(route.target_path, "/embeddings")
        self.assertEqual(route.retry_count, 2)
        self.assertEqual(route.retry_delay, 1.0)
        self.assertEqual(route.custom_headers, {"X-Custom": "value"})
        self.assertEqual(route.custom_body_params, {"encoding_format": "float"})

    def test_lookup_route_returns_route_for_valid_rerank_operation_and_model(self):
        """
        Verify that lookup_route returns a valid OperationRoute for rerank operation
        with an existing gateway model.
        """
        route = self.dispatcher.lookup_route("rerank", "rerank-english-v3")

        self.assertIsNotNone(route)
        self.assertIsInstance(route, OperationRoute)
        self.assertEqual(route.provider, "cohere")
        self.assertEqual(route.model, "rerank-english-v3")
        self.assertEqual(route.target_path, "/rerank")

    def test_lookup_route_returns_route_for_valid_audio_transcriptions_operation_and_model(self):
        route = self.dispatcher.lookup_route("audio_transcriptions", "gpt-4o-mini-transcribe")

        self.assertIsNotNone(route)
        self.assertIsInstance(route, OperationRoute)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "gpt-4o-mini-transcribe")
        self.assertEqual(route.target_path, "/audio/transcriptions")

    def test_lookup_route_returns_route_for_valid_audio_speech_operation_and_model(self):
        route = self.dispatcher.lookup_route("audio_speech", "tts-1")

        self.assertIsNotNone(route)
        self.assertIsInstance(route, OperationRoute)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "tts-1")
        self.assertEqual(route.target_path, "/audio/speech")

    def test_lookup_route_returns_route_for_valid_pdf_conversion_operation_and_model(self):
        route = self.dispatcher.lookup_route("pdf_conversions", "pdf-converter")

        self.assertIsNotNone(route)
        self.assertIsInstance(route, OperationRoute)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "pdf-converter")
        self.assertEqual(route.target_path, "/api")

    def test_lookup_route_returns_none_for_unknown_operation(self):
        """
        Verify that lookup_route returns None for an unknown operation type.
        """
        route = self.dispatcher.lookup_route("unknown_operation", "text-embedding-3-small")
        self.assertIsNone(route)

    def test_lookup_route_returns_none_for_unknown_model_in_embeddings(self):
        """
        Verify that lookup_route returns None when the gateway model is not found
        in the embeddings section.
        """
        route = self.dispatcher.lookup_route("embeddings", "unknown-model")
        self.assertIsNone(route)

    def test_lookup_route_returns_none_for_unknown_model_in_rerank(self):
        """
        Verify that lookup_route returns None when the gateway model is not found
        in the rerank section.
        """
        route = self.dispatcher.lookup_route("rerank", "unknown-model")
        self.assertIsNone(route)

    def test_lookup_route_returns_none_for_empty_rules_section(self):
        """
        Verify that lookup_route returns None when the operation section is empty.
        """
        mock_client = Mock(spec=httpx.AsyncClient)
        dispatcher = OperationDispatcher(
            self.providers_config,
            {"embeddings": {}, "rerank": {}, "images_generations": {}, "images_edits": {}},
            mock_client,
        )
        route = dispatcher.lookup_route("embeddings", "any-model")
        self.assertIsNone(route)

    def test_lookup_route_returns_none_for_missing_routes(self):
        """
        Verify that lookup_route returns None when the model config exists
        but has no routes.
        """
        rules = {
            "embeddings": {
                "model-without-routes": {"routes": []},
            },
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
        }
        mock_client = Mock(spec=httpx.AsyncClient)
        dispatcher = OperationDispatcher(self.providers_config, rules, mock_client)
        route = dispatcher.lookup_route("embeddings", "model-without-routes")
        self.assertIsNone(route)

    def test_lookup_route_returns_first_route_when_multiple_exist(self):
        """
        Verify that lookup_route returns the first route when multiple routes exist.
        """
        rules = {
            "embeddings": {
                "multi-route-model": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        },
                        {
                            "provider": "cohere",
                            "model": "embed-english-v3",
                            "target_path": "/v2/embed",
                        },
                    ]
                },
            },
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
        }
        mock_client = Mock(spec=httpx.AsyncClient)
        dispatcher = OperationDispatcher(self.providers_config, rules, mock_client)
        route = dispatcher.lookup_route("embeddings", "multi-route-model")

        self.assertIsNotNone(route)
        self.assertEqual(route.provider, "openai")
        self.assertEqual(route.model, "text-embedding-3-small")

    def test_lookup_routes_returns_all_routes_in_config_order(self):
        rules = {
            "embeddings": {
                "multi-route-model": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        },
                        {
                            "provider": "cohere",
                            "model": "embed-english-v3",
                            "target_path": "/v2/embed",
                        },
                    ]
                },
            },
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
        }
        mock_client = Mock(spec=httpx.AsyncClient)
        dispatcher = OperationDispatcher(self.providers_config, rules, mock_client)
        routes = dispatcher.lookup_routes("embeddings", "multi-route-model")

        self.assertEqual(len(routes), 2)
        self.assertEqual(routes[0].provider, "openai")
        self.assertEqual(routes[0].model, "text-embedding-3-small")
        self.assertEqual(routes[1].provider, "cohere")
        self.assertEqual(routes[1].model, "embed-english-v3")

    def test_lookup_route_returns_none_for_invalid_route_data(self):
        """
        Verify that lookup_route returns None when route data is invalid
        and cannot be parsed into OperationRoute.
        """
        rules = {
            "embeddings": {
                "invalid-route-model": {
                    "routes": [
                        {
                            "provider": "openai",
                            # Missing required fields
                        }
                    ]
                },
            },
            "rerank": {},
            "images_generations": {},
            "images_edits": {},
        }
        mock_http_client = Mock(spec=httpx.AsyncClient)
        dispatcher = OperationDispatcher(self.providers_config, rules, mock_http_client)
        route = dispatcher.lookup_route("embeddings", "invalid-route-model")
        self.assertIsNone(route)
        self.assertEqual(dispatcher.lookup_routes("embeddings", "invalid-route-model"), [])


if __name__ == "__main__":
    unittest.main()
