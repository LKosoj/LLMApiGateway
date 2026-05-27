"""Provider topology endpoint.

Returns a ReactFlow-compatible graph describing configured providers,
their health, active request counts and penalty scores.

Access rules:
- Master key: sees all providers and all active requests.
- Virtual key: sees all providers, but active_requests counts are
  filtered to the caller's own requests only.
- Anonymous: 401 (handled by auth middleware).
"""
from __future__ import annotations

import logging
import math
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.active_requests import get_active_requests_registry
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.utils.ttl_cache import AsyncTtlCache

logger = logging.getLogger(__name__)

router = APIRouter()

_topology_cache = AsyncTtlCache(5.0)

_RADIUS = 300


def _provider_health(provider: str, status_rows: list[dict[str, Any]]) -> str:
    """Return worst health status for a provider across all its models/keys."""
    provider_rows = [r for r in status_rows if r.get("provider") == provider]
    if not provider_rows:
        return "ok"
    statuses = {r.get("health_status", "ok") for r in provider_rows}
    if "error" in statuses:
        return "error"
    if "invalid" in statuses:
        return "invalid"
    return "ok"


def _provider_penalty(provider: str, status_rows: list[dict[str, Any]]) -> float:
    """Return max penalty score for a provider."""
    provider_rows = [r for r in status_rows if r.get("provider") == provider]
    if not provider_rows:
        return 0.0
    return max(float(r.get("penalty_score", 0.0)) for r in provider_rows)


def _provider_models(provider: str, status_rows: list[dict[str, Any]]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for r in status_rows:
        if r.get("provider") == provider:
            model = r.get("model", "")
            if model and model not in seen:
                seen.add(model)
                result.append(model)
    return sorted(result)


@router.get("/topology", tags=["Topology"])
async def get_topology(request: Request) -> JSONResponse:
    """Return provider topology graph for the ReactFlow UI."""
    role = getattr(request.state, "api_key_role", ROLE_USER)
    key_id: int | None = getattr(request.state, "api_key_id", None)

    is_master = role == ROLE_MASTER
    cache_key = ("topology", role, key_id)

    async def _build() -> dict[str, Any]:
        config_loader = getattr(request.app.state, "config_loader", None)
        providers: dict[str, Any] = {}
        if config_loader is not None:
            providers = config_loader.providers_config or {}

        registry = get_active_requests_registry(request.app)
        active_records = registry.list_records(api_key_id=None if is_master else key_id)

        upstream_state: UpstreamRoutingState | None = getattr(
            request.app.state, "upstream_routing_state", None
        )
        status_rows: list[dict[str, Any]] = []
        if isinstance(upstream_state, UpstreamRoutingState):
            status_rows = upstream_state.get_status_rows()

        provider_names = list(providers.keys())
        n = len(provider_names)

        # Central gateway node
        nodes: list[dict[str, Any]] = [
            {
                "id": "gateway",
                "type": "central",
                "label": "LLM Gateway",
                "data": {},
                "position": {"x": 0, "y": 0},
            }
        ]

        # Active requests count by provider (filtered per role)
        active_by_provider: dict[str, int] = {}
        for rec in active_records:
            prov = rec.get("provider")
            if prov:
                active_by_provider[prov] = active_by_provider.get(prov, 0) + 1

        # Health by provider
        health_by_provider: dict[str, str] = {}

        edges: list[dict[str, Any]] = []

        for i, pname in enumerate(provider_names):
            angle = 2 * math.pi * i / n if n > 0 else 0
            x = round(_RADIUS * math.cos(angle))
            y = round(_RADIUS * math.sin(angle))

            health = _provider_health(pname, status_rows)
            health_by_provider[pname] = health

            nodes.append(
                {
                    "id": f"provider:{pname}",
                    "type": "provider",
                    "label": pname,
                    "data": {
                        "health": health,
                        "active_requests": active_by_provider.get(pname, 0),
                        "penalty": _provider_penalty(pname, status_rows),
                        "models": _provider_models(pname, status_rows),
                    },
                    "position": {"x": x, "y": y},
                }
            )
            edges.append(
                {
                    "id": f"gateway->{pname}",
                    "source": "gateway",
                    "target": f"provider:{pname}",
                    "animated": active_by_provider.get(pname, 0) > 0,
                }
            )

        return {
            "nodes": nodes,
            "edges": edges,
            "active_count_by_provider": active_by_provider,
            "health_by_provider": health_by_provider,
        }

    result = await _topology_cache.get_or_compute(cache_key, _build)
    return JSONResponse(content=result)
