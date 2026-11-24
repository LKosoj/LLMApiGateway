"""Pure-ASGI response validation and exactly-once terminal finalization."""

from __future__ import annotations

import asyncio
import codecs
import json
import re
from collections.abc import Awaitable, Callable
from enum import StrEnum

from fastapi import Request
from starlette.requests import ClientDisconnect
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from ..api.error_envelope import error_response
from ..services.accounting import AccountingReceipt
from ..services.fusion_keepalive import json_keepalive_enabled
from ..services.runtime_config import AppServices
from ..services.stream_observation import (
    SSEEvent,
    SSEFramer,
    SSEFramingError,
    StreamObservationCapacity,
    StreamObservationError,
    StreamObservationLease,
    parse_sse_json,
)
from ..utils.usage_tracking import get_provider_cost_currency
from .response_observation_contracts import (
    FinalizationResult,
    MaybeAwaitable as _MaybeAwaitable,
    ResponseFinalizer,
    ResponseObservation,
    ResponseStart,
    TerminalReason,
    TerminalSignal,
    TransportResult,
    WireMode,
    await_if_needed as _await_if_needed,
)


_SESSION_STATE_KEY = "_llmgateway_response_observation_session"
_UPSTREAM_PROTOCOL_DETAIL = "Upstream response failed protocol validation."
_ACCOUNTING_UNAVAILABLE_DETAIL = "Response accounting is temporarily unavailable."


def _client_billing(native_costs: tuple[tuple[str, float], ...]) -> dict | list[dict]:
    items = [
        {"currency": currency, "net_amount": amount, "status": "settled"}
        for currency, amount in native_costs
    ]
    return items[0] if len(items) == 1 else items


def _strip_unsettled_sse_cost(body: bytes) -> bytes:
    # Keep the framing and control fields of every event. Only replace JSON data
    # with a cost field, after the accounting observer has read its original value.
    frames = re.split(rb"(\r?\n\r?\n)", body)
    for index in range(0, len(frames), 2):
        frame = frames[index]
        lines = frame.splitlines()
        data_lines = [line[5:].lstrip(b" ") for line in lines if line.startswith(b"data:")]
        try:
            payload = json.loads(b"\n".join(data_lines))
        except (ValueError, RecursionError):
            continue
        if not isinstance(payload, dict):
            continue
        changed = False
        for envelope in (payload, payload.get("response"), payload.get("message")):
            usage = envelope.get("usage") if isinstance(envelope, dict) else None
            if isinstance(usage, dict) and "cost" in usage:
                del usage["cost"]
                changed = True
        if changed:
            control_lines = [line for line in lines if not line.startswith(b"data:")]
            data = b"data: " + json.dumps(payload, separators=(",", ":")).encode()
            frames[index] = b"\n".join([*control_lines, data])
    return b"".join(frames)


class ResponseObservationStateError(RuntimeError):
    """Invalid response-observation ownership or request-capture state."""


