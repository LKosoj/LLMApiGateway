from __future__ import annotations

import asyncio
from collections.abc import Callable

import pytest
from fastapi import FastAPI, Request

import llm_gateway_core.api.v1.chat_accounting as chat_accounting
from llm_gateway_core.api.v1.chat_accounting import (
    ChatStreamDialect,
    bind_chat_request,
    build_chat_response_observation,
    install_chat_terminal_handoff,
    release_chat,
    take_chat_terminal_owner,
)
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseStart,
    TerminalReason,
    TerminalSignal,
    WireMode,
)
from llm_gateway_core.services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingReservation,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
    CostSource,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.chat_accounting import ChatTerminalObservation
from llm_gateway_core.services.stream_observation import SSEEvent
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _context(route_template: str) -> AccountingRequestContext:
    policy = classify_billing_policy("POST", route_template)
    assert policy is not None
    return AccountingRequestContext(
        method="POST",
        route_template=route_template,
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
    )


def _terminal_observation() -> ChatTerminalObservation:
    return ChatTerminalObservation(
        top_provider="provider-a",
        top_model="model-a",
        components=(
            BillingComponent(
                provider="provider-a",
                model="model-a",
                usage=AccountingUsage(
                    prompt_tokens=2,
                    completion_tokens=3,
                    total_tokens=5,
                    cost=0.01,
                ),
                cost_source=CostSource.UPSTREAM,
            ),
        ),
    )


def _receipt(event) -> AccountingReceipt:
    return AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
        usage_row_id=1,
    )


def _request(route_template: str = "/v1/chat/completions"):
    app = FastAPI()
    services = make_app_services()
    services.accounting_service.release.return_value = True
    app.state.services = services
    snapshot = make_runtime_snapshot(
        cost_rate_registry={
            ("provider-a", "model-a"): ModelCostRates(1.0, 2.0),
        }
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": route_template,
            "raw_path": route_template.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
            "state": {
                ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: _context(route_template),
                "runtime_snapshot": snapshot,
            },
        }
    )
    return request, services, snapshot


def _build_observation(
    *,
    route_template: str = "/v1/chat/completions",
    streaming: bool = False,
    dialect: ChatStreamDialect = ChatStreamDialect.OPENAI,
):
    request, services, snapshot = _request(route_template)
    owner = take_chat_terminal_owner(request)
    handoff = install_chat_terminal_handoff(request)
    bound = False

    def bind() -> None:
        nonlocal bound
        if bound:
            return
        bind_chat_request(
            owner,
            gateway_model="gateway-model",
            streaming=streaming,
            dialect=dialect,
        )
        bound = True

    observation = build_chat_response_observation(
        owner,
        handoff,
        services,
        bind_request=bind,
    )
    return request, services, snapshot, owner, handoff, observation


def _start(status: int, mode: WireMode) -> ResponseStart:
    content_type = {
        WireMode.JSON: b"application/json",
        WireMode.SSE: b"text/event-stream",
        WireMode.OPAQUE: b"application/octet-stream",
    }[mode]
    return ResponseStart(
        status_code=status,
        headers=((b"content-type", content_type),),
        wire_mode=mode,
        charset="utf-8",
    )


@pytest.mark.parametrize(
    "route_template",
    ["/v1/chat/completions", "/v1/messages", "/v1/responses"],
)
def test_take_owner_consumes_context_and_captures_exact_dependencies(
    route_template: str,
) -> None:
    request, services, snapshot = _request(route_template)

    owner = take_chat_terminal_owner(request)

    assert ACCOUNTING_REQUEST_CONTEXT_STATE_KEY not in request.scope["state"]
    assert owner.accounting_service is services.accounting_service
    assert owner.cost_rate_registry is snapshot.cost_rate_registry
    with pytest.raises(AccountingValidationError):
        _ = owner.gateway_model
    with pytest.raises(AccountingValidationError):
        take_chat_terminal_owner(request)


