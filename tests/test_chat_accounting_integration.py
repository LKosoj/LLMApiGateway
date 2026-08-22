from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
from fastapi import FastAPI, HTTPException, Request
from starlette.background import BackgroundTask
from starlette.requests import ClientDisconnect
from starlette.responses import Response, StreamingResponse

from llm_gateway_core.api.v1 import chat as chat_api
from llm_gateway_core.api.v1.chat_accounting import (
    ChatTerminalHandoff,
    publish_chat_terminal_observation,
)
from llm_gateway_core.middleware.accounting_admission import (
    ACCOUNTING_REQUEST_CONTEXT_STATE_KEY,
    AccountingRequestContext,
)
from llm_gateway_core.middleware.chat_logging import (
    prepare_chat_response_observation,
)
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.middleware.response_observation import (
    ResponseObservationMiddleware,
    response_observation_published,
)
from llm_gateway_core.services.accounting import (
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
from llm_gateway_core.services.chat_accounting import ObservedChatResponse
from llm_gateway_core.services.stream_observation import SSEFramer
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.utils.usage_tracking import ModelCostRates, estimate_prompt_tokens
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _context(
    route_template: str = "/v1/chat/completions",
) -> AccountingRequestContext:
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


def _observation() -> ChatTerminalObservation:
    component = BillingComponent(
        provider="provider-a",
        model="model-a",
        usage=AccountingUsage(
            prompt_tokens=2,
            completion_tokens=3,
            total_tokens=5,
            cost=0.01,
        ),
        cost_source=CostSource.UPSTREAM,
    )
    return ChatTerminalObservation(
        top_provider="provider-a",
        top_model="model-a",
        components=(component,),
    )


def _receipt(event) -> AccountingReceipt:
    return AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
        usage_row_id=1,
    )


def _scope(
    *,
    snapshot,
    context: AccountingRequestContext | None,
    path: str = "/v1/chat/completions",
) -> dict:
    state = {"runtime_snapshot": snapshot}
    if context is not None:
        state[ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = context
    return {
        "type": "http",
        "asgi": {"version": "3.0", "spec_version": "2.4"},
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "query_string": b"",
        "root_path": "",
        "headers": [
            (b"content-type", b"application/json"),
            (b"content-length", b"49"),
        ],
        "client": ("127.0.0.1", 1234),
        "server": ("testserver", 80),
        "state": state,
    }


async def _run_app(
    app: FastAPI,
    *,
    disconnect_after_request: bool = False,
    include_accounting_context: bool = True,
    path: str = "/v1/chat/completions",
    request_payload: dict | None = None,
) -> list[dict]:
    if request_payload is None:
        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hi"}],
        }
    request_body = json.dumps(request_payload).encode()
    receive_count = 0

    async def receive() -> dict:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return {"type": "http.request", "body": request_body, "more_body": False}
        if disconnect_after_request:
            return {"type": "http.disconnect"}
        await asyncio.sleep(0)
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[dict] = []
    never_sent = asyncio.Event()

    async def send(message: dict) -> None:
        if disconnect_after_request:
            await never_sent.wait()
        sent.append(message)

    snapshot = app.state.snapshot
    context = _context(path) if include_accounting_context else None
    scope = _scope(snapshot=snapshot, context=context, path=path)
    if disconnect_after_request:
        scope["asgi"]["spec_version"] = "2.3"
    await app(
        scope,
        receive,
        send,
    )
    return sent


def _app() -> tuple[FastAPI, object, list[str]]:
    app = FastAPI()
    app.add_middleware(
        ResponseObservationMiddleware,
        request_preparer=prepare_chat_response_observation,
    )
    services = make_app_services()
    services.accounting_service.release.return_value = True
    events: list[str] = []

    async def commit(_reservation, event):
        events.append("commit")
        return _receipt(event)

    services.accounting_service.commit.side_effect = commit
    app.state.services = services
    app.state.snapshot = make_runtime_snapshot(
        cost_rate_registry={
            ("provider-a", "model-a"): ModelCostRates(1.0, 2.0),
        }
    )
    return app, services.accounting_service, events


def _direct_request(*, anthropic_payload: dict | None = None) -> Request:
    app = FastAPI()
    app.state.services = make_app_services()
    snapshot = make_runtime_snapshot(
        cost_rate_registry={
            ("provider-a", "model-a"): ModelCostRates(1.0, 2.0),
        }
    )
    state = {"runtime_snapshot": snapshot}
    if anthropic_payload is not None:
        state["llmgateway_original_anthropic_payload"] = anthropic_payload
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
            "state": state,
        }
    )


