from __future__ import annotations

import json
import io
from contextlib import ExitStack, asynccontextmanager, contextmanager
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, Request

from llm_gateway_core.api.v1 import pdf
from llm_gateway_core.api.v1.operation_runtime import ValidatedUpload
from llm_gateway_core.config.loader import OperationRoute
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.services.accounting import (
    DEFAULT_OPERATION_COST_USD,
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingReservation,
    CostSource,
    OperationCostCalculator,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _route(
    provider: str = "converter",
    model: str = "pdf-model",
) -> OperationRoute:
    return OperationRoute(
        provider=provider,
        model=model,
        target_path="https://converter.example/pdf/api",
    )


def _services():
    rate_limiter = Mock(spec=RateLimiter)
    services = make_app_services(rate_limiter=rate_limiter)
    services.accounting_service.release.return_value = True
    return services


def _request(
    method: str,
    route_template: str,
    *,
    path: str | None = None,
    request_id: str = "request-1",
    api_key_id: int | None = 7,
    calculators: dict[tuple[str, str], OperationCostCalculator] | None = None,
    services=None,
    with_context: bool = True,
) -> tuple[Request, object]:
    policy = classify_billing_policy(method, route_template)
    assert policy is not None
    context = AccountingRequestContext(
        method=method,
        route_template=route_template,
        policy=policy,
        request_id=request_id,
        reservation=AccountingReservation(
            reservation_id=f"reservation-{request_id}",
            request_id=request_id,
            api_key_id=api_key_id,
            reserved_usd=1.0,
        ),
    )
    services = services or _services()
    snapshot = make_runtime_snapshot(
        operation_cost_calculator_registry=calculators or {},
    )
    app = FastAPI()
    app.state.services = services
    state: dict[str, object] = {
        "llmgateway_request_id": request_id,
        "runtime_snapshot": snapshot,
    }
    if with_context:
        state[ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = context
    request_path = path or route_template
    return (
        Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": method,
                "scheme": "http",
                "path": request_path,
                "raw_path": request_path.encode("utf-8"),
                "query_string": b"",
                "root_path": "",
                "headers": [(b"content-type", b"multipart/form-data")],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
                "app": app,
                "state": state,
            }
        ),
        services,
    )


def _install_successful_commit(
    service,
    events: list[object],
    *,
    before_return=None,
) -> None:
    async def commit(_reservation, event):
        if before_return is not None:
            before_return(event)
        events.append(event)
        return AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id=event.event_id,
            billing_fingerprint=event.billing_fingerprint,
            usage_row_id=1,
        )

    service.commit.side_effect = commit


@contextmanager
def _post_dependencies(
    payload: object,
    *,
    gateway_model: str = "gateway/pdf",
    route: OperationRoute | None = None,
):
    selected_route = route or _route()
    proxy = AsyncMock(return_value=(payload, 200))
    resolve = AsyncMock(
        return_value=(selected_route, "https://converter.example/pdf/api", {}, Mock(), 0, 0.0)
    )
    parse = Mock()

    @asynccontextmanager
    async def parsed_request(request):
        parse(request)
        yield (
            gateway_model,
            {},
            ValidatedUpload(
                "document.pdf",
                "application/pdf",
                1,
                io.BytesIO(b"x"),
            ),
        )

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(pdf, "_parse_pdf_multipart_request", parsed_request)
        )
        stack.enter_context(patch.object(pdf, "_resolve_pdf_route", resolve))
        stack.enter_context(patch.object(pdf, "proxy_multipart_to_downstream", proxy))
        yield parse, resolve, proxy


