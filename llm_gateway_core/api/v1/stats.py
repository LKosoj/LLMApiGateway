import asyncio
import logging
import httpx
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, Request, HTTPException, Query
from fastapi.responses import HTMLResponse, JSONResponse

from llm_gateway_core.config.loader import (
    ANTHROPIC_API_VERSION,
    resolve_provider_api_key_value,
    resolve_provider_config_auth_headers,
    resolve_provider_config_api_keys,
)
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.db.tokens_usage_db import validate_usage_records_pagination
from llm_gateway_core.db.fallback_events_db import validate_fallback_records_pagination
from llm_gateway_core.db.rejections_db import VALID_CATEGORIES
from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.upstream_routing_state import fingerprint_api_key
from llm_gateway_core.utils.api_keys import split_api_keys
from llm_gateway_core.utils.html_cache import get_template
from llm_gateway_core.utils.ttl_cache import AsyncTtlCache

if TYPE_CHECKING:
    from llm_gateway_core.config.loader import ConfigLoader
    from llm_gateway_core.services.runtime_config import AppServices, RuntimeSnapshot

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
_upstream_stats_cache = AsyncTtlCache(USAGE_STATS_CACHE_TTL_SECONDS)

ANALYTICS_RANGES = {
    "24h": timedelta(hours=24),
    "7d": timedelta(days=7),
    "14d": timedelta(days=14),
    "30d": timedelta(days=30),
    "90d": timedelta(days=90),
    "180d": timedelta(days=180),
    "365d": timedelta(days=365),
    "12m": timedelta(days=365),
}
ANALYTICS_BUCKETS = {"hour", "day", "week", "month"}


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


def _capture_runtime_snapshot(request: Request) -> "RuntimeSnapshot":
    return cast("RuntimeSnapshot", request.state.runtime_snapshot)


def _effective_api_key_filter(request: Request) -> int | None:
    """Return the ``api_key_id`` filter to apply for the current caller.

    Master callers always see the full usage set. Virtual-key callers are
    always scoped to their own ``api_key_id`` so they can only inspect their
    own statistics.
    """
    role = getattr(request.state, "api_key_role", None)
    if role == ROLE_USER:
        api_key_id = getattr(request.state, "api_key_id", None)
        if api_key_id is None:
            raise HTTPException(status_code=401, detail="Virtual key session is missing api_key_id.")
        return api_key_id
    if role != ROLE_MASTER:
        raise HTTPException(status_code=401, detail="Authenticated API key role is not recognized.")
    return None


def _is_master_caller(request: Request) -> bool:
    return getattr(request.state, "api_key_role", None) == ROLE_MASTER


