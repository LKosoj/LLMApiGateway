from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field, replace

import pytest
from fastapi import FastAPI, Request
from starlette.types import Message, Scope

from llm_gateway_core.middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    ResponseObservationMiddleware,
    ResponseStart,
    SSEEvent,
    TerminalReason,
    TerminalSignal,
    TransportResult,
    WireMode,
    publish_response_observation,
)
from llm_gateway_core.services.stream_observation import (
    StreamObservationCapacity,
)
from llm_gateway_core.services.task_supervisor import (
    TaskSupervisor,
    TaskSupervisorStateError,
)
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services


class _IteratorFailure(RuntimeError):
    pass


class _SendFailure(RuntimeError):
    pass


@dataclass
class _Probe:
    signals: list[TerminalSignal] = field(default_factory=list)
    json_payloads: list[tuple[ResponseStart, bytes]] = field(default_factory=list)
    sse_events: list[tuple[ResponseStart, SSEEvent]] = field(default_factory=list)
    opaque_starts: list[ResponseStart] = field(default_factory=list)
    body_chunks: list[tuple[WireMode, bytes]] = field(default_factory=list)
    transports: list[TransportResult] = field(default_factory=list)
    order: list[tuple[str, object]] = field(default_factory=list)


def _scope(app: FastAPI) -> Scope:
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/observed",
        "raw_path": b"/observed",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "app": app,
        "state": {"llmgateway_request_id": "request-1"},
    }


def _observation(
    services: object,
    probe: _Probe,
    *,
    finalize_hook: Callable[[TerminalSignal], Awaitable[None]] | None = None,
) -> ResponseObservation:
    async def finalize(signal: TerminalSignal) -> object:
        probe.signals.append(signal)
        probe.order.append(("finalize", signal.reason))
        if finalize_hook is not None:
            await finalize_hook(signal)
        return object()

    def classify_json(start: ResponseStart, payload: bytes) -> TerminalReason:
        probe.json_payloads.append((start, payload))
        return TerminalReason.COMPLETE

    def classify_sse(
        start: ResponseStart,
        event: SSEEvent,
    ) -> TerminalReason | None:
        probe.sse_events.append((start, event))
        return TerminalReason.COMPLETE if event.done else None

    def classify_opaque(start: ResponseStart) -> TerminalReason:
        probe.opaque_starts.append(start)
        return TerminalReason.COMPLETE

    def observe_body_chunk(start: ResponseStart, body: bytes) -> None:
        probe.body_chunks.append((start.wire_mode, body))

    def transport_finished(result: TransportResult) -> None:
        probe.transports.append(result)
        probe.order.append(("transport", result.reason))

    return ResponseObservation(
        finalizer=ResponseFinalizer(services.task_supervisor, finalize),
        classify_json=classify_json,
        classify_sse=classify_sse,
        classify_opaque=classify_opaque,
        observe_body_chunk=observe_body_chunk,
        transport_finished=transport_finished,
    )


async def _run_observed(
    source_messages: list[Message],
    *,
    services: object,
    observation: ResponseObservation,
    probe: _Probe,
    app_error: BaseException | None = None,
    fail_send_at: int | None = None,
) -> list[Message]:
    app_state = FastAPI()
    app_state.state.services = services
    scope = _scope(app_state)
    sent: list[Message] = []
    send_attempt = 0

    async def app(_scope: Scope, _receive, send) -> None:
        for message in source_messages:
            await send(dict(message))
        if app_error is not None:
            raise app_error

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        nonlocal send_attempt
        send_attempt += 1
        marker: object = (
            "start"
            if message.get("type") == "http.response.start"
            else message.get("body", b"")
        )
        probe.order.append(("send", marker))
        if fail_send_at == send_attempt:
            raise _SendFailure("transport send failed")
        sent.append(dict(message))

    async def prepare(request: Request) -> None:
        publish_response_observation(request.scope, observation)

    middleware = ResponseObservationMiddleware(app, request_preparer=prepare)
    await middleware(scope, receive, send)
    return sent


