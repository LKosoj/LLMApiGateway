import copy
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1 import chat as chat_api
from llm_gateway_core.api.v1.responses_upstream import (
    chat_request_to_responses,
    responses_response_to_chat,
    responses_stream_to_chat,
    responses_usage_to_chat,
)
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.chat_accounting import build_direct_chat_terminal_observation
from llm_gateway_core.services.request_handler import make_llm_request
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async


def _event(kind: str, data: dict) -> bytes:
    return f"event: {kind}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n".encode()


async def _body(response):
    result = b""
    async for chunk in response.body_iterator:
        result += chunk
    return result


def test_request_conversion_covers_messages_tools_and_does_not_mutate_input():
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": "rules"},
            {
                "role": "user",
                "content": [{"type": "text", "text": "hi"}, {"type": "image_url", "image_url": {"url": "x"}}],
            },
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "call-1", "type": "function", "function": {"name": "f", "arguments": "{}"}}],
            },
            {"role": "tool", "tool_call_id": "call-1", "content": "result"},
        ],
        "max_tokens": 42,
        "reasoning_effort": "high",
        "response_format": {"type": "json_schema", "json_schema": {"name": "answer", "schema": {"type": "object"}}},
        "tools": [
            {"type": "function", "function": {"name": "f", "description": "desc", "parameters": {"type": "object"}}}
        ],
        "tool_choice": {"type": "function", "function": {"name": "f"}},
        "stream_options": {"include_usage": True},
    }
    original = copy.deepcopy(payload)
    result = chat_request_to_responses(payload)
    assert payload == original
    assert result["input"][0] == {"role": "system", "content": "rules"}
    assert result["input"][1]["content"][1] == {"type": "input_image", "image_url": "x"}
    assert result["input"][3]["type"] == "function_call"
    assert result["input"][4] == {"type": "function_call_output", "call_id": "call-1", "output": "result"}
    assert result["max_output_tokens"] == 42
    assert result["reasoning"] == {"effort": "high"}
    assert result["text"]["format"] == {"type": "json_schema", "name": "answer", "schema": {"type": "object"}}
    assert result["tools"][0]["name"] == "f"
    assert result["tool_choice"] == {"type": "function", "name": "f"}
    assert "messages" not in result and "stream_options" not in result


@pytest.mark.parametrize(
    "field",
    [
        "stop",
        "logprobs",
        "top_logprobs",
        "logit_bias",
        "seed",
        "audio",
        "modalities",
        "prediction",
        "web_search_options",
        "functions",
        "function_call",
    ],
)
def test_request_conversion_rejects_unsupported_fields(field):
    with pytest.raises(HTTPException) as exc:
        chat_request_to_responses({"messages": [], field: 1})
    assert exc.value.status_code == 400


def test_request_conversion_rejects_n_greater_than_one():
    with pytest.raises(HTTPException) as exc:
        chat_request_to_responses({"messages": [], "n": 2})
    assert exc.value.status_code == 400


@pytest.mark.parametrize("value", [0, 0.7, -1])
@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("field", ["frequency_penalty", "presence_penalty"])
def test_request_conversion_removes_penalties_without_mutating_input(field, value, stream):
    payload = {"messages": [], field: value, "stream": stream}
    original = copy.deepcopy(payload)

    result = chat_request_to_responses(payload)

    assert result == {"input": [], "stream": stream}
    assert payload == original


def test_request_conversion_moves_verbosity_into_text_without_losing_format():
    payload = {"messages": [], "verbosity": "low", "text": {"format": {"type": "text"}}}
    original = copy.deepcopy(payload)
    result = chat_request_to_responses(payload)
    assert result["text"] == {"format": {"type": "text"}, "verbosity": "low"}
    assert "verbosity" not in result
    assert payload == original


@pytest.mark.parametrize("strict", [None, False, True])
def test_chat_function_strict_default_is_preserved(strict):
    function = {"name": "lookup", "parameters": {"type": "object", "properties": {}}}
    if strict is not None:
        function["strict"] = strict
    result = chat_request_to_responses({"messages": [], "tools": [{"type": "function", "function": function}]})
    assert result["tools"][0]["strict"] is (strict if strict is not None else False)


