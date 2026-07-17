from __future__ import annotations

import asyncio
import json
import logging
from contextlib import ExitStack, contextmanager
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request

from llm_gateway_core.api.v1 import web
from llm_gateway_core.api.v1 import web_adapters as web_adapters_owner
from llm_gateway_core.api.v1 import web_research_orchestration as web_research_owner
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.services.accounting import (
    DEFAULT_OPERATION_COST_USD,
    AccountingEventKind,
    AccountingReceipt,
    AccountingReservation,
    AccountingUsage,
    CostSource,
    OperationCostCalculator,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.deep_research_accounting import (
    DeepResearchChildAdmission,
    DeepResearchChildSeal,
    DeepResearchContextTokenCodec,
)
from llm_gateway_core.services.deep_research_protocol import (
    DeepResearchCallbackOperation,
    DeepResearchCallbackRequest,
    DeepResearchResult,
)
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


SEARCH_MODEL = "gateway-search"
READ_MODEL = "gateway-read"
RESEARCH_MODEL = "gateway-research"
DEEP_RESEARCH_MODEL = "gateway-deep-research"


def _config_loader() -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = {}
    loader.fallback_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    loader.operation_rules = {
        web.WEB_SEARCH_SECTION: {SEARCH_MODEL: {}},
        web.WEB_READ_SECTION: {READ_MODEL: {}},
        web.WEB_RESEARCH_SECTION: {
            RESEARCH_MODEL: {
                "search_model": "research-search",
                "read_model": "research-read",
                "rerank_model": "research-rerank",
                "analysis_model": "research-analysis",
            }
        },
        web.WEB_DEEP_RESEARCH_SECTION: {
            DEEP_RESEARCH_MODEL: {
                "search_model": "deep-search",
                "read_model": "deep-read",
                "fast_model": "deep-fast",
                "smart_model": "deep-smart",
                "strategic_model": "deep-strategic",
            }
        },
    }
    return loader


def _request(
    path: str,
    *,
    calculators: dict[tuple[str, str], OperationCostCalculator] | None = None,
    with_context: bool = True,
) -> tuple[Request, object]:
    policy = classify_billing_policy("POST", path)
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template=path,
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
    )
    services = make_app_services()
    services.accounting_service.release.return_value = True
    services.rate_limiter.add_tokens = Mock()
    loader = _config_loader()
    snapshot = make_runtime_snapshot(
        config_loader=loader,
        operation_dispatcher=Mock(spec=OperationDispatcher),
        operation_cost_calculator_registry=calculators or {},
    )
    app = FastAPI()
    app.state.services = services
    state = {
        "llmgateway_request_id": "request-1",
        "runtime_snapshot": snapshot,
        "api_key_record": (
            ApiKeyRecord(
                id=7,
                name="web-accounting-test",
                api_key="llmgw_test-virtual-key",
                budget_usd=None,
                spent_usd=0.0,
                rpm=None,
                tpm=None,
            )
            if path == "/v1/web/deep-research"
            else None
        ),
    }
    if with_context:
        state[ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = context
    return (
        Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": path,
                "raw_path": path.encode("ascii"),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"application/json")],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
                "app": app,
                "state": state,
            }
        ),
        services,
    )


def _install_successful_commit(service, *, before_return=None) -> None:
    async def commit(_reservation, event):
        if before_return is not None:
            before_return(event)
        return AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id=event.event_id,
            billing_fingerprint=event.billing_fingerprint,
            usage_row_id=1,
        )

    service.commit.side_effect = commit


