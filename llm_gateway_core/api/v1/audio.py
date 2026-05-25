import json
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request, Response, UploadFile
from fastapi.responses import JSONResponse
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...config.loader import ConfigLoader, OperationRoute, REQUEST_FORMAT_NVIDIA_RIVA_GRPC, resolve_provider_api_key
from ...services.access_control import enforce_virtual_key_access
from ...services.request_handler import OperationDispatcher, normalize_retry_settings
from .audio_adapters import sanitize_nvidia_riva_request_payload, transcribe_with_nvidia_riva_grpc
from .operation_proxy import (
    extract_downstream_error_detail,
    proxy_json_raw_to_downstream,
    proxy_multipart_raw_to_downstream,
    record_operation_usage,
    read_json_request_body,
    request_duration_ms,
    sanitize_target_url_for_log,
    should_retry_operation_status,
    sleep_before_retry,
)

logger = logging.getLogger(__name__)

router = APIRouter()
AUDIO_SPEECH_SECTION = "audio_speech"
AUDIO_SPEECH_OPERATION = "audio_speech"
AUDIO_TRANSCRIPTIONS_SECTION = "audio_transcriptions"
AUDIO_TRANSCRIPTION_OPERATION = "audio_transcription"
AUDIO_TRANSCRIPTION_DOWNSTREAM_TIMEOUT = httpx.Timeout(connect=30.0, read=1200.0, write=60.0, pool=30.0)
TRUTHY_FORM_VALUES = frozenset({"1", "on", "true", "yes"})
PROTECTED_AUDIO_CUSTOM_PARAM_KEYS = frozenset({"file", "model"})
PROTECTED_AUDIO_SPEECH_CUSTOM_PARAM_KEYS = frozenset({"input", "model"})
AUDIO_VOICES_DEFAULT_TARGET_PATH = "/voices"
KNOWN_VOICE_METADATA: dict[str, dict[str, str]] = {
    "aidar": {"gender": "male", "language": "ru"},
    "eugene": {"gender": "male", "language": "ru"},
    "baya": {"gender": "female", "language": "ru"},
    "kseniya": {"gender": "female", "language": "ru"},
    "xenia": {"gender": "female", "language": "ru"},
}


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


def _parse_form_field_value(value: object) -> object:
    if isinstance(value, str):
        return value.strip()
    return value


def _is_truthy_form_value(value: object) -> bool:
    if value is True:
        return True
    if isinstance(value, str):
        return value.strip().lower() in TRUTHY_FORM_VALUES
    return False


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
    return value.filename or "audio.bin", await value.read(), value.content_type


async def _parse_audio_transcription_request(
    request: Request,
) -> tuple[str, dict[str, object], list[tuple[str, tuple[str, bytes, str | None]]]]:
    content_type = request.headers.get("content-type", "").lower()
    if "multipart/form-data" not in content_type:
        raise HTTPException(status_code=400, detail="Audio transcriptions endpoint expects multipart/form-data.")

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

        _append_scalar_form_value(scalar_fields, key, _parse_form_field_value(value))

    requested_model = scalar_fields.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    if upload_file is None:
        raise HTTPException(status_code=400, detail="Missing 'file' in request body")

    if _is_truthy_form_value(scalar_fields.get("stream")):
        raise HTTPException(status_code=400, detail="Audio transcription streaming is not supported.")

    return requested_model, scalar_fields, [("file", upload_file)]


async def _parse_audio_speech_request(request: Request) -> tuple[str, dict[str, object]]:
    content_type = request.headers.get("content-type", "").lower()
    if "application/json" not in content_type:
        raise HTTPException(status_code=400, detail="Audio speech endpoint expects application/json.")

    request_payload = await read_json_request_body(request, "audio speech")
    requested_model = request_payload.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    input_text = request_payload.get("input")
    if not isinstance(input_text, str) or not input_text.strip():
        raise HTTPException(status_code=400, detail="Missing 'input' in request body")

    if _is_truthy_form_value(request_payload.get("stream")):
        raise HTTPException(status_code=400, detail="Audio speech streaming is not supported.")

    return requested_model.strip(), request_payload