def _provider_config(*, provider_type: str = "openai") -> SimpleNamespace:
    return SimpleNamespace(
        apikey="upstream-secret",
        baseUrl="https://provider.invalid",
        models={},
        routing=None,
        type=provider_type,
        upstream_key_pools=None,
    )


def _dispatch_request(*, fusion: bool = False, router: bool = False) -> tuple[Request, object]:
    app = FastAPI()
    app.state.services = make_app_services()
    snapshot = make_runtime_snapshot()
    loader = snapshot.config_loader
    loader.fallback_rules = {}
    loader.model_rules = {}
    loader.fusion_rules = {"gateway-model": {"panel": []}} if fusion else {}
    loader.router_rules = (
        {"gateway-model": {"selector_model": "selector", "targets": []}}
        if router
        else {}
    )
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
            "state": {"runtime_snapshot": snapshot},
        }
    )
    return request, snapshot


async def _attempt_direct(
    response_data: object,
    *,
    streaming: bool,
    provider_type: str = "openai",
    request: Request | None = None,
) -> tuple[object, ChatTerminalHandoff]:
    if request is None:
        request = _direct_request()
    handoff = ChatTerminalHandoff()
    auth_material = SimpleNamespace(
        headers={"Authorization": "Bearer upstream-secret"},
        upstream_key_fingerprint=None,
    )
    with (
        patch.object(
            chat_api,
            "resolve_provider_auth_material",
            new=AsyncMock(return_value=auth_material),
        ),
        patch.object(
            chat_api,
            "make_llm_request",
            new=AsyncMock(return_value=(response_data, None)),
        ),
    ):
        result, error, _attempt_number = await chat_api._attempt_model_fallback_rule(
            request,
            object(),
            {"provider-a": _provider_config(provider_type=provider_type)},
            "gateway-model",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": streaming,
            },
            {"provider": "provider-a", "model": "model-a"},
            streaming,
            proxy_http_clients={},
            upstream_routing_state=UpstreamRoutingState(),
            chat_accounting_handoff=handoff,
            cost_rate_registry={
                ("provider-a", "model-a"): ModelCostRates(1.0, 2.0),
            },
        )
    assert error is None
    assert result is not None
    return result, handoff


def _observe_handoff_stream(handoff: ChatTerminalHandoff, payload: bytes) -> None:
    batch = SSEFramer().feed(payload)
    for event in batch.events:
        handoff.observe_stream_event(event)


def test_direct_json_publishes_strict_observation_before_client_conversion() -> None:
    async def scenario() -> None:
        # The former edge-case fixture used `"choices": []` to stand in for a
        # "successful" non-stream response. On the non-stream path that shape
        # is now itself a degenerate response (see
        # chat_dispatch.py::detect_degenerate_non_stream_response) and fails
        # over instead of reaching accounting, so this test's own invariant —
        # strict observation is published before the response is converted
        # for the client — needs a real choices-shaped payload to exercise.
        # Degenerate-response detection itself is covered by
        # tests/test_chat_model_behavior.py and tests/test_chat_fallback.py.
        payload = {
            "id": "chatcmpl-direct",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost": 0,
            },
        }

        result, handoff = await _attempt_direct(payload, streaming=False)

        assert result is payload
        observation = handoff.take()
        assert observation.top_provider == "provider-a"
        assert observation.top_model == "model-a"
        assert observation.usage.cost == 0

    run_async(scenario())


def test_direct_openai_stream_publishes_at_upstream_terminal_and_preserves_bytes() -> None:
    async def scenario() -> None:
        expected = (
            b'data: {"choices":[],"usage":{"prompt_tokens":2,'
            b'"completion_tokens":3,"total_tokens":5,"cost":0}}\n\n'
            b"data: [DONE]\n\n"
        )

        async def body() -> AsyncIterator[bytes]:
            yield expected[:57]
            yield expected[57:]

        response = StreamingResponse(body(), media_type="text/event-stream")
        source_iterator = response.body_iterator
        result, handoff = await _attempt_direct(
            response,
            streaming=True,
        )
        assert isinstance(result, StreamingResponse)
        assert result.body_iterator is source_iterator

        received = b"".join([chunk async for chunk in result.body_iterator])

        assert received == expected
        _observe_handoff_stream(handoff, received)
        observation = handoff.take()
        assert observation.usage.prompt_tokens == 2
        assert observation.usage.completion_tokens == 3
        assert observation.usage.cost == 0

    run_async(scenario())


