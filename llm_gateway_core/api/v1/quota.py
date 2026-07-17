"""Quota Dashboard API.

Provides per-virtual-key RPM/TPM snapshot and budget status for the
Quota Dashboard page.  Master callers see all keys; virtual-key callers
see only their own card.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse

from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.utils.html_cache import get_template
from llm_gateway_core.utils.ttl_cache import AsyncTtlCache

if TYPE_CHECKING:
    from llm_gateway_core.services.runtime_config import AppServices

logger = logging.getLogger(__name__)

quota_router = APIRouter()

QUOTA_CACHE_TTL_SECONDS = 5.0
_quota_cache = AsyncTtlCache(QUOTA_CACHE_TTL_SECONDS)

WINDOW_SECONDS = 60.0


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


def _effective_key_filter(request: Request) -> int | None:
    """Return the api_key_id filter for the current caller.

    Virtual-key callers are always scoped to their own key_id.
    Master callers see everything (returns None).

    Raises HTTP 401 if the auth middleware did not attach a recognised role —
    this is a hard fail rather than silently exposing the master view.
    """
    role = getattr(request.state, "api_key_role", None)
    if role == ROLE_MASTER:
        return None
    if role == ROLE_USER:
        key_id = getattr(request.state, "api_key_id", None)
        if type(key_id) is int and key_id > 0:
            return key_id
    raise HTTPException(status_code=401, detail="Authentication required.")


async def _build_quota_snapshot(
    api_keys_db: ApiKeysDB,
    fallback_events_db: FallbackEventsDB,
    rate_limiter: RateLimiter,
    key_id_filter: int | None,
) -> list[dict]:
    """Build the per-key quota snapshot used by the dashboard endpoint."""

    # Fetch all keys (sync → thread)
    all_keys = await asyncio.to_thread(api_keys_db.list_all)

    if key_id_filter is not None:
        all_keys = [k for k in all_keys if k.id == key_id_filter]

    # Fetch 24h fallback counts for all relevant keys
    fallback_counts = await fallback_events_db.get_events_count_last_24h(
        api_key_id=key_id_filter
    )

    result: list[dict] = []
    for key in all_keys:
        # --- Rate-limiter snapshot ---
        rpm_current = 0
        tpm_current = 0
        reset_in_seconds: int = int(WINDOW_SECONDS)  # default: full window remaining

        rpm_count, tpm_tokens, oldest_age = rate_limiter.get_window_snapshot(key.id)
        rpm_current = rpm_count
        tpm_current = tpm_tokens
        if oldest_age is None:
            # No events in window — full window duration remains
            reset_in_seconds = int(WINDOW_SECONDS)
        else:
            # reset_in_seconds = how long until the oldest event leaves the window
            reset_in_seconds = max(0, int(WINDOW_SECONDS - oldest_age))

        result.append(
            {
                "key_id": key.id,
                "name": key.name,
                "disabled": key.disabled,
                "rpm": {
                    "current": rpm_current,
                    "limit": key.rpm,
                    "reset_in_seconds": reset_in_seconds,
                },
                "tpm": {
                    "current": tpm_current,
                    "limit": key.tpm,
                    "reset_in_seconds": reset_in_seconds,
                },
                "budget": {
                    "spent_usd": round(key.spent_usd, 6),
                    "limit_usd": key.budget_usd,
                },
                "fallback_events_24h": fallback_counts.get(key.id, 0),
            }
        )

    return result


@quota_router.get("/ui/quota", response_class=HTMLResponse, tags=["Quota Dashboard UI"])
async def get_quota_page(request: Request) -> HTMLResponse:
    """Serve the Quota Dashboard HTML page."""
    html_path = STATIC_DIR / "quota.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Quota dashboard page not found.")
    try:
        return HTMLResponse(content=await get_template(html_path))
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("Error reading quota dashboard HTML: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load quota dashboard page.")


@quota_router.get("/api/quota/keys", response_class=JSONResponse, tags=["Quota Dashboard API"])
async def get_quota_keys(request: Request) -> JSONResponse:
    """Return per-key quota snapshot.

    Master callers see all keys.  Virtual-key callers see only their own card.
    The endpoint is accessible to both roles; scoping is enforced inside.
    """
    key_id_filter = _effective_key_filter(request)
    services = _capture_app_services(request)
    api_keys_db = services.api_keys_db
    fallback_events_db = services.fallback_events_db
    rate_limiter = services.rate_limiter

    async def _fetch() -> list[dict]:
        return await _build_quota_snapshot(
            api_keys_db, fallback_events_db, rate_limiter, key_id_filter
        )

    cache_key = ("quota_keys", key_id_filter)
    try:
        data = await _quota_cache.get_or_compute(cache_key, _fetch)
    except Exception:
        logger.exception("Error building quota snapshot")
        raise HTTPException(status_code=500, detail="Could not retrieve quota data.")

    return JSONResponse(content=data)
