"""RouterAI-compatible asynchronous video creation, polling and download."""

import logging
import time
from datetime import datetime
from typing import cast
from urllib.parse import quote
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.background import BackgroundTask

from ...config.schemas import OperationRoute
from ...db.video_jobs_db import VideoJobsDB
from ...services.access_control import enforce_virtual_key_access
from ...services.accounting import AccountingError
from ...services.operation_accounting import OperationEventProvenance, parse_upstream_operation_observation
from ...services.provider_auth import resolve_provider_auth_headers
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ..accounting_http import AccountingHttpUse, accounting_error_response
from .operation_accounting import (
    finalize_buffered_operation,
    release_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from .operation_fallback_log import clear_route_key_fingerprint, record_operation_route_attempt
from .video_adapters import VideoRequest, build_video_payload, video_cost_usd

logger = logging.getLogger(__name__)
router = APIRouter()
SECTION = "videos_generations"
OPERATION = "video_generation"
# Other responses can describe uncertain acceptance, especially 409/429/5xx.
CREATE_REFUSAL_STATUSES = frozenset({400, 401, 402, 403, 404, 413, 415, 422})


def _owner(request: Request) -> str:
    key_id = getattr(request.state, "api_key_id", None)
    return "master" if key_id is None else f"key:{key_id}"


def _store(request: Request) -> VideoJobsDB:
    return cast(AppServices, request.app.state.services).video_jobs_db


async def _connection(request: Request, route: OperationRoute, gateway_model: str):
    services = cast(AppServices, request.app.state.services)
    snapshot = cast(RuntimeSnapshot, request.state.runtime_snapshot)
    provider = snapshot.config_loader.providers_config.get(route.provider)
    if provider is None:
        raise HTTPException(503, "Video provider configuration is unavailable")
    request.state.llmgateway_gateway_model = gateway_model
    snapshot.operation_dispatcher.set_request_state(
        request=request, operation=OPERATION, route=route,
        provider_name=route.provider, provider_model=route.model,
    )
    clear_route_key_fingerprint(request)
    auth_headers = await resolve_provider_auth_headers(
        request, provider_name=route.provider, provider_config=provider,
    )
    headers = snapshot.operation_dispatcher.build_headers(route, auth_headers=auth_headers)
    url = snapshot.operation_dispatcher.build_target_url(route, provider).rstrip("/")
    client = snapshot.proxy_http_clients.get(route.provider, services.http_client)
    return url, headers, client


def _task_payload(response: httpx.Response) -> dict:
    try:
        payload = response.json()
    except ValueError as exc:
        raise HTTPException(502, "Video provider returned invalid JSON; task creation may have succeeded") from exc
    if not isinstance(payload, dict):
        raise HTTPException(502, "Video provider returned an invalid task")
    upstream_id = payload.get("id")
    if not isinstance(upstream_id, str) or not upstream_id.strip():
        raise HTTPException(502, "Video provider returned no task ID; do not automatically create another task")
    return payload


def _public_task(payload: dict, job: dict) -> dict:
    result = dict(payload)
    # Ссылки провайдера открываются только его ключом; клиент качает через content_url.
    result.pop("unsigned_urls", None)
    result.update(
        id=job["id"], model=job["gateway_model"],
        polling_url=f"/v1/videos/{job['id']}",
        content_url=f"/v1/videos/{job['id']}/content",
    )
    return result


async def _load_job(request: Request, job_id: str) -> dict:
    job = await _store(request).get(job_id, _owner(request))
    if job is None:
        raise HTTPException(404, "Video task not found")
    enforce_virtual_key_access(request, job["gateway_model"])
    return job


async def _account_task(request: Request, owner, response: JSONResponse, payload: dict, job: dict):
    if payload.get("status") != "completed":
        await release_operation(owner)
        return response
    try:
        cost = video_cost_usd(payload)
    except (ValueError, TypeError, OverflowError) as exc:
        raise HTTPException(502, "Video provider returned an invalid cost") from exc
    billing = job["billing_payload"]
    if billing is None and cost is not None:
        billing = await _store(request).freeze_billing(job["id"], {"usage": {"cost": cost}})
    if billing is None:
        # Missing settlement is unknown, not a free or flat-priced generation.
        await release_operation(owner)
        return response
    observation = parse_upstream_operation_observation(
        billing, policy=owner.context.policy, gateway_model=job["gateway_model"],
        provider=None, model=None, cost_rate_registry=owner.cost_rate_registry,
        operation_cost_calculator_registry=owner.operation_cost_calculator_registry,
        occurred_at=datetime.fromisoformat(job["created_at"]),
    )
    route = job["route"]
    return await finalize_buffered_operation(
        owner, response, observation,
        provenance=OperationEventProvenance(
            event_id=job["id"], method="POST", route_template="/v1/videos",
            provider=route["provider"], model=route["model"],
        ),
    )


async def _with_accounting(request: Request, operation):
    owner = None
    try:
        owner = take_operation_terminal_owner(request)
        return await operation(owner)
    except AccountingError as exc:
        if owner is not None:
            await release_operation_if_open(owner, primary_error=exc)
        return accounting_error_response(request, exc, use=AccountingHttpUse.ADMISSION)
    except BaseException as exc:
        if owner is not None:
            await release_operation_if_open(owner, primary_error=exc)
        raise


@router.post("/videos")
async def create_video(body: VideoRequest, request: Request):
    async def operation(owner):
        enforce_virtual_key_access(request, body.model)
        store = _store(request)
        snapshot = cast(RuntimeSnapshot, request.state.runtime_snapshot)
        routes = snapshot.operation_dispatcher.lookup_routes(SECTION, body.model)
        if not routes:
            raise HTTPException(404, f"No video route configured for model '{body.model}'")
        for index, route in enumerate(routes):
            payload = build_video_payload(body, route.model, route.request_format)
            url, headers, client = await _connection(request, route, body.model)
            if route.request_format == "bettertoken_videos":
                headers["Idempotency-Key"] = request.headers.get("Idempotency-Key") or str(uuid4())
            started_at = time.perf_counter()
            try:
                upstream = await client.post(url, headers=headers, json=payload)
            except httpx.RequestError as exc:
                record_operation_route_attempt(
                    request, operation=OPERATION, gateway_model=body.model, provider=route.provider,
                    model=route.model, success=False, error=exc, started_at=started_at,
                )
                raise HTTPException(502, "Video task acceptance is unknown; no fallback was attempted") from exc
            if upstream.is_error:
                error = HTTPException(upstream.status_code, "Video provider could not create the task")
                record_operation_route_attempt(
                    request, operation=OPERATION, gateway_model=body.model, provider=route.provider,
                    model=route.model, success=False, error=error, started_at=started_at,
                )
                try:
                    error_payload = upstream.json()
                except ValueError:
                    error_payload = None
                has_task = isinstance(error_payload, dict) and bool(error_payload.get("id"))
                if upstream.status_code in CREATE_REFUSAL_STATUSES and not has_task and index + 1 < len(routes):
                    logger.warning("Video creation refused by %s (%s); trying next provider", route.provider, upstream.status_code)
                    continue
                raise error
            payload = _task_payload(upstream)
            # Capture the exact endpoint, but resolve credentials from the current provider config.
            stored_route = {
                "provider": route.provider, "model": route.model, "target_path": url,
                "request_format": route.request_format,
                "custom_headers": route.custom_headers,
            }
            job = await store.create(_owner(request), body.model, stored_route, payload["id"])
            record_operation_route_attempt(
                request, operation=OPERATION, gateway_model=body.model, provider=route.provider,
                model=route.model, success=True, started_at=started_at,
            )
            response = JSONResponse(_public_task(payload, job), status_code=upstream.status_code)
            return await _account_task(request, owner, response, payload, job)
        raise HTTPException(502, "All video providers refused task creation")

    return await _with_accounting(request, operation)


@router.get("/videos/{job_id}")
async def get_video(job_id: str, request: Request):
    async def operation(owner):
        job = await _load_job(request, job_id)
        route = OperationRoute(**job["route"])
        url, headers, client = await _connection(request, route, job["gateway_model"])
        try:
            upstream = await client.get(f"{url}/{quote(job['upstream_id'], safe='')}", headers=headers)
        except httpx.RequestError as exc:
            raise HTTPException(502, "Video provider status request failed") from exc
        if upstream.is_error:
            raise HTTPException(upstream.status_code, "Video provider status request failed")
        payload = _task_payload(upstream)
        if payload["id"] != job["upstream_id"]:
            raise HTTPException(502, "Video provider returned a different task ID")
        return await _account_task(request, owner, JSONResponse(_public_task(payload, job)), payload, job)

    return await _with_accounting(request, operation)


@router.get("/videos/{job_id}/content")
async def get_video_content(job_id: str, request: Request):
    job = await _load_job(request, job_id)
    route = OperationRoute(**job["route"])
    url, headers, client = await _connection(request, route, job["gateway_model"])
    if "range" in request.headers:
        headers["Range"] = request.headers["range"]
    target = f"{url}/{quote(job['upstream_id'], safe='')}/content"
    try:
        upstream = await client.send(client.build_request("GET", target, headers=headers), stream=True)
    except httpx.RequestError as exc:
        raise HTTPException(502, "Video download failed") from exc
    if upstream.is_error or upstream.is_redirect:
        await upstream.aclose()
        raise HTTPException(upstream.status_code if upstream.is_error else 502, "Video download failed")

    async def content():
        try:
            async for chunk in upstream.aiter_bytes():
                yield chunk
        finally:
            await upstream.aclose()

    response_headers = {
        key: upstream.headers[key]
        for key in ("content-type", "content-disposition", "content-range", "accept-ranges")
        if key in upstream.headers
    }
    return StreamingResponse(
        content(), status_code=upstream.status_code, headers=response_headers,
        background=BackgroundTask(upstream.aclose),
    )
