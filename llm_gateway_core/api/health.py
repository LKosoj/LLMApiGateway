"""Public readiness endpoint."""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from ..build_info import build_headers
from ..services.health import (
    HealthReport,
    failed_health_report,
    starting_health_report,
)
from ..services.runtime_config import AppServices


async def health(request: Request) -> JSONResponse:
    try:
        services = request.app.state.services
    except AttributeError:
        services = None

    if isinstance(services, AppServices):
        try:
            report = await services.health_service.report()
        except Exception:
            report = failed_health_report()
        if not isinstance(report, HealthReport):
            report = failed_health_report()
    else:
        report = starting_health_report()

    status_code = 200 if report.ok else 503
    headers = {**build_headers(), "Cache-Control": "no-store"}
    return JSONResponse(report.as_dict(), status_code=status_code, headers=headers)


async def liveness(request: Request) -> JSONResponse:
    """Report that the process is alive, without any readiness checks.

    Intended for container/orchestrator liveness probes, which must not
    restart the process merely because a dependency is transiently
    degraded (that is what /health readiness is for).
    """
    headers = {**build_headers(), "Cache-Control": "no-store"}
    return JSONResponse({"status": "ok"}, status_code=200, headers=headers)


health_router = APIRouter()
health_router.add_api_route(
    "/health",
    health,
    methods=["GET"],
    tags=["Health"],
    operation_id="health_get",
)
health_router.add_api_route(
    "/health",
    health,
    methods=["HEAD"],
    tags=["Health"],
    operation_id="health_head",
)
health_router.add_api_route(
    "/healthz",
    liveness,
    methods=["GET"],
    tags=["Health"],
    operation_id="healthz_get",
)
health_router.add_api_route(
    "/healthz",
    liveness,
    methods=["HEAD"],
    tags=["Health"],
    operation_id="healthz_head",
)
