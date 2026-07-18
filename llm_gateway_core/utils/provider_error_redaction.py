"""Redact secrets/tokens/URLs from raw upstream provider error text.

Upstream error bodies occasionally echo back request headers or URLs that
contain live credentials (Authorization headers, API keys, signed URLs).
Those bodies currently reach the client verbatim inside the final 503
"all providers failed" detail (and a couple of web-adapter error paths).
:func:`redact_provider_error_text` produces a bounded, client-safe summary
suitable for inclusion in an HTTP error body.

Pure function: no logging, no I/O, no request/DB access. Internal logs and
fallback-event DB rows keep the raw text untouched.
"""
from __future__ import annotations

import re

MAX_PROVIDER_ERROR_TEXT_LENGTH = 240

_BEARER_TOKEN_PATTERN = re.compile(r"\bBearer\s+\S+", re.IGNORECASE)
# Standalone "<scheme> <token>" pairs (no preceding "key:"/"key=" context) for
# schemes other than Bearer. "Basic"/"Token" are common English words, so the
# token half is required to look credential-shaped: it must contain a digit,
# or one of the "+/=_-" symbols, or be >=16 chars with both upper- and
# lowercase letters. This avoids mangling plain capitalized words like
# "Basic plan required" or "Token REQUIRED/INVALID/MISSING ..." while still
# catching real tokens such as base64 blobs (which almost always carry "="
# padding or a digit). The scheme name itself stays case-insensitive via the
# scoped (?i:...) group; the guard below is deliberately case-sensitive.
_AUTH_SCHEME_TOKEN_PATTERN = re.compile(
    r"\b((?i:Basic|Token|ApiKey))\s+"
    r"(?=\S*[0-9]|\S*[+/=_-]|(?=\S{16,})(?=\S*[A-Z])(?=\S*[a-z]))"
    r"\S+"
)
_KEY_VALUE_PATTERN = re.compile(
    r'\b(api[_-]?key|access[_-]?token|token|secret|authorization)\s*([:=])\s*"?'
    r'((?:(?:Basic|Bearer|Token|ApiKey)\s+)?[^\s"]+)"?',
    re.IGNORECASE,
)
_PREFIXED_KEY_PATTERN = re.compile(
    r"\b(?:sk-[A-Za-z0-9_-]{3,}|gsk_[A-Za-z0-9_-]{3,}|lgk_[A-Za-z0-9_-]{3,}|AIza[A-Za-z0-9_-]{3,})"
)
_JWT_PATTERN = re.compile(r"\b[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\b")
_URL_PATTERN = re.compile(r"https?://\S+", re.IGNORECASE)
_WHITESPACE_PATTERN = re.compile(r"\s+")


def _redact_auth_scheme_token(match: "re.Match[str]") -> str:
    return f"{match.group(1)} [redacted]"


def _redact_key_value(match: "re.Match[str]") -> str:
    name, separator, _value = match.group(1), match.group(2), match.group(3)
    return f"{name}{separator}[redacted]"


def redact_provider_error_text(text: object) -> str:
    """Return a bounded, credential-safe summary of a raw upstream error.

    Redaction order: ``Bearer <token>`` headers, other standalone auth-scheme
    tokens (``Basic``/``Token``/``ApiKey``), ``key: value``/``key=value``
    pairs for common credential names (whose value may itself carry a scheme
    prefix, e.g. ``authorization: Basic <token>``), our own and third-party
    API key prefixes, JWT-shaped tokens, then bare URLs. Whitespace is
    collapsed to single spaces and the result is truncated to
    :data:`MAX_PROVIDER_ERROR_TEXT_LENGTH` characters. Empty/``None`` input
    (and input that redacts down to nothing) becomes ``"Provider error"``.
    """
    if text is None:
        return "Provider error"
    raw = text if isinstance(text, str) else str(text)
    if not raw.strip():
        return "Provider error"

    redacted = _BEARER_TOKEN_PATTERN.sub("Bearer [redacted]", raw)
    redacted = _AUTH_SCHEME_TOKEN_PATTERN.sub(_redact_auth_scheme_token, redacted)
    redacted = _KEY_VALUE_PATTERN.sub(_redact_key_value, redacted)
    redacted = _PREFIXED_KEY_PATTERN.sub("[redacted-key]", redacted)
    redacted = _JWT_PATTERN.sub("[redacted-token]", redacted)
    redacted = _URL_PATTERN.sub("[redacted-url]", redacted)
    redacted = _WHITESPACE_PATTERN.sub(" ", redacted).strip()

    if len(redacted) > MAX_PROVIDER_ERROR_TEXT_LENGTH:
        redacted = redacted[: MAX_PROVIDER_ERROR_TEXT_LENGTH - 3].rstrip() + "..."

    return redacted or "Provider error"
