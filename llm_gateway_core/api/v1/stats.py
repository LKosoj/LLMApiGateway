import asyncio
import logging
from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

# Import the TokensUsageDB
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB, validate_usage_records_pagination
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB, validate_fallback_records_pagination
from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.active_requests import get_active_requests_registry
from llm_gateway_core.utils.html_cache import get_template
from llm_gateway_core.utils.ttl_cache import AsyncTtlCache

stats_router = APIRouter()

HTML_DIR = STATIC_DIR

# Dashboards poll these endpoints every few seconds from every open tab; a
# short-lived cache keeps SQLite from re-aggregating the whole table for
# each tab. 30s is well below the granularity of the smallest period bucket
# ('hour' updates per minute in the UI), so user-perceived freshness is
# unaffected.
USAGE_STATS_CACHE_TTL_SECONDS = 30.0
_usage_stats_cache = AsyncTtlCache(USAGE_STATS_CACHE_TTL_SECONDS)
_fallback_stats_cache = AsyncTtlCache(USAGE_STATS_CACHE_TTL_SECONDS)


def _effective_api_key_filter(request: Request, requested_api_key_id: int | None = None) -> int | None:
    """Return the ``api_key_id`` filter to apply for the current caller.

    Master callers may request a specific virtual key id, or omit the filter
    to see everything. Virtual-key callers are always scoped to their own
    ``api_key_id`` so they can only inspect their own statistics.
    """
    role = getattr(request.state, "api_key_role", ROLE_MASTER)
    if role == ROLE_USER:
        return getattr(request.state, "api_key_id", None)
    if requested_api_key_id is not None and requested_api_key_id <= 0:
        raise HTTPException(status_code=400, detail="api_key_id must be a positive integer.")
    return requested_api_key_id


def _is_master_caller(request: Request) -> bool:
    return getattr(request.state, "api_key_role", ROLE_MASTER) == ROLE_MASTER


def _filter_complete_usage_rows(rows: list[dict]) -> list[dict]:
    return [
        row for row in rows
        if row.get("gateway_model") is not None
        and row.get("provider") is not None
        and row.get("model") is not None
    ]


def _mark_completed_usage_rows(rows: list[dict]) -> list[dict]:
    completed_rows = []
    for row in rows:
        completed_row = dict(row)
        completed_row["status"] = "completed"
        completed_rows.append(completed_row)
    return completed_rows


@stats_router.get("/ui/usage-stats", response_class=HTMLResponse, tags=["Usage Stats UI"])
async def get_usage_stats_page(request: Request):
    """Serves the HTML page for the token usage statistics viewer."""
    stats_html_path = HTML_DIR / "usage-stats.html"
    if not stats_html_path.exists():
        logging.error(f"Usage stats HTML file not found at {stats_html_path}")
        raise HTTPException(status_code=404, detail="Usage statistics page not found.")
    try:
        return HTMLResponse(content=await get_template(stats_html_path))
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error reading usage stats HTML file: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load usage statistics page.")

@stats_router.get("/api/usage-stats/{period}", response_class=JSONResponse, tags=["Usage Stats API"])
async def get_aggregated_stats(request: Request, period: str, api_key_id: int | None = None):
    """
    Fetches aggregated token usage statistics by the specified period and model.
    """
    tokens_usage_db: TokensUsageDB = request.app.state.tokens_usage_db
    if not tokens_usage_db:
        logging.error("TokensUsageDB not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: TokensUsageDB not available.")

    try:
        # Validate period input
        if period not in ['hour', 'day', 'week', 'month']:
            raise HTTPException(status_code=400, detail="Invalid period. Must be 'hour', 'day', 'week', or 'month'.")

        api_key_id_filter = _effective_api_key_filter(request, api_key_id)

        async def _fetch():
            end_date = datetime.now(timezone.utc)
            start_date = None
            if period == 'hour':
                start_date = end_date - timedelta(hours=24)
            elif period == 'day':
                start_date = end_date - timedelta(weeks=2)
            elif period == 'week':
                start_date = end_date - timedelta(weeks=15)
            elif period == 'month':
                start_date = end_date - timedelta(days=365) # Approximately 12 months

            rows = await tokens_usage_db.get_aggregated_usage(
                period,
                start_date=start_date,
                end_date=end_date,
                api_key_id=api_key_id_filter,
            )
            return _filter_complete_usage_rows(rows)

        cache_key = (period, api_key_id_filter)
        aggregated_data = await _usage_stats_cache.get_or_compute(cache_key, _fetch)
        return JSONResponse(content=aggregated_data)
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.error(f"Error fetching aggregated usage statistics for period '{period}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not retrieve usage statistics: {e}")

