import json
import unittest

from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat_streaming import _sanitize_openai_stream_tool_call_rescue
from tests._async_compat import run_async


WEATHER_SCHEMA_MAP = {
    "get_weather": {
        "type": "object",
        "properties": {"location": {"type": "string"}},
    }
}


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


async def _collect(body_iterator) -> list[bytes]:
    return [chunk async for chunk in body_iterator]


def _run_sanitizer(events: list[dict], tool_schema_map: dict = WEATHER_SCHEMA_MAP) -> list[bytes]:
    async def source():
        for event in events:
            if isinstance(event, dict):
                yield _sse(event)
            elif event == "data: [DONE]":
                yield b"data: [DONE]\n\n"
            else:
                yield event

    response = StreamingResponse(source(), media_type="text/event-stream")
    sanitized = _sanitize_openai_stream_tool_call_rescue(response, "gateway-model", tool_schema_map)
    return run_async(_collect(sanitized.body_iterator))


def _decode_events(raw_chunks: list[bytes]) -> list[dict | str]:
    text = b"".join(raw_chunks).decode("utf-8")
    events = []
    for event in text.split("\n\n"):
        event = event.strip()
        if not event:
            continue
        if event == "data: [DONE]":
            events.append("[DONE]")
            continue
        assert event.startswith("data: ")
        events.append(json.loads(event[len("data: "):]))
    return events


def _collect_text_content(events: list[dict | str]) -> str:
    parts = []
    for event in events:
        if not isinstance(event, dict):
            continue
        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            content = delta.get("content")
            if isinstance(content, str):
                parts.append(content)
    return "".join(parts)


def _collect_tool_calls(events: list[dict | str]) -> list[dict]:
    calls: dict[int, dict] = {}
    for event in events:
        if not isinstance(event, dict):
            continue
        for choice in event.get("choices", []):
            delta = choice.get("delta", {})
            for tool_call_delta in delta.get("tool_calls", []) or []:
                index = tool_call_delta.get("index", 0)
                existing = calls.setdefault(index, {"id": None, "name": None, "arguments": ""})
                if tool_call_delta.get("id"):
                    existing["id"] = tool_call_delta["id"]
                function = tool_call_delta.get("function", {})
                if function.get("name"):
                    existing["name"] = function["name"]
                if isinstance(function.get("arguments"), str):
                    existing["arguments"] += function["arguments"]
    return [calls[i] for i in sorted(calls)]


class PlainTextPassthroughTests(unittest.TestCase):
    def test_ordinary_text_is_forwarded_transparently_after_hold_window(self):
        events = [
            {"id": "s1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "Hello "}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": "there, "}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": "how can I help?"}}]},
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        self.assertEqual(_collect_text_content(decoded), "Hello there, how can I help?")
        self.assertEqual(decoded[-1], "[DONE]")

    def test_native_tool_calls_delta_passes_through_unmodified(self):
        native_tool_call_delta = {
            "id": "call_native",
            "type": "function",
            "function": {"name": "get_weather", "arguments": '{"location": "Berlin"}'},
        }
        events = [
            {
                "id": "s1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {"role": "assistant", "tool_calls": [native_tool_call_delta]},
                    }
                ],
            },
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "tool_calls", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        tool_calls = _collect_tool_calls(decoded)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["id"], "call_native")
        self.assertEqual(tool_calls[0]["name"], "get_weather")


