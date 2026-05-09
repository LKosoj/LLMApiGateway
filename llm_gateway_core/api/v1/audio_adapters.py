import asyncio
import io
import logging
import subprocess
import wave
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from fastapi import HTTPException

from ...config.loader import AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH

logger = logging.getLogger(__name__)

NVIDIA_RIVA_GRPC_REQUEST_FORMAT = "nvidia_riva_grpc"
NVIDIA_RIVA_API_CATALOG_GRPC_URI = "grpc.nvcf.nvidia.com:443"
NVIDIA_RIVA_AUDIO_ENCODING_LINEAR_PCM = 1
NVIDIA_RIVA_API_CATALOG_DEFAULT_LANGUAGES = ("multi", "indic")
NVIDIA_RIVA_SUPPORTED_RESPONSE_FORMATS = frozenset({"json", "text", "verbose_json"})
NVIDIA_RIVA_SUPPORTED_TIMESTAMP_GRANULARITIES = frozenset({"word", "segment"})
NVIDIA_RIVA_ALLOWED_PAYLOAD_KEYS = frozenset(
    {
        "audio_channel_count",
        "boosted_lm_score",
        "boosted_lm_words",
        "custom_configuration",
        "diarization_max_speakers",
        "enable_automatic_punctuation",
        "language",
        "max_alternatives",
        "model",
        "profanity_filter",
        "response_format",
        "sample_rate_hertz",
        "speaker_diarization",
        "timestamp_granularities[]",
        "verbatim_transcripts",
    }
)
NVIDIA_RIVA_RETRYABLE_STATUS_CODES = frozenset({"deadline_exceeded", "internal", "resource_exhausted", "unavailable"})


@dataclass
class AudioAdapterResponse:
    body: dict[str, Any] | str
    content_type: str
    status_code: int = 200

    @property
    def is_json(self) -> bool:
        return self.content_type.startswith("application/json")


@dataclass
class _StreamingRecognizeEnvelope:
    results: list[Any]


@dataclass
class _NvidiaRivaCapabilities:
    supported_language_codes: list[str]
    supports_offline: bool
    supports_online: bool


def _coerce_bool(value: object, field_name: str) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized_value = value.strip().lower()
        if normalized_value in {"true", "1", "yes", "on"}:
            return True
        if normalized_value in {"false", "0", "no", "off"}:
            return False
    raise HTTPException(status_code=400, detail=f"Invalid '{field_name}' for NVIDIA audio route; expected boolean.")


def _coerce_int(value: object, field_name: str) -> int:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Invalid '{field_name}' for NVIDIA audio route; expected integer.")
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid '{field_name}' for NVIDIA audio route; expected integer.",
        ) from exc