class ResponseObservationSession:
    """Request-scoped handoff shared by auth, endpoint, and ASGI middleware."""

    __slots__ = (
        "_capture_complete",
        "_capture_enabled",
        "_capture_max_bytes",
        "_captured_body",
        "_disconnected",
        "_observation",
        "_receive_started",
    )

    def __init__(self) -> None:
        self._observation: ResponseObservation | None = None
        self._capture_enabled = False
        self._capture_complete = False
        self._capture_max_bytes = 0
        self._captured_body = bytearray()
        self._receive_started = False
        self._disconnected = False

    @property
    def observation(self) -> ResponseObservation | None:
        return self._observation

    @property
    def disconnected(self) -> bool:
        return self._disconnected

    def publish(self, observation: ResponseObservation) -> None:
        if not isinstance(observation, ResponseObservation):
            raise TypeError("observation must be a ResponseObservation")
        if self._observation is not None:
            raise ResponseObservationStateError(
                "A response observation is already published."
            )
        self._observation = observation

    def enable_request_capture(self, max_bytes: int) -> None:
        if (
            isinstance(max_bytes, bool)
            or not isinstance(max_bytes, int)
            or max_bytes <= 0
        ):
            raise ValueError("max_bytes must be a positive integer")
        if self._receive_started:
            raise ResponseObservationStateError(
                "Request capture must be enabled before receiving the body."
            )
        if self._capture_enabled:
            raise ResponseObservationStateError("Request capture is already enabled.")
        self._capture_enabled = True
        self._capture_max_bytes = max_bytes

    def captured_request_body(self) -> bytes:
        if not self._capture_enabled or not self._capture_complete:
            raise ResponseObservationStateError(
                "The captured request body is not complete."
            )
        return bytes(self._captured_body)

    def observe_receive(self, message: Message) -> None:
        self._receive_started = True
        message_type = message.get("type")
        if message_type == "http.disconnect":
            self._disconnected = True
            return
        if message_type != "http.request" or not self._capture_enabled:
            return
        body = _message_body(message)
        if len(self._captured_body) + len(body) > self._capture_max_bytes:
            raise ResponseObservationStateError(
                "The captured request body exceeds its configured limit."
            )
        self._captured_body.extend(body)
        if not bool(message.get("more_body", False)):
            self._capture_complete = True


def get_response_observation_session(
    scope: Scope,
) -> ResponseObservationSession | None:
    state = scope.get("state")
    if not isinstance(state, dict):
        return None
    session = state.get(_SESSION_STATE_KEY)
    return session if isinstance(session, ResponseObservationSession) else None


def _ensure_response_observation_session(scope: Scope) -> ResponseObservationSession:
    state = scope.setdefault("state", {})
    session = state.get(_SESSION_STATE_KEY)
    if session is None:
        session = ResponseObservationSession()
        state[_SESSION_STATE_KEY] = session
    if not isinstance(session, ResponseObservationSession):
        raise ResponseObservationStateError("Invalid response observation session.")
    return session


def publish_response_observation(
    scope: Scope,
    observation: ResponseObservation,
) -> None:
    _ensure_response_observation_session(scope).publish(observation)


def get_response_observation(scope: Scope) -> ResponseObservation | None:
    session = get_response_observation_session(scope)
    return session.observation if session is not None else None


def response_observation_published(scope: Scope) -> bool:
    return get_response_observation(scope) is not None


def enable_request_capture(scope: Scope, max_bytes: int) -> None:
    _ensure_response_observation_session(scope).enable_request_capture(max_bytes)


def captured_request_body(scope: Scope) -> bytes:
    session = get_response_observation_session(scope)
    if session is None:
        raise ResponseObservationStateError("Response observation session is missing.")
    return session.captured_request_body()


class _FailureKind(StrEnum):
    PROTOCOL = "protocol"
    ACCOUNTING = "accounting"


class _ObservationFailure(RuntimeError):
    def __init__(self, kind: _FailureKind) -> None:
        self.kind = kind
        super().__init__(kind.value)


class _LeaseGroup:
    __slots__ = ("_leases", "_remaining_bytes")

    def __init__(self) -> None:
        self._leases: list[StreamObservationLease] = []
        self._remaining_bytes = 0

    @property
    def remaining_bytes(self) -> int:
        return self._remaining_bytes

    def append(self, lease: StreamObservationLease) -> None:
        self._leases.append(lease)
        self._remaining_bytes += lease.remaining_bytes

    def release_all(self) -> None:
        for lease in self._leases:
            lease.release_all()
        self._leases.clear()
        self._remaining_bytes = 0


