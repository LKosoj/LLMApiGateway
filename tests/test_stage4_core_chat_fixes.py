"""Focused tests for Stage 4 (Core Chat, Routing, Dispatch, Streaming, Dialects)."""
from __future__ import annotations

import json
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse
from starlette.requests import Request

from llm_gateway_core.api.v1.chat_accounting import (
    ChatStreamDialect,
    ChatTerminalHandoff,
    ChatTerminalOwner,
    install_chat_terminal_handoff,
)
from llm_gateway_core.api.v1.chat_dialects import (
    _anthropic_response_to_openai,
    _openai_request_to_anthropic_payload,
    _openai_response_to_responses,
    _translate_responses_request,
)
from llm_gateway_core.api.v1.chat_dispatch import attempt_model_fallback_rule
from llm_gateway_core.api.v1.chat_streaming import (
    _DirectChatStreamObservationBuilder,
    _anthropic_stream_to_openai,
    _openai_stream_to_anthropic,
    _openai_stream_to_responses,
    _sanitize_anthropic_stream_think_tags,
    _sanitize_openai_json_object_stream,
    _sanitize_openai_stream_think_tags,
    _sanitize_openai_stream_tool_call_rescue,
)
from llm_gateway_core.api.v1.responses_upstream import responses_stream_to_chat
from llm_gateway_core.api.v1.responses_web_search import (
    ResponsesWebSearchBinding,
    complete_responses_web_search,
)
from llm_gateway_core.middleware.accounting_admission import AccountingRequestContext
from llm_gateway_core.middleware.response_observation import (
    ResponseStart,
    TerminalReason,
    TerminalSignal,
    WireMode,
)
from llm_gateway_core.services import upstream_routing_state as urs_mod
from llm_gateway_core.services.accounting import (
    AccountingReceipt,
    AccountingReservation,
    AccountingUsage,
    BillingComponent,
    CostSource,
    ProjectionStatus,
    SourceStatus,
    classify_billing_policy,
)
from llm_gateway_core.services.chat_accounting import ChatTerminalObservation
from llm_gateway_core.services.provider_auth import resolve_provider_auth_headers
from llm_gateway_core.services.request_handler import (
    _extract_stream_error_detail,
    _make_json_request,
    parse_retry_after_header,
)
from llm_gateway_core.services.router_model import RouterModelService
from llm_gateway_core.services.stream_observation import SSEEvent
from llm_gateway_core.services.tool_call_rescue import (
    could_become_dialect_marker,
    rescue_inline_tool_calls,
)
from llm_gateway_core.services.upstream_routing_state import (
    UpstreamQuotaLimits,
    UpstreamRoutingState,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async


def _make_request(path: str = "/v1/chat/completions") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": path,
        "headers": [],
        "query_string": b"",
        "state": {},
    }
    return Request(scope)


async def _collect_stream(response: StreamingResponse) -> bytes:
    chunks: list[bytes] = []
    async for chunk in response.body_iterator:
        chunks.append(chunk if isinstance(chunk, bytes) else chunk.encode("utf-8"))
    return b"".join(chunks)


def _stream_from_chunks(chunks: list[bytes]) -> StreamingResponse:
    async def gen():
        for chunk in chunks:
            yield chunk

    return StreamingResponse(gen(), media_type="text/event-stream")


# --- 1. request_handler.py ---


def test_parse_retry_after_rejects_nan_and_inf() -> None:
    assert parse_retry_after_header("nan") is None
    assert parse_retry_after_header("NaN") is None
    assert parse_retry_after_header("inf") is None
    assert parse_retry_after_header("-inf") is None
    assert parse_retry_after_header("12.5") == 12.5


def test_extract_stream_error_detail_ignores_null_error_and_detail() -> None:
    assert _extract_stream_error_detail({"error": None, "detail": None, "choices": []}) is None
    assert _extract_stream_error_detail({"error": {"message": "upstream boom"}}) == "upstream boom"
    assert _extract_stream_error_detail({"detail": "bad request"}) == "bad request"