@contextmanager
def _get_dependencies(
    payload: object,
    *,
    route: OperationRoute | None = None,
):
    selected_route = route or _route()
    resolve = AsyncMock(
        return_value=(selected_route, "https://converter.example/pdf/api", {}, Mock(), 0, 0.0)
    )
    proxy = AsyncMock(
        return_value=(
            json.dumps(payload).encode("utf-8"),
            200,
            "application/json",
            {},
        )
    )
    with ExitStack() as stack:
        stack.enter_context(patch.object(pdf, "_resolve_pdf_route", resolve))
        stack.enter_context(patch.object(pdf, "_proxy_get_raw_to_downstream", proxy))
        yield resolve, proxy


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


def _wire_body(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        bytes(message.get("body", b""))
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _assert_no_legacy_writes(services) -> None:
    services.tokens_usage_db.insert_usage.assert_not_called()
    services.tokens_usage_db.insert_usage_once.assert_not_called()
    services.api_keys_db.record_spent.assert_not_called()
    services.rate_limiter.add_tokens.assert_not_called()


def test_pdf_convert_commits_upstream_zero_without_legacy_writes() -> None:
    async def scenario() -> None:
        request, services = _request("POST", "/v1/pdf/convert")
        events: list[object] = []
        response_started = False

        def before_commit(_event) -> None:
            assert response_started is False

        _install_successful_commit(
            services.accounting_service,
            events,
            before_return=before_commit,
        )
        services.active_requests_registry.start(
            request_id="request-1",
            path="/v1/pdf/convert",
            api_key_id=7,
        )
        payload = {"status": "completed", "usage": {"cost": 0}}

        with _post_dependencies(payload) as (_parse, _resolve, proxy):
            response = await pdf.convert_pdf(request)

        assert response.status_code == 200
        assert _response_json(response) == payload
        proxy.assert_awaited_once()
        services.accounting_service.commit.assert_not_awaited()
        assert services.active_requests_registry.list_records()

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
        assert len(events) == 1
        assert events[0].usage.cost == 0.0
        assert events[0].cost_source is CostSource.UPSTREAM
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        assert services.active_requests_registry.list_records() == []
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_pdf_post_and_get_after_restart_use_same_canonical_event() -> None:
    async def scenario() -> None:
        post_services = _services()
        restarted_services = _services()
        events: list[object] = []
        _install_successful_commit(post_services.accounting_service, events)
        _install_successful_commit(restarted_services.accounting_service, events)
        payload = {
            "id": "job-1",
            "status": "completed",
            "usage": {"cost": 0.25},
        }
        post_request, _ = _request(
            "POST",
            "/v1/pdf/jobs",
            request_id="create-request",
            services=post_services,
        )
        get_request, _ = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            request_id="poll-request",
            services=restarted_services,
        )

        with _post_dependencies(payload):
            post_response = await pdf.create_pdf_job(post_request)
        with _get_dependencies(payload):
            get_response = await pdf.get_pdf_job(
                "job-1",
                "gateway/pdf",
                get_request,
            )

        assert post_response.status_code == get_response.status_code == 200
        assert events == []
        post_messages = await _send_response(post_request, post_response)
        get_messages = await _send_response(get_request, get_response)
        assert _wire_body(post_messages) == post_response.body
        assert _wire_body(get_messages) == get_response.body
        assert len(events) == 2
        first, repeated = events
        assert first.event_id == repeated.event_id
        assert first.billing_fingerprint == repeated.billing_fingerprint
        assert first.request_id == "create-request"
        assert repeated.request_id == "poll-request"
        assert first.method == repeated.method == "POST"
        assert first.route_template == repeated.route_template == "/v1/pdf/jobs"
        assert (first.provider, first.model) == ("converter", "pdf-model")
        assert (repeated.provider, repeated.model) == ("converter", "pdf-model")
        post_services.accounting_service.commit.assert_awaited_once()
        restarted_services.accounting_service.commit.assert_awaited_once()
        post_services.accounting_service.release.assert_not_awaited()
        restarted_services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(post_services)
        _assert_no_legacy_writes(restarted_services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("route", "gateway_model", "cost"),
    [
        (_route("converter-b", "pdf-model"), "gateway/pdf", 0.25),
        (_route("converter", "pdf-model-b"), "gateway/pdf", 0.25),
        (_route(), "gateway/pdf-b", 0.25),
        (_route(), "gateway/pdf", 0.5),
    ],
)
def test_pdf_job_same_identity_route_model_or_usage_drift_changes_fingerprint(
    route: OperationRoute,
    gateway_model: str,
    cost: float,
) -> None:
    async def scenario() -> None:
        services = _services()
        events: list[object] = []
        _install_successful_commit(services.accounting_service, events)
        first_request, _ = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            request_id="poll-1",
            services=services,
        )
        changed_request, _ = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            request_id="poll-2",
            services=services,
        )
        baseline = {"id": "job-1", "status": "completed", "usage": {"cost": 0.25}}
        changed = {"id": "job-1", "status": "completed", "usage": {"cost": cost}}

        with _get_dependencies(baseline):
            first_response = await pdf.get_pdf_job(
                "job-1",
                "gateway/pdf",
                first_request,
            )
        with _get_dependencies(changed, route=route):
            changed_response = await pdf.get_pdf_job(
                "job-1",
                gateway_model,
                changed_request,
            )

        assert events == []
        first_messages = await _send_response(first_request, first_response)
        changed_messages = await _send_response(changed_request, changed_response)
        assert _wire_body(first_messages) == first_response.body
        assert _wire_body(changed_messages) == changed_response.body
        assert len(events) == 2
        assert events[0].event_id == events[1].event_id
        assert events[0].billing_fingerprint != events[1].billing_fingerprint

    run_async(scenario())