def test_complete_json_lazy_binds_and_duplicate_finalize_commits_once() -> None:
    async def scenario() -> None:
        _request_value, services, _, owner, handoff, observation = (
            _build_observation()
        )
        handoff.publish(_terminal_observation())

        async def commit(_reservation, event):
            return _receipt(event)

        services.accounting_service.commit.side_effect = commit
        start = _start(200, WireMode.JSON)
        assert observation.classify_json(start, b"{}") is TerminalReason.COMPLETE
        assert owner.gateway_model == "gateway-model"

        first, second = await asyncio.gather(
            observation.finalizer.finalize(
                TerminalSignal(TerminalReason.COMPLETE, start)
            ),
            observation.finalizer.finalize(
                TerminalSignal(TerminalReason.CLIENT_DISCONNECT, start)
            ),
        )

        assert first is second
        assert first.signal.reason is TerminalReason.COMPLETE
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    ("dialect", "route_template", "events"),
    [
        (
            ChatStreamDialect.OPENAI,
            "/v1/chat/completions",
            (
                SSEEvent('{"choices":[]}'),
                SSEEvent("[DONE]", done=True),
            ),
        ),
        (
            ChatStreamDialect.ANTHROPIC,
            "/v1/messages",
            (
                SSEEvent('{"type":"message_delta"}', event="message_delta"),
                SSEEvent('{"type":"message_stop"}', event="message_stop"),
            ),
        ),
        (
            ChatStreamDialect.RESPONSES,
            "/v1/responses",
            (
                SSEEvent(
                    '{"type":"response.output_text.delta"}',
                    event="response.output_text.delta",
                ),
                SSEEvent(
                    '{"type":"response.completed"}',
                    event="response.completed",
                ),
                SSEEvent("[DONE]", done=True),
            ),
        ),
    ],
)
def test_canonical_sse_dialects_commit_only_on_terminal_event(
    dialect: ChatStreamDialect,
    route_template: str,
    events: tuple[SSEEvent, ...],
) -> None:
    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation(
            route_template=route_template,
            streaming=True,
            dialect=dialect,
        )
        handoff.publish(_terminal_observation())

        async def commit(_reservation, event):
            return _receipt(event)

        services.accounting_service.commit.side_effect = commit
        start = _start(200, WireMode.SSE)
        for event in events[:-1]:
            assert observation.classify_sse(start, event) is None
        assert observation.classify_sse(start, events[-1]) is TerminalReason.COMPLETE
        await observation.finalizer.finalize(
            TerminalSignal(TerminalReason.COMPLETE, start)
        )

        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_openai_sse_null_keepalive_is_ignored() -> None:
    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation(
            route_template="/v1/chat/completions",
            streaming=True,
            dialect=ChatStreamDialect.OPENAI,
        )
        handoff.publish(_terminal_observation())
        start = _start(200, WireMode.SSE)

        assert observation.classify_sse(start, SSEEvent("null")) is None
        assert (
            observation.classify_sse(start, SSEEvent("[DONE]", done=True))
            is TerminalReason.COMPLETE
        )
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    ("dialect", "route_template", "events"),
    [
        (
            ChatStreamDialect.OPENAI,
            "/v1/chat/completions",
            (SSEEvent('{"error":{"message":"failed"}}'),),
        ),
        (
            ChatStreamDialect.ANTHROPIC,
            "/v1/messages",
            (SSEEvent('{"type":"error"}', event="error"),),
        ),
        (
            ChatStreamDialect.RESPONSES,
            "/v1/responses",
            (
                SSEEvent('{"type":"response.failed"}'),
                SSEEvent("[DONE]", done=True),
            ),
        ),
    ],
)
def test_canonical_sse_error_dialects_release_on_terminal_event(
    dialect: ChatStreamDialect,
    route_template: str,
    events: tuple[SSEEvent, ...],
) -> None:
    async def scenario() -> None:
        _, services, _, _, _, observation = _build_observation(
            route_template=route_template,
            streaming=True,
            dialect=dialect,
        )
        start = _start(200, WireMode.SSE)
        for event in events[:-1]:
            assert observation.classify_sse(start, event) is None
        assert (
            observation.classify_sse(start, events[-1])
            is TerminalReason.UPSTREAM_ERROR
        )
        await observation.finalizer.finalize(
            TerminalSignal(TerminalReason.UPSTREAM_ERROR, start)
        )

        services.accounting_service.release.assert_awaited_once()
        services.accounting_service.commit.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    ("reason", "status"),
    [
        (TerminalReason.COMPLETE, 400),
        (TerminalReason.UPSTREAM_ERROR, 200),
        (TerminalReason.PROTOCOL_ERROR, 200),
        (TerminalReason.EMPTY, 200),
    ],
)
def test_rule_a_releases_every_non_success_terminal(
    reason: TerminalReason,
    status: int,
) -> None:
    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation()
        handoff.publish(_terminal_observation())
        start = _start(status, WireMode.JSON)

        result = await observation.finalizer.finalize(
            TerminalSignal(reason, start)
        )

        assert result.value is False
        services.accounting_service.release.assert_awaited_once()
        services.accounting_service.commit.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    "reason",
    [TerminalReason.CLIENT_DISCONNECT, TerminalReason.CANCELLED],
)
def test_rule_b_commits_partial_usage_for_disconnect_or_cancel_below_400(
    reason: TerminalReason,
) -> None:
    """CLIENT_DISCONNECT/CANCELLED terminals with a successful upstream status
    (< 400) now commit the already-published observation instead of releasing
    the reservation: the gateway already paid upstream for whatever tokens
    were generated, so they must not be dropped silently."""

    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation()
        handoff.publish(_terminal_observation())

        async def commit(_reservation, event):
            return _receipt(event)

        services.accounting_service.commit.side_effect = commit
        start = _start(200, WireMode.JSON)
        # Bind the request the way a real dispatch would (via classify_json)
        # before the terminal signal arrives.
        observation.classify_json(start, b"{}")

        result = await observation.finalizer.finalize(
            TerminalSignal(reason, start)
        )

        assert isinstance(result.value, AccountingReceipt)
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_success_without_typed_handoff_releases_and_fails_closed() -> None:
    async def scenario() -> None:
        _, services, _, _, _handoff, observation = _build_observation()
        start = _start(200, WireMode.JSON)
        assert observation.classify_json(start, b"{}") is TerminalReason.COMPLETE

        with pytest.raises(AccountingValidationError):
            await observation.finalizer.finalize(
                TerminalSignal(TerminalReason.COMPLETE, start)
            )

        services.accounting_service.release.assert_awaited_once()
        services.accounting_service.commit.assert_not_awaited()

    run_async(scenario())


