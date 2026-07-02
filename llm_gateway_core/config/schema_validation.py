from __future__ import annotations

import re
from typing import Any, Dict, List


SECURITY_HEADERS = frozenset({"authorization", "cookie", "x-api-key"})
FORBIDDEN_OPERATION_BODY_PARAMS = frozenset({"stream", "messages", "tool_choice", "tools", "model"})
RERANK_DEFAULT_TARGET_PATH = "/score"
IMAGES_EDITS_DEFAULT_TARGET_PATH = "/images/edits"
AUDIO_SPEECH_DEFAULT_TARGET_PATH = "/audio/speech"
AUDIO_TRANSCRIPTIONS_DEFAULT_TARGET_PATH = "/audio/transcriptions"
PDF_CONVERSIONS_DEFAULT_TARGET_PATH = "/api"
REQUEST_FORMAT_QUERY_PASSAGES = "query_passages"
REQUEST_FORMAT_QUERY_TEXTS = "query_texts"
REQUEST_FORMAT_OPENAI_IMAGES = "openai_images"
REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART = "openai_images_multipart"
REQUEST_FORMAT_NVIDIA_GENAI_JSON = "nvidia_genai_json"
REQUEST_FORMAT_NVIDIA_RIVA_GRPC = "nvidia_riva_grpc"
RESPONSE_FORMAT_RANKINGS_LOGIT = "rankings_logit"
RESPONSE_FORMAT_SCORES = "scores"
RESPONSE_FORMAT_OPENAI_IMAGES = "openai_images"
RESPONSE_FORMAT_NVIDIA_ARTIFACTS = "nvidia_artifacts"
RESPONSE_OUTPUT_FORMAT_JINA_RESULTS = "jina_results"
SUPPORTED_REQUEST_FORMATS = frozenset(
    {
        REQUEST_FORMAT_QUERY_PASSAGES,
        REQUEST_FORMAT_QUERY_TEXTS,
        REQUEST_FORMAT_OPENAI_IMAGES,
        REQUEST_FORMAT_OPENAI_IMAGES_MULTIPART,
        REQUEST_FORMAT_NVIDIA_GENAI_JSON,
        REQUEST_FORMAT_NVIDIA_RIVA_GRPC,
    }
)
SUPPORTED_RESPONSE_FORMATS = frozenset(
    {
        RESPONSE_FORMAT_RANKINGS_LOGIT,
        RESPONSE_FORMAT_SCORES,
        RESPONSE_FORMAT_OPENAI_IMAGES,
        RESPONSE_FORMAT_NVIDIA_ARTIFACTS,
    }
)
SUPPORTED_RESPONSE_OUTPUT_FORMATS = frozenset({RESPONSE_OUTPUT_FORMAT_JINA_RESULTS})


def validate_non_empty_string(value: str, field_name: str) -> str:
    normalized_value = value.strip()
    if not normalized_value:
        raise ValueError(f"'{field_name}' must not be empty.")
    return normalized_value


def validate_target_path(value: str) -> str:
    normalized_value = value.strip()
    if normalized_value.startswith(("http://", "https://")):
        return normalized_value
    if not normalized_value.startswith("/"):
        raise ValueError("'target_path' must start with '/' or be an absolute http(s) URL.")
    return normalized_value


def validate_route_format(value: str | None, field_name: str, allowed_values: frozenset[str]) -> str | None:
    if value is None:
        return None

    normalized_value = value.strip()
    if not normalized_value:
        return None

    if normalized_value not in allowed_values:
        allowed_values_text = ", ".join(sorted(allowed_values))
        raise ValueError(f"'{field_name}' must be one of: {allowed_values_text}.")

    return normalized_value


def validate_non_negative_number(value: int | float | None, field_name: str) -> int | float | None:
    if value is None:
        return None

    if value < 0:
        raise ValueError(f"'{field_name}' must be greater than or equal to 0.")

    return value


def validate_custom_headers(headers: Dict[str, Any]) -> Dict[str, Any]:
    blocked_headers = sorted(header for header in headers if header.lower() in SECURITY_HEADERS)
    if blocked_headers:
        raise ValueError(
            "custom_headers must not contain protected headers: Authorization, Cookie, X-Api-Key."
        )
    return headers


def validate_custom_body_params(body_params: Dict[str, Any]) -> Dict[str, Any]:
    blocked_keys = sorted(param for param in body_params if param.lower() in FORBIDDEN_OPERATION_BODY_PARAMS)
    if blocked_keys:
        raise ValueError(
            "custom_body_params must not contain reserved keys: stream, messages, tool_choice, tools, model."
        )
    return body_params


def validate_json_object(value: Dict[str, Any] | None, field_name: str) -> Dict[str, Any] | None:
    if value is None:
        return None

    if not isinstance(value, dict):
        raise ValueError(f"'{field_name}' must be a JSON object.")

    return value


def validate_provider_name_list(value: Any, field_name: str) -> List[str] | None:
    if value is None:
        return None

    if not isinstance(value, list):
        raise ValueError(f"'{field_name}' must be a list of provider names.")

    if not value:
        raise ValueError(f"'{field_name}' must not be empty when provided.")

    normalized_values: list[str] = []
    seen_values: set[str] = set()
    for provider_name in value:
        if not isinstance(provider_name, str):
            raise ValueError(f"'{field_name}' must contain only provider name strings.")
        normalized_provider_name = provider_name.strip()
        if not normalized_provider_name:
            raise ValueError(f"'{field_name}' must not contain empty provider names.")
        if normalized_provider_name in seen_values:
            raise ValueError(f"'{field_name}' must not contain duplicate provider names.")
        seen_values.add(normalized_provider_name)
        normalized_values.append(normalized_provider_name)

    return normalized_values


def validate_header_name(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    normalized_value = validate_non_empty_string(value, field_name)
    if not re.fullmatch(r"[A-Za-z0-9-]+", normalized_value):
        raise ValueError(f"'{field_name}' must contain only letters, digits, and hyphens.")
    return normalized_value


def empty_operation_rules() -> Dict[str, Dict[str, Any]]:
    return {
        "embeddings": {},
        "rerank": {},
        "images_generations": {},
        "images_edits": {},
        "audio_speech": {},
        "audio_transcriptions": {},
        "web_search": {},
        "web_read": {},
        "web_research": {},
        "web_deep_research": {},
        "pdf_conversions": {},
    }