def _install_deep_research_accounting(request: Request, services):
    context = request.scope["state"][ACCOUNTING_REQUEST_CONTEXT_STATE_KEY]
    handle, token = DeepResearchContextTokenCodec.create_process_local().issue_parent(
        reservation=context.reservation,
        gateway_model=DEEP_RESEARCH_MODEL,
        expires_at=datetime.now(timezone.utc) + timedelta(hours=1),
    )
    child_receipt = AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id="deep-research-child-1",
        billing_fingerprint="a" * 64,
        usage_row_id=2,
    )
    seal = DeepResearchChildSeal(
        receipts=(child_receipt,),
        aggregate_usage=AccountingUsage(
            prompt_tokens=11,
            completion_tokens=7,
            total_tokens=18,
            reasoning_tokens=3,
            cached_tokens=2,
            cost=0.37,
        ),
    )
    services.accounting_service.begin_deep_research_parent.return_value = (
        handle,
        token,
    )
    services.accounting_service.seal_deep_research_children.return_value = seal
    services.accounting_service.cancel_deep_research_children.return_value = None
    _install_successful_commit(services.accounting_service)
    return handle, token, seal


def _response_json(response) -> object:
    return json.loads(response.body)


async def _send_response(
    request: Request,
    response,
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


async def _call_observed_endpoint(request: Request, endpoint) -> list[dict[str, object]]:
    messages: list[dict[str, object]] = []

    async def app(scope, receive, send) -> None:
        response = await endpoint()
        await response(scope, receive, send)

    async def receive() -> dict[str, object]:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: dict[str, object]) -> None:
        messages.append(message)

    await ResponseObservationMiddleware(app)(request.scope, receive, send)
    return messages


def _wire_body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        bytes(message.get("body", b"")) for message in messages if message.get("type") == "http.response.body"
    )


def _assert_no_legacy_writes(services) -> None:
    services.tokens_usage_db.insert_usage.assert_not_called()
    services.tokens_usage_db.insert_usage_once.assert_not_called()
    services.api_keys_db.record_spent.assert_not_called()
    services.rate_limiter.add_tokens.assert_not_called()


def _payload(path: str) -> dict[str, object]:
    if path == "/v1/web/search":
        return {"model": SEARCH_MODEL, "query": "topic"}
    if path == "/v1/web/read":
        return {"model": READ_MODEL, "url": "https://example.test/article"}
    if path == "/v1/tavily/search":
        return {"model": SEARCH_MODEL, "query": "topic"}
    if path == "/v1/tavily/extract":
        return {"model": READ_MODEL, "urls": ["https://example.test/article"]}
    if path == "/v1/web/research":
        return {"model": RESEARCH_MODEL, "query": "topic", "language": "en"}
    if path == "/v1/web/deep-research":
        return {"model": DEEP_RESEARCH_MODEL, "query": "topic"}
    raise AssertionError(path)


async def _call_endpoint(path: str, request: Request):
    endpoints = {
        "/v1/web/search": web.web_search,
        "/v1/web/read": web.web_read,
        "/v1/tavily/search": web.tavily_search,
        "/v1/tavily/extract": web.tavily_extract,
        "/v1/web/research": web.web_research,
        "/v1/web/deep-research": web.web_deep_research,
    }
    return await endpoints[path](request)


