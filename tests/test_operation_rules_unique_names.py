import unittest

from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails


class OperationRulesUniqueNamesTests(unittest.TestCase):
    """
    Tests for gateway_model_name uniqueness constraints in operation rules.

    Requirements:
    - Same name in chat (fallback) rules and embeddings rules: ALLOWED
    - Same name in chat (fallback) rules and rerank rules: ALLOWED
    - Duplicate in embeddings section: REJECTED
    - Duplicate in rerank section: REJECTED
    """

    def setUp(self):
        self.config_loader = ConfigLoader()
        self.providers_config = {
            "openai": ProviderDetails(baseUrl="https://openai.example", apikey="DIRECT-KEY"),
            "cohere": ProviderDetails(baseUrl="https://cohere.example", apikey="DIRECT-KEY"),
        }

    def test_same_name_in_chat_and_embeddings_allowed(self):
        """
        Verify that a gateway_model_name can be shared between chat rules (fallback)
        and embeddings rules without causing a conflict.
        """
        chat_fallback_rules = [
            {
                "gateway_model_name": "shared-model",
                "fallback_models": [
                    {"provider": "openai", "model": "gpt-4o"}
                ],
                "rotate_models": False,
            }
        ]

        embeddings_rules = {
            "embeddings": [
                {
                    "gateway_model_name": "shared-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "embed-english-v3.0",
                            "target_path": "/v2/embed",
                        }
                    ],
                }
            ],
            "rerank": [],
        }

        # Build fallback rules config (chat rules)
        fallback_config = self.config_loader._build_fallback_rules_config(chat_fallback_rules)

        # Build operation config (embeddings/rerank rules)
        operation_config = self.config_loader._build_operation_config(embeddings_rules)

        # Both configs should be created successfully
        self.assertIn("shared-model", fallback_config)
        self.assertIn("shared-model", operation_config["embeddings"])

        # Validate operation routes against providers
        self.config_loader.validate_operation_routes(
            operation_config,
            providers_config=self.providers_config,
        )

    def test_same_name_in_chat_and_rerank_allowed(self):
        """
        Verify that a gateway_model_name can be shared between chat rules (fallback)
        and rerank rules without causing a conflict.
        """
        chat_fallback_rules = [
            {
                "gateway_model_name": "shared-model",
                "fallback_models": [
                    {"provider": "openai", "model": "gpt-4o"}
                ],
                "rotate_models": False,
            }
        ]

        rerank_rules = {
            "embeddings": [],
            "rerank": [
                {
                    "gateway_model_name": "shared-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-v3.5",
                        }
                    ],
                }
            ],
        }

        # Build fallback rules config (chat rules)
        fallback_config = self.config_loader._build_fallback_rules_config(chat_fallback_rules)

        # Build operation config (embeddings/rerank rules)
        operation_config = self.config_loader._build_operation_config(rerank_rules)

        # Both configs should be created successfully
        self.assertIn("shared-model", fallback_config)
        self.assertIn("shared-model", operation_config["rerank"])

        # Validate operation routes against providers
        self.config_loader.validate_operation_routes(
            operation_config,
            providers_config=self.providers_config,
        )

    def test_duplicate_in_embeddings_section_rejected(self):
        """
        Verify that duplicate gateway_model_name within the embeddings section
        is properly rejected with a clear error message.
        """
        embeddings_rules = {
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
                    "gateway_model_name": "embed-model",  # Duplicate!
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
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'embed-model' found in embeddings operation routes",
        ):
            self.config_loader._build_operation_config(embeddings_rules)

    def test_duplicate_in_rerank_section_rejected(self):
        """
        Verify that duplicate gateway_model_name within the rerank section
        is properly rejected with a clear error message.
        """
        rerank_rules = {
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
                },
                {
                    "gateway_model_name": "rerank-model",  # Duplicate!
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "rerank-model-v2",
                        }
                    ],
                },
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'rerank-model' found in rerank operation routes",
        ):
            self.config_loader._build_operation_config(rerank_rules)

    def test_same_name_in_embeddings_and_rerank_allowed(self):
        """
        Verify that the same gateway_model_name can be used in both embeddings
        and rerank sections without causing a conflict.
        This is a common use case where a model supports both operations.
        """
        operation_rules = {
            "embeddings": [
                {
                    "gateway_model_name": "multi-purpose-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                        }
                    ],
                }
            ],
            "rerank": [
                {
                    "gateway_model_name": "multi-purpose-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-v3.5",
                        }
                    ],
                }
            ],
        }

        # Build operation config should succeed
        operation_config = self.config_loader._build_operation_config(operation_rules)

        # Both sections should contain the same model name
        self.assertIn("multi-purpose-model", operation_config["embeddings"])
        self.assertIn("multi-purpose-model", operation_config["rerank"])

        # Validate operation routes against providers
        self.config_loader.validate_operation_routes(
            operation_config,
            providers_config=self.providers_config,
        )

    def test_duplicate_in_audio_transcriptions_section_rejected(self):
        audio_rules = {
            "embeddings": [],
            "rerank": [],
            "audio_transcriptions": [
                {
                    "gateway_model_name": "audio-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "gpt-4o-mini-transcribe",
                        }
                    ],
                },
                {
                    "gateway_model_name": "audio-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "another-audio-model",
                            "target_path": "/audio/transcriptions",
                        }
                    ],
                },
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'audio-model' found in audio_transcriptions operation routes",
        ):
            self.config_loader._build_operation_config(audio_rules)

    def test_duplicate_in_audio_speech_section_rejected(self):
        audio_rules = {
            "embeddings": [],
            "rerank": [],
            "audio_speech": [
                {
                    "gateway_model_name": "speech-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "tts-1",
                        }
                    ],
                },
                {
                    "gateway_model_name": "speech-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "another-tts-model",
                            "target_path": "/audio/speech",
                        }
                    ],
                },
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'speech-model' found in audio_speech operation routes",
        ):
            self.config_loader._build_operation_config(audio_rules)

    def test_duplicate_in_pdf_conversions_section_rejected(self):
        pdf_rules = {
            "embeddings": [],
            "rerank": [],
            "pdf_conversions": [
                {
                    "gateway_model_name": "pdf-model",
                    "routes": [
                        {
                            "provider": "openai",
                            "model": "pdf-converter",
                        }
                    ],
                },
                {
                    "gateway_model_name": "pdf-model",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "another-pdf-converter",
                            "target_path": "/api",
                        }
                    ],
                },
            ],
        }

        with self.assertRaisesRegex(
            ValueError,
            "Duplicate gateway_model_name 'pdf-model' found in pdf_conversions operation routes",
        ):
            self.config_loader._build_operation_config(pdf_rules)


if __name__ == "__main__":
    unittest.main()
