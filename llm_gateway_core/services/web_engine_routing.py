"""Cooldown/concurrency routing for keyless web search engines.

Wraps each engine call with the shared ``UpstreamRoutingState`` under a
dedicated namespace (``provider="web_free_search"``, ``model=<engine>``,
``key_fingerprint="keyless"``) so engine failures reuse the existing
cooldown/429-ladder machinery without touching LLM-key routing state.
``apply_penalty`` is always False: the penalty score is designed for
LLM keys, not for anonymous engines.
"""
from __future__ import annotations

import asyncio
import logging
import weakref
from collections.abc import Awaitable, Callable
from contextlib import nullcontext
from typing import Any

import httpx

from llm_gateway_core.api.v1.web_engine_errors import FreeEngineError
from llm_gateway_core.services.upstream_routing_state import (
    DEFAULT_COOLDOWN_SECONDS,
    KEYLESS_FINGERPRINT,
    UpstreamRoutingState,
)

logger = logging.getLogger(__name__)

WEB_ENGINE_PROVIDER = "web_free_search"
BOT_WALL_COOLDOWN_SECONDS = 60.0
DEFAULT_ENGINE_CONCURRENCY = 2

# HTML scrapers tolerate a single in-flight request; JSON APIs allow two.
ENGINE_CONCURRENCY: dict[str, int] = {
    "bing": 1,
    "ddg_html": 1,
    "ddg_lite": 1,
    "searxng": 1,
    "anysearch": 2,
    "youcom": 2,
    "firecrawl": 2,
    "keenable": 2,
}

# Semaphores are bound to the loop they were created on; keying the cache by
# the running loop keeps tests (one fresh asyncio.run per test) from reusing a
# semaphore attached to a closed loop. Weak keys drop entries once the loop is
# garbage-collected; reset_engine_semaphores() clears the cache explicitly.
# Keyed by engine only: mixing default and override limits for one engine must
# not create two semaphores and exceed the engine's total concurrency.
_engine_semaphores: weakref.WeakKeyDictionary[
    asyncio.AbstractEventLoop, dict[str, asyncio.Semaphore]
] = weakref.WeakKeyDictionary()


def reset_engine_semaphores() -> None:
    """Drop all cached per-engine semaphores (test helper)."""
    _engine_semaphores.clear()


def classify_engine_failure(exc: BaseException) -> str:
    """Map an engine exception onto a failure class for cooldown routing."""
    if isinstance(exc, FreeEngineError):
        return exc.failure_class
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in (401, 403):
            return "auth"
        if status_code in (402, 429):
            return "quota"
        if 500 <= status_code <= 599:
            return "transient"
        return "unknown"
    if isinstance(exc, (httpx.TimeoutException, httpx.TransportError)):
        return "transient"
    return "unknown"


class _HttpStatusErrorDetail(str):
    """``httpx.HTTPStatusError`` seen by ``record_failure``/``classify_error``.

    Both read ``getattr(error_detail, "status_code", None)``, which the raw
    httpx exception lacks (it only has ``.response.status_code``). A str
    subclass with the attribute mirrors the production ``RequestErrorDetail``
    pattern and stays visible to error-signal extraction.
    """

    status_code: int

    def __new__(cls, exc: httpx.HTTPStatusError) -> "_HttpStatusErrorDetail":
        detail = super().__new__(cls, str(exc))
        detail.status_code = exc.response.status_code
        return detail


def _error_detail_for(exc: BaseException) -> object:
    if isinstance(exc, httpx.HTTPStatusError):
        return _HttpStatusErrorDetail(exc)
    return exc


def _retry_after_seconds(exc: BaseException) -> float | None:
    """Numeric Retry-After hint, if the failure carries one (HTTP-date ignored)."""
    raw: Any = getattr(exc, "retry_after", None)
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    if headers is not None:
        raw = headers.get("retry-after", raw)
    if raw is None:
        return None
    try:
        seconds = float(raw)
    except (TypeError, ValueError):
        return None
    return seconds if seconds > 0 else None