def _coerce_multipart_scalar(value: object) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if value is None:
        return ""
    if isinstance(value, (str, int, float)):
        return str(value)
    return json.dumps(value, ensure_ascii=False)


def _flatten_scalar_fields(payload: dict[str, object]) -> list[tuple[str, str]]:
    flattened: list[tuple[str, str]] = []
    for field_name, field_value in payload.items():
        if isinstance(field_value, list):
            for item in field_value:
                flattened.append((field_name, _coerce_multipart_scalar(item)))
            continue

        flattened.append((field_name, _coerce_multipart_scalar(field_value)))
    return flattened


def _build_route_payload(
    request_payload: dict[str, object],
    route,
) -> dict[str, object]:
    downstream_payload = dict(request_payload)
    downstream_payload["model"] = route.model

    for param_name, param_value in route.custom_body_params.items():
        if param_name.lower() in PROTECTED_AUDIO_CUSTOM_PARAM_KEYS:
            continue
        downstream_payload[param_name] = param_value

    return downstream_payload


def _build_speech_route_payload(
    request_payload: dict[str, object],
    route,
) -> dict[str, object]:
    downstream_payload = dict(request_payload)
    downstream_payload["model"] = route.model

    for param_name, param_value in route.custom_body_params.items():
        if param_name.lower() in PROTECTED_AUDIO_SPEECH_CUSTOM_PARAM_KEYS:
            continue
        downstream_payload[param_name] = param_value

    return downstream_payload


def _prepare_audio_request_state(
    request: Request,
    dispatcher: OperationDispatcher,
    route,
    *,
    gateway_model: str,
    operation: str = AUDIO_TRANSCRIPTION_OPERATION,
) -> tuple[int, float]:
    retry_settings = normalize_retry_settings(route.retry_count, route.retry_delay)

    request.state.llmgateway_gateway_model = gateway_model
    dispatcher.set_request_state(
        request=request,
        operation=operation,
        route=route,
        provider_name=route.provider,
        provider_model=route.model,
    )
    return retry_settings


def _prepare_audio_http_request(
    dispatcher: OperationDispatcher,
    route,
    provider_config,
) -> tuple[str, dict[str, str]]:
    provider_api_key = resolve_provider_api_key(provider_config.apikey)
    target_url = dispatcher.build_target_url(route, provider_config)
    headers = dispatcher.build_headers(route, provider_api_key)
    multipart_headers = {key: value for key, value in headers.items() if key.lower() != "content-type"}
    return target_url, multipart_headers


def _prepare_audio_json_http_request(
    dispatcher: OperationDispatcher,
    route,
    provider_config,
) -> tuple[str, dict[str, str]]:
    provider_api_key = resolve_provider_api_key(provider_config.apikey)
    target_url = dispatcher.build_target_url(route, provider_config)
    headers = dispatcher.build_headers(route, provider_api_key)
    return target_url, headers


def _prepare_audio_voices_http_request(
    dispatcher: OperationDispatcher,
    route: OperationRoute,
    provider_config,
) -> tuple[str, dict[str, str]]:
    provider_api_key = resolve_provider_api_key(provider_config.apikey)
    target_path = route.voices_target_path or AUDIO_VOICES_DEFAULT_TARGET_PATH
    target_url = (
        target_path
        if target_path.startswith(("http://", "https://"))
        else f"{provider_config.baseUrl.rstrip('/')}/{target_path.lstrip('/')}"
    )
    headers = dispatcher.build_headers(route, provider_api_key)
    headers.pop("Content-Type", None)
    headers.pop("content-type", None)
    return target_url, headers


async def _fetch_audio_voices_payload(
    target_url: str,
    headers: dict[str, str],
    http_client: httpx.AsyncClient,
    *,
    retry_count: int,
    retry_delay: float,
) -> dict[str, object]:
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Fetching audio voices from downstream %s", sanitized_target_url)

    total_attempts = retry_count + 1
    attempt_number = 0
    while attempt_number < total_attempts:
        attempt_number += 1
        try:
            response = await http_client.get(target_url, headers=headers)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Audio voices downstream request to %s failed with network error: %s",
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

        logger.info("Audio voices downstream response status %s from %s", response.status_code, sanitized_target_url)
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

        try:
            payload = response.json()
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc
        if not isinstance(payload, dict):
            raise HTTPException(status_code=503, detail="Downstream voices response must be a JSON object.")
        return payload

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


