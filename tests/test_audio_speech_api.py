import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import audio as audio_api
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.images_audio_accounting_test_support import (
    install_images_audio_accounting_passthrough,
)
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


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
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
            install_images_audio_accounting_passthrough(stack)
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
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

            with TestClient(main.app) as client:
                services = client.app.state.services
                self.assertIs(services.http_client, fake_http_client)
                lease = client.portal.call(services.runtime_manager.acquire_current)
                try:
                    dispatcher = lease.snapshot.operation_dispatcher
                    self.assertIsInstance(dispatcher, OperationDispatcher)
                    yield client, dispatcher, fake_http_client
                finally:
                    client.portal.call(lease.release)

    def test_audio_speech_json_request_proxies_to_downstream_and_records_usage(self):
        with self._client(_FakeDownstreamResponse(b"audio-bytes", content_type="audio/wav")) as (
            client,
            dispatcher,
            fake_http_client,
        ):
            tokens_usage_db = client.app.state.services.tokens_usage_db
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
        tokens_usage_db.insert_usage.assert_not_called()

    def test_neuraldeep_music_returns_completed_audio_through_speech_endpoint(self):
        providers = json.loads(VALID_PROVIDERS_TEXT)
        providers.append({"neuraldeep": dict(providers[0]["openai"])})
        self.providers_path.write_text(json.dumps(providers), encoding="utf-8")
        rules = json.loads(VALID_OPERATION_RULES_TEXT)
        rules["audio_speech"][0]["routes"][0].update(
            provider="neuraldeep", model="lyria-3-pro", target_path="/music/generate", custom_body_params={},
        )
        self.operation_rules_path.write_text(json.dumps(rules), encoding="utf-8")
        self.config_loader.load_complete()
        with self._client(_FakeDownstreamResponse(json_data={"task_id": "track-1"})) as (
            client, _dispatcher, fake_http_client,
        ):
            fake_http_client.get.side_effect = [
                _FakeDownstreamResponse(json_data={"status": "succeeded"}),
                _FakeDownstreamResponse(b"completed-music", content_type="audio/wav"),
            ]
            response = client.post(
                "/v1/audio/speech",
                json={"model": "gateway/audio-speech", "input": "Jazz", "response_format": "wav"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.content, b"completed-music")
            self.assertEqual(response.headers["content-type"], "audio/wav")
            fake_http_client.post.assert_awaited_once()
            self.assertEqual(fake_http_client.post.await_args.args[0], "https://openai.example/v1/music/generate")
            self.assertEqual(fake_http_client.post.await_args.kwargs["json"], {"model": "lyria-3-pro", "prompt": "Jazz"})
            self.assertEqual(fake_http_client.get.await_count, 2)
            fake_http_client.get.reset_mock()
            voices = client.get(
                "/v1/audio/voices?model=gateway/audio-speech",
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            self.assertEqual(voices.status_code, 200)
            self.assertEqual(voices.json()["data"], [])
            fake_http_client.get.assert_not_awaited()

    def _configure_music_fallback(self, *, primary_target_path="/music/generate"):
        providers = json.loads(VALID_PROVIDERS_TEXT)
        providers.extend(
            [
                {"neuraldeep": {"baseUrl": "https://neuraldeep.example/v1", "apikey": "ND-KEY"}},
                {
                    "sa3": {
                        "baseUrl": "https://sa3.example/v1",
                        "apikey": "SA3-KEY",
                    }
                },
            ]
        )
        self.providers_path.write_text(json.dumps(providers), encoding="utf-8")
        rules = json.loads(VALID_OPERATION_RULES_TEXT)
        rules["audio_speech"][0]["routes"] = [
            {
                "provider": "neuraldeep",
                "model": "lyria-3-pro",
                "target_path": primary_target_path,
                "custom_body_params": {},
            },
            {
                "provider": "sa3",
                "model": "sa3-medium",
                "target_path": "/audio/speech",
                "custom_body_params": {"seconds": 30},
            },
        ]
        self.operation_rules_path.write_text(json.dumps(rules), encoding="utf-8")
        self.config_loader.load_complete()

    def test_neuraldeep_music_falls_back_to_sa3_on_unavailable_primary(self):
        for primary_failure in (
            _FakeDownstreamResponse(status_code=429),
            _FakeDownstreamResponse(status_code=503),
            httpx.ConnectError(
                "NeuralDeep unavailable",
                request=httpx.Request("POST", "https://neuraldeep.example/v1/music/generate"),
            ),
        ):
            with self.subTest(primary_failure=primary_failure):
                self._configure_music_fallback()
                with self._client(primary_failure) as (client, _dispatcher, fake_http_client):
                    fake_http_client.post.side_effect = [
                        primary_failure,
                        _FakeDownstreamResponse(b"sa3-music", content_type="audio/mpeg"),
                    ]
                    response = client.post(
                        "/v1/audio/speech",
                        json={
                            "model": "gateway/audio-speech",
                            "input": "lofi house loop, warm Rhodes piano",
                            "audio_duration": 45,
                            "response_format": "mp3",
                        },
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.content, b"sa3-music")
                self.assertEqual(fake_http_client.post.await_count, 2)
                self.assertEqual(fake_http_client.post.await_args_list[1].args[0], "https://sa3.example/v1/audio/speech")
                self.assertEqual(
                    fake_http_client.post.await_args_list[1].kwargs["headers"]["Authorization"],
                    "Bearer SA3-KEY",
                )
                self.assertEqual(
                    fake_http_client.post.await_args_list[1].kwargs["json"],
                    {
                        "model": "sa3-medium",
                        "input": "lofi house loop, warm Rhodes piano",
                        "seconds": 45,
                        "response_format": "mp3",
                    },
                )

    def test_music_quota_cooldown_skips_neuraldeep_on_the_next_request(self):
        self._configure_music_fallback()
        quota = _FakeDownstreamResponse(status_code=429, json_data={"error": {"type": "quota_exceeded"}})
        quota.headers["retry-after"] = "120"
        with self._client(quota) as (client, _dispatcher, fake_http_client):
            fake_http_client.post.side_effect = [
                quota, _FakeDownstreamResponse(b"sa3-music", content_type="audio/mpeg"),
            ]
            payload = {"model": "gateway/audio-speech", "input": "lofi music"}
            headers = {"Authorization": "Bearer test-gateway-key"}
            first = client.post("/v1/audio/speech", json=payload, headers=headers)
            self.assertEqual(first.status_code, 200)
            self.assertEqual(fake_http_client.post.await_count, 2)
            fake_http_client.post.reset_mock()
            fake_http_client.post.side_effect = None
            fake_http_client.post.return_value = _FakeDownstreamResponse(b"sa3-again", content_type="audio/mpeg")
            second = client.post("/v1/audio/speech", json=payload, headers=headers)
            self.assertEqual(second.status_code, 200)
            self.assertEqual(second.content, b"sa3-again")
            fake_http_client.post.assert_awaited_once()
            self.assertEqual(fake_http_client.post.await_args.args[0], "https://sa3.example/v1/audio/speech")

    def test_music_fallback_uses_default_seconds_and_drops_music_only_fields(self):
        self._configure_music_fallback()
        with self._client(_FakeDownstreamResponse(status_code=503)) as (client, _dispatcher, fake_http_client):
            fake_http_client.post.side_effect = [
                _FakeDownstreamResponse(status_code=503),
                _FakeDownstreamResponse(b"sa3-music", content_type="audio/mpeg"),
            ]
            response = client.post(
                "/v1/audio/speech",
                json={
                    "model": "gateway/audio-speech",
                    "input": "lofi house loop",
                    "lyrics": "[instrumental]",
                    "vocal_language": "en",
                    "bpm": 90,
                    "response_format": "mp3",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fake_http_client.post.await_args_list[1].kwargs["json"], {
            "model": "sa3-medium",
            "input": "lofi house loop",
            "seconds": 30,
            "response_format": "mp3",
        })

    def test_music_client_errors_do_not_fallback_on_nonavailability_statuses(self):
        for status_code in (400, 401, 402, 422):
            with self.subTest(status_code=status_code):
                self._configure_music_fallback()
                with self._client(_FakeDownstreamResponse(status_code=status_code)) as (
                    client,
                    _dispatcher,
                    fake_http_client,
                ):
                    response = client.post(
                        "/v1/audio/speech",
                        json={"model": "gateway/audio-speech", "input": "music"},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                self.assertEqual(response.status_code, status_code)
                fake_http_client.post.assert_awaited_once()

    def test_successful_neuraldeep_music_does_not_call_sa3(self):
        self._configure_music_fallback()
        with self._client(_FakeDownstreamResponse(json_data={"task_id": "track-1"})) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            fake_http_client.get.side_effect = [
                _FakeDownstreamResponse(json_data={"status": "succeeded"}),
                _FakeDownstreamResponse(b"neuraldeep-music", content_type="audio/mpeg"),
            ]
            response = client.post(
                "/v1/audio/speech",
                json={"model": "gateway/audio-speech", "input": "music"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"neuraldeep-music")
        fake_http_client.post.assert_awaited_once()

    def test_non_neuraldeep_speech_uses_only_first_route(self):
        self._configure_music_fallback(primary_target_path="/audio/speech")
        for downstream_response, expected_status in (
            (_FakeDownstreamResponse(b"speech", content_type="audio/mpeg"), 200),
            (_FakeDownstreamResponse(status_code=503), 503),
        ):
            with self.subTest(expected_status=expected_status), self._client(downstream_response) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/audio/speech",
                    json={"model": "gateway/audio-speech", "input": "speech"},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

            self.assertEqual(response.status_code, expected_status)
            fake_http_client.post.assert_awaited_once()
            self.assertEqual(fake_http_client.post.await_args.args[0], "https://neuraldeep.example/v1/audio/speech")

    def test_music_fallback_returns_final_error_when_sa3_also_fails(self):
        self._configure_music_fallback()
        with self._client(_FakeDownstreamResponse(status_code=503)) as (client, _dispatcher, fake_http_client):
            fake_http_client.post.side_effect = [
                _FakeDownstreamResponse(status_code=503),
                _FakeDownstreamResponse(status_code=503),
            ]
            response = client.post(
                "/v1/audio/speech",
                json={"model": "gateway/audio-speech", "input": "music"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(fake_http_client.post.await_count, 2)

    def test_audio_speech_preserves_llm_enhance_boolean_for_each_route(self):
        for model in ("gateway/audio-speech", "gateway/audio-speech-alt"):
            for enabled in (False, True):
                with self.subTest(model=model, enabled=enabled), self._client(
                    _FakeDownstreamResponse(b"audio-bytes", content_type="audio/wav")
                ) as (client, _dispatcher, fake_http_client):
                    response = client.post(
                        "/v1/audio/speech",
                        json={"model": model, "input": "Hello", "llm_enhance": enabled},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                    self.assertEqual(response.status_code, 200)
                    self.assertEqual(response.content, b"audio-bytes")
                    fake_http_client.post.assert_awaited_once()
                    payload = fake_http_client.post.await_args.kwargs["json"]
                    self.assertIs(payload["llm_enhance"], enabled)

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

    def test_audio_speech_forwards_multi_speaker_turns_unchanged(self):
        turns = [
            {"text": "Добрый вечер!", "voice": "speaker-a"},
            {"text": "Здравствуйте.", "voice": "speaker-b"},
        ]
        with self._client(_FakeDownstreamResponse(b"audio-bytes", content_type="audio/mpeg")) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            response = client.post(
                "/v1/audio/speech",
                json={"model": "gateway/audio-speech", "input": turns, "voice": "speaker-a"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"audio-bytes")
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.kwargs["json"]["input"], turns)

    def test_audio_speech_rejects_invalid_multi_speaker_turns(self):
        for bad_input in ([], [{"voice": "speaker-a"}], [{"text": "  "}], ["plain string"]):
            with self.subTest(input=bad_input), self._client(_FakeDownstreamResponse(b"unused")) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/audio/speech",
                    json={"model": "gateway/audio-speech", "input": bad_input},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["detail"], "'input' list must contain turn objects with non-empty 'text'"
                )
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

    def test_audio_voice_models_use_snapshot_over_conflicting_legacy_operation_rules(self):
        services = make_app_services()
        snapshot = make_runtime_snapshot(
            config_loader=self.config_loader,
            http_client=services.http_client,
        )
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    services=services,
                    operation_rules={audio_api.AUDIO_SPEECH_SECTION: {"legacy/audio-speech": {}}},
                )
            ),
            state=SimpleNamespace(runtime_snapshot=snapshot, api_key_record=None),
        )

        self.assertEqual(
            audio_api._allowed_audio_speech_model_names(request),
            ["gateway/audio-speech", "gateway/audio-speech-alt"],
        )

    def test_audio_runtime_fails_closed_without_snapshot(self):
        services = make_app_services()
        request = SimpleNamespace(
            app=SimpleNamespace(state=SimpleNamespace(services=services)),
            state=SimpleNamespace(),
        )

        with self.assertRaises(AttributeError):
            audio_api._get_operation_runtime(request)
        with self.assertRaises(AttributeError):
            audio_api._allowed_audio_speech_model_names(request)

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
