import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.middleware.auth import (
    SESSION_HMAC_CONFIGURATION_ERROR,
    create_authenticated_session,
    verify_session_hmac,
)


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.include_router(auth_router)
    return app


class AuthHmacEmptySecretTests(unittest.TestCase):
    def test_verify_session_hmac_returns_false_when_runtime_secret_is_empty(self):
        expires_at = int(time.time()) + 60

        with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", ""):
            self.assertFalse(verify_session_hmac("forged-token", 1, expires_at, "nonce"))
            self.assertFalse(verify_session_hmac("", 1, expires_at, "nonce"))

    def test_create_authenticated_session_fails_closed_when_runtime_secret_is_empty(self):
        with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", ""):
            with self.assertRaises(RuntimeError) as context:
                create_authenticated_session()

        self.assertEqual(str(context.exception), SESSION_HMAC_CONFIGURATION_ERROR)

    def test_login_endpoint_returns_configuration_error_when_runtime_secret_is_empty(self):
        client = TestClient(build_test_app())
        try:
            with patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", ""):
                response = client.post("/auth/login", json={"api_key": "any-token"})
        finally:
            client.close()

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json(), {"detail": SESSION_HMAC_CONFIGURATION_ERROR})


if __name__ == "__main__":
    unittest.main()
