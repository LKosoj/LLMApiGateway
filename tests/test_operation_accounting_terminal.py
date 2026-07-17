from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Callable
from datetime import datetime, timezone
from types import MappingProxyType
from unittest.mock import patch

import pytest
from fastapi import FastAPI, Request
from starlette.background import BackgroundTask
from starlette.responses import JSONResponse, Response, StreamingResponse

from llm_gateway_core.api.v1.operation_accounting import (
    bind_streaming_operation,
    finalize_buffered_operation,
    release_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
    get_accounting_request_context,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    ResponseObservationMiddleware,
    TerminalReason,
    publish_response_observation,
    response_observation_published,
)
from llm_gateway_core.services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingReservation,
    AccountingValidationError,
    OperationCostCalculator,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.operation_accounting import (
    OperationEventProvenance,
    OperationTerminalObservation,
    parse_upstream_operation_observation,
)
from llm_gateway_core.services.stream_observation import SSEEvent
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


class _TerminalPrimary(BaseException):
    pass


class _CountingRegistry(ActiveRequestsRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.finish_calls = 0

    def finish(self, request_id: str | None) -> None:
        self.finish_calls += 1
        super().finish(request_id)


def _context() -> AccountingRequestContext:
    policy = classify_billing_policy("POST", "/v1/images/generations")
    assert policy is not None
    return AccountingRequestContext(
        method="POST",
        route_template="/v1/images/generations",
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
    )


def _observation(*, policy=None) -> OperationTerminalObservation:
    target_policy = policy if policy is not None else _context().policy
    return parse_upstream_operation_observation(
        {"usage": {"cost": 0.2}},
        policy=target_policy,
        gateway_model="image-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )


def _request(
    *,
    accounting_service=None,
) -> tuple[Request, object, object, _CountingRegistry]:
    app = FastAPI()
    registry = _CountingRegistry()
    services = make_app_services(
        active_requests_registry=registry,
        **(
            {"accounting_service": accounting_service}
            if accounting_service is not None
            else {}
        ),
    )
    services.accounting_service.release.return_value = True
    app.state.services = services
    snapshot = make_runtime_snapshot(
        cost_rate_registry={
            ("provider-a", "model-a"): ModelCostRates(1.0, 2.0)
        },
        operation_cost_calculator_registry={
            ("images_generation", "image-model"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.3,
            )
        },
    )
    registry.start(
        request_id="request-1",
        path="/v1/images/generations",
        api_key_id=7,
        operation="images_generation",
    )
    state = {
        ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: _context(),
        "llmgateway_active_request_id": "request-1",
        "llmgateway_request_id": "request-1",
        "runtime_snapshot": snapshot,
    }
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/images/generations",
            "raw_path": b"/v1/images/generations",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
            "state": state,
        }
    )
    return request, services.accounting_service, snapshot, registry


def _receipt(event) -> AccountingReceipt:
    return AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
        usage_row_id=1,
    )


def _install_successful_commit(service, *, before_return=None) -> None:
    async def commit(reservation, event):
        if before_return is not None:
            before_return(reservation, event)
        return _receipt(event)

    service.commit.side_effect = commit


async def _run_response(
    request: Request,
    response: Response,
    *,
    before_send: Callable[[dict[str, object]], None] | None = None,
) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def app(scope, receive, send) -> None:
        await response(scope, receive, send)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        if before_send is not None:
            before_send(message)
        messages.append(message)

    await ResponseObservationMiddleware(app)(request.scope, receive, send)
    return messages


def _body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _done_observer(
    observation: OperationTerminalObservation,
) -> Callable[[SSEEvent], OperationTerminalObservation | None]:
    def observe(event: SSEEvent) -> OperationTerminalObservation | None:
        return observation if event.done else None

    return observe


