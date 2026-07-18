import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.error_envelope import (
    StructuredHTTPException,
    build_error_payload,
    error_type_for_status,
    message_from_detail,
)


class ErrorEnvelopeTests(unittest.TestCase):
    def test_build_error_payload_preserves_string_detail_and_adds_request_id(self):
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("unused")

        with TestClient(app) as client:
            request = client.build_request("GET", "/boom")
            scope = {
                "type": "http",
                "method": "GET",
                "path": "/boom",
                "headers": [],
                "query_string": b"",
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
            from starlette.requests import Request

            starlette_request = Request(scope)
            starlette_request.state.llmgateway_request_id = "req-123"
            payload = build_error_payload(
                starlette_request,
                status_code=500,
                detail="Internal server error",
            )

        self.assertEqual(payload["detail"], "Internal server error")
        self.assertEqual(payload["error"]["message"], "Internal server error")
        self.assertEqual(payload["error"]["type"], "internal_error")
        self.assertEqual(payload["request_id"], "req-123")
        self.assertEqual(payload["error"]["request_id"], "req-123")
        self.assertEqual(request.url.path, "/boom")

    def test_message_from_detail_uses_dict_message_without_changing_detail(self):
        detail = {"message": "bad input", "field": "model"}

        self.assertEqual(message_from_detail(detail), "bad input")

    def test_status_code_type_mapping(self):
        self.assertEqual(error_type_for_status(401), "authentication_error")
        self.assertEqual(error_type_for_status(403), "permission_error")
        self.assertEqual(error_type_for_status(429), "rate_limit_error")
        self.assertEqual(error_type_for_status(400), "invalid_request_error")
        self.assertEqual(error_type_for_status(503), "internal_error")
        self.assertEqual(error_type_for_status(302), "http_error")

    def test_build_error_payload_without_extra_is_unchanged(self):
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("unused")

        with TestClient(app):
            from starlette.requests import Request

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/boom",
                "headers": [],
                "query_string": b"",
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
            starlette_request = Request(scope)
            starlette_request.state.llmgateway_request_id = "req-no-extra"

            payload = build_error_payload(
                starlette_request,
                status_code=503,
                detail="All configured providers failed.",
            )

        self.assertEqual(
            payload,
            {
                "detail": "All configured providers failed.",
                "error": {
                    "message": "All configured providers failed.",
                    "type": "internal_error",
                    "code": "http_503",
                    "request_id": "req-no-extra",
                },
                "request_id": "req-no-extra",
            },
        )

    def test_build_error_payload_merges_extra_without_overwriting_reserved_keys(self):
        app = FastAPI()

        @app.get("/boom")
        async def boom():
            raise RuntimeError("unused")

        with TestClient(app):
            from starlette.requests import Request

            scope = {
                "type": "http",
                "method": "GET",
                "path": "/boom",
                "headers": [],
                "query_string": b"",
                "client": ("testclient", 50000),
                "server": ("testserver", 80),
                "scheme": "http",
                "app": app,
            }
            starlette_request = Request(scope)
            starlette_request.state.llmgateway_request_id = "req-with-extra"

            payload = build_error_payload(
                starlette_request,
                status_code=503,
                detail="All configured providers failed.",
                extra={
                    "attempts": [{"provider": "p", "model": "m"}],
                    "retry_after_seconds": 42,
                    "detail": "attacker-controlled detail",
                    "error": {"hacked": True},
                    "request_id": "attacker-controlled-id",
                },
            )

        self.assertEqual(payload["detail"], "All configured providers failed.")
        self.assertEqual(payload["error"]["message"], "All configured providers failed.")
        self.assertNotIn("hacked", payload["error"])
        self.assertEqual(payload["request_id"], "req-with-extra")
        self.assertEqual(payload["attempts"], [{"provider": "p", "model": "m"}])
        self.assertEqual(payload["retry_after_seconds"], 42)


class HttpExceptionHandlerIntegrationTests(unittest.TestCase):
    def test_http_exception_handler_adds_envelope_without_changing_detail(self):
        app = FastAPI()

        @app.get("/bad")
        async def bad():
            raise HTTPException(status_code=400, detail="bad request", headers={"X-Test": "kept"})

        app.add_exception_handler(HTTPException, main.http_exception_handler)
        app.middleware("http")(main.log_middleware_functional)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/bad")

        body = response.json()
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.headers["X-Test"], "kept")
        self.assertEqual(body["detail"], "bad request")
        self.assertEqual(body["error"]["message"], "bad request")
        self.assertEqual(body["error"]["type"], "invalid_request_error")
        self.assertEqual(body["error"]["code"], "http_400")
        self.assertTrue(body["request_id"])

    def test_http_exception_handler_merges_structured_extra_payload_and_retry_after_header(self):
        app = FastAPI()

        @app.get("/exhausted")
        async def exhausted():
            raise StructuredHTTPException(
                503,
                "All configured providers failed for model 'gateway-model'.",
                extra_payload={
                    "attempts": [{"provider": "p1", "model": "m1", "key": "key1"}],
                    "retry_after_seconds": 42,
                },
                headers={"Retry-After": "42"},
            )

        app.add_exception_handler(HTTPException, main.http_exception_handler)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/exhausted")

        body = response.json()
        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "42")
        self.assertEqual(
            body["detail"], "All configured providers failed for model 'gateway-model'."
        )
        self.assertEqual(body["attempts"], [{"provider": "p1", "model": "m1", "key": "key1"}])
        self.assertEqual(body["retry_after_seconds"], 42)

    def test_http_exception_handler_plain_http_exception_has_no_extra_fields(self):
        app = FastAPI()

        @app.get("/bad")
        async def bad():
            raise HTTPException(status_code=400, detail="bad request")

        app.add_exception_handler(HTTPException, main.http_exception_handler)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/bad")

        body = response.json()
        self.assertEqual(set(body.keys()) - {"request_id"}, {"detail", "error"})

    def test_http_exception_handler_propagates_code_from_dict_detail(self):
        app = FastAPI()

        @app.get("/no-capable-model")
        async def no_capable_model():
            raise HTTPException(
                status_code=422,
                detail={
                    "message": "No fallback candidate for gateway model 'x' supports this request.",
                    "code": "no_capable_model",
                    "gateway_model": "x",
                    "candidates": [],
                },
            )

        app.add_exception_handler(HTTPException, main.http_exception_handler)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/no-capable-model")

        body = response.json()
        self.assertEqual(response.status_code, 422)
        self.assertEqual(body["error"]["code"], "no_capable_model")
        self.assertEqual(
            body["error"]["message"],
            "No fallback candidate for gateway model 'x' supports this request.",
        )
        self.assertEqual(body["detail"]["code"], "no_capable_model")

    def test_http_exception_handler_string_detail_keeps_default_status_code(self):
        app = FastAPI()

        @app.get("/bad")
        async def bad():
            raise HTTPException(status_code=400, detail="bad request")

        app.add_exception_handler(HTTPException, main.http_exception_handler)

        with TestClient(app, raise_server_exceptions=False) as client:
            response = client.get("/bad")

        body = response.json()
        self.assertEqual(body["error"]["code"], "http_400")


if __name__ == "__main__":
    unittest.main()