class _ResponseObserver:
    __slots__ = (
        "_actual_completed",
        "_actual_started",
        "_body_messages",
        "_json_keepalive",
        "_leases",
        "_observation",
        "_pending_start",
        "_response_cost_usd",
        "_native_costs",
        "_publish_cost_header",
        "_strip_sse_cost",
        "_send",
        "_services",
        "_settled",
        "_sse_cost_payload",
        "_sse_validated_event",
        "_sse_framer",
        "_start",
    )

    def __init__(
        self,
        *,
        send: Send,
        services: AppServices,
        observation: ResponseObservation,
        strip_sse_cost: bool = False,
    ) -> None:
        self._send = send
        self._services = services
        self._observation = observation
        self._pending_start: Message | None = None
        self._response_cost_usd: float | None = None
        self._native_costs: tuple[tuple[str, float], ...] = ()
        self._publish_cost_header = True
        self._strip_sse_cost = strip_sse_cost
        self._start: ResponseStart | None = None
        self._body_messages: list[Message] = []
        self._json_keepalive = False
        self._leases = _LeaseGroup()
        self._sse_framer: SSEFramer | None = None
        self._sse_validated_event = False
        self._settled = False
        self._sse_cost_payload: dict = {"type": "usage"}
        self._actual_started = False
        self._actual_completed = False

    @property
    def actual_started(self) -> bool:
        return self._actual_started

    @property
    def actual_completed(self) -> bool:
        return self._actual_completed

    async def send(self, message: Message) -> None:
        message_type = message.get("type")
        if message_type == "http.response.start":
            await self._capture_start(message)
            return
        if message_type != "http.response.body":
            await self._send(message)
            return
        if self._start is None:
            raise _ObservationFailure(_FailureKind.PROTOCOL)

        body = _message_body(message)
        if self._start.wire_mode is WireMode.SSE and self._settled and body:
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        if body and self._observation.observe_body_chunk is not None:
            try:
                await _await_if_needed(
                    self._observation.observe_body_chunk(
                        self._start,
                        body,
                    )
                )
            except asyncio.CancelledError as exc:
                if _request_task_is_cancelling():
                    raise
                raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
            except BaseException as exc:
                raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc

        if self._start.wire_mode is WireMode.JSON:
            await self._send_json(message, body)
        elif self._start.wire_mode is WireMode.SSE:
            await self._send_sse(message, body)
        else:
            await self._send_opaque(message)

    async def abort(self, reason: TerminalReason) -> None:
        if not self._settled:
            await self._settle_or_accounting_failure(reason)

    async def finish_transport(self, reason: TerminalReason) -> None:
        callback = self._observation.transport_finished
        if callback is None:
            return
        await _await_if_needed(
            callback(
                TransportResult(
                    reason=reason,
                    response_started=self._actual_started,
                    response_completed=self._actual_completed,
                )
            )
        )

    def release_capacity(self) -> None:
        self._leases.release_all()
        if self._sse_framer is not None:
            self._sse_framer.abort()

    async def _capture_start(self, message: Message) -> None:
        if self._start is not None:
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        status_code = message.get("status")
        headers = message.get("headers", [])
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
            or not isinstance(headers, (list, tuple))
        ):
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        try:
            raw_headers = tuple(
                (bytes(name), bytes(value)) for name, value in headers
            )
            wire_mode, charset = _wire_mode(raw_headers)
        except (TypeError, ValueError, LookupError) as exc:
            raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
        self._pending_start = dict(message)
        self._pending_start["headers"] = list(raw_headers)
        self._start = ResponseStart(
            status_code=status_code,
            headers=raw_headers,
            wire_mode=wire_mode,
            charset=charset,
        )
        self._json_keepalive = json_keepalive_enabled(raw_headers)
        if wire_mode is WireMode.SSE:
            self._sse_framer = SSEFramer(
                max_event_bytes=self._services.stream_event_max_bytes
            )
        elif wire_mode is WireMode.OPAQUE:
            try:
                reason = await _classify_reason(
                    self._observation.classify_opaque(self._start)
                )
            except asyncio.CancelledError as exc:
                if _request_task_is_cancelling():
                    raise
                raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
            except _ObservationFailure:
                raise
            except BaseException as exc:
                raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
            await self._settle_classified_reason(reason)
            await self._flush_start()

    async def _send_json(self, message: Message, body: bytes) -> None:
        more_body = bool(message.get("more_body", False))
        if self._settled:
            if body and not _is_json_whitespace(body):
                raise _ObservationFailure(_FailureKind.PROTOCOL)
            await self._send_body(message)
            return
        # Opt-in only. Ordinary JSON still buffers every chunk until the
        # document is complete. Whitespace keepalives must leave immediately
        # and must not occupy the small retained-frame budget.
        if (
            self._json_keepalive
            and more_body
            and not self._body_messages
            and _is_json_whitespace(body)
        ):
            if len(body) > self._services.json_response_max_bytes:
                raise _ObservationFailure(_FailureKind.PROTOCOL)
            await self._flush_start()
            await self._send_body(message)
            return
        if self._actual_started and not self._json_keepalive:
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        if body:
            if self._leases.remaining_bytes + len(body) > self._services.json_response_max_bytes:
                raise _ObservationFailure(_FailureKind.PROTOCOL)
        self._retain_body_message(
            self._services.json_response_capacity,
            message,
            body,
        )
        if more_body:
            return
        payload = b"".join(_message_body(item) for item in self._body_messages)
        try:
            assert self._start is not None
            decoded = payload.decode(self._start.charset, errors="strict")
            document = json.loads(decoded, parse_constant=_reject_json_constant)
        except (UnicodeError, LookupError, RecursionError, ValueError) as exc:
            raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
        try:
            reason = await _classify_reason(
                self._observation.classify_json(self._start, payload)
            )
        except asyncio.CancelledError as exc:
            if _request_task_is_cancelling():
                raise
            raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
        except _ObservationFailure:
            raise
        except BaseException as exc:
            raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
        await self._settle_classified_reason(reason)
        cost = self._response_cost_usd
        if cost is not None and isinstance(document, dict):
            usage = document.get("usage")
            if not isinstance(usage, dict):
                usage = document["usage"] = {}
            existing_cost = usage.get("cost")
            if isinstance(existing_cost, bool) or existing_cost != cost or self._native_costs:
                usage["cost"] = cost
                if self._native_costs:
                    document["billing"] = _client_billing(self._native_costs)
                payload = json.dumps(document, ensure_ascii=True, separators=(",", ":")).encode(
                    self._start.charset
                )
                self._body_messages = [{**message, "body": payload}]
                assert self._pending_start is not None
                self._pending_start["headers"] = [
                    (name, str(len(payload)).encode() if name.lower() == b"content-length" else value)
                    for name, value in self._pending_start["headers"]
                ]
        await self._flush_start()
        for body_message in self._body_messages:
            await self._send_body(body_message)
        self._body_messages.clear()
        self._leases.release_all()

    async def _send_sse(self, message: Message, body: bytes) -> None:
        framer = self._sse_framer
        if framer is None or self._start is None:
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        if self._settled:
            await self._flush_start()
            await self._send_body(message)
            return
        self._retain_body_message(
            self._services.stream_observation_capacity,
            message,
            body,
        )
        try:
            batch = framer.feed(body)
            if framer.terminal_trailing_bytes:
                raise _ObservationFailure(_FailureKind.PROTOCOL)
            if batch.events:
                self._sse_validated_event = True
            terminal_reason = await self._classify_sse_events(batch.events)
            terminal_bytes = batch.last_event_bytes if terminal_reason is not None else 0
            if terminal_reason is not None and (
                framer.buffered_bytes or batch.trailing_frame_bytes
            ):
                raise _ObservationFailure(_FailureKind.PROTOCOL)
            if not bool(message.get("more_body", False)):
                final_batch = framer.finish()
                if final_batch.events:
                    self._sse_validated_event = True
                final_reason = await self._classify_sse_events(final_batch.events)
                if final_reason is not None:
                    terminal_bytes = final_batch.last_event_bytes
                terminal_reason = terminal_reason or final_reason
                if terminal_reason is None:
                    terminal_reason = (
                        TerminalReason.UPSTREAM_ERROR
                        if self._start.status_code >= 400
                        else TerminalReason.EMPTY
                    )
                    await self._settle_classified_reason(terminal_reason)
                elif not self._settled:
                    await self._settle_classified_reason(terminal_reason)
            elif terminal_reason is not None and not self._settled:
                await self._settle_classified_reason(terminal_reason)
        except asyncio.CancelledError as exc:
            if _request_task_is_cancelling():
                raise
            raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
        except _ObservationFailure:
            raise
        except SSEFramingError as exc:
            raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
        except BaseException as exc:
            raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc

        can_flush = terminal_reason is not None or (
            framer.buffered_bytes == 0
            and (self._actual_started or self._sse_validated_event)
        )
        if not can_flush:
            return

        if not self._actual_started:
            await self._flush_start()
        if self._response_cost_usd is not None or self._strip_sse_cost:
            payload = b"".join(_message_body(item) for item in self._body_messages)
            terminal_offset = len(payload) - terminal_bytes
            prefix, terminal = payload[:terminal_offset], payload[terminal_offset:]
            if self._strip_sse_cost:
                prefix = _strip_unsettled_sse_cost(prefix)
                terminal = _strip_unsettled_sse_cost(terminal)
        if self._response_cost_usd is not None:
            cost_payload = self._sse_cost_payload
            usage = cost_payload.setdefault("usage", {})
            usage["cost"] = self._response_cost_usd
            if self._native_costs:
                cost_payload["billing"] = _client_billing(self._native_costs)
            cost_event = b"event: usage\ndata: " + json.dumps(cost_payload, separators=(",", ":")).encode() + b"\n\n"
            prefix += cost_event
        if self._response_cost_usd is not None or self._strip_sse_cost:
            self._body_messages = [{**message, "body": prefix + terminal}]
        for body_message in self._body_messages:
            await self._send_body(body_message)
        self._body_messages.clear()
        self._leases.release_all()
        self._sse_validated_event = False

    async def _send_opaque(self, message: Message) -> None:
        await self._flush_start()
        await self._send_body(message)

    async def _classify_sse_events(
        self,
        events: tuple[SSEEvent, ...],
    ) -> TerminalReason | None:
        assert self._start is not None
        terminal_reason: TerminalReason | None = None
        for event in events:
            if terminal_reason is not None:
                raise _ObservationFailure(_FailureKind.PROTOCOL)
            try:
                reason = await _classify_optional_reason(
                    self._observation.classify_sse(self._start, event)
                )
            except asyncio.CancelledError as exc:
                if _request_task_is_cancelling():
                    raise
                raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
            except _ObservationFailure:
                raise
            except BaseException as exc:
                raise _ObservationFailure(_FailureKind.PROTOCOL) from exc
            if reason is not None:
                terminal_reason = reason
            self._remember_sse_usage(event)
        return terminal_reason

    def _remember_sse_usage(self, event: SSEEvent) -> None:
        if event.done:
            return
        try:
            payload = parse_sse_json(event)
        except (ValueError, RecursionError):
            return
        if not isinstance(payload, dict):
            return
        if payload.get("object") == "chat.completion.chunk":
            for key in ("id", "object", "created", "model"):
                if key in payload:
                    self._sse_cost_payload[key] = payload[key]
            self._sse_cost_payload["choices"] = []
            usage = payload.get("usage")
            if isinstance(usage, dict):
                self._sse_cost_payload["usage"] = usage
        elif payload.get("type") == "response.completed":
            self._sse_cost_payload["type"] = "response.usage"
            sequence = payload.get("sequence_number")
            if type(sequence) is int:
                self._sse_cost_payload["sequence_number"] = sequence + 1

    async def _settle_classified_reason(self, reason: TerminalReason) -> None:
        await self._settle_or_accounting_failure(reason)
        if reason in {TerminalReason.PROTOCOL_ERROR, TerminalReason.EMPTY}:
            raise _ObservationFailure(_FailureKind.PROTOCOL)

    async def _settle_or_accounting_failure(self, reason: TerminalReason) -> None:
        try:
            await self._settle(reason)
        except asyncio.CancelledError as exc:
            if _request_task_is_cancelling():
                raise
            raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
        except BaseException as exc:
            raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc

    async def _settle(self, reason: TerminalReason) -> None:
        result = await self._observation.finalizer.finalize(
            TerminalSignal(reason=reason, start=self._start)
        )
        if reason is TerminalReason.COMPLETE and isinstance(result.value, AccountingReceipt):
            self._response_cost_usd = result.value.response_cost_usd
            self._native_costs = result.value.native_costs
            self._publish_cost_header = result.value.publish_cost_header
        self._settled = True

    def _retain_body_message(
        self,
        capacity: StreamObservationCapacity,
        message: Message,
        body: bytes,
    ) -> None:
        if len(self._body_messages) >= capacity.snapshot.max_items:
            raise _ObservationFailure(_FailureKind.ACCOUNTING)
        if body:
            self._reserve(capacity, body)
        self._body_messages.append(dict(message))

    def _reserve(self, capacity: StreamObservationCapacity, body: bytes) -> None:
        try:
            lease = capacity.try_acquire(len(body))
        except StreamObservationError as exc:
            raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
        self._leases.append(lease)

    async def _flush_start(self) -> None:
        if self._actual_started:
            return
        if self._pending_start is None:
            raise _ObservationFailure(_FailureKind.PROTOCOL)
        if self._start is not None and self._start.wire_mode is WireMode.SSE:
            self._pending_start["headers"] = [
                (name, value) for name, value in self._pending_start["headers"]
                if name.lower() != b"content-length"
            ]
        if self._response_cost_usd is not None and self._publish_cost_header:
            headers = self._pending_start["headers"]
            self._pending_start["headers"] = [
                (name, value) for name, value in headers if name.lower() != b"x-cost-usd"
            ] + [(b"x-cost-usd", str(self._response_cost_usd).encode())]
        await self._send(self._pending_start)
        self._actual_started = True

    async def _send_body(self, message: Message) -> None:
        await self._send(message)
        if not bool(message.get("more_body", False)):
            self._actual_completed = True


