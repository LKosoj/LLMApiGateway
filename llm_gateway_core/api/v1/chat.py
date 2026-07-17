import asyncio
import gzip
import io
import json
import logging
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from ...config.settings import settings as settings
from ...db.fallback_events_db import FallbackEventsDB
from ...services.access_control import enforce_virtual_key_access
from ...services.error_classifier import (
    _build_fallback_error_message as _build_fallback_error_message,
    classify_error as classify_error,
)
from ...services.request_handler import (
    LocalStreamObservationError as LocalStreamObservationError,
    make_llm_request as make_llm_request,
)
from ...services.provider_auth import resolve_provider_auth_material as resolve_provider_auth_material
from ...services.stream_observation import StreamObservationCapacity
from ...services.upstream_routing_state import UpstreamRoutingState
from ...utils.text_sanitize import sanitize_payload
from ...utils.usage_tracking import ModelCostRates
from .chat_accounting import ChatTerminalHandoff, get_chat_terminal_handoff

from .chat_dialects import (
    ANTHROPIC_DEFAULT_MAX_TOKENS as ANTHROPIC_DEFAULT_MAX_TOKENS,
    ANTHROPIC_TOOL_NAME_RE as ANTHROPIC_TOOL_NAME_RE,
    TOKEN_COUNT_ENCODING_FALLBACKS as TOKEN_COUNT_ENCODING_FALLBACKS,
    _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH as _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH,
    _extract_anthropic_text as _extract_anthropic_text,
    _anthropic_image_source_to_openai_part as _anthropic_image_source_to_openai_part,
    _anthropic_document_source_to_openai_parts as _anthropic_document_source_to_openai_parts,
    _anthropic_content_blocks_to_openai_parts as _anthropic_content_blocks_to_openai_parts,
    _collapse_openai_parts_to_content as _collapse_openai_parts_to_content,
    _anthropic_tool_result_to_openai_content as _anthropic_tool_result_to_openai_content,
    _anthropic_tools_to_openai as _anthropic_tools_to_openai,
    _anthropic_tool_choice_to_openai as _anthropic_tool_choice_to_openai,
    _anthropic_message_to_openai_messages as _anthropic_message_to_openai_messages,
    _anthropic_request_to_openai_payload as _anthropic_request_to_openai_payload,
    _openai_content_to_plain_text as _openai_content_to_plain_text,
    _openai_user_content_to_anthropic_blocks as _openai_user_content_to_anthropic_blocks,
    _openai_tool_content_to_anthropic as _openai_tool_content_to_anthropic,
    _openai_tool_name_to_anthropic as _openai_tool_name_to_anthropic,
    _anthropic_tool_name_to_openai as _anthropic_tool_name_to_openai,
    _openai_tools_to_anthropic as _openai_tools_to_anthropic,
    _openai_tool_choice_to_anthropic as _openai_tool_choice_to_anthropic,
    _openai_request_to_anthropic_payload as _openai_request_to_anthropic_payload,
    _coerce_anthropic_usage_token_count as _coerce_anthropic_usage_token_count,
    _anthropic_usage_to_openai_usage as _anthropic_usage_to_openai_usage,
    _anthropic_response_to_openai as _anthropic_response_to_openai,
    _resolve_model_name_for_token_count as _resolve_model_name_for_token_count,
    _resolve_tiktoken_encoding as _resolve_tiktoken_encoding,
    _build_token_count_payload as _build_token_count_payload,
    _estimate_input_tokens as _estimate_input_tokens,
    _map_finish_reason_to_anthropic as _map_finish_reason_to_anthropic,
    _openai_tool_calls_to_anthropic_blocks as _openai_tool_calls_to_anthropic_blocks,
    _openai_response_to_anthropic as _openai_response_to_anthropic,
    _coerce_responses_text as _coerce_responses_text,
    _responses_content_to_openai_content as _responses_content_to_openai_content,
    _responses_input_to_openai_messages as _responses_input_to_openai_messages,
    _responses_tools_to_openai as _responses_tools_to_openai,
    _responses_request_to_openai_payload as _responses_request_to_openai_payload,
    _build_openai_usage_to_responses_usage as _build_openai_usage_to_responses_usage,
    _openai_message_content_to_responses_content as _openai_message_content_to_responses_content,
    _openai_response_to_responses as _openai_response_to_responses,
)
from .chat_streaming import (
    JSON_STREAM_OPENING_PREFIXES as JSON_STREAM_OPENING_PREFIXES,
    JSON_STREAM_CLOSING_FENCE_RE as JSON_STREAM_CLOSING_FENCE_RE,
    JSON_STREAM_SUFFIX_BUFFER_SIZE as JSON_STREAM_SUFFIX_BUFFER_SIZE,
    _STREAM_TEMPLATE_FIELDS as _STREAM_TEMPLATE_FIELDS,
    get_token_usage as get_token_usage,
    _buffer_json_stream_suffix as _buffer_json_stream_suffix,
    _flush_json_stream_suffix as _flush_json_stream_suffix,
    _strip_stream_think_blocks as _strip_stream_think_blocks,
    _flush_stream_think_buffer as _flush_stream_think_buffer,
    _sanitize_json_object_stream_content_fragment as _sanitize_json_object_stream_content_fragment,
    _sanitize_json_object_stream_delta as _sanitize_json_object_stream_delta,
    _extract_stream_template as _extract_stream_template,
    _build_openai_stream_delta_chunk as _build_openai_stream_delta_chunk,
    _new_stream_think_state as _new_stream_think_state,
    _sanitize_openai_stream_think_tags as _sanitize_openai_stream_think_tags,
    _sanitize_anthropic_response_content_think_tags as _sanitize_anthropic_response_content_think_tags,
    _extract_sse_data_payload as _extract_sse_data_payload,
    _DirectChatStreamObservationBuilder as _DirectChatStreamObservationBuilder,
    _sanitize_anthropic_stream_think_tags as _sanitize_anthropic_stream_think_tags,
    _sanitize_openai_json_object_stream as _sanitize_openai_json_object_stream,
    _anthropic_stream_to_openai as _anthropic_stream_to_openai,
    _format_anthropic_sse_event as _format_anthropic_sse_event,
    _is_openai_stream_error_chunk as _is_openai_stream_error_chunk,
    _update_request_usage_tracker as _update_request_usage_tracker,
    _openai_stream_to_anthropic as _openai_stream_to_anthropic,
    _format_responses_sse_event as _format_responses_sse_event,
    _openai_stream_to_responses as _openai_stream_to_responses,
)
from .chat_dispatch import (
    TEMPORARY_MODEL_FAILURE_MARKERS as TEMPORARY_MODEL_FAILURE_MARKERS,
    TEMPORARY_MODEL_FAILURE_STATUS_RE as TEMPORARY_MODEL_FAILURE_STATUS_RE,
    ANTHROPIC_API_KEY_HEADER_NAME as ANTHROPIC_API_KEY_HEADER_NAME,
    _provider_key_routing_strategy as _provider_key_routing_strategy,
    _provider_key_routing_affinity_enabled as _provider_key_routing_affinity_enabled,
    _provider_key_routing_affinity_header as _provider_key_routing_affinity_header,
    _provider_key_routing_affinity_ttl as _provider_key_routing_affinity_ttl,
    _upstream_key_candidates_for_provider as _upstream_key_candidates_for_provider,
    _extract_authorization_token as _extract_authorization_token,
    _extract_gateway_auth_token_from_request as _extract_gateway_auth_token_from_request,
    _auth_scope_for_request as _auth_scope_for_request,
    _affinity_scope_for_request as _affinity_scope_for_request,
    _rotation_scope_for_request as _rotation_scope_for_request,
    _require_model_rotation_db as _require_model_rotation_db,
    _is_temporary_model_failure as _is_temporary_model_failure,
    _finalize_chat_success_response as _finalize_chat_success_response,
    _extract_retry_after as _extract_retry_after,
    _compute_retry_sleep_seconds as _compute_retry_sleep_seconds,
    _local_stream_observation_http_error as _local_stream_observation_http_error,
)
from .chat_dispatch import (
    attempt_model_fallback_rule as _attempt_model_fallback_rule_impl,
    dispatch_chat_request as _dispatch_chat_request_impl,
)