def test_usage_preserves_zero_cost_and_nested_cached_reasoning_usage():
    usage = responses_usage_to_chat(
        {
            "input_tokens": 3,
            "output_tokens": 4,
            "total_tokens": 7,
            "cost": 0,
            "input_tokens_details": {"cached_tokens": 2},
            "output_tokens_details": {"reasoning_tokens": 1},
        }
    )
    assert usage == {
        "total_tokens": 7,
        "cost": 0,
        "prompt_tokens": 3,
        "completion_tokens": 4,
        "prompt_tokens_details": {"cached_tokens": 2},
        "completion_tokens_details": {"reasoning_tokens": 1},
    }


def test_response_conversion_handles_message_tools_refusal_and_incomplete():
    result = responses_response_to_chat(
        {
            "id": "resp-1",
            "model": "m",
            "created_at": 12,
            "status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [
                {
                    "type": "message",
                    "content": [{"type": "output_text", "text": "hello"}, {"type": "refusal", "refusal": "no"}],
                },
                {"type": "reasoning", "summary": [{"text": "think"}]},
                {"type": "function_call", "call_id": "c", "name": "f", "arguments": "{}"},
            ],
            "usage": {"input_tokens": 1, "output_tokens": 2, "cost": 0},
        }
    )
    message = result["choices"][0]["message"]
    assert message["content"] == "hello"
    assert message["refusal"] == "no"
    assert message["reasoning_content"] == "think"
    assert message["tool_calls"][0]["id"] == "c"
    assert result["choices"][0]["finish_reason"] == "length"
    assert result["usage"]["cost"] == 0


def test_response_conversion_rejects_failed_or_unknown_status():
    assert responses_response_to_chat({"error": {"message": "bad"}}) == {"error": {"message": "bad"}}
    assert "error" in responses_response_to_chat({"status": "failed"})


def test_fragmented_responses_sse_converts_utf8_and_two_function_calls_once():
    events = [
        _event(
            "response.created", {"type": "response.created", "response": {"id": "r", "model": "m", "created_at": 1}}
        ),
        _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "Привет"}),
        _event(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 7,
                "item": {"type": "function_call", "call_id": "c1", "name": "a", "arguments": ""},
            },
        ),
        _event(
            "response.function_call_arguments.delta",
            {"type": "response.function_call_arguments.delta", "output_index": 7, "delta": '{"x"'},
        ),
        _event(
            "response.output_item.added",
            {
                "type": "response.output_item.added",
                "output_index": 2,
                "item": {"type": "function_call", "call_id": "c2", "name": "b", "arguments": ""},
            },
        ),
        _event(
            "response.function_call_arguments.delta",
            {"type": "response.function_call_arguments.delta", "output_index": 2, "delta": "}"},
        ),
        _event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "r",
                    "model": "m",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 2, "cost": 0},
                },
            },
        ),
    ]

    raw_events = b"".join(events)

    class Source:
        def __init__(self):
            self.closed = False
            self.offset = 0

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self.offset >= len(raw_events):
                raise StopAsyncIteration
            data = raw_events[self.offset : self.offset + 1]
            self.offset += 1
            return data

        async def aclose(self):
            self.closed = True

    async def scenario():
        source = Source()
        response = responses_stream_to_chat(StreamingResponse(source, media_type="text/event-stream"), "m")
        body = await _body(response)
        assert body.count(b"data: [DONE]") == 1
        chunks = [json.loads(line[6:]) for line in body.decode().splitlines() if line.startswith("data: {")]
        assert any(chunk["choices"][0]["delta"].get("content") == "Привет" for chunk in chunks)
        calls = [
            chunk["choices"][0]["delta"].get("tool_calls", [{}])[0]
            for chunk in chunks
            if chunk["choices"][0]["delta"].get("tool_calls")
        ]
        assert [call["index"] for call in calls] == [0, 0, 1, 1]
        assert source.closed

    run_async(scenario())


def test_stream_failed_and_truncated_never_emit_done_and_close_source():
    async def scenario():
        for chunks in (
            [_event("response.failed", {"type": "response.failed", "error": {"message": "bad"}})],
            [_event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "partial"})],
        ):

            class Source:
                closed = False

                def __aiter__(self):
                    return self

                async def __anext__(self):
                    if chunks:
                        return chunks.pop(0)
                    raise StopAsyncIteration

                async def aclose(self):
                    self.closed = True

            source = Source()
            body = await _body(responses_stream_to_chat(StreamingResponse(source), "m"))
            assert b"data: [DONE]" not in body
            assert source.closed

    run_async(scenario())