def test_make_json_request_ignores_null_error_and_detail() -> None:
    async def scenario() -> None:
        class FakeClient:
            async def post(self, url, *, headers, json):  # noqa: ARG002
                return httpx.Response(
                    200,
                    json={
                        "id": "chatcmpl-1",
                        "error": None,
                        "detail": None,
                        "choices": [{"index": 0, "message": {"role": "assistant", "content": "ok"}}],
                    },
                )

        body, err = await _make_json_request(FakeClient(), "https://example.test/v1/chat/completions", {}, {})
        assert err is None
        assert body is not None
        assert body["id"] == "chatcmpl-1"

    run_async(scenario())


# --- 2. responses_upstream.py ---


def test_responses_stream_to_chat_handles_malformed_events_safely() -> None:
    async def scenario() -> None:
        resp = _stream_from_chunks(
            [
                b"data: 123\n\n",
                b'data: {"type":"response.created"}\n\n',
                b'data: {"type":"response.output_text.delta"}\n\n',
                b'data: {"type":"response.function_call_arguments.delta","output_index":99,"delta":"{}"}\n\n',
                b'data: {"type":"response.output_text.delta","delta":"hello"}\n\n',
                b'data: {"type":"response.completed","response":{"id":"r1","status":"completed","output":[{"type":"message","content":[{"type":"output_text","text":"hello"}]}]}}\n\n',
            ]
        )
        converted = responses_stream_to_chat(resp, "gpt-test")
        raw = await _collect_stream(converted)
        assert b'"content": "hello"' in raw
        assert b"data: [DONE]\n\n" in raw

    run_async(scenario())


# --- 3. chat_streaming.py ---


def test_stream_sanitizers_process_delta_on_finish_reason_chunk_and_crlf() -> None:
    async def scenario() -> None:
        # 1. think tags sanitizer: finish_reason + content + CRLF + data: without space
        think_chunk = json.dumps(
            {
                "id": "c1",
                "choices": [{"index": 0, "delta": {"content": "<think>secret</think>visible"}, "finish_reason": "stop"}],
            }
        ).encode()
        resp = _stream_from_chunks([b"data:" + think_chunk + b"\r\n\r\ndata:[DONE]\r\n\r\n"])
        out = await _collect_stream(_sanitize_openai_stream_think_tags(resp, "m"))
        assert b"secret" not in out
        assert b"visible" in out

        # 2. tool call rescue sanitizer: finish_reason + dialect in same chunk + CRLF + data: without space
        rescue_chunk = json.dumps(
            {
                "id": "c2",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": '<function=get_weather>{"city":"Paris"}</function>'},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        resp2 = _stream_from_chunks([b"data:" + rescue_chunk + b"\r\n\r\ndata:[DONE]\r\n\r\n"])
        out2 = await _collect_stream(_sanitize_openai_stream_tool_call_rescue(resp2, "m", {"get_weather": {}}))
        assert b'"tool_calls"' in out2
        assert b'"finish_reason":"tool_calls"' in out2
        assert b"<function=" not in out2

        # 3. json object sanitizer: finish_reason + fenced JSON in same chunk + CRLF + data: without space
        json_chunk = json.dumps(
            {
                "id": "c3",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"content": '```json\n{"ok":true}\n```'},
                        "finish_reason": "stop",
                    }
                ],
            }
        ).encode()
        resp3 = _stream_from_chunks([b"data:" + json_chunk + b"\r\n\r\ndata:[DONE]\r\n\r\n"])
        out3 = await _collect_stream(_sanitize_openai_json_object_stream(resp3, "m"))
        assert b"```" not in out3
        assert b'{\\"ok\\":true}' in out3

    run_async(scenario())