if TYPE_CHECKING:
    from ...services.runtime_config import RuntimeSnapshot

router = APIRouter()
anthropic_router = APIRouter()
responses_router = APIRouter()


def _read_json_request_body(request_body_bytes: bytes, endpoint_name: str, request: Request = None) -> dict:
    if not request_body_bytes:
        return {}

    # Handle GZip compression in request body
    if request and "gzip" in request.headers.get("content-encoding", "").lower():
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(request_body_bytes)) as f:
                request_body_bytes = f.read()
        except Exception as e:
            logging.warning(f"Failed to decompress GZip request body for {endpoint_name}: {e}")

    try:
        request_body_json = json.loads(request_body_bytes.decode("utf-8"))
        if not isinstance(request_body_json, dict):
            raise ValueError("Request body must be a JSON object")
        return sanitize_payload(request_body_json)
    except Exception as e:
        logging.error(f"Error reading request body for {endpoint_name}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error reading request body: {str(e)}")


async def _attempt_model_fallback_rule(
    request: Request,
    http_client: object,
    providers_config: dict,
    requested_model: str,
    request_body_json: dict,
    model_fallback_rule: dict,
    is_streaming: bool,
    attempt_label: str = "fallback model",
    *,
    proxy_http_clients: Mapping[str, object],
    upstream_routing_state: UpstreamRoutingState,
    fallback_events_db: FallbackEventsDB | None = None,
    request_id: str | None = None,
    attempt_number_start: int = 1,
    total_attempts_budget: int | None = None,
    stop_after_context_overflow: bool = False,
    chat_accounting_handoff: ChatTerminalHandoff | None = None,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates] | None = None,
    stream_observation_capacity: StreamObservationCapacity | None = None,
    stream_event_max_bytes: int | None = None,
) -> tuple[object | None, str | None, int]:
    """Attempt a single provider+model, optionally retrying.

    ``total_attempts_budget`` enforces a *chain-wide* cap on the number of
    upstream calls: every attempt (initial or retry, regular or sub-provider)
    decrements the budget. When the budget is exhausted, the function returns
    immediately with the last error so the caller does not advance to the next
    fallback model. ``None`` disables the cap (legacy behavior).
    """
    return await _attempt_model_fallback_rule_impl(
        request,
        http_client,
        providers_config,
        requested_model,
        request_body_json,
        model_fallback_rule,
        is_streaming,
        attempt_label,
        proxy_http_clients=proxy_http_clients,
        upstream_routing_state=upstream_routing_state,
        fallback_events_db=fallback_events_db,
        request_id=request_id,
        attempt_number_start=attempt_number_start,
        total_attempts_budget=total_attempts_budget,
        stop_after_context_overflow=stop_after_context_overflow,
        chat_accounting_handoff=chat_accounting_handoff,
        cost_rate_registry=cost_rate_registry,
        stream_observation_capacity=stream_observation_capacity,
        stream_event_max_bytes=stream_event_max_bytes,
        make_llm_request_hook=make_llm_request,
        asyncio_hook=asyncio,
        logging_hook=logging,
        classify_error_hook=classify_error,
        resolve_provider_auth_material_hook=resolve_provider_auth_material,
    )


