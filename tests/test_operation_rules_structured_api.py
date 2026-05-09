import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader


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


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class OperationRulesStructuredApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(INITIAL_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
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
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openrouter"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_get_operation_rules_structured_returns_current_structure(self):
        with self._client() as (client, _):
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
        with self._client() as (client, _):
            post_response = client.post(
                "/v1/config/model-operations/structured",
                json=UPDATED_OPERATION_RULES_PAYLOAD,
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            get_response = client.get(
                "/v1/config/model-operations/structured",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(post_response.status_code, 200)
        self.assertEqual(get_response.status_code, 200)
        self.assertEqual(get_response.json(), UPDATED_OPERATION_RULES_RESPONSE)
        self.assertEqual(
            client.app.state.operation_rules,
            self.config_loader.operation_rules,
        )

        persisted_payload = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        self.assertEqual(persisted_payload, UPDATED_OPERATION_RULES_RESPONSE)

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

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertIn("detail", response_json)
        self.assertIn("errors", response_json["detail"])
        self.assertTrue(response_json["detail"]["errors"])
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

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertIn("detail", response_json)
        self.assertIn("errors", response_json["detail"])
        self.assertTrue(response_json["detail"]["errors"])
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

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertIn("detail", response_json)
        self.assertIn("errors", response_json["detail"])
        self.assertTrue(response_json["detail"]["errors"])
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

        with self._client() as (client, _):
            response = client.post(
                "/v1/config/model-operations/structured",
                json=invalid_payload,
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        response_json = response.json()
        self.assertIn("detail", response_json)
        self.assertIn("errors", response_json["detail"])
        self.assertTrue(response_json["detail"]["errors"])
        self.assertEqual(self.operation_rules_path.read_text(encoding="utf-8"), original_file_content)


if __name__ == "__main__":
    unittest.main()
