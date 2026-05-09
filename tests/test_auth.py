import unittest
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.middleware.auth import api_key_auth


def build_test_app() -> FastAPI:
    app = FastAPI()
    app.middleware("http")(api_key_auth)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.post("/v1/chat/completions")
    async def chat():
        return {"status": "protected-chat"}

    @app.get("/v1/config/providers")
    async def config():
        return {"status": "protected-config"}

    return app


class AuthMiddlewareEndpointTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = build_test_app()
        cls.client = TestClient(cls.app)

    @classmethod
    def tearDownClass(cls):
        cls.client.close()

    @patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key")
    def test_chat_endpoint_auth_matrix(self):
        self.assertEqual(
            self.client.post("/v1/chat/completions", json={"model": "demo"}).status_code,
            401,
        )
        self.assertEqual(
            self.client.post(
                "/v1/chat/completions",
                json={"model": "demo"},
                headers={"Authorization": "Bearer wrong-key"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.post(
                "/v1/chat/completions",
                json={"model": "demo"},
                headers={"Authorization": "Bearer test-gateway-key"},
            ).status_code,
            200,
        )

    @patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key")
    def test_config_endpoint_auth_matrix(self):
        self.assertEqual(self.client.get("/v1/config/providers").status_code, 401)
        self.assertEqual(
            self.client.get(
                "/v1/config/providers",
                headers={"Authorization": "Bearer wrong-key"},
            ).status_code,
            403,
        )
        self.assertEqual(
            self.client.get(
                "/v1/config/providers",
                headers={"Authorization": "Bearer test-gateway-key"},
            ).status_code,
            200,
        )

    @patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key")
    def test_health_endpoint_stays_public_for_all_token_states(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(
            self.client.get(
                "/health",
                headers={"Authorization": "Bearer wrong-key"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.get(
                "/health",
                headers={"Authorization": "Bearer test-gateway-key"},
            ).status_code,
            200,
        )


if __name__ == "__main__":
    unittest.main()