class ResponseObservationMiddleware:
    """Validate observed responses and settle their owner at the ASGI boundary."""

    def __init__(
        self,
        app: ASGIApp,
        request_preparer: Callable[[Request], Awaitable[None]] | None = None,
    ) -> None:
        self.app = app
        self.request_preparer = request_preparer

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        session = _ensure_response_observation_session(scope)

        async def observed_receive() -> Message:
            message = await receive()
            session.observe_receive(message)
            return message

        observer: _ResponseObserver | None = None
        primary_error: BaseException | None = None
        transport_reason = TerminalReason.EMPTY
        passthrough_started = False
        passthrough_completed = False
        try:
            if self.request_preparer is not None:
                await self.request_preparer(Request(scope, observed_receive))

            async def observed_send(message: Message) -> None:
                nonlocal observer, passthrough_completed, passthrough_started
                if observer is None:
                    observation = session.observation
                    if observation is None:
                        await send(message)
                        if message.get("type") == "http.response.start":
                            passthrough_started = True
                        elif message.get("type") == "http.response.body" and not bool(
                            message.get("more_body", False)
                        ):
                            passthrough_completed = True
                        return
                    if passthrough_started:
                        raise _ObservationFailure(_FailureKind.PROTOCOL)
                    state = scope.get("state", {})
                    snapshot = state.get("runtime_snapshot")
                    observer = _ResponseObserver(
                        send=send,
                        services=_app_services(scope),
                        observation=observation,
                        strip_sse_cost=get_provider_cost_currency(
                            getattr(snapshot, "cost_rate_registry", {}), state.get("llmgateway_provider"),
                        ) is not None,
                    )
                await observer.send(message)

            await self.app(scope, observed_receive, observed_send)
            if observer is not None:
                if session.disconnected and not observer.actual_completed:
                    transport_reason = TerminalReason.CLIENT_DISCONNECT
                    await _abort_preserving_primary(observer, transport_reason)
                elif observer.actual_completed:
                    signal = observer._observation.finalizer.signal
                    transport_reason = (
                        signal.reason if signal is not None else TerminalReason.COMPLETE
                    )
                else:
                    transport_reason = TerminalReason.EMPTY
                    await observer.abort(transport_reason)
                    if not observer.actual_started:
                        raise _ObservationFailure(_FailureKind.PROTOCOL)
            elif session.observation is not None:
                if session.disconnected and not passthrough_completed:
                    transport_reason = TerminalReason.CLIENT_DISCONNECT
                    await _finalize_unbound_preserving_primary(
                        session.observation,
                        transport_reason,
                    )
                else:
                    transport_reason = TerminalReason.PROTOCOL_ERROR
                    await _finalize_unbound(
                        session.observation,
                        transport_reason,
                    )
                    if not passthrough_started:
                        raise _ObservationFailure(_FailureKind.PROTOCOL)
        except asyncio.CancelledError as exc:
            primary_error = exc
            transport_reason = TerminalReason.CANCELLED
            if observer is not None:
                await _abort_preserving_primary(observer, transport_reason)
            elif session.observation is not None:
                await _finalize_unbound_preserving_primary(
                    session.observation,
                    transport_reason,
                )
            raise
        except ClientDisconnect as exc:
            primary_error = exc
            transport_reason = TerminalReason.CLIENT_DISCONNECT
            if observer is not None:
                await _abort_preserving_primary(observer, transport_reason)
            elif session.observation is not None:
                await _finalize_unbound_preserving_primary(
                    session.observation,
                    transport_reason,
                )
            raise
        except _ObservationFailure as exc:
            primary_error = exc
            transport_reason = (
                TerminalReason.PROTOCOL_ERROR
                if exc.kind is _FailureKind.PROTOCOL
                else TerminalReason.UPSTREAM_ERROR
            )
            if observer is not None:
                await _abort_preserving_primary(observer, transport_reason)
            elif session.observation is not None:
                await _finalize_unbound_preserving_primary(
                    session.observation,
                    transport_reason,
                )
            if (observer is not None and observer.actual_started) or passthrough_started:
                cause = exc.__cause__
                if cause is not None:
                    raise cause
                raise
            await _send_safe_failure(scope, send, exc.kind)
        except BaseException as exc:
            primary_error = exc
            transport_reason = TerminalReason.UPSTREAM_ERROR
            if observer is not None:
                await _abort_preserving_primary(observer, transport_reason)
            elif session.observation is not None:
                await _finalize_unbound_preserving_primary(
                    session.observation,
                    transport_reason,
                )
            raise
        finally:
            if observer is not None:
                observer.release_capacity()
                try:
                    await observer.finish_transport(transport_reason)
                except BaseException:
                    if primary_error is None:
                        raise
            elif session.observation is not None:
                try:
                    await _finish_unbound_transport(
                        session.observation,
                        transport_reason,
                        response_started=passthrough_started,
                        response_completed=passthrough_completed,
                    )
                except BaseException:
                    if primary_error is None:
                        raise


