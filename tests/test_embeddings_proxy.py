import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from tests._async_compat import run_async
from llm_gateway_core.api.v1.embeddings import proxy_to_downstream


class _FakeDownstreamResponse:
    def __init__(self, payload, status_code: int = 200, text: str = "", headers: dict | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text
        self.headers = headers or {"content-type": "application/json"}

    def json(self):
        return self._payload


class _FakeStreamResponse:
    def __init__(self, chunks: list[bytes], status_code: int = 200, headers: dict | None = None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {"content-type": "text/event-stream"}

    async def aread(self):
        return b"".join(self._chunks)

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _FakeStreamContextManager:
    def __init__(self, response):
        self._response = response

    async def __aenter__(self):
        return self._response

    async def __aexit__(self, exc_type, exc, tb):
        return False


async def _collect_streaming_response_body(response: StreamingResponse) -> bytes:
    body = []
    async for chunk in response.body_iterator:
        body.append(chunk)
    return b"".join(body)


class EmbeddingsProxyTests(unittest.TestCase):
    def setUp(self):
        self.fake_http_client = Mock(spec=httpx.AsyncClient)

    def test_proxy_to_downstream_sends_request_via_shared_http_client_and_returns_raw_json(self):
        downstream_payload = {
            "object": "list",
            "data": [{"embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
        }
        self.fake_http_client.post = AsyncMock(return_value=_FakeDownstreamResponse(downstream_payload, status_code=200))

        with patch("llm_gateway_core.api.v1.embeddings.logger") as logger_mock:
            response_json, status_code = run_async(
                proxy_to_downstream(
                    "https://user:secret@example.com/v1/embeddings",
                    {"Content-Type": "application/json"},
                    {"model": "text-embedding-3-small", "input": ["hello"]},
                    self.fake_http_client,
                )
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(response_json, downstream_payload)
        self.fake_http_client.post.assert_awaited_once_with(
            "https://user:secret@example.com/v1/embeddings",
            headers={"Content-Type": "application/json"},
            json={"model": "text-embedding-3-small", "input": ["hello"]},
        )
        logger_messages = " ".join(
            str(call.args) + str(call.kwargs)
            for call in list(logger_mock.info.call_args_list) + list(logger_mock.warning.call_args_list)
        )
        self.assertIn("https://example.com/v1/embeddings", logger_messages)
        self.assertIn("200", logger_messages)
        self.assertNotIn("hello", logger_messages)

    def test_proxy_to_downstream_supports_streaming_passthrough(self):
        stream_response = _FakeStreamResponse(
            [b'data: {"id":"chunk-1"}\n\n', b"data: [DONE]\n\n"],
            headers={"content-type": "text/event-stream"},
        )
        self.fake_http_client.stream = Mock(return_value=_FakeStreamContextManager(stream_response))

        response = run_async(
            proxy_to_downstream(
                "https://example.com/v1/embeddings",
                {"Content-Type": "application/json"},
                {"model": "text-embedding-3-small", "input": ["hello"], "stream": True},
                self.fake_http_client,
            )
        )

        self.assertIsInstance(response, StreamingResponse)
        self.assertEqual(response.media_type, "text/event-stream")
        self.assertEqual(
            run_async(_collect_streaming_response_body(response)),
            b'data: {"id":"chunk-1"}\n\n' + b"data: [DONE]\n\n",
        )
        self.fake_http_client.stream.assert_called_once_with(
            "POST",
            "https://example.com/v1/embeddings",
            headers={"Content-Type": "application/json"},
            json={"model": "text-embedding-3-small", "input": ["hello"], "stream": True},
        )

    def test_proxy_to_downstream_maps_network_and_timeout_errors_to_503(self):
        for exception in (
            httpx.ConnectError(
                "connection failed",
                request=httpx.Request("POST", "https://example.com/v1/embeddings"),
            ),
            httpx.ReadTimeout(
                "timed out",
                request=httpx.Request("POST", "https://example.com/v1/embeddings"),
            ),
        ):
            with self.subTest(exception=type(exception).__name__):
                self.fake_http_client.post = AsyncMock(side_effect=exception)

                with self.assertRaises(HTTPException) as exc_info:
                    run_async(
                        proxy_to_downstream(
                            "https://example.com/v1/embeddings",
                            {"Content-Type": "application/json"},
                            {"model": "text-embedding-3-small", "input": ["hello"]},
                            self.fake_http_client,
                        )
                    )

                self.assertEqual(exc_info.exception.status_code, 503)
                self.assertIn("Downstream request failed", exc_info.exception.detail)

    def test_proxy_to_downstream_retries_network_errors_when_route_retry_is_enabled(self):
        downstream_payload = {
            "object": "list",
            "data": [{"embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
        }
        self.fake_http_client.post = AsyncMock(
            side_effect=[
                httpx.ConnectError(
                    "connection failed",
                    request=httpx.Request("POST", "https://example.com/v1/embeddings"),
                ),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        )

        with patch("llm_gateway_core.api.v1.embeddings.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            response_json, status_code = run_async(
                proxy_to_downstream(
                    "https://example.com/v1/embeddings",
                    {"Content-Type": "application/json"},
                    {"model": "text-embedding-3-small", "input": ["hello"]},
                    self.fake_http_client,
                    retry_count=1,
                    retry_delay=2.0,
                )
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(response_json, downstream_payload)
        self.assertEqual(self.fake_http_client.post.await_count, 2)
        sleep_mock.assert_awaited_once_with(2.0)

    def test_proxy_to_downstream_retries_retryable_http_statuses(self):
        downstream_payload = {
            "object": "list",
            "data": [{"embedding": [0.1, 0.2], "index": 0}],
            "model": "text-embedding-3-small",
        }
        self.fake_http_client.post = AsyncMock(
            side_effect=[
                _FakeDownstreamResponse({"error": {"message": "busy"}}, status_code=503),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        )

        with patch("llm_gateway_core.api.v1.embeddings.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            response_json, status_code = run_async(
                proxy_to_downstream(
                    "https://example.com/v1/embeddings",
                    {"Content-Type": "application/json"},
                    {"model": "text-embedding-3-small", "input": ["hello"]},
                    self.fake_http_client,
                    retry_count=1,
                    retry_delay=1.0,
                )
            )

        self.assertEqual(status_code, 200)
        self.assertEqual(response_json, downstream_payload)
        self.assertEqual(self.fake_http_client.post.await_count, 2)
        sleep_mock.assert_awaited_once_with(1.0)

    def test_proxy_to_downstream_does_not_retry_non_retryable_http_statuses(self):
        self.fake_http_client.post = AsyncMock(
            return_value=_FakeDownstreamResponse({"error": {"message": "bad request"}}, status_code=400)
        )

        with patch("llm_gateway_core.api.v1.embeddings.asyncio.sleep", new=AsyncMock()) as sleep_mock:
            with self.assertRaises(HTTPException) as exc_info:
                run_async(
                    proxy_to_downstream(
                        "https://example.com/v1/embeddings",
                        {"Content-Type": "application/json"},
                        {"model": "text-embedding-3-small", "input": ["hello"]},
                        self.fake_http_client,
                        retry_count=3,
                        retry_delay=5.0,
                    )
                )

        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertEqual(exc_info.exception.detail, "Downstream request failed with status 400.")
        self.fake_http_client.post.assert_awaited_once()
        sleep_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