@contextmanager
def _web_dependencies(
    path: str,
    *,
    search_error: BaseException | None = None,
    mutate_gateway_model: str | None = None,
    deep_result: object | None = None,
    deep_error: BaseException | None = None,
):
    async def run_work(_request, _operation, work_factory, **_kwargs):
        return await work_factory()

    async def search(*_args, **kwargs):
        if search_error is not None:
            raise search_error
        if mutate_gateway_model is not None:
            _args[0].state.llmgateway_gateway_model = mutate_gateway_model
        if kwargs.get("search_model") == "research-search":
            return []
        return [
            {
                "url": "https://example.test/article",
                "title": "Article",
                "snippet": "Snippet",
            }
        ]

    conduct = AsyncMock(
        return_value=deep_result
        or DeepResearchResult(
            query="topic",
            report="Deep report",
            sources=(),
            source_urls=(),
            context=(),
            research_result=None,
            generated_images=(),
            costs=99.0,
        )
    )
    if deep_error is not None:
        conduct.side_effect = deep_error

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                web,
                "read_json_request_body",
                AsyncMock(return_value=_payload(path)),
            )
        )
        stack.enter_context(patch.object(web, "enforce_virtual_key_access"))
        update_mock = stack.enter_context(patch.object(web, "update_active_request_from_state"))
        stack.enter_context(patch.object(web_adapters_owner, "update_active_request_from_state", update_mock))
        stack.enter_context(patch.object(web, "_validate_http_url", side_effect=lambda url: url))
        stack.enter_context(
            patch.object(
                web_research_owner,
                "_validate_http_url",
                side_effect=lambda url: url,
            )
        )
        search_mock = AsyncMock(side_effect=search)
        stack.enter_context(patch.object(web, "_search_with_model", search_mock))
        stack.enter_context(patch.object(web_research_owner, "_search_with_model", search_mock))
        read_mock = AsyncMock(
            return_value={
                "url": "https://example.test/article",
                "title": "Article",
                "content": "Content",
            }
        )
        stack.enter_context(patch.object(web, "_read_with_model", read_mock))
        stack.enter_context(patch.object(web_research_owner, "_read_with_model", read_mock))
        stack.enter_context(
            patch.object(
                web,
                "_plan_evidence_matrix",
                AsyncMock(return_value={"mode": "not_applicable"}),
            )
        )
        stack.enter_context(
            patch.object(
                web,
                "_run_with_client_disconnect_cancellation",
                AsyncMock(side_effect=run_work),
            )
        )
        stack.enter_context(patch.object(web, "_run_deep_research_process", conduct))
        yield search_mock, conduct


@pytest.mark.parametrize(
    ("path", "operation", "gateway_model", "configured_rate", "expected_cost", "expected_source"),
    [
        (
            "/v1/web/search",
            "web_search",
            SEARCH_MODEL,
            0.21,
            0.21,
            CostSource.OPERATION_CONFIGURED,
        ),
        (
            "/v1/web/read",
            "web_read",
            READ_MODEL,
            None,
            DEFAULT_OPERATION_COST_USD,
            CostSource.OPERATION_DEFAULT,
        ),
        (
            "/v1/tavily/search",
            "web_search",
            SEARCH_MODEL,
            0.22,
            0.22,
            CostSource.OPERATION_CONFIGURED,
        ),
        (
            "/v1/tavily/extract",
            "web_read",
            READ_MODEL,
            None,
            DEFAULT_OPERATION_COST_USD,
            CostSource.OPERATION_DEFAULT,
        ),
        (
            "/v1/web/research",
            "web_research",
            RESEARCH_MODEL,
            0.23,
            0.23,
            CostSource.OPERATION_CONFIGURED,
        ),
    ],
)
def test_web_endpoints_commit_one_synthesized_flat_charge(
    path: str,
    operation: str,
    gateway_model: str,
    configured_rate: float | None,
    expected_cost: float,
    expected_source: CostSource,
) -> None:
    async def scenario() -> None:
        calculators = (
            {
                (operation, gateway_model): OperationCostCalculator(
                    unit="operation",
                    rate_usd=configured_rate,
                )
            }
            if configured_rate is not None
            else {}
        )
        request, services = _request(path, calculators=calculators)
        response_started = False
        with _web_dependencies(path) as (search, _conduct):

            def before_commit(_event) -> None:
                assert response_started is False
                if path in {
                    "/v1/web/search",
                    "/v1/tavily/search",
                    "/v1/web/research",
                }:
                    search.assert_awaited()

            _install_successful_commit(
                services.accounting_service,
                before_return=before_commit,
            )
            response = await _call_endpoint(path, request)

        assert response.status_code == 200
        assert isinstance(_response_json(response), dict)
        services.accounting_service.commit.assert_not_awaited()

        def before_send(message: dict[str, object]) -> None:
            nonlocal response_started
            if message.get("type") == "http.response.start":
                response_started = True

        messages = await _send_response(
            request,
            response,
            before_send=before_send,
        )
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == expected_cost
        assert event.cost_source is expected_source
        assert event.usage.duration_ms is not None
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize(
    "error",
    [HTTPException(status_code=503, detail="search failed"), asyncio.CancelledError()],
)
def test_web_error_or_cancellation_releases_without_charge(error: BaseException) -> None:
    async def scenario() -> None:
        path = "/v1/web/search"
        request, services = _request(path)
        with _web_dependencies(path, search_error=error):
            with pytest.raises(type(error)):
                await _call_endpoint(path, request)

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_web_missing_accounting_context_fails_before_work() -> None:
    async def scenario() -> None:
        path = "/v1/web/search"
        request, services = _request(path, with_context=False)
        with _web_dependencies(path) as (search, _conduct):
            response = await _call_endpoint(path, request)

        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        search.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_web_terminal_charge_uses_outer_model_after_nested_call() -> None:
    async def scenario() -> None:
        path = "/v1/web/search"
        calculators = {
            ("web_search", SEARCH_MODEL): OperationCostCalculator(
                unit="operation",
                rate_usd=0.21,
            ),
            ("web_search", "internal-model"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.91,
            ),
        }
        request, services = _request(path, calculators=calculators)
        _install_successful_commit(services.accounting_service)
        with _web_dependencies(
            path,
            mutate_gateway_model="internal-model",
        ):
            response = await _call_endpoint(path, request)

        assert response.status_code == 200
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        event = services.accounting_service.commit.await_args.args[1]
        assert event.gateway_model == SEARCH_MODEL
        assert event.usage.cost == 0.21

    run_async(scenario())