def test_stream_converters_handle_crlf_and_no_space_data_and_empty_anthropic_usage() -> None:
    async def scenario() -> None:
        # Empty stream -> _openai_stream_to_anthropic emits Anthropic-shaped usage
        empty_resp = _stream_from_chunks([b"data:[DONE]\r\n\r\n"])
        out_empty = await _collect_stream(_openai_stream_to_anthropic(_make_request(), empty_resp, "claude-test"))
        assert b'"input_tokens":0,"output_tokens":0' in out_empty
        assert b'"prompt_tokens"' not in out_empty

        # _openai_stream_to_responses with data: without space and CRLF
        chunk = json.dumps({"id": "c1", "choices": [{"index": 0, "delta": {"content": "hi"}}]}).encode()
        out_resp = await _collect_stream(
            _openai_stream_to_responses(_stream_from_chunks([b"data:" + chunk + b"\r\n\r\ndata:[DONE]\r\n\r\n"]), "m")
        )
        assert b'"delta":"hi"' in out_resp

        # _anthropic_stream_to_openai with CRLF and data: without space
        anth_stream = (
            b'event: content_block_delta\r\n'
            b'data:{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"yo"}}\r\n\r\n'
        )
        out_oai = await _collect_stream(_anthropic_stream_to_openai(_make_request(), _stream_from_chunks([anth_stream]), "m"))
        assert b'"content":"yo"' in out_oai

        # _sanitize_anthropic_stream_think_tags with CRLF
        anth_think = (
            b'event: content_block_delta\r\n'
            b'data:{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"<think>x</think>ok"}}\r\n\r\n'
            b'event: content_block_stop\r\n'
            b'data:{"type":"content_block_stop","index":0}\r\n\r\n'
        )
        out_anth_clean = await _collect_stream(_sanitize_anthropic_stream_think_tags(_stream_from_chunks([anth_think]), "m"))
        assert b"<think>" not in out_anth_clean
        assert b"ok" in out_anth_clean

    run_async(scenario())


def test_stream_observation_builder_anthropic_partial_and_responses_zero_usage() -> None:
    rates = MappingProxyType({("p", "m"): ModelCostRates(input_rate=1.0, output_rate=2.0)})

    # Anthropic partial estimates output_tokens from text/thinking/input_json deltas
    anth_builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.ANTHROPIC,
        provider="p",
        model="m",
        cost_rate_registry=rates,
        estimated_prompt_tokens=10,
    )
    anth_builder.observe(
        SSEEvent(
            event="message_start",
            data=json.dumps({"type": "message_start", "message": {"usage": {"input_tokens": 12, "output_tokens": 0}}}),
        )
    )
    anth_builder.observe(
        SSEEvent(
            event="content_block_delta",
            data=json.dumps({"type": "content_block_delta", "delta": {"type": "text_delta", "text": "Hello world "}}),
        )
    )
    anth_builder.observe(
        SSEEvent(
            event="content_block_delta",
            data=json.dumps({"type": "content_block_delta", "delta": {"type": "thinking_delta", "thinking": "thinking hard "}}),
        )
    )
    anth_builder.observe(
        SSEEvent(
            event="content_block_delta",
            data=json.dumps({"type": "content_block_delta", "delta": {"type": "input_json_delta", "partial_json": '{"a":1}'}}),
        )
    )
    partial = anth_builder.build_partial()
    assert partial is not None
    assert partial.usage.prompt_tokens == 12
    assert partial.usage.completion_tokens > 0
    assert partial.usage.is_estimated is True

    # Responses observe() with zero usage falls back to _build_estimated()
    resp_builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.RESPONSES,
        provider="p",
        model="m",
        cost_rate_registry=rates,
        estimated_prompt_tokens=20,
    )
    resp_builder.observe(
        SSEEvent(data=json.dumps({"type": "response.output_text.delta", "delta": "Some generated text"}))
    )
    obs = resp_builder.observe(
        SSEEvent(
            data=json.dumps(
                {
                    "type": "response.completed",
                    "response": {
                        "id": "resp_1",
                        "usage": {"input_tokens": 0, "output_tokens": 0, "total_tokens": 0},
                    },
                }
            )
        )
    )
    assert obs is not None
    assert obs.usage.prompt_tokens == 20
    assert obs.usage.completion_tokens > 0
    assert obs.usage.is_estimated is True


# --- 4. chat_dialects.py ---


