"""Error-signal classification and fallback-chain message helpers.

Split out of ``api/v1/chat.py`` to shrink the router module. These helpers
are pure: they only consume strings/dicts/payloads and do not touch the
request, DB, or any FastAPI state. Kept in one place because they all feed
the same pipeline — upstream error_detail → classified error_type →
human-readable summary for the fallback log.
"""
from __future__ import annotations

import copy
import json
import logging
import re

from ..config.settings import settings
from ..utils.log_redaction import redact_payload_for_log
from ..utils.text_sanitize import sanitize_payload
from .tool_schema_normalizer import normalize_openai_tools


CONTEXT_OVERFLOW_ERROR_CODE_PATTERNS = (
    "context_length_exceeded",
    "context_window_exceeded",
    "prompt_too_long",
)
CONTEXT_OVERFLOW_ERROR_MESSAGE_PATTERNS = (
    "maximum context length",
    "maximum context size",
    "prompt is too long",
    "input is too long",
    "reduce the length of the messages",
    "context overflow",
)
CONTEXT_OVERFLOW_CONTEXT_MARKERS = ("exceed", "maximum", "limit", "overflow", "too long")
CONTEXT_OVERFLOW_TOKEN_VALUE_PATTERN = re.compile(r"\b[0-9][0-9,._]*\s*[km]?\s+tokens?\b")
CONTEXT_OVERFLOW_MAXIMUM_CONTEXT_PATTERN = re.compile(r"\bmaximum\s+context\b")
HTTP_STATUS_ERROR_PATTERN = re.compile(
    r"\b(?:error|status(?:\s+code)?|http)\s+(400|401|403|404|429|5[0-9]{2})\b"
)
HTTP_SERVER_ERROR_MESSAGE_PATTERN = re.compile(r"\b(5[0-9]{2})\s+internal\s+server\s+error\b")
DOWNSTREAM_ERROR_PATTERN = re.compile(r"\bdownstream\s+error\s+(429|400|401|403|404|5[0-9]{2})\b")


def classify_error(error_detail: object | None) -> str | None:
    """Classify an error detail into a standardized error_type."""
    if not error_detail:
        return None
    if _is_context_overflow_error(error_detail):
        return "context_overflow"

    status_code = getattr(error_detail, "status_code", None)
    if isinstance(status_code, int) and (
        status_code in {400, 401, 403, 404, 429} or 500 <= status_code <= 599
    ):
        return f"http_{status_code}"

    lower = str(error_detail).lower()
    if "readtimeout" in lower or "read timed out" in lower:
        return "read_timeout"
    if "connecttimeout" in lower or "connect timed out" in lower:
        return "connect_timeout"
    if "connecterror" in lower or "connection refused" in lower:
        return "connect_error"
    # Check HTTP status codes
    if '"code":"429"' in lower:
        return "http_429"
    status_match = HTTP_STATUS_ERROR_PATTERN.search(lower)
    if status_match:
        return f"http_{status_match.group(1)}"
    server_error_match = HTTP_SERVER_ERROR_MESSAGE_PATTERN.search(lower)
    if server_error_match:
        return f"http_{server_error_match.group(1)}"
    # Check for "Downstream error XXX"
    downstream_error_match = DOWNSTREAM_ERROR_PATTERN.search(lower)
    if downstream_error_match:
        return f"http_{downstream_error_match.group(1)}"
    return "unknown"


def _format_fallback_error_summary_value(value: object) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False)


def _extract_system_message_preview(attempt_payload: dict, limit: int = 100) -> str | None:
    messages = attempt_payload.get("messages")
    if not isinstance(messages, list):
        return None

    for message in messages:
        if not isinstance(message, dict) or message.get("role") != "system":
            continue

        content = message.get("content")
        content_parts: list[str] = []
        if isinstance(content, str):
            content_parts.append(content)
        elif isinstance(content, list):
            for item in content:
                if isinstance(item, str):
                    content_parts.append(item)
                    continue
                if isinstance(item, dict):
                    text = item.get("text")
                    if isinstance(text, str):
                        content_parts.append(text)
        elif isinstance(content, dict):
            text = content.get("text")
            if isinstance(text, str):
                content_parts.append(text)

        if not content_parts:
            return None

        normalized_preview = re.sub(r"\s+", " ", "".join(content_parts)).strip()
        if not normalized_preview:
            return None
        return normalized_preview[:limit]

    return None