class DialectRescueTests(unittest.TestCase):
    def test_function_tag_dialect_is_synthesized_into_tool_calls(self):
        events = [
            {
                "id": "s1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": '<function=get_weather>{"location": "Paris"}</function>',
                        },
                    }
                ],
            },
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        tool_calls = _collect_tool_calls(decoded)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "get_weather")
        self.assertEqual(json.loads(tool_calls[0]["arguments"]), {"location": "Paris"})
        # finish_reason must be overridden to tool_calls once synthesized.
        last_choice_event = next(
            e for e in reversed(decoded) if isinstance(e, dict) and e["choices"][0].get("finish_reason")
        )
        self.assertEqual(last_choice_event["choices"][0]["finish_reason"], "tool_calls")

    def test_marker_split_across_chunk_boundaries_is_still_rescued(self):
        # The dialect marker "<function=" itself is split across two
        # separate content deltas (as a real token-by-token stream would),
        # and the JSON payload is further split mid-way through as well.
        events = [
            {"id": "s1", "choices": [{"index": 0, "delta": {"role": "assistant", "content": "<funct"}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": "ion=get_wea"}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": 'ther>{"locat'}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": 'ion": "Paris"}</functio'}}]},
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": "n>"}}]},
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        tool_calls = _collect_tool_calls(decoded)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "get_weather")
        self.assertEqual(json.loads(tool_calls[0]["arguments"]), {"location": "Paris"})
        # No raw dialect text should have leaked into a content delta.
        self.assertNotIn("<function=", _collect_text_content(decoded))

    def test_kimi_dialect_marker_split_raw_sse_bytes_across_chunk_boundaries(self):
        # Beyond splitting across separate content deltas, also split the raw
        # SSE bytes of a single event mid-marker/mid-JSON (mirroring how the
        # existing think-tag sanitizer test splits "<think>" mid-tag at the
        # transport level), to exercise the incremental UTF-8 decoder and the
        # buffer's partial-event reassembly at the same time.
        full_event = _sse(
            {
                "id": "s1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": (
                                "<|tool_calls_section_begin|>"
                                "<|tool_call_begin|>functions.get_weather:0"
                                "<|tool_call_argument_begin|>"
                                '{"location": "Paris"}'
                                "<|tool_call_end|>"
                                "<|tool_calls_section_end|>"
                            ),
                        },
                    }
                ],
            }
        )
        split_point = len(full_event) // 2
        raw_events = [
            full_event[:split_point],
            full_event[split_point:],
            _sse({"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]}),
            b"data: [DONE]\n\n",
        ]
        decoded = _decode_events(_run_sanitizer(raw_events))
        tool_calls = _collect_tool_calls(decoded)
        self.assertEqual(len(tool_calls), 1)
        self.assertEqual(tool_calls[0]["name"], "get_weather")
        self.assertEqual(json.loads(tool_calls[0]["arguments"]), {"location": "Paris"})

    def test_unparseable_dialect_falls_back_to_raw_text_passthrough(self):
        # Marker present (function tag) but the closing tag never arrives:
        # since headers are already committed once streaming, this must
        # degrade to raw-text passthrough rather than aborting the stream.
        events = [
            {
                "id": "s1",
                "choices": [
                    {
                        "index": 0,
                        "delta": {
                            "role": "assistant",
                            "content": '<function=get_weather>{"location": "Paris"}',
                        },
                    }
                ],
            },
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        self.assertEqual(_collect_tool_calls(decoded), [])
        self.assertEqual(
            _collect_text_content(decoded),
            '<function=get_weather>{"location": "Paris"}',
        )
        last_choice_event = next(
            e for e in reversed(decoded) if isinstance(e, dict) and e["choices"][0].get("finish_reason")
        )
        self.assertEqual(last_choice_event["choices"][0]["finish_reason"], "stop")

    def test_bare_json_dialect_is_unreachable_in_streaming_and_passes_through_as_text(self):
        # Characterizes a deliberate, documented limitation (see the
        # DIALECT_MARKERS/DIALECT_TAG_MARKERS module docstring in
        # tool_call_rescue.py, and _sanitize_openai_stream_tool_call_rescue's
        # own docstring): only the three tag-delimited dialects (Kimi,
        # <function=>, <tool_call>) ever confirm "dialect" mode in streaming.
        # A lone leading "{" is not proof of a tool call, so as soon as the
        # accumulated text stops being a possible marker prefix (in
        # practice after the second character, since no tag marker starts
        # with '{"'), the hold-window flushes it transparently and it is
        # never handed to rescue_inline_tool_calls -- even for a
        # well-formed, schema-matching bare-JSON tool call. This trade-off
        # avoids stalling ordinary prose that happens to start with a
        # brace; the bare/```json-fenced dialect is only ever rescued in
        # the non-streaming path (a short, self-contained message).
        bare_json_tool_call = json.dumps(
            {"name": "get_weather", "arguments": {"location": "Paris"}}
        )
        events = [
            {
                "id": "s1",
                "choices": [
                    {"index": 0, "delta": {"role": "assistant", "content": bare_json_tool_call[:5]}}
                ],
            },
            {"id": "s1", "choices": [{"index": 0, "delta": {"content": bare_json_tool_call[5:]}}]},
            {"id": "s1", "choices": [{"index": 0, "finish_reason": "stop", "delta": {}}]},
            "data: [DONE]",
        ]
        decoded = _decode_events(_run_sanitizer(events))
        self.assertEqual(_collect_tool_calls(decoded), [])
        self.assertEqual(_collect_text_content(decoded), bare_json_tool_call)


if __name__ == "__main__":
    unittest.main()
