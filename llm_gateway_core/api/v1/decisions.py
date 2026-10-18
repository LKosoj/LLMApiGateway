"""Typed decisions forwarded to a configured provider."""

import logging
import time
from collections.abc import Mapping
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Request

from ..accounting_http import AccountingHttpUse, accounting_error_response
from ...services.access_control import enforce_virtual_key_access
from ...services.accounting import AccountingCostError, AccountingError, AccountingErrorCode
from ...services.operation_accounting import parse_upstream_operation_observation
from ...services.provider_auth import resolve_provider_auth_headers
from ...services.request_handler import normalize_retry_settings
from .operation_accounting import (
    finalize_buffered_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from .operation_proxy import (
    json_response,
    proxy_json_to_downstream,
    read_json_request_body,
    request_duration_ms,
)

logger = logging.getLogger(__name__)
router = APIRouter()


def validate_decisions_request(payload: dict) -> str:
    model = payload.get("model")
    if not isinstance(model, str) or not model.strip():
        raise HTTPException(400, "Missing 'model' in request body.")
    if not isinstance(payload.get("state"), (str, dict, list)):
        raise HTTPException(400, "'state' must be a string, object or array.")
    questions = payload.get("questions")
    if not isinstance(questions, dict) or not questions:
        raise HTTPException(400, "'questions' must be a non-empty object.")
    for name, question in questions.items():
        if not isinstance(question, dict):
            raise HTTPException(400, f"Question '{name}' must be an object.")
        question_type = question.get("type")
        if question_type not in ("noul", "choice", "score"):
            raise HTTPException(400, f"Question '{name}' has an unsupported type.")
        if not isinstance(question.get("instructions"), (str, dict, list)):
            raise HTTPException(400, f"Question '{name}' requires instructions as text or JSON.")
        criteria = question.get("criteria")
        if question_type == "choice" and (not isinstance(criteria, dict) or not criteria):
            raise HTTPException(400, f"Question '{name}' requires a non-empty criteria object.")
        if question_type == "score" and (not isinstance(criteria, list) or not criteria):
            raise HTTPException(400, f"Question '{name}' requires a non-empty criteria array.")
        if question_type == "noul" and "criteria" in question:
            if not isinstance(criteria, dict) or set(criteria) != {"true", "false"}:
                raise HTTPException(400, f"Question '{name}' criteria must contain 'true' and 'false'.")
    if payload.get("stream"):
        raise HTTPException(400, "Decisions do not support streaming.")
    return model


async def call_decision_routes(request: Request, requested_model: str, payload: dict, routes, *,
                               cost_rate_registry, observe):
    """Try decisions routes in order; ``observe(json, route)`` builds the accounting observation."""
    snapshot = request.state.runtime_snapshot
    services = request.app.state.services
    dispatcher = snapshot.operation_dispatcher
    for route_index, route in enumerate(routes):
        try:
            provider_config = snapshot.config_loader.providers_config.get(route.provider)
            if provider_config is None:
                raise HTTPException(500, "Provider configuration not available.")
            auth_headers = await resolve_provider_auth_headers(
                request, provider_name=route.provider, provider_config=provider_config,
            )
            downstream_payload = dispatcher.build_payload(payload, route, "decisions")
            retry_count, retry_delay = normalize_retry_settings(route.retry_count, route.retry_delay)
            request.state.llmgateway_gateway_model = requested_model
            dispatcher.set_request_state(
                request=request, operation="decisions", route=route,
                provider_name=route.provider, provider_model=route.model,
            )
            downstream_json, status_code = await proxy_json_to_downstream(
                dispatcher.build_target_url(route, provider_config),
                dispatcher.build_headers(route, auth_headers=auth_headers),
                downstream_payload,
                snapshot.proxy_http_clients.get(route.provider, services.http_client),
                retry_count=retry_count, retry_delay=retry_delay,
            )
            if not isinstance(downstream_json, dict) or not isinstance(downstream_json.get("answers"), dict):
                raise HTTPException(502, "Decisions response must contain an 'answers' object.")
            usage = downstream_json.get("usage")
            if (
                isinstance(usage, Mapping) and usage.get("cost") is None
                and (route.provider, route.model) not in cost_rate_registry
            ):
                # RUB is preserved in the response; it cannot be recorded as USD.
                raise AccountingCostError(AccountingErrorCode.COST_UNAVAILABLE)
            observation = observe(downstream_json, route)
        except (HTTPException, AccountingError) as exc:
            retryable = isinstance(exc, AccountingError) or exc.status_code in {502, 503}
            if not retryable or route_index == len(routes) - 1:
                raise
            logger.warning(
                "Decisions route failed for gateway model '%s'; trying next route. "
                "provider=%s model=%s reason=%s",
                requested_model, route.provider, route.model,
                exc.code.value if isinstance(exc, AccountingError) else exc.status_code,
            )
            continue
        return downstream_json, status_code, observation
    raise HTTPException(404, f"No decisions route configured for model '{requested_model}'.")


@router.post("/decisions")
async def create_decisions(request: Request):
    payload = await read_json_request_body(request, "decisions endpoint")
    requested_model = validate_decisions_request(payload)
    enforce_virtual_key_access(request, requested_model)
    routes = request.state.runtime_snapshot.operation_dispatcher.lookup_routes("decisions", requested_model)
    if not routes:
        raise HTTPException(404, f"No decisions route configured for model '{requested_model}'.")

    try:
        owner = take_operation_terminal_owner(request)
    except AccountingError as exc:
        return accounting_error_response(request, exc, use=AccountingHttpUse.ADMISSION)

    started_at = time.monotonic()

    def observe(downstream_json: dict, route):
        return parse_upstream_operation_observation(
            downstream_json, policy=owner.context.policy,
            gateway_model=requested_model, provider=route.provider, model=route.model,
            cost_rate_registry=owner.cost_rate_registry,
            operation_cost_calculator_registry=owner.operation_cost_calculator_registry,
            occurred_at=datetime.now(timezone.utc),
            duration_ms=request_duration_ms(started_at),
        )

    try:
        downstream_json, status_code, observation = await call_decision_routes(
            request, requested_model, payload, routes,
            cost_rate_registry=owner.cost_rate_registry, observe=observe,
        )
        return await finalize_buffered_operation(
            owner, json_response(downstream_json, status_code), observation,
        )
    except AccountingError as exc:
        await release_operation_if_open(owner, primary_error=exc)
        return accounting_error_response(request, exc, use=AccountingHttpUse.ADMISSION)
    except BaseException as exc:
        await release_operation_if_open(owner, primary_error=exc)
        raise
