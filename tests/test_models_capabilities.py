import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader
from tests.chat_accounting_test_support import install_main_chat_accounting_double


VALID_PROVIDERS_TEXT = """
[
  {
    "openrouter": {
      "baseUrl": "https://openrouter.example/v1",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

VALID_FALLBACK_RULES_TEXT = """
[
  {
    "gateway_model_name": "gateway/chat-only",
    "fallback_models": [
      {
        "provider": "openrouter",
        "model": "chat-provider-only"
      }
    ],
    "rotate_models": false
  },
  {
    "gateway_model_name": "gateway/chat-and-embeddings",
    "fallback_models": [
      {
        "provider": "openrouter",
        "model": "chat-provider-shared"
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
      "gateway_model_name": "gateway/embeddings-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "embed-model-v1",
          "target_path": "/embeddings"
        }
      ]
    },
    {
      "gateway_model_name": "gateway/chat-and-embeddings",
      "routes": [
        {
          "provider": "openrouter",
          "model": "embed-model-shared",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "gateway/rerank-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "rerank-model-v1"
        }
      ]
    }
  ],
  "images_generations": [
    {
      "gateway_model_name": "gateway/image-generation-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "gpt-image-1",
          "target_path": "/images/generations"
        }
      ]
    }
  ],
  "images_edits": [
    {
      "gateway_model_name": "gateway/image-edit-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "gpt-image-1"
        }
      ]
    }
  ],
  "audio_speech": [
    {
      "gateway_model_name": "gateway/audio-speech-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "tts-1"
        }
      ]
    }
  ],
  "audio_transcriptions": [
    {
      "gateway_model_name": "gateway/audio-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "gpt-4o-mini-transcribe"
        }
      ]
    }
  ],
  "pdf_conversions": [
    {
      "gateway_model_name": "gateway/pdf-conversion-only",
      "routes": [
        {
          "provider": "openrouter",
          "model": "pdf-converter"
        }
      ]
    }
  ],
  "web_search": [
    {
      "gateway_model_name": "gateway/web-search-only",
      "query_model": "gateway/chat-only"
    }
  ],
  "web_read": [
    {
      "gateway_model_name": "gateway/web-read-only"
    }
  ],
  "web_research": [
    {
      "gateway_model_name": "gateway/web-research-only",
      "search_model": "gateway/web-search-only",
      "read_model": "gateway/web-read-only",
      "rerank_model": "gateway/rerank-only",
      "analysis_model": "gateway/chat-only"
    }
  ],
  "web_deep_research": [
    {
      "gateway_model_name": "gateway/web-deep-research-only",
      "search_model": "gateway/web-search-only",
      "read_model": "gateway/web-read-only",
      "fast_model": "gateway/chat-only",
      "smart_model": "gateway/chat-only",
      "strategic_model": "gateway/chat-only",
      "embedding_model": "gateway/embeddings-only"
    }
  ]
}
""".strip()


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FallbackModelsResponse:
    def __init__(self):
        self.status_code = 200
        self.text = '{"data":[{"id":"raw-provider-only","object":"model"}]}'

    def json(self):
        return {
            "data": [
                {
                    "id": "raw-provider-only",
                    "object": "model",
                }
            ]
        }


class ModelsCapabilitiesTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.fusion_rules_path = Path(self.temp_dir.name) / "models_fusion_rules.json"
        self.model_rules_path = Path(self.temp_dir.name) / "models_model_rules.json"
        self.router_rules_path = Path(self.temp_dir.name) / "models_router_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fusion_rules_path.write_text("[]", encoding="utf-8")
        self.model_rules_path.write_text("{}", encoding="utf-8")
        self.router_rules_path.write_text(
            """[
  {
    "gateway_model_name": "gateway/router",
    "selector_model": "gateway/chat-only",
    "targets": [
      {"type": "gateway_model", "model": "gateway/chat-only"}
    ]
  }
]""",
            encoding="utf-8",
        )
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openrouter")
        self.fallback_provider_patcher.start()

        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
            fusion_rules_filename=str(self.fusion_rules_path),
            model_rules_filename=str(self.model_rules_path),
            router_rules_filename=str(self.router_rules_path),
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
        fake_http_client.get = AsyncMock(return_value=_FallbackModelsResponse())
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
            install_main_chat_accounting_double(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("main.create_shared_http_client", return_value=fake_http_client)
            )
            stack.enter_context(
                patch(
                    "main.ConfigUpdateCoordinator",
                    return_value=config_update_coordinator,
                )
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openrouter"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_models_endpoint_merges_capabilities_from_chat_embeddings_and_rerank_rules(self):
        with self._client() as (client, fake_http_client):
            response = client.get("/v1/models", headers={"Authorization": "Bearer test-gateway-key"})

        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertEqual(response_json["object"], "list")
        self.assertIsInstance(response_json["data"], list)

        models_by_id = {item["id"]: item for item in response_json["data"]}
        self.assertEqual(models_by_id["gateway/chat-only"]["capabilities"], ["chat"])
        self.assertEqual(models_by_id["gateway/chat-only"]["type"], "text")
        self.assertEqual(models_by_id["gateway/router"]["capabilities"], ["chat"])
        self.assertEqual(models_by_id["gateway/router"]["type"], "text")
        self.assertEqual(
            models_by_id["gateway/chat-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        )
        self.assertEqual(models_by_id["gateway/embeddings-only"]["capabilities"], ["embeddings"])
        self.assertEqual(models_by_id["gateway/embeddings-only"]["type"], "embeddings")
        self.assertEqual(
            models_by_id["gateway/embeddings-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
            },
        )
        self.assertEqual(models_by_id["gateway/rerank-only"]["capabilities"], ["rerank"])
        self.assertEqual(models_by_id["gateway/rerank-only"]["type"], "rerank")
        self.assertEqual(
            models_by_id["gateway/rerank-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        )
        self.assertEqual(models_by_id["gateway/image-generation-only"]["capabilities"], ["images"])
        self.assertEqual(models_by_id["gateway/image-generation-only"]["type"], "image")
        self.assertEqual(models_by_id["gateway/image-generation-only"]["image_operations"], ["generation"])
        self.assertEqual(
            models_by_id["gateway/image-generation-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["image"],
            },
        )
        self.assertEqual(models_by_id["gateway/image-edit-only"]["capabilities"], ["images"])
        self.assertEqual(models_by_id["gateway/image-edit-only"]["type"], "image")
        self.assertEqual(models_by_id["gateway/image-edit-only"]["image_operations"], ["edit"])
        self.assertEqual(
            models_by_id["gateway/image-edit-only"]["architecture"],
            {
                "input_modalities": ["text", "image"],
                "output_modalities": ["image"],
            },
        )
        self.assertEqual(models_by_id["gateway/audio-only"]["capabilities"], ["audio_transcription"])
        self.assertEqual(models_by_id["gateway/audio-only"]["type"], "audio")
        self.assertEqual(
            models_by_id["gateway/audio-only"]["architecture"],
            {
                "input_modalities": ["audio"],
                "output_modalities": ["text"],
            },
        )
        self.assertEqual(models_by_id["gateway/audio-speech-only"]["capabilities"], ["audio_speech"])
        self.assertEqual(models_by_id["gateway/audio-speech-only"]["type"], "audio")
        self.assertEqual(
            models_by_id["gateway/audio-speech-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["audio"],
            },
        )
        self.assertEqual(models_by_id["gateway/pdf-conversion-only"]["capabilities"], ["pdf_conversion"])
        self.assertEqual(models_by_id["gateway/pdf-conversion-only"]["type"], "document")
        self.assertEqual(
            models_by_id["gateway/pdf-conversion-only"]["architecture"],
            {
                "input_modalities": ["document"],
                "output_modalities": ["document"],
            },
        )
        self.assertEqual(models_by_id["gateway/web-search-only"]["capabilities"], ["web_search"])
        self.assertEqual(models_by_id["gateway/web-search-only"]["type"], "web")
        self.assertEqual(models_by_id["gateway/web-search-only"]["web_operations"], ["search"])
        self.assertEqual(
            models_by_id["gateway/web-search-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        )
        self.assertEqual(models_by_id["gateway/web-read-only"]["capabilities"], ["web_read"])
        self.assertEqual(models_by_id["gateway/web-read-only"]["type"], "web")
        self.assertEqual(models_by_id["gateway/web-read-only"]["web_operations"], ["read"])
        self.assertEqual(models_by_id["gateway/web-research-only"]["capabilities"], ["web_research"])
        self.assertEqual(models_by_id["gateway/web-research-only"]["type"], "web")
        self.assertEqual(models_by_id["gateway/web-research-only"]["web_operations"], ["research"])
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["capabilities"], ["web_deep_research"])
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["type"], "web")
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["web_operations"], ["deep_research"])
        self.assertEqual(models_by_id["gateway/chat-and-embeddings"]["capabilities"], ["chat", "embeddings"])
        self.assertNotIn("type", models_by_id["gateway/chat-and-embeddings"])
        self.assertEqual(
            models_by_id["gateway/chat-and-embeddings"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["text", "embeddings"],
            },
        )
        self.assertNotIn("capabilities", models_by_id["raw-provider-only"])
        self.assertNotIn("type", models_by_id["raw-provider-only"])
        fake_http_client.get.assert_awaited_once()

    def test_models_endpoint_in_anthropic_format_preserves_gateway_metadata_markers(self):
        with self._client() as (client, fake_http_client):
            response = client.get(
                "/v1/models",
                headers={
                    "X-Api-Key": "test-gateway-key",
                    "anthropic-version": "2023-06-01",
                },
            )

        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertIsInstance(response_json["data"], list)

        models_by_id = {item["id"]: item for item in response_json["data"]}
        self.assertEqual(models_by_id["gateway/embeddings-only"]["type"], "model")
        self.assertEqual(models_by_id["gateway/embeddings-only"]["gateway_type"], "embeddings")
        self.assertEqual(models_by_id["gateway/embeddings-only"]["capabilities"], ["embeddings"])
        self.assertEqual(
            models_by_id["gateway/embeddings-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
            },
        )
        self.assertEqual(models_by_id["gateway/chat-only"]["gateway_type"], "text")
        self.assertEqual(models_by_id["gateway/chat-only"]["capabilities"], ["chat"])
        self.assertEqual(models_by_id["gateway/router"]["gateway_type"], "text")
        self.assertEqual(models_by_id["gateway/router"]["capabilities"], ["chat"])
        self.assertEqual(models_by_id["gateway/image-edit-only"]["gateway_type"], "image")
        self.assertEqual(models_by_id["gateway/image-edit-only"]["capabilities"], ["images"])
        self.assertEqual(models_by_id["gateway/image-edit-only"]["image_operations"], ["edit"])
        self.assertEqual(
            models_by_id["gateway/image-edit-only"]["architecture"],
            {
                "input_modalities": ["text", "image"],
                "output_modalities": ["image"],
            },
        )
        self.assertEqual(models_by_id["gateway/audio-only"]["gateway_type"], "audio")
        self.assertEqual(models_by_id["gateway/audio-only"]["capabilities"], ["audio_transcription"])
        self.assertEqual(
            models_by_id["gateway/audio-only"]["architecture"],
            {
                "input_modalities": ["audio"],
                "output_modalities": ["text"],
            },
        )
        self.assertEqual(models_by_id["gateway/audio-speech-only"]["gateway_type"], "audio")
        self.assertEqual(models_by_id["gateway/audio-speech-only"]["capabilities"], ["audio_speech"])
        self.assertEqual(
            models_by_id["gateway/audio-speech-only"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["audio"],
            },
        )
        self.assertEqual(models_by_id["gateway/pdf-conversion-only"]["gateway_type"], "document")
        self.assertEqual(models_by_id["gateway/pdf-conversion-only"]["capabilities"], ["pdf_conversion"])
        self.assertEqual(
            models_by_id["gateway/pdf-conversion-only"]["architecture"],
            {
                "input_modalities": ["document"],
                "output_modalities": ["document"],
            },
        )
        self.assertEqual(models_by_id["gateway/web-research-only"]["gateway_type"], "web")
        self.assertEqual(models_by_id["gateway/web-research-only"]["capabilities"], ["web_research"])
        self.assertEqual(models_by_id["gateway/web-research-only"]["web_operations"], ["research"])
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["gateway_type"], "web")
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["capabilities"], ["web_deep_research"])
        self.assertEqual(models_by_id["gateway/web-deep-research-only"]["web_operations"], ["deep_research"])
        self.assertNotIn("gateway_type", models_by_id["gateway/chat-and-embeddings"])
        self.assertEqual(models_by_id["gateway/chat-and-embeddings"]["capabilities"], ["chat", "embeddings"])
        self.assertNotIn("capabilities", models_by_id["raw-provider-only"])
        fake_http_client.get.assert_awaited_once()

    def test_specific_model_in_anthropic_format_preserves_gateway_metadata_markers(self):
        with self._client() as (client, _fake_http_client):
            response = client.get(
                "/v1/models/gateway/embeddings-only",
                headers={
                    "X-Api-Key": "test-gateway-key",
                    "anthropic-version": "2023-06-01",
                },
            )

        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertEqual(response_json["id"], "gateway/embeddings-only")
        self.assertEqual(response_json["type"], "model")
        self.assertEqual(response_json["gateway_type"], "embeddings")
        self.assertEqual(response_json["capabilities"], ["embeddings"])
        self.assertEqual(
            response_json["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["embeddings"],
            },
        )


if __name__ == "__main__":
    unittest.main()