def _request() -> Request:
    app = FastAPI()
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
            "client": ("127.0.0.1", 1),
            "server": ("test", 80),
            "app": app,
            "state": {},
        }
    )


@pytest.mark.parametrize("stream", [False, True])
@pytest.mark.parametrize("provider_order", [None, False, True])
@pytest.mark.parametrize("incompatible", [{"stop": ["END"]}, {"n": 2}, {"seed": 42}])
def test_conversion_failure_advances_to_chat_candidate(stream, provider_order, incompatible):
    async def scenario():
        request = _request()
        routing_state = UpstreamRoutingState()
        events = Mock()
        providers = {
            "p": ProviderDetails(
                baseUrl="https://provider.example/v1",
                apikey="key",
                models={"responses-model": {"api_type": "responses"}},
            )
        }
        first_rule = {"provider": "p", "model": "responses-model", "retry_count": 2}
        if provider_order is not None:
            first_rule.update(providers_order=["first", "second"], use_provider_order_as_fallback=provider_order)
        request.state.runtime_snapshot = SimpleNamespace(
            config_loader=SimpleNamespace(
                providers_config=providers,
                model_rules={},
                fallback_rules={"gateway": {
                    "fallback_models": [first_rule, {"provider": "p", "model": "chat-model"}],
                    "rotate_models": False,
                }},
            ),
            proxy_http_clients={},
            cost_rate_registry={},
        )
        request.app.state.services = SimpleNamespace(
            http_client=object(),
            active_requests_registry=ActiveRequestsRegistry(),
            fallback_events_db=events,
            upstream_routing_state=routing_state,
            stream_observation_capacity=StreamObservationCapacity(max_items=8, max_bytes=65536),
            stream_event_max_bytes=4096,
        )
        payload = {
            "model": "gateway", "messages": [{"role": "user", "content": "hi"}],
            "presence_penalty": 0.7, "frequency_penalty": 0.5, "stream": stream, **incompatible,
        }
        original = copy.deepcopy(payload)
        completion = {"choices": [{"message": {"role": "assistant", "content": "ok"}, "finish_reason": "stop"}]}
        if stream:
            async def chunks():
                yield f"data: {json.dumps(completion)}\n\n".encode()
                yield b"data: [DONE]\n\n"

            response = StreamingResponse(chunks(), media_type="text/event-stream")
        else:
            response = completion
        make_request = AsyncMock(return_value=(response, None))
        with (
            patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request),
            patch.object(routing_state, "record_failure", wraps=routing_state.record_failure) as record_failure,
            patch.object(routing_state, "record_attempt_start", wraps=routing_state.record_attempt_start) as record_start,
        ):
            result = await chat_api._dispatch_chat_request(request, payload, enforce_model_access=False)
            if stream:
                assert b"ok" in await _body(result)
            else:
                assert result["choices"][0]["message"]["content"] == "ok"
        assert payload == original
        make_request.assert_awaited_once()
        assert make_request.await_args.args[1] == "https://provider.example/v1/chat/completions"
        sent = make_request.await_args.args[3]
        assert sent["presence_penalty"] == 0.7 and sent["frequency_penalty"] == 0.5
        for field, value in incompatible.items():
            assert sent[field] == value
        assert record_start.call_count == 1
        record_failure.assert_not_called()
        attempts = request.state.llmgateway_fallback_attempts
        assert [entry["model"] for entry in attempts] == ["responses-model", "chat-model"]
        assert attempts[0]["error_class"] == "http_400" and attempts[0]["http_status"] == 400
        assert [call.kwargs["success"] for call in events.insert_event.call_args_list] == [False, True]
        assert [call.kwargs["attempt_number"] for call in events.insert_event.call_args_list] == [1, 2]

    run_async(scenario())


def test_provider_schema_rejects_invalid_api_type_and_anthropic_override():
    with pytest.raises(ValueError, match="api_type"):
        ProviderDetails(baseUrl="https://x.example", apikey="key", models={"m": {"api_type": "other"}})
    with pytest.raises(ValueError, match="api_type"):
        ProviderDetails(
            baseUrl="https://x.example", apikey="key", type="anthropic", models={"m": {"api_type": "responses"}}
        )


