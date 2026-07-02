import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "OPENAI-KEY"
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
    "gateway_model_name": "gateway-chat-model",
    "fallback_models": [
      {
        "provider": "openai",
        "model": "gpt-4o-mini"
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
      "gateway_model_name": "gateway/embed-small",
      "routes": [
        {
          "provider": "openai",
          "model": "text-embedding-3-small",
          "target_path": "/embeddings"
        }
      ]
    }
  ],
  "rerank": [
    {
      "gateway_model_name": "gateway/rerank-v1",
      "routes": [
        {
          "provider": "cohere",
          "model": "rerank-v3.5",
          "target_path": "/score"
        }
      ]
    }
  ],
  "audio_transcriptions": [
    {
      "gateway_model_name": "gateway/audio-transcribe",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-4o-mini-transcribe"
        }
      ]
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


class _FakeDownstreamResponse:
    def __init__(
        self,
        payload,
        status_code: int = 200,
        text: str | None = None,
        content_type: str = "application/json",
    ):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)
        self.content = self.text.encode("utf-8")
        self.headers = {"content-type": content_type}

    def json(self):
        return self._payload


class OperationRulesIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openai")
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
        fake_http_client.post = AsyncMock(return_value=_FakeDownstreamResponse({"unused": True}))
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            chat_make_request_mock = stack.enter_context(
                patch("llm_gateway_core.api.v1.chat.make_llm_request", new_callable=AsyncMock)
            )

            with TestClient(main.app) as client:
                yield client, fake_http_client, chat_make_request_mock

    def test_new_operation_routes_require_same_gateway_auth(self):
        with self._client() as (client, fake_http_client, _chat_make_request_mock):
            fake_http_client.post = AsyncMock(
                side_effect=[
                    _FakeDownstreamResponse(
                        {
                            "object": "list",
                            "data": [{"object": "embedding", "embedding": [0.1, 0.2], "index": 0}],
                            "model": "text-embedding-3-small",
                        }
                    ),
                    _FakeDownstreamResponse(
                        {
                            "results": [
                                {"index": 0, "relevance_score": 0.91},
                            ]
                        }
                    ),
                    _FakeDownstreamResponse(
                        {
                            "text": "hello",
                        }
                    ),
                ]
            )

            unauthorized_embeddings = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
            )
            unauthorized_rerank = client.post(
                "/v1/rerank",
                json={"model": "gateway/rerank-v1", "query": "hello", "documents": ["Doc A"]},
            )
            unauthorized_audio = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
            )

            authorized_headers = {"Authorization": "Bearer test-gateway-key"}
            authorized_embeddings = client.post(
                "/v1/embeddings",
                json={"model": "gateway/embed-small", "input": ["hello"]},
                headers=authorized_headers,
            )
            authorized_rerank = client.post(
                "/v1/rerank",
                json={"model": "gateway/rerank-v1", "query": "hello", "documents": ["Doc A"]},
                headers=authorized_headers,
            )
            authorized_audio = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers=authorized_headers,
            )

        self.assertEqual(unauthorized_embeddings.status_code, 401)
        self.assertEqual(unauthorized_rerank.status_code, 401)
        self.assertEqual(unauthorized_audio.status_code, 401)
        self.assertEqual(authorized_embeddings.status_code, 200)
        self.assertEqual(authorized_rerank.status_code, 200)
        self.assertEqual(authorized_audio.status_code, 200)
        self.assertEqual(fake_http_client.post.await_count, 3)

    def test_chat_endpoints_continue_to_use_same_fallback_rules(self):
        with self._client() as (client, _fake_http_client, chat_make_request_mock):
            chat_make_request_mock.return_value = ({"id": "chat-success"}, None)

            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gateway-chat-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "chat-success"})
        self.assertEqual(
            chat_make_request_mock.await_args.args[1],
            "https://openai.example/v1/chat/completions",
        )
        self.assertEqual(chat_make_request_mock.await_args.args[3]["model"], "gpt-4o-mini")

    def test_models_endpoint_returns_chat_models_with_chat_capabilities(self):
        with self._client() as (client, _fake_http_client, _chat_make_request_mock):
            response = client.get(
                "/v1/models",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["object"], "list")
        models_by_id = {item["id"]: item for item in response.json()["data"]}
        self.assertIn("gateway-chat-model", models_by_id)
        self.assertEqual(models_by_id["gateway-chat-model"]["capabilities"], ["chat"])
        self.assertEqual(models_by_id["gateway-chat-model"]["type"], "text")
        self.assertEqual(
            models_by_id["gateway-chat-model"]["architecture"],
            {
                "input_modalities": ["text"],
                "output_modalities": ["text"],
            },
        )


if __name__ == "__main__":
    unittest.main()