def test_pdf_job_fingerprint_conflict_is_fail_closed() -> None:
    async def scenario() -> None:
        request, services = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
        )
        services.accounting_service.commit.side_effect = AccountingError(
            AccountingErrorCode.FINGERPRINT_CONFLICT
        )
        payload = {"id": "job-1", "status": "completed", "usage": {"cost": 0.25}}

        with _get_dependencies(payload):
            response = await pdf.get_pdf_job("job-1", "gateway/pdf", request)

        assert response.status_code == 200
        assert _response_json(response) == payload
        services.accounting_service.commit.assert_not_awaited()

        messages = await _send_response(request, response)
        starts = [
            message
            for message in messages
            if message.get("type") == "http.response.start"
        ]
        assert [message.get("status") for message in starts] == [503]
        assert json.loads(_wire_body(messages))["error"]["code"] == (
            "accounting_unavailable"
        )
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_pdf_job_different_auth_identity_uses_separate_event() -> None:
    async def scenario() -> None:
        events: list[object] = []
        first_services = _services()
        second_services = _services()
        _install_successful_commit(first_services.accounting_service, events)
        _install_successful_commit(second_services.accounting_service, events)
        first_request, _ = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            request_id="master-poll",
            api_key_id=None,
            services=first_services,
        )
        second_request, _ = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            request_id="key-poll",
            api_key_id=7,
            services=second_services,
        )
        payload = {"id": "job-1", "status": "completed"}

        with _get_dependencies(payload):
            first_response = await pdf.get_pdf_job(
                "job-1",
                "gateway/pdf",
                first_request,
            )
        with _get_dependencies(payload):
            second_response = await pdf.get_pdf_job(
                "job-1",
                "gateway/pdf",
                second_request,
            )

        assert events == []
        first_messages = await _send_response(first_request, first_response)
        second_messages = await _send_response(second_request, second_response)
        assert _wire_body(first_messages) == first_response.body
        assert _wire_body(second_messages) == second_response.body
        assert len(events) == 2
        assert events[0].event_id != events[1].event_id
        assert events[0].billing_fingerprint != events[1].billing_fingerprint

    run_async(scenario())


