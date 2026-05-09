import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from llm_gateway_core.api.v1.chat import _attempt_model_fallback_rule
from llm_gateway_core.services.request_handler import (
    _extract_stream_error_detail,
    _make_json_request,
    _parse_stream_chunk_json,
)
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

        chunk_json = _parse_stream_chunk_json(chunk)

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
            app=SimpleNamespace(state=SimpleNamespace(proxy_http_clients={})),
            state=SimpleNamespace(),
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
                )
            )

        self.assertIsNone(response_data)
        self.assertEqual(
            error_detail,
            "Model provider-model failed with provider 'test-provider': rate limit exceeded",
        )
        self.assertEqual(attempt_number, 2)
        classify_error_mock.assert_called_once_with("rate limit exceeded")
        event_kwargs = fallback_events_db.insert_event.call_args.kwargs
        self.assertFalse(event_kwargs["success"])
        self.assertEqual(event_kwargs["error_type"], "unknown")
        self.assertEqual(event_kwargs["error_message"], "rate limit exceeded")


if __name__ == "__main__":
    unittest.main()
