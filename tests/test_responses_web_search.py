import pytest
from fastapi import HTTPException
from starlette.requests import Request

from llm_gateway_core.api.v1.chat_accounting import install_chat_terminal_handoff
from llm_gateway_core.api.v1.chat_dialects import _translate_responses_request
from llm_gateway_core.api.v1.responses_web_search import (
    complete_responses_web_search,
    parse_web_search_query,
    publish_responses_chat_observation,
    responses_chat_to_sse,
    ResponsesWebSearchBinding,
    external_web_access_allowed,
    responses_web_search_binding,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async


def _request() -> Request:
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/v1/responses",
        "raw_path": b"/v1/responses",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 1234),
        "server": ("test", 80),
        "state": {},
    }
    return Request(scope)


def _chat_response(content: str, usage: dict) -> dict:
    return {
        "id": "chatcmpl-answer",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": usage,
    }


def _tool_response(calls: list[dict], usage: dict) -> dict:
    return {
        "id": "chatcmpl-tools",
        "created": 1,
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": None, "tool_calls": calls},
                "finish_reason": "tool_calls",
            }
        ],
        "usage": usage,
    }


def _web_call(arguments: str, call_id: str = "call_search") -> dict:
    return {
        "id": call_id,
        "type": "function",
        "function": {"name": "web_search", "arguments": arguments},
    }


@pytest.mark.parametrize(
    ("arguments", "expected"),
    [
        ({"query": " cats "}, "cats"),
        ('{"query":"cats"}', "cats"),
        ({"q": "cats"}, "cats"),
        ({"action": {"query": "cats"}}, "cats"),
        ({"action": {"queries": ["", " cats ", "dogs"]}}, "cats"),
        ("cats", "cats"),
        ({"query": "  "}, None),
        ("", None),
        ({}, None),
    ],
)
def test_parse_web_search_query_shapes(arguments, expected):
    assert parse_web_search_query(arguments) == expected


@pytest.mark.parametrize(
    ("size", "expected"),
    [
        (None, 5),
        ("low", 3),
        ("medium", 5),
        ("HIGH", 8),
        ("weird", 5),
    ],
)
def test_search_context_size_maps_to_max_results(size, expected):
    tool = {"type": "web_search"}
    if size is not None:
        tool["search_context_size"] = size
    assert responses_web_search_binding(tool, "web_search").max_results == expected


def test_codex_web_search_constraints_become_gateway_search():
    payload, _model, bindings = _translate_responses_request(
        {
            "model": "llmgateway/high",
            "input": "hi",
            "tools": [
                {
                    "type": "web_search",
                    "external_web_access": True,
                    "search_context_size": "low",
                    "filters": {"allowed_domains": ["https://docs.python.org/3/", "Example.COM"]},
                    "user_location": {"city": "Moscow", "country": "RU"},
                }
            ],
        }
    )

    assert len(bindings) == 1
    assert bindings[0].function_name == "web_search"
    assert bindings[0].max_results == 3
    assert bindings[0].include_domains == ("docs.python.org", "example.com")
    assert bindings[0].external_web_access is True
    description = payload["tools"][0]["function"]["description"]
    assert "Moscow" in description
    assert "RU" in description


def test_hosted_web_search_is_renamed_when_the_client_already_has_that_function():
    _payload, _model, bindings = _translate_responses_request(
        {
            "model": "m",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "web_search", "parameters": {"type": "object"}},
                {"type": "web_search", "external_web_access": True},
            ],
        }
    )

    assert [binding.function_name for binding in bindings] == ["tool_1__web_search"]


def test_external_web_access_false_drops_the_tool_and_does_not_search():
    payload, _model, bindings = _translate_responses_request(
        {
            "model": "llmgateway/high",
            "input": "look this up",
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                {
                    "type": "web_search",
                    "external_web_access": False,
                    "filters": {"allowed_domains": ["docs.python.org"]},
                },
            ],
        }
    )

    assert bindings == []
    assert [tool["function"]["name"] for tool in payload["tools"]] == ["exec_command"]
    assert external_web_access_allowed({"external_web_access": False}) is False
    assert external_web_access_allowed({"type": "web_search"}) is True
    assert external_web_access_allowed({"external_web_access": True}) is True