def test_take_owner_consumes_context_and_publishes_response_owner_once() -> None:
    request, service, snapshot, _ = _request()

    owner = take_operation_terminal_owner(request)

    assert ACCOUNTING_REQUEST_CONTEXT_STATE_KEY not in request.scope["state"]
    assert response_observation_published(request.scope)
    assert owner.accounting_service is service
    assert owner.cost_rate_registry is snapshot.cost_rate_registry
    assert (
        owner.operation_cost_calculator_registry
        is snapshot.operation_cost_calculator_registry
    )
    with pytest.raises(AccountingValidationError):
        take_operation_terminal_owner(request)


def test_publish_failure_restores_exact_auth_accounting_context() -> None:
    request, service, _, _ = _request()
    context = get_accounting_request_context(request.scope)

    with patch(
        "llm_gateway_core.api.v1.operation_accounting.publish_response_observation",
        side_effect=RuntimeError("publish failed"),
    ):
        with pytest.raises(RuntimeError, match="publish failed"):
            take_operation_terminal_owner(request)

    assert get_accounting_request_context(request.scope) is context
    assert not response_observation_published(request.scope)
    service.commit.assert_not_awaited()
    service.release.assert_not_awaited()


def test_existing_response_owner_is_rejected_before_context_handoff() -> None:
    request, service, _, _ = _request()
    context = get_accounting_request_context(request.scope)

    async def finalize(_signal) -> None:
        return None

    async def classify_json(_start, _body) -> TerminalReason:
        return TerminalReason.COMPLETE

    async def classify_sse(_start, _event) -> TerminalReason | None:
        return None

    async def classify_opaque(_start) -> TerminalReason:
        return TerminalReason.COMPLETE

    publish_response_observation(
        request.scope,
        ResponseObservation(
            finalizer=ResponseFinalizer(
                request.app.state.services.task_supervisor,
                finalize,
            ),
            classify_json=classify_json,
            classify_sse=classify_sse,
            classify_opaque=classify_opaque,
        ),
    )

    with pytest.raises(AccountingValidationError):
        take_operation_terminal_owner(request)

    assert get_accounting_request_context(request.scope) is context
    service.commit.assert_not_awaited()
    service.release.assert_not_awaited()


def test_buffered_json_commits_before_start_and_preserves_wire_response() -> None:
    async def scenario() -> None:
        request, service, _, registry = _request()
        owner = take_operation_terminal_owner(request)
        ran: list[str] = []
        response = JSONResponse(
            {"result": "exact"},
            status_code=201,
            headers={"x-test": "value"},
            background=BackgroundTask(ran.append, "done"),
        )
        sent_types: list[str] = []

        def before_commit(_reservation, event) -> None:
            assert sent_types == []
            assert event.request_id == "request-1"

        _install_successful_commit(service, before_return=before_commit)
        result = await finalize_buffered_operation(owner, response, _observation())

        assert result is response
        service.commit.assert_not_awaited()
        messages = await _run_response(
            request,
            response,
            before_send=lambda message: sent_types.append(str(message["type"])),
        )

        assert messages[0]["status"] == 201
        assert _body(messages) == response.body
        assert response.headers["x-test"] == "value"
        assert ran == ["done"]
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()
        assert registry.list_records() == []
        assert registry.finish_calls == 1

    run_async(scenario())


def test_opaque_response_uses_bound_observation_without_copying_body() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        response = Response(
            b"\x00exact-binary\xff",
            media_type="application/octet-stream",
        )
        sent = False

        def before_commit(_reservation, _event) -> None:
            assert sent is False

        def before_send(_message: dict[str, object]) -> None:
            nonlocal sent
            sent = True

        _install_successful_commit(service, before_return=before_commit)
        await finalize_buffered_operation(owner, response, _observation())
        messages = await _run_response(request, response, before_send=before_send)

        assert _body(messages) == response.body
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_http_error_releases_even_when_success_observation_was_bound() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        response = JSONResponse({"error": "upstream"}, status_code=502)
        await finalize_buffered_operation(owner, response, _observation())

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 502
        assert _body(messages) == response.body
        service.commit.assert_not_awaited()
        service.release.assert_awaited_once()

    run_async(scenario())


