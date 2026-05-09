import json
import logging
import time
from urllib.parse import quote

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...config.loader import ConfigLoader, resolve_provider_api_key
from ...services.access_control import enforce_virtual_key_access
from ...services.request_handler import OperationDispatcher, normalize_retry_settings
from .operation_proxy import (
    extract_downstream_error_detail,
    proxy_multipart_to_downstream,
    record_operation_usage,
    request_duration_ms,
    sanitize_target_url_for_log,
    should_retry_operation_status,
    sleep_before_retry,
)

logger = logging.getLogger(__name__)

router = APIRouter()
PDF_CONVERSIONS_SECTION = "pdf_conversions"
PDF_CONVERSION_OPERATION = "pdf_conversion"
PROTECTED_PDF_CUSTOM_PARAM_KEYS = frozenset({"file", "model"})


def _get_operation_runtime(
    request: Request,
) -> tuple[OperationDispatcher, httpx.AsyncClient, ConfigLoader, dict]:
    dispatcher: OperationDispatcher | None = getattr(request.app.state, "operation_dispatcher", None)
    if dispatcher is None:
        logger.error("OperationDispatcher not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Operation dispatcher not available.")

    http_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logger.error("Shared httpx.AsyncClient not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")

    config_loader_instance: ConfigLoader | None = getattr(request.app.state, "config_loader", None)
    if config_loader_instance is None:
        logger.error("ConfigLoader not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")

    proxy_http_clients: dict = getattr(request.app.state, "proxy_http_clients", {})
    return dispatcher, http_client, config_loader_instance, proxy_http_clients


def _append_scalar_form_value(payload: dict[str, object], field_name: str, field_value: object) -> None:
    if field_name not in payload:
        payload[field_name] = field_value
        return

    existing_value = payload[field_name]
    if isinstance(existing_value, list):
        existing_value.append(field_value)
        return

    payload[field_name] = [existing_value, field_value]


async def _serialize_upload(value: UploadFile | StarletteUploadFile) -> tuple[str, bytes, str | None]:
    return value.filename or "document.pdf", await value.read(), value.content_type


async def _parse_pdf_multipart_request(
    request: Request,
) -> tuple[str, dict[str, object], list[tuple[str, tuple[str, bytes, str | None]]]]:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="PDF conversion endpoint expects multipart/form-data.")

    form = await request.form()
    scalar_fields: dict[str, object] = {}
    upload_file: tuple[str, bytes, str | None] | None = None

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

    return requested_model.strip(), scalar_fields, [("file", upload_file)]


def _build_route_payload(request_payload: dict[str, object], route) -> dict[str, object]:
    downstream_payload = dict(request_payload)
    for param_name, param_value in route.custom_body_params.items():
        if param_name.lower() in PROTECTED_PDF_CUSTOM_PARAM_KEYS:
            continue
        downstream_payload[param_name] = param_value
    return downstream_payload


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


def _prepare_pdf_http_request(
    dispatcher: OperationDispatcher,
    route,
    provider_config,
) -> tuple[str, dict[str, str]]:
    provider_api_key = resolve_provider_api_key(provider_config.apikey)
    base_url = dispatcher.build_target_url(route, provider_config).rstrip("/")
    headers = dispatcher.build_headers(route, provider_api_key)
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
            raise HTTPException(status_code=503, detail=f"Downstream request failed: {exc}") from exc

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


def _response_from_raw_body(
    response_body: bytes,
    status_code: int,
    content_type: str | None,
    headers: dict[str, str] | None = None,
):
    if _is_json_content_type(content_type):
        try:
            parsed_response = json.loads(response_body.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc
        return JSONResponse(content=parsed_response, status_code=status_code)

    response_headers = dict(headers or {})
    if content_type:
        response_headers["content-type"] = content_type
    return Response(content=response_body, status_code=status_code, headers=response_headers)


def _resolve_pdf_route(request: Request, requested_model: str):
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
    base_url, headers = _prepare_pdf_http_request(dispatcher, route, provider_config)
    effective_client = proxy_http_clients.get(route.provider, http_client)
    return route, base_url, headers, effective_client, retry_count, retry_delay


@router.post("/pdf/convert")
async def convert_pdf(request: Request):
    requested_model, request_payload, files_payload = await _parse_pdf_multipart_request(request)
    request_started_at = time.monotonic()
    route, base_url, headers, effective_client, retry_count, retry_delay = _resolve_pdf_route(
        request,
        requested_model,
    )

    response_payload, downstream_status_code = await proxy_multipart_to_downstream(
        _join_downstream_path(base_url, "convert"),
        headers,
        _build_route_payload(request_payload, route),
        files_payload,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )

    await record_operation_usage(
        request,
        response_payload if isinstance(response_payload, dict) else {},
        gateway_model=requested_model,
        operation=PDF_CONVERSION_OPERATION,
        duration_ms=request_duration_ms(request_started_at),
    )
    return JSONResponse(content=response_payload, status_code=downstream_status_code)


@router.post("/pdf/jobs")
async def create_pdf_job(request: Request):
    requested_model, request_payload, files_payload = await _parse_pdf_multipart_request(request)
    request_started_at = time.monotonic()
    route, base_url, headers, effective_client, retry_count, retry_delay = _resolve_pdf_route(
        request,
        requested_model,
    )

    response_payload, downstream_status_code = await proxy_multipart_to_downstream(
        _join_downstream_path(base_url, "jobs"),
        headers,
        _build_route_payload(request_payload, route),
        files_payload,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )

    await record_operation_usage(
        request,
        response_payload if isinstance(response_payload, dict) else {},
        gateway_model=requested_model,
        operation=PDF_CONVERSION_OPERATION,
        duration_ms=request_duration_ms(request_started_at),
    )
    return JSONResponse(content=response_payload, status_code=downstream_status_code)


@router.get("/pdf/jobs/{job_id}")
async def get_pdf_job(job_id: str, model: str, request: Request):
    _route, base_url, headers, effective_client, retry_count, retry_delay = _resolve_pdf_route(request, model)
    response_body, status_code, content_type, response_headers = await _proxy_get_raw_to_downstream(
        _join_downstream_path(base_url, "jobs", job_id),
        headers,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )
    return _response_from_raw_body(response_body, status_code, content_type, response_headers)


@router.get("/pdf/jobs/{job_id}/result")
async def get_pdf_job_result(job_id: str, model: str, request: Request):
    _route, base_url, headers, effective_client, retry_count, retry_delay = _resolve_pdf_route(request, model)
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
    _route, base_url, headers, effective_client, retry_count, retry_delay = _resolve_pdf_route(request, model)
    response_body, status_code, content_type, response_headers = await _proxy_get_raw_to_downstream(
        _join_downstream_path(base_url, "jobs", job_id, "download", artifact),
        headers,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )
    return _response_from_raw_body(response_body, status_code, content_type, response_headers)