def test_direct_openai_stream_rejects_non_finite_json() -> None:
    async def scenario() -> None:
        frames = b'data: {"choices":[],"usage":{},"value":NaN}\n\n'

        async def body() -> AsyncIterator[bytes]:
            yield frames

        response = StreamingResponse(body(), media_type="text/event-stream")
        result, handoff = await _attempt_direct(response, streaming=True)
        assert isinstance(result, StreamingResponse)

        received = b"".join([chunk async for chunk in result.body_iterator])
        with pytest.raises(AccountingValidationError):
            _observe_handoff_stream(handoff, received)

    run_async(scenario())


def test_direct_native_anthropic_stream_publishes_actual_split_usage() -> None:
    async def scenario() -> None:
        frames = (
            b'event: message_start\ndata: {"type":"message_start","message":'
            b'{"usage":{"input_tokens":2,"output_tokens":0}}}\n\n'
            b'event: message_delta\ndata: {"type":"message_delta","delta":'
            b'{"stop_reason":"end_turn"},"usage":{"output_tokens":3,"cost":0}}\n\n'
            b'event: message_stop\ndata: {"type":"message_stop"}\n\n'
        )

        async def body() -> AsyncIterator[bytes]:
            yield frames[:81]
            yield frames[81:]

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hi"}],
            "stream": True,
        }
        request = _direct_request(anthropic_payload=anthropic_payload)
        response = StreamingResponse(body(), media_type="text/event-stream")
        source_iterator = response.body_iterator
        result, handoff = await _attempt_direct(
            response,
            streaming=True,
            provider_type="anthropic",
            request=request,
        )
        assert isinstance(result, StreamingResponse)
        assert result.body_iterator is source_iterator

        received = b"".join([chunk async for chunk in result.body_iterator])

        assert received == frames
        _observe_handoff_stream(handoff, received)
        observation = handoff.take()
        assert observation.usage.prompt_tokens == 2
        assert observation.usage.completion_tokens == 3
        assert observation.usage.cost == 0

    run_async(scenario())


def test_direct_responses_stream_uses_client_wire_observer_without_wrapper() -> None:
    async def scenario() -> None:
        frames = (
            b'event: response.completed\ndata: {"type":"response.completed",'
            b'"response":{"usage":{"input_tokens":2,"output_tokens":3,'
            b'"total_tokens":5,"cost":0}}}\n\n'
            b"data: [DONE]\n\n"
        )

        async def body() -> AsyncIterator[bytes]:
            yield frames

        request = _direct_request()
        request.state.llmgateway_original_responses_payload = {"stream": True}
        response = StreamingResponse(body(), media_type="text/event-stream")
        source_iterator = response.body_iterator
        result, handoff = await _attempt_direct(
            response,
            streaming=True,
            request=request,
        )

        assert result is response
        assert response.body_iterator is source_iterator
        received = b"".join([chunk async for chunk in response.body_iterator])
        _observe_handoff_stream(handoff, received)
        observation = handoff.take()
        assert observation.usage.prompt_tokens == 2
        assert observation.usage.completion_tokens == 3
        assert observation.usage.cost == 0

    run_async(scenario())


def test_direct_stream_without_actual_usage_commits_estimated_usage() -> None:
    """A stream that reaches ``[DONE]`` without ever emitting a usage payload
    no longer fails closed: the upstream call already succeeded, so accounting
    falls back to a tiktoken-based estimate instead of losing the already
    incurred cost."""

    async def scenario() -> None:
        async def body() -> AsyncIterator[bytes]:
            yield b'data: {"choices":[{"finish_reason":"stop"}]}\n\n'
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(body(), media_type="text/event-stream")
        source_iterator = response.body_iterator
        result, handoff = await _attempt_direct(
            response,
            streaming=True,
        )
        assert isinstance(result, StreamingResponse)
        assert result.body_iterator is source_iterator

        received = b"".join([chunk async for chunk in result.body_iterator])
        _observe_handoff_stream(handoff, received)
        observation = handoff.take()

        expected_prompt_tokens = estimate_prompt_tokens(
            json.dumps(
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                }
            ),
            "model-a",
        )
        assert observation.usage.is_estimated is True
        assert observation.usage.prompt_tokens == expected_prompt_tokens
        # No delta content was ever streamed, so the estimated completion is 0.
        assert observation.usage.completion_tokens == 0
        assert observation.usage.cost == pytest.approx(
            expected_prompt_tokens * 1.0 / 1_000_000
        )

    run_async(scenario())


