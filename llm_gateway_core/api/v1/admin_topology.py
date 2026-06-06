"""Provider topology endpoint.

Returns a ReactFlow-compatible routing map describing how gateway models route
to providers via their fallback chains, enriched with live provider health,
active request counts and penalty scores.

Graph structure (three tiers):
- ``gateway`` — the central node.
- ``model:<gateway_model_name>`` — one per fallback rule.
- ``provider:<name>`` — the union of configured providers and any provider
  referenced by a fallback target. A provider referenced by a rule but missing
  from the configuration is marked ``health: "invalid"`` / ``configured: false``.

Edges:
- ``gateway -> model:<name>`` (``alias``) — structural.
- ``model:<name> -> provider:<p>`` (``fallback``) — one per provider reached by
  the rule's ``fallback_models``; multiple targets on the same provider collapse
  into a single edge carrying the minimum priority and the list of models.
- ``model:<name> -> provider:<p>`` (``context_overflow``) — the optional
  ``context_overflow_fallback`` target.

Access rules:
- Master key: sees all providers and all active requests.
- Virtual key: sees all providers, but active_requests counts are
  filtered to the caller's own requests only.
- Anonymous: 401 (handled by auth middleware).
"""
from __future__ import annotations

import logging
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


def _referenced_providers(fallback_rules: dict[str, Any]) -> set[str]:
    """Collect every provider name referenced by any fallback target."""
    referenced: set[str] = set()
    for cfg in fallback_rules.values():
        if not isinstance(cfg, dict):
            continue
        for fm in cfg.get("fallback_models") or []:
            provider = fm.get("provider")
            if provider:
                referenced.add(provider)
        ctx = cfg.get("context_overflow_fallback")
        if isinstance(ctx, dict) and ctx.get("provider"):
            referenced.add(ctx["provider"])
    return referenced


