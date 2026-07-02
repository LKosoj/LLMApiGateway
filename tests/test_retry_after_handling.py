"""Tests for Retry-After header compliance and jitter in retry scheduling."""

import unittest
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import main
from llm_gateway_core.api.v1.chat import (
    _compute_retry_sleep_seconds,
    _extract_retry_after,
)
from llm_gateway_core.services.request_handler import (
    MAX_RETRY_AFTER_SECONDS,
    STREAM_PRIMING_MAX_BYTES,
    RequestErrorDetail,
    _clamped_retry_after,
    _make_json_request,
    _make_streaming_request,
    parse_retry_after_header,
)
from tests._async_compat import run_async


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


class _FakeStreamingResponse:
    status_code = 200
    headers = {}

    def __init__(self, chunks):
        self._chunks = chunks

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk

    async def aread(self):
        return b""


class _FakeStreamContext:
    def __init__(self, response):
        self.response = response
        self.closed = False

    async def __aenter__(self):
        return self.response

    async def __aexit__(self, exc_type, exc, tb):
        self.closed = True


class _FakeStreamingClient:
    def __init__(self, chunks):
        self.context = _FakeStreamContext(_FakeStreamingResponse(chunks))

    def stream(self, *args, **kwargs):
        return self.context


class MakeStreamingRequestTests(unittest.TestCase):
    def test_stream_priming_buffer_is_limited_before_first_content_chunk(self):
        comment_chunk = b": keepalive\n\n"
        oversized_comments = comment_chunk * ((STREAM_PRIMING_MAX_BYTES // len(comment_chunk)) + 1)
        fake_client = _FakeStreamingClient([oversized_comments])

        response_data, error_detail = run_async(
            _make_streaming_request(
                fake_client,
                "https://upstream.example/v1/chat/completions",
                {},
                {},
            )
        )

        self.assertIsNone(response_data)
        self.assertEqual(error_detail, "Stream priming exceeded maximum buffered bytes before content.")
        self.assertTrue(fake_client.context.closed)


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
    @patch("llm_gateway_core.api.v1.chat.asyncio.sleep", new_callable=AsyncMock)
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_retry_after_seconds_used_instead_of_retry_delay(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        sleep_mock,
    ):
        from fastapi.testclient import TestClient

        fake_config_loader = Mock()
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
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # First attempt fails with Retry-After=8; second succeeds.
        make_llm_request_mock.side_effect = [
            (None, RequestErrorDetail("too many", retry_after=8.0, status_code=429)),
            ({"id": "after-retry"}, None),
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