def _normalize_voice_item(item: object, source: str | None = None) -> dict[str, object] | None:
    if isinstance(item, str):
        voice_id = item.strip()
        if not voice_id:
            return None
        voice: dict[str, object] = {"id": voice_id, "name": voice_id}
    elif isinstance(item, dict):
        raw_id = item.get("id") or item.get("voice") or item.get("name")
        if not isinstance(raw_id, str) or not raw_id.strip():
            return None
        voice_id = raw_id.strip()
        raw_name = item.get("name")
        voice = {
            "id": voice_id,
            "name": raw_name.strip() if isinstance(raw_name, str) and raw_name.strip() else voice_id,
        }
        for field_name in ("model", "provider", "type", "language", "gender"):
            field_value = item.get(field_name)
            if isinstance(field_value, str) and field_value.strip():
                voice[field_name] = field_value.strip()
    else:
        return None

    if source:
        voice["source"] = source

    metadata = KNOWN_VOICE_METADATA.get(str(voice["id"]).lower())
    if metadata:
        voice.update(metadata)

    return voice


def _voice_matches_model(voice: dict[str, object] | None, model: str | None) -> bool:
    if voice is None or not model:
        return True
    voice_model = voice.get("model")
    return not isinstance(voice_model, str) or voice_model == model


def _merge_voice(voices_by_id: dict[str, dict[str, object]], voice: dict[str, object] | None) -> None:
    if voice is None:
        return

    voice_id = str(voice["id"])
    existing = voices_by_id.get(voice_id)
    if existing is None:
        voices_by_id[voice_id] = voice
        return

    for key, value in voice.items():
        if key == "name" and existing.get("name") != voice_id:
            continue
        existing[key] = value


def _normalize_audio_voices_payload(payload: dict[str, object], model: str | None = None) -> list[dict[str, object]]:
    voices_by_id: dict[str, dict[str, object]] = {}
    raw_voices = payload.get("voices", [])
    model_scoped_catalog = (
        isinstance(raw_voices, list)
        and any(isinstance(item, dict) and isinstance(item.get("model"), str) for item in raw_voices)
    )
    for item in raw_voices if isinstance(raw_voices, list) else []:
        voice = _normalize_voice_item(item)
        if _voice_matches_model(voice, model):
            _merge_voice(voices_by_id, voice)
    for item in payload.get("preset_voices", []) if isinstance(payload.get("preset_voices"), list) else []:
        if model and model_scoped_catalog and isinstance(item, str):
            continue
        voice = _normalize_voice_item(item, "preset")
        if _voice_matches_model(voice, model):
            _merge_voice(voices_by_id, voice)
    for item in payload.get("custom_voices", []) if isinstance(payload.get("custom_voices"), list) else []:
        voice = _normalize_voice_item(item, "custom")
        if _voice_matches_model(voice, model):
            _merge_voice(voices_by_id, voice)

    default_voice = payload.get("default_voice")
    if isinstance(default_voice, str) and default_voice in voices_by_id:
        voices_by_id[default_voice]["default"] = True

    return sorted(voices_by_id.values(), key=lambda voice: str(voice["id"]))


def _allowed_audio_speech_model_names(request: Request) -> list[str]:
    operation_rules = getattr(request.app.state, "operation_rules", {})
    audio_speech_rules = operation_rules.get(AUDIO_SPEECH_SECTION, {})
    if not isinstance(audio_speech_rules, dict):
        return []

    model_names = sorted(audio_speech_rules.keys())
    api_key_record = getattr(request.state, "api_key_record", None)
    if api_key_record is not None and api_key_record.allowed_models:
        model_names = [model_name for model_name in model_names if api_key_record.model_allowed(model_name)]
    return model_names