def _record_engine_failure(
    state: UpstreamRoutingState, provider: str, engine: str, key_fingerprint: str, exc: BaseException,
) -> None:
    failure_class = classify_engine_failure(exc)
    if failure_class == "invalid-response":
        # Layout/format breakage is not an availability signal: no cooldown.
        return
    error_detail = _error_detail_for(exc)
    if failure_class == "bot-wall":
        state.record_failure(
            provider, engine, key_fingerprint, error_detail,
            temporary=True, apply_penalty=False, retry_after=BOT_WALL_COOLDOWN_SECONDS,
        )
    elif failure_class == "quota":
        floor = _retry_after_seconds(exc)
        if getattr(error_detail, "status_code", None) == 429:
            state.record_failure(
                provider, engine, key_fingerprint, error_detail,
                temporary=True, apply_penalty=False,
                retry_after_floor_seconds=floor,
            )
        else:
            # Non-429 quota signals (e.g. HTTP 402) land in the generic
            # temporary branch, which ignores retry_after_floor_seconds;
            # pass an explicit cooldown that still honors the server hint.
            state.record_failure(
                provider, engine, key_fingerprint, error_detail,
                temporary=True, apply_penalty=False,
                retry_after=max(DEFAULT_COOLDOWN_SECONDS, floor or 0.0),
            )
    elif failure_class == "auth":
        # status_code 401/403 triggers the stock access-denied cooldown branch.
        state.record_failure(
            provider, engine, key_fingerprint, error_detail,
            temporary=False, apply_penalty=False,
        )
    else:  # transient / unknown
        state.record_failure(
            provider, engine, key_fingerprint, error_detail,
            temporary=True, apply_penalty=False,
        )
    logger.warning("web engine %s failed (%s): %s", engine, failure_class, exc)


def _engine_semaphore(engine: str, limit: int) -> asyncio.Semaphore:
    """One semaphore per engine per loop; the limit is first-call-wins.

    The first call for an engine fixes its semaphore limit for the lifetime
    of the loop; later calls (even with a different ``concurrency_limit``)
    reuse that semaphore so an override cannot inflate total concurrency.
    """
    loop = asyncio.get_running_loop()
    per_loop = _engine_semaphores.get(loop)
    if per_loop is None:
        per_loop = {}
        _engine_semaphores[loop] = per_loop
    semaphore = per_loop.get(engine)
    if semaphore is None:
        semaphore = asyncio.Semaphore(limit)
        per_loop[engine] = semaphore
    return semaphore


async def run_engine(
    state: UpstreamRoutingState,
    engine: str,
    factory: Callable[[], Awaitable[list[dict]]],
    *,
    concurrency_limit: int | None = None,
    provider: str = WEB_ENGINE_PROVIDER,
    key_fingerprint: str = KEYLESS_FINGERPRINT,
) -> list[dict] | None:
    """Run one engine under its cooldown and per-engine concurrency slot.

    Returns ``None`` when the engine is cooling down (no failure recorded),
    otherwise the factory result. A transient failure gets exactly one retry;
    the final exception is recorded per its failure class and re-raised.

    All cooldown checks and success/failure records use the
    ``(provider, engine, key_fingerprint)`` namespace; the defaults keep the
    keyless free-search namespace, while paid adapters pass their own
    provider/fingerprint so their cooldowns stay isolated.

    The per-engine semaphore limit is fixed by the first call on this loop
    (first-call-wins); ``concurrency_limit`` overrides only apply at creation.
    ``concurrency_limit=0`` skips the semaphore entirely (legacy uncapped
    behavior for paid adapters); cooldown checks and recording still apply.
    """
    cooldown = state.cooldown_remaining_seconds(provider, engine, key_fingerprint)
    if cooldown is not None and cooldown > 0:
        logger.debug("web engine %s skipped: cooldown %.0fs remaining", engine, cooldown)
        return None

    limit = concurrency_limit if concurrency_limit is not None else ENGINE_CONCURRENCY.get(
        engine, DEFAULT_ENGINE_CONCURRENCY
    )
    slot = nullcontext() if limit == 0 else _engine_semaphore(engine, limit)
    async with slot:
        # Re-check after acquiring the slot: a request ahead of us in the
        # queue may have just put the engine into cooldown.
        cooldown = state.cooldown_remaining_seconds(provider, engine, key_fingerprint)
        if cooldown is not None and cooldown > 0:
            logger.debug("web engine %s skipped after queueing: cooldown %.0fs remaining", engine, cooldown)
            return None
        try:
            result = await factory()
        except Exception as exc:
            if classify_engine_failure(exc) != "transient":
                _record_engine_failure(state, provider, engine, key_fingerprint, exc)
                raise
            logger.debug("web engine %s transient failure, retrying once: %s", engine, exc)
            try:
                result = await factory()
            except Exception as retry_exc:
                _record_engine_failure(state, provider, engine, key_fingerprint, retry_exc)
                raise
    state.record_success(provider, engine, key_fingerprint)
    return result
