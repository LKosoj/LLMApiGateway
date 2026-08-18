"""Tests for Retry-After header compliance and jitter in retry scheduling."""

import asyncio
import gzip
import unittest
import zlib
from contextlib import ExitStack
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import main
from llm_gateway_core.api.v1.chat import (
    _anthropic_stream_to_openai,
    _compute_retry_sleep_seconds,
    _extract_retry_after,
    _openai_stream_to_anthropic,
    _openai_stream_to_responses,
    _sanitize_anthropic_stream_think_tags,
    _sanitize_openai_json_object_stream,
    _sanitize_openai_stream_think_tags,
)
from llm_gateway_core.services.request_handler import (
    LocalStreamObservationError,
    MAX_RETRY_AFTER_SECONDS,
    RequestErrorDetail,
    _StreamResourceOwner,
    _clamped_retry_after,
    _make_json_request,
    _make_streaming_request,
    make_llm_request,
    parse_retry_after_header,
)
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from tests._async_compat import run_async
from tests.chat_accounting_test_support import install_main_chat_accounting_double


class ParseRetryAfterHeaderTests(unittest.TestCase):
    def test_returns_none_when_missing(self):
        self.assertIsNone(parse_retry_after_header(None))
        self.assertIsNone(parse_retry_after_header(""))
        self.assertIsNone(parse_retry_after_header("   "))

    def test_parses_delta_seconds(self):
        self.assertEqual(parse_retry_after_header("30"), 30.0)
        self.assertEqual(parse_retry_after_header(" 5.5 "), 5.5)

    def test_rejects_negative_delta_seconds(self):
        self.assertIsNone(parse_retry_after_header("-1"))

    def test_parses_http_date(self):
        target = datetime.now(tz=timezone.utc) + timedelta(seconds=30)
        header = format_datetime(target, usegmt=True)

        parsed = parse_retry_after_header(header)

        self.assertIsNotNone(parsed)
        self.assertGreater(parsed, 20)
        self.assertLess(parsed, 40)

    def test_returns_zero_for_past_http_date(self):
        past_date = datetime.now(tz=timezone.utc) - timedelta(seconds=60)
        header = format_datetime(past_date, usegmt=True)

        self.assertEqual(parse_retry_after_header(header), 0.0)

    def test_returns_none_for_invalid_input(self):
        self.assertIsNone(parse_retry_after_header("not-a-date"))


class ClampedRetryAfterTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(_clamped_retry_after(None))

    def test_negative_becomes_zero(self):
        self.assertEqual(_clamped_retry_after(-1), 0.0)

    def test_within_bounds_returned_as_is(self):
        self.assertEqual(_clamped_retry_after(42.5), 42.5)

    def test_clamped_to_max(self):
        self.assertEqual(_clamped_retry_after(10_000.0), MAX_RETRY_AFTER_SECONDS)


class RequestErrorDetailTests(unittest.TestCase):
    def test_str_compatibility(self):
        detail = RequestErrorDetail("something failed", retry_after=15.0, status_code=429)

        self.assertEqual(detail, "something failed")
        self.assertIsInstance(detail, str)
        self.assertEqual(detail.retry_after, 15.0)
        self.assertEqual(detail.status_code, 429)

    def test_default_metadata_is_none(self):
        detail = RequestErrorDetail("oops")
        self.assertIsNone(detail.retry_after)
        self.assertIsNone(detail.status_code)


class MakeJsonRequestRetryAfterTests(unittest.TestCase):
    def test_json_request_records_retry_after_on_429(self):
        response = httpx.Response(
            status_code=429,
            text="slow down",
            headers={"Retry-After": "7"},
        )
        fake_client = SimpleNamespace(post=AsyncMock(return_value=response))

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(response_data)
        self.assertIsInstance(error_detail, RequestErrorDetail)
        self.assertEqual(error_detail.retry_after, 7.0)
        self.assertEqual(error_detail.status_code, 429)

    def test_json_request_sets_retry_after_none_when_header_missing(self):
        response = httpx.Response(status_code=500, text="internal error")
        fake_client = SimpleNamespace(post=AsyncMock(return_value=response))

        _response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsInstance(error_detail, RequestErrorDetail)
        self.assertIsNone(error_detail.retry_after)
        self.assertEqual(error_detail.status_code, 500)