def _optional_query_value(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_bool_query(value: str | None) -> bool | None:
    cleaned = _optional_query_value(value)
    if cleaned is None:
        return None
    normalized = cleaned.lower()
    if normalized in {"1", "true", "yes"}:
        return True
    if normalized in {"0", "false", "no"}:
        return False
    raise HTTPException(status_code=400, detail="estimated must be true or false.")


def _resolve_analytics_window(range_name: str, bucket: str | None) -> tuple[datetime, datetime, str]:
    if range_name not in ANALYTICS_RANGES:
        raise HTTPException(
            status_code=400,
            detail="range must be one of: " + ", ".join(ANALYTICS_RANGES),
        )
    resolved_bucket = bucket or ("hour" if range_name == "24h" else "month" if range_name == "12m" else "day")
    if resolved_bucket not in ANALYTICS_BUCKETS:
        raise HTTPException(
            status_code=400,
            detail="bucket must be one of: " + ", ".join(sorted(ANALYTICS_BUCKETS)),
        )
    end_date = datetime.now(timezone.utc)
    return end_date - ANALYTICS_RANGES[range_name], end_date, resolved_bucket


def _resolve_analytics_scope(
    request: Request,
    requested_api_key_id: int | None,
    api_key_scope: str | None,
) -> tuple[str, int | None, bool, str]:
    role = getattr(request.state, "api_key_role", None)
    if role == ROLE_USER:
        api_key_id = getattr(request.state, "api_key_id", None)
        if api_key_id is None:
            raise HTTPException(status_code=401, detail="Virtual key session is missing api_key_id.")
        return role, api_key_id, False, "own"

    if role != ROLE_MASTER:
        raise HTTPException(status_code=401, detail="Authenticated API key role is not recognized.")

    scope = _optional_query_value(api_key_scope) or "all"
    if scope not in {"all", "unattributed"}:
        raise HTTPException(status_code=400, detail="api_key_scope must be 'all' or 'unattributed'.")
    if scope == "unattributed":
        return role, None, True, "unattributed"
    return role, None, False, "all"


def _serialize_api_key_option(record: ApiKeyRecord) -> dict:
    return {
        "id": record.id,
        "name": record.name,
        "disabled": record.disabled,
        "budget_usd": record.budget_usd,
        "spent_usd": record.spent_usd,
        "budget_period": record.budget_period,
        "last_used_at": record.last_used_at,
    }


def _attach_api_key_names(rows: list[dict], key_names: dict[int, str]) -> list[dict]:
    enriched = []
    for row in rows:
        item = dict(row)
        label = str(item.get("label") or "")
        if label == "unattributed":
            item["api_key_id"] = None
            item["api_key_name"] = "Unattributed"
        else:
            try:
                key_id = int(label)
            except ValueError:
                item["api_key_id"] = None
                item["api_key_name"] = label or "Unknown"
            else:
                item["api_key_id"] = key_id
                item["api_key_name"] = key_names.get(key_id, f"Virtual key #{key_id}")
        enriched.append(item)
    return enriched


def _empty_rejection_dashboard() -> dict:
    return {
        "summary": {"rejections": 0},
        "series": [],
        "categories": [],
        "status_codes": [],
        "recent": [],
    }


def _merge_fallback_outcomes(rows: list[dict], outcomes: list[dict]) -> list[dict]:
    """Attach fallback attempt/error stats to usage breakdown rows by label."""
    by_label = {str(item.get("label")): item for item in outcomes}
    merged = []
    for row in rows:
        item = dict(row)
        outcome = by_label.get(str(item.get("label")))
        item["fallback_attempts"] = int(outcome["attempts"]) if outcome else 0
        item["fallback_errors"] = int(outcome["errors"]) if outcome else 0
        item["fallback_success_rate"] = outcome["success_rate"] if outcome else None
        merged.append(item)
    return merged


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


@stats_router.get("/api/usage-stats/dashboard", response_class=JSONResponse, tags=["Usage Stats API"])
async def get_usage_stats_dashboard(
    request: Request,
    range_name: str = Query("30d", alias="range"),
    bucket: str | None = None,
    api_key_id: int | None = None,
    api_key_scope: str | None = None,
    operation: str | None = None,
    gateway_model: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    x_title: str | None = None,
):
    return await get_analytics_dashboard(
        request,
        range_name=range_name,
        bucket=bucket,
        api_key_id=api_key_id,
        api_key_scope=api_key_scope,
        operation=operation,
        gateway_model=gateway_model,
        provider=provider,
        model=model,
        x_title=x_title,
    )


@stats_router.get("/api/usage-stats/{period}", response_class=JSONResponse, tags=["Usage Stats API"])
async def get_aggregated_stats(request: Request, period: str, api_key_id: int | None = None):
    """
    Fetches aggregated token usage statistics by the specified period and model.
    """
    tokens_usage_db = _capture_app_services(request).tokens_usage_db

    try:
        # Validate period input
        if period not in ['hour', 'day', 'week', 'month']:
            raise HTTPException(status_code=400, detail="Invalid period. Must be 'hour', 'day', 'week', or 'month'.")

        api_key_id_filter = _effective_api_key_filter(request)

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
        raise HTTPException(status_code=500, detail="Could not retrieve usage statistics.")

@stats_router.get("/api/usage-records", response_class=JSONResponse, tags=["Usage Stats API"])
async def get_usage_records(
    request: Request, limit: int = 25, offset: int = 0, api_key_id: int | None = None
):
    """
    Fetches the latest token usage records with pagination.
    """
    services = _capture_app_services(request)
    tokens_usage_db = services.tokens_usage_db
    active_requests_registry = services.active_requests_registry

    try:
        limit, offset = validate_usage_records_pagination(limit, offset)
        api_key_id_filter = _effective_api_key_filter(request)

        active_records = active_requests_registry.list_records(
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
        raise HTTPException(status_code=500, detail="Could not retrieve usage records.")


@stats_router.get("/api/analytics-dashboard", response_class=JSONResponse, tags=["Analytics Dashboard API"])
async def get_analytics_dashboard(
    request: Request,
    range_name: str = Query("30d", alias="range"),
    bucket: str | None = None,
    api_key_id: int | None = None,
    api_key_scope: str | None = None,
    operation: str | None = None,
    gateway_model: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    upstream_key_fingerprint: str | None = None,
    usage_source: str | None = None,
    x_title: str | None = None,
    estimated: str | None = None,
    rejection_category: str | None = None,
    rejection_status_code: int | None = None,
):
    """Return a full scoped analytics payload for the usage statistics UI."""
    services = _capture_app_services(request)
    tokens_usage_db = services.tokens_usage_db
    fallback_events_db = services.fallback_events_db
    rejections_db = services.rejections_db
    api_keys_db = services.api_keys_db
    active_requests_registry = services.active_requests_registry

    start_date, end_date, resolved_bucket = _resolve_analytics_window(range_name, bucket)
    role, api_key_id_filter, api_key_unattributed, scope_name = _resolve_analytics_scope(
        request,
        api_key_id,
        api_key_scope,
    )
    is_master = role == ROLE_MASTER
    operation_filter = _optional_query_value(operation)
    gateway_model_filter = _optional_query_value(gateway_model)
    provider_filter = _optional_query_value(provider)
    model_filter = _optional_query_value(model)
    usage_source_filter = _optional_query_value(usage_source)
    x_title_filter = _optional_query_value(x_title)
    if upstream_key_fingerprint and not is_master:
        raise HTTPException(status_code=403, detail="upstream_key_fingerprint filter is available only to the master API key.")
    upstream_key_filter = _optional_query_value(upstream_key_fingerprint) if is_master else None
    estimated_filter = _parse_bool_query(estimated)
    rejection_category_filter = _optional_query_value(rejection_category)
    if rejection_category_filter is not None and rejection_category_filter not in VALID_CATEGORIES:
        raise HTTPException(
            status_code=400,
            detail="rejection_category must be one of: " + ", ".join(sorted(VALID_CATEGORIES)),
        )
    if rejection_status_code is not None and rejection_status_code <= 0:
        raise HTTPException(status_code=400, detail="rejection_status_code must be a positive integer.")

    warnings: list[str] = []
    routing_filters_active = any(
        [
            operation_filter,
            gateway_model_filter,
            provider_filter,
            model_filter,
            upstream_key_filter,
            usage_source_filter,
            x_title_filter,
            estimated_filter is not None,
        ]
    )
    include_rejections = not routing_filters_active
    if routing_filters_active:
        warnings.append(
            "Rejection analytics are hidden while usage routing filters are active because rejection events are not stored with those fields."
        )

    if is_master:
        api_key_records = await asyncio.to_thread(api_keys_db.list_all)
    else:
        own_record = await asyncio.to_thread(api_keys_db.get_by_id, api_key_id_filter)
        api_key_records = [own_record] if own_record is not None else []
    key_names = {record.id: record.name for record in api_key_records}

    usage_task = tokens_usage_db.get_dashboard_usage(
        resolved_bucket,
        start_date,
        end_date,
        api_key_id=api_key_id_filter,
        api_key_unattributed=api_key_unattributed,
        operation=operation_filter,
        gateway_model=gateway_model_filter,
        provider=provider_filter,
        model=model_filter,
        upstream_key_fingerprint=upstream_key_filter,
        usage_source=usage_source_filter,
        x_title=x_title_filter,
        is_estimated=estimated_filter,
        recent_limit=10,
    )
    options_task = tokens_usage_db.get_dashboard_filter_options(
        start_date,
        end_date,
        api_key_id=api_key_id_filter,
        api_key_unattributed=api_key_unattributed,
    )
    fallback_task = fallback_events_db.get_dashboard_fallback(
        resolved_bucket,
        start_date,
        end_date,
        api_key_id=api_key_id_filter,
        api_key_unattributed=api_key_unattributed,
        operation=operation_filter,
        gateway_model=gateway_model_filter,
        provider=provider_filter,
        model=model_filter,
        upstream_key_fingerprint=upstream_key_filter,
    )
    gather_tasks = [usage_task, options_task, fallback_task]
    if include_rejections:
        gather_tasks.append(
            rejections_db.get_dashboard_rejections(
                resolved_bucket,
                start_date,
                end_date,
                api_key_id=api_key_id_filter,
                api_key_unattributed=api_key_unattributed,
                category=rejection_category_filter,
                status_code=rejection_status_code,
            )
        )
    if is_master:
        gather_tasks.append(tokens_usage_db.get_lifetime_totals())
    results = await asyncio.gather(*gather_tasks)
    usage_data, filter_options, fallback_data = results[:3]
    next_index = 3
    if include_rejections:
        rejection_data = results[next_index]
        next_index += 1
    else:
        rejection_data = _empty_rejection_dashboard()
    lifetime_data = results[next_index] if is_master else None

    active_records = active_requests_registry.list_records(
        api_key_id=api_key_id_filter if not api_key_unattributed else None
    )
    if api_key_unattributed:
        active_records = [row for row in active_records if row.get("api_key_id") is None]
    if operation_filter:
        active_records = [row for row in active_records if row.get("operation") == operation_filter]
    if gateway_model_filter:
        active_records = [row for row in active_records if row.get("gateway_model") == gateway_model_filter]
    if provider_filter:
        active_records = [row for row in active_records if row.get("provider") == provider_filter]
    if model_filter:
        active_records = [row for row in active_records if row.get("model") == model_filter]
    if upstream_key_filter:
        active_records = [
            row for row in active_records
            if row.get("upstream_key_fingerprint") == upstream_key_filter
        ]
    if usage_source_filter:
        active_records = [row for row in active_records if row.get("usage_source") == usage_source_filter]
    if x_title_filter:
        active_records = [row for row in active_records if row.get("x_title") == x_title_filter]
    if estimated_filter is not None:
        active_records = [
            row for row in active_records
            if bool(row.get("is_estimated")) == estimated_filter
        ]

    usage_summary = usage_data["summary"]
    total_tokens = int(usage_summary.get("total_tokens") or 0)
    total_cost = float(usage_summary.get("cost") or 0.0)
    cost_per_million_tokens = (total_cost / total_tokens * 1_000_000) if total_tokens else 0.0

    breakdowns = dict(usage_data["breakdowns"])
    breakdowns["api_keys"] = _attach_api_key_names(breakdowns.get("api_keys", []), key_names)
    breakdowns["providers"] = _merge_fallback_outcomes(
        breakdowns.get("providers", []), fallback_data.get("providers", [])
    )
    breakdowns["resolved_targets"] = _merge_fallback_outcomes(
        breakdowns.get("resolved_targets", []), fallback_data.get("targets", [])
    )
    if not is_master:
        breakdowns["api_keys"] = []
        breakdowns["upstream_keys"] = []

    if is_master:
        fallback_payload = {
            key: value
            for key, value in fallback_data.items()
            if key not in {"providers", "targets"}
        }
    else:
        fallback_payload = {
            "summary": fallback_data["summary"],
            "series": fallback_data["series"],
            "error_types": [],
            "upstream": [],
        }

    reliability = {
        "fallback": fallback_payload,
        "rejections": rejection_data,
    }

    filter_options["api_keys"] = [_serialize_api_key_option(record) for record in api_key_records] if is_master else []
    if not is_master:
        filter_options["upstream_keys"] = []

    recent_records = active_records[:5] + _mark_completed_usage_rows(usage_data.get("recent_records", []))
    if not is_master:
        recent_records = [
            {key: value for key, value in row.items() if key != "upstream_key_fingerprint"}
            for row in recent_records
        ]

    payload = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "scope": {
            "role": role,
            "api_key_id": api_key_id_filter,
            "api_key_scope": scope_name,
            "can_filter_keys": False,
            "can_filter_upstream_keys": is_master,
        },
        "filters": {
            "range": range_name,
            "bucket": resolved_bucket,
            "api_key_id": None,
            "api_key_scope": scope_name,
            "operation": operation_filter,
            "gateway_model": gateway_model_filter,
            "provider": provider_filter,
            "model": model_filter,
            "upstream_key_fingerprint": upstream_key_filter,
            "usage_source": usage_source_filter,
            "x_title": x_title_filter,
            "estimated": estimated_filter,
            "rejection_category": rejection_category_filter,
            "rejection_status_code": rejection_status_code,
        },
        "totals": {
            **usage_summary,
            "active_requests": len(active_records),
            "fallback_attempts": fallback_payload["summary"]["attempts"],
            "fallback_errors": fallback_payload["summary"]["errors"],
            "fallback_success_rate": fallback_payload["summary"]["success_rate"],
            "rejections": reliability["rejections"]["summary"]["rejections"],
            "cost_per_million_tokens": cost_per_million_tokens,
        },
        "lifetime": lifetime_data,
        "series": {
            "usage": usage_data["series"],
            "fallback": fallback_payload["series"],
            "rejections": rejection_data["series"],
        },
        "breakdowns": breakdowns,
        "reliability": reliability,
        "recent_records": recent_records,
        "filter_options": filter_options,
        "warnings": warnings,
    }
    return JSONResponse(content=payload)


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


def _iter_fallback_targets(fallback_rules: dict):
    for config in fallback_rules.values():
        if not isinstance(config, dict):
            continue
        for rule in config.get("fallback_models") or []:
            if isinstance(rule, dict):
                yield rule
        context_rule = config.get("context_overflow_fallback")
        if isinstance(context_rule, dict):
            yield context_rule


def _build_health_probe_request(provider_config, model: str, api_key: str | None):
    auth_headers = resolve_provider_config_auth_headers(provider_config, api_key)
    if getattr(provider_config, "type", "openai") == "anthropic":
        return (
            f"{provider_config.baseUrl.rstrip('/')}/v1/messages",
            {
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_API_VERSION,
                **auth_headers,
            },
            {
                "model": model,
                "messages": [{"role": "user", "content": "Reply with OK."}],
                "max_tokens": 1,
            },
        )
    return (
        f"{provider_config.baseUrl.rstrip('/')}/chat/completions",
        {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
            "X-Title": "LLMGateway Upstream Health",
            **auth_headers,
        },
        {
            "model": model,
            "messages": [{"role": "user", "content": "Reply with OK."}],
            "max_tokens": 1,
            "temperature": 0,
        },
    )


def _resolve_health_probe_api_keys(provider_config, pool_name: str | None) -> list[str | None]:
    if not pool_name:
        return resolve_provider_config_api_keys(provider_config) or [None]

    pools = getattr(provider_config, "upstream_key_pools", None)
    pool_config = pools.get(pool_name) if isinstance(pools, dict) else None
    if pool_config is None:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid upstream health configuration: upstream_key_pool '{pool_name}' is not configured.",
        )

    api_keys: list[str | None] = []
    for key_spec in pool_config.keys:
        if not getattr(key_spec, "enabled", True):
            continue
        resolved_value = resolve_provider_api_key_value(key_spec.apikey)
        api_keys.extend(split_api_keys(resolved_value))

    if not api_keys:
        raise HTTPException(
            status_code=500,
            detail=f"Invalid upstream health configuration: upstream_key_pool '{pool_name}' has no enabled keys.",
        )
    return api_keys


