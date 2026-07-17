import copy
import json
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.config.loader import ConfigLoader
from tests.rules_editor_test_support import transactional_rules_editor_client


VALID_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example/v1",
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
    "gateway_model_name": "gateway/chat-model",
    "fallback_models": [
      {
        "provider": "openrouter",
        "model": "gpt-4o-mini"
      }
    ],
    "rotate_models": false
  }
]
""".strip()

INITIAL_OPERATION_RULES_TEXT = """
{
  "embeddings": [
    {
      "gateway_model_name": "gateway/embed-initial",
      "routes": [
        {
          "provider": "openrouter",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [],
  "images_generations": [],
  "images_edits": []
}
""".strip()

UPDATED_OPERATION_RULES_PAYLOAD = {
    "embeddings": [
        {
            "gateway_model_name": "gateway/embed-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "text-embedding-3-large",
                    "target_path": "/embeddings",
                    "retry_count": 2,
                    "retry_delay": 1,
                    "custom_body_params": {"dimensions": 1024},
                }
            ],
        }
    ],
    "rerank": [
        {
            "gateway_model_name": "gateway/rerank-updated",
            "routes": [
                {
                    "provider": "cohere",
                    "model": "rerank-v3.5",
                    "retry_count": 1,
                    "retry_delay": 3,
                    "request_format": "query_passages",
                    "response_format": "rankings_logit",
                    "response_output_format": "jina_results",
                }
            ],
        }
    ],
    "images_generations": [
        {
            "gateway_model_name": "gateway/image-gen-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "image-model-v1",
                    "target_path": "/v1/genai/example/image-model-v1",
                    "request_format": "nvidia_genai_json",
                    "response_format": "nvidia_artifacts",
                    "request_mapping": {
                        "constants": {"mode": "base"},
                        "fields": {"prompt": "prompt"},
                    },
                    "response_mapping": {
                        "artifacts_path": "artifacts",
                        "base64_field": "base64",
                    },
                }
            ],
        }
    ],
    "images_edits": [],
    "audio_transcriptions": [
        {
            "gateway_model_name": "gateway/audio-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "gpt-4o-mini-transcribe",
                    "request_format": "nvidia_riva_grpc",
                    "retry_count": 1,
                    "custom_body_params": {"language": "en"},
                    "custom_headers": {"function-id": "func-123"},
                }
            ],
        }
    ],
}

UPDATED_OPERATION_RULES_RESPONSE = {
    "embeddings": [
        {
            "gateway_model_name": "gateway/embed-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "text-embedding-3-large",
                    "target_path": "/embeddings",
                    "retry_delay": 1.0,
                    "retry_count": 2,
                    "custom_headers": {},
                    "custom_body_params": {"dimensions": 1024},
                }
            ],
        }
    ],
    "rerank": [
        {
            "gateway_model_name": "gateway/rerank-updated",
            "routes": [
                {
                    "provider": "cohere",
                    "model": "rerank-v3.5",
                    "target_path": "/score",
                    "request_format": "query_passages",
                    "response_format": "rankings_logit",
                    "response_output_format": "jina_results",
                    "retry_delay": 3.0,
                    "retry_count": 1,
                    "custom_headers": {},
                    "custom_body_params": {},
                }
            ],
        }
    ],
    "images_generations": [
        {
            "gateway_model_name": "gateway/image-gen-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "image-model-v1",
                    "target_path": "/v1/genai/example/image-model-v1",
                    "request_format": "nvidia_genai_json",
                    "response_format": "nvidia_artifacts",
                    "request_mapping": {
                        "constants": {"mode": "base"},
                        "fields": {"prompt": "prompt"},
                    },
                    "response_mapping": {
                        "artifacts_path": "artifacts",
                        "base64_field": "base64",
                    },
                    "custom_headers": {},
                    "custom_body_params": {},
                }
            ],
        }
    ],
    "images_edits": [],
    "audio_transcriptions": [
        {
            "gateway_model_name": "gateway/audio-updated",
            "routes": [
                {
                    "provider": "openrouter",
                    "model": "gpt-4o-mini-transcribe",
                    "target_path": "/audio/transcriptions",
                    "request_format": "nvidia_riva_grpc",
                    "retry_count": 1,
                    "custom_headers": {"function-id": "func-123"},
                    "custom_body_params": {"language": "en"},
                }
            ],
        }
    ],
}


class OperationRulesStructuredApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.router_rules_path = Path(self.temp_dir.name) / "models_router_rules.json"
        self.model_rules_path = Path(self.temp_dir.name) / "models_model_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(INITIAL_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fusion_rules_path.write_text("[]", encoding="utf-8")
        self.router_rules_path.write_text("[]", encoding="utf-8")
        self.model_rules_path.write_text("{}", encoding="utf-8")
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openrouter",
        )
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            router_rules_filename=str(self.router_rules_path),
            model_rules_filename=str(self.model_rules_path),
        ).load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self):
        with transactional_rules_editor_client(self.config_loader) as result:
            yield result

    def test_get_operation_rules_structured_returns_current_structure(self):
        with self._client() as (client, _runtime):
            response = client.get(
                "/v1/config/model-operations/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "embeddings": [
                    {
                        "gateway_model_name": "gateway/embed-initial",
                        "routes": [
                            {
                                "provider": "openrouter",
                                "model": "text-embedding-3-small",
                                "target_path": "/embeddings",
                                "custom_headers": {},
                                "custom_body_params": {},
                            }
                        ],
                    }
                ],
                "rerank": [],
                "images_generations": [],
                "images_edits": [],
            },
        )

    def test_post_operation_rules_structured_saves_valid_payload_and_reload_is_visible_without_restart(self):
        with self._client() as (client, runtime):
            post_response = client.post(
                "/v1/config/model-operations/structured",
                json=UPDATED_OPERATION_RULES_PAYLOAD,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            get_response = client.get(
                "/v1/config/model-operations/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            client.get("/_test/runtime-generation")
            published = runtime.observed_snapshot

        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json(), UPDATED_OPERATION_RULES_RESPONSE)
        self.assertEqual(published.generation, 2)
        operation_rules = published.config_loader.operation_rules
        self.assertEqual(
            operation_rules["embeddings"]["gateway/embed-updated"]["routes"][0]["model"],
            "text-embedding-3-large",
        )
        self.assertEqual(
            operation_rules["rerank"]["gateway/rerank-updated"]["routes"][0][
                "response_output_format"
            ],
            "jina_results",
        )
        self.assertEqual(
            operation_rules["images_generations"]["gateway/image-gen-updated"][
                "routes"
            ][0]["request_format"],
            "nvidia_genai_json",
        )
        self.assertEqual(
            operation_rules["audio_transcriptions"]["gateway/audio-updated"]["routes"][
                0
            ]["custom_headers"],
            {"function-id": "func-123"},
        )
        self.assertIn("gateway/embed-initial", self.config_loader.operation_rules["embeddings"])

        persisted_payload = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted_payload, UPDATED_OPERATION_RULES_RESPONSE)

    def test_operation_cost_calculators_round_trip_without_losing_pdf_metadata(self):
        payload = copy.deepcopy(UPDATED_OPERATION_RULES_PAYLOAD)
        payload["images_generations"][0]["cost_calculator"] = {
            "unit": "operation",
            "rate_usd": 0,
        }
        payload["audio_transcriptions"][0]["cost_calculator"] = {
            "unit": "operation",
            "rate_usd": 0.4,
        }
        payload["pdf_conversions"] = [
            {
                "gateway_model_name": "gateway/pdf",
                "cost_calculator": {"unit": "operation", "rate_usd": 0.7},
                "routes": [
                    {
                        "provider": "openrouter",
                        "model": "pdf-model",
                        "target_path": "/pdf/conversions",
                    }
                ],
            }
        ]
        payload["web_search"] = [
            {
                "gateway_model_name": "gateway/web-search",
                "cost_calculator": {"unit": "operation", "rate_usd": 0.05},
            }
        ]
        payload["web_read"] = [
            {
                "gateway_model_name": "gateway/web-read",
                "cost_calculator": {"unit": "operation", "rate_usd": 0.1},
            }
        ]
        payload["web_research"] = [
            {
                "gateway_model_name": "gateway/web-research",
                "search_model": "gateway/web-search",
                "read_model": "gateway/web-read",
                "rerank_model": "gateway/rerank-updated",
                "analysis_model": "gateway/chat-model",
                "cost_calculator": {"unit": "operation", "rate_usd": 0.3},
            }
        ]
        payload["web_deep_research"] = [
            {
                "gateway_model_name": "gateway/web-deep-research",
                "search_model": "gateway/web-search",
                "read_model": "gateway/web-read",
                "fast_model": "gateway/chat-model",
                "smart_model": "gateway/chat-model",
                "strategic_model": "gateway/chat-model",
                "cost_calculator": {"unit": "operation", "rate_usd": 0.8},
            }
        ]

        with self._client() as (client, runtime):
            post_response = client.post(
                "/v1/config/model-operations/structured",
                json=payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            get_response = client.get(
                "/v1/config/model-operations/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            client.get("/_test/runtime-generation")
            published = runtime.observed_snapshot

        self.assertEqual(post_response.status_code, 200, post_response.text)
        self.assertEqual(get_response.status_code, 200, get_response.text)
        response_payload = get_response.json()
        expected_rates = {
            "images_generations": 0,
            "audio_transcriptions": 0.4,
            "pdf_conversions": 0.7,
            "web_search": 0.05,
            "web_read": 0.1,
            "web_research": 0.3,
            "web_deep_research": 0.8,
        }
        for section_name, rate_usd in expected_rates.items():
            self.assertEqual(
                response_payload[section_name][0]["cost_calculator"],
                {"unit": "operation", "rate_usd": rate_usd},
            )
            gateway_model = response_payload[section_name][0]["gateway_model_name"]
            self.assertEqual(
                published.config_loader.operation_rules[section_name][gateway_model][
                    "cost_calculator"
                ],
                {"unit": "operation", "rate_usd": rate_usd},
            )

        persisted = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted["pdf_conversions"][0]["cost_calculator"]["rate_usd"], 0.7)
        self.assertNotIn("cost_calculator", persisted["embeddings"][0])
        self.assertNotIn("cost_calculator", persisted["rerank"][0])

    def test_post_operation_rules_structured_returns_400_for_invalid_payload(self):
        invalid_payload = {
            "embeddings": [
                {
                    "gateway_model_name": "gateway/embed-invalid",
                    "routes": [
                        {
                            "provider": "openrouter",
                            "model": "text-embedding-3-small",
                            "target_path": "embeddings",
                        }
                    ],
                }
            ],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
        }
        original_file_content = self.operation_rules_path.read_text(encoding="utf-8")

        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertEqual(response_json["detail"]["code"], "config_validation_failed")
        self.assertEqual(self.operation_rules_path.read_text(encoding="utf-8"), original_file_content)

    def test_post_operation_rules_structured_returns_400_for_invalid_request_format(self):
        invalid_payload = {
            "embeddings": [],
            "rerank": [
                {
                    "gateway_model_name": "gateway/rerank-invalid-format",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-v3.5",
                            "request_format": "unsupported-format",
                        }
                    ],
                }
            ],
            "images_generations": [],
            "images_edits": [],
        }
        original_file_content = self.operation_rules_path.read_text(encoding="utf-8")

        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertEqual(response_json["detail"]["code"], "config_validation_failed")
        self.assertEqual(self.operation_rules_path.read_text(encoding="utf-8"), original_file_content)

    def test_post_operation_rules_structured_returns_400_for_negative_retry_count(self):
        invalid_payload = {
            "embeddings": [
                {
                    "gateway_model_name": "gateway/embed-invalid-retry",
                    "routes": [
                        {
                            "provider": "openrouter",
                            "model": "text-embedding-3-small",
                            "target_path": "/embeddings",
                            "retry_count": -1,
                        }
                    ],
                }
            ],
            "rerank": [],
            "images_generations": [],
            "images_edits": [],
        }
        original_file_content = self.operation_rules_path.read_text(encoding="utf-8")

        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertEqual(response_json["detail"]["code"], "config_validation_failed")
        self.assertEqual(self.operation_rules_path.read_text(encoding="utf-8"), original_file_content)

    def test_post_operation_rules_structured_returns_400_for_invalid_response_output_format(self):
        invalid_payload = {
            "embeddings": [],
            "rerank": [
                {
                    "gateway_model_name": "gateway/rerank-invalid-output",
                    "routes": [
                        {
                            "provider": "cohere",
                            "model": "rerank-v3.5",
                            "response_output_format": "unsupported-format",
                        }
                    ],
                }
            ],
            "images_generations": [],
            "images_edits": [],
        }
        original_file_content = self.operation_rules_path.read_text(encoding="utf-8")

        with self._client() as (client, _runtime):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertEqual(response_json["detail"]["code"], "config_validation_failed")
        self.assertEqual(self.operation_rules_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