class MakeJsonRequestResponseHeadersSinkTests(unittest.TestCase):
    def test_json_request_populates_response_headers_sink_on_success(self):
        response = httpx.Response(
            status_code=200,
            json={"ok": True},
            headers={"X-RateLimit-Remaining": "42"},
        )
        fake_client = SimpleNamespace(post=AsyncMock(return_value=response))
        sink: dict[str, str] = {}

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                response_headers_sink=sink,
            )
        )

        self.assertIsNone(error_detail)
        self.assertEqual(response_data, {"ok": True})
        self.assertEqual(sink.get("x-ratelimit-remaining"), "42")

    def test_json_request_response_headers_sink_untouched_when_not_provided(self):
        response = httpx.Response(
            status_code=200,
            json={"ok": True},
            headers={"X-RateLimit-Remaining": "42"},
        )
        fake_client = SimpleNamespace(post=AsyncMock(return_value=response))

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(error_detail)
        self.assertEqual(response_data, {"ok": True})


class _FakeStreamingResponse:
    def __init__(self, chunks, *, status_code=200, headers=None):
        self._chunks = chunks
        self.status_code = status_code
        self.headers = headers or {}
        self.aread_called = False

    async def aiter_raw(self, *, chunk_size=None):
        for chunk in self._chunks:
            if chunk_size is None:
                yield chunk
                continue
            for offset in range(0, len(chunk), chunk_size):
                yield chunk[offset:offset + chunk_size]

    async def aread(self):
        self.aread_called = True
        raise AssertionError("streaming error bodies must not use unbounded aread()")


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response
        self.closed = False
        self.exit_calls = 0

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.exit_calls += 1
        self.closed = True


class _FakeStreamingClient:
    def __init__(self, chunks, *, status_code=200, headers=None):
        self.response = _FakeStreamingResponse(
            chunks,
            status_code=status_code,
            headers=headers,
        )
        self.context = _FakeStreamContext(self.response)
        self.stream_kwargs = None

    def stream(self, *args, **kwargs):
        self.stream_kwargs = kwargs
        return self.context


