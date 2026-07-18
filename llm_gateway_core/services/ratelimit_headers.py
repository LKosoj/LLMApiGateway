"""Parse provider ``x-ratelimit-*`` response headers into quota observations.

Pure functions only: no network I/O, no shared state. Callers (currently
``api/v1/chat_dispatch.py``) feed the parsed observations into
``UpstreamRoutingState.record_observed_limit()``.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Literal, Mapping
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

RateLimitAxis = Literal["rpm", "rpd", "tpm", "tpd"]

# Compound duration like "1m59.56s" or "7.66s"; every segment is optional so a
# bare "" also "matches" (rejected separately since callers filter it first).
_RESET_DURATION_PATTERN = re.compile(
    r"^(?:(\d+)h)?(?:(\d+)m)?(?:(\d+(?:\.\d+)?)s)?$"
)
# OpenRouter's epoch-millisecond reset is implausible below this many
# milliseconds (~2001-09-09 in seconds, rescaled) and treated as invalid.
_MIN_PLAUSIBLE_EPOCH_MILLIS = 1_000_000_000_000


@dataclass(frozen=True)
class RateLimitObservation:
    axis: RateLimitAxis
    limit: int | None
    remaining: int | None
    reset_at_monotonic: float | None
    source: str = "header"


def parse_reset_duration_seconds(raw: str) -> float | None:
    """Parse a Groq/Cerebras-style reset duration into seconds.

    Accepts compound duration strings (``"1m59.56s"``, ``"7.66s"``) and,
    as a fallback, a plain number meaning seconds (``"45"``). Returns
    ``None`` when the input cannot be parsed at all.
    """
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None

    match = _RESET_DURATION_PATTERN.match(text)
    if match and any(match.groups()):
        hours, minutes, seconds = match.groups()
        total = 0.0
        if hours:
            total += int(hours) * 3600.0
        if minutes:
            total += int(minutes) * 60.0
        if seconds:
            total += float(seconds)
        return total

    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(raw: str | None, axis: str, field: str) -> int | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        logger.debug("Unparseable %s %s header value: %r", axis, field, raw)
        return None


def _build_observation(
    axis: RateLimitAxis,
    *,
    limit_raw: str | None,
    remaining_raw: str | None,
    reset_at_monotonic: float | None,
) -> RateLimitObservation | None:
    limit = _parse_int(limit_raw, axis, "limit")
    remaining = _parse_int(remaining_raw, axis, "remaining")
    if remaining is not None and remaining < 0:
        remaining = 0
    if limit is None and remaining is None and reset_at_monotonic is None:
        return None
    return RateLimitObservation(
        axis=axis,
        limit=limit,
        remaining=remaining,
        reset_at_monotonic=reset_at_monotonic,
        source="header",
    )


def _reset_at_from_duration(raw: str | None, now_monotonic: float) -> float | None:
    if raw is None:
        return None
    seconds = parse_reset_duration_seconds(raw)
    if seconds is None:
        return None
    return now_monotonic + seconds


def _reset_at_from_epoch_millis(
    raw: str | None, now_monotonic: float, now_wall: float
) -> float | None:
    if raw is None:
        return None
    text = raw.strip()
    if not text:
        return None
    try:
        epoch_millis = int(text)
    except ValueError:
        logger.debug("Unparseable epoch-millisecond reset header value: %r", raw)
        return None
    if epoch_millis < _MIN_PLAUSIBLE_EPOCH_MILLIS:
        logger.debug("Implausible epoch-millisecond reset header value: %r", raw)
        return None
    return epoch_millis / 1000.0 - now_wall + now_monotonic


def _parse_groq_headers(
    headers: Mapping[str, str], now_monotonic: float
) -> tuple[RateLimitObservation, ...]:
    observations: list[RateLimitObservation] = []
    rpm = _build_observation(
        "rpm",
        limit_raw=headers.get("x-ratelimit-limit-requests"),
        remaining_raw=headers.get("x-ratelimit-remaining-requests"),
        reset_at_monotonic=_reset_at_from_duration(
            headers.get("x-ratelimit-reset-requests"), now_monotonic
        ),
    )
    if rpm is not None:
        observations.append(rpm)
    tpm = _build_observation(
        "tpm",
        limit_raw=headers.get("x-ratelimit-limit-tokens"),
        remaining_raw=headers.get("x-ratelimit-remaining-tokens"),
        reset_at_monotonic=_reset_at_from_duration(
            headers.get("x-ratelimit-reset-tokens"), now_monotonic
        ),
    )
    if tpm is not None:
        observations.append(tpm)
    return tuple(observations)


def _parse_cerebras_headers(
    headers: Mapping[str, str], now_monotonic: float
) -> tuple[RateLimitObservation, ...]:
    observations: list[RateLimitObservation] = []
    rpd = _build_observation(
        "rpd",
        limit_raw=headers.get("x-ratelimit-limit-requests-day"),
        remaining_raw=headers.get("x-ratelimit-remaining-requests-day"),
        reset_at_monotonic=_reset_at_from_duration(
            headers.get("x-ratelimit-reset-requests-day"), now_monotonic
        ),
    )
    if rpd is not None:
        observations.append(rpd)
    tpm = _build_observation(
        "tpm",
        limit_raw=headers.get("x-ratelimit-limit-tokens-minute"),
        remaining_raw=headers.get("x-ratelimit-remaining-tokens-minute"),
        reset_at_monotonic=_reset_at_from_duration(
            headers.get("x-ratelimit-reset-tokens-minute"), now_monotonic
        ),
    )
    if tpm is not None:
        observations.append(tpm)
    return tuple(observations)


def _parse_openrouter_headers(
    headers: Mapping[str, str], now_monotonic: float, now_wall: float
) -> tuple[RateLimitObservation, ...]:
    rpd = _build_observation(
        "rpd",
        limit_raw=headers.get("x-ratelimit-limit"),
        remaining_raw=headers.get("x-ratelimit-remaining"),
        reset_at_monotonic=_reset_at_from_epoch_millis(
            headers.get("x-ratelimit-reset"), now_monotonic, now_wall
        ),
    )
    return (rpd,) if rpd is not None else ()


def parse_ratelimit_headers(
    target_url: str,
    headers: Mapping[str, str],
    *,
    now_monotonic: float,
    now_wall: float,
) -> tuple[RateLimitObservation, ...]:
    """Parse provider-specific ``x-ratelimit-*`` headers into observations.

    The provider spec is chosen from ``target_url``'s hostname. Unknown
    hosts return an empty tuple without raising. Header lookup is always
    case-insensitive regardless of the casing in ``headers``.
    """
    hostname = urlsplit(target_url).hostname
    normalized_headers = {str(key).lower(): value for key, value in headers.items()}

    if hostname == "api.groq.com":
        return _parse_groq_headers(normalized_headers, now_monotonic)
    if hostname == "api.cerebras.ai":
        return _parse_cerebras_headers(normalized_headers, now_monotonic)
    if hostname == "openrouter.ai":
        return _parse_openrouter_headers(normalized_headers, now_monotonic, now_wall)
    return ()