def test_fusion_dispatch_uses_observed_result_for_outer_handoff() -> None:
    async def scenario() -> None:
        request, snapshot = _dispatch_request(fusion=True)
        handoff = ChatTerminalHandoff()
        public_response = {"id": "fusion-result", "choices": [], "usage": {}}
        observed = ObservedChatResponse(
            response=public_response,
            observation=_observation(),
        )
        run_observed = AsyncMock(return_value=observed)
        run_legacy = AsyncMock()

        with (
            patch.object(snapshot.fusion_service, "run_observed", new=run_observed),
            patch.object(snapshot.fusion_service, "run", new=run_legacy),
        ):
            result = await chat_api._dispatch_chat_request(
                request,
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                enforce_model_access=False,
                accounting_handoff=handoff,
            )

        assert result is public_response
        assert handoff.take() is observed.observation
        run_observed.assert_awaited_once()
        run_legacy.assert_not_awaited()

    run_async(scenario())


def test_direct_dispatch_threads_exact_handoff_and_runtime_rates() -> None:
    async def scenario() -> None:
        request, snapshot = _dispatch_request()
        services = request.app.state.services
        snapshot.config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-a"}
                ],
                "rotate_models": False,
            }
        }
        handoff = ChatTerminalHandoff()
        public_response = _openai_json_response()

        async def attempt(*_args, **kwargs):
            assert kwargs["chat_accounting_handoff"] is handoff
            assert kwargs["cost_rate_registry"] is snapshot.cost_rate_registry
            assert (
                kwargs["stream_observation_capacity"]
                is services.stream_observation_capacity
            )
            assert (
                kwargs["stream_event_max_bytes"]
                == services.stream_event_max_bytes
            )
            handoff.publish(_observation())
            request.state.llmgateway_provider = "provider-a"
            request.state.llmgateway_provider_model = "model-a"
            return public_response, None, 2

        with (
            patch.object(chat_api, "_attempt_model_fallback_rule", new=attempt),
            patch.object(
                chat_api.settings,
                "stream_event_max_bytes",
                services.stream_event_max_bytes + 1,
            ),
        ):
            result = await chat_api._dispatch_chat_request(
                request,
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                enforce_model_access=False,
                accounting_handoff=handoff,
            )

        assert result is public_response
        assert handoff.take().top_model == "model-a"

    run_async(scenario())


def test_local_stream_failure_returns_503_without_next_fallback() -> None:
    async def scenario() -> None:
        request, snapshot = _dispatch_request()
        snapshot.config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-a"},
                    {"provider": "provider-b", "model": "model-b"},
                ],
                "rotate_models": False,
            }
        }
        attempts = 0

        async def attempt(*_args, **_kwargs):
            nonlocal attempts
            attempts += 1
            raise chat_api.LocalStreamObservationError(
                "decode_capacity_exhausted"
            )

        with (
            patch.object(chat_api, "_attempt_model_fallback_rule", new=attempt),
            pytest.raises(HTTPException) as raised,
        ):
            await chat_api._dispatch_chat_request(
                request,
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                enforce_model_access=False,
            )

        assert raised.value.status_code == 503
        assert raised.value.detail == "Local stream observation is unavailable."
        assert attempts == 1

    run_async(scenario())


def test_stream_without_first_token_falls_back_to_next_model() -> None:
    """An upstream that never emits a token is a provider failure, not ours."""

    async def scenario() -> None:
        request, snapshot = _dispatch_request()
        snapshot.config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-a"},
                    {"provider": "provider-b", "model": "model-b"},
                ],
                "rotate_models": False,
            }
        }
        second_response = StreamingResponse(
            iter(()),
            media_type="text/event-stream",
        )
        attempted_models: list[str] = []

        async def attempt(*args, **kwargs):
            model_fallback_rule = args[5]
            attempted_models.append(model_fallback_rule["model"])
            if len(attempted_models) == 1:
                return (
                    None,
                    "Stream ended before any content chunks were received.",
                    1,
                )
            request.state.llmgateway_provider = "provider-b"
            request.state.llmgateway_provider_model = "model-b"
            return second_response, None, 2

        with patch.object(chat_api, "_attempt_model_fallback_rule", new=attempt):
            result = await chat_api._dispatch_chat_request(
                request,
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                    "stream": True,
                },
                enforce_model_access=False,
            )

        assert attempted_models == ["model-a", "model-b"]
        assert result is second_response

    run_async(scenario())


def test_router_dispatch_uses_observed_result_for_outer_handoff() -> None:
    async def scenario() -> None:
        request, snapshot = _dispatch_request(router=True)
        handoff = ChatTerminalHandoff()
        public_response = {"id": "router-result", "choices": [], "usage": {}}
        observed = ObservedChatResponse(
            response=public_response,
            observation=_observation(),
        )
        run_observed = AsyncMock(return_value=observed)
        run_legacy = AsyncMock()

        with (
            patch.object(
                snapshot.router_model_service,
                "run_observed",
                new=run_observed,
            ),
            patch.object(snapshot.router_model_service, "run", new=run_legacy),
        ):
            result = await chat_api._dispatch_chat_request(
                request,
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hi"}],
                },
                enforce_model_access=False,
                accounting_handoff=handoff,
            )

        assert result is public_response
        assert handoff.take() is observed.observation
        run_observed.assert_awaited_once()
        run_legacy.assert_not_awaited()

    run_async(scenario())