def _start(content_type: bytes, *, status: int = 200) -> Message:
    return {
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", content_type), (b"x-exact", b"yes")],
    }


def _body(messages: list[Message]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _assert_capacity_zero(services: object) -> None:
    for capacity in (
        services.stream_observation_capacity,
        services.json_response_capacity,
    ):
        snapshot = capacity.snapshot
        assert snapshot.active_items == 0
        assert snapshot.active_bytes == 0
        assert snapshot.waiters == 0


@pytest.mark.parametrize(
    ("content_type", "payload", "expected_mode"),
    [
        (b"application/json", b"{}", WireMode.JSON),
        (
            b"Application/Problem+Json; Charset=UTF-8",
            b'{"detail":"exact"}',
            WireMode.JSON,
        ),
        (b"text/event-stream; charset=iso-8859-1", b"data: [DONE]\n\n", WireMode.SSE),
        (b"text/json; charset=not-a-codec", b"opaque bytes", WireMode.OPAQUE),
    ],
)
def test_normalized_mime_selects_json_suffix_json_sse_or_opaque(
    content_type: bytes,
    payload: bytes,
    expected_mode: WireMode,
) -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)

        sent = await _run_observed(
            [
                _start(content_type),
                {"type": "http.response.body", "body": payload},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 200
        assert _body(sent) == payload
        assert probe.signals[0].reason is TerminalReason.COMPLETE
        assert probe.signals[0].start is not None
        assert probe.signals[0].start.wire_mode is expected_mode
        assert [mode for mode, _chunk in probe.body_chunks] == [expected_mode]
        assert len(probe.transports) == 1
        assert probe.transports[0] == TransportResult(
            reason=TerminalReason.COMPLETE,
            response_started=True,
            response_completed=True,
        )
        _assert_capacity_zero(services)

    run_async(scenario())


def test_json_validation_preserves_exact_headers_bytes_and_chunk_boundaries() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)
        source = [
            _start(b"application/vnd.gateway+json; charset=utf-8", status=201),
            {
                "type": "http.response.body",
                "body": b'{"message":"',
                "more_body": True,
            },
            {
                "type": "http.response.body",
                "body": "точно".encode(),
                "more_body": True,
            },
            {"type": "http.response.body", "body": b'"}'},
        ]

        sent = await _run_observed(
            source,
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent == source
        assert probe.json_payloads == [
            (probe.signals[0].start, _body(source))  # type: ignore[arg-type]
        ]
        assert probe.body_chunks == [
            (WireMode.JSON, source[1]["body"]),
            (WireMode.JSON, source[2]["body"]),
            (WireMode.JSON, source[3]["body"]),
        ]
        assert probe.order.index(("finalize", TerminalReason.COMPLETE)) < probe.order.index(
            ("send", "start")
        )
        assert len(probe.transports) == 1
        _assert_capacity_zero(services)

    run_async(scenario())


def test_pending_response_start_freezes_validated_headers() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        app_state = FastAPI()
        app_state.state.services = services
        scope = _scope(app_state)
        headers = [(b"content-type", b"application/json")]
        sent: list[Message] = []

        async def app(_scope: Scope, _receive, send) -> None:
            await send(
                {
                    "type": "http.response.start",
                    "status": 200,
                    "headers": headers,
                }
            )
            headers[0] = (b"content-type", b"text/event-stream")
            await send({"type": "http.response.body", "body": b"{}"})

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(dict(message))

        async def prepare(request: Request) -> None:
            publish_response_observation(
                request.scope,
                _observation(services, probe),
            )

        middleware = ResponseObservationMiddleware(app, request_preparer=prepare)
        await middleware(scope, receive, send)

        assert sent[0]["headers"] == [(b"content-type", b"application/json")]
        assert _body(sent) == b"{}"
        assert probe.signals[0].start is not None
        assert probe.signals[0].start.wire_mode is WireMode.JSON
        _assert_capacity_zero(services)

    run_async(scenario())


@pytest.mark.parametrize(
    "content_types",
    [
        (b"application/json", b"text/event-stream"),
        (b"application/json", b"application/json"),
    ],
)
def test_duplicate_content_type_headers_return_protocol_envelope(
    content_types: tuple[bytes, bytes],
) -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        start = _start(content_types[0])
        start["headers"] = [
            (b"content-type", content_types[0]),
            (b"content-type", content_types[1]),
        ]

        sent = await _run_observed(
            [start, {"type": "http.response.body", "body": b"{}"}],
            services=services,
            observation=_observation(services, probe),
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("payload", "json_limit"),
    [
        (b'{"broken":', 1024),
        (b'{"oversized":true}', 8),
        (b'{"value":NaN}', 1024),
        (b'{"value":Infinity}', 1024),
        (b'{"value":-Infinity}', 1024),
    ],
)
def test_malformed_or_oversized_json_before_start_returns_protocol_envelope(
    payload: bytes,
    json_limit: int,
) -> None:
    async def scenario() -> None:
        json_capacity = StreamObservationCapacity(max_items=4, max_bytes=2_048)
        services = make_app_services(
            json_response_capacity=json_capacity,
            json_response_max_bytes=json_limit,
        )
        probe = _Probe()
        observation = _observation(services, probe)

        sent = await _run_observed(
            [
                _start(b"application/json"),
                {"type": "http.response.body", "body": payload},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.PROTOCOL_ERROR,
                response_started=False,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_deep_json_returns_protocol_envelope() -> None:
    async def scenario() -> None:
        max_bytes = 100_000
        services = make_app_services(
            json_response_capacity=StreamObservationCapacity(
                max_items=4,
                max_bytes=max_bytes,
            ),
            json_response_max_bytes=max_bytes,
        )
        probe = _Probe()
        payload = b"[" * 20_000 + b"0" + b"]" * 20_000

        sent = await _run_observed(
            [
                _start(b"application/json"),
                {"type": "http.response.body", "body": payload},
            ],
            services=services,
            observation=_observation(services, probe),
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_non_text_json_charset_returns_protocol_envelope() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        sent = await _run_observed(
            [
                _start(b"application/json; charset=base64_codec"),
                {"type": "http.response.body", "body": b"e30="},
            ],
            services=services,
            observation=_observation(services, probe),
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_oversized_sse_event_before_start_returns_protocol_envelope() -> None:
    async def scenario() -> None:
        stream_capacity = StreamObservationCapacity(max_items=4, max_bytes=128)
        services = make_app_services(
            stream_observation_capacity=stream_capacity,
            stream_event_max_bytes=8,
        )
        probe = _Probe()
        observation = _observation(services, probe)

        sent = await _run_observed(
            [
                _start(b"text/event-stream"),
                {
                    "type": "http.response.body",
                    "body": b"data: oversized\n\n",
                },
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        assert len(probe.transports) == 1
        _assert_capacity_zero(services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("content_type", "payload", "mode"),
    [
        (b"application/json", b"{}", WireMode.JSON),
        (b"text/event-stream", b"data: event\n\n", WireMode.SSE),
        (b"application/octet-stream", b"opaque", WireMode.OPAQUE),
    ],
)
def test_protocol_classifier_reason_before_start_returns_protocol_envelope(
    content_type: bytes,
    payload: bytes,
    mode: WireMode,
) -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)

        if mode is WireMode.JSON:
            observation = replace(
                observation,
                classify_json=lambda _start, _payload: TerminalReason.PROTOCOL_ERROR,
            )
        elif mode is WireMode.SSE:
            observation = replace(
                observation,
                classify_sse=lambda _start, _event: TerminalReason.PROTOCOL_ERROR,
            )
        else:
            observation = replace(
                observation,
                classify_opaque=lambda _start: TerminalReason.PROTOCOL_ERROR,
            )

        sent = await _run_observed(
            [
                _start(content_type),
                {"type": "http.response.body", "body": payload},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.PROTOCOL_ERROR,
                response_started=False,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_sse_classifier_exception_before_start_returns_protocol_envelope() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        def reject_event(_start: ResponseStart, _event: SSEEvent) -> None:
            raise ValueError("invalid SSE dialect")

        observation = replace(
            _observation(services, probe),
            classify_sse=reject_event,
        )
        sent = await _run_observed(
            [
                _start(b"text/event-stream"),
                {"type": "http.response.body", "body": b"data: invalid\n\n"},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_body_observer_failure_before_start_returns_accounting_envelope() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        def fail_observer(_start: ResponseStart, _body: bytes) -> None:
            raise TaskSupervisorStateError("supervisor is closed")

        observation = replace(
            _observation(services, probe),
            observe_body_chunk=fail_observer,
        )
        sent = await _run_observed(
            [
                _start(b"application/json"),
                {"type": "http.response.body", "body": b"{}"},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 503
        assert json.loads(_body(sent))["error"]["code"] == "accounting_unavailable"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.UPSTREAM_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


@pytest.mark.parametrize("content_type", [b"application/json", b"text/event-stream"])
def test_empty_retained_body_frames_are_bounded(content_type: bytes) -> None:
    async def scenario() -> None:
        capacity = StreamObservationCapacity(max_items=2, max_bytes=128)
        services = make_app_services(
            stream_observation_capacity=capacity,
            json_response_capacity=capacity,
        )
        probe = _Probe()

        sent = await _run_observed(
            [
                _start(content_type),
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": True,
                },
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": True,
                },
                {
                    "type": "http.response.body",
                    "body": b"",
                    "more_body": True,
                },
            ],
            services=services,
            observation=_observation(services, probe),
            probe=probe,
        )

        assert sent[0]["status"] == 503
        assert json.loads(_body(sent))["error"]["code"] == "accounting_unavailable"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.UPSTREAM_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_finalizer_cancellation_without_request_cancellation_returns_503() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        async def cancel_finalizer(_signal: TerminalSignal) -> None:
            raise asyncio.CancelledError

        observation = _observation(
            services,
            probe,
            finalize_hook=cancel_finalizer,
        )
        sent = await _run_observed(
            [
                _start(b"application/json"),
                {"type": "http.response.body", "body": b"{}"},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 503
        assert json.loads(_body(sent))["error"]["code"] == "accounting_unavailable"
        assert [signal.reason for signal in probe.signals] == [TerminalReason.COMPLETE]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_split_sse_done_is_finalized_before_any_terminal_chunk_is_sent() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)
        source = [
            _start(b"text/event-stream"),
            {
                "type": "http.response.body",
                "body": b'data: {"delta":"one"}\n\n',
                "more_body": True,
            },
            {
                "type": "http.response.body",
                "body": b"data: [DO",
                "more_body": True,
            },
            {"type": "http.response.body", "body": b"NE]\n\n"},
        ]

        sent = await _run_observed(
            source,
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent == source
        assert [event.data for _start, event in probe.sse_events] == [
            '{"delta":"one"}',
            "[DONE]",
        ]
        assert [event.done for _start, event in probe.sse_events] == [False, True]
        finalize_index = probe.order.index(("finalize", TerminalReason.COMPLETE))
        assert finalize_index < probe.order.index(("send", b"data: [DO"))
        assert finalize_index < probe.order.index(("send", b"NE]\n\n"))
        assert len(probe.transports) == 1
        _assert_capacity_zero(services)

    run_async(scenario())


def test_sse_terminal_with_trailing_bytes_before_start_returns_protocol_error() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        sent = await _run_observed(
            [
                _start(b"text/event-stream"),
                {
                    "type": "http.response.body",
                    "body": b"data: [DONE]\n\n\xff",
                },
            ],
            services=services,
            observation=_observation(services, probe),
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


@pytest.mark.parametrize(
    "body",
    [
        b"data: stop\n\ndata: tail\n\n",
        b"data: stop\n\ndata: tail",
        b"data: stop\n\n: trailing comment\n\n",
        b"data: stop\n\n\n\n",
    ],
)
def test_semantic_sse_terminal_with_event_tail_in_same_chunk_is_rejected(
    body: bytes,
) -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()

        def classify_sse(
            start: ResponseStart,
            event: SSEEvent,
        ) -> TerminalReason | None:
            probe.sse_events.append((start, event))
            return TerminalReason.COMPLETE if event.data == "stop" else None

        observation = replace(
            _observation(services, probe),
            classify_sse=classify_sse,
        )

        sent = await _run_observed(
            [
                _start(b"text/event-stream"),
                {
                    "type": "http.response.body",
                    "body": body,
                },
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [event.data for _start, event in probe.sse_events] == ["stop"]
        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.PROTOCOL_ERROR
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_nonempty_sse_chunk_after_terminal_closes_without_sending_tail() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        terminal = b"data: [DONE]\n\n"

        with pytest.raises(RuntimeError):
            await _run_observed(
                [
                    _start(b"text/event-stream"),
                    {
                        "type": "http.response.body",
                        "body": terminal,
                        "more_body": True,
                    },
                    {"type": "http.response.body", "body": b"late"},
                ],
                services=services,
                observation=_observation(services, probe),
                probe=probe,
            )

        sent_bodies = [
            value for kind, value in probe.order if kind == "send" and value != "start"
        ]
        assert sent_bodies == [terminal]
        assert [signal.reason for signal in probe.signals] == [TerminalReason.COMPLETE]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.PROTOCOL_ERROR,
                response_started=True,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_sse_without_terminal_before_start_returns_protocol_envelope() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)

        sent = await _run_observed(
            [
                _start(b"text/event-stream"),
                {"type": "http.response.body", "body": b"data: partial"},
            ],
            services=services,
            observation=observation,
            probe=probe,
        )

        assert sent[0]["status"] == 502
        assert json.loads(_body(sent))["error"]["code"] == "upstream_protocol_error"
        assert [signal.reason for signal in probe.signals] == [TerminalReason.EMPTY]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.PROTOCOL_ERROR,
                response_started=False,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_sse_without_terminal_after_start_closes_without_second_envelope() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)
        source = [
            _start(b"text/event-stream"),
            {
                "type": "http.response.body",
                "body": b"data: nonterminal\n\n",
                "more_body": True,
            },
            {"type": "http.response.body", "body": b""},
        ]

        with pytest.raises(RuntimeError):
            await _run_observed(
                source,
                services=services,
                observation=observation,
                probe=probe,
            )

        sent_bodies = [
            value for kind, value in probe.order if kind == "send" and value != "start"
        ]
        assert sent_bodies == [b"data: nonterminal\n\n"]
        assert [signal.reason for signal in probe.signals] == [TerminalReason.EMPTY]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.PROTOCOL_ERROR,
                response_started=True,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_response_finalizer_duplicate_signals_share_one_result_identity() -> None:
    async def scenario() -> None:
        supervisor = TaskSupervisor()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[TerminalSignal] = []

        async def callback(signal: TerminalSignal) -> str:
            calls.append(signal)
            entered.set()
            await release.wait()
            return "receipt"

        finalizer = ResponseFinalizer(supervisor, callback)
        first_signal = TerminalSignal(TerminalReason.COMPLETE)
        duplicate_signal = TerminalSignal(TerminalReason.UPSTREAM_ERROR)
        first = asyncio.create_task(finalizer.finalize(first_signal))
        await entered.wait()
        duplicate = asyncio.create_task(finalizer.finalize(duplicate_signal))
        release.set()

        first_result, duplicate_result = await asyncio.gather(first, duplicate)

        assert first_result is duplicate_result
        assert first_result.signal is first_signal
        assert calls == [first_signal]
        assert finalizer.signal is first_signal
        await supervisor.close()

    run_async(scenario())


def test_response_finalizer_waiter_cancellation_does_not_cancel_owned_task() -> None:
    async def scenario() -> None:
        supervisor = TaskSupervisor()
        entered = asyncio.Event()
        release = asyncio.Event()
        calls: list[TerminalSignal] = []

        async def callback(signal: TerminalSignal) -> str:
            calls.append(signal)
            entered.set()
            await release.wait()
            return "receipt"

        finalizer = ResponseFinalizer(supervisor, callback)
        first_signal = TerminalSignal(TerminalReason.COMPLETE)
        cancelled_waiter = asyncio.create_task(finalizer.finalize(first_signal))
        await entered.wait()
        cancelled_waiter.cancel()
        with pytest.raises(asyncio.CancelledError):
            await cancelled_waiter

        surviving_waiter = asyncio.create_task(
            finalizer.finalize(TerminalSignal(TerminalReason.CANCELLED))
        )
        release.set()
        result = await surviving_waiter

        assert result.signal is first_signal
        assert result.value == "receipt"
        assert calls == [first_signal]
        await supervisor.close()

    run_async(scenario())


def test_response_finalizer_task_creation_failure_aborts_owner_once() -> None:
    async def scenario() -> None:
        supervisor = TaskSupervisor()
        await supervisor.close()
        callback_calls = 0
        abort_calls = 0

        async def callback(_signal: TerminalSignal) -> object:
            nonlocal callback_calls
            callback_calls += 1
            return object()

        async def abort() -> None:
            nonlocal abort_calls
            abort_calls += 1

        finalizer = ResponseFinalizer(supervisor, callback, abort=abort)
        first_signal = TerminalSignal(TerminalReason.COMPLETE)
        with pytest.raises(TaskSupervisorStateError):
            await finalizer.finalize(first_signal)
        with pytest.raises(TaskSupervisorStateError):
            await finalizer.finalize(TerminalSignal(TerminalReason.CANCELLED))

        assert finalizer.signal is first_signal
        assert callback_calls == 0
        assert abort_calls == 1

    run_async(scenario())


def test_iterator_failure_before_response_start_finalizes_and_reports_transport_once() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)

        with pytest.raises(_IteratorFailure, match="iterator failed"):
            await _run_observed(
                [],
                services=services,
                observation=observation,
                probe=probe,
                app_error=_IteratorFailure("iterator failed"),
            )

        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.UPSTREAM_ERROR
        ]
        assert probe.signals[0].start is None
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.UPSTREAM_ERROR,
                response_started=False,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_iterator_failure_after_sse_start_finalizes_and_releases_capacity() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)
        source = [
            _start(b"text/event-stream"),
            {
                "type": "http.response.body",
                "body": b"data: nonterminal\n\n",
                "more_body": True,
            },
        ]

        with pytest.raises(_IteratorFailure, match="iterator failed"):
            await _run_observed(
                source,
                services=services,
                observation=observation,
                probe=probe,
                app_error=_IteratorFailure("iterator failed"),
            )

        assert [signal.reason for signal in probe.signals] == [
            TerminalReason.UPSTREAM_ERROR
        ]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.UPSTREAM_ERROR,
                response_started=True,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())


def test_send_failure_keeps_first_finalization_and_reports_transport_once() -> None:
    async def scenario() -> None:
        services = make_app_services()
        probe = _Probe()
        observation = _observation(services, probe)

        with pytest.raises(_SendFailure, match="transport send failed"):
            await _run_observed(
                [
                    _start(b"application/json"),
                    {"type": "http.response.body", "body": b'{"ok":true}'},
                ],
                services=services,
                observation=observation,
                probe=probe,
                fail_send_at=1,
            )

        assert [signal.reason for signal in probe.signals] == [TerminalReason.COMPLETE]
        assert probe.transports == [
            TransportResult(
                reason=TerminalReason.UPSTREAM_ERROR,
                response_started=False,
                response_completed=False,
            )
        ]
        _assert_capacity_zero(services)

    run_async(scenario())
