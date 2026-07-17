import copy
import json
import logging
import random
import re
import time
from collections.abc import Mapping
from typing import TYPE_CHECKING, cast
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

from ...config.loader import (
    ANTHROPIC_API_VERSION,
    resolve_provider_api_keys,
    resolve_provider_api_key_value,
)
from ...config.settings import settings
from ...db.fallback_events_db import FallbackEventsDB
from ...services.access_control import enforce_virtual_key_access
from ...services.accounting import AccountingValidationError
from ...services.active_requests import update_active_request
from ...services.error_classifier import (
    _build_fallback_error_message,
    _is_context_overflow_error,
    _log_failed_attempt_warning,
    _normalize_provider_attempt_payload,
)
from ...services.chat_accounting import (
    ObservedChatResponse,
    build_direct_chat_terminal_observation,
)
from ...services.model_policy import resolve_model_name
from ...services.payload_transform import apply_payload_transforms
from ...services.request_handler import (
    LocalStreamObservationError,
    MAX_RETRY_AFTER_SECONDS,
    SECURITY_HEADER_NAMES,
    normalize_retry_settings,
)
from ...services.stream_observation import StreamObservationCapacity
from ...services.upstream_routing_state import (
    UpstreamKeyCandidate,
    UpstreamRoutingState,
    fingerprint_api_key,
    upstream_limits_for_model,
)
from ...utils.api_keys import split_api_keys
from ...utils.log_redaction import redact_payload_for_log
from ...utils.usage_tracking import ModelCostRates, estimate_prompt_tokens, extract_request_x_title
from .chat_accounting import ChatStreamDialect, ChatTerminalHandoff
from .chat_dialects import (
    ANTHROPIC_DEFAULT_MAX_TOKENS,
    _anthropic_response_to_openai,
    _openai_request_to_anthropic_payload,
)
from .chat_sanitizers import (
    expects_json_object_response as _expects_json_object_response,
    sanitize_json_object_response_content as _sanitize_json_object_response_content,
    sanitize_openai_response_content_think_tags as _sanitize_openai_response_content_think_tags,
)
from .chat_streaming import (
    _DirectChatStreamObservationBuilder,
    _anthropic_stream_to_openai,
    _sanitize_anthropic_response_content_think_tags,
    _sanitize_anthropic_stream_think_tags,
    _sanitize_openai_json_object_stream,
    _sanitize_openai_stream_think_tags,
)

if TYPE_CHECKING:
    from ...db.model_rotation_db import ModelRotationDB
    from ...services.runtime_config import AppServices, RuntimeSnapshot

TEMPORARY_MODEL_FAILURE_MARKERS = (
    "currently overloaded",
    "engine is overloaded",
    "overloaded",
    "rate_limit",
    "rate limit",
    "too many requests",
    "try again later",
    "temporarily unavailable",
    "temporary unavailable",
    "service unavailable",
)
TEMPORARY_MODEL_FAILURE_STATUS_RE = re.compile(
    r"\b(?:downstream\s+error|http|status(?:\s+code)?|error)\s*(429|5[0-9]{2})\b",
    re.IGNORECASE,
)
ANTHROPIC_API_KEY_HEADER_NAME = "X-Api-Key"


def _provider_key_routing_strategy(provider_config: object, pool_config: object | None) -> str:
    pool_strategy = getattr(pool_config, "strategy", None)
    if pool_strategy:
        return str(pool_strategy)
    routing_config = getattr(provider_config, "routing", None)
    return str(getattr(routing_config, "strategy", "round-robin"))


def _provider_key_routing_affinity_enabled(provider_config: object, pool_config: object | None) -> bool:
    pool_affinity = getattr(pool_config, "session_affinity", None)
    if pool_affinity is not None:
        return bool(pool_affinity)
    routing_config = getattr(provider_config, "routing", None)
    return bool(getattr(routing_config, "session_affinity", False))


def _provider_key_routing_affinity_header(provider_config: object, pool_config: object | None) -> str:
    pool_header = getattr(pool_config, "session_affinity_header", None)
    if pool_header:
        return str(pool_header)
    routing_config = getattr(provider_config, "routing", None)
    return str(getattr(routing_config, "session_affinity_header", "X-Session-Id"))


def _provider_key_routing_affinity_ttl(provider_config: object, pool_config: object | None) -> int:
    pool_ttl = getattr(pool_config, "session_affinity_ttl_seconds", None)
    if pool_ttl is not None:
        return int(pool_ttl)
    routing_config = getattr(provider_config, "routing", None)
    return int(getattr(routing_config, "session_affinity_ttl_seconds", 3600))