def _install_chat_routes(app: FastAPI) -> None:
    app.include_router(chat_api.router, prefix="/v1/chat")
    app.include_router(chat_api.anthropic_router, prefix="/v1")
    app.include_router(chat_api.responses_router, prefix="/v1")


def _openai_json_response() -> dict:
    return {
        "id": "chatcmpl-integration",
        "object": "chat.completion",
        "created": 1,
        "model": "model-a",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 3,
            "total_tokens": 5,
            "cost": 0,
        },
    }


def _openai_stream_response() -> StreamingResponse:
    async def body() -> AsyncIterator[bytes]:
        yield (
            b'data: {"id":"chatcmpl-integration","object":"chat.completion.chunk",'
            b'"created":1,"model":"model-a","choices":[{"index":0,"delta":'
            b'{"role":"assistant","content":"ok"},"finish_reason":null}]}\n\n'
        )
        yield (
            b'data: {"id":"chatcmpl-integration","object":"chat.completion.chunk",'
            b'"created":1,"model":"model-a","choices":[{"index":0,"delta":{},'
            b'"finish_reason":"stop"}],"usage":{"prompt_tokens":2,'
            b'"completion_tokens":3,"total_tokens":5,"cost":0}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


@pytest.mark.parametrize(
    ("path", "request_payload", "expected_shape"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            "choices",
        ),
        (
            "/v1/messages",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
            },
            "content",
        ),
        (
            "/v1/responses",
            {"model": "gateway-model", "input": "hi"},
            "output",
        ),
    ],
)
def test_client_json_dialects_commit_typed_handoff_without_legacy_write(
    path: str,
    request_payload: dict,
    expected_shape: str,
) -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        _install_chat_routes(app)

        async def dispatch(
            _request: Request,
            _payload: dict,
            *,
            enforce_model_access: bool = True,
            accounting_handoff: ChatTerminalHandoff | None = None,
        ) -> dict:
            assert enforce_model_access
            assert accounting_handoff is not None
            accounting_handoff.publish(_observation())
            return _openai_json_response()

        with patch.object(chat_api, "_dispatch_chat_request", new=dispatch):
            sent = await _run_app(
                app,
                path=path,
                request_payload=request_payload,
            )

        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        public_payload = json.loads(body)
        assert expected_shape in public_payload
        accounting_service.commit.assert_awaited_once()
        accounting_service.release.assert_not_awaited()
        app.state.services.tokens_usage_db.insert_usage.assert_not_called()

    run_async(scenario())


@pytest.mark.parametrize(
    ("path", "request_payload", "terminal_marker"),
    [
        (
            "/v1/chat/completions",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            b"data: [DONE]\n\n",
        ),
        (
            "/v1/messages",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
            },
            b"event: message_stop\n",
        ),
        (
            "/v1/responses",
            {"model": "gateway-model", "input": "hi", "stream": True},
            b'"type":"response.completed"',
        ),
    ],
)
def test_client_sse_dialects_commit_typed_handoff_without_legacy_write(
    path: str,
    request_payload: dict,
    terminal_marker: bytes,
) -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        _install_chat_routes(app)

        async def dispatch(
            _request: Request,
            _payload: dict,
            *,
            enforce_model_access: bool = True,
            accounting_handoff: ChatTerminalHandoff | None = None,
        ) -> StreamingResponse:
            assert enforce_model_access
            assert accounting_handoff is not None
            accounting_handoff.publish(_observation())
            return _openai_stream_response()

        with patch.object(chat_api, "_dispatch_chat_request", new=dispatch):
            sent = await _run_app(
                app,
                path=path,
                request_payload=request_payload,
            )

        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert terminal_marker in body
        accounting_service.commit.assert_awaited_once()
        accounting_service.release.assert_not_awaited()
        app.state.services.tokens_usage_db.insert_usage.assert_not_called()

    run_async(scenario())