@dataclass(frozen=True, slots=True)
class _HealthProbeTarget:
    provider: str
    model: str
    upstream_key_fingerprint: str
    url: str
    headers: dict[str, str]
    payload: dict[str, Any]
    http_client: httpx.AsyncClient


def _materialize_health_probe_targets(
    config_loader: "ConfigLoader",
    proxy_http_clients: Mapping[str, httpx.AsyncClient],
    shared_http_client: httpx.AsyncClient,
) -> tuple[_HealthProbeTarget, ...]:
    targets: list[_HealthProbeTarget] = []
    seen_targets: set[tuple[str, str, str]] = set()
    for rule in _iter_fallback_targets(config_loader.fallback_rules):
        provider_name = rule.get("provider")
        provider_model = rule.get("model")
        if not provider_name or not provider_model:
            continue
        provider_config = config_loader.providers_config.get(provider_name)
        if provider_config is None:
            continue
        api_keys = _resolve_health_probe_api_keys(
            provider_config,
            rule.get("upstream_key_pool"),
        )
        effective_client = proxy_http_clients.get(provider_name, shared_http_client)
        for api_key in api_keys:
            key_fingerprint = fingerprint_api_key(api_key)
            target_key = (provider_name, provider_model, key_fingerprint)
            if target_key in seen_targets:
                continue
            seen_targets.add(target_key)
            target_url, headers, payload = _build_health_probe_request(
                provider_config,
                provider_model,
                api_key,
            )
            targets.append(
                _HealthProbeTarget(
                    provider=provider_name,
                    model=provider_model,
                    upstream_key_fingerprint=key_fingerprint,
                    url=target_url,
                    headers=dict(headers),
                    payload=dict(payload),
                    http_client=effective_client,
                )
            )
    return tuple(targets)