def _summarize_fallback_attempt_payload(
    attempt_payload: dict | None,
    is_streaming: bool | None = None,
) -> str:
    if not isinstance(attempt_payload, dict):
        return ""

    redacted_payload = redact_payload_for_log(attempt_payload)
    summary_parts: list[str] = []

    model = redacted_payload.get("model")
    if model:
        summary_parts.append(f"model={_format_fallback_error_summary_value(model)}")

    response_format = redacted_payload.get("response_format")
    if isinstance(response_format, dict):
        response_format_type = response_format.get("type")
        if response_format_type:
            summary_parts.append(f"response_format.type={_format_fallback_error_summary_value(response_format_type)}")
            if response_format_type == "json_object":
                system_preview = _extract_system_message_preview(attempt_payload)
                if system_preview:
                    summary_parts.append(f"system_preview={json.dumps(system_preview, ensure_ascii=False)}")

    stream_value = redacted_payload.get("stream", is_streaming)
    if stream_value is not None:
        summary_parts.append(f"stream={_format_fallback_error_summary_value(stream_value)}")

    for key in ("max_tokens", "temperature"):
        if key in redacted_payload:
            summary_parts.append(f"{key}={_format_fallback_error_summary_value(redacted_payload[key])}")

    provider_config = redacted_payload.get("provider")
    if isinstance(provider_config, dict) and provider_config.get("order"):
        summary_parts.append(
            f"provider.order={_format_fallback_error_summary_value(provider_config['order'])}"
        )

    return ", ".join(summary_parts)


def _build_fallback_error_message(
    error_detail: str | None,
    attempt_payload: dict | None,
    is_streaming: bool | None = None,
) -> str | None:
    if not error_detail:
        return None

    is_generic_status_code_error = re.search(
        r"\brequest failed with status code (400|401|403|404|429|500|502|503)\b",
        error_detail,
        re.IGNORECASE,
    )
    error_type = classify_error(error_detail)
    should_append_summary = bool(is_generic_status_code_error) or error_type in {
        "read_timeout",
        "connect_timeout",
        "connect_error",
    }

    if not should_append_summary:
        return error_detail

    payload_summary = _summarize_fallback_attempt_payload(attempt_payload, is_streaming=is_streaming)
    if not payload_summary:
        return error_detail

    error_message = error_detail.rstrip()
    if error_message.endswith("."):
        return f"{error_message} Request summary: {payload_summary}."
    return f"{error_message}. Request summary: {payload_summary}."


def _provider_requires_single_system_message(provider_model: str | None) -> bool:
    return isinstance(provider_model, str) and provider_model.startswith("mm.MiniMax-")


def _normalize_provider_attempt_payload(provider_payload: dict, provider_model: str | None = None) -> None:
    """Normalize payload bits that stricter OpenAI-compatible providers reject."""
    if provider_payload.get("response_format") is None:
        provider_payload.pop("response_format", None)

    if _provider_requires_single_system_message(provider_model):
        messages = provider_payload.get("messages")
        if isinstance(messages, list):
            system_indices = [
                idx
                for idx, message in enumerate(messages)
                if isinstance(message, dict) and message.get("role") == "system"
            ]
            if len(system_indices) > 1:
                merged_system_message = copy.deepcopy(messages[system_indices[0]])
                merged_parts: list[str] = []
                for idx in system_indices:
                    content = messages[idx].get("content")
                    if content is None:
                        continue
                    if isinstance(content, str):
                        merged_parts.append(content)
                    else:
                        merged_parts.append(json.dumps(content, ensure_ascii=False))
                merged_system_message["content"] = "\n\n".join(part for part in merged_parts if part)

                provider_payload["messages"] = [
                    merged_system_message if idx == system_indices[0] else message
                    for idx, message in enumerate(messages)
                    if idx == system_indices[0] or idx not in system_indices
                ]

    tools = provider_payload.get("tools")
    if not isinstance(tools, list):
        return

    normalized_tools: list[object] = []
    changed = False
    for tool in tools:
        if (
            not isinstance(tool, dict)
            or tool.get("type") != "function"
            or not isinstance(tool.get("function"), dict)
        ):
            normalized_tools.append(tool)
            continue

        function_payload = tool["function"]
        if "parameters" in function_payload and function_payload["parameters"] is not None:
            normalized_tools.append(tool)
            continue

        normalized_tool = copy.deepcopy(tool)
        normalized_tool["function"]["parameters"] = {"type": "object", "properties": {}}
        normalized_tools.append(normalized_tool)
        changed = True

    if changed:
        provider_payload["tools"] = normalized_tools

    # Rewrite JSON Schema dialects (draft-04 vs draft-07, nullable unions,
    # unsupported meta keys) to the most broadly-compatible form.
    provider_payload["tools"] = normalize_openai_tools(provider_payload["tools"])