async def _dispatch_chat_request(
    request: Request,
    request_body_json: dict,
    *,
    enforce_model_access: bool = True,
    accounting_handoff: ChatTerminalHandoff | None = None,
):
    return await _dispatch_chat_request_impl(
        request,
        request_body_json,
        enforce_model_access=enforce_model_access,
        accounting_handoff=accounting_handoff,
        attempt_model_fallback_rule_hook=_attempt_model_fallback_rule,
        logging_hook=logging,
    )


@router.post("/completions")
async def chat_completions(request: Request):
    request_body_bytes = await request.body()
    request_body_json = _read_json_request_body(request_body_bytes, "/v1/chat/completions", request)
    return await _dispatch_chat_request(
        request,
        request_body_json,
        accounting_handoff=get_chat_terminal_handoff(request),
    )


@responses_router.post("/responses")
async def openai_responses(request: Request):
    request_body_bytes = await request.body()
    responses_payload = _read_json_request_body(request_body_bytes, "/v1/responses", request)
    openai_payload, requested_model = _responses_request_to_openai_payload(responses_payload)
    request.state.llmgateway_original_responses_payload = responses_payload

    # Set metadata for stats
    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = "chat"

    response_data = await _dispatch_chat_request(
        request,
        openai_payload,
        accounting_handoff=get_chat_terminal_handoff(request),
    )

    if openai_payload.get("stream", False):
        if not isinstance(response_data, StreamingResponse):
            raise HTTPException(status_code=500, detail="Internal server error: expected streaming response")
        return _openai_stream_to_responses(response_data, requested_model, request=request)

    if not isinstance(response_data, dict):
        raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
    _update_request_usage_tracker(request, response_data.get("usage"))
    return _openai_response_to_responses(response_data, requested_model)