def test_chat_dialects_fixes() -> None:
    # 1. _openai_response_to_responses preserves reasoning & non-empty content alongside tool_calls
    oai_resp = {
        "id": "resp-1",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "reasoning_content": "let me check",
                    "content": "Calling tool now",
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
                    ],
                },
            }
        ],
    }
    converted = _openai_response_to_responses(oai_resp, "m")
    assert [item["type"] for item in converted["output"]] == ["message", "message", "function_call"]

    # When content is None/"" and no reasoning, output is just [function_call]
    oai_resp_only_tool = {
        "id": "resp-2",
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {"id": "c1", "type": "function", "function": {"name": "search", "arguments": "{}"}}
                    ],
                },
            }
        ],
    }
    converted2 = _openai_response_to_responses(oai_resp_only_tool, "m")
    assert [item["type"] for item in converted2["output"]] == ["function_call"]

    # 2. _translate_responses_request normalizes flat tool_choice and flat text.format json_schema
    translated, _, _ = _translate_responses_request(
        {
            "model": "m",
            "input": "hi",
            "tool_choice": {"type": "function", "name": "lookup"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "Result",
                    "schema": {"type": "object"},
                    "strict": True,
                }
            },
        }
    )
    assert translated["tool_choice"] == {"type": "function", "function": {"name": "lookup"}}
    assert translated["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Result", "schema": {"type": "object"}, "strict": True},
    }

    # 3. _openai_request_to_anthropic_payload merges consecutive user and tool+user messages
    anth_payload = _openai_request_to_anthropic_payload(
        {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "first"},
                {"role": "user", "content": "second"},
                {"role": "assistant", "content": None, "tool_calls": [{"id": "t1", "type": "function", "function": {"name": "f", "arguments": "{}"}}]},
                {"role": "tool", "tool_call_id": "t1", "content": "tool out"},
                {"role": "user", "content": "follow up"},
            ],
        }
    )
    assert len(anth_payload["messages"]) == 3
    assert anth_payload["messages"][0]["role"] == "user"
    assert len(anth_payload["messages"][0]["content"]) == 2
    assert anth_payload["messages"][2]["role"] == "user"
    assert [b["type"] for b in anth_payload["messages"][2]["content"]] == ["tool_result", "text"]

    # 4. _anthropic_response_to_openai collects thinking blocks into reasoning_content
    oai_from_anth = _anthropic_response_to_openai(
        {
            "id": "msg_1",
            "content": [
                {"type": "thinking", "thinking": "step 1. "},
                {"type": "thinking", "thinking": "step 2."},
                {"type": "text", "text": "final answer"},
            ],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 5, "output_tokens": 10},
        },
        "claude",
    )
    msg = oai_from_anth["choices"][0]["message"]
    assert msg["reasoning_content"] == "step 1. step 2."
    assert msg["content"] == "final answer"


# --- 5. upstream_routing_state.py ---


def test_session_affinity_pruning_and_atomic_model_limits(monkeypatch: pytest.MonkeyPatch) -> None:
    now = [100.0]
    state = UpstreamRoutingState(monotonic_func=lambda: now[0])
    monkeypatch.setattr(urs_mod, "MAX_SESSION_AFFINITY_ENTRIES", 3)

    for i in range(5):
        state.select_key("prov", "m", ["k1", "k2"], session_id=f"s{i}", session_affinity_ttl_seconds=10.0)
    assert len(state._session_affinity) == 3

    # Expire all entries and insert one more
    now[0] = 200.0
    state.select_key("prov", "m", ["k1", "k2"], session_id="s_new", session_affinity_ttl_seconds=10.0)
    assert len(state._session_affinity) == 1

    # Atomic model limits check in record_attempt_start
    limits = UpstreamQuotaLimits(rpm=1)
    assert state.record_attempt_start("prov", "m", "fp1", limits=limits) is None
    assert state.record_attempt_start("prov", "m", "fp1", limits=limits) == "rpm quota exhausted"


# --- 6. tool_call_rescue.py ---


def test_tool_call_rescue_leading_whitespace() -> None:
    assert could_become_dialect_marker("  \n<fun") is True
    assert could_become_dialect_marker("  hello") is False

    kimi = (
        "<|tool_calls_section_begin|>"
        "<|tool_call_begin|>functions.lookup:0<|tool_call_argument_begin|>\n  {\"q\": \"hi\"}"
        "<|tool_call_end|><|tool_calls_section_end|>"
    )
    res_kimi = rescue_inline_tool_calls(kimi, {})
    assert not res_kimi.failed
    assert len(res_kimi.tool_calls) == 1
    assert res_kimi.tool_calls[0].name == "lookup"

    fn_tag = '<function=lookup>\n  {"q": "hi"}\n</function>'
    res_fn = rescue_inline_tool_calls(fn_tag, {})
    assert not res_fn.failed
    assert len(res_fn.tool_calls) == 1
    assert res_fn.tool_calls[0].name == "lookup"