def test_fallback_selects_responses_url_payload_and_converts_response():
    async def scenario():
        request = _request()
        response = {
            "id": "r",
            "model": "upstream",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
            "usage": {"input_tokens": 1, "output_tokens": 2, "cost": 0},
        }
        make_request = AsyncMock(return_value=(response, None))
        provider = SimpleNamespace(
            baseUrl="https://provider.example/v1",
            apikey="key",
            models={"upstream": {"api_type": "responses"}},
            type="openai",
            routing=None,
            upstream_key_pools=None,
        )
        with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request):
            result, error, _ = await chat_api._attempt_model_fallback_rule(
                request,
                object(),
                {"p": provider},
                "gateway",
                {
                    "model": "gateway", "messages": [{"role": "user", "content": "hi"}],
                    "frequency_penalty": 0.5, "presence_penalty": 0.7,
                },
                {"provider": "p", "model": "upstream"},
                False,
                proxy_http_clients={},
                upstream_routing_state=UpstreamRoutingState(),
            )
        assert error is None
        assert result["choices"][0]["message"]["content"] == "ok"
        assert make_request.await_args.args[1].endswith("/responses")
        assert make_request.await_args.args[3]["input"] == [{"role": "user", "content": "hi"}]
        assert "presence_penalty" not in make_request.await_args.args[3]
        assert "frequency_penalty" not in make_request.await_args.args[3]

    run_async(scenario())


@pytest.mark.parametrize("use_provider_order", [False, True])
def test_responses_fallback_preserves_provider_order(use_provider_order):
    async def scenario():
        request = _request()
        response = {
            "id": "r",
            "model": "upstream",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
        }
        make_request = AsyncMock(return_value=(response, None))
        providers = {
            "first": SimpleNamespace(
                baseUrl="https://first.example/v1",
                apikey="key-1",
                models={"upstream": {"api_type": "responses"}},
                type="openai",
                routing=None,
                upstream_key_pools=None,
            ),
            "second": SimpleNamespace(
                baseUrl="https://second.example/v1",
                apikey="key-2",
                models={"upstream": {"api_type": "responses"}},
                type="openai",
                routing=None,
                upstream_key_pools=None,
            ),
        }
        rule = {
            "provider": "second",
            "model": "upstream",
            "providers_order": ["first", "second"],
            "use_provider_order_as_fallback": use_provider_order,
        }
        with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request):
            result, error, _ = await chat_api._attempt_model_fallback_rule(
                request,
                object(),
                providers,
                "gateway",
                {"model": "gateway", "messages": [{"role": "user", "content": "hi"}]},
                rule,
                False,
                proxy_http_clients={},
                upstream_routing_state=UpstreamRoutingState(),
            )
        assert error is None
        assert result["choices"][0]["message"]["content"] == "ok"
        assert make_request.await_args.args[1] == "https://second.example/v1/responses"
        assert make_request.await_args.args[3]["input"] == [{"role": "user", "content": "hi"}]
        expected_order = ["first"] if use_provider_order else ["first", "second"]
        assert make_request.await_args.args[3]["provider"] == {"order": expected_order}

    run_async(scenario())


def test_real_non_streaming_responses_request_accepts_error_null():
    async def scenario():
        seen = {}

        def handler(request):
            seen["payload"] = json.loads(request.content)
            return httpx.Response(
                200,
                json={
                    "error": None,
                    "id": "r",
                    "model": "m",
                    "status": "completed",
                    "output": [{"type": "message", "content": [{"type": "output_text", "text": "ok"}]}],
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response, error = await make_llm_request(
                client,
                "https://provider.example/v1/responses",
                {},
                {"model": "m", "input": [{"role": "user", "content": "hi"}]},
                False,
            )
        assert error is None
        assert responses_response_to_chat(response)["choices"][0]["message"]["content"] == "ok"
        assert seen["payload"]["input"][0]["content"] == "hi"

    run_async(scenario())


def test_request_conversion_accepts_flat_responses_json_schema_and_tool_choice():
    result = chat_request_to_responses(
        {
            "messages": [{"role": "user", "content": "hi"}],
            "response_format": {"type": "json_schema", "name": "answer", "schema": {"type": "object"}, "strict": True},
            "tool_choice": {"type": "function", "name": "lookup"},
        }
    )
    assert result["text"]["format"] == {
        "type": "json_schema",
        "name": "answer",
        "schema": {"type": "object"},
        "strict": True,
    }
    assert result["tool_choice"] == {"type": "function", "name": "lookup"}


def test_direct_observation_uses_upstream_cost_or_registry_when_missing():
    registry = {("p", "m"): ModelCostRates(input_rate=2.0, output_rate=3.0)}
    with_cost = build_direct_chat_terminal_observation(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 3, "cost": 0}},
        provider="p",
        model="m",
        cost_rate_registry=registry,
    )
    without_cost = build_direct_chat_terminal_observation(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 3}}, provider="p", model="m", cost_rate_registry=registry
    )
    assert with_cost.usage.cost == 0
    assert without_cost.usage.cost == pytest.approx(0.000013)