def test_chat_observability_is_logging_only() -> None:
    services = make_app_services()
    services.usd_budget_ledger.commit_reserved = Mock()
    services.rate_limiter.add_tokens = Mock()
    tokens_usage = {
        "api_key_id": 7,
        "gateway_model": "gateway-model",
        "provider": "provider-a",
        "model": "model-a",
        "prompt_tokens": 2,
        "completion_tokens": 3,
        "total_tokens": 5,
        "cost": 0.01,
        "_usd_budget_reserved": True,
        "_usd_budget_reserved_estimate": 0.1,
    }

    with patch.object(chat_logging.settings, "log_chat_messages", False):
        chat_logging.record_chat_observability(
            {},
            '{"model":"gateway-model"}',
            "ok",
            tokens_usage,
            services=services,
        )

    services.tokens_usage_db.insert_usage.assert_not_called()
    services.api_keys_db.record_spent.assert_not_called()
    services.usd_budget_ledger.commit_reserved.assert_not_called()
    services.rate_limiter.add_tokens.assert_not_called()


def test_response_observation_commits_buffered_response_before_first_send() -> None:
    async def scenario() -> None:
        app, service, events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> Response:
            await request.body()
            publish_chat_terminal_observation(request, _observation())
            return Response(
                b'{"result":"exact-json-body"}',
                status_code=201,
                media_type="application/json",
                headers={"x-test": "exact"},
            )

        sent: list[dict] = []

        async def send(message: dict) -> None:
            events.append(message["type"])
            sent.append(message)

        request_body = json.dumps({"model": "gateway-model"}).encode()
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await app(
            _scope(snapshot=app.state.snapshot, context=_context()),
            receive,
            send,
        )

        assert events[0] == "commit"
        assert sent[0]["type"] == "http.response.start"
        assert sent[0]["status"] == 201
        assert (b"x-test", b"exact") in sent[0]["headers"]
        assert b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ) == b'{"result":"exact-json-body"}'
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_response_observation_drives_chunk_and_transport_lifecycle_once() -> None:
    async def scenario() -> None:
        app, service, _events = _app()
        instances: list[FakeChunkProcessor] = []

        class FakeChunkProcessor:
            def __init__(self, *_args: object, **_kwargs: object) -> None:
                self.tokens_usage: dict[str, object] = {}
                self.started = 0
                self.chunks: list[bytes] = []
                self.finished = 0
                self.waited = 0
                instances.append(self)

            def start(self) -> None:
                self.started += 1

            async def enqueue_chunk(self, body: bytes) -> None:
                self.chunks.append(body)

            async def finish(self) -> None:
                self.finished += 1

            async def wait(self, _timeout: float) -> bool:
                self.waited += 1
                return True

            def task_exception(self) -> None:
                return None

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> Response:
            await request.body()
            publish_chat_terminal_observation(request, _observation())
            return Response(b'{"result":"ok"}', media_type="application/json")

        registry = app.state.services.active_requests_registry
        with (
            patch.object(chat_logging, "ChunkProcessor", FakeChunkProcessor),
            patch.object(registry, "finish", wraps=registry.finish) as finish,
        ):
            sent = await _run_app(app)

        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert body == b'{"result":"ok"}'
        assert len(instances) == 1
        processor = instances[0]
        assert processor.started == 1
        assert processor.chunks == [body]
        assert processor.finished == 1
        assert processor.waited == 1
        finish.assert_called_once()
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_response_observation_holds_openai_terminal_until_commit() -> None:
    async def scenario() -> None:
        app, service, events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()
            publish_chat_terminal_observation(request, _observation())

            async def body() -> AsyncIterator[bytes]:
                yield b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        sent: list[dict] = []

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body" and b"[DONE]" in message.get(
                "body", b""
            ):
                events.append("terminal-send")
            sent.append(message)

        request_body = json.dumps(
            {"model": "gateway-model", "stream": True}
        ).encode()
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        await app(
            _scope(snapshot=app.state.snapshot, context=_context()),
            receive,
            send,
        )

        assert events == ["commit", "terminal-send"]
        assert b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ).endswith(b"data: [DONE]\n\n")
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_response_observation_ignores_openai_null_keepalive() -> None:
    async def scenario() -> None:
        app, service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()
            publish_chat_terminal_observation(request, _observation())

            async def body() -> AsyncIterator[bytes]:
                yield b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n'
                yield b"data: null\n\n"
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        sent = await _run_app(
            app,
            request_payload={"model": "gateway-model", "stream": True},
        )
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert b"data: null" in body
        assert body.endswith(b"data: [DONE]\n\n")
        statuses = [
            message.get("status")
            for message in sent
            if message["type"] == "http.response.start"
        ]
        assert statuses == [200]
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_response_observation_releases_before_openai_error_event_send() -> None:
    async def scenario() -> None:
        app, service, events = _app()

        async def release(_reservation) -> bool:
            events.append("release")
            return True

        service.release.side_effect = release

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()

            async def body() -> AsyncIterator[bytes]:
                yield b'data: {"error":{"message":"failed"}}\n\n'

            return StreamingResponse(body(), media_type="text/event-stream")

        request_body = json.dumps(
            {"model": "gateway-model", "stream": True}
        ).encode()
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {
                    "type": "http.request",
                    "body": request_body,
                    "more_body": False,
                }
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body" and b'"error"' in message.get(
                "body", b""
            ):
                events.append("error-send")

        await app(
            _scope(snapshot=app.state.snapshot, context=_context()),
            receive,
            send,
        )

        assert events == ["release", "error-send"]
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