def _upstream_key_candidates_for_provider(
    provider_name: str,
    provider_config: object,
    pool_name: str | None,
) -> tuple[list[UpstreamKeyCandidate], object | None, str]:
    if not pool_name:
        if not getattr(provider_config, "apikey", None) and getattr(provider_config, "upstream_key_pools", None):
            raise ValueError("fallback rule must specify upstream_key_pool for this pool-only provider.")
        api_keys = resolve_provider_api_keys(getattr(provider_config, "apikey", None))
        return (
            [UpstreamKeyCandidate(api_key=api_key, order=index) for index, api_key in enumerate(api_keys)],
            None,
            "default",
        )

    pools = getattr(provider_config, "upstream_key_pools", None)
    pool_config = pools.get(pool_name) if isinstance(pools, dict) else None
    if pool_config is None:
        raise ValueError(f"upstream_key_pool '{pool_name}' is not configured for provider.")

    candidates: list[UpstreamKeyCandidate] = []
    for key_index, key_spec in enumerate(pool_config.keys):
        if not getattr(key_spec, "enabled", True):
            continue
        resolved_value = resolve_provider_api_key_value(key_spec.apikey)
        for split_index, api_key in enumerate(split_api_keys(resolved_value)):
            candidate_id = key_spec.id
            if candidate_id and "," in str(resolved_value):
                candidate_id = f"{candidate_id}:{split_index}"
            candidates.append(
                UpstreamKeyCandidate(
                    api_key=api_key,
                    order=len(candidates),
                    priority=int(getattr(key_spec, "priority", 0)),
                    candidate_id=candidate_id or f"{pool_name}:{key_index}:{split_index}",
                )
            )

    if not candidates:
        raise ValueError(f"upstream_key_pool '{pool_name}' for provider has no enabled keys.")
    return candidates, pool_config, pool_name


def _extract_authorization_token(auth_header: str) -> str:
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]

    return auth_header


