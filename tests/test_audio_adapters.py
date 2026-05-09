import io
import shutil
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

from fastapi import HTTPException

from llm_gateway_core.api.v1.audio_adapters import (
    _NvidiaRivaCapabilities,
    _build_nvidia_riva_recognition_config,
    _extract_raw_pcm_audio_from_wav,
    _iter_wav_audio_chunks,
    _resolve_nvidia_api_catalog_request_payload,
    _resolve_nvidia_api_catalog_use_streaming,
    normalize_nvidia_riva_response_to_openai,
    resolve_nvidia_riva_grpc_target,
    sanitize_nvidia_riva_request_payload,
    transcribe_with_nvidia_riva_grpc,
)
from tests._async_compat import run_async


class _FakeWord:
    def __init__(
        self,
        word: str,
        start_time: int,
        end_time: int,
        confidence: float,
        *,
        language_code: str = "",
        speaker_tag: int = 0,
    ) -> None:
        self.word = word
        self.start_time = start_time
        self.end_time = end_time
        self.confidence = confidence
        self.language_code = language_code
        self.speaker_tag = speaker_tag


class _FakeAlternative:
    def __init__(
        self,
        transcript: str,
        *,
        confidence: float = 0.0,
        words: list[_FakeWord] | None = None,
        language_code: list[str] | None = None,
    ) -> None:
        self.transcript = transcript
        self.confidence = confidence
        self.words = words or []
        self.language_code = language_code or []


class _FakeResult:
    def __init__(self, alternative: _FakeAlternative, *, audio_processed: float) -> None:
        self.alternatives = [alternative]
        self.audio_processed = audio_processed


class _FakeResponse:
    def __init__(self, results: list[_FakeResult]) -> None:
        self.results = results


class _FakeRecognitionConfig:
    def __init__(self, **kwargs) -> None:
        self.custom_configuration = {}
        for key, value in kwargs.items():
            setattr(self, key, value)


class _FakeRivaClient:
    RecognitionConfig = _FakeRecognitionConfig

    @staticmethod
    def add_word_boosting_to_config(config, words, score) -> None:
        return None

    @staticmethod
    def add_speaker_diarization_to_config(config, enabled, max_speakers) -> None:
        return None

    @staticmethod
    def add_custom_configuration_to_config(config, custom_configuration) -> None:
        return None


