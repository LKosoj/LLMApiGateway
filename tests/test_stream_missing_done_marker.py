"""Upstreams that end an SSE stream without `data: [DONE]`.

MiniMax closes the body right after the terminal finish_reason chunk. The
gateway treats a missing terminal as an empty/protocol failure and aborts the
response mid-body, so the client loses an answer that already arrived in full.
"""

import json
import unittest

from llm_gateway_core.services.request_handler import _make_streaming_request
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from tests._async_compat import run_async
from tests.test_retry_after_handling import _FakeStreamingClient


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _content_chunk(text: str) -> bytes:
    return _sse(
        {
            "id": "chunk-1",
            "object": "chat.completion.chunk",
            "model": "MiniMax-M2.7-highspeed",
            "choices": [{"index": 0, "delta": {"content": text}}],
        }
    )


def _finish_chunk(text: str = "", finish_reason: str = "stop") -> bytes:
    return _sse(
        {
            "id": "chunk-2",
            "object": "chat.completion.chunk",
            "model": "MiniMax-M2.7-highspeed",
            "choices": [
                {
                    "index": 0,
                    "delta": {"content": text},
                    "finish_reason": finish_reason,
                }
            ],
        }
    )


async def _collect(chunks):
    client = _FakeStreamingClient(chunks)
    capacity = StreamObservationCapacity(max_items=16, max_bytes=65536)
    response, error_detail = await _make_streaming_request(
        client,
        "https://upstream.example/v1/chat/completions",
        {},
        {},
        stream_observation_capacity=capacity,
        stream_event_max_bytes=4096,
    )
    body = b""
    async for piece in response.body_iterator:
        body += piece
    return body, error_detail, capacity


class MissingDoneMarkerTests(unittest.TestCase):
    def test_finish_reason_without_done_marker_gets_a_synthesized_terminal(self):
        async def scenario():
            body, error_detail, capacity = await _collect(
                [_content_chunk("Привет"), _finish_chunk("!")]
            )

            self.assertIsNone(error_detail)
            self.assertIn(b"data: [DONE]\n\n", body)
            self.assertTrue(body.endswith(b"data: [DONE]\n\n"))
            # The upstream text is still delivered ahead of the marker.
            self.assertIn(b"\\u041f\\u0440\\u0438\\u0432\\u0435\\u0442", body)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_upstream_done_marker_is_not_duplicated(self):
        async def scenario():
            body, error_detail, _capacity = await _collect(
                [_content_chunk("да"), _finish_chunk(), b"data: [DONE]\n\n"]
            )

            self.assertIsNone(error_detail)
            self.assertEqual(body.count(b"data: [DONE]"), 1)

        run_async(scenario())

    def test_stream_cut_short_without_finish_reason_is_not_completed(self):
        async def scenario():
            body, error_detail, _capacity = await _collect(
                [_content_chunk("обрыв на середине")]
            )

            # No finish_reason means the upstream never finished: the truncated
            # answer must not be dressed up as a complete one.
            self.assertIsNone(error_detail)
            self.assertNotIn(b"[DONE]", body)

        run_async(scenario())

    def test_finish_reason_split_across_raw_chunks_still_terminates(self):
        async def scenario():
            event = _finish_chunk("хвост")
            body, error_detail, _capacity = await _collect(
                [_content_chunk("начало"), event[:20], event[20:]]
            )

            self.assertIsNone(error_detail)
            self.assertTrue(body.endswith(b"data: [DONE]\n\n"))

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