def test_deep_research_success_uses_signed_child_context_and_zero_cost_rollup() -> None:
    async def scenario() -> None:
        path = "/v1/web/deep-research"
        request, services = _request(path)
        handle, token, seal = _install_deep_research_accounting(request, services)
        with _web_dependencies(path) as (_search, conduct):
            response = await _call_endpoint(path, request)

        assert response.status_code == 200
        response_payload = _response_json(response)
        assert response_payload["output"] == "Deep report"
        assert response_payload["usage"]["cost"] == 0.37
        assert response_payload["usage"]["prompt_tokens"] == 11
        assert response_payload["usage"]["completion_tokens"] == 7
        assert response_payload["usage"]["total_tokens"] == 18
        conduct.assert_awaited_once()
        job = conduct.await_args.args[1]
        assert token.startswith("dr1.")
        assert job.gateway_api_key == token
        assert token != request.state.api_key_record.api_key
        services.accounting_service.begin_deep_research_parent.assert_called_once()
        begin_call = services.accounting_service.begin_deep_research_parent.call_args
        assert begin_call.args == (handle.reservation,)
        assert begin_call.kwargs["gateway_model"] == DEEP_RESEARCH_MODEL
        assert begin_call.kwargs["auth_identity"].api_key_id == 7
        services.accounting_service.seal_deep_research_children.assert_awaited_once_with(handle)
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_awaited_once()
        reservation, event = services.accounting_service.commit.await_args.args
        assert reservation == handle.reservation
        assert event.kind is AccountingEventKind.ROLLUP
        assert event.usage.cost == 0.0
        assert event.child_event_ids == seal.child_event_ids
        assert event.child_fingerprints == seal.child_fingerprints
        services.accounting_service.cancel_deep_research_children.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_deep_research_invalid_reported_cost_is_diagnostic_only(caplog) -> None:
    async def scenario() -> None:
        path = "/v1/web/deep-research"
        request, services = _request(path)
        _install_deep_research_accounting(request, services)
        result = DeepResearchResult(
            query="topic",
            report="Deep report",
            sources=(),
            source_urls=(),
            context=(),
            research_result=None,
            generated_images=(),
            costs=None,
        )
        with (
            caplog.at_level(logging.WARNING),
            _web_dependencies(path, deep_result=result),
        ):
            response = await _call_endpoint(path, request)

        assert response.status_code == 200
        assert _response_json(response)["usage"]["cost"] == 0.37
        assert "diagnostic cost is unavailable or invalid" in caplog.text
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_awaited_once()

    run_async(scenario())