async def _abort_preserving_primary(
    observer: _ResponseObserver,
    reason: TerminalReason,
) -> None:
    try:
        await observer.abort(reason)
    except BaseException:
        return


async def _finalize_unbound(
    observation: ResponseObservation,
    reason: TerminalReason,
) -> None:
    try:
        await observation.finalizer.finalize(TerminalSignal(reason=reason))
    except asyncio.CancelledError as exc:
        if _request_task_is_cancelling():
            raise
        raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
    except BaseException as exc:
        raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc


async def _finalize_unbound_preserving_primary(
    observation: ResponseObservation,
    reason: TerminalReason,
) -> None:
    try:
        await _finalize_unbound(observation, reason)
    except BaseException:
        return


async def _finish_unbound_transport(
    observation: ResponseObservation,
    reason: TerminalReason,
    *,
    response_started: bool,
    response_completed: bool,
) -> None:
    callback = observation.transport_finished
    if callback is None:
        return
    await _await_if_needed(
        callback(
            TransportResult(
                reason=reason,
                response_started=response_started,
                response_completed=response_completed,
            )
        )
    )


def _app_services(scope: Scope) -> AppServices:
    try:
        services = scope["app"].state.services
    except (AttributeError, KeyError, TypeError) as exc:
        raise _ObservationFailure(_FailureKind.ACCOUNTING) from exc
    if not isinstance(services, AppServices):
        raise _ObservationFailure(_FailureKind.ACCOUNTING)
    return services


