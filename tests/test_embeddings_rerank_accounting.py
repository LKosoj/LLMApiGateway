from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from contextlib import ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1 import embeddings
from llm_gateway_core.config.loader import OperationRoute
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
)
from llm_gateway_core.services.accounting import (
    AccountingReceipt,
    AccountingReservation,
    CostSource,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _route(provider: str = "provider-a", model: str = "model-a") -> OperationRoute:
    return OperationRoute(
        provider=provider,
        model=model,
        target_path=f"/{model}",
    )


def _request(
    path: str,
    *,
    cost_rate_registry: dict[tuple[str, str], ModelCostRates] | None = None,
    include_accounting_context: bool = True,
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
    snapshot = make_runtime_snapshot(
        cost_rate_registry=cost_rate_registry or {},
    )
    app = FastAPI()
    app.state.services = services
    state: dict[str, object] = {
        "llmgateway_request_id": "request-1",
        "runtime_snapshot": snapshot,
    }
    if include_accounting_context:
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
                "headers": [],
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


@contextmanager
def _endpoint_dependencies(
    request_body: dict,
    routes: list[OperationRoute],
    downstream_result: object,
):
    dispatcher = Mock(spec=OperationDispatcher)
    dispatcher.lookup_routes.return_value = routes
    dispatcher.build_target_url.side_effect = (
        lambda route, _provider_config: f"https://{route.provider}.example{route.target_path}"
    )
    dispatcher.build_headers.return_value = {}
    dispatcher.build_payload.side_effect = (
        lambda body, route, _operation: {**body, "model": route.model}
    )
    dispatcher.set_request_state.return_value = None
    config_loader = SimpleNamespace(
        providers_config={route.provider: object() for route in routes}
    )
    downstream = AsyncMock()
    if isinstance(downstream_result, list):
        downstream.side_effect = downstream_result
    else:
        downstream.return_value = downstream_result

    with ExitStack() as stack:
        stack.enter_context(
            patch.object(
                embeddings,
                "read_json_request_body",
                AsyncMock(return_value=request_body),
            )
        )
        stack.enter_context(patch.object(embeddings, "enforce_virtual_key_access"))
        stack.enter_context(
            patch.object(
                embeddings,
                "_get_operation_runtime",
                return_value=(dispatcher, Mock(), config_loader, {}),
            )
        )
        stack.enter_context(
            patch.object(
                embeddings,
                "resolve_provider_auth_headers",
                AsyncMock(return_value={}),
            )
        )
        stack.enter_context(
            patch.object(embeddings, "proxy_json_to_downstream", downstream)
        )
        yield dispatcher, downstream


def _response_json(response) -> dict:
    return json.loads(response.body)


async def _run_response(
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


def _body_bytes(messages: list[dict[str, object]]) -> bytes:
    return b"".join(
        message.get("body", b"")
        for message in messages
        if message.get("type") == "http.response.body"
    )


def _assert_no_legacy_writes(services) -> None:
    services.tokens_usage_db.insert_usage.assert_not_called()
    services.tokens_usage_db.insert_usage_once.assert_not_called()
    services.api_keys_db.record_spent.assert_not_called()


@pytest.mark.parametrize(
    ("usage", "registry", "expected_cost", "expected_source"),
    [
        (
            {"prompt_tokens": 4, "total_tokens": 4, "cost": 0},
            {},
            0.0,
            CostSource.UPSTREAM,
        ),
        (
            {"prompt_tokens": 4, "total_tokens": 4},
            {("provider-a", "model-a"): ModelCostRates(1.5, 2.5)},
            0.000006,
            CostSource.TOKEN_REGISTRY,
        ),
    ],
)
def test_buffered_embeddings_commits_upstream_or_registry_cost(
    usage: dict,
    registry: dict[tuple[str, str], ModelCostRates],
    expected_cost: float,
    expected_source: CostSource,
) -> None:
    async def scenario() -> None:
        request, services = _request(
            "/v1/embeddings",
            cost_rate_registry=registry,
        )
        _install_successful_commit(services.accounting_service)
        downstream_payload = {
            "data": [{"embedding": [0.1], "index": 0}],
            "usage": usage,
        }
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"]},
            [_route()],
            (downstream_payload, 200),
        ):
            response = await embeddings.create_embeddings(request)

        services.accounting_service.commit.assert_not_awaited()
        await _run_response(request, response)
        assert response.status_code == 200
        assert _response_json(response) == downstream_payload
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        event = services.accounting_service.commit.await_args.args[1]
        assert event.provider == "provider-a"
        assert event.model == "model-a"
        assert event.usage.cost == expected_cost
        assert event.usage.duration_ms is not None
        assert event.cost_source is expected_source
        _assert_no_legacy_writes(services)

    run_async(scenario())


@pytest.mark.parametrize(
    ("downstream_payload", "registry"),
    [
        (
            {"usage": {"prompt_tokens": 2, "total_tokens": 2, "cost": "bad"}},
            {("provider-a", "model-a"): ModelCostRates(1.0, 1.0)},
        ),
        ({"data": []}, {}),
    ],
)
def test_buffered_embeddings_fails_closed_for_invalid_or_missing_cost_usage(
    downstream_payload: dict,
    registry: dict[tuple[str, str], ModelCostRates],
) -> None:
    async def scenario() -> None:
        request, services = _request(
            "/v1/embeddings",
            cost_rate_registry=registry,
        )
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"]},
            [_route()],
            (downstream_payload, 200),
        ):
            response = await embeddings.create_embeddings(request)

        await _run_response(request, response)
        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_buffered_embeddings_commits_cost_unavailable_when_registry_price_missing() -> None:
    """A missing/unconfigured registry price must not turn an already-successful
    upstream call into a 503: usage is recorded with ``cost_unavailable=True``
    and the response still succeeds."""

    async def scenario() -> None:
        request, services = _request(
            "/v1/embeddings",
            cost_rate_registry={},
        )
        _install_successful_commit(services.accounting_service)
        downstream_payload = {"usage": {"prompt_tokens": 2, "total_tokens": 2}}
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"]},
            [_route()],
            (downstream_payload, 200),
        ):
            response = await embeddings.create_embeddings(request)

        await _run_response(request, response)
        assert response.status_code == 200
        assert _response_json(response) == downstream_payload
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == 0.0
        assert event.usage.cost_unavailable is True
        assert event.cost_source is CostSource.TOKEN_REGISTRY
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_buffered_rerank_normalizes_before_committing_raw_usage() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/rerank")
        _install_successful_commit(services.accounting_service)
        downstream_payload = {
            "results": [{"index": 1, "relevance_score": 0.98}],
            "usage": {"input_tokens": 3, "total_tokens": 3, "cost": 0},
        }
        with _endpoint_dependencies(
            {
                "model": "gateway-rerank",
                "query": "query",
                "documents": ["A", "B"],
            },
            [_route()],
            (downstream_payload, 200),
        ):
            response = await embeddings.create_rerank(request)

        services.accounting_service.commit.assert_not_awaited()
        await _run_response(request, response)
        assert response.status_code == 200
        assert _response_json(response) == {"data": [{"index": 1, "score": 0.98}]}
        event = services.accounting_service.commit.await_args.args[1]
        assert event.operation == "rerank"
        assert event.usage.prompt_tokens == 3
        assert event.cost_source is CostSource.UPSTREAM
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_rerank_normalization_failure_releases_without_commit() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/rerank")
        downstream_payload = {
            "results": [{"index": 0, "relevance_score": "not-a-score"}],
            "usage": {"input_tokens": 3, "total_tokens": 3, "cost": 0},
        }
        with _endpoint_dependencies(
            {
                "model": "gateway-rerank",
                "query": "query",
                "documents": ["A"],
            },
            [_route()],
            (downstream_payload, 200),
        ):
            with pytest.raises(HTTPException) as exc_info:
                await embeddings.create_rerank(request)

        assert exc_info.value.status_code == 502
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_fallback_commits_only_final_route_identity() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/embeddings")
        _install_successful_commit(services.accounting_service)
        routes = [
            _route("provider-a", "model-a"),
            _route("provider-b", "model-b"),
        ]
        downstream_payload = {
            "data": [],
            "usage": {"prompt_tokens": 2, "total_tokens": 2, "cost": 0},
        }
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"]},
            routes,
            [HTTPException(status_code=503, detail="primary failed"), (downstream_payload, 200)],
        ) as (_, downstream):
            response = await embeddings.create_embeddings(request)

        services.accounting_service.commit.assert_not_awaited()
        await _run_response(request, response)
        assert response.status_code == 200
        assert downstream.await_count == 2
        event = services.accounting_service.commit.await_args.args[1]
        assert event.provider == "provider-b"
        assert event.model == "model-b"
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_missing_accounting_context_fails_closed() -> None:
    async def scenario() -> None:
        request, services = _request(
            "/v1/embeddings",
            include_accounting_context=False,
        )
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"]},
            [_route()],
            ({"usage": {"prompt_tokens": 1, "total_tokens": 1, "cost": 0}}, 200),
        ) as (_, downstream):
            response = await embeddings.create_embeddings(request)

        await _run_response(request, response)
        assert response.status_code == 503
        assert _response_json(response)["error"]["code"] == "accounting_unavailable"
        downstream.assert_not_awaited()
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_stream_commits_actual_usage_before_done_frame() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/embeddings")
        yielded: list[bytes] = []

        def before_commit(_event) -> None:
            assert b"[DONE]" not in b"".join(yielded)

        _install_successful_commit(
            services.accounting_service,
            before_return=before_commit,
        )
        source_chunks = [
            b'data: {"data":[{"embedding":[0.1]}]}\n\n',
            b'data: {"usage":{"prompt_tokens":2,"total_tokens":2,"cost":0}}\n\n',
            b"data: [DO",
            b"NE]\n\n",
        ]

        async def source() -> AsyncIterator[bytes]:
            for chunk in source_chunks:
                yield chunk

        downstream = StreamingResponse(source(), media_type="text/event-stream")
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"], "stream": True},
            [_route()],
            downstream,
        ):
            response = await embeddings.create_embeddings(request)

        assert isinstance(response, StreamingResponse)
        assert response is downstream

        def before_send(message: dict[str, object]) -> None:
            if message.get("type") == "http.response.body":
                yielded.append(bytes(message.get("body", b"")))

        messages = await _run_response(
            request,
            response,
            before_send=before_send,
        )

        assert _body_bytes(messages) == b"".join(source_chunks)
        event = services.accounting_service.commit.await_args.args[1]
        assert event.provider == "provider-a"
        assert event.model == "model-a"
        assert event.usage.prompt_tokens == 2
        assert event.cost_source is CostSource.UPSTREAM
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_stream_eof_without_done_releases_and_closes_transport() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/embeddings")

        async def source() -> AsyncIterator[bytes]:
            yield b'data: {"usage":{"prompt_tokens":2,"total_tokens":2,"cost":0}}\n\n'
            yield b"partial"

        downstream = StreamingResponse(source(), media_type="text/event-stream")
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"], "stream": True},
            [_route()],
            downstream,
        ):
            response = await embeddings.create_embeddings(request)

        assert isinstance(response, StreamingResponse)
        with pytest.raises(RuntimeError):
            await _run_response(request, response)

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_stream_non_finite_json_returns_protocol_error() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/embeddings")

        async def source() -> AsyncIterator[bytes]:
            yield (
                b'data: {"usage":{"prompt_tokens":2,"total_tokens":2,'
                b'"cost":0},"value":NaN}\n\n'
            )
            yield b"data: [DONE]\n\n"

        downstream = StreamingResponse(source(), media_type="text/event-stream")
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"], "stream": True},
            [_route()],
            downstream,
        ):
            response = await embeddings.create_embeddings(request)

        messages = await _run_response(request, response)

        response_start = next(
            message
            for message in messages
            if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 502
        assert json.loads(_body_bytes(messages))["error"]["code"] == (
            "upstream_protocol_error"
        )
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())


def test_embeddings_stream_disconnect_before_iteration_releases_without_charge() -> None:
    async def scenario() -> None:
        request, services = _request("/v1/embeddings")

        async def source() -> AsyncIterator[bytes]:
            yield b'data: {"usage":{"prompt_tokens":2,"total_tokens":2,"cost":0}}\n\n'
            yield b"data: [DONE]\n\n"

        downstream = StreamingResponse(source(), media_type="text/event-stream")
        with _endpoint_dependencies(
            {"model": "gateway-model", "input": ["hello"], "stream": True},
            [_route()],
            downstream,
        ):
            response = await embeddings.create_embeddings(request)

        assert isinstance(response, StreamingResponse)
        never_sent = asyncio.Event()

        async def receive() -> dict[str, object]:
            return {"type": "http.disconnect"}

        async def send(_message: dict[str, object]) -> None:
            await never_sent.wait()

        async def app(scope, app_receive, app_send) -> None:
            await response(scope, app_receive, app_send)

        scope = {
            **request.scope,
            "asgi": {"version": "3.0", "spec_version": "2.3"},
        }
        await ResponseObservationMiddleware(app)(scope, receive, send)

        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()
        _assert_no_legacy_writes(services)

    run_async(scenario())
