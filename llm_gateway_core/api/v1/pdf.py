import hashlib
import json
import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import TYPE_CHECKING, cast
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...config.settings import settings
from ...config.loader import ConfigLoader
from ...services.accounting import AccountingError, AccountingValidationError
from ...services.access_control import enforce_virtual_key_access
from ...services.operation_accounting import (
    OperationEventProvenance,
    OperationTerminalObservation,
    parse_upstream_operation_observation,
)
from ...services.payload_transform import apply_payload_transforms
from ...services.provider_auth import resolve_provider_auth_headers
from ...services.request_handler import OperationDispatcher, normalize_retry_settings
from ..accounting_http import AccountingHttpUse, accounting_error_response
from .operation_accounting import (
    OperationTerminalOwner,
    finalize_buffered_operation,
    release_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from .operation_proxy import (
    extract_downstream_error_detail,
    proxy_multipart_to_downstream,
    request_duration_ms,
    sanitize_target_url_for_log,
    should_retry_operation_status,
    sleep_before_retry,
)
from .operation_runtime import (
    ValidatedUpload,
    admit_upload_processing,
    validate_upload_limited,
)

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices, RuntimeSnapshot

logger = logging.getLogger(__name__)

router = APIRouter()
PDF_CONVERSIONS_SECTION = "pdf_conversions"
PDF_CONVERSION_OPERATION = "pdf_conversion"
PROTECTED_PDF_CUSTOM_PARAM_KEYS = frozenset({"file", "model"})
PDF_JOB_SUCCESS_STATUSES = frozenset({"completed", "complete", "succeeded", "success", "done"})
PDF_JOB_CANONICAL_METHOD = "POST"
PDF_JOB_CANONICAL_ROUTE = "/v1/pdf/jobs"
MULTIPART_MAX_FIELDS = 64
MULTIPART_MAX_SCALAR_PART_BYTES = 64 * 1024


def _get_operation_runtime(
    request: Request,
) -> tuple[OperationDispatcher, httpx.AsyncClient, ConfigLoader, Mapping[str, httpx.AsyncClient]]:
    services = cast("AppServices", request.app.state.services)
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    return (
        runtime_snapshot.operation_dispatcher,
        services.http_client,
        runtime_snapshot.config_loader,
        runtime_snapshot.proxy_http_clients,
    )


def _append_scalar_form_value(payload: dict[str, object], field_name: str, field_value: object) -> None:
    if field_name not in payload:
        payload[field_name] = field_value
        return

    existing_value = payload[field_name]
    if isinstance(existing_value, list):
        existing_value.append(field_value)
        return

    payload[field_name] = [existing_value, field_value]


async def _serialize_upload(
    value: UploadFile | StarletteUploadFile,
) -> ValidatedUpload:
    return await validate_upload_limited(
        value,
        default_filename="document.pdf",
        kind="pdf",
        max_bytes=settings.pdf_upload_max_bytes,
    )


@asynccontextmanager
async def _parse_pdf_multipart_request(
    request: Request,
) -> AsyncIterator[tuple[str, dict[str, object], ValidatedUpload]]:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="PDF conversion endpoint expects multipart/form-data.")

    scalar_fields: dict[str, object] = {}
    upload_file: ValidatedUpload | None = None

    async with request.form(
        max_files=1,
        max_fields=MULTIPART_MAX_FIELDS,
        max_part_size=MULTIPART_MAX_SCALAR_PART_BYTES,
    ) as form:
        for key, value in form.multi_items():
            if isinstance(value, (UploadFile, StarletteUploadFile)):
                if key != "file":
                    raise HTTPException(status_code=400, detail=f"Unsupported multipart file field '{key}'.")
                if upload_file is not None:
                    raise HTTPException(status_code=400, detail="Only one 'file' is supported in request body.")
                upload_file = await _serialize_upload(value)
                continue

            _append_scalar_form_value(scalar_fields, key, value.strip() if isinstance(value, str) else value)

        requested_model = scalar_fields.pop("model", None)
        if not isinstance(requested_model, str) or not requested_model.strip():
            raise HTTPException(status_code=400, detail="Missing 'model' in request body")

        if upload_file is None:
            raise HTTPException(status_code=400, detail="Missing 'file' in request body")

        yield requested_model.strip(), scalar_fields, upload_file