def _extract_gateway_auth_token_from_request(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return _extract_authorization_token(auth_header)

    return request.headers.get(ANTHROPIC_API_KEY_HEADER_NAME, "")


def _auth_scope_for_request(request: Request) -> str:
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is not None:
        return f"user:{api_key_id}"
    role = getattr(request.state, "api_key_role", "master")
    if role != "master":
        return f"role:{role}"
    token = _extract_gateway_auth_token_from_request(request) or settings.gateway_api_key
    return f"master:{fingerprint_api_key(token)}"


def _affinity_scope_for_request(request: Request) -> str:
    return _auth_scope_for_request(request)


def _rotation_scope_for_request(request: Request) -> str:
    return _auth_scope_for_request(request)


def _require_model_rotation_db(request: Request) -> "ModelRotationDB":
    services = cast("AppServices", request.app.state.services)
    return services.model_rotation_db


def _is_temporary_model_failure(error_detail: object) -> bool:
    if not error_detail:
        return False

    status_code = getattr(error_detail, "status_code", None)
    try:
        status_code_int = int(status_code)
    except (TypeError, ValueError):
        status_code_int = None
    if status_code_int == 429 or (status_code_int is not None and 500 <= status_code_int <= 599):
        return True

    lower_error_text = str(error_detail).lower()
    if (
        "readtimeout" in lower_error_text
        or "read timed out" in lower_error_text
        or "connecttimeout" in lower_error_text
        or "connect timed out" in lower_error_text
        or "connecterror" in lower_error_text
        or "connection refused" in lower_error_text
    ):
        return True
    if '"code":"429"' in lower_error_text or TEMPORARY_MODEL_FAILURE_STATUS_RE.search(lower_error_text):
        return True
    return any(marker in lower_error_text for marker in TEMPORARY_MODEL_FAILURE_MARKERS)


def _finalize_chat_success_response(
    response_data: object,
    requested_model: str,
    request_body_json: dict,
    *,
    strip_think_tags: bool = False,
    is_anthropic_raw: bool = False,
) -> object:
    # Native Anthropic replies bypass the OpenAI round-trip and therefore the
    # OpenAI-shaped sanitizers below. Handle the flag in their own shape; the
    # JSON-object sanitization stays OpenAI-only and is intentionally untouched.
    if is_anthropic_raw:
        if strip_think_tags:
            if isinstance(response_data, dict):
                _sanitize_anthropic_response_content_think_tags(response_data, requested_model)
            elif isinstance(response_data, StreamingResponse):
                return _sanitize_anthropic_stream_think_tags(response_data, requested_model)
        return response_data

    expects_json_object_response = _expects_json_object_response(request_body_json)

    if isinstance(response_data, dict):
        if expects_json_object_response:
            _sanitize_json_object_response_content(response_data, requested_model)
        elif strip_think_tags:
            _sanitize_openai_response_content_think_tags(response_data, requested_model)
        return response_data

    if isinstance(response_data, StreamingResponse):
        if expects_json_object_response:
            return _sanitize_openai_json_object_stream(response_data, requested_model)
        if strip_think_tags:
            return _sanitize_openai_stream_think_tags(response_data, requested_model)
        return response_data

    return response_data


async def attempt_model_fallback_rule(
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
    make_llm_request_hook: object,
    asyncio_hook: object,
    logging_hook: object,
    classify_error_hook: object,
    resolve_provider_auth_material_hook: object,
) -> tuple[object | None, str | None, int]:
    """Attempt a single provider+model, optionally retrying.

    ``total_attempts_budget`` enforces a *chain-wide* cap on the number of
    upstream calls: every attempt (initial or retry, regular or sub-provider)
    decrements the budget. When the budget is exhausted, the function returns
    immediately with the last error so the caller does not advance to the next
    fallback model. ``None`` disables the cap (legacy behavior).
    """
    make_llm_request = make_llm_request_hook
    asyncio = asyncio_hook
    logging = logging_hook
    classify_error = classify_error_hook
    resolve_provider_auth_material = resolve_provider_auth_material_hook

    provider_name = model_fallback_rule.get("provider")
    provider_model = model_fallback_rule.get("model")
    retry_count, retry_delay = normalize_retry_settings(
        model_fallback_rule.get("retry_count"),
        model_fallback_rule.get("retry_delay"),
    )
    subproviders_ordering = model_fallback_rule.get("providers_order")

    # Use a per-provider proxy client when configured, otherwise the shared client.
    if provider_name and provider_name in proxy_http_clients:
        http_client = proxy_http_clients[provider_name]

    logging.info(
        "Attempting %s '%s' for gateway model '%s' via provider '%s' and subproviders ordering: %s",
        attempt_label,
        provider_model,
        requested_model,
        provider_name,
        subproviders_ordering,
    )

    attempt_number = attempt_number_start
    x_title = extract_request_x_title(request)

    def _record_event(
        success: bool,
        error_detail_val: str | None,
        duration_ms: int,
        attempt_payload_for_error: dict | None = None,
        upstream_key_fingerprint: str | None = None,
    ):
        nonlocal attempt_number
        if fallback_events_db and request_id:
            try:
                fallback_events_db.insert_event(
                    request_id=request_id,
                    gateway_model=requested_model,
                    attempt_number=attempt_number,
                    provider=provider_name or "unknown",
                    model=provider_model or "unknown",
                    success=success,
                    error_type=None if success else classify_error(error_detail_val),
                    error_message=None if success else _build_fallback_error_message(
                        error_detail_val,
                        attempt_payload_for_error,
                        is_streaming=is_streaming,
                    ),
                    duration_ms=duration_ms,
                    operation=getattr(request.state, "llmgateway_operation", "chat"),
                    api_key_id=getattr(request.state, "api_key_id", None),
                    upstream_key_fingerprint=upstream_key_fingerprint,
                    x_title=x_title,
                )
            except Exception as exc:
                logging.debug("Failed to record fallback event: %s", exc)
        attempt_number += 1

    provider_config = providers_config.get(provider_name)
    if provider_config is None:
        logging.error(
            "Provider '%s' referenced by model '%s' is missing from providers configuration.",
            provider_name,
            requested_model,
        )
        _record_event(False, "Configured provider is unavailable for the requested model.", 0)
        return None, "Configured provider is unavailable for the requested model.", attempt_number

    provider_base_url = provider_config.baseUrl
    upstream_key_pool_name = model_fallback_rule.get("upstream_key_pool")
    try:
        key_candidates, key_pool_config, key_pool_name = _upstream_key_candidates_for_provider(
            provider_name,
            provider_config,
            upstream_key_pool_name,
        )
    except ValueError as exc:
        error_detail = f"Invalid upstream key routing configuration for provider '{provider_name}': {exc}"
        logging.error(error_detail)
        _record_event(False, error_detail, 0)
        return None, error_detail, attempt_number

    strategy = _provider_key_routing_strategy(provider_config, key_pool_config)
    affinity_enabled = _provider_key_routing_affinity_enabled(provider_config, key_pool_config)
    affinity_header = _provider_key_routing_affinity_header(provider_config, key_pool_config)
    affinity_ttl_seconds = _provider_key_routing_affinity_ttl(provider_config, key_pool_config)
    session_id = request.headers.get(affinity_header) if affinity_enabled else None
    affinity_scope = _affinity_scope_for_request(request) if affinity_enabled else None
    selected_key = upstream_routing_state.select_key_from_candidates(
        provider_name or "unknown",
        provider_model or "unknown",
        key_candidates,
        limits=upstream_limits_for_model(provider_config, provider_model or ""),
        strategy=strategy,
        session_id=session_id,
        affinity_scope=affinity_scope,
        session_affinity_ttl_seconds=affinity_ttl_seconds,
        pool_name=key_pool_name,
    )
    if not selected_key.available:
        error_detail = (
            f"No upstream key is currently available for provider '{provider_name}' "
            f"and model '{provider_model}': {selected_key.blocked_reason}."
        )
        logging.warning(error_detail)
        _record_event(False, error_detail, 0, upstream_key_fingerprint=selected_key.fingerprint)
        return None, error_detail, attempt_number
    provider_api_key = selected_key.api_key
    upstream_key_fingerprint = selected_key.fingerprint
    auth_material = await resolve_provider_auth_material(
        request,
        provider_name=provider_name or "unknown",
        provider_config=provider_config,
        api_key=provider_api_key,
    )
    if auth_material.upstream_key_fingerprint:
        upstream_key_fingerprint = auth_material.upstream_key_fingerprint
    is_anthropic_provider = getattr(provider_config, "type", "openai") == "anthropic"
    client_expects_anthropic = isinstance(
        getattr(request.state, "llmgateway_original_anthropic_payload", None),
        dict,
    )
    client_expects_responses = isinstance(
        getattr(request.state, "llmgateway_original_responses_payload", None),
        dict,
    )
    anthropic_tool_name_reverse_map: dict[str, str] = {}

    def _observe_success(response_data: object) -> object:
        if chat_accounting_handoff is None:
            return response_data
        if (
            not isinstance(provider_name, str)
            or not isinstance(provider_model, str)
            or not isinstance(cost_rate_registry, Mapping)
        ):
            raise AccountingValidationError
        if is_streaming:
            builder = _DirectChatStreamObservationBuilder(
                dialect=(
                    ChatStreamDialect.RESPONSES
                    if client_expects_responses
                    else (
                        ChatStreamDialect.ANTHROPIC
                        if client_expects_anthropic
                        else ChatStreamDialect.OPENAI
                    )
                ),
                provider=provider_name,
                model=provider_model,
                cost_rate_registry=cost_rate_registry,
                estimated_prompt_tokens=estimate_prompt_tokens(
                    json.dumps(request_body_json),
                    provider_model,
                ),
            )
            chat_accounting_handoff.publish_stream_observer(
                builder.observe,
                build_partial=builder.build_partial,
            )
            return response_data
        if not isinstance(response_data, Mapping):
            raise AccountingValidationError
        chat_accounting_handoff.publish(
            build_direct_chat_terminal_observation(
                response_data,
                provider=provider_name,
                model=provider_model,
                cost_rate_registry=cost_rate_registry,
            )
        )
        return response_data

    if is_anthropic_provider:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_API_VERSION,
            **auth_material.headers,
        }
        target_url = f"{provider_base_url.rstrip('/')}/v1/messages"
        original_anthropic_payload = getattr(
            request.state, "llmgateway_original_anthropic_payload", None
        )
        if isinstance(original_anthropic_payload, dict):
            # Anthropic client → Anthropic provider: preserve every native field
            # (system, cache_control, thinking, ...) that would be lost if we
            # round-tripped through the OpenAI shape.
            provider_payload_template = copy.deepcopy(original_anthropic_payload)
            if "max_tokens" not in provider_payload_template:
                provider_payload_template["max_tokens"] = ANTHROPIC_DEFAULT_MAX_TOKENS
        else:
            openai_to_anthropic_tool_name_map: dict[str, str] = {}
            provider_payload_template = _openai_request_to_anthropic_payload(
                request_body_json,
                tool_name_map=openai_to_anthropic_tool_name_map,
            )
            anthropic_tool_name_reverse_map = {
                anthropic_name: openai_name
                for openai_name, anthropic_name in openai_to_anthropic_tool_name_map.items()
            }
        provider_payload_template["model"] = provider_model
        # Anthropic does not accept OpenAI-only flags.
        provider_payload_template.pop("stream_options", None)
        if is_streaming:
            provider_payload_template["stream"] = True
        else:
            provider_payload_template.pop("stream", None)
    else:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
            "X-Title": "LLMGateway",
            **auth_material.headers,
        }
        target_url = f"{provider_base_url.rstrip('/')}/chat/completions"
        provider_payload_template = copy.deepcopy(request_body_json)
        provider_payload_template["model"] = provider_model
        if provider_name == "openrouter" and "usage" not in provider_payload_template:
            provider_payload_template["usage"] = {"include": True}

    custom_body_params = model_fallback_rule.get("custom_body_params", {})
    if custom_body_params:
        for key, value in custom_body_params.items():
            provider_payload_template[key] = value

    provider_payload_template = apply_payload_transforms(
        provider_payload_template,
        model_fallback_rule.get("payload_transforms"),
    )

    if is_streaming and not is_anthropic_provider:
        # Force the upstream to emit a final usage chunk for every
        # OpenAI-compatible streaming call, regardless of what the client
        # requested, so the accounting engine always sees real usage instead
        # of having to release the reservation unbilled. Applied last (after
        # custom_body_params and payload_transforms, both of which can
        # otherwise overwrite the whole "stream_options" key) and merged so
        # any other stream_options entries configured through those
        # mechanisms are preserved.
        stream_options = provider_payload_template.get("stream_options")
        if isinstance(stream_options, dict):
            # custom_body_params are copied in by reference, so this dict can
            # still be the one owned by the loaded rule: mutating it in place
            # would leak the forced flag back into the shared config.
            stream_options = dict(stream_options)
        else:
            stream_options = {}
        stream_options["include_usage"] = True
        provider_payload_template["stream_options"] = stream_options

    custom_headers = model_fallback_rule.get("custom_headers", {})
    if custom_headers:
        for key, value in custom_headers.items():
            # Defense-in-depth: loader already blocks these via FallbackModelRule
            # validation, but a hot-reload or in-memory mutation must never end up
            # overriding Authorization / Cookie / X-Api-Key at the provider call.
            if key.lower() in SECURITY_HEADER_NAMES:
                logging.warning(
                    "Blocked protected header '%s' from custom_headers for rule '%s' (provider=%s).",
                    key,
                    requested_model,
                    provider_name,
                )
                continue
            headers[key] = value

    if not is_anthropic_provider:
        _normalize_provider_attempt_payload(provider_payload_template, provider_model=provider_model)

    last_error_detail = f"Model '{provider_model}' failed without an explicit provider error."
    stream_request_kwargs = (
        {
            "stream_observation_capacity": stream_observation_capacity,
            "stream_event_max_bytes": stream_event_max_bytes,
            "stream_request_id": request_id,
        }
        if is_streaming
        else {}
    )

    def _budget_exhausted() -> bool:
        return total_attempts_budget is not None and attempt_number > total_attempts_budget

    def _track_attempt_start() -> None:
        request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
        attempts = getattr(request.state, "llmgateway_fallback_attempts", None)
        if not isinstance(attempts, list):
            attempts = []
            request.state.llmgateway_fallback_attempts = attempts
        attempts.append(
            {
                "provider": provider_name,
                "model": provider_model,
                "upstream_key_fingerprint": upstream_key_fingerprint,
            }
        )
        upstream_routing_state.record_attempt_start(
            provider_name or "unknown",
            provider_model or "unknown",
            upstream_key_fingerprint,
        )

    def _track_attempt_result(success: bool, error_detail_val: object | None) -> None:
        if success:
            upstream_routing_state.record_success(
                provider_name or "unknown",
                provider_model or "unknown",
                upstream_key_fingerprint,
            )
            return
        temporary = _is_temporary_model_failure(error_detail_val)
        upstream_routing_state.record_failure(
            provider_name or "unknown",
            provider_model or "unknown",
            upstream_key_fingerprint,
            error_detail_val,
            temporary=temporary,
            apply_penalty=bool(model_fallback_rule.get("dynamic_penalty_enabled")),
            retry_after=_extract_retry_after(error_detail_val),
        )

    remaining_attempts = retry_count
    while remaining_attempts >= 0:
        if _budget_exhausted():
            logging.warning(
                "Chain-wide attempt budget (%s) exhausted before trying %s for model '%s'.",
                total_attempts_budget,
                provider_model,
                requested_model,
            )
            break

        # Sub-provider ordering is an OpenRouter-style hint that has no meaning
        # for native Anthropic providers; force the plain path for them.
        if (
            not subproviders_ordering
            or not model_fallback_rule.get("use_provider_order_as_fallback")
            or is_anthropic_provider
        ):
            if subproviders_ordering and not is_anthropic_provider:
                logging.info(
                    "Attempting model '%s' in provider '%s' with provider order hint %s",
                    provider_model,
                    provider_name,
                    subproviders_ordering,
                )
            else:
                logging.info("Attempting model '%s' in provider '%s'", provider_model, provider_name)

            attempt_payload = copy.deepcopy(provider_payload_template)
            if subproviders_ordering and not is_anthropic_provider:
                attempt_payload["provider"] = {"order": subproviders_ordering}
                attempt_payload["allow_fallbacks"] = False

            update_active_request(
                request,
                gateway_model=requested_model,
                operation=getattr(request.state, "llmgateway_operation", "chat"),
                provider=provider_name,
                model=provider_model,
                upstream_key_fingerprint=upstream_key_fingerprint,
            )
            _track_attempt_start()
            t0 = time.monotonic()
            response_data, error_detail = await make_llm_request(
                http_client,
                target_url,
                headers,
                attempt_payload,
                is_streaming,
                **stream_request_kwargs,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)

            if response_data and error_detail is None:
                response_data = _observe_success(response_data)
                if is_anthropic_provider:
                    if client_expects_anthropic:
                        # Native end-to-end Anthropic: skip the lossy OpenAI
                        # round-trip. ``anthropic_messages`` reads this flag
                        # and forwards the payload/stream as-is.
                        request.state.llmgateway_response_is_anthropic_raw = True
                    elif is_streaming:
                        response_data = _anthropic_stream_to_openai(
                            request,
                            response_data,
                            requested_model,
                            tool_name_reverse_map=anthropic_tool_name_reverse_map,
                        )
                    else:
                        response_data = _anthropic_response_to_openai(
                            response_data,
                            requested_model,
                            tool_name_reverse_map=anthropic_tool_name_reverse_map,
                        )
                request.state.llmgateway_provider = provider_name
                request.state.llmgateway_provider_model = provider_model
                request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
                _track_attempt_result(True, None)
                _record_event(True, None, duration_ms, upstream_key_fingerprint=upstream_key_fingerprint)
                logging.info(
                    "Connection success to model '%s' in provider '%s'. %s response...",
                    provider_model,
                    provider_name,
                    "Starting streaming" if is_streaming else "Waiting",
                )
                return response_data, None, attempt_number

            _record_event(False, error_detail, duration_ms, attempt_payload, upstream_key_fingerprint)
            _track_attempt_result(False, error_detail)
            _log_failed_attempt_warning(
                provider_model,
                provider_name,
                error_detail,
                target_url,
                attempt_payload,
            )
            last_error_detail = f"Model {provider_model} failed with provider '{provider_name}': {error_detail}"
            if stop_after_context_overflow and _is_context_overflow_error(error_detail):
                logging.warning(
                    "Context overflow detected on model '%s' before retry; returning to dispatcher.",
                    provider_model,
                )
                return None, last_error_detail, attempt_number
            logging.debug(
                "Continuing after failed attempt for model '%s' in provider '%s'.",
                provider_model,
                provider_name,
            )
        else:
            logging.info(
                "Provider '%s' uses sub-provider ordering. Target model: %s. Order: %s",
                provider_name,
                provider_model,
                subproviders_ordering,
            )

            for sub_provider in subproviders_ordering:
                if _budget_exhausted():
                    logging.warning(
                        "Chain-wide attempt budget (%s) exhausted before sub-provider '%s' for model '%s'.",
                        total_attempts_budget,
                        sub_provider,
                        requested_model,
                    )
                    break

                logging.info(
                    "Attempting model '%s' on sub-provider '%s' in provider '%s'",
                    provider_model,
                    sub_provider,
                    provider_name,
                )
                attempt_payload = copy.deepcopy(provider_payload_template)
                attempt_payload["provider"] = {"order": [sub_provider]}
                attempt_payload["allow_fallbacks"] = False

                update_active_request(
                    request,
                    gateway_model=requested_model,
                    operation=getattr(request.state, "llmgateway_operation", "chat"),
                    provider=provider_name,
                    model=provider_model,
                    upstream_key_fingerprint=upstream_key_fingerprint,
                )
                _track_attempt_start()
                t0 = time.monotonic()
                response_data, error_detail = await make_llm_request(
                    http_client,
                    target_url,
                    headers,
                    attempt_payload,
                    is_streaming,
                    **stream_request_kwargs,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                if response_data and error_detail is None:
                    response_data = _observe_success(response_data)
                    request.state.llmgateway_provider = provider_name
                    request.state.llmgateway_provider_model = provider_model
                    request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
                    _track_attempt_result(True, None)
                    _record_event(True, None, duration_ms, upstream_key_fingerprint=upstream_key_fingerprint)
                    logging.info(
                        "Connection success with model '%s' in provider '%s' via '%s'. %s response...",
                        provider_model,
                        provider_name,
                        sub_provider,
                        "Starting streaming" if is_streaming else "Received",
                    )
                    return response_data, None, attempt_number

                _record_event(False, error_detail, duration_ms, attempt_payload, upstream_key_fingerprint)
                _track_attempt_result(False, error_detail)
                _log_failed_attempt_warning(
                    provider_model,
                    provider_name,
                    error_detail,
                    target_url,
                    attempt_payload,
                    sub_provider=sub_provider,
                )
                last_error_detail = (
                    f"Model '{provider_model}' failed from provider '{provider_name}' "
                    f"and sub-provider {sub_provider} : {error_detail}"
                )
                if stop_after_context_overflow and _is_context_overflow_error(error_detail):
                    logging.warning(
                        "Context overflow detected on model '%s' sub-provider '%s' before retry; returning to dispatcher.",
                        provider_model,
                        sub_provider,
                    )
                    return None, last_error_detail, attempt_number

            logging.warning("All sub-providers for '%s' failed.", provider_name)

        if remaining_attempts > 0 and not _budget_exhausted():
            sleep_seconds = _compute_retry_sleep_seconds(retry_delay, error_detail)
            if sleep_seconds > 0:
                logging.info(
                    "RETRYING %s in %.2f seconds (source=%s)... %s attempts left.",
                    provider_model,
                    sleep_seconds,
                    "retry-after" if _extract_retry_after(error_detail) is not None else "retry_delay+jitter",
                    remaining_attempts - 1,
                )
                await asyncio.sleep(sleep_seconds)
        remaining_attempts -= 1

    return None, last_error_detail, attempt_number


def _extract_retry_after(error_detail: object) -> float | None:
    """Return the ``retry_after`` metadata from a ``RequestErrorDetail`` error, if any."""
    retry_after = getattr(error_detail, "retry_after", None)
    if retry_after is None:
        return None
    try:
        retry_after_seconds = float(retry_after)
    except (TypeError, ValueError):
        return None
    if retry_after_seconds < 0:
        return None
    return retry_after_seconds


def _compute_retry_sleep_seconds(retry_delay: float, error_detail: object) -> float:
    """Resolve the effective sleep time before the next retry attempt.

    Preference order:
      1. Upstream ``Retry-After`` hint (clamped to :data:`MAX_RETRY_AFTER_SECONDS`);
         honored verbatim — no jitter — so we cooperate with provider-side rate limits.
      2. Configured ``retry_delay`` with +/-25% jitter to avoid thundering-herd
         retries when many requests race the same outage.
      3. ``0`` to indicate no sleep.
    """
    retry_after = _extract_retry_after(error_detail)
    if retry_after is not None and retry_after > 0:
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)

    if retry_delay > 0:
        jittered = retry_delay * random.uniform(0.75, 1.25)
        return max(0.0, min(jittered, MAX_RETRY_AFTER_SECONDS))

    return 0.0