# --- 7. chat_dispatch.py ---


def test_subprovider_branch_respects_budget_on_tool_choice_downgrade() -> None:
    async def scenario() -> None:
        request = _make_request()
        providers_config = {
            "openrouter": SimpleNamespace(
                baseUrl="https://openrouter.test/api/v1",
                apikey="sk-test",
                type="openai",
                models={},
            )
        }
        calls = 0

        async def fake_make_llm_request(*args, **kwargs):  # noqa: ARG001
            nonlocal calls
            calls += 1
            return None, "tool_choice is not supported by this model"

        async def fake_auth(*args, **kwargs):  # noqa: ARG001
            return SimpleNamespace(headers={"Authorization": "Bearer sk-test"}, upstream_key_fingerprint="fp1")

        resp, err, _next_attempt = await attempt_model_fallback_rule(
            request,
            object(),
            providers_config,
            "gw-model",
            {
                "model": "gw-model",
                "messages": [{"role": "user", "content": "hi"}],
                "tools": [{"type": "function", "function": {"name": "f", "parameters": {}}}],
                "tool_choice": "required",
            },
            {
                "provider": "openrouter",
                "model": "m1",
                "providers_order": ["sub1", "sub2"],
                "use_provider_order_as_fallback": True,
            },
            False,
            proxy_http_clients={},
            upstream_routing_state=UpstreamRoutingState(),
            total_attempts_budget=1,
            make_llm_request_hook=fake_make_llm_request,
            asyncio_hook=SimpleNamespace(sleep=AsyncMock()),
            logging_hook=MagicMock(),
            classify_error_hook=lambda _e: "client_error",
            resolve_provider_auth_material_hook=fake_auth,
        )
        assert resp is None
        assert err is not None
        # Budget was 1: must NOT make a 2nd attempt even after forced tool_choice downgrade!
        assert calls == 1

    run_async(scenario())


# --- 8. chat_accounting.py, responses_web_search.py, router_model.py ---


def test_chat_terminal_owner_commits_partial_failure_but_releases_normal_publish() -> None:
    async def scenario() -> None:
        request = _make_request()
        request.state.llmgateway_gateway_model = "gw-model"
        accounting_service = MagicMock()

        async def _commit(_reservation, event):
            return AccountingReceipt(
                source_status=SourceStatus.ACCEPTED,
                projection_status=ProjectionStatus.APPLIED,
                event_id=event.event_id,
                billing_fingerprint=event.billing_fingerprint,
                usage_row_id=1,
            )

        accounting_service.commit = AsyncMock(side_effect=_commit)
        accounting_service.release = AsyncMock(return_value=True)
        policy = classify_billing_policy("POST", "/v1/chat/completions")
        assert policy is not None
        context = AccountingRequestContext(
            request_id="req-1",
            method="POST",
            route_template="/v1/chat/completions",
            policy=policy,
            reservation=AccountingReservation(
                reservation_id="res-1",
                api_key_id=1,
                reserved_usd=0.1,
                request_id="req-1",
            ),
        )
        owner = ChatTerminalOwner(
            accounting_service=accounting_service,
            context=context,
            cost_rate_registry=MappingProxyType({}),
            request=request,
        )
        handoff = ChatTerminalHandoff()
        owner._attach_handoff(handoff)

        comp = BillingComponent(
            provider="p1",
            model="m1",
            usage=AccountingUsage(prompt_tokens=5, completion_tokens=5, total_tokens=10, cost=0.01),
            cost_source=CostSource.UPSTREAM,
        )
        handoff.publish_partial_failure(
            ChatTerminalObservation(top_provider="p1", top_model="m1", components=(comp,))
        )
        res = await owner._finalize(
            TerminalSignal(
                TerminalReason.UPSTREAM_ERROR,
                ResponseStart(502, (), WireMode.JSON, None),
            )
        )
        assert isinstance(res, AccountingReceipt)
        accounting_service.commit.assert_awaited_once()
        accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_responses_web_search_publishes_partial_failure_on_second_round_error() -> None:
    async def scenario() -> None:
        request = _make_request("/v1/responses")
        outer_handoff = install_chat_terminal_handoff(request)
        round_num = 0
        comp1 = BillingComponent(
            provider="p1",
            model="m1",
            usage=AccountingUsage(prompt_tokens=10, completion_tokens=5, total_tokens=15, cost=0.02),
            cost_source=CostSource.UPSTREAM,
        )

        async def fake_dispatch(_req, _payload, *, accounting_handoff):
            nonlocal round_num
            round_num += 1
            if round_num == 1:
                accounting_handoff.publish(
                    ChatTerminalObservation(top_provider="p1", top_model="m1", components=(comp1,))
                )
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": None,
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "web_search", "arguments": '{"query":"test"}'},
                                    }
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
                }
            raise HTTPException(status_code=503, detail="round 2 failed")

        async def fake_search(_req, **_kwargs):
            return "search result"

        with pytest.raises(HTTPException):
            await complete_responses_web_search(
                request,
                {"model": "gw", "messages": [{"role": "user", "content": "search"}]},
                [ResponsesWebSearchBinding("web_search", 3, (), True)],
                dispatch=fake_dispatch,
                search=fake_search,
            )

        partial = outer_handoff.take_partial_failure()
        assert partial is not None
        assert partial.components == (comp1,)

    run_async(scenario())