async def _get_audio_voices_for_model(request: Request, requested_model: str) -> list[dict[str, object]]:
    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    route = dispatcher.lookup_route(AUDIO_SPEECH_SECTION, requested_model)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"No audio speech route configured for model '{requested_model}'.",
        )

    provider_config = config_loader_instance.providers_config.get(route.provider)
    if provider_config is None:
        logger.error(
            "Provider '%s' for audio voices route '%s' is missing from providers_config.",
            route.provider,
            requested_model,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error: Provider configuration not available.",
        )

    retry_count, retry_delay = normalize_retry_settings(route.retry_count, route.retry_delay)
    target_url, headers = _prepare_audio_voices_http_request(dispatcher, route, provider_config)
    effective_client = proxy_http_clients.get(route.provider, http_client)
    downstream_payload = await _fetch_audio_voices_payload(
        target_url,
        headers,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )
    return _normalize_audio_voices_payload(downstream_payload, model=route.model)


def _is_json_content_type(content_type: str | None) -> bool:
    if not content_type:
        return False
    normalized_content_type = content_type.lower()
    return "/json" in normalized_content_type or "+json" in normalized_content_type


def _should_try_next_route(exc: HTTPException, route_index: int, routes: list[OperationRoute]) -> bool:
    return exc.status_code == 503 and route_index < len(routes) - 1


def _log_route_fallback(
    requested_model: str,
    route: OperationRoute,
    route_index: int,
    routes: list[OperationRoute],
    exc: HTTPException,
) -> None:
    logger.warning(
        "Audio transcription route failed for gateway model '%s'; falling back to next route. "
        "route=%s/%s provider=%s model=%s detail=%s",
        requested_model,
        route_index + 1,
        len(routes),
        route.provider,
        route.model,
        exc.detail,
    )


@router.post("/audio/transcriptions")
async def create_audio_transcription(request: Request):
    requested_model, request_payload, files_payload = await _parse_audio_transcription_request(request)

    enforce_virtual_key_access(request, requested_model)

    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    routes = dispatcher.lookup_routes(AUDIO_TRANSCRIPTIONS_SECTION, requested_model)
    if not routes:
        raise HTTPException(
            status_code=404,
            detail=f"No audio transcription route configured for model '{requested_model}'.",
        )

    request_started_at = time.monotonic()

    for route_index, route in enumerate(routes):
        try:
            provider_config = config_loader_instance.providers_config.get(route.provider)
            if provider_config is None:
                logger.error(
                    "Provider '%s' for audio transcription route '%s' is missing from providers_config.",
                    route.provider,
                    requested_model,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Provider configuration not available.",
                )

            retry_count, retry_delay = _prepare_audio_request_state(
                request,
                dispatcher,
                route,
                gateway_model=requested_model,
            )
            route_request_payload = request_payload
            if route.request_format == REQUEST_FORMAT_NVIDIA_RIVA_GRPC:
                route_request_payload = sanitize_nvidia_riva_request_payload(request_payload)

            route_payload = _build_route_payload(route_request_payload, route)
            if route.request_format == REQUEST_FORMAT_NVIDIA_RIVA_GRPC:
                adapter_response = await transcribe_with_nvidia_riva_grpc(
                    request_payload=route_payload,
                    files_payload=files_payload,
                    provider_name=route.provider,
                    provider_base_url=provider_config.baseUrl,
                    provider_api_key=resolve_provider_api_key(provider_config.apikey),
                    route_custom_headers=route.custom_headers,
                    target_path=route.target_path,
                    retry_count=retry_count,
                    retry_delay=retry_delay,
                )
                duration_ms = request_duration_ms(request_started_at)
                await record_operation_usage(
                    request,
                    {},
                    gateway_model=requested_model,
                    operation=AUDIO_TRANSCRIPTION_OPERATION,
                    duration_ms=duration_ms,
                )
                if adapter_response.is_json:
                    return JSONResponse(content=adapter_response.body, status_code=adapter_response.status_code)

                return Response(
                    content=adapter_response.body,
                    status_code=adapter_response.status_code,
                    headers={"content-type": adapter_response.content_type},
                )

            target_url, headers = _prepare_audio_http_request(
                dispatcher,
                route,
                provider_config,
            )
            effective_client = proxy_http_clients.get(route.provider, http_client)
            response_body, downstream_status_code, downstream_content_type = await proxy_multipart_raw_to_downstream(
                target_url,
                headers,
                _flatten_scalar_fields(route_payload),
                files_payload,
                effective_client,
                retry_count=retry_count,
                retry_delay=retry_delay,
                timeout=AUDIO_TRANSCRIPTION_DOWNSTREAM_TIMEOUT,
            )

            duration_ms = request_duration_ms(request_started_at)
            if _is_json_content_type(downstream_content_type):
                try:
                    parsed_response = json.loads(response_body.decode("utf-8"))
                except (UnicodeDecodeError, ValueError) as exc:
                    raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc

                usage_payload = parsed_response if isinstance(parsed_response, dict) else {}
                await record_operation_usage(
                    request,
                    usage_payload,
                    gateway_model=requested_model,
                    operation=AUDIO_TRANSCRIPTION_OPERATION,
                    duration_ms=duration_ms,
                )
                return JSONResponse(content=parsed_response, status_code=downstream_status_code)

            await record_operation_usage(
                request,
                {},
                gateway_model=requested_model,
                operation=AUDIO_TRANSCRIPTION_OPERATION,
                duration_ms=duration_ms,
            )
            response_headers = {"content-type": downstream_content_type} if downstream_content_type else None
            return Response(content=response_body, status_code=downstream_status_code, headers=response_headers)
        except HTTPException as exc:
            if _should_try_next_route(exc, route_index, routes):
                _log_route_fallback(requested_model, route, route_index, routes, exc)
                continue
            raise

    raise HTTPException(status_code=503, detail="Audio transcription request failed after exhausting fallback routes.")


