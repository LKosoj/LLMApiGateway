"""Admin endpoint for upstream subscription quota information."""
from __future__ import annotations

from dataclasses import asdict
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, Request

if TYPE_CHECKING:
    from llm_gateway_core.services.runtime_config import AppServices, RuntimeSnapshot

router = APIRouter()


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


def _capture_runtime_snapshot(request: Request) -> "RuntimeSnapshot":
    return cast("RuntimeSnapshot", request.state.runtime_snapshot)


@router.get("/admin/upstream-quotas")
async def get_upstream_quotas(request: Request) -> dict:
    service = _capture_app_services(request).upstream_subscription_quota_service
    providers = _capture_runtime_snapshot(request).config_loader.providers_config
    snapshots = await service.fetch_all(providers_config=providers)
    return {"snapshots": [asdict(s) for s in snapshots]}