@stats_router.get("/api/fallback-stats/{period}", response_class=JSONResponse, tags=["Fallback Analytics API"])
async def get_fallback_stats(request: Request, period: str):
    """
    Fetches aggregated fallback failure statistics by the specified period.
    Only includes failed attempts (not successes).
    """
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Fallback analytics are available only to the master API key.")

    fallback_events_db = _capture_app_services(request).fallback_events_db

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
        raise HTTPException(status_code=500, detail="Could not retrieve fallback statistics.")


@stats_router.get("/api/fallback-records", response_class=JSONResponse, tags=["Fallback Analytics API"])
async def get_fallback_records(request: Request, limit: int = 25, offset: int = 0):
    """
    Fetches fallback chain records with pagination.
    Only includes requests where 2+ attempts were made (i.e., fallbacks occurred).
    """
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Fallback analytics are available only to the master API key.")

    fallback_events_db = _capture_app_services(request).fallback_events_db

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
        raise HTTPException(status_code=500, detail="Could not retrieve fallback records.")


@stats_router.get("/api/upstream-status", response_class=JSONResponse, tags=["Upstream Analytics API"])
async def get_upstream_status(request: Request):
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Upstream status is available only to the master API key.")
    upstream_state = _capture_app_services(request).upstream_routing_state
    return JSONResponse(content={"rows": upstream_state.get_status_rows()})