@router.get("/audio/voices")
async def list_audio_voices(request: Request, model: str | None = None):
    requested_model = model.strip() if isinstance(model, str) else None
    if requested_model:
        enforce_virtual_key_access(request, requested_model)
        return JSONResponse(
            content={
                "object": "audio.voice_list",
                "model": requested_model,
                "data": await _get_audio_voices_for_model(request, requested_model),
            }
        )

    voice_catalog: dict[str, list[dict[str, object]]] = {}
    for model_name in _allowed_audio_speech_model_names(request):
        voice_catalog[model_name] = await _get_audio_voices_for_model(request, model_name)

    return JSONResponse(content={"object": "audio.voice_catalog", "data": voice_catalog})


@router.post("/audio/speech")
async def create_audio_speech(request: Request):
    requested_model, request_payload = await _parse_audio_speech_request(request)

    enforce_virtual_key_access(request, requested_model)

    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    route = dispatcher.lookup_route(AUDIO_SPEECH_SECTION, requested_model)
    if route is None:
        raise HTTPException(
            status_code=404,
            detail=f"No audio speech route configured for model '{requested_model}'.",
        )

    provider_config = config_loader_instance.providers_config.get(route.provider)
    if provider_config is None:
        logger.error(
            "Provider '%s' for audio speech route '%s' is missing from providers_config.",
            route.provider,
            requested_model,
        )
        raise HTTPException(
            status_code=500,
            detail="Internal server error: Provider configuration not available.",
        )

    request_started_at = time.monotonic()
    retry_count, retry_delay = _prepare_audio_request_state(
        request,
        dispatcher,
        route,
        gateway_model=requested_model,
        operation=AUDIO_SPEECH_OPERATION,
    )
    route_payload = _build_speech_route_payload(request_payload, route)
    target_url, headers = _prepare_audio_json_http_request(dispatcher, route, provider_config)
    effective_client = proxy_http_clients.get(route.provider, http_client)
    response_body, downstream_status_code, downstream_content_type = await proxy_json_raw_to_downstream(
        target_url,
        headers,
        route_payload,
        effective_client,
        retry_count=retry_count,
        retry_delay=retry_delay,
    )

    await record_operation_usage(
        request,
        {},
        gateway_model=requested_model,
        operation=AUDIO_SPEECH_OPERATION,
        duration_ms=request_duration_ms(request_started_at),
    )

    response_headers = {"content-type": downstream_content_type or "audio/mpeg"}
    return Response(content=response_body, status_code=downstream_status_code, headers=response_headers)