def _local_stream_observation_http_error(
    error: LocalStreamObservationError,
) -> HTTPException:
    logging.error(
        "Local stream observation unavailable reason=%s",
        error.reason_code,
    )
    return HTTPException(
        status_code=503,
        detail="Local stream observation is unavailable.",
    )


async def dispatch_chat_request(
    request: Request,
    request_body_json: dict,
    *,
    enforce_model_access: bool = True,
    accounting_handoff: ChatTerminalHandoff | None = None,
    attempt_model_fallback_rule_hook: object,
    logging_hook: object,
):
    _attempt_model_fallback_rule = attempt_model_fallback_rule_hook
    logging = logging_hook
    services = cast("AppServices", request.app.state.services)
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    config_loader_instance = runtime_snapshot.config_loader
    http_client = services.http_client

    providers_config = config_loader_instance.providers_config
    fallback_rules = config_loader_instance.fallback_rules

    # Reuse the middleware request_id so active and completed usage rows correlate.
    request_id = (
        getattr(request.state, "llmgateway_request_id", None)
        or getattr(request.state, "llmgateway_active_request_id", None)
        or str(uuid4())
    )
    request.state.llmgateway_request_id = request_id

    fallback_events_db = services.fallback_events_db

    requested_model = request_body_json.get("model")
    is_streaming = request_body_json.get("stream", False)

    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    if enforce_model_access:
        enforce_virtual_key_access(request, requested_model)

    try:
        model_resolution = resolve_model_name(
            requested_model,
            getattr(config_loader_instance, "model_rules", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    routing_model = model_resolution.effective_model
    if model_resolution.changed:
        logging.info(
            "Model policy resolved requested model '%s' to routing model '%s' via %s.",
            requested_model,
            routing_model,
            model_resolution.matched_rule,
        )

    payload_to_log = redact_payload_for_log(request_body_json)
    logging.debug(
        "/v1/chat/completions: Request for model '%s'. Payload: %s",
        payload_to_log.get("model", requested_model),
        payload_to_log,
    )

    # Only set if not already set by a specific controller (like Anthropic)
    if not getattr(request.state, "llmgateway_gateway_model", None):
        request.state.llmgateway_gateway_model = requested_model

    # Always ensure operation is set
    if not getattr(request.state, "llmgateway_operation", None):
        request.state.llmgateway_operation = "chat"
    update_active_request(
        request,
        gateway_model=requested_model,
        operation=getattr(request.state, "llmgateway_operation", "chat"),
    )

    fusion_rules = getattr(config_loader_instance, "fusion_rules", None)
    fusion_config = fusion_rules.get(routing_model) if isinstance(fusion_rules, dict) else None
    if fusion_config is not None:
        if request.headers.get("x-llmgateway-fusion"):
            raise HTTPException(status_code=400, detail="Nested Fusion calls are not allowed.")
        if is_streaming:
            raise HTTPException(status_code=400, detail="Fusion models do not support streaming responses.")
        fusion_service = runtime_snapshot.fusion_service
        proxy_http_clients = runtime_snapshot.proxy_http_clients
        if accounting_handoff is not None:
            observed = await fusion_service.run_observed(
                request=request,
                gateway_model_name=routing_model,
                fusion_config=fusion_config,
                request_body=request_body_json,
                http_client=http_client,
                proxy_http_clients=proxy_http_clients,
            )
            if not isinstance(observed, ObservedChatResponse):
                raise AccountingValidationError
            accounting_handoff.publish(observed.observation)
            return observed.response
        return await fusion_service.run(
            request=request,
            gateway_model_name=routing_model,
            fusion_config=fusion_config,
            request_body=request_body_json,
            http_client=http_client,
            proxy_http_clients=proxy_http_clients,
        )

    router_rules = getattr(config_loader_instance, "router_rules", None)
    router_config = router_rules.get(routing_model) if isinstance(router_rules, dict) else None
    if router_config is not None:
        router_service = runtime_snapshot.router_model_service
        if accounting_handoff is not None:
            observed = await router_service.run_observed(
                request=request,
                gateway_model_name=routing_model,
                router_config=router_config,
                request_body=request_body_json,
            )
            if not isinstance(observed, ObservedChatResponse):
                raise AccountingValidationError
            accounting_handoff.publish(observed.observation)
            return observed.response
        return await router_service.run(
            request=request,
            gateway_model_name=routing_model,
            router_config=router_config,
            request_body=request_body_json,
        )

    model_config = fallback_rules.get(routing_model)
    context_overflow_fallback = None
    strip_think_tags = False
    max_total_attempts: int | None = None
    dynamic_penalty_enabled = False
    if not model_config:
        if model_resolution.changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model '{requested_model}' resolved to '{routing_model}', "
                    "but no routing rule is configured for the resolved model."
                ),
            )
        logging.warning(f"No specific fallback sequence found for model '{requested_model}'. Using '{settings.fallback_provider}' fallback provider.")

        model_fallbacks_sequence = [{"provider": settings.fallback_provider, "model": requested_model}]
        rotate_models = False
        logging.info(f"Using fallback provider: {settings.fallback_provider}")
    else:
        model_fallbacks_sequence = model_config["fallback_models"]
        rotate_models = model_config["rotate_models"]
        context_overflow_fallback = model_config.get("context_overflow_fallback")
        strip_think_tags = bool(model_config.get("strip_think_tags", False))
        max_total_attempts = model_config.get("max_total_attempts")
        dynamic_penalty_enabled = bool(model_config.get("dynamic_penalty", False))
        logging.info(f"Found routing rule for model '{requested_model}'. Provider sequence length: {len(model_fallbacks_sequence)}")
        logging.info(f"Model rotation is {'enabled' if rotate_models else 'disabled'} for model '{requested_model}'")
        if context_overflow_fallback:
            logging.info("Special context overflow fallback is configured for model '%s'.", requested_model)
        if strip_think_tags:
            logging.info("Literal <think> tag stripping is enabled for gateway model '%s'.", requested_model)
        if dynamic_penalty_enabled:
            logging.info("Dynamic upstream penalty ordering is enabled for gateway model '%s'.", requested_model)

    forced_owner_model = getattr(request.state, "llmgateway_forced_fallback_owner_model", None)
    forced_start_index = getattr(request.state, "llmgateway_forced_fallback_start_index", None)
    if model_config and forced_owner_model == routing_model and isinstance(forced_start_index, int):
        if forced_start_index < 0 or forced_start_index >= len(model_fallbacks_sequence):
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Invalid forced fallback start index {forced_start_index} "
                    f"for model '{routing_model}'."
                ),
            )
        model_fallbacks_sequence = model_fallbacks_sequence[forced_start_index:]
        rotate_models = False
        logging.info(
            "Router selected fallback entry %s for model '%s'; starting chain from that entry.",
            forced_start_index,
            routing_model,
        )

    compress_tool_results = bool(model_config.get("compress_tool_results", False)) if model_config else False
    if compress_tool_results:
        from llm_gateway_core.services.token_compression import compress_messages
        _rtk_stats = compress_messages(request_body_json, enabled=True)
        if _rtk_stats is not None:
            request.state.llmgateway_compression_stats = _rtk_stats

    if dynamic_penalty_enabled:
        model_fallbacks_sequence = services.upstream_routing_state.order_rules_by_penalty(
            list(model_fallbacks_sequence),
            providers_config,
        )

    model_fallbacks_sequence = [
        {**model_fallback_rule, "dynamic_penalty_enabled": dynamic_penalty_enabled}
        for model_fallback_rule in model_fallbacks_sequence
    ]
    if context_overflow_fallback:
        context_overflow_fallback = {
            **context_overflow_fallback,
            "dynamic_penalty_enabled": dynamic_penalty_enabled,
        }

    rotation_scope = _rotation_scope_for_request(request)

    start_index = 0
    if rotate_models and len(model_fallbacks_sequence) > 1:
        start_index = await _require_model_rotation_db(request).get_next_model_index(
            api_key=rotation_scope,
            gateway_model=requested_model,
            total_models=len(model_fallbacks_sequence),
        )
        logging.info(f"Model rotation: Starting with model index {start_index} for '{requested_model}'")

    if rotate_models and len(model_fallbacks_sequence) > 1:
        # Guard against hot-reload racing with rotation: the sequence captured here is a
        # local snapshot, but `get_next_model_index` uses its own `total_models`. If the
        # on-disk config was reloaded between both calls, `start_index` might exceed
        # `len(model_fallbacks_sequence)` and produce an empty reorder (users would see
        # "No providers were attempted"). Normalize against the local snapshot length.
        sequence_length = len(model_fallbacks_sequence)
        normalized_start_index = start_index % sequence_length
        if normalized_start_index != start_index:
            logging.warning(
                "Model rotation: start_index %s exceeds local sequence length %s (likely hot-reload race); "
                "normalizing to %s for '%s'.",
                start_index,
                sequence_length,
                normalized_start_index,
                requested_model,
            )
        start_index = normalized_start_index
        reordered_sequence = model_fallbacks_sequence[start_index:] + model_fallbacks_sequence[:start_index]
        model_fallbacks_sequence = reordered_sequence

    last_error_detail = "No providers were attempted."
    context_overflow_fallback_attempted = False
    attempt_number = 1
    for model_fallback_rule in model_fallbacks_sequence:
        if max_total_attempts is not None and attempt_number > max_total_attempts:
            logging.warning(
                "Chain-wide attempt budget (%s) exhausted for model '%s'; "
                "skipping remaining fallback models.",
                max_total_attempts,
                requested_model,
            )
            break

        try:
            response_data, error_detail, attempt_number = await _attempt_model_fallback_rule(
                request,
                http_client,
                providers_config,
                requested_model,
                request_body_json,
                model_fallback_rule,
                is_streaming,
                fallback_events_db=fallback_events_db,
                request_id=request_id,
                attempt_number_start=attempt_number,
                total_attempts_budget=max_total_attempts,
                stop_after_context_overflow=bool(
                    context_overflow_fallback and not context_overflow_fallback_attempted
                ),
                proxy_http_clients=runtime_snapshot.proxy_http_clients,
                upstream_routing_state=services.upstream_routing_state,
                chat_accounting_handoff=accounting_handoff,
                cost_rate_registry=runtime_snapshot.cost_rate_registry,
                stream_observation_capacity=services.stream_observation_capacity,
                stream_event_max_bytes=services.stream_event_max_bytes,
            )
        except LocalStreamObservationError as exc:
            raise _local_stream_observation_http_error(exc) from None

        if response_data and error_detail is None:
            return _finalize_chat_success_response(
                response_data,
                requested_model,
                request_body_json,
                strip_think_tags=strip_think_tags,
                is_anthropic_raw=bool(
                    getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
                ),
            )

        last_error_detail = error_detail or last_error_detail

        if (
            context_overflow_fallback
            and not context_overflow_fallback_attempted
            and _is_context_overflow_error(last_error_detail)
        ):
            context_overflow_fallback_attempted = True
            logging.warning(
                "Context overflow detected for model '%s'. Switching current request to the dedicated context overflow fallback.",
                requested_model,
            )
            try:
                context_response, context_error_detail, attempt_number = await _attempt_model_fallback_rule(
                    request,
                    http_client,
                    providers_config,
                    requested_model,
                    request_body_json,
                    context_overflow_fallback,
                    is_streaming,
                    attempt_label="context overflow fallback model",
                    fallback_events_db=fallback_events_db,
                    request_id=request_id,
                    attempt_number_start=attempt_number,
                    total_attempts_budget=max_total_attempts,
                    proxy_http_clients=runtime_snapshot.proxy_http_clients,
                    upstream_routing_state=services.upstream_routing_state,
                    chat_accounting_handoff=accounting_handoff,
                    cost_rate_registry=runtime_snapshot.cost_rate_registry,
                    stream_observation_capacity=services.stream_observation_capacity,
                    stream_event_max_bytes=services.stream_event_max_bytes,
                )
            except LocalStreamObservationError as exc:
                raise _local_stream_observation_http_error(exc) from None
            if context_response and context_error_detail is None:
                return _finalize_chat_success_response(
                    context_response,
                    requested_model,
                    request_body_json,
                    strip_think_tags=strip_think_tags,
                    is_anthropic_raw=bool(
                        getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
                    ),
                )

            if context_error_detail:
                last_error_detail = context_error_detail

    logging.error(f"All providers failed for model '{requested_model}'. Last error: {last_error_detail}")
    raise HTTPException(status_code=503, detail=f"All configured providers failed for model '{requested_model}'. Last error: {last_error_detail}")
