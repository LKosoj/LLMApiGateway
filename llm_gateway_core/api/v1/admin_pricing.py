"""Admin endpoints for managing per-model pricing and cost calculation.

All endpoints require the master role (GATEWAY_API_KEY). Virtual keys
receive a 403 enforced by the auth middleware via MASTER_ONLY_PREFIXES.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ...config.paths import STATIC_DIR
from ...middleware.auth import ROLE_MASTER
from ...utils.html_cache import get_template
from ...utils.usage_tracking import (
    ModelCostRates,
    build_model_cost_rate_registry,
    calculate_model_cost_usd,
)

# Re-use the atomic write and JSON5 comment helpers from the rules editor.
from .rules_editor import _backup_if_has_comments, _write_text_atomically

logger = logging.getLogger(__name__)

admin_pricing_router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    input_rate: float = Field(..., ge=0)
    output_rate: float = Field(..., ge=0)


class PricingListResponse(BaseModel):
    items: list[ModelPricing]


class PricingUpdateRequest(BaseModel):
    items: list[ModelPricing]


class CalculateRequest(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    prompt_tokens: int = Field(..., ge=0)
    completion_tokens: int = Field(..., ge=0)


class CalculateResponse(BaseModel):
    cost_usd: float
    input_rate: float
    output_rate: float


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _require_master(request: Request) -> None:
    role = getattr(request.state, "api_key_role", None)
    if role != ROLE_MASTER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This endpoint is reserved for the master API key",
        )


def _get_config_loader(request: Request):
    loader = getattr(request.app.state, "config_loader", None)
    if loader is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="ConfigLoader not available",
        )
    return loader


def _get_providers_path(request: Request) -> Path:
    loader = _get_config_loader(request)
    return loader.providers_path


def _build_registry(request: Request) -> dict[tuple[str, str], ModelCostRates]:
    loader = _get_config_loader(request)
    return build_model_cost_rate_registry(loader.providers_config)


def _pricing_items_from_registry(
    registry: dict[tuple[str, str], ModelCostRates],
) -> list[ModelPricing]:
    return [
        ModelPricing(
            provider=provider,
            model=model,
            input_rate=rates.input_rate,
            output_rate=rates.output_rate,
        )
        for (provider, model), rates in sorted(registry.items())
    ]


def _rewrite_providers_json(
    providers_path: Path,
    new_items: list[ModelPricing],
) -> None:
    """Update the `models` block for every provider in providers.json.

    Preserves JSON5 comments via a backup (uses the same mechanism as
    rules_editor). Writes atomically.
    """
    if not providers_path.exists():
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"providers.json not found at {providers_path}",
        )

    try:
        import json5 as _json5

        raw = providers_path.read_text(encoding="utf-8")
        provider_list: list[Any] = _json5.loads(raw)
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Failed to parse providers.json: {exc}",
        ) from exc

    if not isinstance(provider_list, list):
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="providers.json root must be a list",
        )

    # Build lookup: provider_name -> {model_name: {input_rate, output_rate}}
    new_models_by_provider: dict[str, dict[str, dict[str, float]]] = {}
    for item in new_items:
        new_models_by_provider.setdefault(item.provider, {})[item.model] = {
            "input_rate": item.input_rate,
            "output_rate": item.output_rate,
        }

    # Patch each provider entry in the list
    for entry in provider_list:
        if not isinstance(entry, dict):
            continue
        for provider_name, details in entry.items():
            if not isinstance(details, dict):
                continue
            if provider_name in new_models_by_provider:
                details["models"] = new_models_by_provider[provider_name]
            else:
                # Remove models block if provider has no rates in new list
                details.pop("models", None)

    # Back up before overwriting in case file has JSON5 comments
    _backup_if_has_comments(providers_path)

    new_content = json.dumps(provider_list, ensure_ascii=False, indent=2) + "\n"
    _write_text_atomically(providers_path, new_content)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@admin_pricing_router.get("/ui/pricing", response_class=HTMLResponse, include_in_schema=False)
async def get_pricing_page(request: Request):
    _require_master(request)
    html_path = STATIC_DIR / "pricing.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="Pricing UI page not found.")
    return HTMLResponse(content=await get_template(html_path))


@admin_pricing_router.get(
    "/admin/pricing",
    response_model=PricingListResponse,
    tags=["Admin Pricing V1"],
)
async def get_pricing(request: Request) -> PricingListResponse:
    _require_master(request)
    registry = _build_registry(request)
    return PricingListResponse(items=_pricing_items_from_registry(registry))


@admin_pricing_router.put(
    "/admin/pricing",
    response_model=PricingListResponse,
    tags=["Admin Pricing V1"],
)
async def update_pricing(
    request: Request,
    payload: PricingUpdateRequest,
) -> PricingListResponse:
    _require_master(request)
    providers_path = _get_providers_path(request)

    _rewrite_providers_json(providers_path, payload.items)

    # Hot-reload: update providers_config in ConfigLoader
    loader = _get_config_loader(request)
    reloaded = loader.reload_providers_config()
    if not reloaded:
        logger.warning(
            "Pricing saved to disk but hot-reload of providers_config failed; "
            "restart may be required for changes to take effect."
        )

    # Rebuild registry from freshly reloaded config
    registry = build_model_cost_rate_registry(loader.providers_config)

    # Update cost_rate_registry on app.state if it exists
    if hasattr(request.app.state, "cost_rate_registry"):
        request.app.state.cost_rate_registry = registry

    return PricingListResponse(items=_pricing_items_from_registry(registry))


@admin_pricing_router.post(
    "/admin/pricing/calculate",
    response_model=CalculateResponse,
    tags=["Admin Pricing V1"],
)
async def calculate_cost(
    request: Request,
    payload: CalculateRequest,
) -> CalculateResponse:
    _require_master(request)
    registry = _build_registry(request)

    rates = registry.get((payload.provider, payload.model))
    if rates is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Model '{payload.model}' for provider '{payload.provider}' not found in pricing registry",
        )

    cost = calculate_model_cost_usd(
        prompt_tokens=payload.prompt_tokens,
        completion_tokens=payload.completion_tokens,
        rates=rates,
    )
    if cost is None:
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Invalid pricing for model '{payload.model}' and provider '{payload.provider}'",
        )

    return CalculateResponse(
        cost_usd=cost,
        input_rate=rates.input_rate,
        output_rate=rates.output_rate,
    )