async def _send_safe_failure(
    scope: Scope,
    send: Send,
    kind: _FailureKind,
) -> None:
    if kind is _FailureKind.PROTOCOL:
        response = error_response(
            Request(scope),
            status_code=502,
            detail=_UPSTREAM_PROTOCOL_DETAIL,
            code="upstream_protocol_error",
        )
    else:
        response = error_response(
            Request(scope),
            status_code=503,
            detail=_ACCOUNTING_UNAVAILABLE_DETAIL,
            code="accounting_unavailable",
        )
    await response(scope, _empty_receive, send)


async def _empty_receive() -> Message:
    return {"type": "http.request", "body": b"", "more_body": False}


def _wire_mode(headers: tuple[tuple[bytes, bytes], ...]) -> tuple[WireMode, str]:
    content_types: list[str] = []
    for name, value in headers:
        if name.lower() == b"content-type":
            content_types.append(value.decode("latin-1"))
    if len(content_types) > 1:
        raise ValueError("duplicate content-type headers")
    content_type = content_types[0] if content_types else ""
    parts = [part.strip() for part in content_type.split(";")]
    mime = parts[0].lower()
    charset = "utf-8"
    for parameter in parts[1:]:
        name, separator, value = parameter.partition("=")
        if separator and name.strip().lower() == "charset":
            charset = value.strip().strip('"').lower()
            break
    if mime == "text/event-stream":
        return WireMode.SSE, "utf-8"
    if mime == "application/json" or (
        mime.startswith("application/") and mime.endswith("+json")
    ):
        if not charset:
            raise ValueError("empty charset")
        codecs.lookup(charset)
        return WireMode.JSON, charset
    return WireMode.OPAQUE, charset or "utf-8"