class AudioAdaptersTests(unittest.TestCase):
    @staticmethod
    def _build_wav_bytes() -> bytes:
        buffer = io.BytesIO()
        with wave.open(buffer, "wb") as wav_file:
            wav_file.setnchannels(1)
            wav_file.setsampwidth(2)
            wav_file.setframerate(16000)
            wav_file.writeframes(b"\x00\x00" * 160)
        return buffer.getvalue()

    def test_resolve_nvidia_riva_grpc_target_uses_api_catalog_for_integrate_base_url(self):
        grpc_uri, use_ssl, is_api_catalog = resolve_nvidia_riva_grpc_target("https://integrate.api.nvidia.com/v1")

        self.assertEqual(grpc_uri, "grpc.nvcf.nvidia.com:443")
        self.assertTrue(use_ssl)
        self.assertTrue(is_api_catalog)

    def test_resolve_nvidia_riva_grpc_target_parses_custom_https_base_url(self):
        grpc_uri, use_ssl, is_api_catalog = resolve_nvidia_riva_grpc_target("https://speech.example:50051")

        self.assertEqual(grpc_uri, "speech.example:50051")
        self.assertTrue(use_ssl)
        self.assertFalse(is_api_catalog)

    def test_normalize_nvidia_riva_response_to_openai_builds_verbose_json_words_and_segments(self):
        response = _FakeResponse(
            [
                _FakeResult(
                    _FakeAlternative(
                        "hello world",
                        confidence=0.97,
                        words=[
                            _FakeWord("hello", 0, 400, 0.95, language_code="en-US", speaker_tag=1),
                            _FakeWord("world", 450, 900, 0.93, language_code="en-US", speaker_tag=1),
                        ],
                        language_code=["en-US"],
                    ),
                    audio_processed=0.9,
                )
            ]
        )

        normalized = normalize_nvidia_riva_response_to_openai(
            response,
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "response_format": "verbose_json",
                "timestamp_granularities[]": ["word", "segment"],
            },
        )

        self.assertTrue(normalized.is_json)
        self.assertEqual(normalized.body["text"], "hello world")
        self.assertEqual(normalized.body["language"], "en-US")
        self.assertEqual(normalized.body["duration"], 0.9)
        self.assertEqual(normalized.body["words"][0]["word"], "hello")
        self.assertEqual(normalized.body["words"][0]["start"], 0.0)
        self.assertEqual(normalized.body["segments"][0]["text"], "hello world")
        self.assertEqual(normalized.body["segments"][0]["end"], 0.9)

    def test_normalize_nvidia_riva_response_to_openai_returns_text_body(self):
        response = _FakeResponse(
            [_FakeResult(_FakeAlternative("plain transcript"), audio_processed=1.2)]
        )

        normalized = normalize_nvidia_riva_response_to_openai(
            response,
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "response_format": "text",
            },
        )

        self.assertFalse(normalized.is_json)
        self.assertEqual(normalized.body, "plain transcript")
        self.assertEqual(normalized.content_type, "text/plain; charset=utf-8")

    def test_sanitize_nvidia_riva_request_payload_ignores_unsupported_openai_fields(self):
        sanitized = sanitize_nvidia_riva_request_payload(
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "response_format": "json",
                "temperature": "0",
                "prompt": "ignore me",
                "language": "ru",
            }
        )

        self.assertEqual(
            sanitized,
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "response_format": "json",
                "language": "ru",
            },
        )

    def test_build_nvidia_riva_recognition_config_omits_model_for_api_catalog(self):
        config = _build_nvidia_riva_recognition_config(
            _FakeRivaClient,
            {"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
            self._build_wav_bytes(),
            include_model=False,
        )

        self.assertFalse(hasattr(config, "model"))
        self.assertEqual(config.sample_rate_hertz, 16000)
        self.assertEqual(config.audio_channel_count, 1)
        self.assertEqual(config.encoding, 1)

    def test_iter_wav_audio_chunks_strips_wav_container_header(self):
        chunks = list(_iter_wav_audio_chunks(self._build_wav_bytes(), chunk_n_frames=80))

        self.assertGreater(len(chunks), 1)
        self.assertFalse(chunks[0].startswith(b"RIFF"))

    def test_extract_raw_pcm_audio_from_wav_returns_frame_bytes_without_container_header(self):
        raw_audio = _extract_raw_pcm_audio_from_wav(self._build_wav_bytes())

        self.assertEqual(raw_audio, b"\x00\x00" * 160)
        self.assertFalse(raw_audio.startswith(b"RIFF"))

    def test_resolve_nvidia_api_catalog_request_payload_defaults_to_multi_when_upstream_supports_it(self):
        resolved_payload = _resolve_nvidia_api_catalog_request_payload(
            {"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
            supported_language_codes=["en", "ru", "multi"],
        )

        self.assertEqual(
            resolved_payload,
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "language": "multi",
            },
        )

    def test_resolve_nvidia_api_catalog_request_payload_defaults_to_indic_when_upstream_supports_it(self):
        resolved_payload = _resolve_nvidia_api_catalog_request_payload(
            {"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
            supported_language_codes=["en-US", "indic"],
        )

        self.assertEqual(
            resolved_payload,
            {
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "language": "indic",
            },
        )

    def test_resolve_nvidia_api_catalog_request_payload_requires_language_without_indic_capability(self):
        with self.assertRaises(HTTPException) as context:
            _resolve_nvidia_api_catalog_request_payload(
                {"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
                supported_language_codes=["en-US", "hi-IN"],
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("requires 'language'", context.exception.detail)
        self.assertIn("en-US, hi-IN", context.exception.detail)

    def test_resolve_nvidia_api_catalog_use_streaming_prefers_offline_when_available(self):
        use_streaming = _resolve_nvidia_api_catalog_use_streaming(
            _NvidiaRivaCapabilities(
                supported_language_codes=["ru", "multi"],
                supports_offline=True,
                supports_online=True,
            )
        )

        self.assertFalse(use_streaming)

    def test_resolve_nvidia_api_catalog_use_streaming_keeps_online_when_offline_unavailable(self):
        use_streaming = _resolve_nvidia_api_catalog_use_streaming(
            _NvidiaRivaCapabilities(
                supported_language_codes=["en-US"],
                supports_offline=False,
                supports_online=True,
            )
        )

        self.assertTrue(use_streaming)

    def test_transcribe_with_nvidia_riva_grpc_rejects_unsupported_merged_payload_fields(self):
        with self.assertRaises(HTTPException) as context:
            run_async(
                transcribe_with_nvidia_riva_grpc(
                    request_payload={
                        "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                        "prompt": "unsupported prompt",
                    },
                    files_payload=[("file", ("sample.wav", b"wave-bytes", "audio/wav"))],
                    provider_name="nvidia",
                    provider_base_url="https://integrate.api.nvidia.com/v1",
                    provider_api_key="NVIDIA-KEY",
                    route_custom_headers={"function-id": "func-123"},
                    target_path="/audio/transcriptions",
                )
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertIn("prompt", context.exception.detail)

    def test_transcribe_with_nvidia_riva_grpc_requires_function_id_for_api_catalog(self):
        with self.assertRaises(HTTPException) as context:
            run_async(
                transcribe_with_nvidia_riva_grpc(
                    request_payload={"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
                    files_payload=[("file", ("sample.wav", b"wave-bytes", "audio/wav"))],
                    provider_name="nvidia",
                    provider_base_url="https://integrate.api.nvidia.com/v1",
                    provider_api_key="NVIDIA-KEY",
                    route_custom_headers={},
                    target_path="/audio/transcriptions",
                )
            )

        self.assertEqual(context.exception.status_code, 400)
        self.assertEqual(
            context.exception.detail,
            "NVIDIA API Catalog audio routes require custom_headers.function-id.",
        )

    def test_transcribe_with_nvidia_riva_grpc_uses_streaming_for_api_catalog(self):
        captured_call_kwargs: dict[str, object] = {}

        def fake_call_nvidia_riva_grpc_sync(**kwargs):
            captured_call_kwargs.update(kwargs)
            return _FakeResponse([_FakeResult(_FakeAlternative("streamed"), audio_processed=1.0)])

        with patch(
            "llm_gateway_core.api.v1.audio_adapters._call_nvidia_riva_grpc_sync",
            side_effect=fake_call_nvidia_riva_grpc_sync,
        ):
            response = run_async(
                transcribe_with_nvidia_riva_grpc(
                    request_payload={"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
                    files_payload=[("file", ("sample.wav", self._build_wav_bytes(), "audio/wav"))],
                    provider_name="nvidia",
                    provider_base_url="https://integrate.api.nvidia.com/v1",
                    provider_api_key="NVIDIA-KEY",
                    route_custom_headers={"function-id": "func-123"},
                    target_path="/audio/transcriptions",
                )
            )

        self.assertEqual(response.body, {"text": "streamed"})
        self.assertIs(captured_call_kwargs["use_streaming"], True)

    @unittest.skipUnless(shutil.which("ffmpeg"), "ffmpeg is required for MP3->WAV conversion test")
    def test_transcribe_with_nvidia_riva_grpc_converts_mp3_input_to_wav(self):
        captured_audio_bytes: dict[str, bytes] = {}

        def fake_call_nvidia_riva_grpc_sync(**kwargs):
            captured_audio_bytes["audio_bytes"] = kwargs["audio_bytes"]
            return _FakeResponse([_FakeResult(_FakeAlternative("converted"), audio_processed=1.0)])

        with patch(
            "llm_gateway_core.api.v1.audio_adapters._call_nvidia_riva_grpc_sync",
            side_effect=fake_call_nvidia_riva_grpc_sync,
        ):
            response = run_async(
                transcribe_with_nvidia_riva_grpc(
                    request_payload={"model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"},
                    files_payload=[
                        (
                            "file",
                            (
                                "test.mp3",
                                Path(__file__).with_name("test.mp3").read_bytes(),
                                "audio/mpeg",
                            ),
                        )
                    ],
                    provider_name="nvidia",
                    provider_base_url="https://speech.example:50051",
                    provider_api_key="NVIDIA-KEY",
                    route_custom_headers={},
                    target_path="/audio/transcriptions",
                )
            )

        self.assertEqual(response.body, {"text": "converted"})
        with wave.open(io.BytesIO(captured_audio_bytes["audio_bytes"]), "rb") as wav_file:
            self.assertEqual(wav_file.getframerate(), 16000)
            self.assertEqual(wav_file.getnchannels(), 1)


if __name__ == "__main__":
    unittest.main()
