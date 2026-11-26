import json
from collections.abc import Mapping
from typing import Any

from .provider_error_redaction import redact_provider_error_text


REDACTED_VALUE = "<REDACTED>"
REDACTED_SECRET_VALUE = "***REDACTED***"
REDACTED_BODY_PLACEHOLDER = "<REDACTED REQUEST BODY>"
SENSITIVE_HEADER_NAMES = {
    "authorization",
    "cookie",
    "set-cookie",
    "proxy-authorization",
    "x-api-key",
    "api-key",
    "apikey",
}
SENSITIVE_HEADER_TOKENS = ("api-key", "apikey", "token", "secret", "cookie")
SENSITIVE_PAYLOAD_KEYS = {
    "access_token",
    "access-token",
    "api_key",
    "api-key",
    "apikey",
    "auth_token",
    "auth-token",
    "authorization",
    "bearer",
    "client_secret",
    "client-secret",
    "cookie",
    "credential",
    "credentials",
    "password",
    "passwd",
    "private_key",
    "private-key",
    "proxy-authorization",
    "refresh_token",
    "refresh-token",
    "secret",
    "secret_key",
    "secret-key",
    "session_id",
    "session-id",
    "set-cookie",
    "token",
    "x-api-key",
    "x-auth-token",
}
_SENSITIVE_PAYLOAD_SUBSTRINGS = (
    "api_key",
    "api-key",
    "apikey",
    "secret",
    "password",
    "passwd",
    "private_key",
    "private-key",
    "credential",
    "authorization",
    "cookie",
)
# Keys that can carry bulk user/model content (chat messages, Responses-API
# "input"/"instructions", system prompts, plain "prompt" strings). All of them
# are gated by the same include_messages flag "messages" already used, so
# free-form input isn't logged unless a caller explicitly opts in.
BULK_CONTENT_PAYLOAD_KEYS = {"messages", "input", "instructions", "system", "prompt"}


def is_sensitive_payload_key(key: object) -> bool:
    if not isinstance(key, str):
        return False
    normalized = key.strip().lower()
    if not normalized:
        return False
    if normalized in SENSITIVE_PAYLOAD_KEYS:
        return True
    if normalized == "tokens" or normalized.endswith(("_tokens", "-tokens")):
        return False
    if any(marker in normalized for marker in _SENSITIVE_PAYLOAD_SUBSTRINGS):
        return True
    if normalized.endswith(("_token", "-token")) or normalized.startswith(("token_", "token-")):
        return True
    return False


def redact_exception_message(exc: BaseException) -> str:
    raw = str(exc)
    if not raw.strip():
        return ""
    return redact_provider_error_text(raw)


def sanitize_exception_type_name(exception_type: object) -> str:
    if (
        not isinstance(exception_type, str)
        or not exception_type.isascii()
        or not exception_type.isidentifier()
        or len(exception_type) > 128
    ):
        return "BaseException"
    return exception_type


def safe_exception_type_name(exception: BaseException) -> str:
    return sanitize_exception_type_name(type(exception).__name__)


def is_sensitive_header_name(header_name: str) -> bool:
    normalized_name = header_name.strip().lower().replace("_", "-")
    if normalized_name in SENSITIVE_HEADER_NAMES:
        return True

    return any(token in normalized_name for token in SENSITIVE_HEADER_TOKENS)


def redact_headers_for_log(headers: Mapping[str, Any] | None) -> dict[str, Any]:
    if not headers:
        return {}

    redacted_headers: dict[str, Any] = {}
    for key, value in headers.items():
        redacted_headers[key] = REDACTED_VALUE if is_sensitive_header_name(key) else value

    return redacted_headers


def redact_payload_for_log(payload: Any, include_messages: bool = False) -> Any:
    if isinstance(payload, dict):
        redacted_payload = {}
        for key, value in payload.items():
            normalized_key = key.strip().lower() if isinstance(key, str) else key
            if normalized_key in BULK_CONTENT_PAYLOAD_KEYS and not include_messages:
                continue
            if is_sensitive_payload_key(key):
                redacted_payload[key] = REDACTED_SECRET_VALUE
                continue
            redacted_payload[key] = redact_payload_for_log(value, include_messages=include_messages)
        return redacted_payload

    if isinstance(payload, list):
        return [redact_payload_for_log(item, include_messages=include_messages) for item in payload]

    return payload


def redact_request_body_text(body_text: str) -> str:
    if not body_text:
        return ""

    try:
        parsed_body = json.loads(body_text)
    except Exception:
        return REDACTED_BODY_PLACEHOLDER

    redacted_body = redact_payload_for_log(parsed_body)
    return json.dumps(redacted_body, ensure_ascii=False, indent=2, sort_keys=True)