def test_router_model_publishes_partial_failure_when_delegate_fails() -> None:
    async def scenario() -> None:
        request = _make_request()
        outer_handoff = install_chat_terminal_handoff(request)
        selector_comp = BillingComponent(
            provider="dec-prov",
            model="dec-model",
            usage=AccountingUsage(prompt_tokens=10, completion_tokens=2, total_tokens=12, cost=0.005),
            cost_source=CostSource.UPSTREAM,
        )
        service = RouterModelService(
            SimpleNamespace(fallback_rules={}, providers_config={}),
            cost_rate_registry=MappingProxyType({}),
        )

        async def fake_select(**_kwargs):
            return (
                {"id": "gateway:target", "type": "gateway_model", "gateway_model": "target"},
                {"candidate_id": "gateway:target"},
                "decision_model",
                (selector_comp,),
                {"prompt_tokens": 10, "completion_tokens": 2},
            )

        async def failing_dispatch(**_kwargs):
            raise HTTPException(status_code=503, detail="delegate failed")

        service._select = fake_select  # type: ignore[method-assign]
        service._dispatch_selected_target = failing_dispatch  # type: ignore[method-assign]

        with pytest.raises(HTTPException):
            await service.run_observed(
                request=request,
                gateway_model_name="router-m",
                router_config={"selector_model": "dec", "targets": []},
                request_body={"model": "router-m", "messages": [{"role": "user", "content": "hi"}]},
            )
        partial = outer_handoff.take_partial_failure()
        assert partial is not None
        assert partial.components == (selector_comp,)

        # Also test run_stream
        stream_handoff = ChatTerminalHandoff()
        with pytest.raises(HTTPException):
            await service.run_stream(
                request=request,
                gateway_model_name="router-m",
                router_config={"selector_model": "dec", "targets": []},
                request_body={"model": "router-m", "stream": True, "messages": [{"role": "user", "content": "hi"}]},
                accounting_handoff=stream_handoff,
            )
        stream_partial = stream_handoff.take_partial_failure()
        assert stream_partial is not None
        assert stream_partial.components == (selector_comp,)

    run_async(scenario())


# --- 9. provider_auth.py ---


def test_provider_auth_narrows_state_exception() -> None:
    async def scenario() -> None:
        provider_cfg = SimpleNamespace(
            apikey="sk-123",
            type="openai",
            auth_header=None,
            auth_prefix=None,
        )

        # Missing/read-only state raises AttributeError -> handled cleanly
        headers = await resolve_provider_auth_headers(
            SimpleNamespace(state=None),  # type: ignore[arg-type]
            provider_name="p",
            provider_config=provider_cfg,
        )
        assert "Authorization" in headers

        # Unexpected RuntimeError in state setter must propagate (not swallowed)
        class BrokenState:
            def __setattr__(self, name, value):
                raise RuntimeError("unexpected")

        with pytest.raises(RuntimeError, match="unexpected"):
            await resolve_provider_auth_headers(
                SimpleNamespace(state=BrokenState()),  # type: ignore[arg-type]
                provider_name="p",
                provider_config=provider_cfg,
            )

    run_async(scenario())
