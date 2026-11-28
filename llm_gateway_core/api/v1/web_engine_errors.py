"""Typed failures for keyless web search engines.

The routing layer (``services/web_engine_routing.py``) maps these classes onto
``UpstreamRoutingState`` cooldowns. ``status_code`` lets the existing
``record_failure`` access-denied branch recognise auth failures without an
upstream HTTP response object.
"""
from __future__ import annotations


class FreeEngineError(Exception):
    """Base class for keyless engine failures."""

    failure_class = "unknown"
    status_code: int | None = None


class BotWallError(FreeEngineError):
    """Anti-bot challenge or egress refusal; short cooldown, then retry later."""

    failure_class = "bot-wall"
    status_code = 202


class QuotaError(FreeEngineError):
    """Anonymous quota or rate limit exhausted."""

    failure_class = "quota"
    status_code = 429


class AuthError(FreeEngineError):
    """Endpoint demands credentials this keyless engine does not have."""

    failure_class = "auth"
    status_code = 401


class TransientError(FreeEngineError):
    """Timeout, network fault, or 5xx; worth one immediate retry."""

    failure_class = "transient"


class InvalidResponseError(FreeEngineError):
    """Unparseable or structurally unexpected payload; skip without cooldown."""

    failure_class = "invalid-response"