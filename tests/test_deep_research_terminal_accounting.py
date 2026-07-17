from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, create_autospec, patch
from uuid import UUID

import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from llm_gateway_core.api.v1.deep_research_accounting import (
    DeepResearchTerminalOwner,
    take_deep_research_terminal_owner,
)
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
    get_accounting_request_context,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
    response_observation_published,
)
from llm_gateway_core.services.accounting import (
    DEFAULT_OPERATION_COST_USD,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingReceipt,
    AccountingReservation,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.deep_research_accounting import (
    DeepResearchAuthIdentity,
    DeepResearchChildAdmission,
    DeepResearchChildSeal,
    DeepResearchContextTokenCodec,
)
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


PARENT_MODEL = "deep-research-model"
CHILD_MODEL = "web-search-model"
RAW_API_KEY = "llmgw_sensitive-parent-key"


class _CountingRegistry(ActiveRequestsRegistry):
    def __init__(self) -> None:
        super().__init__()
        self.finish_calls = 0

    def finish(self, request_id: str | None) -> None:
        self.finish_calls += 1
        super().finish(request_id)


def _accepted_receipt(
    event_id: str,
    billing_fingerprint: str = "a" * 64,
) -> AccountingReceipt:
    return AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=event_id,
        billing_fingerprint=billing_fingerprint,
        usage_row_id=1,
    )