def test_successful_pre_release_is_a_trusted_no_charge_response() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        assert await release_operation(owner) is True
        response = JSONResponse({"status": "queued"}, status_code=202)

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 202
        assert _body(messages) == response.body
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


def test_buffered_binding_failure_releases_and_preserves_primary_error() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        wrong_policy = classify_billing_policy("POST", "/v1/pdf/convert")
        assert wrong_policy is not None
        primary = _TerminalPrimary()
        service.release.side_effect = ValueError("cleanup")

        with patch(
            "llm_gateway_core.api.v1.operation_accounting."
            "OperationTerminalOwner._bind_terminal_observation",
            side_effect=primary,
        ):
            with pytest.raises(_TerminalPrimary) as exc_info:
                await finalize_buffered_operation(
                    owner,
                    Response(b"body"),
                    _observation(policy=wrong_policy),
                )

        assert exc_info.value is primary
        service.release.assert_awaited_once()

    run_async(scenario())


def test_finalizer_normalization_failure_releases_before_commit_handoff() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        wrong_policy = classify_billing_policy("POST", "/v1/pdf/convert")
        assert wrong_policy is not None
        response = JSONResponse({"result": "body"})
        await finalize_buffered_operation(
            owner,
            response,
            _observation(policy=wrong_policy),
        )

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 503
        assert b'"code":"accounting_unavailable"' in _body(messages)
        service.commit.assert_not_awaited()
        service.release.assert_awaited_once()

    run_async(scenario())


def test_owner_release_is_one_shot_and_release_if_open_is_idempotent() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)

        await release_operation_if_open(owner)
        await release_operation_if_open(owner)
        with pytest.raises(AccountingValidationError):
            await release_operation(owner)
        with pytest.raises(AccountingValidationError):
            await finalize_buffered_operation(
                owner,
                Response(b"body"),
                _observation(),
            )
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


def test_release_if_open_does_not_mask_primary_but_surfaces_sole_cleanup_error() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        service.release.side_effect = RuntimeError("cleanup failed")
        primary = ValueError("primary failed")

        await release_operation_if_open(owner, primary_error=primary)
        service.release.assert_awaited_once()

        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        service.release.side_effect = RuntimeError("cleanup failed")
        with pytest.raises(RuntimeError, match="cleanup failed"):
            await release_operation_if_open(owner)

    run_async(scenario())