def test_deep_research_gateway_callbacks_charge_distinct_children() -> None:
    async def scenario() -> None:
        path = "/v1/web/deep-research"
        request, services = _request(path)
        handle, token, _seal = _install_deep_research_accounting(request, services)
        child_receipts: list[AccountingReceipt] = []
        committed_events = []

        async def reserve_child(
            supplied_token: str,
            *,
            request_id: str,
            estimate_usd: float,
        ) -> DeepResearchChildAdmission:
            assert supplied_token == token
            return DeepResearchChildAdmission(
                reservation=AccountingReservation(
                    reservation_id=f"deep-child-reservation-{len(child_receipts)}",
                    request_id=request_id,
                    api_key_id=7,
                    reserved_usd=estimate_usd,
                ),
                parent_event_id=handle.rollup_event_id,
                ordinal=len(child_receipts),
            )

        async def commit(reservation, event):
            committed_events.append(event)
            receipt = AccountingReceipt(
                source_status=SourceStatus.ACCEPTED,
                projection_status=ProjectionStatus.APPLIED,
                event_id=event.event_id,
                billing_fingerprint=event.billing_fingerprint,
                usage_row_id=len(committed_events),
            )
            if reservation != handle.reservation:
                child_receipts.append(receipt)
            return receipt

        async def seal_children(supplied_handle):
            assert supplied_handle == handle
            return DeepResearchChildSeal(
                receipts=tuple(child_receipts),
                aggregate_usage=AccountingUsage(cost=sum(event.usage.cost for event in committed_events)),
            )

        services.accounting_service.reserve_deep_research_child.side_effect = reserve_child
        services.accounting_service.commit.side_effect = commit
        services.accounting_service.seal_deep_research_children.side_effect = seal_children

        async def conduct_with_gateway_callbacks(_runner, job, callbacks):
            await callbacks.handle(
                DeepResearchCallbackRequest(
                    job_id=job.job_id,
                    message_id="search-1",
                    operation=DeepResearchCallbackOperation.SEARCH,
                    arguments={"query": "child query", "max_results": 3},
                )
            )
            await callbacks.handle(
                DeepResearchCallbackRequest(
                    job_id=job.job_id,
                    message_id="read-1",
                    operation=DeepResearchCallbackOperation.READ,
                    arguments={"url": "https://example.test/article"},
                )
            )
            return DeepResearchResult(
                query=job.query,
                report="Deep report",
                sources=(),
                source_urls=(),
                context=(),
                research_result=None,
                generated_images=(),
                costs=99.0,
            )

        with _web_dependencies(path) as (_search, conduct):
            conduct.side_effect = conduct_with_gateway_callbacks
            response = await _call_endpoint(path, request)

        assert response.status_code == 200
        assert _response_json(response)["usage"]["cost"] == pytest.approx(0.2)
        assert services.accounting_service.reserve_deep_research_child.await_count == 2
        assert len(committed_events) == 2
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        child_events = committed_events[:2]
        assert [event.operation for event in child_events] == [
            "web_search",
            "web_read",
        ]
        assert [event.usage.cost for event in child_events] == [0.1, 0.1]
        assert all(event.parent_event_id == handle.rollup_event_id for event in child_events)
        rollup = committed_events[2]
        assert rollup.kind is AccountingEventKind.ROLLUP
        assert rollup.usage.cost == 0.0
        assert rollup.child_event_ids == tuple(event.event_id for event in child_events)
        assert sum(event.usage.cost for event in committed_events) == pytest.approx(0.2)
        services.accounting_service.cancel_deep_research_children.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_deep_research_callbacks_use_captured_generation_and_fresh_request_state() -> None:
    async def scenario() -> None:
        path = "/v1/web/deep-research"
        request, services = _request(path)
        _install_deep_research_accounting(request, services)
        captured_snapshot = request.state.runtime_snapshot
        replacement_snapshot = make_runtime_snapshot(
            generation=2,
            config_loader=_config_loader(),
            operation_dispatcher=Mock(spec=OperationDispatcher),
        )
        marker = object()
        request.state.original_marker = marker
        request.state.llmgateway_active_request_id = "active-deep-research"
        request.state.llmgateway_request_id = "active-deep-research"
        services.active_requests_registry.start(
            request_id="active-deep-research",
            path=path,
            api_key_id=7,
        )

        async def reserve_child(_token, *, request_id, estimate_usd):
            return DeepResearchChildAdmission(
                reservation=AccountingReservation(
                    reservation_id="captured-generation-child",
                    request_id=request_id,
                    api_key_id=7,
                    reserved_usd=estimate_usd,
                ),
                parent_event_id="captured-generation-parent",
                ordinal=0,
            )

        services.accounting_service.reserve_deep_research_child.side_effect = reserve_child
        services.accounting_service.commit.return_value = AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id="captured-generation-child-event",
            billing_fingerprint="c" * 64,
            usage_row_id=3,
        )
        update_active_request = web.update_active_request_from_state

        async def conduct_after_generation_change(_runner, job, callbacks):
            update_active_request(request)
            request.state.runtime_snapshot = replacement_snapshot
            await callbacks.handle(
                DeepResearchCallbackRequest(
                    job_id=job.job_id,
                    message_id="search-captured-generation",
                    operation=DeepResearchCallbackOperation.SEARCH,
                    arguments={"query": "child query", "max_results": 1},
                )
            )
            return DeepResearchResult(
                query=job.query,
                report="Deep report",
                sources=(),
                source_urls=(),
                context=(),
                research_result=None,
                generated_images=(),
                costs=0.1,
            )

        with _web_dependencies(path) as (search, conduct):

            async def callback_search(callback_request, **_kwargs):
                callback_request.state.llmgateway_gateway_model = "nested-callback-model"
                callback_request.state.llmgateway_operation = "web_search"
                update_active_request(callback_request)
                return [
                    {
                        "url": "https://example.test/article",
                        "title": "Article",
                        "snippet": "Snippet",
                    }
                ]

            search.side_effect = callback_search
            conduct.side_effect = conduct_after_generation_change
            response = await _call_endpoint(path, request)

        callback_request = search.await_args.args[0]
        assert response.status_code == 200
        assert callback_request is not request
        assert callback_request.scope["state"] is not request.scope["state"]
        assert callback_request.state.runtime_snapshot is captured_snapshot
        assert callback_request.state.runtime_snapshot.generation == 1
        assert not hasattr(callback_request.state, "llmgateway_active_request_id")
        assert not hasattr(callback_request.state, "llmgateway_request_id")
        assert request.state.runtime_snapshot is replacement_snapshot
        assert request.state.original_marker is marker
        assert request.state.llmgateway_gateway_model == DEEP_RESEARCH_MODEL
        active_record = services.active_requests_registry.list_records()[0]
        assert active_record["operation"] == "web_deep_research"
        assert active_record["gateway_model"] == DEEP_RESEARCH_MODEL

    run_async(scenario())


@pytest.mark.parametrize(
    "error",
    [HTTPException(status_code=503, detail="deep failed"), asyncio.CancelledError()],
)
def test_deep_research_error_or_cancellation_is_release_only(
    error: BaseException,
) -> None:
    async def scenario() -> None:
        path = "/v1/web/deep-research"
        request, services = _request(path)
        handle, _token, _seal = _install_deep_research_accounting(
            request,
            services,
        )
        with _web_dependencies(path, deep_error=error):
            with pytest.raises(type(error)):
                await _call_observed_endpoint(
                    request,
                    lambda: _call_endpoint(path, request),
                )

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.seal_deep_research_children.assert_not_awaited()
        services.accounting_service.cancel_deep_research_children.assert_awaited_once_with(handle)
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())