def _owner_setup():
    parent_reservation = AccountingReservation(
        reservation_id="parent-reservation",
        request_id="parent-request",
        api_key_id=7,
        reserved_usd=1.0,
    )
    policy = classify_billing_policy("POST", "/v1/web/deep-research")
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template="/v1/web/deep-research",
        policy=policy,
        request_id=parent_reservation.request_id,
        reservation=parent_reservation,
    )
    auth_identity = DeepResearchAuthIdentity(
        api_key_id=7,
        allowed_models=(PARENT_MODEL, CHILD_MODEL, CHILD_MODEL),
    )
    codec = DeepResearchContextTokenCodec.create_process_local()
    handle, token = codec.issue_parent(
        reservation=parent_reservation,
        gateway_model=PARENT_MODEL,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    service = create_autospec(AccountingService, instance=True, spec_set=True)
    service.begin_deep_research_parent.return_value = (handle, token)
    registry = _CountingRegistry()
    owner = DeepResearchTerminalOwner(
        accounting_service=service,
        context=context,
        auth_identity=auth_identity,
        estimate_usd=0.25,
        cost_rate_registry={},
        operation_cost_calculator_registry={},
        active_requests_registry=registry,
        active_request_id=parent_reservation.request_id,
    )
    return owner, service, auth_identity, handle, token, codec


def _install_successful_commit(service: AccountingService) -> None:
    async def commit(
        _reservation: AccountingReservation,
        event: AccountingEvent,
    ) -> AccountingReceipt:
        return _accepted_receipt(event.event_id, event.billing_fingerprint)

    service.commit.side_effect = commit


def _request_setup() -> tuple[
    Request,
    AccountingService,
    DeepResearchChildSeal,
    object,
    _CountingRegistry,
]:
    parent_reservation = AccountingReservation(
        reservation_id="parent-reservation",
        request_id="parent-request",
        api_key_id=7,
        reserved_usd=1.0,
    )
    policy = classify_billing_policy("POST", "/v1/web/deep-research")
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template="/v1/web/deep-research",
        policy=policy,
        request_id=parent_reservation.request_id,
        reservation=parent_reservation,
    )
    handle, token = DeepResearchContextTokenCodec.create_process_local().issue_parent(
        reservation=parent_reservation,
        gateway_model=PARENT_MODEL,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    seal = DeepResearchChildSeal(
        receipts=(_accepted_receipt("child-event"),),
        aggregate_usage=AccountingUsage(cost=0.37),
    )
    service = create_autospec(AccountingService, instance=True, spec_set=True)
    service.begin_deep_research_parent.return_value = (handle, token)
    service.seal_deep_research_children.return_value = seal
    service.cancel_deep_research_children.return_value = None
    service.release.return_value = True
    registry = _CountingRegistry()
    registry.start(
        request_id=parent_reservation.request_id,
        path="/v1/web/deep-research",
        api_key_id=7,
    )
    services = make_app_services(
        accounting_service=service,
        active_requests_registry=registry,
    )
    app = FastAPI()
    app.state.services = services
    scope = {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/web/deep-research",
        "raw_path": b"/v1/web/deep-research",
        "query_string": b"",
        "root_path": "",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "app": app,
        "state": {
            ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: context,
            "runtime_snapshot": make_runtime_snapshot(),
            "api_key_record": ApiKeyRecord(
                id=7,
                name="deep-research-test",
                api_key=RAW_API_KEY,
                budget_usd=None,
                spent_usd=0.0,
                rpm=None,
                tpm=None,
            ),
            "llmgateway_request_id": parent_reservation.request_id,
            "llmgateway_active_request_id": parent_reservation.request_id,
        },
    }
    return Request(scope), service, seal, handle, registry


async def _run_response(
    request: Request,
    response: JSONResponse,
    *,
    before_send=None,
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


def test_begin_returns_signed_opaque_token_without_raw_api_key() -> None:
    owner, service, auth_identity, handle, token, codec = _owner_setup()

    issued = owner.begin(PARENT_MODEL)

    assert issued == token
    assert issued.startswith("dr1.")
    assert RAW_API_KEY not in issued
    assert codec.decode(issued, now=datetime.now(timezone.utc)) == handle.delegated_identity
    assert owner.token == issued
    service.begin_deep_research_parent.assert_called_once_with(
        handle.reservation,
        gateway_model=PARENT_MODEL,
        auth_identity=auth_identity,
    )


def test_in_process_operation_child_commits_parent_linked_charge() -> None:
    owner, service, _auth_identity, handle, token, _codec = _owner_setup()
    child_request_id = "00000000-0000-4000-8000-000000000001"
    child_reservation = AccountingReservation(
        reservation_id="child-reservation",
        request_id=child_request_id,
        api_key_id=7,
        reserved_usd=0.25,
    )
    service.reserve_deep_research_child.return_value = DeepResearchChildAdmission(
        reservation=child_reservation,
        parent_event_id=handle.rollup_event_id,
        ordinal=0,
    )
    _install_successful_commit(service)
    work = AsyncMock(return_value={"items": ["result"]})
    owner.begin(PARENT_MODEL)

    with patch(
        "llm_gateway_core.api.v1.deep_research_accounting.uuid.uuid4",
        return_value=UUID(child_request_id),
    ):
        result = run_async(
            owner.run_flat_operation_child(
                route_template="/v1/web/search",
                gateway_model=CHILD_MODEL,
                work=work,
            )
        )

    assert result == {"items": ["result"]}
    work.assert_awaited_once_with()
    service.reserve_deep_research_child.assert_awaited_once_with(
        token,
        request_id=child_request_id,
        estimate_usd=0.25,
    )
    committed_reservation, event = service.commit.await_args.args
    assert committed_reservation == child_reservation
    assert event.kind is AccountingEventKind.CHARGE
    assert event.event_id == child_request_id
    assert event.parent_event_id == handle.rollup_event_id
    assert event.route_template == "/v1/web/search"
    assert event.operation == "web_search"
    assert event.usage.cost == DEFAULT_OPERATION_COST_USD
    assert event.cost_source is CostSource.OPERATION_DEFAULT
    service.release.assert_not_awaited()


def test_in_process_operation_work_failure_releases_child() -> None:
    owner, service, _auth_identity, handle, _token, _codec = _owner_setup()
    child_request_id = "00000000-0000-4000-8000-000000000002"
    child_reservation = AccountingReservation(
        reservation_id="child-reservation",
        request_id=child_request_id,
        api_key_id=7,
        reserved_usd=0.25,
    )
    service.reserve_deep_research_child.return_value = DeepResearchChildAdmission(
        reservation=child_reservation,
        parent_event_id=handle.rollup_event_id,
        ordinal=0,
    )
    service.release.return_value = True
    owner.begin(PARENT_MODEL)

    with (
        patch(
            "llm_gateway_core.api.v1.deep_research_accounting.uuid.uuid4",
            return_value=UUID(child_request_id),
        ),
        pytest.raises(RuntimeError, match="child failed"),
    ):
        run_async(
            owner.run_flat_operation_child(
                route_template="/v1/web/read",
                gateway_model=CHILD_MODEL,
                work=AsyncMock(side_effect=RuntimeError("child failed")),
            )
        )

    service.release.assert_awaited_once_with(child_reservation)
    service.commit.assert_not_awaited()


def test_in_process_operation_commit_failure_releases_child() -> None:
    owner, service, _auth_identity, handle, _token, _codec = _owner_setup()
    child_request_id = "00000000-0000-4000-8000-000000000003"
    child_reservation = AccountingReservation(
        reservation_id="child-reservation",
        request_id=child_request_id,
        api_key_id=7,
        reserved_usd=0.25,
    )
    service.reserve_deep_research_child.return_value = DeepResearchChildAdmission(
        reservation=child_reservation,
        parent_event_id=handle.rollup_event_id,
        ordinal=0,
    )
    service.commit.side_effect = AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
    service.release.return_value = True
    owner.begin(PARENT_MODEL)

    with (
        patch(
            "llm_gateway_core.api.v1.deep_research_accounting.uuid.uuid4",
            return_value=UUID(child_request_id),
        ),
        pytest.raises(AccountingError),
    ):
        run_async(
            owner.run_flat_operation_child(
                route_template="/v1/web/search",
                gateway_model=CHILD_MODEL,
                work=AsyncMock(return_value={"items": ["result"]}),
            )
        )

    service.release.assert_awaited_once_with(child_reservation)


def test_seal_for_response_defers_rollup_commit_until_asgi_finalizer() -> None:
    owner, service, _auth_identity, handle, _token, _codec = _owner_setup()
    child_receipt = _accepted_receipt("child-event")
    seal = DeepResearchChildSeal(
        receipts=(child_receipt,),
        aggregate_usage=AccountingUsage(cost=0.37),
    )
    service.seal_deep_research_children.return_value = seal
    _install_successful_commit(service)
    owner.begin(PARENT_MODEL)

    result = run_async(owner.seal_for_response())

    assert result is seal
    service.seal_deep_research_children.assert_awaited_once_with(handle)
    service.commit.assert_not_awaited()
    assert owner.is_ready
    assert not owner.is_closed
    service.release.assert_not_awaited()


def test_successful_json_commits_rollup_before_start_and_finishes_active_once() -> None:
    async def scenario() -> None:
        request, service, seal, handle, registry = _request_setup()
        owner = take_deep_research_terminal_owner(request)
        owner.begin(PARENT_MODEL)
        assert await owner.seal_for_response() is seal
        sent_types: list[str] = []

        async def commit(
            reservation: AccountingReservation,
            event: AccountingEvent,
        ) -> AccountingReceipt:
            assert sent_types == []
            assert reservation == handle.reservation
            assert event.kind is AccountingEventKind.ROLLUP
            assert event.event_id == handle.rollup_event_id
            assert event.usage.cost == 0.0
            assert event.cost_source is CostSource.RECEIPT_ROLLUP
            assert event.child_event_ids == seal.child_event_ids
            assert event.child_fingerprints == seal.child_fingerprints
            return _accepted_receipt(event.event_id, event.billing_fingerprint)

        service.commit.side_effect = commit
        response = JSONResponse(
            {"result": "exact"},
            status_code=201,
            headers={"x-exact": "yes"},
        )

        messages = await _run_response(
            request,
            response,
            before_send=lambda message: sent_types.append(str(message["type"])),
        )

        assert messages[0]["status"] == 201
        assert _body(messages) == response.body
        assert response.headers["x-exact"] == "yes"
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()
        service.cancel_deep_research_children.assert_not_awaited()
        assert owner.is_closed
        assert registry.finish_calls == 1
        assert registry.list_records() == []

    run_async(scenario())


def test_failure_response_after_seal_releases_parent_without_rollup() -> None:
    async def scenario() -> None:
        request, service, seal, handle, registry = _request_setup()
        owner = take_deep_research_terminal_owner(request)
        owner.begin(PARENT_MODEL)
        assert await owner.seal_for_response() is seal
        response = JSONResponse({"error": "failed"}, status_code=502)

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 502
        assert _body(messages) == response.body
        service.commit.assert_not_awaited()
        service.release.assert_awaited_once_with(handle.reservation)
        service.cancel_deep_research_children.assert_not_awaited()
        assert owner.is_closed
        assert registry.finish_calls == 1
        assert registry.list_records() == []

    run_async(scenario())


def test_parent_rollup_commit_failure_returns_safe_503_and_releases_parent() -> None:
    async def scenario() -> None:
        request, service, seal, handle, registry = _request_setup()
        owner = take_deep_research_terminal_owner(request)
        owner.begin(PARENT_MODEL)
        assert await owner.seal_for_response() is seal
        service.commit.side_effect = AccountingError(
            AccountingErrorCode.ACCOUNTING_FAILED
        )
        response = JSONResponse({"result": "must-not-leak"})

        messages = await _run_response(request, response)

        assert messages[0]["status"] == 503
        assert json.loads(_body(messages))["error"]["code"] == "accounting_unavailable"
        assert b"must-not-leak" not in _body(messages)
        service.commit.assert_awaited_once()
        service.release.assert_awaited_once_with(handle.reservation)
        service.cancel_deep_research_children.assert_not_awaited()
        assert owner.is_closed
        assert registry.finish_calls == 1
        assert registry.list_records() == []

    run_async(scenario())


def test_release_if_open_cancels_children_and_releases_parent() -> None:
    owner, service, _auth_identity, handle, _token, _codec = _owner_setup()
    service.cancel_deep_research_children.return_value = None
    service.release.return_value = True
    owner.begin(PARENT_MODEL)

    run_async(owner.release_if_open())
    run_async(owner.release_if_open())

    service.cancel_deep_research_children.assert_awaited_once_with(handle)
    service.release.assert_awaited_once_with(handle.reservation)
    assert owner.is_closed


def test_take_owner_consumes_context_and_publishes_response_observation_once() -> None:
    request, service, _seal, _handle, _registry = _request_setup()

    owner = take_deep_research_terminal_owner(request)

    assert isinstance(owner, DeepResearchTerminalOwner)
    assert ACCOUNTING_REQUEST_CONTEXT_STATE_KEY not in request.scope["state"]
    assert response_observation_published(request.scope)
    with pytest.raises(AccountingValidationError):
        take_deep_research_terminal_owner(request)
    service.commit.assert_not_awaited()
    service.release.assert_not_awaited()


def test_publish_failure_restores_exact_accounting_context() -> None:
    request, service, _seal, _handle, _registry = _request_setup()
    context = get_accounting_request_context(request.scope)

    with patch(
        "llm_gateway_core.api.v1.deep_research_accounting."
        "publish_response_observation",
        side_effect=RuntimeError("publish failed"),
    ):
        with pytest.raises(RuntimeError, match="publish failed"):
            take_deep_research_terminal_owner(request)

    assert get_accounting_request_context(request.scope) is context
    assert not response_observation_published(request.scope)
    service.commit.assert_not_awaited()
    service.release.assert_not_awaited()


@pytest.mark.parametrize(
    "terminal_path",
    ["http_error", "protocol_error", "cancelled", "disconnect"],
)
def test_active_parent_terminal_failures_cancel_children_release_and_finish_once(
    terminal_path: str,
) -> None:
    async def scenario() -> None:
        request, service, _seal, handle, registry = _request_setup()
        owner = take_deep_research_terminal_owner(request)
        owner.begin(PARENT_MODEL)
        sent: list[dict[str, object]] = []

        async def app(scope, receive, send) -> None:
            if terminal_path == "http_error":
                await JSONResponse({"error": "failed"}, status_code=502)(
                    scope,
                    receive,
                    send,
                )
            elif terminal_path == "protocol_error":
                await send(
                    {
                        "type": "http.response.start",
                        "status": 200,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send(
                    {
                        "type": "http.response.body",
                        "body": b'{"invalid":',
                    }
                )
            elif terminal_path == "cancelled":
                raise asyncio.CancelledError
            else:
                assert (await receive())["type"] == "http.disconnect"

        async def receive() -> dict[str, object]:
            if terminal_path == "disconnect":
                return {"type": "http.disconnect"}
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict[str, object]) -> None:
            sent.append(message)

        middleware = ResponseObservationMiddleware(app)
        if terminal_path == "cancelled":
            with pytest.raises(asyncio.CancelledError):
                await middleware(request.scope, receive, send)
        else:
            await middleware(request.scope, receive, send)

        if terminal_path == "protocol_error":
            assert sent[0]["status"] == 502
            assert json.loads(_body(sent))["error"]["code"] == (
                "upstream_protocol_error"
            )
        service.commit.assert_not_awaited()
        service.cancel_deep_research_children.assert_awaited_once_with(handle)
        service.release.assert_awaited_once_with(handle.reservation)
        assert owner.is_closed
        assert registry.finish_calls == 1
        assert registry.list_records() == []

    run_async(scenario())


def test_take_owner_identity_mismatch_does_not_consume_accounting_context() -> None:
    parent_reservation = AccountingReservation(
        reservation_id="parent-reservation",
        request_id="parent-request",
        api_key_id=7,
        reserved_usd=1.0,
    )
    policy = classify_billing_policy("POST", "/v1/web/deep-research")
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template="/v1/web/deep-research",
        policy=policy,
        request_id=parent_reservation.request_id,
        reservation=parent_reservation,
    )
    services = make_app_services()
    app = FastAPI()
    app.state.services = services
    scope = {
        "type": "http",
        "app": app,
        "state": {
            ACCOUNTING_REQUEST_CONTEXT_STATE_KEY: context,
            "runtime_snapshot": make_runtime_snapshot(),
            "api_key_record": ApiKeyRecord(
                id=8,
                name="mismatched-key",
                api_key=RAW_API_KEY,
                budget_usd=None,
                spent_usd=0.0,
                rpm=None,
                tpm=None,
            ),
        },
    }
    request = Request(scope)

    with pytest.raises(AccountingValidationError):
        take_deep_research_terminal_owner(request)

    assert get_accounting_request_context(scope) is context