def test_response_observation_rejects_openai_done_after_error() -> None:
    async def scenario() -> None:
        app, service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()

            async def body() -> AsyncIterator[bytes]:
                yield (
                    b'data: {"error":{"message":"failed"}}\n\n'
                    b"data: [DONE]\n\n"
                )

            return StreamingResponse(body(), media_type="text/event-stream")

        sent = await _run_app(
            app,
            request_payload={"model": "gateway-model", "stream": True},
        )

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert response_start["status"] == 502
        assert json.loads(body)["error"]["code"] == "upstream_protocol_error"
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


def test_response_observation_rejects_invalid_sse_on_http_error() -> None:
    async def scenario() -> None:
        app, service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()

            async def body() -> AsyncIterator[bytes]:
                yield b"data: not-json\n\n"

            return StreamingResponse(
                body(),
                status_code=502,
                media_type="text/event-stream",
            )

        sent = await _run_app(
            app,
            request_payload={"model": "gateway-model", "stream": True},
        )

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert response_start["status"] == 502
        assert json.loads(body)["error"]["code"] == "upstream_protocol_error"
        service.release.assert_awaited_once()
        service.commit.assert_not_awaited()

    run_async(scenario())


def test_response_observation_releases_on_immediate_disconnect() -> None:
    async def scenario() -> None:
        app, service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()
            publish_chat_terminal_observation(request, _observation())

            async def body() -> AsyncIterator[bytes]:
                await asyncio.Event().wait()
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        await _run_app(
            app,
            disconnect_after_request=True,
            request_payload={"model": "gateway-model", "stream": True},
        )

        service.commit.assert_not_awaited()
        service.release.assert_awaited_once()

    run_async(scenario())


def test_early_chat_4xx_without_body_read_preserves_response_and_releases() -> None:
    async def scenario() -> None:
        app, service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(_request: Request) -> Response:
            return Response(
                b'{"error":"invalid request"}',
                status_code=400,
                media_type="application/json",
            )

        with patch.object(chat_logging, "record_chat_observability"):
            sent = await _run_app(app)

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 400
        assert b"invalid request" in b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        service.commit.assert_not_awaited()
        service.release.assert_awaited_once()

    run_async(scenario())


def test_response_observation_preserves_raw_headers_and_background() -> None:
    async def scenario() -> None:
        app, service, _events = _app()
        background_calls: list[str] = []
        expected_headers = [
            (b"content-type", b"text/event-stream; charset=utf-8"),
            (b"set-cookie", b"first=1; Path=/"),
            (b"set-cookie", b"second=2; Path=/"),
            (b"x-duplicate", b"first"),
            (b"x-duplicate", b"second"),
        ]

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()
            publish_chat_terminal_observation(request, _observation())

            async def body() -> AsyncIterator[bytes]:
                yield b"data: [DONE]\n\n"

            response = StreamingResponse(body(), media_type="text/event-stream")
            response.raw_headers = expected_headers
            response.background = BackgroundTask(background_calls.append, "done")
            return response

        sent = await _run_app(
            app,
            request_payload={"model": "gateway-model", "stream": True},
        )

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        assert response_start["headers"] == expected_headers
        assert background_calls == ["done"]
        service.commit.assert_awaited_once()
        service.release.assert_not_awaited()

    run_async(scenario())


def test_chat_terminal_handoff_is_one_shot() -> None:
    handoff = ChatTerminalHandoff()

    handoff.publish(_observation())

    with pytest.raises(AccountingValidationError):
        handoff.publish(_observation())
    assert isinstance(handoff.take(), ChatTerminalObservation)
    with pytest.raises(AccountingValidationError):
        handoff.take()


