"""Admin endpoints for managing per-model pricing and cost calculation.

All endpoints require the master role (GATEWAY_API_KEY). Virtual keys
receive a 403 enforced by the auth middleware via MASTER_ONLY_PREFIXES.
"""

from __future__ import annotations

import json
import math
from collections.abc import Mapping
from typing import Any

import json5
from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, ValidationError, field_validator

from ...config.config_store import (
    ConfigFile,
    ConfigSourceBundle,
    ConfigSourceError,
)
from ...config.paths import STATIC_DIR
from ...middleware.auth import ROLE_MASTER
from ...services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from ...services.pricing_autofill import (
    PricingClassification,
    PricingRow,
    PricingSource,
    classify_pricing_rows,
    collect_operation_used_models,
    collect_used_models,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ...utils.html_cache import get_template
from ...utils.usage_tracking import (
    ModelCostRates,
    calculate_model_cost_usd,
)
from .config_update_http import (
    ConfigRequestInvalid,
    RawConfigBody,
    capture_config_update_runtime,
    config_error_response,
    config_response_headers,
    parse_config_if_match,
    read_raw_config_body,
)

admin_pricing_router = APIRouter()


# ---------------------------------------------------------------------------
# Pydantic models
# ---------------------------------------------------------------------------


class ModelPricing(BaseModel):
    provider: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    input_rate: float = Field(..., ge=0)
    output_rate: float = Field(..., ge=0)

    @field_validator("provider", "model")
    @classmethod
    def validate_identifier(cls, value: str) -> str:
        if not value or value != value.strip():
            raise ValueError("pricing identifiers must be non-empty and untrimmed")
        return value

    @field_validator("input_rate", "output_rate", mode="before")
    @classmethod
    def validate_rate(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("pricing rates must be finite numbers")
        try:
            rate = float(value)
        except (TypeError, ValueError):
            return value
        if not math.isfinite(rate):
            raise ValueError("pricing rates must be finite numbers")
        return value


class PricingListItem(BaseModel):
    """One ``/admin/pricing`` row.

    ``source`` explains where the rate came from (or why there isn't one):
    an operator-configured ``providers.json`` entry, a rate auto-filled from
    the OpenRouter catalog, or one of the "no configured rate yet" statuses
    for a model the runtime config actually uses. ``input_rate``/
    ``output_rate`` are only populated for ``configured``/
    ``openrouter_autofill`` rows; ``default_cost_per_request`` is only
    populated for ``operation_default`` rows.
    """

    provider: str
    model: str
    source: PricingSource
    input_rate: float | None = None
    output_rate: float | None = None
    default_cost_per_request: float | None = None


class PricingListResponse(BaseModel):
    items: list[PricingListItem]


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


class PricingPatchError(ValueError):
    """Credential-safe failure while applying a full-list pricing update."""

    def __init__(self) -> None:
        super().__init__("Pricing update is invalid.")


PRICING_UPDATE_OPENAPI = {
    "requestBody": {
        "required": True,
        "content": {
            "application/json": {
                "schema": {
                    "type": "object",
                    "required": ["items"],
                    "properties": {
                        "items": {
                            "type": "array",
                            # Inlined (not a "$ref": ".../ModelPricing") on
                            # purpose: FastAPI only registers a model under
                            # components/schemas when it appears in an actual
                            # response_model or Body() parameter. ModelPricing
                            # is validated manually (see update_pricing) and no
                            # longer nested in any response model now that GET
                            # and PUT both respond with PricingListItem, so a
                            # "$ref" here would point at a schema that doesn't
                            # exist. Inlining sidesteps that entirely; see the
                            # same pattern in rules_editor.py.
                            "items": ModelPricing.model_json_schema(),
                        }
                    },
                },
            }
        },
    }
}


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


def _pricing_items_from_registry(
    registry: Mapping[tuple[str, str], ModelCostRates],
) -> list[PricingListItem]:
    return [
        PricingListItem(
            provider=provider,
            model=model,
            source=PricingSource.CONFIGURED,
            input_rate=rates.input_rate,
            output_rate=rates.output_rate,
        )
        for (provider, model), rates in sorted(registry.items())
    ]


def _row_to_item(row: PricingRow) -> PricingListItem:
    return PricingListItem(
        provider=row.provider,
        model=row.model,
        source=row.source,
        input_rate=row.input_rate,
        output_rate=row.output_rate,
        default_cost_per_request=row.default_cost_per_request,
    )


async def _apply_pricing_autofill(
    services: AppServices,
    snapshot: RuntimeSnapshot,
    classification: PricingClassification,
) -> RuntimeSnapshot:
    """Best-effort persist of OpenRouter-autofilled rates into providers.json.

    The GET response is already fully classified from ``classification``
    (computed before this write), so any failure here (a concurrent edit,
    a source read/decode failure, an unexpected validation failure) is
    absorbed silently: the caller still returns a correct, complete payload
    from the pre-write ``snapshot``, and the next GET simply retries the
    autofill. This never raises.
    """
    items = [
        ModelPricing(
            provider=row.provider,
            model=row.model,
            input_rate=row.input_rate,
            output_rate=row.output_rate,
        )
        for row in classification.rows
        if row.source in (PricingSource.CONFIGURED, PricingSource.OPENROUTER_AUTOFILL)
    ]
    try:
        candidate_text = patch_provider_pricing(
            _provider_source_text(snapshot),
            items,
        )
        result = await services.config_update_coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.PROVIDERS,
            candidate_bytes=candidate_text.encode("utf-8"),
            expected_revision=None,
            comments_backup=True,
        )
    except (ConfigUpdateError, PricingPatchError, ValidationError):
        return snapshot
    return result.snapshot


def patch_provider_pricing(
    source_text: str,
    items: list[ModelPricing],
) -> str:
    """Apply a full pricing list without replacing unrelated provider metadata."""
    if not isinstance(source_text, str) or not isinstance(items, list):
        raise PricingPatchError

    try:
        provider_list = json5.loads(source_text)
    except ValueError:
        raise PricingPatchError from None
    if not isinstance(provider_list, list):
        raise PricingPatchError

    providers: dict[str, dict[str, Any]] = {}
    for entry in provider_list:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise PricingPatchError
        provider_name, details = next(iter(entry.items()))
        if (
            not isinstance(provider_name, str)
            or not provider_name
            or not isinstance(details, dict)
            or provider_name in providers
        ):
            raise PricingPatchError
        models = details.get("models")
        if "models" in details:
            if not isinstance(models, dict):
                raise PricingPatchError
            if any(not isinstance(metadata, dict) for metadata in models.values()):
                raise PricingPatchError
        providers[provider_name] = details

    rates_by_model: dict[tuple[str, str], tuple[float, float]] = {}
    for item in items:
        if not isinstance(item, ModelPricing):
            raise PricingPatchError
        if (
            not isinstance(item.provider, str)
            or not isinstance(item.model, str)
            or not item.provider
            or item.provider != item.provider.strip()
            or not item.model
            or item.model != item.model.strip()
            or item.provider not in providers
            or isinstance(item.input_rate, bool)
            or isinstance(item.output_rate, bool)
        ):
            raise PricingPatchError
        try:
            input_rate = float(item.input_rate)
            output_rate = float(item.output_rate)
        except (TypeError, ValueError):
            raise PricingPatchError from None
        if (
            not math.isfinite(input_rate)
            or not math.isfinite(output_rate)
            or input_rate < 0
            or output_rate < 0
        ):
            raise PricingPatchError
        key = (item.provider, item.model)
        if key in rates_by_model:
            raise PricingPatchError
        rates_by_model[key] = (input_rate, output_rate)

    for provider_name, details in providers.items():
        models = details.get("models")
        if not isinstance(models, dict):
            continue
        for model_name in tuple(models):
            metadata = models[model_name]
            if (provider_name, model_name) in rates_by_model:
                continue
            metadata.pop("input_rate", None)
            metadata.pop("output_rate", None)
            if not metadata:
                del models[model_name]

    for (provider_name, model_name), (input_rate, output_rate) in rates_by_model.items():
        details = providers[provider_name]
        models = details.get("models")
        if models is None:
            models = {}
            details["models"] = models
        metadata = models.get(model_name)
        if metadata is None:
            metadata = {}
            models[model_name] = metadata
        metadata["input_rate"] = input_rate
        metadata["output_rate"] = output_rate

    try:
        return json.dumps(
            provider_list,
            ensure_ascii=False,
            indent=2,
            allow_nan=False,
        ) + "\n"
    except (TypeError, ValueError):
        raise PricingPatchError from None


def _snapshot_source_bundle(snapshot: RuntimeSnapshot) -> ConfigSourceBundle:
    bundle = snapshot.config_loader.source_bundle
    if not isinstance(bundle, ConfigSourceBundle):
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    return bundle


def _provider_source_text(snapshot: RuntimeSnapshot) -> str:
    document = _snapshot_source_bundle(snapshot)[ConfigFile.PROVIDERS]
    if not document.exists or document.content is None:
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    content = document.content
    if content.startswith(b"\xef\xbb\xbf"):
        content = content[3:]
    try:
        return content.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ConfigUpdateError(
            ConfigUpdateErrorCode.UPDATE_UNAVAILABLE
        ) from None


def _pricing_json_response(
    snapshot: RuntimeSnapshot,
    payload: PricingListResponse,
) -> JSONResponse:
    return JSONResponse(
        content=payload.model_dump(mode="json"),
        headers=config_response_headers(snapshot, ConfigFile.PROVIDERS),
    )


async def _read_config_update_request(
    request: Request,
) -> tuple[ConfigRevision | None, RawConfigBody]:
    expected_revision = parse_config_if_match(
        request,
        ConfigFile.PROVIDERS,
    )
    raw_body = await read_raw_config_body(request)
    return expected_revision, raw_body


def _decode_pricing_envelope(raw_body: RawConfigBody) -> Any:
    try:
        return json.loads(raw_body.validation_text)
    except (json.JSONDecodeError, RecursionError):
        raise ConfigRequestInvalid from None


def _capture_config_update_base(
    request: Request,
    expected_revision: ConfigRevision | None,
) -> tuple[AppServices, RuntimeSnapshot, ConfigUpdateCoordinator]:
    services, snapshot = capture_config_update_runtime(request)
    coordinator = services.config_update_coordinator
    coordinator.check_base(
        base_snapshot=snapshot,
        config_file=ConfigFile.PROVIDERS,
        expected_revision=expected_revision,
    )
    source_bundle = _snapshot_source_bundle(snapshot)
    try:
        current_bundle = source_bundle.recapture()
    except ConfigSourceError:
        raise ConfigUpdateError(
            ConfigUpdateErrorCode.REVISION_CONFLICT
        ) from None
    if current_bundle != source_bundle:
        raise ConfigUpdateError(ConfigUpdateErrorCode.REVISION_CONFLICT)
    return services, snapshot, coordinator


def _validation_failed() -> ConfigUpdateError:
    return ConfigUpdateError(ConfigUpdateErrorCode.VALIDATION_FAILED)


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
async def get_pricing(request: Request) -> JSONResponse:
    _require_master(request)
    try:
        services, snapshot = capture_config_update_runtime(request)
        used_models = collect_used_models(snapshot.config_loader)
        operation_model_pairs = collect_operation_used_models(snapshot.config_loader)
        openrouter_catalog = await services.openrouter_free_models_service.get_pricing_catalog()
        classification = classify_pricing_rows(
            used_models=used_models,
            configured_registry=snapshot.cost_rate_registry,
            operation_model_pairs=operation_model_pairs,
            openrouter_catalog=openrouter_catalog,
        )
        if classification.autofill_additions:
            snapshot = await _apply_pricing_autofill(services, snapshot, classification)
        payload = PricingListResponse(
            items=[_row_to_item(row) for row in classification.rows]
        )
        return _pricing_json_response(snapshot, payload)
    except ConfigUpdateError as exc:
        return config_error_response(exc)


@admin_pricing_router.put(
    "/admin/pricing",
    response_model=PricingListResponse,
    tags=["Admin Pricing V1"],
    openapi_extra=PRICING_UPDATE_OPENAPI,
)
async def update_pricing(
    request: Request,
) -> JSONResponse:
    _require_master(request)
    try:
        expected_revision, raw_body = await _read_config_update_request(
            request
        )
        envelope = _decode_pricing_envelope(raw_body)
        _services, snapshot, coordinator = _capture_config_update_base(
            request,
            expected_revision,
        )
        try:
            payload = PricingUpdateRequest.model_validate(envelope)
            candidate_text = patch_provider_pricing(
                _provider_source_text(snapshot),
                payload.items,
            )
        except (PricingPatchError, ValidationError):
            raise _validation_failed() from None

        result = await coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.PROVIDERS,
            candidate_bytes=candidate_text.encode("utf-8"),
            expected_revision=expected_revision,
            comments_backup=True,
        )
        response_payload = PricingListResponse(
            items=_pricing_items_from_registry(
                result.snapshot.cost_rate_registry,
            )
        )
        return _pricing_json_response(result.snapshot, response_payload)
    except (ConfigRequestInvalid, ConfigUpdateError) as exc:
        return config_error_response(exc)


@admin_pricing_router.post(
    "/admin/pricing/calculate",
    response_model=CalculateResponse,
    tags=["Admin Pricing V1"],
)
async def calculate_cost(
    request: Request,
    payload: CalculateRequest,
) -> CalculateResponse | JSONResponse:
    _require_master(request)
    try:
        _services, snapshot = capture_config_update_runtime(request)
    except ConfigUpdateError as exc:
        return config_error_response(exc)

    rates = snapshot.cost_rate_registry.get((payload.provider, payload.model))
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
