import logging
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager, nullcontext
from datetime import datetime, timezone
from functools import partial
from typing import TYPE_CHECKING, cast

import httpx
from fastapi import APIRouter, HTTPException, Request, UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile

from ..accounting_http import AccountingHttpUse, accounting_error_response
from ...config.loader import ConfigLoader, OperationRoute
from ...config.settings import IMAGE_UPLOAD_MAX_PARTS, settings
from ...services.access_control import enforce_virtual_key_access
from ...services.accounting import AccountingError
from ...services.operation_accounting import (
    OperationTerminalObservation,
    parse_upstream_operation_observation,
)
from ...services.provider_auth import resolve_provider_auth_headers
from ...services.request_handler import OperationDispatcher, normalize_retry_settings
from .image_adapters import (
    ImageDownstreamResponseError,
    ImageRouteConfigError,
    build_downstream_image_request,
    estimate_image_request_processing_weight,
    normalize_downstream_image_response,
)
from .operation_proxy import (
    json_response,
    proxy_json_to_downstream,
    proxy_multipart_to_downstream,
    read_json_request_body,
    request_duration_ms,
)
from .operation_accounting import (
    OperationTerminalOwner,
    finalize_buffered_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from .operation_runtime import (
    ValidatedUpload,
    admit_upload_processing,
    validate_upload_limited,
    validate_upload_total,
)

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices, RuntimeSnapshot

logger = logging.getLogger(__name__)

router = APIRouter()
IMAGE_GENERATIONS_SECTION = "images_generations"
IMAGE_EDITS_SECTION = "images_edits"
IMAGE_GENERATION_OPERATION = "images_generation"
IMAGE_EDIT_OPERATION = "images_edit"
IMAGE_FALLBACK_STATUS_CODES = frozenset({413, 503})
TRUTHY_FORM_VALUES = frozenset({"1", "on", "true", "yes"})
IMAGE_FORM_MAX_FILES = IMAGE_UPLOAD_MAX_PARTS
IMAGE_FORM_MAX_FIELDS = 64
IMAGE_FORM_MAX_PART_SIZE = 64 * 1024
IMAGE_FORM_MAX_IMAGES = 4


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


def _should_try_next_image_route(exc: HTTPException, route_index: int, routes: list[OperationRoute]) -> bool:
    return exc.status_code in IMAGE_FALLBACK_STATUS_CODES and route_index < len(routes) - 1


def _log_image_route_fallback(
    operation_label: str,
    requested_model: str,
    route: OperationRoute,
    route_index: int,
    routes: list[OperationRoute],
    exc: HTTPException,
) -> None:
    logger.warning(
        "%s route failed for gateway model '%s'; falling back to next route. "
        "route=%s/%s provider=%s model=%s detail=%s",
        operation_label,
        requested_model,
        route_index + 1,
        len(routes),
        route.provider,
        route.model,
        exc.detail,
    )


def _require_model(payload: dict) -> str:
    requested_model = payload.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")
    return requested_model


def _ensure_stream_not_requested(payload: dict) -> None:
    if payload.get("stream"):
        raise HTTPException(status_code=400, detail="Image streaming is not supported.")


def _validate_generation_body(payload: dict) -> str:
    requested_model = _require_model(payload)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="Missing 'prompt' in request body")
    _ensure_stream_not_requested(payload)
    return requested_model


def _validate_edit_json_body(payload: dict) -> str:
    requested_model = _require_model(payload)
    prompt = payload.get("prompt")
    if not isinstance(prompt, str) or not prompt.strip():
        raise HTTPException(status_code=400, detail="Missing 'prompt' in request body")

    images = payload.get("images")
    if not isinstance(images, list) or not images:
        raise HTTPException(status_code=400, detail="Missing or invalid 'images' in request body")

    _ensure_stream_not_requested(payload)
    return requested_model


def _parse_form_field_value(value: object) -> object:
    if isinstance(value, str):
        normalized_value = value.strip()
        return normalized_value
    return value


def _is_truthy_form_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_FORM_VALUES
    return False


async def _validate_image_upload(
    value: UploadFile | StarletteUploadFile,
) -> ValidatedUpload:
    return await validate_upload_limited(
        value,
        default_filename="upload.bin",
        kind="image",
        max_bytes=settings.image_upload_max_file_bytes,
    )