def _is_json_whitespace(body: bytes) -> bool:
    return body.translate(None, b" \t\r\n") == b""


def _message_body(message: Message) -> bytes:
    body = message.get("body", b"")
    if not isinstance(body, bytes):
        raise _ObservationFailure(_FailureKind.PROTOCOL)
    return body


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constants are not supported")


async def _classify_reason(value: _MaybeAwaitable[TerminalReason]) -> TerminalReason:
    reason = await _await_if_needed(value)
    if not isinstance(reason, TerminalReason):
        raise _ObservationFailure(_FailureKind.PROTOCOL)
    return reason


async def _classify_optional_reason(
    value: _MaybeAwaitable[TerminalReason | None],
) -> TerminalReason | None:
    reason = await _await_if_needed(value)
    if reason is not None and not isinstance(reason, TerminalReason):
        raise _ObservationFailure(_FailureKind.PROTOCOL)
    return reason


def _request_task_is_cancelling() -> bool:
    task = asyncio.current_task()
    return task is not None and task.cancelling() > 0


__all__ = [
    "FinalizationResult",
    "ResponseFinalizer",
    "ResponseObservation",
    "ResponseObservationMiddleware",
    "ResponseObservationSession",
    "ResponseObservationStateError",
    "ResponseStart",
    "SSEEvent",
    "TerminalReason",
    "TerminalSignal",
    "TransportResult",
    "WireMode",
    "captured_request_body",
    "enable_request_capture",
    "get_response_observation",
    "get_response_observation_session",
    "publish_response_observation",
    "response_observation_published",
]