@stats_router.get("/api/usage-records", response_class=JSONResponse, tags=["Usage Stats API"])
async def get_usage_records(
    request: Request, limit: int = 25, offset: int = 0, api_key_id: int | None = None
):
    """
    Fetches the latest token usage records with pagination.
    """
    tokens_usage_db: TokensUsageDB = request.app.state.tokens_usage_db
    if not tokens_usage_db:
        logging.error("TokensUsageDB not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: TokensUsageDB not available.")

    try:
        limit, offset = validate_usage_records_pagination(limit, offset)
        api_key_id_filter = _effective_api_key_filter(request, api_key_id)

        active_records = get_active_requests_registry(request.app).list_records(
            api_key_id=api_key_id_filter
        )
        active_count = len(active_records)
        active_page_records = active_records[offset:offset + limit] if limit else []
        completed_limit = max(0, limit - len(active_page_records))
        completed_offset = max(0, offset - active_count) if offset >= active_count else 0

        async def _fetch_completed_records():
            if completed_limit == 0:
                return []
            return await tokens_usage_db.get_latest_usage_records(
                limit=completed_limit,
                offset=completed_offset,
                api_key_id=api_key_id_filter,
            )

        completed_records, completed_total_records = await asyncio.gather(
            _fetch_completed_records(),
            tokens_usage_db.get_total_records_count(api_key_id=api_key_id_filter),
        )
        records = active_page_records + _mark_completed_usage_rows(completed_records)
        return JSONResponse(
            content={
                "records": records,
                "total_records": active_count + completed_total_records,
            }
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.error(f"Error fetching usage records: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not retrieve usage records: {e}")


def _get_period_start_date(period: str) -> datetime | None:
    """Calculate start_date based on period for consistent use across endpoints."""
    end_date = datetime.now(timezone.utc)
    if period == 'hour':
        return end_date - timedelta(hours=24)
    elif period == 'day':
        return end_date - timedelta(weeks=2)
    elif period == 'week':
        return end_date - timedelta(weeks=15)
    elif period == 'month':
        return end_date - timedelta(days=365)
    return None


@stats_router.get("/api/fallback-stats/{period}", response_class=JSONResponse, tags=["Fallback Analytics API"])
async def get_fallback_stats(request: Request, period: str):
    """
    Fetches aggregated fallback failure statistics by the specified period.
    Only includes failed attempts (not successes).
    """
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Fallback analytics are available only to the master API key.")

    fallback_events_db: FallbackEventsDB = getattr(request.app.state, "fallback_events_db", None)
    if not fallback_events_db:
        logging.error("FallbackEventsDB not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: FallbackEventsDB not available.")

    try:
        if period not in ['hour', 'day', 'week', 'month']:
            raise HTTPException(status_code=400, detail="Invalid period. Must be 'hour', 'day', 'week', or 'month'.")

        async def _fetch():
            start_date = _get_period_start_date(period)
            end_date = datetime.now(timezone.utc)
            return await fallback_events_db.get_aggregated_stats(
                period,
                start_date=start_date,
                end_date=end_date,
            )

        aggregated_data = await _fallback_stats_cache.get_or_compute(period, _fetch)
        return JSONResponse(content=aggregated_data)
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.error(f"Error fetching fallback stats for period '{period}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not retrieve fallback statistics: {e}")


@stats_router.get("/api/fallback-records", response_class=JSONResponse, tags=["Fallback Analytics API"])
async def get_fallback_records(request: Request, limit: int = 25, offset: int = 0):
    """
    Fetches fallback chain records with pagination.
    Only includes requests where 2+ attempts were made (i.e., fallbacks occurred).
    """
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Fallback analytics are available only to the master API key.")

    fallback_events_db: FallbackEventsDB = getattr(request.app.state, "fallback_events_db", None)
    if not fallback_events_db:
        logging.error("FallbackEventsDB not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: FallbackEventsDB not available.")

    try:
        limit, offset = validate_fallback_records_pagination(limit, offset)
        records, total_records = await fallback_events_db.get_fallback_records(limit=limit, offset=offset)
        return JSONResponse(content={"records": records, "total_records": total_records})
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except HTTPException as he:
        raise he
    except Exception as e:
        logging.error(f"Error fetching fallback records: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=f"Could not retrieve fallback records: {e}")