def test_chat_suffix_lookalike_bypasses_accounting_owner() -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        endpoint_calls = 0

        @app.post("/shadow/v1/chat/completions")
        async def endpoint() -> Response:
            nonlocal endpoint_calls
            endpoint_calls += 1
            return Response(b"lookalike-ok", media_type="application/json")

        sent = await _run_app(
            app,
            path="/shadow/v1/chat/completions",
            include_accounting_context=False,
        )

        assert endpoint_calls == 1
        assert b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        ) == b"lookalike-ok"
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_unmatched_chat_suffix_bypasses_middleware_without_runtime_snapshot() -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.4"},
                "http_version": "1.1",
                "method": "POST",
                "scheme": "http",
                "path": "/shadow/v1/chat/completions",
                "raw_path": b"/shadow/v1/chat/completions",
                "query_string": b"",
                "root_path": "",
                "headers": [],
                "client": ("127.0.0.1", 1234),
                "server": ("testserver", 80),
                "state": {},
                "app": app,
            }
        )

        await prepare_chat_response_observation(request)

        assert not response_observation_published(request.scope)
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_exact_chat_without_context_fails_before_endpoint_upstream() -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        endpoint_called = False

        @app.post("/v1/chat/completions")
        async def endpoint() -> Response:
            nonlocal endpoint_called
            endpoint_called = True
            return Response(b"must-not-run")

        request_body = json.dumps({"model": "gateway-model"}).encode()
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            return {"type": "http.disconnect"}

        async def send(_message: dict) -> None:
            return None

        scope = _scope(snapshot=app.state.snapshot, context=_context())
        del scope["state"][ACCOUNTING_REQUEST_CONTEXT_STATE_KEY]
        with pytest.raises(AccountingValidationError):
            await app(scope, receive, send)

        assert not endpoint_called
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize(
    "request_payload",
    [
        pytest.param({}, id="missing-model"),
        pytest.param({"model": 17}, id="malformed-model"),
    ],
)
def test_invalid_model_4xx_releases_admitted_chat_reservation(
    request_payload: dict,
) -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        endpoint_calls = 0

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> Response:
            nonlocal endpoint_calls
            endpoint_calls += 1
            await request.body()
            return Response(b"invalid-model", status_code=400)

        sent = await _run_app(app, request_payload=request_payload)

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        assert response_start["status"] == 400
        assert endpoint_calls == 1
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_body_read_error_releases_owner_and_preserves_primary() -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()
        primary = RuntimeError("request body failed")

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> Response:
            await request.body()
            return Response(b"must-not-run")

        async def receive() -> dict:
            raise primary

        async def send(_message: dict) -> None:
            return None

        with pytest.raises(RuntimeError, match="request body failed") as exc_info:
            await app(
                _scope(snapshot=app.state.snapshot, context=_context()),
                receive,
                send,
            )

        assert exc_info.value is primary
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_success_without_typed_observation_returns_accounting_failure() -> None:
    async def scenario() -> None:
        app, accounting_service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> Response:
            await request.body()
            return Response(b'{"result":"unobserved"}', media_type="application/json")

        sent = await _run_app(app)

        response_start = next(
            message for message in sent if message["type"] == "http.response.start"
        )
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message["type"] == "http.response.body"
        )
        assert response_start["status"] == 503
        assert b"accounting_unavailable" in body
        accounting_service.commit.assert_not_awaited()
        accounting_service.release.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize("terminal_first", [False, True])
def test_response_observation_send_failure_commits_already_available_usage(
    terminal_first: bool,
) -> None:
    """A client-disconnect during send now commits whatever observation the
    handoff already holds, whether or not the terminal SSE event was seen
    before the send failed: the gateway already paid upstream for those
    tokens, so a mid-stream disconnect must not silently release them."""

    async def scenario() -> None:
        app, accounting_service, _events = _app()

        @app.post("/v1/chat/completions")
        async def endpoint(request: Request) -> StreamingResponse:
            await request.body()
            publish_chat_terminal_observation(request, _observation())

            async def body() -> AsyncIterator[bytes]:
                if not terminal_first:
                    yield b'data: {"choices":[{"delta":{"content":"partial"}}]}\n\n'
                yield b"data: [DONE]\n\n"

            return StreamingResponse(body(), media_type="text/event-stream")

        request_body = json.dumps(
            {"model": "gateway-model", "stream": True}
        ).encode()
        received = False

        async def receive() -> dict:
            nonlocal received
            if not received:
                received = True
                return {"type": "http.request", "body": request_body, "more_body": False}
            await asyncio.sleep(0)
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            if message["type"] == "http.response.body":
                raise OSError("client disconnected")

        with pytest.raises(ClientDisconnect):
            await app(
                _scope(snapshot=app.state.snapshot, context=_context()),
                receive,
                send,
            )

        # The endpoint published the terminal observation eagerly, before any
        # bytes were streamed. Whether the client disconnect happens before or
        # after the terminal ``[DONE]`` event was classified, the handoff
        # already holds a complete observation, so both cases must commit it.
        accounting_service.commit.assert_awaited_once()
        accounting_service.release.assert_not_awaited()

    run_async(scenario())
