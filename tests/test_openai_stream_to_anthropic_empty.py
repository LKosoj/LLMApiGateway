import json
import unittest

from fastapi import Request
from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat import _openai_stream_to_anthropic
from tests._async_compat import run_async


def _parse_sse_events(chunks: list[bytes]) -> list[dict]:
    body = b"".join(chunks).decode("utf-8")
    events: list[dict] = []
    for block in body.strip().split("\n\n"):
        event_name = None
        event_data = None
        for line in block.splitlines():
            if line.startswith("event: "):
                event_name = line[len("event: "):]
            elif line.startswith("data: "):
                event_data = json.loads(line[len("data: "):])
        if event_name is not None and event_data is not None:
            events.append({"event": event_name, "data": event_data})
    return events


class OpenAiStreamToAnthropicEmptyTests(unittest.TestCase):
    def test_finish_only_upstream_emits_balanced_empty_text_block(self):
        async def body_iterator():
            yield (
                b'data: {"id":"chatcmpl-empty","choices":['
                b'{"delta":{},"finish_reason":"stop","index":0}'
                b"]}\n\n"
            )

        response = StreamingResponse(body_iterator(), media_type="text/event-stream")
        request = Request(scope={"type": "http", "method": "POST", "path": "/v1/messages"})
        result = _openai_stream_to_anthropic(request, response, "gateway-model")

        async def consume():
            return [chunk async for chunk in result.body_iterator]

        events = _parse_sse_events(run_async(consume()))

        self.assertEqual(
            [event["event"] for event in events],
            [
                "message_start",
                "content_block_start",
                "content_block_stop",
                "message_delta",
                "message_stop",
            ],
        )
        self.assertEqual(events[1]["data"]["content_block"], {"type": "text", "text": ""})
        self.assertEqual(events[1]["data"]["index"], 0)
        self.assertEqual(events[2]["data"], {"type": "content_block_stop", "index": 0})


if __name__ == "__main__":
    unittest.main()
