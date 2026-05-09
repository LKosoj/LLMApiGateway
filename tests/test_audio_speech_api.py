import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
    }
  }
]
""".strip()

VALID_FALLBACK_RULES_TEXT = """
[
  {
    "gateway_model_name": "chat-model",
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
  "embeddings": [],
  "rerank": [],
  "images_generations": [],
  "images_edits": [],
  "audio_speech": [
    {
      "gateway_model_name": "gateway/audio-speech",
      "routes": [
        {
          "provider": "openai",
          "model": "tts-1",
          "custom_body_params": {
            "voice": "nova"
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/audio-speech-alt",
      "routes": [
        {
          "provider": "openai",
          "model": "tts-alt",
          "target_path": "/audio/speech-alt",
          "voices_target_path": "/tts-alt/voices"
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
        content: bytes = b"",
        *,
        status_code: int = 200,
        content_type: str = "audio/mpeg",
        json_data=None,
    ):
        self.content = content
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self.text = content.decode("utf-8", errors="replace")
        self._json_data = json_data

    def json(self):
        if self._json_data is not None:
            return self._json_data
        raise ValueError("Payload is not JSON")


class AudioSpeechApiTests(unittest.TestCase):
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
    def _client(self, downstream_response, *, downstream_get_response=None):
        fake_http_client = Mock()
        fake_http_client.post = AsyncMock(return_value=downstream_response)
        fake_http_client.get = AsyncMock(
            return_value=downstream_get_response
            or _FakeDownstreamResponse(json_data={"voices": []}, content_type="application/json")
        )
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

            with TestClient(main.app) as client:
                dispatcher = getattr(client.app.state, "operation_dispatcher", None)
                self.assertIsInstance(dispatcher, OperationDispatcher)
                self.assertIs(client.app.state.http_client, fake_http_client)
                yield client, dispatcher, fake_http_client

    def test_audio_speech_json_request_proxies_to_downstream_and_records_usage(self):
        with self._client(_FakeDownstreamResponse(b"audio-bytes", content_type="audio/wav")) as (
            client,
            dispatcher,
            fake_http_client,
        ):
            self.assertIsNotNone(dispatcher.lookup_route("audio_speech", "gateway/audio-speech"))

            response = client.post(
                "/v1/audio/speech",
                json={
                    "model": "gateway/audio-speech",
                    "input": "Hello from the gateway",
                    "voice": "alloy",
                    "response_format": "wav",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"audio-bytes")
        self.assertEqual(response.headers["content-type"], "audio/wav")
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://openai.example/v1/audio/speech")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "tts-1",
                "input": "Hello from the gateway",
                "voice": "nova",
                "response_format": "wav",
            },
        )
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()
        call_args = dict(client.app.state.tokens_usage_db.insert_usage.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(call_args["operation"], "audio_speech")
        self.assertEqual(call_args["gateway_model"], "gateway/audio-speech")
        self.assertEqual(call_args["provider"], "openai")
        self.assertEqual(call_args["model"], "tts-1")

    def test_audio_speech_rejects_missing_input(self):
        with self._client(_FakeDownstreamResponse(b"unused")) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/speech",
                json={"model": "gateway/audio-speech"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'input' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_audio_speech_unknown_model(self):
        with self._client(_FakeDownstreamResponse(b"unused")) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/speech",
                json={"model": "unknown-speech-model", "input": "hello"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"],
            "No audio speech route configured for model 'unknown-speech-model'.",
        )
        fake_http_client.post.assert_not_awaited()

    def test_audio_voices_for_model_proxies_to_downstream_and_normalizes(self):
        voices_response = _FakeDownstreamResponse(
            json_data={
                "voices": [
                    {"id": "xenia", "name": "Xenia", "model": "tts-1", "provider": "silero"},
                    {"id": "en_0", "name": "en_0", "model": "tts-alt", "provider": "silero"},
                ],
                "preset_voices": ["xenia", "en_0"],
                "default_voice": "xenia",
            },
            content_type="application/json",
        )
        with self._client(_FakeDownstreamResponse(b"unused"), downstream_get_response=voices_response) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            response = client.get(
                "/v1/audio/voices?model=gateway/audio-speech",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "audio.voice_list",
                "model": "gateway/audio-speech",
                "data": [
                    {
                        "id": "xenia",
                        "name": "Xenia",
                        "model": "tts-1",
                        "provider": "silero",
                        "gender": "female",
                        "language": "ru",
                        "default": True,
                    }
                ],
            },
        )
        fake_http_client.get.assert_awaited_once()
        self.assertEqual(fake_http_client.get.await_args.args[0], "https://openai.example/v1/voices")
        self.assertEqual(fake_http_client.get.await_args.kwargs["headers"], {"Authorization": "Bearer DIRECT-KEY"})
        fake_http_client.post.assert_not_awaited()

    def test_audio_voices_for_model_keeps_unscoped_downstream_voices(self):
        voices_response = _FakeDownstreamResponse(
            json_data={
                "custom_voices": [{"id": "clone-1", "name": "Clone"}],
                "default_voice": "clone-1",
            },
            content_type="application/json",
        )
        with self._client(_FakeDownstreamResponse(b"unused"), downstream_get_response=voices_response) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            response = client.get(
                "/v1/audio/voices?model=gateway/audio-speech",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["data"],
            [
                {
                    "id": "clone-1",
                    "name": "Clone",
                    "source": "custom",
                    "default": True,
                }
            ],
        )
        fake_http_client.get.assert_awaited_once()

    def test_audio_voices_catalog_returns_model_to_voices_map(self):
        first_response = _FakeDownstreamResponse(
            json_data={"custom_voices": [{"id": "clone-1", "name": "Clone"}]},
            content_type="application/json",
        )
        second_response = _FakeDownstreamResponse(
            json_data={"preset_voices": ["aidar"]},
            content_type="application/json",
        )
        with self._client(_FakeDownstreamResponse(b"unused")) as (client, _dispatcher, fake_http_client):
            fake_http_client.get.side_effect = [first_response, second_response]
            response = client.get(
                "/v1/audio/voices",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "object": "audio.voice_catalog",
                "data": {
                    "gateway/audio-speech": [
                        {
                            "id": "clone-1",
                            "name": "Clone",
                            "source": "custom",
                        }
                    ],
                    "gateway/audio-speech-alt": [
                        {
                            "id": "aidar",
                            "name": "aidar",
                            "source": "preset",
                            "gender": "male",
                            "language": "ru",
                        }
                    ],
                },
            },
        )
        self.assertEqual(
            [call.args[0] for call in fake_http_client.get.await_args_list],
            ["https://openai.example/v1/voices", "https://openai.example/v1/tts-alt/voices"],
        )

    def test_audio_voices_unknown_model(self):
        with self._client(_FakeDownstreamResponse(b"unused")) as (client, _dispatcher, fake_http_client):
            response = client.get(
                "/v1/audio/voices?model=unknown-speech-model",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"],
            "No audio speech route configured for model 'unknown-speech-model'.",
        )
        fake_http_client.get.assert_not_awaited()
        fake_http_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
