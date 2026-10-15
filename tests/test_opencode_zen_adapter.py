import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from fastapi.responses import StreamingResponse

from tests._async_compat import run_async

from llm_gateway_core.services.opencode_zen import make_opencode_zen_request


def _event(chunk):
    return ("data: " + json.dumps(chunk) + "\n\n").encode()


def _request():
    return SimpleNamespace(
        state=SimpleNamespace(), headers={"x-session-id": "test-session"}
    )


def _chunks(include_genuine=True):
    calls = [
        {"index": 0, "id": "read-id", "type": "function",
         "function": {"name": "re", "arguments": '{"file'}},
    ]
    if include_genuine:
        calls.append({"index": 1, "id": "bash-id", "type": "function",
                      "function": {"name": "ba", "arguments": '{"command":"ec'}})
    yield _event({"id": "zen", "model": "big-pickle", "choices": [
        {"index": 0, "delta": {"content": "answer", "reasoning": "think",
                                  "tool_calls": calls}}
    ]})
    calls = [{"index": 0, "function": {"name": "ad", "arguments": '_path":"x"}'}}]
    if include_genuine:
        calls.append({"index": 1, "function": {"name": "sh", "arguments": 'ho"}'}})
    yield _event({"id": "zen", "choices": [{"index": 0, "delta": {
        "tool_calls": calls}, "finish_reason": "tool_calls"}]})
    yield _event({"id": "zen", "usage": {"prompt_tokens": 3, "completion_tokens": 4},
                  "choices": [{"index": 0, "delta": {"tool_calls": None}}]})
    yield b"data: [DONE]\n\n"


async def _async_chunks(include_genuine=True, closed=None):
    try:
        for chunk in _chunks(include_genuine):
            for offset in range(0, len(chunk), 7):
                yield chunk[offset:offset + 7]
    finally:
        if closed is not None:
            closed.append(True)


@pytest.mark.parametrize("is_streaming", [False, True])
def test_filters_injected_tool_calls_and_preserves_response(is_streaming):
    source = _async_chunks()
    make_request = AsyncMock(
        return_value=(StreamingResponse(source), None)
    )
    payload = {
        "model": "big-pickle", "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"type": "function", "function": {"name": "bash"}},
                  {"type": "function", "function": {"name": "weather"}}],
        "stream": is_streaming,
    }
    original = json.loads(json.dumps(payload))
    response, error = run_async(make_opencode_zen_request(
        make_request, _request(), object(), "https://zen.test", {
            "HTTP-Referer": "old", "x-opencode-request": "old", "Authorization": "x"
        }, payload, is_streaming, stream_event_max_bytes=16384,
    ))

    assert error is None
    assert payload == original
    sent = make_request.await_args.args[2:4]
    headers, sent_payload = sent
    assert headers["User-Agent"] == "opencode/1.19.0"
    assert "HTTP-Referer" not in headers and "x-opencode-request" not in headers
    assert sent_payload["stream"] is True
    assert {tool["function"]["name"] for tool in sent_payload["tools"]} == {
        "bash", "weather", "read"
    }
    if is_streaming:
        result = run_async(_collect(response.body_iterator))
        assert b'"name": "read"' not in result
        assert b'"read-id"' not in result
        assert b'"name": "bash"' in result
        assert b'"arguments": "{\\"command\\":\\"echo\\"}"' in result
        assert b'"content": "answer"' in result and b'"reasoning": "think"' in result
        assert b'"prompt_tokens": 3' in result
    else:
        assert response["usage"] == {"prompt_tokens": 3, "completion_tokens": 4}
        choice = response["choices"][0]
        assert choice["finish_reason"] == "tool_calls"
        assert choice["message"]["content"] == "answer"
        assert choice["message"]["reasoning"] == "think"
        assert choice["message"]["tool_calls"][0]["function"]["name"] == "bash"
        assert choice["message"]["tool_calls"][0]["function"]["arguments"] == (
            '{"command":"echo"}'
        )


def test_only_injected_calls_become_stop_without_tool_calls():
    make_request = AsyncMock(return_value=(
        StreamingResponse(_async_chunks(include_genuine=False)), None
    ))
    response, error = run_async(make_opencode_zen_request(
        make_request, _request(), object(), "https://zen.test", {},
        {"messages": []}, False, stream_event_max_bytes=16384,
    ))
    assert error is None
    assert response["choices"][0]["finish_reason"] == "stop"
    assert "tool_calls" not in response["choices"][0]["message"]


def test_error_passthrough_and_session_identity_are_stable():
    error_response = object()
    make_request = AsyncMock(return_value=(error_response, "upstream error"))
    request = _request()
    headers = {"x-session-id": "caller", "X-Session-Id": "caller-2",
               "HTTP-Referer": "old", "x-opencode-request": "old"}
    first = run_async(make_opencode_zen_request(
        make_request, request, object(), "url", headers, {"messages": []}, False,
        stream_event_max_bytes=16384,
    ))
    assert first == (error_response, "upstream error")
    captured = make_request.await_args.args[2]
    assert "HTTP-Referer" not in captured and "x-opencode-request" not in captured
    assert captured["X-Session-Id"] == captured["x-opencode-session-id"]
    session = captured["X-Session-Id"]
    make_request.reset_mock()
    second = run_async(make_opencode_zen_request(
        make_request, request, object(), "url", headers, {"messages": []}, False,
        stream_event_max_bytes=16384,
    ))
    assert second == (error_response, "upstream error")
    assert make_request.await_args.args[2]["X-Session-Id"] == session


async def _collect(iterator):
    return b"".join([chunk async for chunk in iterator])


def test_closing_filtered_stream_closes_upstream():
    closed = []

    async def run():
        make_request = AsyncMock(return_value=(
            StreamingResponse(_async_chunks(closed=closed)), None
        ))
        response, error = await make_opencode_zen_request(
            make_request, _request(), object(), "url", {}, {"messages": []}, True,
            stream_event_max_bytes=16384,
        )
        assert error is None
        await anext(response.body_iterator)
        await response.body_iterator.aclose()

    run_async(run())
    assert closed == [True]
