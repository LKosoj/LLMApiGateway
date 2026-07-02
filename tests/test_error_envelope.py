import unittest

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from llm_gateway_core.api.error_envelope import (
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


class HttpExceptionHandlerIntegrationTests(unittest.TestCase):
    def test_http_exception_handler_adds_envelope_without_changing_detail(self):
        import main

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


if __name__ == "__main__":
    unittest.main()