def _build_route_payload(request_payload: dict[str, object], route) -> dict[str, object]:
    downstream_payload = dict(request_payload)
    for param_name, param_value in route.custom_body_params.items():
        if param_name.lower() in PROTECTED_PDF_CUSTOM_PARAM_KEYS:
            continue
        downstream_payload[param_name] = param_value
    return apply_payload_transforms(downstream_payload, route.payload_transforms)


def _prepare_pdf_route(
    request: Request,
    dispatcher: OperationDispatcher,
    route,
    *,
    gateway_model: str,
) -> tuple[int, float]:
    retry_settings = normalize_retry_settings(route.retry_count, route.retry_delay)
    request.state.llmgateway_gateway_model = gateway_model
    dispatcher.set_request_state(
        request=request,
        operation=PDF_CONVERSION_OPERATION,
        route=route,
        provider_name=route.provider,
        provider_model=route.model,
    )
    return retry_settings


async def _prepare_pdf_http_request(
    request: Request,
    dispatcher: OperationDispatcher,
    route,
    provider_config,
) -> tuple[str, dict[str, str]]:
    base_url = dispatcher.build_target_url(route, provider_config).rstrip("/")
    auth_headers = await resolve_provider_auth_headers(
        request,
        provider_name=route.provider,
        provider_config=provider_config,
    )
    headers = dispatcher.build_headers(route, auth_headers=auth_headers)
    multipart_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    return base_url, multipart_headers


def _join_downstream_path(base_url: str, *parts: str) -> str:
    normalized_parts = [quote(str(part).strip("/"), safe="") for part in parts]
    return "/".join([base_url.rstrip("/"), *normalized_parts])


async def _proxy_get_raw_to_downstream(
    target_url: str,
    headers: dict,
    http_client: httpx.AsyncClient,
    retry_count: int = 0,
    retry_delay: float = 0.0,
) -> tuple[bytes, int, str | None, dict[str, str]]:
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Proxying raw PDF operation GET request to downstream %s", sanitized_target_url)

    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1

        try:
            response = await http_client.get(target_url, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Raw PDF operation downstream GET request to %s failed with network error: %s",
                sanitized_target_url,
                exc,
                exc_info=True,
            )
            if attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"network error {type(exc).__name__}",
                )
                continue
            raise HTTPException(status_code=503, detail="Downstream request failed.") from exc

        logger.info(
            "Raw PDF operation downstream GET response status %s from %s",
            response.status_code,
            sanitized_target_url,
        )
        if response.status_code >= 400:
            detail = extract_downstream_error_detail(response)
            if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"retryable downstream status {response.status_code}",
                )
                continue
            raise HTTPException(status_code=503, detail=detail)

        passthrough_headers: dict[str, str] = {}
        content_disposition = response.headers.get("content-disposition")
        if content_disposition:
            passthrough_headers["content-disposition"] = content_disposition
        return response.content, response.status_code, response.headers.get("content-type"), passthrough_headers

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    normalized_content_type = content_type.lower()
    return "/json" in normalized_content_type or "+json" in normalized_content_type


def _parse_json_response_body(response_body: bytes) -> object:
    try:
        return json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError) as exc:
        raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc


def _response_from_raw_body(
    response_body: bytes,
    status_code: int,
    content_type: str | None,
    headers: dict[str, str] | None = None,
):
    if _is_json_content_type(content_type):
        parsed_response = _parse_json_response_body(response_body)
        return JSONResponse(content=parsed_response, status_code=status_code)

    response_headers = dict(headers or {})
    if content_type:
        response_headers["content-type"] = content_type
    return Response(content=response_body, status_code=status_code, headers=response_headers)


async def _resolve_pdf_route(request: Request, requested_model: str):
    enforce_virtual_key_access(request, requested_model)
    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    route = dispatcher.lookup_route(PDF_CONVERSIONS_SECTION, requested_model)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"No PDF conversion route configured for model '{requested_model}'.",
        )

    provider_config = config_loader_instance.providers_config.get(route.provider)
    if provider_config is None:
        logger.error(
            "Provider '%s' for PDF conversion route '%s' is missing from providers_config.",
            route.provider,
            requested_model,
        )
        raise HTTPException(status_code=500, detail="Internal server error: Provider configuration not available.")

    retry_count, retry_delay = _prepare_pdf_route(request, dispatcher, route, gateway_model=requested_model)
    base_url, headers = await _prepare_pdf_http_request(request, dispatcher, route, provider_config)
    effective_client = proxy_http_clients.get(route.provider, http_client)
    return route, base_url, headers, effective_client, retry_count, retry_delay