def test_false_flag_blocks_search_even_if_the_model_asks():
    request = _request()
    searches = []
    tool_messages = []

    async def dispatch(request, payload, accounting_handoff=None):
        request.state.llmgateway_provider = "openai"
        request.state.llmgateway_provider_model = "gpt-test"
        if payload["messages"][-1]["role"] == "tool":
            tool_messages.append(payload["messages"][-1]["content"])
            return _chat_response(
                "no live results",
                {"prompt_tokens": 2, "completion_tokens": 2, "total_tokens": 4},
            )
        return _tool_response(
            [_web_call('{"query":"codex docs"}')],
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def search(request, *, query, max_results, include_domains):
        searches.append(query)
        return "should not run"

    binding = ResponsesWebSearchBinding(
        function_name="web_search",
        max_results=5,
        include_domains=("docs.python.org",),
        external_web_access=False,
    )
    result = run_async(
        complete_responses_web_search(
            request,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}]},
            [binding],
            dispatch=dispatch,
            search=search,
        )
    )

    assert searches == []
    assert tool_messages == ["web_search error: external web access is disabled."]
    assert result["choices"][0]["message"]["content"] == "no live results"


def test_gateway_search_runs_when_external_web_access_is_true():
    request = _request()
    payloads = []
    searches = []

    async def dispatch(request, payload, accounting_handoff=None):
        assert accounting_handoff is None
        assert payload["stream"] is False
        payloads.append(payload)
        request.state.llmgateway_provider = "openai"
        request.state.llmgateway_provider_model = "gpt-test"
        request.state.llmgateway_target_path = "/v1/chat/completions"
        if len(payloads) == 1:
            return _tool_response(
                [_web_call('{"query":"codex docs"}')],
                {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3},
            )
        return _chat_response(
            "found it",
            {"prompt_tokens": 5, "completion_tokens": 4, "total_tokens": 9},
        )

    async def search(request, *, query, max_results, include_domains):
        request.state.llmgateway_provider = "llmgateway"
        request.state.llmgateway_provider_model = "proxy"
        request.state.llmgateway_target_path = "/v1/web/search"
        searches.append((query, max_results, include_domains))
        return "[1] Docs\nhttps://docs.python.org\nsnippet"

    payload, _model, bindings = _translate_responses_request(
        {
            "model": "llmgateway/high",
            "input": "look this up",
            "stream": True,
            "tools": [
                {
                    "type": "web_search",
                    "external_web_access": True,
                    "search_context_size": "low",
                    "filters": {"allowed_domains": ["docs.python.org"]},
                }
            ],
        }
    )
    result = run_async(
        complete_responses_web_search(
            request,
            payload,
            bindings,
            dispatch=dispatch,
            search=search,
        )
    )

    assert searches == [("codex docs", 3, ["docs.python.org"])]
    assert result["choices"][0]["message"]["content"] == "found it"
    assert result["usage"] == {"prompt_tokens": 7, "completion_tokens": 5, "total_tokens": 12}
    assert payloads[1]["messages"][-1]["role"] == "tool"
    assert "https://docs.python.org" in payloads[1]["messages"][-1]["content"]
    assert request.state.llmgateway_provider == "openai"
    assert request.state.llmgateway_provider_model == "gpt-test"
    assert request.state.llmgateway_target_path == "/v1/chat/completions"
    assert payload["stream"] is True


