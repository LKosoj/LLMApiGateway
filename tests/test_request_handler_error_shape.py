import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from llm_gateway_core.api.v1.chat import _attempt_model_fallback_rule
from llm_gateway_core.services.request_handler import (
    LocalStreamObservationError,
    RequestErrorDetail,
    _extract_stream_error_detail,
    _make_json_request,
    _parse_stream_chunk_json,
)
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async


class _JsonResponse:
    def __init__(self, payload: dict, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.text = str(payload)

    def json(self):
        return self._payload


class RequestHandlerErrorShapeTests(unittest.TestCase):
    def test_anthropic_sse_error_event_is_parsed_as_stream_error(self):
        chunk = (
            'event: error\n'
            'data: {"type":"error","error":{"message":"overloaded"}}'
        )

        chunk_json = _parse_stream_chunk_json(chunk, max_event_bytes=1024)

        self.assertEqual(
            _extract_stream_error_detail(chunk_json),
            "overloaded",
        )

    def test_make_json_request_preserves_dict_error_message(self):
        fake_client = SimpleNamespace(
            post=AsyncMock(return_value=_JsonResponse({"error": {"message": "bad request"}}))
        )

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "bad request")

    def test_streaming_fallback_forwards_explicit_process_capacity_and_event_limit(self):
        fake_request = SimpleNamespace(state=SimpleNamespace(), headers={})
        capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
        response = object()
        make_request = AsyncMock(return_value=(response, None))

        with patch(
            "llm_gateway_core.api.v1.chat.make_llm_request",
            new=make_request,
        ):
            response_data, error_detail, _attempt_number = run_async(
                _attempt_model_fallback_rule(
                    fake_request,
                    Mock(),
                    {
                        "test-provider": SimpleNamespace(
                            baseUrl="https://upstream.example",
                            apikey="DIRECT-KEY",
                        )
                    },
                    "gateway-model",
                    {
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    },
                    True,
                    proxy_http_clients={},
                    upstream_routing_state=UpstreamRoutingState(),
                    request_id="request-id",
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=512,
                )
            )

        self.assertIs(response_data, response)
        self.assertIsNone(error_detail)
        self.assertIs(
            make_request.await_args.kwargs["stream_observation_capacity"],
            capacity,
        )
        self.assertEqual(
            make_request.await_args.kwargs["stream_event_max_bytes"],
            512,
        )
        self.assertEqual(
            make_request.await_args.kwargs["stream_request_id"],
            "request-id",
        )

    def test_local_stream_failure_bypasses_retry_and_provider_failure_tracking(self):
        fake_request = SimpleNamespace(state=SimpleNamespace(), headers={})
        capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
        make_request = AsyncMock(
            side_effect=LocalStreamObservationError("decode_capacity_exhausted")
        )
        fallback_events_db = Mock()
        routing_state = UpstreamRoutingState()

        with (
            patch(
                "llm_gateway_core.api.v1.chat.make_llm_request",
                new=make_request,
            ),
            patch.object(
                routing_state,
                "record_failure",
                wraps=routing_state.record_failure,
            ) as record_failure,
            self.assertRaises(LocalStreamObservationError),
        ):
            run_async(
                _attempt_model_fallback_rule(
                    fake_request,
                    Mock(),
                    {
                        "test-provider": SimpleNamespace(
                            baseUrl="https://upstream.example",
                            apikey="DIRECT-KEY",
                        )
                    },
                    "gateway-model",
                    {
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "retry_count": 3,
                        "use_provider_order_as_fallback": False,
                    },
                    True,
                    fallback_events_db=fallback_events_db,
                    request_id="request-id",
                    proxy_http_clients={},
                    upstream_routing_state=routing_state,
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=512,
                )
            )

        make_request.assert_awaited_once()
        fallback_events_db.insert_event.assert_not_called()
        record_failure.assert_not_called()

    def test_safe_stream_rate_limit_still_updates_routing_and_event_type(self):
        fake_request = SimpleNamespace(state=SimpleNamespace(), headers={})
        capacity = StreamObservationCapacity(max_items=2, max_bytes=1024)
        error_detail = RequestErrorDetail(
            "Upstream stream reported HTTP status 429.",
            status_code=429,
        )
        make_request = AsyncMock(return_value=(None, error_detail))
        fallback_events_db = Mock()
        routing_state = UpstreamRoutingState()

        with patch(
            "llm_gateway_core.api.v1.chat.make_llm_request",
            new=make_request,
        ):
            response, returned_error, _attempt_number = run_async(
                _attempt_model_fallback_rule(
                    fake_request,
                    Mock(),
                    {
                        "test-provider": SimpleNamespace(
                            baseUrl="https://upstream.example",
                            apikey="DIRECT-KEY",
                        )
                    },
                    "gateway-model",
                    {
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    },
                    True,
                    fallback_events_db=fallback_events_db,
                    request_id="request-id",
                    proxy_http_clients={},
                    upstream_routing_state=routing_state,
                    stream_observation_capacity=capacity,
                    stream_event_max_bytes=512,
                )
            )

        self.assertIsNone(response)
        self.assertIn("HTTP status 429", returned_error)
        self.assertGreater(
            routing_state.get_status_rows()[0]["cooldown_remaining_seconds"],
            0,
        )
        self.assertEqual(
            fallback_events_db.insert_event.call_args.kwargs["error_type"],
            "http_429",
        )

    def test_make_json_request_uses_string_error_value_as_error_detail(self):
        fake_client = SimpleNamespace(
            post=AsyncMock(return_value=_JsonResponse({"error": "rate limit exceeded"}))
        )

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "rate limit exceeded")

    def test_make_json_request_uses_repr_for_unknown_error_shape(self):
        fake_client = SimpleNamespace(
            post=AsyncMock(return_value=_JsonResponse({"error": ["rate", "limit"]}))
        )

        response_data, error_detail = run_async(
            _make_json_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "['rate', 'limit']")

    def test_string_error_shape_reaches_error_classifier_unchanged(self):
        fake_request = SimpleNamespace(
            state=SimpleNamespace(),
            headers={},
        )
        fake_client = SimpleNamespace(
            post=AsyncMock(return_value=_JsonResponse({"error": "rate limit exceeded"}))
        )
        fallback_events_db = Mock()
        fallback_events_db.insert_event = Mock()

        with patch(
            "llm_gateway_core.api.v1.chat.classify_error",
            return_value="unknown",
        ) as classify_error_mock:
            response_data, error_detail, attempt_number = run_async(
                _attempt_model_fallback_rule(
                    fake_request,
                    fake_client,
                    {
                        "test-provider": SimpleNamespace(
                            baseUrl="https://upstream.example",
                            apikey="DIRECT-KEY",
                        )
                    },
                    "gateway-model",
                    {
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    },
                    False,
                    fallback_events_db=fallback_events_db,
                    request_id="request-id",
                    proxy_http_clients={},
                    upstream_routing_state=UpstreamRoutingState(),
                )
            )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Model provider-model failed with provider 'test-provider': rate limit exceeded",
        )
        self.assertEqual(attempt_number, 2)
        self.assertEqual(
            fake_client.post.await_args.kwargs["headers"]["Authorization"],
            "Bearer DIRECT-KEY",
        )
        classify_error_mock.assert_called_once_with("rate limit exceeded")
        event_kwargs = fallback_events_db.insert_event.call_args.kwargs
        self.assertFalse(event_kwargs["success"])
        self.assertEqual(event_kwargs["error_type"], "unknown")
        self.assertEqual(event_kwargs["error_message"], "rate limit exceeded")


if __name__ == "__main__":
    unittest.main()
