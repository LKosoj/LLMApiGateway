"""Narrow buffered and SSE terminal owner for chat accounting."""

from __future__ import annotations

import dataclasses
import time
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from datetime import datetime, timezone
from enum import StrEnum

from fastapi import Request

from ...middleware.accounting_admission import (
    AccountingRequestContext,
    get_accounting_request_context,
    take_accounting_request_context,
)
from ...middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    ResponseStart,
    TerminalReason,
    TerminalSignal,
    TransportResult,
)
from ...services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingValidationError,
)
from ...services.accounting_service import AccountingService
from ...services.chat_accounting import (
    ChatTerminalObservation,
    build_chat_accounting_event,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ...services.stream_observation import SSEEvent, parse_sse_json
from ...utils.usage_tracking import (
    ModelCostRates,
    extract_request_client_ip,
    extract_request_fallback_depth,
    extract_request_user_agent,
    extract_request_x_title,
)


CHAT_TERMINAL_HANDOFF_STATE_KEY = "llmgateway_chat_terminal_handoff"
_EMPTY_HANDOFF = object()
_CONSUMED_HANDOFF = object()


class ChatStreamDialect(StrEnum):
    OPENAI = "openai"
    ANTHROPIC = "anthropic"
    RESPONSES = "responses"


class _OwnerState(StrEnum):
    OPEN = "open"
    COMMITTING = "committing"
    RELEASING = "releasing"
    CLOSED = "closed"


class ChatTerminalHandoff:
    """One-shot typed handoff from chat dispatch to the middleware owner."""

    __slots__ = ("_observation", "_partial_builder", "_stream_observer")

    def __init__(self) -> None:
        self._observation: object = _EMPTY_HANDOFF
        self._stream_observer: (
            Callable[[SSEEvent], ChatTerminalObservation | None] | None
        ) = None
        self._partial_builder: Callable[[], ChatTerminalObservation | None] | None = None

    def publish(self, observation: ChatTerminalObservation) -> None:
        if (
            self._observation is not _EMPTY_HANDOFF
            or self._stream_observer is not None
            or not isinstance(observation, ChatTerminalObservation)
        ):
            raise AccountingValidationError
        self._observation = observation

    def publish_stream_observer(
        self,
        observer: Callable[[SSEEvent], ChatTerminalObservation | None],
        *,
        build_partial: Callable[[], ChatTerminalObservation | None] | None = None,
    ) -> None:
        if (
            self._observation is not _EMPTY_HANDOFF
            or self._stream_observer is not None
            or not callable(observer)
            or (build_partial is not None and not callable(build_partial))
        ):
            raise AccountingValidationError
        self._stream_observer = observer
        self._partial_builder = build_partial

    def observe_stream_event(self, event: SSEEvent) -> None:
        observer = self._stream_observer
        if not isinstance(event, SSEEvent):
            raise AccountingValidationError
        if observer is None:
            return
        observation = observer(event)
        if observation is None:
            return
        if not isinstance(observation, ChatTerminalObservation):
            raise AccountingValidationError
        self._observation = observation
        self._stream_observer = None
        self._partial_builder = None

    def take(self) -> ChatTerminalObservation:
        observation = self._observation
        if not isinstance(observation, ChatTerminalObservation):
            raise AccountingValidationError
        self._observation = _CONSUMED_HANDOFF
        return observation

    def take_partial(self) -> ChatTerminalObservation | None:
        """Best-effort recovery for a request that never reached ``take()``.

        Used on client disconnect/cancellation: returns the already-complete
        observation if one was published (e.g. a non-streaming call that
        already succeeded), otherwise asks the stream builder for a
        best-effort estimate from whatever was actually sent/received so
        far. Returns ``None`` when there is nothing to bill.
        """
        observation = self._observation
        if isinstance(observation, ChatTerminalObservation):
            self._observation = _CONSUMED_HANDOFF
            return observation
        build_partial = self._partial_builder
        self._stream_observer = None
        self._partial_builder = None
        if build_partial is None:
            return None
        partial = build_partial()
        if partial is None:
            return None
        if not isinstance(partial, ChatTerminalObservation):
            raise AccountingValidationError
        self._observation = _CONSUMED_HANDOFF
        return partial

    def __repr__(self) -> str:
        return "<ChatTerminalHandoff>"


def install_chat_terminal_handoff(request: Request) -> ChatTerminalHandoff:
    if not isinstance(request, Request):
        raise AccountingValidationError
    state = request.scope.get("state")
    if (
        not isinstance(state, MutableMapping)
        or CHAT_TERMINAL_HANDOFF_STATE_KEY in state
    ):
        raise AccountingValidationError
    handoff = ChatTerminalHandoff()
    state[CHAT_TERMINAL_HANDOFF_STATE_KEY] = handoff
    return handoff


def get_chat_terminal_handoff(request: Request) -> ChatTerminalHandoff:
    if not isinstance(request, Request):
        raise AccountingValidationError
    state = request.scope.get("state")
    if not isinstance(state, MutableMapping):
        raise AccountingValidationError
    handoff = state.get(CHAT_TERMINAL_HANDOFF_STATE_KEY)
    if not isinstance(handoff, ChatTerminalHandoff):
        raise AccountingValidationError
    return handoff


def publish_chat_terminal_observation(
    request: Request,
    observation: ChatTerminalObservation,
) -> None:
    get_chat_terminal_handoff(request).publish(observation)


def _validate_gateway_model(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or any(character.isspace() for character in value)
        or "\x00" in value
        or len(value.encode("utf-8")) > 255
    ):
        raise AccountingValidationError
    return value


def _is_terminal_sse_event(
    event: SSEEvent,
    dialect: ChatStreamDialect,
) -> bool:
    if not isinstance(event, SSEEvent) or not isinstance(
        dialect,
        ChatStreamDialect,
    ):
        raise AccountingValidationError
    if dialect is ChatStreamDialect.OPENAI and event.done:
        return True
    if event.done:
        raise AccountingValidationError
    try:
        payload = parse_sse_json(event)
    except (TypeError, ValueError, RecursionError):
        raise AccountingValidationError from None
    if not isinstance(payload, dict):
        raise AccountingValidationError
    if dialect is ChatStreamDialect.OPENAI:
        return False

    terminal_name = (
        "message_stop"
        if dialect is ChatStreamDialect.ANTHROPIC
        else "response.completed"
    )
    event_type = payload.get("type")
    if event.event == terminal_name and event_type != terminal_name:
        raise AccountingValidationError
    if event_type == terminal_name and event.event not in {None, terminal_name}:
        raise AccountingValidationError
    return event_type == terminal_name


def _is_error_sse_event(
    event: SSEEvent,
    dialect: ChatStreamDialect,
) -> bool:
    if not isinstance(event, SSEEvent) or not isinstance(
        dialect,
        ChatStreamDialect,
    ):
        raise AccountingValidationError
    if event.done:
        return False
    try:
        payload = parse_sse_json(event)
    except (TypeError, ValueError, RecursionError):
        raise AccountingValidationError from None
    if not isinstance(payload, dict):
        raise AccountingValidationError
    if dialect is ChatStreamDialect.OPENAI:
        # Check the *value* of "error", not just key presence: some providers
        # emit "error": null on every chunk of the schema, including the
        # terminal usage-only chunk (`{"choices": [], "usage": {...}}`), which
        # would otherwise be misclassified as an upstream error and drop the
        # only carrier of real usage. The client-facing twin
        # `_is_openai_stream_error_chunk` in chat_streaming.py now applies the
        # same value check, so the accounting boundary and the SSE converters
        # agree that this chunk is a successful terminal, not an error.
        return payload.get("error") is not None and not payload.get("choices")

    error_name = (
        "error"
        if dialect is ChatStreamDialect.ANTHROPIC
        else "response.failed"
    )
    event_type = payload.get("type")
    if event.event == error_name and event_type != error_name:
        raise AccountingValidationError
    if event_type == error_name and event.event not in {None, error_name}:
        raise AccountingValidationError
    return event_type == error_name


class ChatTerminalOwner:
    """One-shot owner of one admitted chat accounting reservation."""

    __slots__ = (
        "_accounting_service",
        "_context",
        "_cost_rate_registry",
        "_gateway_model",
        "_handoff",
        "_request",
        "_request_streaming",
        "_responses_completed_seen",
        "_started_at",
        "_state",
        "_stream_error_seen",
        "_stream_dialect",
    )

    def __init__(
        self,
        *,
        accounting_service: AccountingService,
        context: AccountingRequestContext,
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
        request: Request,
    ) -> None:
        self._accounting_service = accounting_service
        self._context = context
        self._cost_rate_registry = cost_rate_registry
        self._gateway_model: str | None = None
        self._handoff: ChatTerminalHandoff | None = None
        self._request = request
        self._request_streaming: bool | None = None
        self._responses_completed_seen = False
        self._started_at = time.monotonic()
        self._state = _OwnerState.OPEN
        self._stream_error_seen = False
        self._stream_dialect: ChatStreamDialect | None = None

    @property
    def accounting_service(self) -> AccountingService:
        return self._accounting_service

    @property
    def context(self) -> AccountingRequestContext:
        return self._context

    @property
    def cost_rate_registry(self) -> Mapping[tuple[str, str], ModelCostRates]:
        return self._cost_rate_registry

    @property
    def gateway_model(self) -> str:
        if self._gateway_model is None:
            raise AccountingValidationError
        return self._gateway_model

    def _require_open(self) -> None:
        if self._state is not _OwnerState.OPEN:
            raise AccountingValidationError

    def _bind_request(
        self,
        *,
        gateway_model: str,
        streaming: bool,
        dialect: ChatStreamDialect,
    ) -> None:
        self._require_open()
        if (
            self._gateway_model is not None
            or self._request_streaming is not None
            or self._stream_dialect is not None
            or not isinstance(streaming, bool)
            or not isinstance(dialect, ChatStreamDialect)
        ):
            raise AccountingValidationError
        self._gateway_model = _validate_gateway_model(gateway_model)
        self._request_streaming = streaming
        self._stream_dialect = dialect

    def _attach_handoff(self, handoff: ChatTerminalHandoff) -> None:
        self._require_open()
        if self._handoff is not None or not isinstance(
            handoff,
            ChatTerminalHandoff,
        ):
            raise AccountingValidationError
        self._handoff = handoff

    async def _commit(
        self,
        observation: ChatTerminalObservation,
    ) -> AccountingReceipt:
        self._require_open()
        duration_ms = int((time.monotonic() - self._started_at) * 1000)
        event = build_chat_accounting_event(
            self._context,
            observation,
            gateway_model=self.gateway_model,
            occurred_at=datetime.now(timezone.utc),
            duration_ms=duration_ms,
        )
        event = dataclasses.replace(
            event,
            upstream_key_fingerprint=getattr(
                self._request.state,
                "llmgateway_upstream_key_fingerprint",
                None,
            ),
            x_title=extract_request_x_title(self._request),
            client_ip=extract_request_client_ip(self._request),
            client_user_agent=extract_request_user_agent(self._request),
            fallback_depth=extract_request_fallback_depth(self._request),
        )
        self._state = _OwnerState.COMMITTING
        try:
            receipt = await self._accounting_service.commit(
                self._context.reservation,
                event,
            )
            if not isinstance(receipt, AccountingReceipt):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            return receipt
        finally:
            self._state = _OwnerState.CLOSED

    async def _release(self) -> bool:
        self._require_open()
        self._state = _OwnerState.RELEASING
        try:
            released = await self._accounting_service.release(
                self._context.reservation
            )
            if not isinstance(released, bool):
                raise AccountingValidationError
            if not released:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            return released
        finally:
            self._state = _OwnerState.CLOSED

    async def _release_if_open(self) -> None:
        if self._state is _OwnerState.OPEN:
            await self._release()

    def _require_bound_request(self) -> tuple[bool, ChatStreamDialect]:
        self._require_open()
        if (
            self._gateway_model is None
            or self._request_streaming is None
            or self._stream_dialect is None
        ):
            raise AccountingValidationError
        return self._request_streaming, self._stream_dialect

    @staticmethod
    def _response_status_code(response_start: ResponseStart) -> int:
        if not isinstance(response_start, ResponseStart):
            raise AccountingValidationError
        return response_start.status_code

    def _classify_json(
        self,
        response_start: ResponseStart,
        _body: bytes,
    ) -> TerminalReason:
        if self._response_status_code(response_start) >= 400:
            return TerminalReason.UPSTREAM_ERROR
        streaming, _dialect = self._require_bound_request()
        return (
            TerminalReason.PROTOCOL_ERROR
            if streaming
            else TerminalReason.COMPLETE
        )

    def _classify_opaque(self, response_start: ResponseStart) -> TerminalReason:
        if self._response_status_code(response_start) >= 400:
            return TerminalReason.UPSTREAM_ERROR
        self._require_bound_request()
        return TerminalReason.PROTOCOL_ERROR

    def _classify_sse(
        self,
        response_start: ResponseStart,
        event: SSEEvent,
    ) -> TerminalReason | None:
        if self._response_status_code(response_start) >= 400:
            if not event.done:
                try:
                    payload = parse_sse_json(event)
                except (TypeError, ValueError, RecursionError):
                    return TerminalReason.PROTOCOL_ERROR
                if not isinstance(payload, dict):
                    return TerminalReason.PROTOCOL_ERROR
            return TerminalReason.UPSTREAM_ERROR
        streaming, dialect = self._require_bound_request()
        if not streaming:
            return TerminalReason.PROTOCOL_ERROR
        handoff = self._handoff
        if handoff is None:
            return TerminalReason.PROTOCOL_ERROR
        try:
            if (
                dialect is ChatStreamDialect.RESPONSES
                and self._responses_completed_seen
                and not event.done
            ):
                return TerminalReason.PROTOCOL_ERROR
            error_event = _is_error_sse_event(event, dialect)
            if self._stream_error_seen:
                return (
                    TerminalReason.UPSTREAM_ERROR
                    if event.done
                    else TerminalReason.PROTOCOL_ERROR
                )
            if error_event:
                if dialect is not ChatStreamDialect.RESPONSES:
                    return TerminalReason.UPSTREAM_ERROR
                self._stream_error_seen = True
                return None
            handoff.observe_stream_event(event)
            if dialect is ChatStreamDialect.RESPONSES and event.done:
                return (
                    TerminalReason.COMPLETE
                    if self._responses_completed_seen
                    else TerminalReason.PROTOCOL_ERROR
                )
            if dialect is ChatStreamDialect.RESPONSES and self._responses_completed_seen:
                return TerminalReason.PROTOCOL_ERROR
            terminal = _is_terminal_sse_event(event, dialect)
        except AccountingValidationError:
            return TerminalReason.PROTOCOL_ERROR
        if dialect is ChatStreamDialect.RESPONSES and terminal:
            self._responses_completed_seen = True
            return None
        return TerminalReason.COMPLETE if terminal else None

    async def _finalize(self, signal: TerminalSignal) -> object:
        if not isinstance(signal, TerminalSignal):
            raise AccountingValidationError
        start = signal.start
        if (
            signal.reason is TerminalReason.COMPLETE
            and isinstance(start, ResponseStart)
            and start.status_code < 400
        ):
            handoff = self._handoff
            if handoff is None:
                await self._release_if_open()
                raise AccountingValidationError
            try:
                observation = handoff.take()
                return await self._commit(observation)
            except BaseException:
                try:
                    await self._release_if_open()
                except BaseException:
                    pass
                raise
        if (
            signal.reason in (TerminalReason.CLIENT_DISCONNECT, TerminalReason.CANCELLED)
            and isinstance(start, ResponseStart)
            and start.status_code < 400
        ):
            handoff = self._handoff
            try:
                observation = handoff.take_partial() if handoff is not None else None
            except BaseException:
                try:
                    await self._release_if_open()
                except BaseException:
                    pass
                raise
            if observation is not None:
                try:
                    return await self._commit(observation)
                except BaseException:
                    try:
                        await self._release_if_open()
                    except BaseException:
                        pass
                    raise
        await self._release_if_open()
        return False

    def __repr__(self) -> str:
        return "<ChatTerminalOwner>"


def take_chat_terminal_owner(request: Request) -> ChatTerminalOwner:
    """Capture the exact process/runtime generation and consume its chat context."""
    if not isinstance(request, Request):
        raise AccountingValidationError
    try:
        services = request.app.state.services
        snapshot = request.state.runtime_snapshot
    except (AttributeError, KeyError, TypeError):
        raise AccountingValidationError from None
    if not isinstance(services, AppServices) or not isinstance(snapshot, RuntimeSnapshot):
        raise AccountingValidationError
    if not isinstance(services.accounting_service, AccountingService):
        raise AccountingValidationError

    context = get_accounting_request_context(request.scope)
    if context is None or context.policy.operation != "chat":
        raise AccountingValidationError
    taken = take_accounting_request_context(request.scope)
    if taken is not context:
        raise AccountingValidationError
    return ChatTerminalOwner(
        accounting_service=services.accounting_service,
        context=context,
        cost_rate_registry=snapshot.cost_rate_registry,
        request=request,
    )


def bind_chat_request(
    owner: ChatTerminalOwner,
    *,
    gateway_model: str,
    streaming: bool,
    dialect: ChatStreamDialect,
) -> None:
    if not isinstance(owner, ChatTerminalOwner):
        raise AccountingValidationError
    owner._bind_request(
        gateway_model=gateway_model,
        streaming=streaming,
        dialect=dialect,
    )


def build_chat_response_observation(
    owner: ChatTerminalOwner,
    handoff: ChatTerminalHandoff,
    services: AppServices,
    *,
    bind_request: Callable[[], None],
    observe_body_chunk: (
        Callable[[ResponseStart, bytes], None | Awaitable[None]] | None
    ) = None,
    transport_finished: (
        Callable[[TransportResult], None | Awaitable[None]] | None
    ) = None,
) -> ResponseObservation:
    if (
        not isinstance(owner, ChatTerminalOwner)
        or not isinstance(handoff, ChatTerminalHandoff)
        or not isinstance(services, AppServices)
        or not callable(bind_request)
    ):
        raise AccountingValidationError
    owner._attach_handoff(handoff)

    def classify_json(start: ResponseStart, body: bytes) -> TerminalReason:
        if start.status_code < 400:
            bind_request()
        return owner._classify_json(start, body)

    def classify_sse(
        start: ResponseStart,
        event: SSEEvent,
    ) -> TerminalReason | None:
        if start.status_code < 400:
            bind_request()
        return owner._classify_sse(start, event)

    def classify_opaque(start: ResponseStart) -> TerminalReason:
        if start.status_code < 400:
            bind_request()
        return owner._classify_opaque(start)

    return ResponseObservation(
        finalizer=ResponseFinalizer(
            services.task_supervisor,
            owner._finalize,
            abort=owner._release_if_open,
        ),
        classify_json=classify_json,
        classify_sse=classify_sse,
        classify_opaque=classify_opaque,
        observe_body_chunk=observe_body_chunk,
        transport_finished=transport_finished,
    )


async def release_chat(owner: ChatTerminalOwner) -> bool:
    if not isinstance(owner, ChatTerminalOwner):
        raise AccountingValidationError
    return await owner._release()