@router.get("/topology", tags=["Topology"])
async def get_topology(request: Request) -> JSONResponse:
    """Return the routing-map topology graph for the ReactFlow UI."""
    role = getattr(request.state, "api_key_role", ROLE_USER)
    key_id: int | None = getattr(request.state, "api_key_id", None)

    is_master = role == ROLE_MASTER
    cache_key = ("topology", role, key_id)

    async def _build() -> dict[str, Any]:
        config_loader = getattr(request.app.state, "config_loader", None)
        providers_config: dict[str, Any] = {}
        fallback_rules: dict[str, Any] = {}
        if config_loader is not None:
            providers_config = config_loader.providers_config or {}
            fallback_rules = getattr(config_loader, "fallback_rules", None) or {}

        registry = get_active_requests_registry(request.app)
        active_records = registry.list_records(api_key_id=None if is_master else key_id)

        upstream_state: UpstreamRoutingState | None = getattr(
            request.app.state, "upstream_routing_state", None
        )
        status_rows: list[dict[str, Any]] = []
        if isinstance(upstream_state, UpstreamRoutingState):
            status_rows = upstream_state.get_status_rows()

        # Active request tallies. Each record carries both ``gateway_model`` and
        # ``provider`` once routing is resolved, so we can attribute live traffic
        # to the exact ``model -> provider`` edge — not just the provider node.
        active_by_provider: dict[str, int] = {}
        active_by_model: dict[str, int] = {}
        active_by_route: dict[tuple[str, str], int] = {}
        for rec in active_records:
            prov = rec.get("provider")
            gm = rec.get("gateway_model")
            if prov:
                active_by_provider[prov] = active_by_provider.get(prov, 0) + 1
            if gm:
                active_by_model[gm] = active_by_model.get(gm, 0) + 1
            if prov and gm:
                key = (gm, prov)
                active_by_route[key] = active_by_route.get(key, 0) + 1

        # Provider set = configured providers (stable order) followed by any
        # provider referenced only by a rule (sorted for determinism).
        configured = list(providers_config.keys())
        extra = sorted(_referenced_providers(fallback_rules) - set(configured))
        all_providers = [*configured, *extra]

        nodes: list[dict[str, Any]] = [
            {
                "id": "gateway",
                "type": "central",
                "label": "LLM Gateway",
                "data": {},
                "position": {"x": 0, "y": 0},
            }
        ]

        health_by_provider: dict[str, str] = {}

        # Provider tier (right column in the frontend layout).
        for i, pname in enumerate(all_providers):
            is_configured = pname in providers_config
            health = _provider_health(pname, status_rows) if is_configured else "invalid"
            health_by_provider[pname] = health
            nodes.append(
                {
                    "id": f"provider:{pname}",
                    "type": "provider",
                    "label": pname,
                    "data": {
                        "health": health,
                        "configured": is_configured,
                        "active_requests": active_by_provider.get(pname, 0),
                        "penalty": _provider_penalty(pname, status_rows),
                        "models": _provider_models(pname, status_rows),
                    },
                    "position": {"x": 600, "y": i * 100},
                }
            )

        edges: list[dict[str, Any]] = []

        # Model tier (middle column) + routing edges.
        for mi, gm in enumerate(fallback_rules.keys()):
            cfg = fallback_rules.get(gm) or {}
            model_active = active_by_model.get(gm, 0)

            flags = {
                "rotate_models": bool(cfg.get("rotate_models", False)),
                "dynamic_penalty": bool(cfg.get("dynamic_penalty", False)),
                "strip_think_tags": bool(cfg.get("strip_think_tags", False)),
                "compress_tool_results": bool(cfg.get("compress_tool_results", False)),
            }

            # Ordered chain (for the detail panel) + per-provider aggregation
            # (for the edges). ``priority`` is the 1-based step in the chain.
            chain: list[dict[str, Any]] = []
            prov_agg: dict[str, dict[str, Any]] = {}
            for idx, fm in enumerate(cfg.get("fallback_models") or []):
                provider = fm.get("provider")
                model = fm.get("model")
                step = idx + 1
                chain.append(
                    {
                        "priority": step,
                        "provider": provider,
                        "model": model,
                        "retry_count": fm.get("retry_count"),
                        "retry_delay": fm.get("retry_delay"),
                    }
                )
                if not provider:
                    continue
                agg = prov_agg.setdefault(
                    provider, {"priority": step, "models": [], "steps": []}
                )
                agg["priority"] = min(agg["priority"], step)
                if model and model not in agg["models"]:
                    agg["models"].append(model)
                agg["steps"].append(step)

            model_data: dict[str, Any] = {
                "active_requests": model_active,
                "flags": flags,
                "max_total_attempts": cfg.get("max_total_attempts"),
                "chain": chain,
            }

            ctx = cfg.get("context_overflow_fallback")
            ctx_target: dict[str, Any] | None = None
            if isinstance(ctx, dict) and ctx.get("provider"):
                ctx_target = {"provider": ctx.get("provider"), "model": ctx.get("model")}
                model_data["context_overflow"] = ctx_target

            nodes.append(
                {
                    "id": f"model:{gm}",
                    "type": "model",
                    "label": gm,
                    "data": model_data,
                    "position": {"x": 300, "y": mi * 100},
                }
            )

            edges.append(
                {
                    "id": f"gateway->model:{gm}",
                    "source": "gateway",
                    "target": f"model:{gm}",
                    "type": "alias",
                    "animated": model_active > 0,
                    "data": {"active": model_active},
                }
            )

            for provider, agg in prov_agg.items():
                route_active = active_by_route.get((gm, provider), 0)
                edges.append(
                    {
                        "id": f"model:{gm}->provider:{provider}",
                        "source": f"model:{gm}",
                        "target": f"provider:{provider}",
                        "type": "fallback",
                        "animated": route_active > 0,
                        "data": {
                            "kind": "fallback",
                            "priority": agg["priority"],
                            "steps": sorted(agg["steps"]),
                            "models": agg["models"],
                            "active": route_active,
                        },
                    }
                )

            if ctx_target:
                provider = ctx_target["provider"]
                edges.append(
                    {
                        "id": f"model:{gm}->provider:{provider}:ctx",
                        "source": f"model:{gm}",
                        "target": f"provider:{provider}",
                        "type": "context_overflow",
                        "animated": False,
                        "data": {
                            "kind": "context_overflow",
                            "models": [ctx_target["model"]] if ctx_target.get("model") else [],
                        },
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
