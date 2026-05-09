import unittest

from pydantic import ValidationError

from llm_gateway_core.config.loader import ConfigLoader, FallbackModelRule, OperationRoute, ProviderDetails


class OperationRoutesConfigTests(unittest.TestCase):
    def setUp(self):
        self.config_loader = ConfigLoader()
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="DIRECT-KEY"),
            "cohere": ProviderDetails(baseUrl="https://cohere.example", apikey="DIRECT-KEY"),
        }

    def test_operation_route_rejects_blank_model(self):
        with self.assertRaises(ValidationError) as context:
            OperationRoute(provider="openai", model="   ", target_path="/embeddings")

        self.assertIn("'model' must not be empty.", str(context.exception))

    def test_operation_route_rejects_target_path_without_leading_slash(self):
        with self.assertRaises(ValidationError) as context:
            OperationRoute(provider="openai", model="text-embedding-3-small", target_path="embeddings")

        self.assertIn("'target_path' must start with '/' or be an absolute http(s) URL.", str(context.exception))

    def test_operation_route_accepts_absolute_https_target_path(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="https://example.com/v1/embeddings",
        )

        self.assertEqual(route.target_path, "https://example.com/v1/embeddings")

    def test_operation_route_accepts_retry_settings(self):
        route = OperationRoute(
            provider="openai",
            model="text-embedding-3-small",
            target_path="/embeddings",
            retry_count=2,
            retry_delay=1.5,
        )

        self.assertEqual(route.retry_count, 2)
        self.assertEqual(route.retry_delay, 1.5)

    def test_operation_route_rejects_negative_retry_settings(self):
        with self.assertRaises(ValidationError) as count_context:
            OperationRoute(
                provider="openai",
                model="text-embedding-3-small",
                target_path="/embeddings",
                retry_count=-1,
            )

        with self.assertRaises(ValidationError) as delay_context:
            OperationRoute(
                provider="openai",
                model="text-embedding-3-small",
                target_path="/embeddings",
                retry_delay=-0.5,
            )

        self.assertIn("'retry_count' must be greater than or equal to 0.", str(count_context.exception))
        self.assertIn("'retry_delay' must be greater than or equal to 0.", str(delay_context.exception))

    def test_operation_route_rejects_unknown_request_format(self):
        with self.assertRaises(ValidationError) as context:
            OperationRoute(
                provider="openai",
                model="text-embedding-3-small",
                target_path="/embeddings",
                request_format="unsupported",
            )

        self.assertIn(
            "'request_format' must be one of: nvidia_genai_json, nvidia_riva_grpc, openai_images, "
            "openai_images_multipart, query_passages, query_texts.",
            str(context.exception),
        )

    def test_operation_route_rejects_unknown_response_format(self):
        with self.assertRaises(ValidationError) as context:
            OperationRoute(
                provider="openai",
                model="text-embedding-3-small",
                target_path="/embeddings",
                response_format="unsupported",
            )

        self.assertIn(
            "'response_format' must be one of: nvidia_artifacts, openai_images, rankings_logit, scores.",
            str(context.exception),
        )

    def test_operation_route_accepts_query_texts_and_scores_rerank_formats(self):
        route = OperationRoute(
            provider="openai",
            model="Qwen/Qwen3-Reranker-0.6B",
            target_path="/rerank",
            request_format="query_texts",
            response_format="scores",
        )

        self.assertEqual(route.request_format, "query_texts")
        self.assertEqual(route.response_format, "scores")

    def test_operation_route_accepts_image_mapping_objects(self):
        route = OperationRoute(
            provider="openai",
            model="gpt-image-1",
            target_path="/images/generations",
            request_format="nvidia_genai_json",
            response_format="nvidia_artifacts",
            request_mapping={
                "fields": {
                    "prompt": "prompt",
                    "aspect_ratio": {
                        "from": "size",
                        "transform": "map",
                        "mapping": {"1024x1024": "1:1"},
                    },
                }
            },
            response_mapping={
                "artifacts_path": "artifacts",
                "base64_field": "base64",
            },
        )

        self.assertEqual(route.request_format, "nvidia_genai_json")
        self.assertEqual(route.response_format, "nvidia_artifacts")
        self.assertEqual(route.request_mapping["fields"]["prompt"], "prompt")
        self.assertEqual(route.response_mapping["base64_field"], "base64")

    def test_operation_route_accepts_openai_images_multipart_request_format(self):
        route = OperationRoute(
            provider="openai",
            model="gpt-image-1",
            target_path="/images/edits",
            request_format="openai_images_multipart",
        )

        self.assertEqual(route.request_format, "openai_images_multipart")

    def test_operation_route_accepts_nvidia_audio_grpc_request_format(self):
        route = OperationRoute(
            provider="nvidia",
            model="nvidia/parakeet-1_1b-rnnt-multilingual-asr",
            target_path="/audio/transcriptions",
            request_format="nvidia_riva_grpc",
        )

        self.assertEqual(route.request_format, "nvidia_riva_grpc")

    def test_operation_route_rejects_unknown_response_output_format(self):
        with self.assertRaises(ValidationError) as context:
            OperationRoute(
                provider="openai",
                model="text-embedding-3-small",
                target_path="/score",
                response_output_format="unsupported",
            )

        self.assertIn("'response_output_format' must be one of: jina_results.", str(context.exception))

    def test_operation_route_rejects_security_headers(self):
        for header_name in ("Authorization", "Cookie", "X-Api-Key"):
            with self.subTest(header_name=header_name):
                with self.assertRaises(ValidationError) as context:
                    OperationRoute(
                        provider="openai",
                        model="text-embedding-3-small",
                        target_path="/embeddings",
                        custom_headers={header_name: "secret"},
                    )

                self.assertIn(
                    "custom_headers must not contain protected headers: Authorization, Cookie, X-Api-Key.",
                    str(context.exception),
                )

    def test_operation_route_rejects_forbidden_body_params(self):
        for param_name in ("stream", "messages", "tool_choice", "tools", "model"):
            with self.subTest(param_name=param_name):
                with self.assertRaises(ValidationError) as context:
                    OperationRoute(
                        provider="openai",
                        model="text-embedding-3-small",
                        target_path="/embeddings",
                        custom_body_params={param_name: "forbidden"},
                    )

                self.assertIn(
                    "custom_body_params must not contain reserved keys: stream, messages, tool_choice, tools, model.",
                    str(context.exception),
                )

    def test_fallback_rule_rejects_forbidden_body_params(self):
        for param_name in ("stream", "messages", "tool_choice", "tools", "model"):
            with self.subTest(param_name=param_name):
                with self.assertRaises(ValidationError) as context:
                    FallbackModelRule(
                        provider="openai",
                        model="gpt-4o-mini",
                        custom_body_params={param_name: "forbidden"},
                    )

                self.assertIn(
                    "custom_body_params must not contain reserved keys: stream, messages, tool_choice, tools, model.",
                    str(context.exception),
                )

    def test_build_operation_config_rejects_unknown_top_level_section(self):
        raw_config = {
            "embeddigns": [
                {
                    "gateway_model_name": "typo-model",
                    "routes": [],
                }
            ],
        }

        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            self.config_loader._build_operation_config(raw_config)

    def test_build_operation_config_rejects_duplicate_gateway_model_name_within_section(self):
        raw_config = {
            "embeddings": [
                {
                    "gateway_model_name": "embed-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        }
                    ],
                },
                {
                    "gateway_model_name": "embed-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "embed-english-v3.0",
                            "target_path": "/v2/embed",
                        }
                    ],
                },
            ],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'embed-model' found in embeddings operation routes.",
        ):
            self.config_loader._build_operation_config(raw_config)

    def test_validate_operation_routes_rejects_unknown_provider(self):
        raw_config = {
            "embeddings": [
                {
                    "gateway_model_name": "embed-model",
                    "routes": [
                        {
                            "provider": "missing-provider",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        }
                    ],
                }
            ],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        with self.assertRaisesRegex(
            ValueError,
            "Invalid provider 'missing-provider' used in operation route for 'embed-model' in 'embeddings'.",
        ):
            self.config_loader.validate_operation_routes(
                operation_config,
                providers_config=self.providers_config,
            )

    def test_build_operation_config_sets_score_target_path_for_rerank_routes(self):
        raw_config = {
            "embeddings": [],
            "rerank": [
                {
                    "gateway_model_name": "rerank-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-v3.5",
                        }
                    ],
                }
            ],
            "images_generations": [],
            "images_edits": [],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertEqual(
            operation_config["rerank"]["rerank-model"]["routes"][0]["target_path"],
            "/score",
        )

    def test_build_operation_config_sets_default_target_path_for_image_edits_routes(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [
                {
                    "gateway_model_name": "image-edit-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "gpt-image-1",
                        }
                    ],
                }
            ],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertEqual(
            operation_config["images_edits"]["image-edit-model"]["routes"][0]["target_path"],
            "/images/edits",
        )

    def test_build_operation_config_sets_default_target_path_for_audio_transcriptions_routes(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "audio_transcriptions": [
                {
                    "gateway_model_name": "audio-transcription-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "gpt-4o-mini-transcribe",
                        }
                    ],
                }
            ],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertEqual(
            operation_config["audio_transcriptions"]["audio-transcription-model"]["routes"][0]["target_path"],
            "/audio/transcriptions",
        )

    def test_build_operation_config_sets_default_target_path_for_audio_speech_routes(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "audio_speech": [
                {
                    "gateway_model_name": "audio-speech-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "tts-1",
                        }
                    ],
                }
            ],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertEqual(
            operation_config["audio_speech"]["audio-speech-model"]["routes"][0]["target_path"],
            "/audio/speech",
        )

    def test_build_operation_config_sets_default_target_path_for_pdf_conversions_routes(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "pdf_conversions": [
                {
                    "gateway_model_name": "pdf-conversion-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "pdf-converter",
                        }
                    ],
                }
            ],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertEqual(
            operation_config["pdf_conversions"]["pdf-conversion-model"]["routes"][0]["target_path"],
            "/api",
        )

    def test_build_operation_config_for_web_sections(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "web_search": [
                {
                    "gateway_model_name": "web-search-model",
                    "query_model": "llmgateway/light_model",
                }
            ],
            "web_read": [
                {
                    "gateway_model_name": "web-read-model",
                }
            ],
            "web_research": [
                {
                    "gateway_model_name": "web-research-model",
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "rerank_model": "rerank-model",
                    "analysis_model": "llmgateway/light_model",
                }
            ],
            "web_deep_research": [
                {
                    "gateway_model_name": "web-deep-research-model",
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "fast_model": "llmgateway/light_model",
                    "smart_model": "llmgateway/light_model",
                    "strategic_model": "llmgateway/light_model",
                    "embedding_model": "llmgateway/embedding",
                    "image_generation_model": "models/gemini-2.5-flash-image",
                    "image_generation_size": "1024x1024",
                }
            ],
        }

        operation_config = self.config_loader._build_operation_config(raw_config)

        self.assertNotIn(
            "routes",
            operation_config["web_search"]["web-search-model"],
        )
        self.assertEqual(
            operation_config["web_search"]["web-search-model"]["query_model"],
            "llmgateway/light_model",
        )
        self.assertNotIn(
            "routes",
            operation_config["web_read"]["web-read-model"],
        )
        self.assertEqual(
            operation_config["web_research"]["web-research-model"]["analysis_model"],
            "llmgateway/light_model",
        )
        self.assertEqual(
            operation_config["web_research"]["web-research-model"]["rerank_model"],
            "rerank-model",
        )
        self.assertEqual(
            operation_config["web_deep_research"]["web-deep-research-model"]["search_model"],
            "web-search-model",
        )
        self.assertEqual(
            operation_config["web_deep_research"]["web-deep-research-model"]["fast_model"],
            "llmgateway/light_model",
        )
        self.assertEqual(
            operation_config["web_deep_research"]["web-deep-research-model"]["image_generation_model"],
            "models/gemini-2.5-flash-image",
        )

    def test_build_operation_config_rejects_legacy_routes_in_web_search(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "web_search": [
                {
                    "gateway_model_name": "legacy-web-search",
                    "query_model": "llmgateway/light_model",
                    "routes": [
                        {
                            "provider": "z.ai",
                            "model": "search-prime",
                            "target_path": "https://api.z.ai/api/paas/v4/web_search",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            self.config_loader._build_operation_config(raw_config)

    def test_build_operation_config_rejects_legacy_routes_in_web_read(self):
        raw_config = {
            "embeddings": [],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
            "web_read": [
                {
                    "gateway_model_name": "legacy-web-read",
                    "routes": [
                        {
                            "provider": "z.ai",
                            "model": "reader",
                            "target_path": "https://api.z.ai/api/paas/v4/reader",
                        }
                    ],
                }
            ],
        }

        with self.assertRaisesRegex(ValidationError, "Extra inputs are not permitted"):
            self.config_loader._build_operation_config(raw_config)

    def test_validate_operation_routes_allows_web_research_without_routes(self):
        operation_config = {
            "embeddings": {
                "llmgateway/embedding": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        }
                    ],
                }
            },
            "rerank": {
                "rerank-model": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "rerank-model",
                            "target_path": "/score",
                        }
                    ],
                }
            },
            "images_generations": {
                "llmgateway/image-gen": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "gpt-image-1",
                            "target_path": "/images/generations",
                        }
                    ],
                }
            },
            "images_edits": {},
            "web_search": {"web-search-model": {}},
            "web_read": {"web-read-model": {}},
            "web_research": {
                "web-research-model": {
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "rerank_model": "rerank-model",
                    "analysis_model": "llmgateway/light_model",
                    "routes": [],
                }
            },
            "web_deep_research": {
                "web-deep-research-model": {
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "fast_model": "llmgateway/light_model",
                    "smart_model": "llmgateway/light_model",
                    "strategic_model": "llmgateway/light_model",
                    "embedding_model": "llmgateway/embedding",
                    "image_generation_model": "llmgateway/image-gen",
                    "image_generation_size": "1024x1024",
                    "routes": [],
                }
            },
        }

        self.config_loader.validate_operation_routes(
            operation_config,
            providers_config=self.providers_config,
            fallback_rules={"llmgateway/light_model": {}},
        )

    def test_validate_operation_routes_rejects_unknown_web_research_reference(self):
        operation_config = {
            "web_search": {},
            "web_read": {},
            "web_research": {
                "web-research-model": {
                    "search_model": "missing-search",
                    "read_model": "missing-read",
                    "rerank_model": "missing-rerank",
                    "analysis_model": "llmgateway/light_model",
                    "routes": [],
                }
            },
        }

        with self.assertRaisesRegex(
            ValueError,
            "Web research model 'web-research-model' references unknown search_model 'missing-search'.",
        ):
            self.config_loader.validate_operation_routes(
                operation_config,
                providers_config=self.providers_config,
            )

    def test_validate_operation_routes_rejects_unknown_web_research_chat_model(self):
        operation_config = {
            "web_search": {"web-search-model": {}},
            "web_read": {"web-read-model": {}},
            "rerank": {
                "rerank-model": {
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "rerank-model",
                            "target_path": "/score",
                        }
                    ]
                }
            },
            "web_research": {
                "web-research-model": {
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "rerank_model": "rerank-model",
                    "analysis_model": "missing-chat-model",
                    "routes": [],
                }
            },
        }

        with self.assertRaisesRegex(
            ValueError,
            "references unknown analysis_model 'missing-chat-model'",
        ):
            self.config_loader.validate_operation_routes(
                operation_config,
                providers_config=self.providers_config,
                fallback_rules={},
            )

    def test_build_operation_config_rejects_web_research_without_rerank_model(self):
        raw_config = {
            "web_research": [
                {
                    "gateway_model_name": "web-research-model",
                    "search_model": "web-search-model",
                    "read_model": "web-read-model",
                    "analysis_model": "llmgateway/light_model",
                }
            ],
        }

        with self.assertRaises(ValidationError):
            self.config_loader._build_operation_config(raw_config)

    def test_build_operation_config_rejects_web_deep_research_without_search_or_read_model(self):
        raw_config = {
            "web_deep_research": [
                {
                    "gateway_model_name": "web-deep-research-model",
                    "fast_model": "llmgateway/light_model",
                    "smart_model": "llmgateway/light_model",
                    "strategic_model": "llmgateway/light_model",
                }
            ],
        }

        with self.assertRaises(ValidationError):
            self.config_loader._build_operation_config(raw_config)


if __name__ == "__main__":
    unittest.main()