@pytest.mark.parametrize(
    ("method", "status"),
    [
        ("POST", "queued"),
        ("GET", "running"),
        ("POST", "failed"),
        ("GET", "cancelled"),
    ],
)
def test_pdf_job_non_success_releases_without_charge(method: str, status: str) -> None:
    async def scenario() -> None:
        route_template = "/v1/pdf/jobs" if method == "POST" else "/v1/pdf/jobs/{job_id}"
        path = route_template if method == "POST" else "/v1/pdf/jobs/job-1"
        request, services = _request(method, route_template, path=path)
        payload = {
            "id": "job-1",
            "status": status,
            "usage": {"cost": "invalid-but-not-billable"},
        }

        if method == "POST":
            with _post_dependencies(payload):
                response = await pdf.create_pdf_job(request)
        else:
            with _get_dependencies(payload):
                response = await pdf.get_pdf_job("job-1", "gateway/pdf", request)

        assert response.status_code == 200
        assert _response_json(response) == payload
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("calculators", "expected_cost", "expected_source"),
    [
        (
            {
                ("pdf_conversion", "gateway/pdf"): OperationCostCalculator(
                    unit="operation",
                    rate_usd=0.7,
                )
            },
            0.7,
            CostSource.OPERATION_CONFIGURED,
        ),
        ({}, DEFAULT_OPERATION_COST_USD, CostSource.OPERATION_DEFAULT),
    ],
)
def test_pdf_job_success_without_usage_uses_calculator_or_default(
    calculators: dict[tuple[str, str], OperationCostCalculator],
    expected_cost: float,
    expected_source: CostSource,
) -> None:
    async def scenario() -> None:
        request, services = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
            calculators=calculators,
        )
        events: list[object] = []
        _install_successful_commit(services.accounting_service, events)
        payload = {"id": "job-1", "status": "completed"}

        with _get_dependencies(payload):
            response = await pdf.get_pdf_job("job-1", "gateway/pdf", request)

        assert response.status_code == 200
        services.accounting_service.commit.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        assert events[0].usage.cost == expected_cost
        assert events[0].cost_source is expected_source
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_pdf_job_invalid_present_cost_fails_closed_and_releases() -> None:
    async def scenario() -> None:
        request, services = _request(
            "GET",
            "/v1/pdf/jobs/{job_id}",
            path="/v1/pdf/jobs/job-1",
        )
        payload = {
            "id": "job-1",
            "status": "completed",
            "usage": {"cost": "invalid"},
        }

        with _get_dependencies(payload):
            response = await pdf.get_pdf_job("job-1", "gateway/pdf", request)

        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize(
    "payload",
    [
        {"status": "completed"},
        {"id": "other-job", "status": "completed"},
    ],
)
def test_pdf_job_missing_or_mismatched_job_id_fails_closed(payload: dict[str, object]) -> None:
    async def scenario() -> None:
        method = "POST" if "id" not in payload else "GET"
        route_template = "/v1/pdf/jobs" if method == "POST" else "/v1/pdf/jobs/{job_id}"
        path = route_template if method == "POST" else "/v1/pdf/jobs/job-1"
        request, services = _request(method, route_template, path=path)

        if method == "POST":
            with _post_dependencies(payload):
                response = await pdf.create_pdf_job(request)
        else:
            with _get_dependencies(payload):
                response = await pdf.get_pdf_job("job-1", "gateway/pdf", request)

        assert response.status_code == 503
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_pdf_missing_accounting_context_fails_before_downstream() -> None:
    async def scenario() -> None:
        request, services = _request(
            "POST",
            "/v1/pdf/jobs",
            with_context=False,
        )
        payload = {"id": "job-1", "status": "queued"}

        with _post_dependencies(payload) as (parse, resolve, proxy):
            response = await pdf.create_pdf_job(request)

        assert response.status_code == 503
        parse.assert_called_once()
        resolve.assert_not_awaited()
        proxy.assert_not_awaited()
        messages = await _send_response(request, response)
        assert _wire_body(messages) == response.body
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())
