import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.audio_speakers import merge_transcription_with_diarization
from llm_gateway_core.api.v1.models import _add_gateway_operation_models
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.config.schemas import PayloadTransformConfig
from llm_gateway_core.services.accounting import (
    build_operation_cost_calculator_registry,
    classify_billing_policy,
)
from llm_gateway_core.services.payload_transform import apply_payload_transforms
from tests.images_audio_accounting_test_support import (
    install_images_audio_accounting_passthrough,
)

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32
AUTH = {"Authorization": "Bearer test-gateway-key"}

PROVIDERS_TEXT = json.dumps(
    [
        {"openai": {"baseUrl": "https://openai.example/v1", "apikey": "DIRECT-KEY"}},
        {"neuraldeep": {"baseUrl": "https://neuraldeep.example/v1", "apikey": "ND-KEY"}},
    ]
)
FALLBACK_RULES_TEXT = json.dumps(
    [
        {
            "gateway_model_name": "chat-model",
            "fallback_models": [{"provider": "openai", "model": "gpt-4o-mini"}],
            "rotate_models": False,
        }
    ]
)
OPERATION_RULES = {
    "audio_speech": [
        {
            "gateway_model_name": "gateway/qwen3-tts",
            "routes": [
                {
                    "provider": "neuraldeep",
                    "model": "qwen3-tts",
                    "payload_transforms": {
                        "value_maps": {"language": {"ru": "Russian", "en": "English"}},
                    },
                }
            ],
        }
    ],
    "audio_transcriptions": [
        {
            "gateway_model_name": "gateway/stt",
            "routes": [{"provider": "openai", "model": "whisper-1"}],
        }
    ],
    "audio_diarizations": [
        {
            "gateway_model_name": "gateway/diarizations",
            "routes": [{"provider": "neuraldeep", "model": "nemotron-3-diarization"}],
        }
    ],
    "audio_speaker_transcriptions": [
        {
            "gateway_model_name": "gateway/stt-speakers",
            "transcription_model": "gateway/stt",
            "diarization_model": "gateway/diarizations",
        }
    ],
}

TRANSCRIPTION_PAYLOAD = {
    "text": "Привет. Как дела? Отлично.",
    "language": "ru",
    "duration": 9.0,
    "segments": [
        {"id": 0, "start": 0.0, "end": 2.0, "text": " Привет."},
        {"id": 1, "start": 3.0, "end": 5.0, "text": "Как дела?"},
        {"id": 2, "start": 6.0, "end": 9.0, "text": "Отлично."},
    ],
}
DIARIZATION_PAYLOAD = {
    "duration": 9.0,
    "num_speakers": 2,
    "segments": [
        {"speaker": "SPEAKER_0", "start": 0.1, "end": 2.1},
        {"speaker": "SPEAKER_1", "start": 2.9, "end": 4.0},
        {"speaker": "SPEAKER_0", "start": 4.0, "end": 4.5},
        {"speaker": "SPEAKER_1", "start": 6.2, "end": 8.8},
    ],
}


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FakeResponse:
    def __init__(self, payload, *, status_code=200, content_type="application/json"):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if isinstance(payload, bytes):
            self.content = payload
        else:
            self.content = json.dumps(payload).encode("utf-8")
        self.text = self.content.decode("utf-8", errors="replace")
        self._payload = payload

    def json(self):
        return self._payload


class AudioDiarizationApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        root = Path(self.temp_dir.name)
        files = {
            "providers.json": PROVIDERS_TEXT,
            "models_fallback_rules.json": FALLBACK_RULES_TEXT,
            "models_operation_rules.json": json.dumps(OPERATION_RULES),
            "models_fusion_rules.json": "[]",
            "models_router_rules.json": "[]",
            "models_model_rules.json": "{}",
        }
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
        self.fallback_provider_patcher = patch(
            "llm_gateway_core.config.loader.settings.fallback_provider",
            "openai",
        )
        self.fallback_provider_patcher.start()
        self.config_loader = ConfigLoader(
            providers_filename=str(root / "providers.json"),
            fallback_rules_filename=str(root / "models_fallback_rules.json"),
            operation_rules_filename=str(root / "models_operation_rules.json"),
            fusion_rules_filename=str(root / "models_fusion_rules.json"),
            router_rules_filename=str(root / "models_router_rules.json"),
            model_rules_filename=str(root / "models_model_rules.json"),
        ).load_complete()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self, post_side_effect):
        fake_http_client = Mock()
        fake_http_client.post = AsyncMock(side_effect=post_side_effect)
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        with ExitStack() as stack:
            install_images_audio_accounting_passthrough(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.create_shared_http_client", return_value=fake_http_client))
            stack.enter_context(patch("main.ConfigUpdateCoordinator", return_value=config_update_coordinator))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))
            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_diarize_proxies_multipart_to_default_target_path(self):
        with self._client([_FakeResponse(DIARIZATION_PAYLOAD)]) as (client, fake_http_client):
            response = client.post(
                "/v1/audio/diarize",
                data={"model": "gateway/diarizations", "min_duration": "0.3"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), DIARIZATION_PAYLOAD)
        call = fake_http_client.post.await_args
        self.assertEqual(call.args[0], "https://neuraldeep.example/v1/audio/diarize")
        self.assertEqual(call.kwargs["data"], {"model": "nemotron-3-diarization", "min_duration": "0.3"})
        self.assertEqual(call.kwargs["files"][0][0], "file")

    def test_diarize_passes_rttm_text_through(self):
        rttm = b"SPEAKER audio 1 0.02 1.63 <NA> <NA> SPEAKER_0 <NA> <NA>\n"
        with self._client([_FakeResponse(rttm, content_type="text/plain")]) as (client, _fake):
            response = client.post(
                "/v1/audio/diarize",
                data={"model": "gateway/diarizations", "response_format": "rttm"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, rttm)
        self.assertEqual(response.headers["content-type"], "text/plain")

    def test_diarize_unknown_model_returns_404(self):
        with self._client([]) as (client, fake_http_client):
            response = client.post(
                "/v1/audio/diarize",
                data={"model": "gateway/stt"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 404)
        self.assertIn("No audio diarization route", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_speaker_transcription_calls_both_models_and_merges_by_time(self):
        seen = {}

        async def fake_post(url, **kwargs):
            file_obj = kwargs["files"][0][1][1]
            seen[url] = {"data": kwargs["data"], "file": file_obj, "bytes": file_obj.read()}
            if url.endswith("/audio/diarize"):
                return _FakeResponse(DIARIZATION_PAYLOAD)
            return _FakeResponse(TRANSCRIPTION_PAYLOAD)

        with self._client(fake_post) as (client, _fake):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/stt-speakers", "language": "ru", "response_format": "json"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["text"], "SPEAKER_0: Привет.\nSPEAKER_1: Как дела? Отлично.")
        self.assertEqual([segment["speaker"] for segment in body["segments"]], ["SPEAKER_0", "SPEAKER_1", "SPEAKER_1"])
        self.assertEqual(body["num_speakers"], 2)
        self.assertEqual(body["language"], "ru")

        stt_call = seen["https://openai.example/v1/audio/transcriptions"]
        diarize_call = seen["https://neuraldeep.example/v1/audio/diarize"]
        self.assertEqual(
            stt_call["data"],
            {
                "model": "whisper-1",
                "language": "ru",
                "response_format": "verbose_json",
                "timestamp_granularities[]": "segment",
            },
        )
        self.assertEqual(diarize_call["data"], {"model": "nemotron-3-diarization"})
        self.assertEqual(stt_call["bytes"], WAV_BYTES)
        self.assertEqual(diarize_call["bytes"], WAV_BYTES)
        self.assertIsNot(stt_call["file"], diarize_call["file"])

    def test_speaker_transcription_text_format_returns_plain_text(self):
        async def fake_post(url, **_kwargs):
            if url.endswith("/audio/diarize"):
                return _FakeResponse(DIARIZATION_PAYLOAD)
            return _FakeResponse(TRANSCRIPTION_PAYLOAD)

        with self._client(fake_post) as (client, _fake):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/stt-speakers", "response_format": "text"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers["content-type"].startswith("text/plain"))
        self.assertEqual(response.text, "SPEAKER_0: Привет.\nSPEAKER_1: Как дела? Отлично.")

    def test_speaker_transcription_rejects_unsupported_response_format(self):
        with self._client([]) as (client, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/stt-speakers", "response_format": "srt"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Speaker transcription supports only", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_speaker_transcription_fails_when_diarization_fails(self):
        async def fake_post(url, **_kwargs):
            if url.endswith("/audio/diarize"):
                return _FakeResponse({"detail": "bad audio"}, status_code=422)
            return _FakeResponse(TRANSCRIPTION_PAYLOAD)

        with self._client(fake_post) as (client, _fake):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/stt-speakers"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 422)

    def test_speaker_transcription_fails_without_timestamped_segments(self):
        async def fake_post(url, **_kwargs):
            if url.endswith("/audio/diarize"):
                return _FakeResponse(DIARIZATION_PAYLOAD)
            return _FakeResponse({"text": "no segments"})

        with self._client(fake_post) as (client, _fake):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/stt-speakers"},
                files={"file": ("meeting.wav", WAV_BYTES, "audio/wav")},
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 502)
        self.assertIn("segments", response.json()["detail"])

    def test_speech_route_maps_language_codes_via_value_maps(self):
        with self._client([_FakeResponse(b"RIFF....", content_type="audio/wav")]) as (client, fake_http_client):
            response = client.post(
                "/v1/audio/speech",
                json={
                    "model": "gateway/qwen3-tts",
                    "input": "Привет",
                    "voice": "ryan",
                    "language": "ru",
                    "instructions": "спокойный диктор",
                },
                headers=AUTH,
            )

        self.assertEqual(response.status_code, 200, response.text)
        sent_payload = fake_http_client.post.await_args.kwargs["json"]
        self.assertEqual(sent_payload["language"], "Russian")
        self.assertEqual(sent_payload["instructions"], "спокойный диктор")
        self.assertEqual(sent_payload["model"], "qwen3-tts")


class SpeakerMergeTests(unittest.TestCase):
    def test_segment_without_overlap_takes_nearest_speaker(self):
        merged = merge_transcription_with_diarization(
            {"segments": [{"start": 10.0, "end": 11.0, "text": "эхо"}]},
            {"segments": [{"speaker": "SPEAKER_0", "start": 0.0, "end": 1.0},
                          {"speaker": "SPEAKER_1", "start": 12.0, "end": 13.0}]},
        )
        self.assertEqual(merged["segments"][0]["speaker"], "SPEAKER_1")

    def test_no_speaker_turns_labels_text_unknown(self):
        merged = merge_transcription_with_diarization(
            {"segments": [{"start": 0.0, "end": 1.0, "text": "тишина"}]},
            {"segments": []},
        )
        self.assertIsNone(merged["segments"][0]["speaker"])
        self.assertEqual(merged["text"], "UNKNOWN: тишина")
        self.assertEqual(merged["num_speakers"], 0)

    def test_malformed_diarization_segment_is_rejected(self):
        with self.assertRaises(HTTPException) as context:
            merge_transcription_with_diarization(
                {"segments": []},
                {"segments": [{"start": 0.0, "end": 1.0}]},
            )
        self.assertEqual(context.exception.status_code, 502)


class AudioDiarizationConfigTests(unittest.TestCase):
    def setUp(self):
        self.config_loader = ConfigLoader()
        self.providers = {
            "neuraldeep": ProviderDetails(baseUrl="https://neuraldeep.example/v1", apikey="DIRECT-KEY"),
        }

    def _validate(self, rules):
        operation_config = self.config_loader._build_operation_config(rules)
        self.config_loader.validate_operation_routes(operation_config, providers_config=self.providers, fallback_rules={})
        return operation_config

    def test_diarization_route_gets_default_target_path(self):
        operation_config = self._validate(
            {"audio_diarizations": OPERATION_RULES["audio_diarizations"]}
        )
        route = operation_config["audio_diarizations"]["gateway/diarizations"]["routes"][0]
        self.assertEqual(route["target_path"], "/audio/diarize")

    def test_speaker_transcription_requires_known_references(self):
        rules = {
            "audio_transcriptions": [
                {"gateway_model_name": "gateway/stt", "routes": [{"provider": "neuraldeep", "model": "gigaam-v3"}]}
            ],
            "audio_diarizations": OPERATION_RULES["audio_diarizations"],
            "audio_speaker_transcriptions": [
                {
                    "gateway_model_name": "gateway/stt-speakers",
                    "transcription_model": "gateway/stt",
                    "diarization_model": "gateway/missing",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "unknown diarization_model 'gateway/missing'"):
            self._validate(rules)

        rules["audio_speaker_transcriptions"][0]["diarization_model"] = "gateway/diarizations"
        rules["audio_speaker_transcriptions"][0]["transcription_model"] = "gateway/missing"
        with self.assertRaisesRegex(ValueError, "unknown transcription_model 'gateway/missing'"):
            self._validate(rules)

    def test_speaker_transcription_name_must_not_shadow_transcription_model(self):
        rules = {
            "audio_transcriptions": [
                {"gateway_model_name": "gateway/stt", "routes": [{"provider": "neuraldeep", "model": "gigaam-v3"}]}
            ],
            "audio_diarizations": OPERATION_RULES["audio_diarizations"],
            "audio_speaker_transcriptions": [
                {
                    "gateway_model_name": "gateway/stt",
                    "transcription_model": "gateway/stt",
                    "diarization_model": "gateway/diarizations",
                }
            ],
        }
        with self.assertRaisesRegex(ValueError, "duplicates a gateway model in audio_transcriptions"):
            self._validate(rules)

    def test_speaker_transcription_rejects_routes_field(self):
        with self.assertRaises(ValueError):
            self.config_loader._build_operation_config(
                {
                    "audio_speaker_transcriptions": [
                        {
                            "gateway_model_name": "gateway/stt-speakers",
                            "transcription_model": "gateway/stt",
                            "diarization_model": "gateway/diarizations",
                            "routes": [],
                        }
                    ]
                }
            )

    def test_models_capabilities_and_billing(self):
        gateway_models: dict = {}
        _add_gateway_operation_models(
            gateway_models,
            {
                "audio_diarizations": {"gateway/diarizations": {}},
                "audio_speaker_transcriptions": {"gateway/stt-speakers": {}},
            },
        )
        self.assertEqual(gateway_models["gateway/diarizations"]["capabilities"], ["audio_diarization"])
        self.assertEqual(gateway_models["gateway/stt-speakers"]["capabilities"], ["audio_transcription"])
        policy = classify_billing_policy("POST", "/v1/audio/diarize")
        self.assertEqual(policy.operation, "audio_diarization")

    def test_cost_calculators_are_registered_for_new_sections(self):
        registry = build_operation_cost_calculator_registry(
            {
                "audio_diarizations": {"gateway/diarizations": {"cost_calculator": {"unit": "operation", "rate_usd": 0.01}}},
                "audio_speaker_transcriptions": {
                    "gateway/stt-speakers": {"cost_calculator": {"unit": "operation", "rate_usd": 0.02}}
                },
            }
        )
        self.assertIn(("audio_diarization", "gateway/diarizations"), registry)
        self.assertIn(("audio_transcription", "gateway/stt-speakers"), registry)


class PayloadValueMapsTests(unittest.TestCase):
    def test_value_maps_translate_only_known_string_values(self):
        transforms = {"value_maps": {"language": {"ru": "Russian"}}}
        self.assertEqual(apply_payload_transforms({"language": "ru"}, transforms), {"language": "Russian"})
        self.assertEqual(apply_payload_transforms({"language": "xx"}, transforms), {"language": "xx"})
        self.assertEqual(apply_payload_transforms({}, transforms), {})

    def test_value_maps_must_be_objects(self):
        with self.assertRaises(ValueError):
            PayloadTransformConfig.model_validate({"value_maps": {"language": "Russian"}})
        with self.assertRaises(ValueError):
            PayloadTransformConfig.model_validate({"value_maps": {"model": {"a": "b"}}})


if __name__ == "__main__":
    unittest.main()