def _pdf_job_succeeded(payload: object) -> bool:
    if not isinstance(payload, Mapping):
        return False
    status = payload.get("status")
    return isinstance(status, str) and status.strip().lower() in PDF_JOB_SUCCESS_STATUSES


def _strict_pdf_job_id(value: object) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise AccountingValidationError
    return value


def _terminal_pdf_job_id(payload: object, *, expected_job_id: str | None) -> str:
    if not isinstance(payload, Mapping):
        raise AccountingValidationError
    if expected_job_id is None:
        return _strict_pdf_job_id(payload.get("id"))

    normalized_expected = _strict_pdf_job_id(expected_job_id)
    if "id" in payload and _strict_pdf_job_id(payload["id"]) != normalized_expected:
        raise AccountingValidationError
    return normalized_expected


def _pdf_job_event_id(owner: OperationTerminalOwner, job_id: str) -> str:
    api_key_id = owner.context.reservation.api_key_id
    identity = api_key_id if api_key_id is not None else "master"
    canonical_identity = json.dumps(
        [identity, _strict_pdf_job_id(job_id)],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    return f"pdf-job-{hashlib.sha256(canonical_identity).hexdigest()}"


def _parse_pdf_terminal_observation(
    owner: OperationTerminalOwner,
    payload: object,
    *,
    gateway_model: str,
    duration_ms: int,
) -> OperationTerminalObservation:
    if not isinstance(payload, Mapping):
        raise AccountingValidationError
    return parse_upstream_operation_observation(
        payload,
        policy=owner.context.policy,
        gateway_model=gateway_model,
        provider=None,
        model=None,
        cost_rate_registry=owner.cost_rate_registry,
        operation_cost_calculator_registry=(
            owner.operation_cost_calculator_registry
        ),
        occurred_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )


def _pdf_job_provenance(
    owner: OperationTerminalOwner,
    route,
    *,
    job_id: str,
) -> OperationEventProvenance:
    return OperationEventProvenance(
        event_id=_pdf_job_event_id(owner, job_id),
        method=PDF_JOB_CANONICAL_METHOD,
        route_template=PDF_JOB_CANONICAL_ROUTE,
        provider=route.provider,
        model=route.model,
    )


async def _finalize_pdf_job_response(
    owner: OperationTerminalOwner,
    response: Response,
    payload: object,
    *,
    route,
    gateway_model: str,
    expected_job_id: str | None,
    duration_ms: int,
) -> Response:
    if not _pdf_job_succeeded(payload):
        await release_operation(owner)
        return response

    job_id = _terminal_pdf_job_id(payload, expected_job_id=expected_job_id)
    observation = _parse_pdf_terminal_observation(
        owner,
        payload,
        gateway_model=gateway_model,
        duration_ms=duration_ms,
    )
    return await finalize_buffered_operation(
        owner,
        response,
        observation,
        provenance=_pdf_job_provenance(owner, route, job_id=job_id),
    )


async def _run_pdf_accounting(
    request: Request,
    operation: Callable[[OperationTerminalOwner], Awaitable[object]],
) -> object:
    try:
        owner = take_operation_terminal_owner(request)
    except AccountingError as exc:
        return accounting_error_response(
            request,
            exc,
            use=AccountingHttpUse.ADMISSION,
        )

    try:
        return await operation(owner)
    except AccountingError as exc:
        await release_operation_if_open(owner, primary_error=exc)
        return accounting_error_response(
            request,
            exc,
            use=AccountingHttpUse.ADMISSION,
        )
    except BaseException as exc:
        await release_operation_if_open(owner, primary_error=exc)
        raise


@router.post("/pdf/convert")
async def convert_pdf(request: Request):
    async with _parse_pdf_multipart_request(request) as (
        requested_model,
        request_payload,
        upload,
    ):

        async def operation(owner: OperationTerminalOwner) -> object:
            request_started_at = time.monotonic()
            route, base_url, headers, effective_client, retry_count, retry_delay = await _resolve_pdf_route(
                request,
                requested_model,
            )
            async with admit_upload_processing(request, upload.size):
                response_payload, downstream_status_code = await proxy_multipart_to_downstream(
                    _join_downstream_path(base_url, "convert"),
                    headers,
                    _build_route_payload(request_payload, route),
                    [("file", upload.as_multipart_file())],
                    effective_client,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                )
            response = JSONResponse(
                content=response_payload,
                status_code=downstream_status_code,
            )
            observation = _parse_pdf_terminal_observation(
                owner,
                response_payload,
                gateway_model=requested_model,
                duration_ms=request_duration_ms(request_started_at),
            )
            return await finalize_buffered_operation(owner, response, observation)

        return await _run_pdf_accounting(request, operation)


@router.post("/pdf/jobs")
async def create_pdf_job(request: Request):
    async with _parse_pdf_multipart_request(request) as (
        requested_model,
        request_payload,
        upload,
    ):

        async def operation(owner: OperationTerminalOwner) -> object:
            request_started_at = time.monotonic()
            route, base_url, headers, effective_client, retry_count, retry_delay = await _resolve_pdf_route(
                request,
                requested_model,
            )
            async with admit_upload_processing(request, upload.size):
                response_payload, downstream_status_code = await proxy_multipart_to_downstream(
                    _join_downstream_path(base_url, "jobs"),
                    headers,
                    _build_route_payload(request_payload, route),
                    [("file", upload.as_multipart_file())],
                    effective_client,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                )
            response = JSONResponse(
                content=response_payload,
                status_code=downstream_status_code,
            )
            return await _finalize_pdf_job_response(
                owner,
                response,
                response_payload,
                route=route,
                gateway_model=requested_model,
                expected_job_id=None,
                duration_ms=request_duration_ms(request_started_at),
            )

        return await _run_pdf_accounting(request, operation)


@router.get("/pdf/jobs/{job_id}")
async def get_pdf_job(job_id: str, model: str, request: Request):
    async def operation(owner: OperationTerminalOwner) -> object:
        request_started_at = time.monotonic()
        route, base_url, headers, effective_client, retry_count, retry_delay = await _resolve_pdf_route(
            request,
            model,
        )
        response_body, status_code, content_type, response_headers = await _proxy_get_raw_to_downstream(
            _join_downstream_path(base_url, "jobs", job_id),
            headers,
            effective_client,
            retry_count=retry_count,
            retry_delay=retry_delay,
        )
        if _is_json_content_type(content_type):
            parsed_response = _parse_json_response_body(response_body)
            response = JSONResponse(content=parsed_response, status_code=status_code)
            return await _finalize_pdf_job_response(
                owner,
                response,
                parsed_response,
                route=route,
                gateway_model=model,
                expected_job_id=job_id,
                duration_ms=request_duration_ms(request_started_at),
            )

        response = _response_from_raw_body(
            response_body,
            status_code,
            content_type,
            response_headers,
        )
        await release_operation(owner)
        return response

    return await _run_pdf_accounting(request, operation)


@router.get("/pdf/jobs/{job_id}/result")
async def get_pdf_job_result(job_id: str, model: str, request: Request):
    _route, base_url, headers, effective_client, retry_count, retry_delay = await _resolve_pdf_route(request, model)
    response_body, status_code, content_type, response_headers = await _proxy_get_raw_to_downstream(
        _join_downstream_path(base_url, "jobs", job_id, "result"),
        headers,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )
    return _response_from_raw_body(response_body, status_code, content_type, response_headers)


@router.get("/pdf/jobs/{job_id}/download/{artifact:path}")
async def download_pdf_job_artifact(job_id: str, artifact: str, model: str, request: Request):
    _route, base_url, headers, effective_client, retry_count, retry_delay = await _resolve_pdf_route(request, model)
    response_body, status_code, content_type, response_headers = await _proxy_get_raw_to_downstream(
        _join_downstream_path(base_url, "jobs", job_id, "download", artifact),
        headers,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )
    return _response_from_raw_body(response_body, status_code, content_type, response_headers)