def test_normalization_failure_releases_but_commit_failure_does_not() -> None:
    async def run_case(
        failure_factory: Callable[[object], BaseException],
        *,
        expect_release: bool,
    ) -> None:
        _, services, _, _, handoff, observation = _build_observation()
        handoff.publish(_terminal_observation())
        start = _start(200, WireMode.JSON)
        observation.classify_json(start, b"{}")
        failure = failure_factory(services)

        with pytest.raises(type(failure)) as caught:
            await observation.finalizer.finalize(
                TerminalSignal(TerminalReason.COMPLETE, start)
            )

        assert caught.value is failure
        if expect_release:
            services.accounting_service.release.assert_awaited_once()
        else:
            services.accounting_service.release.assert_not_awaited()

    normalization_failure = AccountingValidationError()
    commit_failure = RuntimeError("commit failed")

    async def scenario() -> None:
        with pytest.MonkeyPatch.context() as monkeypatch:
            monkeypatch.setattr(
                chat_accounting,
                "build_chat_accounting_event",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    normalization_failure
                ),
            )
            await run_case(lambda _services: normalization_failure, expect_release=True)

        def fail_commit(services) -> BaseException:
            services.accounting_service.commit.side_effect = commit_failure
            return commit_failure

        await run_case(fail_commit, expect_release=False)

    run_async(scenario())


@pytest.mark.parametrize(
    ("dialect", "event"),
    [
        (ChatStreamDialect.OPENAI, SSEEvent("not-json")),
        (ChatStreamDialect.OPENAI, SSEEvent('{"value":NaN}')),
        (
            ChatStreamDialect.ANTHROPIC,
            SSEEvent('{"type":"other"}', event="message_stop"),
        ),
        (ChatStreamDialect.RESPONSES, SSEEvent("[DONE]", done=True)),
    ],
)
def test_malformed_or_mismatched_canonical_sse_fails_classification(
    dialect: ChatStreamDialect,
    event: SSEEvent,
) -> None:
    _, services, _, _, _, observation = _build_observation(
        streaming=True,
        dialect=dialect,
    )

    assert (
        observation.classify_sse(_start(200, WireMode.SSE), event)
        is TerminalReason.PROTOCOL_ERROR
    )

    services.accounting_service.commit.assert_not_awaited()
    services.accounting_service.release.assert_not_awaited()


def test_request_wire_mode_mismatch_is_protocol_terminal() -> None:
    _, _, _, _, _, json_observation = _build_observation(streaming=True)
    assert (
        json_observation.classify_json(_start(200, WireMode.JSON), b"{}")
        is TerminalReason.PROTOCOL_ERROR
    )

    _, _, _, _, _, sse_observation = _build_observation(streaming=False)
    assert (
        sse_observation.classify_sse(
            _start(200, WireMode.SSE),
            SSEEvent("[DONE]", done=True),
        )
        is TerminalReason.PROTOCOL_ERROR
    )


def test_owner_is_one_shot_and_legacy_response_wrappers_are_absent() -> None:
    async def scenario() -> None:
        request, services, _ = _request()
        owner = take_chat_terminal_owner(request)
        assert await release_chat(owner) is True
        with pytest.raises(AccountingValidationError):
            await release_chat(owner)
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())
    assert not hasattr(chat_accounting, "finalize_buffered_chat")
    assert not hasattr(chat_accounting, "wrap_streaming_chat")
    assert not hasattr(chat_accounting, "_ChatAccountingStreamingResponse")


def test_none_commit_receipt_and_false_release_fail_closed() -> None:
    async def scenario() -> None:
        _, services, _, _, handoff, observation = _build_observation()
        handoff.publish(_terminal_observation())
        services.accounting_service.commit.return_value = None
        start = _start(200, WireMode.JSON)
        observation.classify_json(start, b"{}")
        with pytest.raises(AccountingError) as commit_error:
            await observation.finalizer.finalize(
                TerminalSignal(TerminalReason.COMPLETE, start)
            )
        assert commit_error.value.code is AccountingErrorCode.ACCOUNTING_FAILED

        request, services, _ = _request()
        owner = take_chat_terminal_owner(request)
        services.accounting_service.release.return_value = False
        with pytest.raises(AccountingError) as release_error:
            await release_chat(owner)
        assert release_error.value.code is AccountingErrorCode.ACCOUNTING_FAILED

    run_async(scenario())
