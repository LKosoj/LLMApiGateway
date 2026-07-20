"""Admin CRUD endpoints for virtual API keys.

All endpoints here require the master role — that is, the caller must hold the
``GATEWAY_API_KEY`` secret (either via ``Authorization`` header or via a master
session cookie). Virtual key holders cannot create, modify, or delete keys.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import math
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request, Response, status
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field

from ..accounting_http import (
    AccountingHttpUse,
    accounting_error_response,
)
from ..error_envelope import error_response
from ...config.paths import STATIC_DIR
from ...db.api_keys_db import (
    VALID_BUDGET_PERIODS,
    ApiKeyRecord,
    validate_metadata_size,
)
from ...middleware.auth import ROLE_MASTER
from ...services.accounting import AccountingError
from ...utils.html_cache import get_template

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices

logger = logging.getLogger(__name__)

admin_api_keys_router = APIRouter()

_KEY_MUTATION_LOCKS: dict[int, asyncio.Lock] = {}


def _key_mutation_lock(key_id: int) -> asyncio.Lock:
    lock = _KEY_MUTATION_LOCKS.get(key_id)
    if lock is None:
        lock = asyncio.Lock()
        _KEY_MUTATION_LOCKS[key_id] = lock
    return lock


class ApiKeyCreatePayload(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)
    budget_usd: float | None = None
    rpm: int | None = None
    tpm: int | None = None
    allowed_models: list[str] | None = None
    metadata: dict[str, Any] | None = None
    budget_period: str | None = None


class ApiKeyUpdatePayload(BaseModel):
    name: str | None = None
    budget_usd: float | None = None
    rpm: int | None = None
    tpm: int | None = None
    allowed_models: list[str] | None = None
    disabled: bool | None = None
    metadata: dict[str, Any] | None = None
    budget_period: str | None = None
    reset_spent: bool = False


def _validate_budget_usd(budget_usd: float | None) -> None:
    if budget_usd is None:
        return
    if not math.isfinite(budget_usd) or budget_usd < 0:
        raise ValueError("budget_usd must be a finite number greater than or equal to 0")


def _validate_payload_constraints(payload: ApiKeyCreatePayload | ApiKeyUpdatePayload) -> None:
    _validate_budget_usd(payload.budget_usd)
    validate_metadata_size(payload.metadata)


def _validate_update_payload_constraints(payload: ApiKeyUpdatePayload) -> None:
    _validate_payload_constraints(payload)
    if (
        "name" in payload.model_fields_set
        and payload.name is not None
        and not payload.name.strip()
    ):
        raise ValueError("API key name must not be empty")
    if "budget_period" in payload.model_fields_set:
        normalized_period = (
            "" if payload.budget_period is None else payload.budget_period.strip().lower()
        )
        if normalized_period and normalized_period not in VALID_BUDGET_PERIODS:
            raise ValueError(
                "budget_period must be one of: " + ", ".join(VALID_BUDGET_PERIODS)
            )


def _require_master(request: Request) -> None:
    role = getattr(request.state, "api_key_role", None)
    if role != ROLE_MASTER:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the master API key can manage virtual keys",
        )


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


def _mask_api_key(api_key: str) -> str:
    """Mask a secret the same way ``static/api-keys.js``'s ``maskSecret`` does,
    so re-masking an already-masked value (defense in depth) is a no-op."""
    if len(api_key) <= 8:
        return "••••"
    return f"{api_key[:4]}…{api_key[-4:]}"


def _serialize_record(record: ApiKeyRecord, *, reveal: bool = False) -> dict[str, Any]:
    return {
        "id": record.id,
        "name": record.name,
        "api_key": record.api_key if reveal else _mask_api_key(record.api_key),
        "budget_usd": record.budget_usd,
        "spent_usd": record.spent_usd,
        "rpm": record.rpm,
        "tpm": record.tpm,
        "allowed_models": list(record.allowed_models),
        "disabled": record.disabled,
        "metadata": dict(record.metadata),
        "created_at": record.created_at,
        "last_used_at": record.last_used_at,
        "budget_period": record.budget_period,
        "budget_reset_at": record.budget_reset_at,
    }


def _record_etag(record: ApiKeyRecord) -> str:
    """Deterministic ``ETag`` covering the admin-editable fields of a key.

    Deliberately excludes usage-driven fields (``spent_usd``, ``last_used_at``,
    ``budget_reset_at``): those change on every gateway request or scheduled
    reset, independent of admin edits, so hashing them would make the
    ``If-Match`` precondition fail on virtually every save of an active key.
    Only fields an admin PATCH can change are covered, which is exactly what
    the optimistic-concurrency check in :func:`update_api_key` needs.
    """
    fields = {
        "name": record.name,
        "budget_usd": record.budget_usd,
        "budget_period": record.budget_period,
        "rpm": record.rpm,
        "tpm": record.tpm,
        "allowed_models": sorted(record.allowed_models),
        "disabled": record.disabled,
        "metadata": record.metadata,
    }
    digest = hashlib.sha256(
        json.dumps(fields, sort_keys=True, ensure_ascii=True).encode("utf-8")
    ).hexdigest()
    return f'"{digest}"'


async def _current_record_for_etag(request: Request, key_id: int) -> ApiKeyRecord | None:
    """Read-only lookup so ``update_api_key`` can evaluate an ``If-Match``
    precondition before delegating the actual mutation to
    ``accounting_service`` (mutation handlers must not own storage directly;
    see ``test_admin_mutation_handlers_have_no_direct_storage_or_cache_owner``)."""
    db = _capture_app_services(request).api_keys_db
    return await asyncio.to_thread(db.get_by_id, key_id)


@admin_api_keys_router.get("/ui/api-keys", response_class=HTMLResponse, include_in_schema=False)
async def get_api_keys_page(request: Request):
    _require_master(request)
    html_path = STATIC_DIR / "api-keys.html"
    if not html_path.exists():
        raise HTTPException(status_code=404, detail="API keys UI page not found.")
    return HTMLResponse(content=await get_template(html_path))


@admin_api_keys_router.get("/admin/api-keys")
async def list_api_keys(request: Request) -> dict[str, Any]:
    _require_master(request)
    db = _capture_app_services(request).api_keys_db
    records = await asyncio.to_thread(db.list_all)
    return {"keys": [_serialize_record(record) for record in records]}


@admin_api_keys_router.post("/admin/api-keys", status_code=status.HTTP_201_CREATED)
async def create_api_key(request: Request, payload: ApiKeyCreatePayload) -> dict[str, Any]:
    _require_master(request)
    db = _capture_app_services(request).api_keys_db
    try:
        _validate_payload_constraints(payload)
        record = await asyncio.to_thread(
            db.create,
            name=payload.name,
            budget_usd=payload.budget_usd,
            rpm=payload.rpm,
            tpm=payload.tpm,
            allowed_models=payload.allowed_models,
            metadata=payload.metadata,
            budget_period=payload.budget_period,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    logger.info("Created virtual API key id=%s name=%s", record.id, record.name)
    return _serialize_record(record, reveal=True)


@admin_api_keys_router.get("/admin/api-keys/{key_id}")
async def get_api_key(request: Request, key_id: int, response: Response) -> dict[str, Any]:
    _require_master(request)
    db = _capture_app_services(request).api_keys_db
    record = await asyncio.to_thread(db.get_by_id, key_id)
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    response.headers["ETag"] = _record_etag(record)
    return _serialize_record(record, reveal=True)


@admin_api_keys_router.patch("/admin/api-keys/{key_id}")
async def update_api_key(
    request: Request, key_id: int, payload: ApiKeyUpdatePayload
) -> Any:
    _require_master(request)
    services = _capture_app_services(request)
    # If-Match is optional and backwards-compatible: a caller that never sent
    # it (e.g. an older client, or a script) gets the pre-existing behavior
    # with no precondition check. Callers that do send it get 412 instead of
    # silently clobbering a concurrent edit from another tab/admin.
    #
    # The etag re-check and the mutation are serialised on a per-key
    # ``asyncio.Lock`` so two concurrent PATCH requests that both fetched the
    # same fresh etag can't both pass the precondition and both write — the
    # second one now re-reads the current record inside the lock and observes
    # the first write's fresh etag, so it correctly returns 412 instead of
    # silently clobbering the earlier admin's change.
    if_match = request.headers.get("if-match")
    async with _key_mutation_lock(key_id):
        if if_match is not None:
            current = await _current_record_for_etag(request, key_id)
            if current is None:
                raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
            if _record_etag(current) != if_match.strip():
                return error_response(
                    request,
                    status_code=status.HTTP_412_PRECONDITION_FAILED,
                    detail="API key was modified by another request; reload and retry",
                    code="api_key_conflict",
                    extra={"current": _serialize_record(current)},
                )
        return await _apply_update(request, services, key_id, payload)


async def _apply_update(
    request: Request,
    services: "AppServices",
    key_id: int,
    payload: "ApiKeyUpdatePayload",
) -> Any:
    try:
        _validate_update_payload_constraints(payload)
        update_fields = {
            field_name: getattr(payload, field_name)
            for field_name in (
                "name",
                "budget_usd",
                "rpm",
                "tpm",
                "allowed_models",
                "disabled",
                "metadata",
                "budget_period",
            )
            if field_name in payload.model_fields_set
        }
        record = await services.accounting_service.update_key(
            key_id,
            changes=update_fields,
            reset_spent=payload.reset_spent,
        )
    except ValueError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except AccountingError as exc:
        return accounting_error_response(
            request,
            exc,
            use=AccountingHttpUse.ADMIN_MUTATION,
        )
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    return _serialize_record(record)


@admin_api_keys_router.delete("/admin/api-keys/{key_id}")
async def delete_api_key(request: Request, key_id: int) -> Any:
    _require_master(request)
    try:
        deleted = await _capture_app_services(request).accounting_service.delete_key(
            key_id
        )
    except AccountingError as exc:
        return accounting_error_response(
            request,
            exc,
            use=AccountingHttpUse.ADMIN_MUTATION,
        )
    if not deleted:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="API key not found")
    # A per-key mutation lock is no longer reachable via any PATCH once the key
    # is deleted, so drop it to keep _KEY_MUTATION_LOCKS bounded by live keys
    # rather than by every key_id ever PATCHed over the process lifetime.
    _KEY_MUTATION_LOCKS.pop(key_id, None)
    return {"deleted": True, "id": key_id}
