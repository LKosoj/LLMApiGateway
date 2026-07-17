import unittest
from unittest.mock import Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.db.api_keys_db import ApiKeyRecord, ApiKeysDB
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.request_logging import RequestLoggingASGIMiddleware
from llm_gateway_core.middleware.runtime_snapshot import RuntimeAvailabilityMiddleware
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from tests.runtime_test_support import bind_app_services


def build_test_app() -> FastAPI:
    app = FastAPI()
    bind_app_services(app)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.head("/health")
    async def health_head():
        return None

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
        self.assertEqual(self.client.head("/health").status_code, 200)
        self.assertEqual(
            self.client.get(
                "/health",
                headers={"Authorization": "Bearer wrong-key"},
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.head(
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
        self.assertEqual(
            self.client.head(
                "/health",
                headers={"Authorization": "Bearer test-gateway-key"},
            ).status_code,
            200,
        )

    @patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "test-gateway-key")
    def test_typed_dependencies_win_over_conflicting_legacy_aliases(self):
        record = ApiKeyRecord(
            id=17,
            name="typed-key",
            api_key="typed-key",
            budget_usd=10.0,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
        )
        api_keys_db = Mock(spec=ApiKeysDB)
        api_keys_db.get_by_key.return_value = record
        registry = Mock(spec=ActiveRequestsRegistry)
        guard = Mock(spec=IpBlockGuard)
        guard.check_blocked.return_value = None
        app = FastAPI()
        services = bind_app_services(
            app,
            api_keys_db=api_keys_db,
            active_requests_registry=registry,
            ip_block_guard=guard,
        )
        for alias in (
            "api_keys_db",
            "usd_budget_ledger",
            "active_requests_registry",
            "ip_block_guard",
        ):
            setattr(app.state, alias, object())
        app.add_middleware(ApiKeyAuthMiddleware)
        app.add_middleware(RequestLoggingASGIMiddleware)

        @app.post("/v1/embeddings")
        async def embeddings():
            return {"status": "ok"}

        with TestClient(app) as client:
            response = client.post(
                "/v1/embeddings",
                headers={"Authorization": "Bearer typed-key"},
            )

        self.assertEqual(response.status_code, 200)
        api_keys_db.get_by_key.assert_called_once_with("typed-key")
        guard.register_success.assert_called_once()
        registry.start.assert_called_once()
        registry.finish.assert_called_once()
        self.assertEqual(services.usd_budget_ledger.reserved_for(record.id), 0.0)

    def test_health_and_unmatched_routes_do_not_require_services(self):
        app = FastAPI()
        app.add_middleware(ApiKeyAuthMiddleware)

        @app.get("/health")
        async def health():
            return {"status": "ok"}

        with TestClient(app) as client:
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.get("/missing").status_code, 404)

    def test_missing_services_reaches_runtime_availability_boundary(self):
        app = FastAPI()
        app.add_middleware(ApiKeyAuthMiddleware)
        app.add_middleware(RuntimeAvailabilityMiddleware)

        @app.get("/v1/models")
        async def models():
            return {"status": "ok"}

        with TestClient(app) as client:
            response = client.get("/v1/models")

        self.assertEqual(response.status_code, 503)
        payload = response.json()
        self.assertEqual(
            payload["detail"],
            "Gateway runtime is temporarily unavailable.",
        )
        self.assertEqual(payload["error"]["code"], "runtime_unavailable")


if __name__ == "__main__":
    unittest.main()
