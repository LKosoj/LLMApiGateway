"""Admin endpoint for upstream subscription quota information."""
from __future__ import annotations

from dataclasses import asdict

from fastapi import APIRouter, HTTPException, Request

from llm_gateway_core.services.upstream_subscription_quota import (
    UpstreamSubscriptionQuotaService,
)

router = APIRouter()


@router.get("/admin/upstream-quotas")
async def get_upstream_quotas(request: Request) -> dict:
    service: UpstreamSubscriptionQuotaService | None = getattr(
        request.app.state, "upstream_subscription_quota_service", None
    )
    if service is None:
        raise HTTPException(status_code=500, detail="UpstreamSubscriptionQuotaService not available.")

    config_loader = getattr(request.app.state, "config_loader", None)
    if config_loader is None:
        raise HTTPException(status_code=500, detail="ConfigLoader not available.")

    providers = config_loader.providers_config
    snapshots = await service.fetch_all(providers_config=providers)
    return {"snapshots": [asdict(s) for s in snapshots]}
