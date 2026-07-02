import unittest
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

import main
from tests._async_compat import run_async
from llm_gateway_core.middleware.content_size import ContentSizeLimitMiddleware


def build_gateway_middleware_test_app(
    *,
    cors_allow_origins: list[str] | None = None,
    max_request_body_bytes: int = 1024,
) -> FastAPI:
    app = FastAPI()

    @app.post("/v1/chat/completions")
    async def chat(request: Request):
        return {"bytes": len(await request.body())}

    main.configure_gateway_middleware(
        app,
        cors_allow_origins=cors_allow_origins,
        max_request_body_bytes=max_request_body_bytes,
    )
    return app


class MiddlewareOrderAndBodyLimitTests(unittest.TestCase):
    def test_cors_preflight_is_handled_before_auth(self):
        app = build_gateway_middleware_test_app(cors_allow_origins=["https://app.example"])

        with TestClient(app) as client:
            response = client.options(
                "/v1/chat/completions",
                headers={
                    "Origin": "https://app.example",
                    "Access-Control-Request-Method": "POST",
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["access-control-allow-origin"], "https://app.example")

    def test_unauthorized_chat_request_is_rejected_before_chat_logging_reads_body(self):
        calls = []
        original_chat_logging = main.log_chat_completions

        async def fail_if_called(request, call_next):
            calls.append(request.url.path)
            raise AssertionError("chat logging must not run before auth rejects the request")

        main.log_chat_completions = fail_if_called
        try:
            app = build_gateway_middleware_test_app(max_request_body_bytes=1024)
        finally:
            main.log_chat_completions = original_chat_logging

        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", content=b'{"model":"demo"}')

        self.assertEqual(response.status_code, 401)
        self.assertEqual(calls, [])

    def test_auth_rejection_keeps_request_id_header(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=1024)

        with TestClient(app) as client:
            response = client.post("/v1/chat/completions", content=b'{"model":"demo"}')

        self.assertEqual(response.status_code, 401)
        self.assertTrue(response.headers.get("X-Request-ID"))

    def test_content_length_over_limit_returns_413_before_body_read(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=4)

        with TestClient(app) as client:
            response = client.post(
                "/v1/chat/completions",
                content=b"12345",
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json(), {"detail": "Request body too large"})

    def test_chunked_body_without_content_length_is_counted(self):
        body_messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": True},
            {"type": "http.request", "body": b"6", "more_body": False},
        ]
        response = run_async(_run_limited_asgi_request(body_messages, max_body_size=5))

        self.assertEqual(response["status"], 413)
        self.assertEqual(response["body"], b'{"detail": "Request body too large"}')

    def test_full_stack_chunked_body_without_content_length_returns_413(self):
        app = build_gateway_middleware_test_app(max_request_body_bytes=5)
        body_messages = [
            {"type": "http.request", "body": b"123", "more_body": True},
            {"type": "http.request", "body": b"45", "more_body": True},
            {"type": "http.request", "body": b"6", "more_body": False},
        ]

        with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key"):
            response = run_async(
                _run_asgi_request(
                    app,
                    body_messages,
                    headers=[(b"authorization", b"Bearer test-gateway-key")],
                )
            )

        self.assertEqual(response["status"], 413)
        self.assertEqual(response["body"], b'{"detail": "Request body too large"}')

    def test_global_exception_response_is_generic_and_includes_request_id(self):
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("SECRET_BACKEND_TOKEN")

        app.add_exception_handler(Exception, main.global_exception_handler)
        app.middleware("http")(main.log_middleware_functional)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/boom")

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Internal server error")
        self.assertTrue(response.json()["request_id"])
        self.assertNotIn("SECRET_BACKEND_TOKEN", response.text)


async def _run_limited_asgi_request(body_messages, *, max_body_size: int):
    async def inner_app(scope, receive, send):
        while True:
            message = await receive()
            if not message.get("more_body", False):
                break
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    app = ContentSizeLimitMiddleware(inner_app, max_body_size=max_body_size)
    messages = list(body_messages)
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "method": "POST",
            "path": "/v1/chat/completions",
            "headers": [],
        },
        receive,
        send,
    )
    return {
        "status": sent[0]["status"],
        "body": b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body"),
    }


async def _run_asgi_request(app, body_messages, *, headers):
    messages = list(body_messages)
    sent = []

    async def receive():
        return messages.pop(0)

    async def send(message):
        sent.append(message)

    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": headers,
            "client": ("testclient", 50000),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    return {
        "status": sent[0]["status"],
        "body": b"".join(message.get("body", b"") for message in sent if message["type"] == "http.response.body"),
    }