def _log_failed_attempt_warning(
    provider_model: str,
    provider_name: str,
    error_detail: str | None,
    target_url: str,
    attempt_payload: dict,
    *,
    sub_provider: str | None = None,
) -> None:
    payload_to_log = redact_payload_for_log(
        attempt_payload,
        include_messages=settings.log_fallback_full_messages,
    )

    if sub_provider:
        logging.warning(
            "Failed attempt with model '%s' via '%s' and subprovider '%s'.\r\nError: %s\r\nTarget Url: %s\r\nPayload: %s",
            provider_model,
            provider_name,
            sub_provider,
            error_detail,
            target_url,
            payload_to_log,
        )
    else:
        logging.warning(
            "Failed attempt with model '%s' via '%s'.\r\nError: %s\r\nTarget Url: %s\r\nPayload: %s",
            provider_model,
            provider_name,
            error_detail,
            target_url,
            payload_to_log,
        )

    if not settings.log_fallback_full_messages:
        return

    messages = attempt_payload.get("messages")
    if messages is None:
        if sub_provider:
            logging.warning(
                "Failed attempt payload for model '%s' via '%s' and subprovider '%s' has no 'messages' key. Payload keys: %s",
                provider_model,
                provider_name,
                sub_provider,
                sorted(attempt_payload.keys()),
            )
        else:
            logging.warning(
                "Failed attempt payload for model '%s' via '%s' has no 'messages' key. Payload keys: %s",
                provider_model,
                provider_name,
                sorted(attempt_payload.keys()),
            )
        return

    if sub_provider:
        logging.warning(
            "Failed attempt raw messages for model '%s' via '%s' and subprovider '%s': %s",
            provider_model,
            provider_name,
            sub_provider,
            messages,
        )
    else:
        logging.warning(
            "Failed attempt raw messages for model '%s' via '%s': %s",
            provider_model,
            provider_name,
            messages,
        )


def _collect_error_signal_strings(value: object, signals: list[str]) -> None:
    if isinstance(value, str):
        stripped_value = value.strip()
        if stripped_value:
            signals.append(stripped_value)
        return

    if isinstance(value, dict):
        for key in ("error", "detail", "message", "code", "type"):
            if key in value:
                _collect_error_signal_strings(value[key], signals)
        for nested_value in value.values():
            if isinstance(nested_value, dict | list):
                _collect_error_signal_strings(nested_value, signals)
        return

    if isinstance(value, list):
        for item in value:
            _collect_error_signal_strings(item, signals)


def _extract_error_signal_candidates(error_detail: object) -> list[str]:
    raw_signals: list[str] = []
    _collect_error_signal_strings(error_detail, raw_signals)

    if isinstance(error_detail, str):
        try:
            parsed_error = sanitize_payload(json.loads(error_detail))
        except Exception:
            parsed_error = None
        if parsed_error is not None:
            _collect_error_signal_strings(parsed_error, raw_signals)

    unique_signals: list[str] = []
    seen_signals: set[str] = set()
    for signal in raw_signals:
        normalized_signal = signal.lower()
        if normalized_signal in seen_signals:
            continue
        seen_signals.add(normalized_signal)
        unique_signals.append(normalized_signal)

    return unique_signals


def _has_requested_token_overflow_signal(signal: str) -> bool:
    if "requested" not in signal or "tokens" not in signal:
        return False
    if any(marker in signal for marker in CONTEXT_OVERFLOW_CONTEXT_MARKERS):
        return True
    return bool(CONTEXT_OVERFLOW_TOKEN_VALUE_PATTERN.search(signal))


def _is_context_overflow_error(error_detail: object) -> bool:
    for signal in _extract_error_signal_candidates(error_detail):
        if any(code_pattern in signal for code_pattern in CONTEXT_OVERFLOW_ERROR_CODE_PATTERNS):
            return True

        if any(message_pattern in signal for message_pattern in CONTEXT_OVERFLOW_ERROR_MESSAGE_PATTERNS):
            return True

        if "context window" in signal and any(marker in signal for marker in CONTEXT_OVERFLOW_CONTEXT_MARKERS):
            return True

        if "context length" in signal and any(marker in signal for marker in CONTEXT_OVERFLOW_CONTEXT_MARKERS):
            return True

        if (
            CONTEXT_OVERFLOW_MAXIMUM_CONTEXT_PATTERN.search(signal)
            and CONTEXT_OVERFLOW_TOKEN_VALUE_PATTERN.search(signal)
        ):
            return True

        if _has_requested_token_overflow_signal(signal):
            return True

        if "too many tokens" in signal and any(word in signal for word in ("prompt", "input", "context")):
            return True

    return False
