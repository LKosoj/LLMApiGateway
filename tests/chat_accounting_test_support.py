from __future__ import annotations

import json
import secrets
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import create_autospec, patch

from llm_gateway_core.api.v1 import chat
from llm_gateway_core.middleware.accounting_admission import (
    take_accounting_request_context,
)
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    TerminalReason,
)
from llm_gateway_core.services.accounting import AccountingReservation
from llm_gateway_core.services.accounting_service import AccountingService


def _install_response_observation_passthrough(stack: ExitStack) -> None:
    handoff = object()

    def take_owner(request):
        context = take_accounting_request_context(request.scope)
        if context is None:
            return object()
        return SimpleNamespace(reservation=context.reservation)

    def build_observation(
        _owner,
        _handoff,
        services,
        *,
        bind_request,
        observe_body_chunk=None,
        transport_finished=None,
    ):
        stream_error_seen = False

        async def finalize(_signal):
            reservation = getattr(_owner, "reservation", None)
            if reservation is not None:
                await services.accounting_service.release(reservation)
            return False

        def classify_json(start, _body):
            bind_request()
            if start.status_code >= 400:
                return TerminalReason.UPSTREAM_ERROR
            return TerminalReason.COMPLETE

        def classify_sse(start, _event):
            nonlocal stream_error_seen
            bind_request()
            if start.status_code >= 400:
                return TerminalReason.UPSTREAM_ERROR
            if stream_error_seen:
                return (
                    TerminalReason.UPSTREAM_ERROR
                    if _event.done
                    else TerminalReason.PROTOCOL_ERROR
                )
            if _event.done:
                return TerminalReason.COMPLETE
            try:
                payload = json.loads(_event.data)
            except (TypeError, ValueError):
                return TerminalReason.PROTOCOL_ERROR
            if isinstance(payload, dict):
                payload_type = payload.get("type")
                if payload_type == "error" and _event.event in {None, "error"}:
                    return TerminalReason.UPSTREAM_ERROR
                if "error" in payload:
                    return TerminalReason.UPSTREAM_ERROR
                if payload_type == "response.failed":
                    stream_error_seen = True
                    return None
            if isinstance(payload, dict) and payload.get("type") == "message_stop":
                return TerminalReason.COMPLETE
            return None

        def classify_opaque(start):
            bind_request()
            if start.status_code >= 400:
                return TerminalReason.UPSTREAM_ERROR
            return TerminalReason.COMPLETE

        return ResponseObservation(
            finalizer=ResponseFinalizer(services.task_supervisor, finalize),
            classify_json=classify_json,
            classify_sse=classify_sse,
            classify_opaque=classify_opaque,
            observe_body_chunk=observe_body_chunk,
            transport_finished=transport_finished,
        )

    async def release(_owner, *, primary_error=None) -> None:
        return None

    stack.enter_context(
        patch.object(
            chat_logging,
            "take_chat_terminal_owner",
            side_effect=take_owner,
        )
    )
    stack.enter_context(
        patch.object(chat_logging, "bind_chat_request", return_value=None)
    )
    stack.enter_context(
        patch.object(
            chat_logging,
            "install_chat_terminal_handoff",
            return_value=handoff,
        )
    )
    stack.enter_context(
        patch.object(
            chat_logging,
            "build_chat_response_observation",
            side_effect=build_observation,
        )
    )
    stack.enter_context(
        patch.object(chat_logging, "release_chat", side_effect=release)
    )


def install_main_chat_accounting_double(stack: ExitStack) -> AccountingService:
    """Install the typed accounting boundary required by legacy main.app tests."""
    service = create_autospec(AccountingService, instance=True, spec_set=True)

    async def reserve(**kwargs: object) -> AccountingReservation:
        return AccountingReservation(
            reservation_id=secrets.token_hex(16),
            request_id=str(kwargs["request_id"]),
            api_key_id=kwargs.get("api_key_id"),  # type: ignore[arg-type]
            reserved_usd=float(kwargs["estimate_usd"]),
        )

    service.reserve.side_effect = reserve
    service.release.return_value = True
    service.reset_due_budgets.return_value = ()
    stack.enter_context(patch("main.AccountingService", return_value=service))

    _install_response_observation_passthrough(stack)
    stack.enter_context(
        patch.object(chat, "get_chat_terminal_handoff", return_value=None)
    )
    return service


def install_legacy_chat_logging_passthrough(stack: ExitStack) -> None:
    """Exercise standalone chat observability with a no-I/O terminal boundary."""
    def effective_route(scope, _routes):
        return SimpleNamespace(
            method=scope.get("method", "POST"),
            route_template=scope.get("path", "/v1/chat/completions"),
        )

    def accounting_context(scope):
        route = effective_route(scope, ())
        return SimpleNamespace(
            method=route.method,
            route_template=route.route_template,
            policy=chat_logging.classify_billing_policy(
                route.method,
                route.route_template,
            ),
        )

    stack.enter_context(
        patch.object(
            chat_logging,
            "resolve_effective_http_route",
            side_effect=effective_route,
        )
    )
    stack.enter_context(
        patch.object(
            chat_logging,
            "get_accounting_request_context",
            side_effect=accounting_context,
        )
    )
    _install_response_observation_passthrough(stack)