@stats_router.post("/api/upstream-health/run", response_class=JSONResponse, tags=["Upstream Analytics API"])
async def run_upstream_health_checks(request: Request):
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Upstream health checks are available only to the master API key.")

    services = _capture_app_services(request)
    runtime_snapshot = _capture_runtime_snapshot(request)
    upstream_state = services.upstream_routing_state
    targets = _materialize_health_probe_targets(
        runtime_snapshot.config_loader,
        runtime_snapshot.proxy_http_clients,
        services.http_client,
    )

    checked_rows: list[dict] = []
    for target in targets:
        health_status = "error"
        last_error = None
        try:
            response = await target.http_client.post(
                target.url,
                headers=target.headers,
                json=target.payload,
                timeout=15.0,
            )
            if response.status_code < 400:
                health_status = "healthy"
            elif response.status_code in {401, 403}:
                health_status = "invalid"
                last_error = f"HTTP {response.status_code}"
            else:
                last_error = f"HTTP {response.status_code}"
        except httpx.HTTPError as exc:
            last_error = exc.__class__.__name__
        upstream_state.mark_health(
            target.provider,
            target.model,
            target.upstream_key_fingerprint,
            health_status,
            last_error,
        )
        checked_rows.append(
            {
                "provider": target.provider,
                "model": target.model,
                "upstream_key_fingerprint": target.upstream_key_fingerprint,
                "health_status": health_status,
                "last_error": last_error,
            }
        )

    return JSONResponse(content={"checked": len(checked_rows), "rows": checked_rows})


@stats_router.get("/api/upstream-stats/{period}", response_class=JSONResponse, tags=["Upstream Analytics API"])
async def get_upstream_stats(request: Request, period: str, api_key_id: int | None = None):
    if not _is_master_caller(request):
        raise HTTPException(status_code=403, detail="Upstream analytics are available only to the master API key.")

    fallback_events_db = _capture_app_services(request).fallback_events_db

    try:
        if period not in ['hour', 'day', 'week', 'month']:
            raise HTTPException(status_code=400, detail="Invalid period. Must be 'hour', 'day', 'week', or 'month'.")
        api_key_id_filter = _effective_api_key_filter(request)

        async def _fetch():
            start_date = _get_period_start_date(period)
            end_date = datetime.now(timezone.utc)
            return await fallback_events_db.get_upstream_stats(
                period,
                start_date=start_date,
                end_date=end_date,
                api_key_id=api_key_id_filter,
            )

        cache_key = (period, api_key_id_filter)
        upstream_stats = await _upstream_stats_cache.get_or_compute(cache_key, _fetch)
        return JSONResponse(content=upstream_stats)
    except HTTPException:
        raise
    except Exception as e:
        logging.error(f"Error fetching upstream stats for period '{period}': {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not retrieve upstream statistics.")