@anthropic_router.post("/messages")
async def anthropic_messages(request: Request):
    request_body_bytes = await request.body()
    anthropic_payload = _read_json_request_body(request_body_bytes, "/v1/messages", request)

    requested_model = anthropic_payload.get("model")
    if requested_model:
        request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = "chat"

    # Preserve the raw Anthropic payload so that native Anthropic providers in
    # the fallback chain receive it verbatim, without a lossy round-trip
    # through the OpenAI shape (cache_control, thinking, metadata, ...).
    request.state.llmgateway_original_anthropic_payload = anthropic_payload

    openai_payload, requested_model = _anthropic_request_to_openai_payload(anthropic_payload)
    response_data = await _dispatch_chat_request(
        request,
        openai_payload,
        accounting_handoff=get_chat_terminal_handoff(request),
    )

    # A native Anthropic provider in the fallback chain produces an Anthropic-shaped
    # response already; forward it verbatim instead of round-tripping through OpenAI.
    is_anthropic_raw = bool(
        getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
    )

    if openai_payload.get("stream", False):
        if not isinstance(response_data, StreamingResponse):
            raise HTTPException(status_code=500, detail="Internal server error: expected streaming response")
        if is_anthropic_raw:
            return response_data
        return _openai_stream_to_anthropic(request, response_data, requested_model)
    if not isinstance(response_data, dict):
        raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
    _update_request_usage_tracker(request, response_data.get("usage"))
    if is_anthropic_raw:
        return response_data
    return _openai_response_to_anthropic(response_data, requested_model)


@anthropic_router.post("/messages/count_tokens")
async def anthropic_messages_count_tokens(request: Request):
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    config_loader_instance = runtime_snapshot.config_loader
    request_body_bytes = await request.body()
    anthropic_payload = _read_json_request_body(request_body_bytes, "/v1/messages/count_tokens", request)
    openai_payload, requested_model = _anthropic_request_to_openai_payload(anthropic_payload)
    enforce_virtual_key_access(request, requested_model)

    provider_model_name = _resolve_model_name_for_token_count(config_loader_instance, requested_model)
    input_tokens = _estimate_input_tokens(openai_payload, provider_model_name)
    return {"input_tokens": input_tokens}