@asynccontextmanager
async def _parse_multipart_edit_request(
    request: Request,
) -> AsyncIterator[tuple[str, dict[str, object]]]:
    scalar_fields: dict[str, object] = {}
    images: list[ValidatedUpload] = []
    mask: ValidatedUpload | None = None

    async with request.form(
        max_files=IMAGE_FORM_MAX_FILES,
        max_fields=IMAGE_FORM_MAX_FIELDS,
        max_part_size=IMAGE_FORM_MAX_PART_SIZE,
    ) as form:
        for key, value in form.multi_items():
            if isinstance(value, (UploadFile, StarletteUploadFile)):
                if key not in {"image", "image[]", "mask"}:
                    raise HTTPException(status_code=400, detail=f"Unsupported multipart file field '{key}'.")

                if key in {"image", "image[]"}:
                    if len(images) >= IMAGE_FORM_MAX_IMAGES:
                        raise HTTPException(
                            status_code=400,
                            detail="At most four image files are supported in request body.",
                        )
                    images.append(await _validate_image_upload(value))
                else:
                    if mask is not None:
                        raise HTTPException(status_code=400, detail="Only one 'mask' file is supported in request body.")
                    mask = await _validate_image_upload(value)
                continue

            scalar_fields[key] = _parse_form_field_value(value)

        requested_model = scalar_fields.get("model")
        if not isinstance(requested_model, str) or not requested_model:
            raise HTTPException(status_code=400, detail="Missing 'model' in request body")

        prompt = scalar_fields.get("prompt")
        if not isinstance(prompt, str) or not prompt:
            raise HTTPException(status_code=400, detail="Missing 'prompt' in request body")

        if _is_truthy_form_value(scalar_fields.get("stream")):
            raise HTTPException(status_code=400, detail="Image streaming is not supported.")

        if not images:
            raise HTTPException(status_code=400, detail="Missing image file in request body")

        uploads = [*images]
        if mask is not None:
            uploads.append(mask)
        validate_upload_total(
            uploads,
            max_bytes=settings.image_upload_max_total_bytes,
        )

        normalized_payload = dict(scalar_fields)
        normalized_payload["images"] = images
        if mask is not None:
            normalized_payload["mask"] = mask

        yield requested_model, normalized_payload


async def _prepare_route_request(
    request: Request,
    dispatcher: OperationDispatcher,
    route,
    provider_config,
    *,
    gateway_model: str,
    operation_name: str,
) -> tuple[str, dict, tuple[int, float]]:
    target_url = dispatcher.build_target_url(route, provider_config)
    auth_headers = await resolve_provider_auth_headers(
        request,
        provider_name=route.provider,
        provider_config=provider_config,
    )
    headers = dispatcher.build_headers(route, auth_headers=auth_headers)
    retry_settings = normalize_retry_settings(route.retry_count, route.retry_delay)

    request.state.llmgateway_gateway_model = gateway_model
    dispatcher.set_request_state(
        request=request,
        operation=operation_name,
        route=route,
        provider_name=route.provider,
        provider_model=route.model,
    )
    return target_url, headers, retry_settings


async def _execute_prepared_request(
    prepared_request,
    *,
    target_url: str,
    headers: dict[str, str],
    http_client: httpx.AsyncClient,
    retry_count: int,
    retry_delay: float,
) -> tuple[dict, int]:
    if prepared_request.transport == "multipart":
        multipart_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
        return await proxy_multipart_to_downstream(
            target_url,
            multipart_headers,
            prepared_request.multipart_data or [],
            prepared_request.multipart_files or [],
            http_client,
            retry_count=retry_count,
            retry_delay=retry_delay,
        )

    return await proxy_json_to_downstream(
        target_url,
        headers,
        prepared_request.json_payload or {},
        http_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )


def _parse_image_terminal_observation(
    owner: OperationTerminalOwner,
    payload: Mapping[str, object],
    *,
    gateway_model: str,
    route: OperationRoute,
    duration_ms: int,
) -> OperationTerminalObservation:
    return parse_upstream_operation_observation(
        payload,
        policy=owner.context.policy,
        gateway_model=gateway_model,
        provider=route.provider,
        model=route.model,
        cost_rate_registry=owner.cost_rate_registry,
        operation_cost_calculator_registry=(
            owner.operation_cost_calculator_registry
        ),
        occurred_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )


async def _run_image_accounting(
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


async def _proxy_image_with_fallback(
    request: Request,
    owner: OperationTerminalOwner,
    *,
    dispatcher: OperationDispatcher,
    config_loader_instance: ConfigLoader,
    proxy_http_clients: Mapping[str, httpx.AsyncClient],
    http_client: httpx.AsyncClient,
    routes: list[OperationRoute],
    requested_model: str,
    request_payload: dict[str, object],
    operation_section: str,
    operation_name: str,
    operation_label: str,
    source_transport: str,
) -> object:
    request_started_at = time.monotonic()

    for route_index, route in enumerate(routes):
        try:
            provider_config = config_loader_instance.providers_config.get(route.provider)
            if provider_config is None:
                logger.error(
                    "Provider '%s' for %s route '%s' is missing from providers_config.",
                    route.provider,
                    operation_label.lower(),
                    requested_model,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Provider configuration not available.",
                )

            target_url, headers, (retry_count, retry_delay) = await _prepare_route_request(
                request,
                dispatcher,
                route,
                provider_config,
                gateway_model=requested_model,
                operation_name=operation_name,
            )
        except HTTPException as exc:
            if _should_try_next_image_route(exc, route_index, routes):
                _log_image_route_fallback(
                    operation_label,
                    requested_model,
                    route,
                    route_index,
                    routes,
                    exc,
                )
                continue
            raise

        processing_weight = estimate_image_request_processing_weight(
            request_payload,
            route,
            operation_section,
            source_transport=source_transport,
        )
        route_error: HTTPException | None = None
        admission_context = (
            admit_upload_processing(request, processing_weight)
            if processing_weight is not None
            else nullcontext()
        )

        async with admission_context:
            try:
                prepared_request = build_downstream_image_request(
                    request_payload,
                    route,
                    operation_section,
                    source_transport=source_transport,
                )
            except ImageRouteConfigError as exc:
                logger.error(
                    "Invalid %s route configuration for '%s': %s",
                    operation_label.lower(),
                    requested_model,
                    exc,
                    exc_info=True,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Invalid image route configuration.",
                ) from exc

            effective_client = proxy_http_clients.get(route.provider, http_client)
            try:
                downstream_json, downstream_status_code = await _execute_prepared_request(
                    prepared_request,
                    target_url=target_url,
                    headers=headers,
                    http_client=effective_client,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                )
                try:
                    response_payload = normalize_downstream_image_response(
                        downstream_json,
                        route,
                        request_payload,
                    )
                except ImageRouteConfigError as exc:
                    logger.error(
                        "Invalid %s response mapping for '%s': %s",
                        operation_label.lower(),
                        requested_model,
                        exc,
                        exc_info=True,
                    )
                    raise HTTPException(
                        status_code=500,
                        detail="Internal server error: Invalid image route configuration.",
                    ) from exc
                except ImageDownstreamResponseError as exc:
                    raise HTTPException(
                        status_code=503,
                        detail=f"Invalid downstream image response: {exc}",
                    ) from exc
            except HTTPException as exc:
                route_error = exc
            else:
                response = json_response(response_payload, downstream_status_code)
                observation = _parse_image_terminal_observation(
                    owner,
                    downstream_json,
                    gateway_model=requested_model,
                    route=route,
                    duration_ms=request_duration_ms(request_started_at),
                )
                return await finalize_buffered_operation(owner, response, observation)

        if route_error is None:
            raise RuntimeError("image route ended without a response or error")
        if _should_try_next_image_route(route_error, route_index, routes):
            _log_image_route_fallback(
                operation_label,
                requested_model,
                route,
                route_index,
                routes,
                route_error,
            )
            continue
        raise route_error

    raise HTTPException(
        status_code=503,
        detail=f"{operation_label} request failed after exhausting fallback routes.",
    )


async def _proxy_images_generation(request: Request, route_path: str) -> object:
    request_body_json = await read_json_request_body(request, route_path)
    requested_model = _validate_generation_body(request_body_json)

    enforce_virtual_key_access(request, requested_model)

    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    routes = dispatcher.lookup_routes(IMAGE_GENERATIONS_SECTION, requested_model)
    if not routes:
        raise HTTPException(status_code=404, detail=f"No image generation route configured for model '{requested_model}'.")

    operation = partial(
        _proxy_image_with_fallback,
        request,
        dispatcher=dispatcher,
        config_loader_instance=config_loader_instance,
        proxy_http_clients=proxy_http_clients,
        http_client=http_client,
        routes=routes,
        requested_model=requested_model,
        request_payload=request_body_json,
        operation_section=IMAGE_GENERATIONS_SECTION,
        operation_name=IMAGE_GENERATION_OPERATION,
        operation_label="Image generation",
        source_transport="json",
    )
    return await _run_image_accounting(request, operation)


@router.post("/images/generations")
async def create_images_generation(request: Request):
    return await _proxy_images_generation(request, "images generation endpoint")


@router.post("/images")
async def create_images(request: Request):
    return await _proxy_images_generation(request, "images endpoint")


async def _proxy_images_edit_payload(
    request: Request,
    requested_model: str,
    request_payload: dict[str, object],
    *,
    source_transport: str,
) -> object:
    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    enforce_virtual_key_access(request, requested_model)

    routes = dispatcher.lookup_routes(IMAGE_EDITS_SECTION, requested_model)
    if not routes:
        raise HTTPException(status_code=404, detail=f"No image edit route configured for model '{requested_model}'.")

    operation = partial(
        _proxy_image_with_fallback,
        request,
        dispatcher=dispatcher,
        config_loader_instance=config_loader_instance,
        proxy_http_clients=proxy_http_clients,
        http_client=http_client,
        routes=routes,
        requested_model=requested_model,
        request_payload=request_payload,
        operation_section=IMAGE_EDITS_SECTION,
        operation_name=IMAGE_EDIT_OPERATION,
        operation_label="Image edit",
        source_transport=source_transport,
    )
    return await _run_image_accounting(request, operation)


@router.post("/images/edits")
async def create_images_edit(request: Request):
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" in content_type:
        async with _parse_multipart_edit_request(request) as (
            requested_model,
            request_payload,
        ):
            return await _proxy_images_edit_payload(
                request,
                requested_model,
                request_payload,
                source_transport="multipart",
            )

    request_body_json = await read_json_request_body(request, "images edit endpoint")
    requested_model = _validate_edit_json_body(request_body_json)
    return await _proxy_images_edit_payload(
        request,
        requested_model,
        request_body_json,
        source_transport="json",
    )