class MakeStreamingRequestTests(unittest.TestCase):
    def test_resource_owner_runs_cleanup_when_task_creation_fails(self):
        class TaskCreationFailure(MemoryError):
            pass

        async def scenario():
            cleanup_calls = 0
            cleanup_started = asyncio.Event()
            cleanup_release = asyncio.Event()
            primary = TaskCreationFailure("SECRET-LOCAL")

            async def cleanup():
                nonlocal cleanup_calls
                cleanup_calls += 1
                cleanup_started.set()
                await cleanup_release.wait()

            owner = _StreamResourceOwner(cleanup)
            with patch(
                "llm_gateway_core.services.request_handler.asyncio.create_task",
                side_effect=primary,
            ):
                loop = asyncio.get_running_loop()
                first_close = loop.create_task(owner.close())
                await cleanup_started.wait()
                second_close = loop.create_task(owner.close())
                await asyncio.sleep(0)
                self.assertEqual(cleanup_calls, 1)
                cleanup_release.set()
                results = await asyncio.gather(
                    first_close,
                    second_close,
                    return_exceptions=True,
                )

                self.assertTrue(all(result is primary for result in results))
                with self.assertRaises(TaskCreationFailure) as raised:
                    await owner.close()
                self.assertIs(raised.exception, primary)

            self.assertEqual(cleanup_calls, 1)

        run_async(scenario())

    def test_resource_owner_finishes_cleanup_after_repeated_cancellation(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=1, max_bytes=8)
            lease = await capacity.acquire(8)
            cleanup_started = asyncio.Event()
            cleanup_release = asyncio.Event()
            cleanup_done = asyncio.Event()
            context_closed = False

            async def cleanup():
                nonlocal context_closed
                cleanup_started.set()
                try:
                    await cleanup_release.wait()
                finally:
                    lease.release_all()
                    context_closed = True
                    cleanup_done.set()

            owner = _StreamResourceOwner(cleanup)
            close_task = asyncio.create_task(owner.close())
            await cleanup_started.wait()

            close_task.cancel("first")
            await asyncio.sleep(0)
            close_task.cancel("second")
            await asyncio.sleep(0)
            self.assertFalse(cleanup_done.is_set())

            cleanup_release.set()
            with self.assertRaises(asyncio.CancelledError) as cancelled:
                await close_task

            self.assertEqual(cancelled.exception.args, ("first",))
            self.assertTrue(cleanup_done.is_set())
            self.assertTrue(context_closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_stream_setup_constructor_failure_closes_entered_context(self):
        class SetupFailure(BaseException):
            pass

        async def scenario():
            client = _FakeStreamingClient([])
            capacity = StreamObservationCapacity(max_items=2, max_bytes=256)
            primary = SetupFailure()

            with patch(
                "llm_gateway_core.services.request_handler._StreamResourceOwner",
                side_effect=primary,
            ):
                try:
                    await _make_streaming_request(
                        client,
                        "https://upstream.example/v1/chat/completions",
                        {},
                        {},
                        stream_observation_capacity=capacity,
                        stream_event_max_bytes=128,
                    )
                except SetupFailure as exc:
                    self.assertIs(exc, primary)
                else:
                    self.fail("stream owner constructor failure was not propagated")

            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_streaming_response_constructor_failure_releases_primed_capacity(self):
        class ResponseConstructionFailure(BaseException):
            pass

        async def scenario():
            client = _FakeStreamingClient(
                [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n']
            )
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            primary = ResponseConstructionFailure()

            with patch(
                "llm_gateway_core.services.request_handler._OwnedStreamingResponse",
                side_effect=primary,
            ):
                with self.assertRaises(ResponseConstructionFailure) as raised:
                    await _make_streaming_request(
                        client,
                        "https://upstream.example/v1/chat/completions",
                        {},
                        {},
                        stream_observation_capacity=capacity,
                        stream_event_max_bytes=256,
                    )

            self.assertIs(raised.exception, primary)
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_streaming_result_construction_failure_releases_primed_capacity(self):
        class ResultConstructionFailure(BaseException):
            pass

        async def scenario():
            client = _FakeStreamingClient(
                [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n']
            )
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            primary = ResultConstructionFailure()

            with patch(
                "llm_gateway_core.services.request_handler._build_streaming_response_result",
                side_effect=primary,
            ):
                with self.assertRaises(ResultConstructionFailure) as raised:
                    await _make_streaming_request(
                        client,
                        "https://upstream.example/v1/chat/completions",
                        {},
                        {},
                        stream_observation_capacity=capacity,
                        stream_event_max_bytes=256,
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(client.context.exit_calls, 1)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_owned_response_task_creation_failure_cleans_stream_once(self):
        class SendFailure(Exception):
            pass

        async def scenario():
            client = _FakeStreamingClient(
                [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n']
            )
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
            )
            self.assertIsNone(error_detail)
            primary = SendFailure()
            task_creation_failure = MemoryError("SECRET-LOCAL")

            async def send(_message):
                raise primary

            async def receive():
                return {"type": "http.disconnect"}

            with patch(
                "llm_gateway_core.services.request_handler.asyncio.create_task",
                side_effect=task_creation_failure,
            ):
                with self.assertRaises(SendFailure) as raised:
                    await response(
                        {"type": "http", "asgi": {"spec_version": "2.4"}},
                        receive,
                        send,
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(client.context.exit_calls, 1)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(scenario())

    def test_concurrent_raw_reads_are_bounded_before_upstream_anext(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=1, max_bytes=256)
            entered_three_reads = asyncio.Event()
            entered_reads = 0

            class SlowResponse:
                status_code = 200
                headers = {}

                async def aiter_raw(self, *, chunk_size=None):
                    nonlocal entered_reads
                    entered_reads += 1
                    if entered_reads == 3:
                        entered_three_reads.set()
                    await asyncio.Event().wait()
                    yield b"unreachable"

            clients = []
            tasks = []
            for _ in range(8):
                client = SimpleNamespace()
                client.context = _FakeStreamContext(SlowResponse())
                client.stream = Mock(return_value=client.context)
                clients.append(client)
                tasks.append(
                    asyncio.create_task(
                        _make_streaming_request(
                            client,
                            "https://upstream.example/v1/chat/completions",
                            {},
                            {},
                            stream_observation_capacity=capacity,
                            stream_event_max_bytes=128,
                        )
                    )
                )

            await asyncio.wait_for(entered_three_reads.wait(), timeout=1)
            for _ in range(5):
                await asyncio.sleep(0)
            self.assertEqual(entered_reads, 3)
            self.assertEqual(capacity.snapshot.active_bytes, 255)

            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

            self.assertTrue(all(client.context.closed for client in clients))
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)
            self.assertEqual(capacity.snapshot.waiters, 0)

        run_async(scenario())

    def test_equal_capacity_gzip_http_error_does_not_deadlock(self):
        async def scenario():
            client = _FakeStreamingClient(
                [gzip.compress(bytes(range(60)))],
                status_code=500,
                headers={"content-encoding": "gzip"},
            )
            capacity = StreamObservationCapacity(max_items=2, max_bytes=60)

            response, error_detail = await asyncio.wait_for(
                _make_streaming_request(
                    client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=60,
                ),
                timeout=1,
            )

            self.assertIsNone(response)
            self.assertEqual(
                error_detail,
                "Upstream request failed with HTTP status 500.",
            )
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)
            self.assertEqual(capacity.snapshot.waiters, 0)

        run_async(scenario())

    def test_asgi_send_failure_closes_owned_stream_before_and_after_first_yield(self):
        class SendFailure(BaseException):
            pass

        async def scenario(fail_on_call: int):
            chunk = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
            fake_client = _FakeStreamingClient([chunk])
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
            response, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
            )
            self.assertIsNone(error_detail)
            primary = SendFailure()
            send_calls = 0

            async def send(_message):
                nonlocal send_calls
                send_calls += 1
                if send_calls == fail_on_call:
                    raise primary

            async def receive():
                return {"type": "http.disconnect"}

            try:
                await response(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
            except SendFailure as exc:
                self.assertIs(exc, primary)
            else:
                self.fail("send failure was not propagated")
            self.assertTrue(fake_client.context.closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        for fail_on_call in (1, 2):
            with self.subTest(fail_on_call=fail_on_call):
                run_async(scenario(fail_on_call))

    def test_chat_stream_wrappers_preserve_owned_cleanup_on_send_failure(self):
        openai_chunk = (
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        )
        anthropic_chunk = (
            b"event: content_block_delta\n"
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        )
        request = SimpleNamespace(state=SimpleNamespace(usage_tracker={}))
        cases = (
            (
                "openai-think",
                openai_chunk,
                lambda response: _sanitize_openai_stream_think_tags(
                    response,
                    "gateway-model",
                ),
            ),
            (
                "anthropic-think",
                anthropic_chunk,
                lambda response: _sanitize_anthropic_stream_think_tags(
                    response,
                    "gateway-model",
                ),
            ),
            (
                "json-object",
                openai_chunk,
                lambda response: _sanitize_openai_json_object_stream(
                    response,
                    "gateway-model",
                ),
            ),
            (
                "anthropic-to-openai",
                anthropic_chunk,
                lambda response: _anthropic_stream_to_openai(
                    request,
                    response,
                    "gateway-model",
                ),
            ),
            (
                "openai-to-anthropic",
                openai_chunk,
                lambda response: _openai_stream_to_anthropic(
                    request,
                    response,
                    "gateway-model",
                ),
            ),
            (
                "openai-to-responses",
                openai_chunk,
                lambda response: _openai_stream_to_responses(
                    response,
                    "gateway-model",
                    request,
                ),
            ),
        )

        async def scenario(chunk, wrap, fail_on_call: int):
            client = _FakeStreamingClient([chunk])
            capacity = StreamObservationCapacity(max_items=2, max_bytes=4096)
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNone(error_detail)
            wrapped = wrap(response)
            self.assertIs(wrapped, response)
            primary = RuntimeError("send failed")
            send_calls = 0

            async def send(_message):
                nonlocal send_calls
                send_calls += 1
                if send_calls == fail_on_call:
                    raise primary

            async def receive():
                return {"type": "http.disconnect"}

            with self.assertRaises(RuntimeError) as raised:
                await wrapped(
                    {"type": "http", "asgi": {"spec_version": "2.4"}},
                    receive,
                    send,
                )
            self.assertIs(raised.exception, primary)
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_items, 0)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        for name, chunk, wrap in cases:
            for fail_on_call in (1, 2):
                with self.subTest(name=name, fail_on_call=fail_on_call):
                    run_async(scenario(chunk, wrap, fail_on_call))

    def test_public_streaming_request_requires_explicit_capacity_and_event_limit(self):
        fake_client = _FakeStreamingClient([])

        with self.assertRaises(LocalStreamObservationError) as missing_capacity:
            run_async(
                make_llm_request(
                    fake_client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    True,
                )
            )
        self.assertEqual(missing_capacity.exception.reason_code, "stream_capacity_required")

        with self.assertRaises(LocalStreamObservationError) as missing_limit:
            run_async(
                make_llm_request(
                    fake_client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    True,
                    stream_observation_capacity=StreamObservationCapacity(
                        max_items=2,
                        max_bytes=64,
                    ),
                )
            )
        self.assertEqual(missing_limit.exception.reason_code, "stream_event_limit_required")

    def test_stream_priming_buffer_is_limited_before_first_content_chunk(self):
        event_limit = 128
        comment_chunk = b": keepalive\n\n"
        oversized_comments = comment_chunk * ((event_limit // len(comment_chunk)) + 1)
        fake_client = _FakeStreamingClient([oversized_comments])
        capacity = StreamObservationCapacity(max_items=4, max_bytes=512)

        response_data, error_detail = run_async(
            _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=event_limit,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream priming exceeded maximum buffered bytes before content.")
        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_prefetch_item_limit_fails_typed_without_self_deadlock(self):
        chunks = [
            b": keepalive\n\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=1, max_bytes=4096)

        async def scenario():
            with self.assertRaises(LocalStreamObservationError) as error:
                await asyncio.wait_for(
                    _make_streaming_request(
                        fake_client,
                        "https://upstream.example/v1/chat/completions",
                        {},
                        {},
                        stream_observation_capacity=capacity,
                        stream_event_max_bytes=1024,
                    ),
                    timeout=1,
                )
            self.assertEqual(
                error.exception.reason_code,
                "prefetch_item_capacity_exhausted",
            )

        run_async(scenario())

        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_items, 0)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_streaming_request_sets_ttft_ms_from_first_content_chunk(self):
        chunks = [
            b": keepalive\n\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        async def scenario():
            response_data, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )

            self.assertIsNone(error_detail)
            self.assertIsNotNone(response_data)
            ttft_ms = response_data.llmgateway_ttft_ms
            self.assertIsInstance(ttft_ms, int)
            self.assertGreaterEqual(ttft_ms, 0)

            async for _ in response_data.body_iterator:
                pass

        run_async(scenario())
        self.assertTrue(fake_client.context.closed)

    def test_streaming_request_has_no_ttft_ms_when_stream_ends_without_content(self):
        fake_client = _FakeStreamingClient([])
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        response_data, error_detail = run_async(
            _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Stream ended before any content chunks were received.",
        )

    def test_raw_gzip_decode_is_bounded_for_success_error_and_cancel(self):
        event = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'

        async def success_scenario():
            compressed = gzip.compress(event)
            client = _FakeStreamingClient(
                [compressed],
                headers={"content-encoding": "gzip"},
            )
            capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNone(error_detail)
            self.assertEqual(
                b"".join([part async for part in response.body_iterator]),
                event,
            )
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        async def error_scenario():
            compressed = gzip.compress(b"X" * 1_000_000)
            client = _FakeStreamingClient(
                [compressed],
                status_code=500,
                headers={"content-encoding": "gzip"},
            )
            capacity = StreamObservationCapacity(max_items=3, max_bytes=512)
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=128,
            )
            self.assertIsNone(response)
            self.assertEqual(
                error_detail,
                "Upstream request failed with HTTP status 500.",
            )
            self.assertLessEqual(capacity.snapshot.high_water_bytes, 512)
            self.assertEqual(capacity.snapshot.active_bytes, 0)
            self.assertTrue(client.context.closed)

        async def cancel_scenario():
            compressor = zlib.compressobj(wbits=16 + zlib.MAX_WBITS)
            first_raw = compressor.compress(event) + compressor.flush(zlib.Z_SYNC_FLUSH)
            final_raw = compressor.compress(b"X" * 1_000_000) + compressor.flush()
            client = _FakeStreamingClient(
                [first_raw, final_raw],
                headers={"content-encoding": "gzip"},
            )
            capacity = StreamObservationCapacity(max_items=3, max_bytes=4096)
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNone(error_detail)
            iterator = response.body_iterator
            self.assertEqual(await anext(iterator), event)
            await iterator.aclose()
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_bytes, 0)

        run_async(success_scenario())
        run_async(error_scenario())
        run_async(cancel_scenario())

    def test_raw_deflate_detects_format_when_header_is_split_after_one_byte(self):
        event = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        compressor = zlib.compressobj(wbits=-zlib.MAX_WBITS)
        compressed = compressor.compress(event) + compressor.flush()
        client = _FakeStreamingClient(
            [compressed[:1], compressed[1:]],
            headers={"content-encoding": "deflate"},
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        async def scenario():
            response, error_detail = await _make_streaming_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNone(error_detail)
            self.assertEqual(
                b"".join([part async for part in response.body_iterator]),
                event,
            )
            self.assertTrue(client.context.closed)
            self.assertEqual(capacity.snapshot.active_bytes, 0)
            self.assertEqual(
                client.stream_kwargs["headers"]["Accept-Encoding"],
                "gzip, deflate, identity",
            )

        run_async(scenario())

    def test_invalid_compression_remains_a_provider_failure(self):
        client = _FakeStreamingClient(
            [b"not-a-gzip-stream"],
            headers={"content-encoding": "gzip"},
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        response, error_detail = run_async(
            make_llm_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {"model": "provider-model", "stream": True},
                True,
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsNone(response)
        self.assertIn("DecodingError", error_detail)
        self.assertIn("compressed_stream_invalid", error_detail)
        self.assertTrue(client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_local_decoder_memory_error_is_fatal_and_releases_stream(self):
        client = _FakeStreamingClient(
            [b"unused"],
            headers={"content-encoding": "gzip"},
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)
        primary = MemoryError("SECRET-LOCAL")

        with (
            patch(
                "llm_gateway_core.services.request_handler.zlib.decompressobj",
                side_effect=primary,
            ),
            patch("llm_gateway_core.services.request_handler.logging.error") as log_error,
            self.assertRaises(MemoryError) as raised,
        ):
            run_async(
                make_llm_request(
                    client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {"model": "provider-model", "stream": True},
                    True,
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=1024,
                )
            )

        self.assertIs(raised.exception, primary)
        log_error.assert_not_called()
        self.assertTrue(client.context.closed)
        self.assertEqual(capacity.snapshot.active_items, 0)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_sse_error_detail_never_exposes_raw_provider_message(self):
        secret = "SECRET-UPSTREAM-DETAIL"
        client = _FakeStreamingClient(
            [
                (
                    'data: {"error":{"message":"'
                    + secret
                    + '"}}\n\n'
                ).encode()
            ]
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        with self.assertLogs(level="WARNING") as logs:
            response, error_detail = run_async(
                make_llm_request(
                    client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {"model": "provider-model", "stream": True},
                    True,
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=1024,
                    stream_request_id="request-123",
                )
            )

        self.assertIsNone(response)
        self.assertEqual(
            error_detail,
            "Upstream stream reported an error event.",
        )
        self.assertNotIn(secret, "\n".join(logs.output))
        self.assertNotIn(secret, error_detail)
        self.assertIn("request_id=request-123", "\n".join(logs.output))

    def test_sse_context_overflow_keeps_only_safe_classification(self):
        client = _FakeStreamingClient(
            [
                b'data: {"error":{"message":"context_length_exceeded: '
                b'SECRET"}}\n\n'
            ]
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        response, error_detail = run_async(
            make_llm_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {"model": "provider-model", "stream": True},
                True,
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsNone(response)
        self.assertEqual(
            error_detail,
            "Upstream stream reported context overflow.",
        )
        self.assertNotIn("SECRET", error_detail)

    def test_sse_rate_limit_keeps_safe_temporary_classification(self):
        secret = "SECRET-RATE-LIMIT-DETAIL"
        client = _FakeStreamingClient(
            [
                (
                    'data: {"error":{"code":429,"message":"'
                    + secret
                    + '"}}\n\n'
                ).encode()
            ]
        )
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        response, error_detail = run_async(
            make_llm_request(
                client,
                "https://upstream.example/v1/chat/completions",
                {},
                {"model": "provider-model", "stream": True},
                True,
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
        )

        self.assertIsNone(response)
        self.assertEqual(
            error_detail,
            "Upstream stream reported HTTP status 429.",
        )
        self.assertEqual(error_detail.status_code, 429)
        self.assertNotIn(secret, error_detail)

    def test_stream_framer_handles_split_utf8_crlf_multiline_and_eof_done(self):
        content = "Привет 👋".encode("utf-8")
        split_at = content.index("👋".encode("utf-8")) + 1
        chunks = [
            b": keepalive\r\n",
            b"data: {\"choices\":\r\n",
            b"data: [{\"delta\":{\"content\":\"" + content[:split_at],
            content[split_at:] + b"\"}}]}\r\n\r\n",
            b"data: [DONE]",
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        async def scenario():
            response, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNotNone(response)
            body = b"".join([chunk async for chunk in response.body_iterator])
            return body, error_detail

        body, error_detail = run_async(scenario())

        self.assertIsNone(error_detail)
        self.assertEqual(body, b"".join(chunks))
        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_invalid_complete_sse_payload_fails_closed_without_payload_log(self):
        secret = b"SUPER-SECRET-PROMPT"
        fake_client = _FakeStreamingClient([b"data: " + secret + b"\n\n"])
        capacity = StreamObservationCapacity(max_items=4, max_bytes=1024)

        with self.assertLogs(level="WARNING") as logs:
            response_data, error_detail = run_async(
                _make_streaming_request(
                    fake_client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=256,
                )
            )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Upstream stream contained an invalid SSE data event.",
        )
        self.assertTrue(fake_client.context.closed)
        joined_logs = "\n".join(logs.output)
        self.assertNotIn(secret.decode("ascii"), joined_logs)
        self.assertIn("reason=invalid_json", joined_logs)
        self.assertIn("sha256=", joined_logs)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_null_sse_data_event_is_skipped_and_stream_continues(self):
        chunks = [
            b'data: {"choices":[{"delta":{"content":"OK"}}]}\n\n',
            b"data: null\n\n",
            b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n\n',
            b"data: [DONE]\n\n",
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=4, max_bytes=4096)

        async def scenario():
            response, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=1024,
            )
            self.assertIsNotNone(response)
            body = b"".join([chunk async for chunk in response.body_iterator])
            return body, error_detail

        body, error_detail = run_async(scenario())

        self.assertIsNone(error_detail)
        self.assertEqual(body, b"".join(chunks))
        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_prefetched_bytes_release_exactly_as_replay_advances(self):
        chunks = [
            b": keepalive\n\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)

        async def scenario():
            response, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
            )
            self.assertIsNone(error_detail)
            self.assertEqual(capacity.snapshot.active_bytes, sum(map(len, chunks)))
            self.assertEqual(capacity.snapshot.active_items, len(chunks))

            iterator = response.body_iterator
            self.assertEqual(await anext(iterator), chunks[0])
            self.assertEqual(capacity.snapshot.active_bytes, sum(map(len, chunks)))
            self.assertEqual(capacity.snapshot.active_items, len(chunks))
            self.assertEqual(await anext(iterator), chunks[1])
            self.assertEqual(capacity.snapshot.active_bytes, len(chunks[1]))
            self.assertEqual(capacity.snapshot.active_items, 1)
            with self.assertRaises(StopAsyncIteration):
                await anext(iterator)

        run_async(scenario())

        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)
        self.assertEqual(capacity.snapshot.active_items, 0)

    def test_replay_close_releases_current_and_unreplayed_prefetch(self):
        chunks = [
            b": keepalive\n\n",
            b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n',
        ]
        fake_client = _FakeStreamingClient(chunks)
        capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)

        async def scenario():
            response, error_detail = await _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
            )
            self.assertIsNone(error_detail)
            iterator = response.body_iterator
            self.assertEqual(await anext(iterator), chunks[0])
            await iterator.aclose()

        run_async(scenario())

        self.assertTrue(fake_client.context.closed)
        self.assertEqual(capacity.snapshot.active_bytes, 0)
        self.assertEqual(capacity.snapshot.active_items, 0)

    def test_concurrent_priming_shares_one_process_capacity(self):
        chunk = b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'
        capacity = StreamObservationCapacity(max_items=1, max_bytes=len(chunk) * 3)
        first_client = _FakeStreamingClient([chunk])
        second_client = _FakeStreamingClient([chunk])

        async def scenario():
            first_response, first_error = await _make_streaming_request(
                first_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=len(chunk),
            )
            self.assertIsNone(first_error)

            second_task = asyncio.create_task(
                _make_streaming_request(
                    second_client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=len(chunk),
                )
            )
            await asyncio.sleep(0)
            self.assertFalse(second_task.done())
            self.assertEqual(capacity.snapshot.waiters, 1)

            self.assertEqual(
                b"".join([part async for part in first_response.body_iterator]),
                chunk,
            )
            second_response, second_error = await second_task
            self.assertIsNone(second_error)
            self.assertEqual(
                b"".join([part async for part in second_response.body_iterator]),
                chunk,
            )

        run_async(scenario())

        self.assertEqual(capacity.snapshot.active_bytes, 0)
        self.assertEqual(capacity.snapshot.active_items, 0)
        self.assertEqual(capacity.snapshot.waiters, 0)

    def test_streaming_http_error_is_bounded_and_credential_safe(self):
        secret = b"SECRET-UPSTREAM-CREDENTIAL"
        fake_client = _FakeStreamingClient(
            [secret, b"unread-tail"],
            status_code=429,
            headers={"retry-after": "7"},
        )
        capacity = StreamObservationCapacity(max_items=2, max_bytes=64)

        with self.assertLogs(level="WARNING") as logs:
            response_data, error_detail = run_async(
                _make_streaming_request(
                    fake_client,
                    "https://upstream.example/v1/chat/completions",
                    {},
                    {},
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=16,
                )
            )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Upstream request failed with HTTP status 429.",
        )
        self.assertEqual(error_detail.retry_after, 7.0)
        self.assertEqual(error_detail.status_code, 429)
        self.assertFalse(fake_client.response.aread_called)
        self.assertNotIn(secret.decode("ascii"), "\n".join(logs.output))
        self.assertIn("reason=upstream_http_error_truncated", "\n".join(logs.output))
        self.assertEqual(capacity.snapshot.active_bytes, 0)

    def test_streaming_http_error_preserves_safe_context_overflow_signal(self):
        upstream_body = (
            b'{"error":{"code":"context_length_exceeded",'
            b'"message":"maximum context length; PRIVATE-PROMPT"}}'
        )
        fake_client = _FakeStreamingClient([upstream_body], status_code=400)
        capacity = StreamObservationCapacity(max_items=2, max_bytes=256)

        response_data, error_detail = run_async(
            _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=128,
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Upstream request failed with HTTP status 400: context overflow.",
        )
        self.assertNotIn("PRIVATE-PROMPT", error_detail)
        self.assertEqual(capacity.snapshot.active_bytes, 0)


class MakeStreamingRequestResponseHeadersSinkTests(unittest.TestCase):
    def test_streaming_request_captures_headers_before_status_branch_check(self):
        async def scenario():
            capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)

            success_client = _FakeStreamingClient(
                [b'data: {"choices":[{"delta":{"content":"ok"}}]}\n\n'],
                status_code=200,
                headers={"x-ratelimit-remaining-requests": "5"},
            )
            success_sink: dict[str, str] = {}
            response, error_detail = await _make_streaming_request(
                success_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=256,
                response_headers_sink=success_sink,
            )
            self.assertIsNone(error_detail)
            self.assertEqual(success_sink.get("x-ratelimit-remaining-requests"), "5")
            # Drain the body so the stream's capacity lease is released before
            # the next request reuses the same capacity instance.
            b"".join([part async for part in response.body_iterator])

            error_client = _FakeStreamingClient(
                [b"error body"],
                status_code=429,
                headers={"x-ratelimit-remaining-requests": "0", "retry-after": "7"},
            )
            error_sink: dict[str, str] = {}
            response_data, error_detail = await _make_streaming_request(
                error_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
                stream_observation_capacity=capacity,
                stream_event_max_bytes=64,
                response_headers_sink=error_sink,
            )
            self.assertIsNone(response_data)
            self.assertEqual(error_detail.status_code, 429)
            self.assertEqual(error_sink.get("x-ratelimit-remaining-requests"), "0")

        run_async(scenario())


class ComputeRetrySleepTests(unittest.TestCase):
    def test_retry_after_takes_precedence_over_retry_delay(self):
        detail = RequestErrorDetail("slow", retry_after=10.0, status_code=429)

        # With retry_after set, we honor it verbatim regardless of retry_delay.
        self.assertEqual(_compute_retry_sleep_seconds(5.0, detail), 10.0)

    def test_retry_after_clamped_to_max(self):
        detail = RequestErrorDetail("slow", retry_after=9999.0, status_code=429)

        self.assertEqual(_compute_retry_sleep_seconds(5.0, detail), MAX_RETRY_AFTER_SECONDS)

    def test_retry_delay_with_jitter_when_no_retry_after(self):
        detail = RequestErrorDetail("server error", status_code=500)

        results = {_compute_retry_sleep_seconds(10.0, detail) for _ in range(50)}

        # Expect jitter: ±25% around 10s means result stays within [7.5, 12.5].
        self.assertTrue(all(7.5 <= r <= 12.5 for r in results))
        # And jitter actually varies — not all returns should be identical.
        self.assertGreater(len(results), 1)

    def test_zero_retry_delay_and_no_retry_after_returns_zero(self):
        detail = RequestErrorDetail("x", status_code=500)
        self.assertEqual(_compute_retry_sleep_seconds(0.0, detail), 0.0)

    def test_zero_retry_after_falls_through_to_retry_delay(self):
        # Upstream may send Retry-After: 0 (RFC allows it). We must not honor
        # that verbatim — that would stampede retries back at the provider.
        # Instead, fall through to the configured retry_delay + jitter path.
        detail = RequestErrorDetail("rate", retry_after=0.0, status_code=429)
        results = {_compute_retry_sleep_seconds(10.0, detail) for _ in range(50)}
        self.assertTrue(all(7.5 <= r <= 12.5 for r in results))

    def test_retry_delay_at_max_is_clamped_not_disabled(self):
        detail = RequestErrorDetail("server error", status_code=500)
        results = {_compute_retry_sleep_seconds(MAX_RETRY_AFTER_SECONDS, detail) for _ in range(50)}

        self.assertTrue(all(90.0 <= r <= MAX_RETRY_AFTER_SECONDS for r in results))
        self.assertGreater(len(results), 1)

    def test_string_error_without_metadata(self):
        # Plain strings (no RequestErrorDetail) behave as if retry_after is None.
        self.assertIsNone(_extract_retry_after("some error"))


class RetryLoopHonorsRetryAfterTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        install_main_chat_accounting_double(self._accounting_stack)
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        coordinator_patcher = patch(
            "main.ConfigUpdateCoordinator",
            return_value=config_update_coordinator,
        )
        coordinator_patcher.start()
        self.addCleanup(coordinator_patcher.stop)

    @patch("llm_gateway_core.api.v1.chat.asyncio")
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_retry_after_seconds_used_instead_of_retry_delay(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        asyncio_mock,
    ):
        from fastapi.testclient import TestClient

        sleep_mock = AsyncMock()
        asyncio_mock.sleep = sleep_mock
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.providers_config = {
            "provider-a": SimpleNamespace(baseUrl="https://provider-a.example", apikey="KEY"),
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "provider-a",
                        "model": "m-a",
                        "retry_count": 1,
                        # retry_delay deliberately small so we can distinguish it from Retry-After.
                        "retry_delay": 1,
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_operation_rules.return_value = {}
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # First attempt fails with Retry-After=8; second succeeds.
        make_llm_request_mock.side_effect = [
            (None, RequestErrorDetail("too many", retry_after=8.0, status_code=429)),
            (
                {
                    "id": "after-retry",
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                },
                None,
            ),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(make_llm_request_mock.await_count, 2)
        # asyncio.sleep must have been awaited with the upstream-hinted value, not retry_delay.
        sleep_mock.assert_awaited_once()
        slept_seconds = sleep_mock.await_args.args[0]
        self.assertEqual(slept_seconds, 8.0)


if __name__ == "__main__":
    unittest.main()
