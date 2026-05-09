import json
import unittest

from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat import _openai_stream_to_responses
from tests._async_compat import run_async


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode("utf-8")


def _parse_sse_events(chunks: list[bytes]) -> list[dict]:
    response_text = b"".join(chunks).decode("utf-8")
    events = []
    for block in response_text.split("\n\n"):
        for line in block.splitlines():
            if not line.startswith("data: "):
                continue
            data = line[len("data: "):]
            if data == "[DONE]":
                continue
            events.append(json.loads(data))
    return events


class ResponsesApiToolCallsEventsTests(unittest.TestCase):
    def test_stream_completed_output_includes_text_message(self):
        async def body():
            yield _sse(
                {
                    "id": "chunk-text-1",
                    "created": 1735689600,
                    "choices": [{"delta": {"content": "Hel"}}],
                }
            )
            yield _sse(
                {
                    "id": "chunk-text-1",
                    "created": 1735689600,
                    "choices": [{"delta": {"content": "lo"}}],
                }
            )
            yield _sse(
                {
                    "id": "chunk-text-1",
                    "created": 1735689600,
                    "choices": [{"finish_reason": "stop"}],
                }
            )
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(body(), media_type="text/event-stream")
        result = _openai_stream_to_responses(response, "gateway-model")

        async def consume():
            return [chunk async for chunk in result.body_iterator]

        events = _parse_sse_events(run_async(consume()))
        completed_event = [event for event in events if event["type"] == "response.completed"][-1]

        self.assertEqual(len(completed_event["response"]["output"]), 1)
        message = completed_event["response"]["output"][0]
        self.assertEqual(message["type"], "message")
        self.assertEqual(message["content"][0]["text"], "Hello")

    def test_stream_emits_output_item_done_and_completed_output_for_tool_call(self):
        async def body():
            yield _sse(
                {
                    "id": "chunk-tool-1",
                    "created": 1735689600,
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "id": "call_weather_1",
                                        "type": "function",
                                        "function": {
                                            "name": "get_weather",
                                            "arguments": "{\"city\":\"Mos",
                                        },
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
            yield _sse(
                {
                    "id": "chunk-tool-1",
                    "created": 1735689600,
                    "choices": [
                        {
                            "delta": {
                                "tool_calls": [
                                    {
                                        "index": 0,
                                        "function": {"arguments": "cow\"}"},
                                    }
                                ]
                            }
                        }
                    ],
                }
            )
            yield _sse(
                {
                    "id": "chunk-tool-1",
                    "created": 1735689600,
                    "choices": [{"finish_reason": "tool_calls"}],
                }
            )
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(body(), media_type="text/event-stream")
        result = _openai_stream_to_responses(response, "gateway-model")

        async def consume():
            return [chunk async for chunk in result.body_iterator]

        events = _parse_sse_events(run_async(consume()))
        event_types = [event["type"] for event in events]

        added_index = event_types.index("response.output_item.added")
        done_index = event_types.index("response.output_item.done")
        completed_index = event_types.index("response.completed")
        self.assertLess(added_index, done_index)
        self.assertLess(done_index, completed_index)

        added_event = events[added_index]
        done_event = events[done_index]
        completed_event = events[completed_index]
        self.assertEqual(added_event["output_index"], 0)
        self.assertEqual(done_event["output_index"], 0)
        self.assertEqual(done_event["item"]["id"], added_event["item"]["id"])
        self.assertEqual(done_event["item"]["call_id"], "call_weather_1")
        self.assertEqual(done_event["item"]["name"], "get_weather")
        self.assertEqual(done_event["item"]["arguments"], "{\"city\":\"Moscow\"}")
        self.assertEqual(done_event["item"]["status"], "completed")
        self.assertEqual(completed_event["response"]["output"], [done_event["item"]])


if __name__ == "__main__":
    unittest.main()