def test_mixed_client_tool_call_is_not_searched():
    request = _request()
    searches = []

    async def dispatch(request, payload, accounting_handoff=None):
        request.state.llmgateway_provider = "openai"
        request.state.llmgateway_provider_model = "gpt-test"
        return _tool_response(
            [
                _web_call('{"query":"cats"}', "call_search"),
                {
                    "id": "call_exec",
                    "type": "function",
                    "function": {"name": "exec_command", "arguments": "{}"},
                },
            ],
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def search(request, *, query, max_results, include_domains):
        searches.append(query)
        return "nope"

    payload, _model, bindings = _translate_responses_request(
        {
            "model": "m",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                {"type": "web_search"},
            ],
        }
    )
    result = run_async(
        complete_responses_web_search(request, payload, bindings, dispatch=dispatch, search=search)
    )

    assert searches == []
    names = [call["function"]["name"] for call in result["choices"][0]["message"]["tool_calls"]]
    assert names == ["web_search", "exec_command"]


def test_search_stops_after_three_model_calls():
    request = _request()
    dispatch_count = 0
    search_count = 0

    async def dispatch(request, payload, accounting_handoff=None):
        nonlocal dispatch_count
        dispatch_count += 1
        request.state.llmgateway_provider = "openai"
        request.state.llmgateway_provider_model = "gpt-test"
        return _tool_response(
            [_web_call('{"query":"again"}', f"call_{dispatch_count}")],
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        )

    async def search(request, *, query, max_results, include_domains):
        nonlocal search_count
        search_count += 1
        return "result"

    binding = responses_web_search_binding({"type": "web_search"}, "web_search")
    run_async(
        complete_responses_web_search(
            request,
            {"model": "m", "messages": [{"role": "user", "content": "hi"}], "stream": True},
            [binding],
            dispatch=dispatch,
            search=search,
        )
    )

    assert dispatch_count == 3
    assert search_count == 2


def test_publish_bills_the_recorded_chat_provider():
    request = _request()
    request.state.llmgateway_provider = "openai"
    request.state.llmgateway_provider_model = "gpt-test"
    request.state.runtime_snapshot = type(
        "Snapshot",
        (),
        {"cost_rate_registry": {("openai", "gpt-test"): ModelCostRates(1.0, 2.0)}},
    )()
    handoff = install_chat_terminal_handoff(request)
    publish_responses_chat_observation(
        request,
        _chat_response("ok", {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10}),
    )
    observation = handoff.take()
    assert observation.top_provider == "openai"
    assert observation.top_model == "gpt-test"
    assert observation.usage.prompt_tokens == 4
    assert observation.usage.completion_tokens == 6


def test_responses_sse_includes_text_completed_and_done():
    response = responses_chat_to_sse(
        _chat_response("hello", {"prompt_tokens": 3, "completion_tokens": 2, "total_tokens": 5}),
        "llmgateway/high",
    )

    async def collect():
        parts = []
        async for chunk in response.body_iterator:
            parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        return "".join(parts)

    text = run_async(collect())
    assert response.media_type == "text/event-stream"
    assert '"type":"response.output_text.delta"' in text
    assert '"delta":"hello"' in text
    assert '"type":"response.completed"' in text
    assert '"input_tokens":3' in text
    assert "data: [DONE]" in text


def test_responses_sse_includes_client_function_calls():
    response = responses_chat_to_sse(
        _tool_response(
            [
                {
                    "id": "call_exec",
                    "type": "function",
                    "function": {"name": "exec_command", "arguments": '{"cmd":"ls"}'},
                }
            ],
            {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        ),
        "llmgateway/high",
    )

    async def collect():
        parts = []
        async for chunk in response.body_iterator:
            parts.append(chunk.decode("utf-8") if isinstance(chunk, bytes) else str(chunk))
        return "".join(parts)

    text = run_async(collect())
    assert '"type":"response.output_item.added"' in text
    assert '"type":"function_call"' in text
    assert '"name":"exec_command"' in text
    assert '"type":"response.function_call_arguments.delta"' in text
    assert '"type":"response.completed"' in text
    assert "data: [DONE]" in text


def test_bad_domain_filter_is_rejected():
    with pytest.raises(HTTPException) as exc:
        _translate_responses_request(
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "web_search", "filters": {"allowed_domains": [1]}}],
            }
        )
    assert exc.value.status_code == 400
