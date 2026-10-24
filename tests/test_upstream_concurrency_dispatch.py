"""Dispatch integration coverage for per-upstream-key concurrency limits."""

import asyncio
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat import _attempt_model_fallback_rule
from llm_gateway_core.services.request_handler import _OwnedStreamingResponse, _StreamResourceOwner
from llm_gateway_core.services.upstream_routing_state import (
    UpstreamRoutingState,
    fingerprint_api_key,
)
from tests._async_compat import run_async
from tests.test_chat_dispatch_key_switch import (
    _KeySwitchScenario,
    _successful_response,
    _two_key_groq_provider,
)


def _provider(api_key: str, *, responses: bool = False) -> SimpleNamespace:
    models = {"provider-model": {"api_type": "responses"}} if responses else {}
    return SimpleNamespace(
        baseUrl="https://provider.example",
        apikey=api_key,
        models=models,
        upstream_limits=SimpleNamespace(model_dump=lambda: {"max_concurrency": 1}),
    )


def _attempt(
    state: UpstreamRoutingState,
    provider: SimpleNamespace,
    make_llm_request: AsyncMock,
    *,
    is_streaming: bool = False,
    provider_order: bool = False,
    fallback_events_db: Mock | None = None,
    request_id: str | None = None,
):
    rule = {
        "provider": "provider",
        "model": "provider-model",
        "use_provider_order_as_fallback": provider_order,
    }
    if provider_order:
        rule["providers_order"] = ["sub-provider"]
    request = SimpleNamespace(state=SimpleNamespace(), headers={})
    with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_llm_request):
        return run_async(
            _attempt_model_fallback_rule(
                request,
                Mock(),
                {"provider": provider},
                "gateway-model",
                {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hello"}],
                    **({"stream": True} if is_streaming else {}),
                },
                rule,
                is_streaming,
                proxy_http_clients={},
                upstream_routing_state=state,
                fallback_events_db=fallback_events_db,
                request_id=request_id,
            )
        )


async def _stream_source():
    yield b'data: {"choices": []}\n\n'


async def _upstream_error_stream_source():
    yield (
        b'data: {"choices":[{"delta":{"content":"[Error] The upstream provider"}}],'
        b'"usage":{"completion_tokens":14}}\n\n'
    )


async def _send_stream(response: StreamingResponse, *, fail: bool = False) -> None:
    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        if fail and message["type"] == "http.response.body":
            raise RuntimeError("send failed")

    await response(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "GET",
            "scheme": "http",
            "path": "/",
            "raw_path": b"/",
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )


class UpstreamConcurrencyDispatchTests(unittest.TestCase):
    def test_first_request_at_max_concurrency_one_reaches_upstream(self):
        state = UpstreamRoutingState()
        request = AsyncMock(return_value=(_successful_response(), None))

        response_data, error_detail, _attempt_number = _attempt(
            state, _provider("ONLY-KEY"), request
        )

        self.assertEqual(response_data, _successful_response())
        self.assertIsNone(error_detail)
        request.assert_awaited_once()
        released = state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()

    def test_busy_first_key_skips_to_second_without_an_extra_attempt(self):
        provider = _two_key_groq_provider()
        provider.upstream_limits = SimpleNamespace(model_dump=lambda: {"max_concurrency": 1})

        async def successful_request(*_args, **_kwargs):
            return _successful_response(), None

        with _KeySwitchScenario(
            {"groq": provider},
            [{"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}],
            successful_request,
        ) as scenario:
            lease = scenario.upstream_state.acquire_concurrency(
                "groq", fingerprint_api_key("KEY-A"), max_concurrency=1
            )
            self.assertIsNotNone(lease)
            response = scenario.post_chat()

        lease.release()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 1)
        self.assertEqual(scenario.auth_headers, ["Bearer KEY-B"])

    def test_all_busy_first_model_does_not_spend_budget_before_next_model(self):
        first = _provider("BUSY-KEY")
        backup = SimpleNamespace(baseUrl="https://backup.example", apikey="BACKUP-KEY")

        async def successful_request(*_args, **_kwargs):
            return _successful_response(), None

        with _KeySwitchScenario(
            {"first": first, "backup": backup},
            [
                {"provider": "first", "model": "first-model", "use_provider_order_as_fallback": False},
                {"provider": "backup", "model": "backup-model", "use_provider_order_as_fallback": False},
            ],
            successful_request,
            max_total_attempts=1,
        ) as scenario:
            lease = scenario.upstream_state.acquire_concurrency(
                "first", fingerprint_api_key("BUSY-KEY"), max_concurrency=1
            )
            self.assertIsNotNone(lease)
            response = scenario.post_chat()

        lease.release()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 1)
        self.assertEqual(scenario.make_llm_request.await_args.args[1], "https://backup.example/chat/completions")

    def test_all_busy_key_does_not_record_a_fallback_event(self):
        state = UpstreamRoutingState()
        lease = state.acquire_concurrency(
            "provider", fingerprint_api_key("BUSY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(lease)
        request = AsyncMock(return_value=(_successful_response(), None))
        fallback_events_db = Mock()

        response_data, error_detail, _attempt_number = _attempt(
            state,
            _provider("BUSY-KEY"),
            request,
            fallback_events_db=fallback_events_db,
            request_id="request-id",
        )

        lease.release()
        self.assertIsNone(response_data)
        self.assertIn("shared key max concurrency reached", error_detail)
        request.assert_not_awaited()
        fallback_events_db.insert_event.assert_not_called()

    def test_reservation_race_reselects_for_plain_and_provider_order(self):
        for provider_order in (False, True):
            with self.subTest(provider_order=provider_order):
                state = UpstreamRoutingState()
                request = AsyncMock(return_value=(_successful_response(), None))
                original_acquire = state.acquire_concurrency
                calls = 0
                winning_lease = None

                def race_once(*args, **kwargs):
                    nonlocal calls, winning_lease
                    calls += 1
                    if calls == 1:
                        winning_lease = original_acquire(*args, **kwargs)
                        return None
                    return original_acquire(*args, **kwargs)

                with patch.object(state, "acquire_concurrency", side_effect=race_once):
                    response_data, error_detail, attempt_number = _attempt(
                        state,
                        _provider("KEY-A,KEY-B"),
                        request,
                        provider_order=provider_order,
                    )

                self.assertIsNotNone(winning_lease)
                winning_lease.release()
                self.assertEqual(response_data, _successful_response())
                self.assertIsNone(error_detail)
                request.assert_awaited_once()
                self.assertEqual(calls, 2)
                self.assertEqual(attempt_number, 2)

    def test_nonstream_error_and_cancellation_release_slot_for_both_dispatch_branches(self):
        for provider_order in (False, True):
            for failure in (RuntimeError("upstream failed"), asyncio.CancelledError()):
                with self.subTest(provider_order=provider_order, failure=type(failure).__name__):
                    state = UpstreamRoutingState()
                    request = AsyncMock(side_effect=failure)

                    with self.assertRaises(type(failure)):
                        _attempt(
                            state,
                            _provider("ONLY-KEY"),
                            request,
                            provider_order=provider_order,
                        )

                    released = state.acquire_concurrency(
                        "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
                    )
                    self.assertIsNotNone(released)
                    released.release()

    def test_stream_slot_is_held_until_close_then_released(self):
        state = UpstreamRoutingState()
        request = AsyncMock(return_value=(StreamingResponse(_stream_source()), None))

        with patch(
            "llm_gateway_core.api.v1.chat_dispatch.detect_upstream_provider_error_stream",
            new=AsyncMock(side_effect=lambda response, *_args, **_kwargs: (response, None)),
        ):
            response_data, error_detail, _attempt_number = _attempt(
                state, _provider("ONLY-KEY"), request, is_streaming=True
            )

        self.assertIsNone(error_detail)
        self.assertIsInstance(response_data, StreamingResponse)
        self.assertIsNone(
            state.acquire_concurrency("provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1)
        )
        run_async(response_data.aclose())
        released = state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()

    def test_masked_stream_error_and_responses_postprocess_exception_release_slot(self):
        state = UpstreamRoutingState()
        stream = StreamingResponse(_upstream_error_stream_source())
        request = AsyncMock(return_value=(stream, None))

        _response_data, error_detail, _attempt_number = _attempt(
            state, _provider("ONLY-KEY"), request, is_streaming=True
        )

        self.assertIn("upstream-provider error", error_detail)
        released = state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()

    def test_postprocess_primary_error_survives_owned_stream_close_failure(self):
        async def failing_owner_close():
            raise ValueError("cleanup failure")

        state = UpstreamRoutingState()
        upstream_response = _OwnedStreamingResponse(
            _stream_source(), stream_owner=_StreamResourceOwner(failing_owner_close)
        )
        request = AsyncMock(return_value=(upstream_response, None))

        with patch(
            "llm_gateway_core.api.v1.chat_dispatch.responses_stream_to_chat",
            side_effect=RuntimeError("primary failure"),
        ):
            with self.assertRaisesRegex(RuntimeError, "primary failure"):
                _attempt(
                    state,
                    _provider("ONLY-KEY", responses=True),
                    request,
                    is_streaming=True,
                )

        released = state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()

    def test_masked_stream_close_cancellation_propagates_after_releasing_slot(self):
        async def cancelled_owner_close():
            raise asyncio.CancelledError()

        state = UpstreamRoutingState()
        upstream_response = _OwnedStreamingResponse(
            _upstream_error_stream_source(),
            stream_owner=_StreamResourceOwner(cancelled_owner_close),
        )
        request = AsyncMock(return_value=(upstream_response, None))

        with self.assertRaises(asyncio.CancelledError):
            _attempt(state, _provider("ONLY-KEY"), request, is_streaming=True)

        released = state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()

    def test_asgi_stream_completion_send_failure_and_task_cancellation_release_slot(self):
        async def cancelled_source(started: asyncio.Event):
            started.set()
            yield b'data: {"choices": []}\n\n'
            await asyncio.Event().wait()

        async def failing_source():
            yield b'data: {"choices": []}\n\n'
            raise RuntimeError("source failed")

        for mode in ("complete", "send_failure", "source_failure", "cancel"):
            with self.subTest(mode=mode):
                state = UpstreamRoutingState()
                source = _stream_source()
                if mode == "cancel":
                    started = asyncio.Event()
                    source = cancelled_source(started)
                elif mode == "source_failure":
                    source = failing_source()
                request = AsyncMock(return_value=(StreamingResponse(source), None))
                with patch(
                    "llm_gateway_core.api.v1.chat_dispatch.detect_upstream_provider_error_stream",
                    new=AsyncMock(side_effect=lambda response, *_args, **_kwargs: (response, None)),
                ):
                    response_data, error_detail, _attempt_number = _attempt(
                        state, _provider("ONLY-KEY"), request, is_streaming=True
                    )

                self.assertIsNone(error_detail)
                if mode == "complete":
                    run_async(_send_stream(response_data))
                elif mode == "send_failure":
                    with self.assertRaisesRegex(RuntimeError, "send failed"):
                        run_async(_send_stream(response_data, fail=True))
                elif mode == "source_failure":
                    with self.assertRaisesRegex(RuntimeError, "source failed"):
                        run_async(_send_stream(response_data))
                else:
                    async def cancel_response():
                        task = asyncio.create_task(_send_stream(response_data))
                        await started.wait()
                        task.cancel()
                        with self.assertRaises(asyncio.CancelledError):
                            await task

                    run_async(cancel_response())

                released = state.acquire_concurrency(
                    "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
                )
                self.assertIsNotNone(released)
                released.release()

        postprocess_state = UpstreamRoutingState()
        postprocess_request = AsyncMock(return_value=(StreamingResponse(_stream_source()), None))
        with patch(
            "llm_gateway_core.api.v1.chat_dispatch.responses_stream_to_chat",
            side_effect=RuntimeError("postprocess failed"),
        ):
            with self.assertRaisesRegex(RuntimeError, "postprocess failed"):
                _attempt(
                    postprocess_state,
                    _provider("ONLY-KEY", responses=True),
                    postprocess_request,
                    is_streaming=True,
                )

        released = postprocess_state.acquire_concurrency(
            "provider", fingerprint_api_key("ONLY-KEY"), max_concurrency=1
        )
        self.assertIsNotNone(released)
        released.release()


if __name__ == "__main__":
    unittest.main()
