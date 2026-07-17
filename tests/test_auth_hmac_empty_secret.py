import importlib
import time
import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.middleware import auth as auth_module
from llm_gateway_core.middleware.auth import (
    SESSION_HMAC_CONFIGURATION_ERROR,
    ApiKeyAuthMiddleware,
    create_authenticated_session,
    verify_session_hmac,
)
from tests.runtime_test_support import bind_app_services


def build_test_app() -> FastAPI:
    app = FastAPI()
    bind_app_services(app)
    app.add_middleware(ApiKeyAuthMiddleware)
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
        payload = response.json()
        self.assertEqual(payload["detail"], SESSION_HMAC_CONFIGURATION_ERROR)
        self.assertEqual(payload["error"]["message"], SESSION_HMAC_CONFIGURATION_ERROR)
        self.assertEqual(payload["error"]["code"], "auth_unavailable")
        self.assertNotIn("set-cookie", response.headers)


class SessionBehaviorTests(unittest.TestCase):
    def test_session_ttl_is_one_year(self):
        self.assertEqual(auth_module.SESSION_TTL_SECONDS, 365 * 24 * 60 * 60)

    @patch(
        "llm_gateway_core.middleware.auth.settings.gateway_api_key",
        "remediation-test-gateway-key",
    )
    def test_session_signature_survives_module_reload(self):
        # A restart (or, here, a module reload standing in for one) must not
        # invalidate sessions issued before it: the signing secret is persisted
        # in the state directory rather than regenerated per process.
        session_id = auth_module.create_authenticated_session(role=auth_module.ROLE_MASTER)
        self.assertTrue(auth_module._has_valid_session(session_id))

        reloaded = importlib.reload(auth_module)

        self.assertTrue(reloaded._has_valid_session(session_id))


if __name__ == "__main__":
    unittest.main()
