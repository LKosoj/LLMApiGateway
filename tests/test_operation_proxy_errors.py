import unittest
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi import HTTPException

from tests._async_compat import run_async
from llm_gateway_core.api.v1.operation_proxy import (
    extract_downstream_error_detail,
    proxy_json_raw_to_downstream,
    proxy_json_to_downstream,
    proxy_multipart_raw_to_downstream,
    proxy_multipart_to_downstream,
    sanitize_target_url_for_log,
)


class _FakeDownstreamResponse:
    def __init__(self, status_code: int, text: str):
        self.status_code = status_code
        self.text = text
        self.content = text.encode("utf-8")
        self.headers = {"content-type": "application/json"}

    def json(self):
        return {"error": self.text}


class OperationProxyErrorDisclosureTests(unittest.TestCase):
    def test_downstream_http_error_detail_does_not_echo_response_body(self):
        response = httpx.Response(
            500,
            text='{"error":"SECRET_UPSTREAM_BODY"}',
            request=httpx.Request("POST", "https://provider.example/v1/run"),
        )

        detail = extract_downstream_error_detail(response)

        self.assertEqual(detail, "Downstream request failed with status 500.")
        self.assertNotIn("SECRET_UPSTREAM_BODY", detail)

    def test_downstream_http_error_body_is_not_logged(self):
        response = httpx.Response(
            500,
            text='{"error":"SECRET_UPSTREAM_BODY"}',
            request=httpx.Request("POST", "https://provider.example/v1/run"),
        )

        with patch("llm_gateway_core.api.v1.operation_proxy.logger.warning") as warning_mock:
            extract_downstream_error_detail(response)

        logged = " ".join(str(arg) for call in warning_mock.call_args_list for arg in call.args)
        self.assertNotIn("SECRET_UPSTREAM_BODY", logged)

    def test_sanitized_target_url_for_log_drops_query_and_fragment(self):
        sanitized = sanitize_target_url_for_log("https://provider.example/v1/run?token=SECRET_TOKEN#SECRET")

        self.assertEqual(sanitized, "https://provider.example/v1/run")
        self.assertNotIn("SECRET_TOKEN", sanitized)

    def test_json_proxy_http_error_detail_does_not_echo_response_body(self):
        http_client = Mock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=_FakeDownstreamResponse(500, "SECRET_UPSTREAM_BODY"))

        with self.assertRaises(HTTPException) as exc_info:
            run_async(
                proxy_json_to_downstream(
                    "https://provider.example/v1/run",
                    {"Authorization": "Bearer secret"},
                    {"input": "hello"},
                    http_client,
                )
            )

        self.assertEqual(exc_info.exception.status_code, 503)
        self.assertEqual(exc_info.exception.detail, "Downstream request failed with status 500.")
        self.assertNotIn("SECRET_UPSTREAM_BODY", str(exc_info.exception.detail))

    def test_raw_json_proxy_http_error_detail_does_not_echo_response_body(self):
        http_client = Mock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(return_value=_FakeDownstreamResponse(400, "SECRET_UPSTREAM_BODY"))

        with self.assertRaises(HTTPException) as exc_info:
            run_async(
                proxy_json_raw_to_downstream(
                    "https://provider.example/v1/run",
                    {"Authorization": "Bearer secret"},
                    {"input": "hello"},
                    http_client,
                )
            )

        self.assertEqual(exc_info.exception.status_code, 400)
        self.assertEqual(exc_info.exception.detail, "Downstream request failed with status 400.")
        self.assertNotIn("SECRET_UPSTREAM_BODY", str(exc_info.exception.detail))

    def test_network_errors_use_generic_client_detail(self):
        request = httpx.Request("POST", "https://provider.example/v1/run?token=SECRET_TOKEN")

        cases = (
            (
                "json",
                lambda client: proxy_json_to_downstream(
                    "https://provider.example/v1/run?token=SECRET_TOKEN",
                    {"Authorization": "Bearer secret"},
                    {"input": "hello"},
                    client,
                ),
            ),
            (
                "raw_json",
                lambda client: proxy_json_raw_to_downstream(
                    "https://provider.example/v1/run?token=SECRET_TOKEN",
                    {"Authorization": "Bearer secret"},
                    {"input": "hello"},
                    client,
                ),
            ),
            (
                "multipart",
                lambda client: proxy_multipart_to_downstream(
                    "https://provider.example/v1/run?token=SECRET_TOKEN",
                    {"Authorization": "Bearer secret"},
                    {"field": "value"},
                    [],
                    client,
                ),
            ),
            (
                "raw_multipart",
                lambda client: proxy_multipart_raw_to_downstream(
                    "https://provider.example/v1/run?token=SECRET_TOKEN",
                    {"Authorization": "Bearer secret"},
                    {"field": "value"},
                    [],
                    client,
                ),
            ),
        )

        for name, call_proxy in cases:
            with self.subTest(name=name):
                http_client = Mock(spec=httpx.AsyncClient)
                http_client.post = AsyncMock(
                    side_effect=httpx.ConnectError("provider failed with SECRET_TOKEN", request=request)
                )

                with self.assertRaises(HTTPException) as exc_info:
                    run_async(call_proxy(http_client))

                self.assertEqual(exc_info.exception.status_code, 503)
                self.assertEqual(exc_info.exception.detail, "Downstream request failed.")
                self.assertNotIn("SECRET_TOKEN", str(exc_info.exception.detail))

    def test_network_error_log_does_not_echo_raw_exception_text_or_url_query(self):
        request = httpx.Request("POST", "https://provider.example/v1/run?token=SECRET_TOKEN")
        http_client = Mock(spec=httpx.AsyncClient)
        http_client.post = AsyncMock(
            side_effect=httpx.ConnectError("provider failed with SECRET_TOKEN", request=request)
        )

        with patch("llm_gateway_core.api.v1.operation_proxy.logger.warning") as warning_mock:
            with self.assertRaises(HTTPException):
                run_async(
                    proxy_json_to_downstream(
                        "https://provider.example/v1/run?token=SECRET_TOKEN",
                        {"Authorization": "Bearer secret"},
                        {"input": "hello"},
                        http_client,
                    )
                )

        logged = " ".join(str(arg) for call in warning_mock.call_args_list for arg in call.args)
        self.assertNotIn("SECRET_TOKEN", logged)
        self.assertTrue(warning_mock.call_args_list)
        self.assertTrue(all(call.kwargs.get("exc_info") is not True for call in warning_mock.call_args_list))


if __name__ == "__main__":
    unittest.main()
