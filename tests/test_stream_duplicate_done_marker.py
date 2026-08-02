"""Upstreams that append bytes after the terminal `data: [DONE]` event.

anymodel repeats the marker verbatim on its `am/*` models. Forwarded as-is it
reaches `SSEFramer` downstream as `terminal_trailing_bytes`, and the response
observer rejects the whole response with a 502 `upstream_protocol_error` — after
the client has already received the answer.
"""

import json
import unittest

from llm_gateway_core.services.request_handler import _make_streaming_request
from llm_gateway_core.services.stream_observation import (
    SSEFramer,
    StreamObservationCapacity,
)
from tests._async_compat import run_async
from tests.test_retry_after_handling import _FakeStreamingClient

DONE = b"data: [DONE]\n\n"


def _sse(payload: dict) -> bytes:
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


def _content_chunk(text: str) -> bytes:
    return _sse(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "am/deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {"content": text}, "finish_reason": None}],
        }
    )


def _finish_chunk() -> bytes:
    return _sse(
        {
            "id": "chatcmpl-1",
            "object": "chat.completion.chunk",
            "model": "am/deepseek-v4-flash",
            "choices": [{"index": 0, "delta": {}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2035, "completion_tokens": 1, "total_tokens": 2036},
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


def _framer_trailing(body: bytes) -> int:
    """Run the client-facing bytes through the same framer the observer uses."""
    framer = SSEFramer(max_event_bytes=65536)
    framer.feed(body)
    return framer.terminal_trailing_bytes


class DuplicateDoneMarkerTests(unittest.TestCase):
    def test_duplicate_done_in_primed_chunk_is_clipped(self):
        """The whole stream arrives in one chunk — the anymodel short-answer case."""

        async def scenario():
            body, error_detail, capacity = await _collect(
                [_content_chunk("Да") + _finish_chunk() + DONE + DONE]
            )

            self.assertIsNone(error_detail)
            self.assertEqual(body.count(DONE), 1)
            self.assertTrue(body.endswith(DONE))
            # What the observer would compute downstream: no post-terminal bytes.
            self.assertEqual(_framer_trailing(body), 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_duplicate_done_after_streaming_starts_is_clipped(self):
        """Content spans several chunks, so the terminal lands in the main loop."""

        async def scenario():
            body, error_detail, _capacity = await _collect(
                [
                    _content_chunk("Пр"),
                    _content_chunk("ивет"),
                    _finish_chunk() + DONE + DONE,
                ]
            )

            self.assertIsNone(error_detail)
            self.assertEqual(body.count(DONE), 1)
            self.assertEqual(_framer_trailing(body), 0)

        run_async(scenario())

    def test_duplicate_done_in_a_separate_raw_chunk_is_never_read(self):
        """A second marker in its own chunk needs no clipping — we stop before it.

        Guards the other half of the contract: reading stops at the first terminal,
        so the repeat never reaches the client in the first place.
        """

        async def scenario():
            body, error_detail, _capacity = await _collect(
                [_content_chunk("да"), _finish_chunk(), DONE, DONE]
            )

            self.assertIsNone(error_detail)
            self.assertEqual(body.count(DONE), 1)
            self.assertEqual(_framer_trailing(body), 0)

        run_async(scenario())

    def test_arbitrary_noise_after_terminal_is_dropped(self):
        """Not just a repeated marker: any post-terminal bytes are upstream noise."""

        async def scenario():
            body, error_detail, _capacity = await _collect(
                [_content_chunk("да") + _finish_chunk() + DONE + _content_chunk("хвост")]
            )

            self.assertIsNone(error_detail)
            self.assertTrue(body.endswith(DONE))
            # Only the two pre-terminal chunks survive; the trailing one is gone.
            self.assertEqual(body.count(b"chat.completion.chunk"), 2)
            self.assertEqual(_framer_trailing(body), 0)

        run_async(scenario())

    def test_single_done_stream_is_forwarded_untouched(self):
        async def scenario():
            chunks = [_content_chunk("да"), _finish_chunk(), DONE]
            body, error_detail, _capacity = await _collect(chunks)

            self.assertIsNone(error_detail)
            self.assertEqual(body, b"".join(chunks))

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