def test_false_release_result_is_an_accounting_invariant_failure() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        service.release.return_value = False

        with pytest.raises(AccountingError) as exc_info:
            await release_operation(owner)

        assert exc_info.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        service.release.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize(
    "source_chunks",
    [
        [b"data: one\n\n", b"data: [DO", b"NE]\n\n"],
        [b"data: one\r", b"\n\r\ndata: [DONE]\r\n", b"\r\n"],
        [b"data: one\n\ndata: [DONE]\n\n"],
    ],
)
def test_sse_uses_canonical_events_and_commits_before_terminal_send(
    source_chunks: list[bytes],
) -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        yielded: list[bytes] = []

        async def source() -> AsyncIterator[bytes]:
            for chunk in source_chunks:
                yield chunk

        response = StreamingResponse(source(), media_type="text/event-stream")

        def before_commit(_reservation, _event) -> None:
            assert b"[DONE]" not in b"".join(yielded)

        def before_send(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.body":
                yielded.append(bytes(message.get("body", b"")))

        _install_successful_commit(service, before_return=before_commit)
        bind_streaming_operation(owner, _done_observer(_observation()))
        messages = await _run_response(request, response, before_send=before_send)

        assert _body(messages) == b"".join(source_chunks)
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_sse_eof_without_terminal_observation_releases_and_closes_transport() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)

        async def source() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            yield b"partial"

        response = StreamingResponse(source(), media_type="text/event-stream")
        bind_streaming_operation(owner, _done_observer(_observation()))
        with pytest.raises(RuntimeError):
            await _run_response(request, response)

        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    "error",
    [RuntimeError("iterator failed"), asyncio.CancelledError()],
)
def test_sse_iterator_error_or_cancellation_after_start_releases(
    error: BaseException,
) -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)

        async def source() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            raise error

        response = StreamingResponse(source(), media_type="text/event-stream")
        bind_streaming_operation(owner, _done_observer(_observation()))

        with pytest.raises(type(error)) as exc_info:
            await _run_response(request, response)
        assert exc_info.value is error
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    "error",
    [RuntimeError("commit failed"), asyncio.CancelledError()],
)
def test_sse_commit_failure_never_competes_with_release(error: BaseException) -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        service.commit.side_effect = error

        async def source() -> AsyncIterator[bytes]:
            yield b"data: one\n\n"
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(source(), media_type="text/event-stream")
        bind_streaming_operation(owner, _done_observer(_observation()))

        with pytest.raises(type(error)) as exc_info:
            await _run_response(request, response)
        if not isinstance(error, asyncio.CancelledError):
            assert exc_info.value is error
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_sse_binding_returns_original_response_and_preserves_background() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        ran: list[str] = []

        async def source() -> AsyncIterator[bytes]:
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(
            source(),
            status_code=202,
            headers={"x-test": "value"},
            media_type="text/event-stream",
            background=BackgroundTask(ran.append, "done"),
        )
        raw_headers = response.raw_headers
        _install_successful_commit(service)

        assert bind_streaming_operation(
            owner,
            _done_observer(_observation()),
        ) is None
        messages = await _run_response(request, response)

        assert response.raw_headers is raw_headers
        assert response.headers["x-test"] == "value"
        assert messages[0]["status"] == 202
        assert ran == ["done"]
        service.commit.assert_awaited_once()

    run_async(scenario())


def test_provenance_is_applied_only_by_terminal_finalizer() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        _install_successful_commit(service)
        provenance = OperationEventProvenance(
            event_id="operation-event",
            method="POST",
            route_template="/v1/images/generations",
        )
        response = JSONResponse({"status": "succeeded"})

        await finalize_buffered_operation(
            owner,
            response,
            _observation(),
            provenance=provenance,
        )
        service.commit.assert_not_awaited()
        await _run_response(request, response)

        event = service.commit.await_args.args[1]
        assert event.event_id == "operation-event"

    run_async(scenario())


def test_chat_context_is_rejected_and_opaque_wire_uses_trusted_observation() -> None:
    request, _, _, _ = _request()
    chat_policy = classify_billing_policy("POST", "/v1/chat/completions")
    assert chat_policy is not None
    request.scope["state"][ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = (
        AccountingRequestContext(
            method="POST",
            route_template="/v1/chat/completions",
            policy=chat_policy,
            request_id="chat-request",
            reservation=AccountingReservation(
                reservation_id="chat-reservation",
                request_id="chat-request",
                api_key_id=7,
                reserved_usd=1.0,
            ),
        )
    )
    with pytest.raises(AccountingValidationError):
        take_operation_terminal_owner(request)

    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        _install_successful_commit(service)
        response = StreamingResponse(iter([b"body"]))
        result = await finalize_buffered_operation(owner, response, _observation())

        assert result is response
        messages = await _run_response(request, response)
        assert _body(messages) == b"body"
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_none_commit_receipt_fails_closed_without_release_race() -> None:
    async def scenario() -> None:
        request, service, _, _ = _request()
        owner = take_operation_terminal_owner(request)
        service.commit.return_value = None
        response = JSONResponse({"result": "body"})
        await finalize_buffered_operation(owner, response, _observation())

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 503
        assert b'"code":"accounting_unavailable"' in _body(messages)
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())