def _coerce_float(value: object, field_name: str) -> float:
    if isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"Invalid '{field_name}' for NVIDIA audio route; expected number.")
    try:
        return float(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(
            status_code=400,
            detail=f"Invalid '{field_name}' for NVIDIA audio route; expected number.",
        ) from exc


def _coerce_string_list(value: object, field_name: str) -> list[str]:
    if isinstance(value, str):
        normalized_value = value.strip()
        return [normalized_value] if normalized_value else []
    if isinstance(value, list):
        normalized_values: list[str] = []
        for item in value:
            if not isinstance(item, str):
                raise HTTPException(
                    status_code=400,
                    detail=f"Invalid '{field_name}' for NVIDIA audio route; expected list of strings.",
                )
            stripped_item = item.strip()
            if stripped_item:
                normalized_values.append(stripped_item)
        return normalized_values
    raise HTTPException(
        status_code=400,
        detail=f"Invalid '{field_name}' for NVIDIA audio route; expected list of strings.",
    )


def _normalize_response_format(request_payload: dict[str, object]) -> str:
    value = request_payload.get("response_format")
    if value is None:
        return "json"
    if not isinstance(value, str):
        raise HTTPException(
            status_code=400,
            detail="Invalid 'response_format' for NVIDIA audio route; expected string.",
        )
    normalized_value = value.strip() or "json"
    if normalized_value not in NVIDIA_RIVA_SUPPORTED_RESPONSE_FORMATS:
        supported_values = ", ".join(sorted(NVIDIA_RIVA_SUPPORTED_RESPONSE_FORMATS))
        raise HTTPException(
            status_code=400,
            detail=(
                "NVIDIA audio route supports only these response_format values: "
                f"{supported_values}."
            ),
        )
    return normalized_value


def _normalize_timestamp_granularities(request_payload: dict[str, object]) -> list[str]:
    value = request_payload.get("timestamp_granularities[]")
    if value is None:
        return []

    normalized_values = _coerce_string_list(value, "timestamp_granularities[]")
    unsupported_values = sorted(set(normalized_values) - NVIDIA_RIVA_SUPPORTED_TIMESTAMP_GRANULARITIES)
    if unsupported_values:
        raise HTTPException(
            status_code=400,
            detail=(
                "NVIDIA audio route supports only these timestamp_granularities[] values: "
                "segment, word."
            ),
        )
    return normalized_values


def _validate_supported_payload_fields(request_payload: dict[str, object]) -> None:
    unsupported_fields: list[str] = []
    for key, value in request_payload.items():
        if key in NVIDIA_RIVA_ALLOWED_PAYLOAD_KEYS:
            continue
        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and len(value) == 0:
            continue
        unsupported_fields.append(key)

    if unsupported_fields:
        raise HTTPException(
            status_code=400,
            detail=(
                "NVIDIA audio route does not support these OpenAI request fields: "
                f"{', '.join(sorted(unsupported_fields))}."
            ),
        )


def sanitize_nvidia_riva_request_payload(request_payload: dict[str, object]) -> dict[str, object]:
    sanitized_payload: dict[str, object] = {}
    ignored_fields: list[str] = []

    for key, value in request_payload.items():
        if key in NVIDIA_RIVA_ALLOWED_PAYLOAD_KEYS:
            sanitized_payload[key] = value
            continue

        if value is None:
            continue
        if isinstance(value, str) and not value.strip():
            continue
        if isinstance(value, list) and len(value) == 0:
            continue
        ignored_fields.append(key)

    if ignored_fields:
        logger.info(
            "Ignoring unsupported OpenAI audio fields for NVIDIA route: %s",
            ", ".join(sorted(ignored_fields)),
        )

    return sanitized_payload


def _detect_wav_audio_specs(audio_bytes: bytes) -> tuple[int, int] | None:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wave_file:
            return wave_file.getframerate(), wave_file.getnchannels()
    except (wave.Error, EOFError):
        return None


def _extract_raw_pcm_audio_from_wav(audio_bytes: bytes) -> bytes:
    try:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wave_file:
            return wave_file.readframes(wave_file.getnframes())
    except (wave.Error, EOFError) as exc:
        raise HTTPException(
            status_code=500,
            detail="Internal server error: NVIDIA audio route expected normalized WAV input.",
        ) from exc


def _build_nvidia_riva_verbose_json(
    response,
    *,
    transcript: str,
    detected_language: str | None,
    include_words: bool,
    include_segments: bool,
) -> dict[str, Any]:
    words_payload: list[dict[str, Any]] = []
    segments_payload: list[dict[str, Any]] = []
    duration: float = 0.0

    for segment_index, result in enumerate(getattr(response, "results", [])):
        duration = max(duration, float(getattr(result, "audio_processed", 0.0) or 0.0))
        alternatives = getattr(result, "alternatives", [])
        if not alternatives:
            continue

        best_alternative = alternatives[0]
        alternative_words = list(getattr(best_alternative, "words", []))
        segment_start = 0.0
        segment_end = float(getattr(result, "audio_processed", 0.0) or 0.0)
        if alternative_words:
            segment_start = float(alternative_words[0].start_time) / 1000.0
            segment_end = float(alternative_words[-1].end_time) / 1000.0

        if include_words:
            for word in alternative_words:
                words_payload.append(
                    {
                        "word": getattr(word, "word", ""),
                        "start": float(getattr(word, "start_time", 0)) / 1000.0,
                        "end": float(getattr(word, "end_time", 0)) / 1000.0,
                        "confidence": float(getattr(word, "confidence", 0.0) or 0.0),
                        **(
                            {"language": getattr(word, "language_code", "")}
                            if getattr(word, "language_code", "")
                            else {}
                        ),
                        **(
                            {"speaker": int(getattr(word, "speaker_tag", 0))}
                            if getattr(word, "speaker_tag", 0)
                            else {}
                        ),
                    }
                )

        if include_segments:
            segments_payload.append(
                {
                    "id": segment_index,
                    "start": segment_start,
                    "end": segment_end,
                    "text": getattr(best_alternative, "transcript", ""),
                    "confidence": float(getattr(best_alternative, "confidence", 0.0) or 0.0),
                }
            )

    payload: dict[str, Any] = {
        "task": "transcribe",
        "text": transcript,
        "duration": duration,
    }
    if detected_language:
        payload["language"] = detected_language
    if include_words:
        payload["words"] = words_payload
    if include_segments:
        payload["segments"] = segments_payload
    return payload


def normalize_nvidia_riva_response_to_openai(
    response,
    request_payload: dict[str, object],
) -> AudioAdapterResponse:
    response_format = _normalize_response_format(request_payload)
    timestamp_granularities = set(_normalize_timestamp_granularities(request_payload))

    transcript_parts: list[str] = []
    detected_language: str | None = None
    for result in getattr(response, "results", []):
        alternatives = getattr(result, "alternatives", [])
        if not alternatives:
            continue
        best_alternative = alternatives[0]
        transcript = getattr(best_alternative, "transcript", "")
        if transcript:
            transcript_parts.append(transcript)

        alternative_languages = list(getattr(best_alternative, "language_code", []))
        if not detected_language and alternative_languages:
            detected_language = alternative_languages[0]

        if not detected_language:
            words = list(getattr(best_alternative, "words", []))
            for word in words:
                language_code = getattr(word, "language_code", "")
                if language_code:
                    detected_language = language_code
                    break

    transcript = "".join(transcript_parts).strip()
    if response_format == "text":
        return AudioAdapterResponse(
            body=transcript,
            content_type="text/plain; charset=utf-8",
        )

    if response_format == "verbose_json":
        verbose_payload = _build_nvidia_riva_verbose_json(
            response,
            transcript=transcript,
            detected_language=detected_language,
            include_words="word" in timestamp_granularities,
            include_segments="segment" in timestamp_granularities,
        )
        return AudioAdapterResponse(body=verbose_payload, content_type="application/json")

    return AudioAdapterResponse(body={"text": transcript}, content_type="application/json")


def resolve_nvidia_riva_grpc_target(provider_base_url: str) -> tuple[str, bool, bool]:
    normalized_base_url = provider_base_url.strip()
    if not normalized_base_url:
        return NVIDIA_RIVA_API_CATALOG_GRPC_URI, True, True

    if normalized_base_url == NVIDIA_RIVA_API_CATALOG_GRPC_URI:
        return NVIDIA_RIVA_API_CATALOG_GRPC_URI, True, True

    if normalized_base_url.startswith(("grpc://", "grpcs://")):
        split_result = urlsplit(normalized_base_url)
        if split_result.hostname is None:
            raise HTTPException(status_code=500, detail="Invalid NVIDIA provider baseUrl for gRPC audio route.")
        uri = split_result.hostname
        if split_result.port is not None:
            uri = f"{uri}:{split_result.port}"
        return uri, split_result.scheme == "grpcs", uri == NVIDIA_RIVA_API_CATALOG_GRPC_URI

    if "://" not in normalized_base_url and "/" not in normalized_base_url and ":" in normalized_base_url:
        return (
            normalized_base_url,
            normalized_base_url == NVIDIA_RIVA_API_CATALOG_GRPC_URI,
            normalized_base_url == NVIDIA_RIVA_API_CATALOG_GRPC_URI,
        )

    split_result = urlsplit(normalized_base_url)
    if split_result.hostname is None:
        raise HTTPException(status_code=500, detail="Invalid NVIDIA provider baseUrl for gRPC audio route.")

    if split_result.hostname == "integrate.api.nvidia.com":
        return NVIDIA_RIVA_API_CATALOG_GRPC_URI, True, True

    uri = split_result.hostname
    if split_result.port is not None:
        uri = f"{uri}:{split_result.port}"
    return uri, split_result.scheme == "https", uri == NVIDIA_RIVA_API_CATALOG_GRPC_URI


def _extract_audio_bytes(files_payload: list[tuple[str, tuple[str, bytes, str | None]]]) -> bytes:
    if not files_payload:
        raise HTTPException(status_code=400, detail="Missing 'file' in request body")

    field_name, file_payload = files_payload[0]
    if field_name != "file":
        raise HTTPException(status_code=400, detail="Unsupported multipart file field for NVIDIA audio route.")

    if len(file_payload) < 2:
        raise HTTPException(status_code=400, detail="Invalid multipart file payload for NVIDIA audio route.")

    return file_payload[1]


def _convert_audio_bytes_to_wav(audio_bytes: bytes) -> bytes:
    try:
        result = subprocess.run(
            ["ffmpeg", "-v", "error", "-i", "pipe:0", "-f", "wav", "pipe:1"],
            input=audio_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
    except FileNotFoundError as exc:
        raise HTTPException(
            status_code=500,
            detail="NVIDIA audio route requires 'ffmpeg' in PATH to convert non-WAV audio.",
        ) from exc

    if result.returncode != 0 or not result.stdout:
        raise HTTPException(
            status_code=400,
            detail="Unsupported audio format for NVIDIA audio route; failed to convert input to WAV.",
        )

    return result.stdout


def _normalize_audio_bytes_for_nvidia(audio_bytes: bytes) -> bytes:
    if _detect_wav_audio_specs(audio_bytes) is not None:
        return audio_bytes

    converted_audio_bytes = _convert_audio_bytes_to_wav(audio_bytes)
    if _detect_wav_audio_specs(converted_audio_bytes) is None:
        raise HTTPException(
            status_code=500,
            detail="Internal server error: ffmpeg conversion did not produce WAV audio.",
        )

    logger.info("Converted non-WAV audio input to WAV for NVIDIA audio route.")
    return converted_audio_bytes


async def _normalize_audio_bytes_for_nvidia_async(audio_bytes: bytes) -> bytes:
    if _detect_wav_audio_specs(audio_bytes) is not None:
        return audio_bytes

    return await asyncio.to_thread(_normalize_audio_bytes_for_nvidia, audio_bytes)


def _iter_wav_audio_chunks(audio_bytes: bytes, *, chunk_n_frames: int = 1600):
    with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
        while True:
            chunk = wav_file.readframes(chunk_n_frames)
            if not chunk:
                break
            yield chunk


def _collect_streaming_recognize_responses(responses) -> _StreamingRecognizeEnvelope:
    collected_results: list[Any] = []
    for response in responses:
        collected_results.extend(getattr(response, "results", []))
    return _StreamingRecognizeEnvelope(results=collected_results)


def _build_nvidia_riva_metadata(
    provider_api_key: str | None,
    route_custom_headers: dict[str, Any],
) -> list[list[str]]:
    metadata: list[list[str]] = []
    if provider_api_key:
        metadata.append(["authorization", f"Bearer {provider_api_key}"])

    for header_name, header_value in route_custom_headers.items():
        if header_name.lower() == "content-type":
            continue
        metadata.append([header_name, str(header_value)])

    return metadata


def _has_function_id(route_custom_headers: dict[str, Any]) -> bool:
    return any(header_name.lower() == "function-id" for header_name in route_custom_headers)


def _build_nvidia_riva_recognition_config(
    riva_client,
    request_payload: dict[str, object],
    audio_bytes: bytes,
    *,
    include_model: bool,
):
    timestamp_granularities = set(_normalize_timestamp_granularities(request_payload))
    enable_word_time_offsets = "word" in timestamp_granularities or "segment" in timestamp_granularities

    config_kwargs: dict[str, Any] = {}
    language = request_payload.get("language")
    if isinstance(language, str) and language.strip():
        config_kwargs["language_code"] = language.strip()

    if include_model:
        model = request_payload.get("model")
        if isinstance(model, str) and model.strip():
            config_kwargs["model"] = model.strip()

    if "max_alternatives" in request_payload:
        config_kwargs["max_alternatives"] = _coerce_int(request_payload["max_alternatives"], "max_alternatives")
    if "profanity_filter" in request_payload:
        config_kwargs["profanity_filter"] = _coerce_bool(request_payload["profanity_filter"], "profanity_filter")
    if "enable_automatic_punctuation" in request_payload:
        config_kwargs["enable_automatic_punctuation"] = _coerce_bool(
            request_payload["enable_automatic_punctuation"],
            "enable_automatic_punctuation",
        )
    if "verbatim_transcripts" in request_payload:
        config_kwargs["verbatim_transcripts"] = _coerce_bool(
            request_payload["verbatim_transcripts"],
            "verbatim_transcripts",
        )
    if "sample_rate_hertz" in request_payload:
        config_kwargs["sample_rate_hertz"] = _coerce_int(
            request_payload["sample_rate_hertz"],
            "sample_rate_hertz",
        )
    if "audio_channel_count" in request_payload:
        config_kwargs["audio_channel_count"] = _coerce_int(
            request_payload["audio_channel_count"],
            "audio_channel_count",
        )
    if enable_word_time_offsets:
        config_kwargs["enable_word_time_offsets"] = True

    config = riva_client.RecognitionConfig(**config_kwargs)

    detected_specs = _detect_wav_audio_specs(audio_bytes)
    if detected_specs is not None:
        sample_rate_hertz, audio_channel_count = detected_specs
        if not getattr(config, "sample_rate_hertz", 0):
            config.sample_rate_hertz = sample_rate_hertz
        if not getattr(config, "audio_channel_count", 0):
            config.audio_channel_count = audio_channel_count
        if not getattr(config, "encoding", 0):
            config.encoding = NVIDIA_RIVA_AUDIO_ENCODING_LINEAR_PCM

    boosted_lm_words = request_payload.get("boosted_lm_words")
    if boosted_lm_words is not None:
        riva_client.add_word_boosting_to_config(
            config,
            _coerce_string_list(boosted_lm_words, "boosted_lm_words"),
            _coerce_float(request_payload.get("boosted_lm_score", 4.0), "boosted_lm_score"),
        )

    if _coerce_bool(request_payload.get("speaker_diarization", False), "speaker_diarization"):
        riva_client.add_speaker_diarization_to_config(
            config,
            True,
            _coerce_int(request_payload.get("diarization_max_speakers", 8), "diarization_max_speakers"),
        )

    custom_configuration = request_payload.get("custom_configuration")
    if custom_configuration is not None:
        if isinstance(custom_configuration, dict):
            for key, value in custom_configuration.items():
                config.custom_configuration[str(key)] = str(value)
        elif isinstance(custom_configuration, str):
            riva_client.add_custom_configuration_to_config(config, custom_configuration)
        else:
            raise HTTPException(
                status_code=400,
                detail="Invalid 'custom_configuration' for NVIDIA audio route; expected string or object.",
            )

    return config


def _grpc_code_name(exc: Exception) -> str | None:
    code = getattr(exc, "code", None)
    if code is None or not callable(code):
        return None
    try:
        grpc_code = code()
    except Exception:
        return None
    return getattr(grpc_code, "name", None)


def _extract_grpc_error_detail(exc: Exception) -> str:
    details = getattr(exc, "details", None)
    if callable(details):
        try:
            message = details()
            if message:
                return str(message)
        except Exception:
            pass
    return str(exc)


def _map_grpc_error_to_http_status(exc: Exception) -> int:
    code_name = _grpc_code_name(exc)
    if code_name == "INVALID_ARGUMENT":
        return 400
    if code_name == "UNAUTHENTICATED":
        return 401
    if code_name == "PERMISSION_DENIED":
        return 403
    if code_name == "NOT_FOUND":
        return 404
    if code_name == "RESOURCE_EXHAUSTED":
        return 429
    if code_name == "DEADLINE_EXCEEDED":
        return 504
    if code_name == "INTERNAL":
        return 502
    return 503


def _is_retryable_grpc_error(exc: Exception) -> bool:
    code_name = _grpc_code_name(exc)
    if code_name is None:
        return False
    return code_name.lower() in NVIDIA_RIVA_RETRYABLE_STATUS_CODES


def _call_nvidia_riva_grpc_sync(
    *,
    grpc_uri: str,
    use_ssl: bool,
    metadata: list[list[str]],
    request_payload: dict[str, object],
    audio_bytes: bytes,
    include_model: bool,
    use_streaming: bool,
):
    try:
        import riva.client as riva_client
    except ImportError as exc:
        raise HTTPException(
            status_code=500,
            detail="NVIDIA audio route requires 'nvidia-riva-client' to be installed on the gateway.",
        ) from exc

    auth = riva_client.Auth(
        uri=grpc_uri,
        use_ssl=use_ssl,
        metadata_args=metadata,
        options=[
            ("grpc.max_receive_message_length", 64 * 1024 * 1024),
            ("grpc.max_send_message_length", 64 * 1024 * 1024),
        ],
    )
    asr_service = riva_client.ASRService(auth)
    effective_request_payload = request_payload
    effective_use_streaming = use_streaming
    if use_streaming and not include_model:
        capabilities = _describe_nvidia_riva_capabilities(
            asr_service,
            auth,
            riva_client,
        )
        effective_request_payload = _resolve_nvidia_api_catalog_request_payload(
            request_payload,
            supported_language_codes=capabilities.supported_language_codes,
        )
        effective_use_streaming = _resolve_nvidia_api_catalog_use_streaming(capabilities)
    config = _build_nvidia_riva_recognition_config(
        riva_client,
        effective_request_payload,
        audio_bytes,
        include_model=include_model,
    )
    raw_audio_bytes = _extract_raw_pcm_audio_from_wav(audio_bytes)
    if effective_use_streaming:
        streaming_config = riva_client.StreamingRecognitionConfig(config=config, interim_results=False)
        responses = asr_service.streaming_response_generator(
            audio_chunks=_iter_wav_audio_chunks(audio_bytes),
            streaming_config=streaming_config,
        )
        return _collect_streaming_recognize_responses(responses)

    return asr_service.offline_recognize(raw_audio_bytes, config)


def _describe_nvidia_riva_capabilities(
    asr_service,
    auth,
    riva_client,
) -> _NvidiaRivaCapabilities:
    config_response = asr_service.stub.GetRivaSpeechRecognitionConfig(
        riva_client.proto.riva_asr_pb2.RivaSpeechRecognitionConfigRequest(),
        metadata=auth.get_auth_metadata(),
    )
    supported_language_codes: list[str] = []
    seen_language_codes: set[str] = set()
    supports_offline = False
    supports_online = False
    for model_config in getattr(config_response, "model_config", []):
        model_parameters = getattr(model_config, "parameters", {})
        offline_flag = str(model_parameters.get("offline", "")).strip().lower()
        streaming_flag = str(model_parameters.get("streaming", "")).strip().lower()
        model_type = str(model_parameters.get("type", "")).strip().lower()

        supports_offline = supports_offline or offline_flag == "true" or model_type == "offline"
        supports_online = supports_online or streaming_flag == "true" or model_type == "online"

        for language_code in str(model_parameters.get("language_code", "")).split(","):
            normalized_language_code = language_code.strip()
            if not normalized_language_code or normalized_language_code in seen_language_codes:
                continue
            seen_language_codes.add(normalized_language_code)
            supported_language_codes.append(normalized_language_code)
    return _NvidiaRivaCapabilities(
        supported_language_codes=supported_language_codes,
        supports_offline=supports_offline,
        supports_online=supports_online,
    )


def _resolve_nvidia_api_catalog_use_streaming(capabilities: _NvidiaRivaCapabilities) -> bool:
    if capabilities.supports_offline:
        return False
    return capabilities.supports_online


def _resolve_nvidia_api_catalog_request_payload(
    request_payload: dict[str, object],
    *,
    supported_language_codes: list[str],
) -> dict[str, object]:
    language = request_payload.get("language")
    if isinstance(language, str) and language.strip():
        return request_payload

    for default_language in NVIDIA_RIVA_API_CATALOG_DEFAULT_LANGUAGES:
        if default_language in supported_language_codes:
            resolved_payload = dict(request_payload)
            resolved_payload["language"] = default_language
            logger.info(
                "Using NVIDIA API Catalog default language '%s' inferred from upstream capabilities.",
                default_language,
            )
            return resolved_payload

    supported_values = ", ".join(supported_language_codes) if supported_language_codes else "unknown"
    raise HTTPException(
        status_code=400,
        detail=(
            "NVIDIA API Catalog function requires 'language'. "
            f"Supported values: {supported_values}."
        ),
    )


async def transcribe_with_nvidia_riva_grpc(
    *,
    request_payload: dict[str, object],
    files_payload: list[tuple[str, tuple[str, bytes, str | None]]],
    provider_name: str,
    provider_base_url: str,
    provider_api_key: str | None,
    route_custom_headers: dict[str, Any],
    target_path: str,
    retry_count: int = 0,
    retry_delay: float = 0.0,
) -> AudioAdapterResponse:
    if target_path != AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH:
        raise HTTPException(
            status_code=500,
            detail=(
                "NVIDIA gRPC audio routes do not use custom target_path values. "
                "Keep '/audio/transcriptions' in route configuration."
            ),
        )

    _validate_supported_payload_fields(request_payload)
    _normalize_response_format(request_payload)
    _normalize_timestamp_granularities(request_payload)

    grpc_uri, use_ssl, is_api_catalog = resolve_nvidia_riva_grpc_target(provider_base_url)
    if is_api_catalog and not _has_function_id(route_custom_headers):
        raise HTTPException(
            status_code=400,
            detail="NVIDIA API Catalog audio routes require custom_headers.function-id.",
        )

    audio_bytes = await _normalize_audio_bytes_for_nvidia_async(_extract_audio_bytes(files_payload))
    metadata = _build_nvidia_riva_metadata(provider_api_key, route_custom_headers)
    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1
        try:
            logger.info(
                "Proxying NVIDIA audio transcription request via gRPC. provider=%s uri=%s attempt=%s/%s",
                provider_name,
                grpc_uri,
                attempt_number,
                total_attempts,
            )
            response = await asyncio.to_thread(
                _call_nvidia_riva_grpc_sync,
                grpc_uri=grpc_uri,
                use_ssl=use_ssl,
                metadata=metadata,
                request_payload=request_payload,
                audio_bytes=audio_bytes,
                include_model=not is_api_catalog,
                use_streaming=is_api_catalog,
            )
            return normalize_nvidia_riva_response_to_openai(response, request_payload)
        except HTTPException:
            raise
        except Exception as exc:
            detail = _extract_grpc_error_detail(exc)
            if attempt_number < total_attempts and _is_retryable_grpc_error(exc):
                logger.warning(
                    "Retrying NVIDIA audio gRPC request after retryable error. provider=%s uri=%s attempt=%s/%s error=%s",
                    provider_name,
                    grpc_uri,
                    attempt_number,
                    total_attempts,
                    detail,
                )
                if retry_delay > 0:
                    await asyncio.sleep(retry_delay)
                continue

            raise HTTPException(
                status_code=_map_grpc_error_to_http_status(exc),
                detail=detail or "NVIDIA audio transcription request failed.",
            ) from exc

    raise HTTPException(status_code=503, detail="NVIDIA audio transcription request failed after exhausting retries.")