def test_real_streaming_request_returns_before_end_and_closes_capacity():
    async def scenario():
        content = _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})
        # The existing reader asks HTTPX for fixed-size raw blocks.
        content += b":" + b" " * (4096 - len(content) - 3) + b"\n\n"
        terminal = _event(
            "response.completed",
            {
                "type": "response.completed",
                "response": {
                    "id": "r",
                    "model": "m",
                    "status": "completed",
                    "output": [],
                    "usage": {"input_tokens": 1, "output_tokens": 1},
                },
            },
        )
        terminal_consumed = False

        class Body(httpx.AsyncByteStream):
            async def __aiter__(self):
                nonlocal terminal_consumed
                yield content
                terminal_consumed = True
                yield terminal

            async def aclose(self):
                pass

        def handler(request):
            assert request.url.path.endswith("/responses")
            return httpx.Response(200, headers={"content-type": "text/event-stream"}, stream=Body())

        capacity = StreamObservationCapacity(max_items=8, max_bytes=65536)
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            response, error = await make_llm_request(
                client,
                "https://provider.example/v1/responses",
                {},
                {"model": "m", "input": []},
                True,
                stream_observation_capacity=capacity,
                stream_event_max_bytes=4096,
            )
            assert error is None and response is not None
            assert not terminal_consumed
            first = await response.body_iterator.__anext__()
            assert b"response.output_text.delta" in first
            remaining = await _body(response)
            assert b"response.completed" in first + remaining
            assert terminal_consumed
        assert capacity.snapshot.active_bytes == 0

    run_async(scenario())


@pytest.mark.parametrize("api_type", [None, "chat_completions"])
def test_models_without_responses_keep_chat_completions(api_type):
    async def scenario():
        metadata = {} if api_type is None else {"api_type": api_type}
        provider = ProviderDetails(baseUrl="https://provider.example/v1", apikey="key", models={"upstream": metadata})
        response = {"choices": [{"message": {"role": "assistant", "content": "OK"}, "finish_reason": "stop"}]}
        make_request = AsyncMock(return_value=(response, None))
        with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request):
            result, error, _ = await chat_api._attempt_model_fallback_rule(
                _request(),
                object(),
                {"p": provider},
                "gateway",
                {"model": "gateway", "messages": [{"role": "user", "content": "hi"}]},
                {"provider": "p", "model": "upstream"},
                False,
                proxy_http_clients={},
                upstream_routing_state=UpstreamRoutingState(),
            )
        assert error is None and result["choices"][0]["message"]["content"] == "OK"
        assert make_request.await_args.args[1] == "https://provider.example/v1/chat/completions"
        assert "messages" in make_request.await_args.args[3]
        assert "input" not in make_request.await_args.args[3]

    run_async(scenario())


def test_responses_adapter_yields_small_delta_before_reading_completion():
    async def scenario():
        terminal_read = False

        async def source():
            nonlocal terminal_read
            yield _event("response.output_text.delta", {"type": "response.output_text.delta", "delta": "hi"})
            terminal_read = True
            yield _event(
                "response.completed", {"type": "response.completed", "response": {"status": "completed", "output": []}}
            )

        response = responses_stream_to_chat(StreamingResponse(source()), "model")
        first = await anext(response.body_iterator)
        assert b'"content": "hi"' in first
        assert not terminal_read
        rest = await _body(response)
        assert terminal_read and b"data: [DONE]" in rest

    run_async(scenario())
