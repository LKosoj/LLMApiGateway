"""Integration tests for IP brute-force blocking in the auth middleware."""

import unittest
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from tests.runtime_test_support import bind_app_services


def build_app(guard: IpBlockGuard | None) -> FastAPI:
    app = FastAPI()
    bind_app_services(
        app,
        ip_block_guard=guard,
        rejections_db=MagicMock(spec=RejectionsDB),
    )
    app.add_middleware(ApiKeyAuthMiddleware)

    @app.get("/health")
    async def health():
        return {"status": "ok"}

    @app.head("/health")
    async def health_head():
        return None

    @app.get("/v1/models")
    async def models():
        return {"status": "ok"}

    return app


class IpBlockMiddlewareTests(unittest.TestCase):
    def setUp(self):
        self.patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            "test-gateway-key",
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def _bad(self, client):
        return client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})

    def test_blocks_after_threshold_then_returns_429(self):
        guard = IpBlockGuard(max_failures=3, block_seconds=1200.0)
        app = build_app(guard)
        with TestClient(app) as client:
            # Three invalid keys are still answered with 403 (the third trips the block).
            for _ in range(3):
                self.assertEqual(self._bad(client).status_code, 403)
            # The next request is rejected pre-auth with 429 + Retry-After.
            blocked = client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
            self.assertEqual(blocked.status_code, 429)
            self.assertEqual(blocked.headers.get("Retry-After"), "1200")
            # Even a valid key from the same IP stays blocked.
            valid = client.get("/v1/models", headers={"Authorization": "Bearer test-gateway-key"})
            self.assertEqual(valid.status_code, 429)

    def test_block_trigger_is_audited_as_ip_blocked(self):
        guard = IpBlockGuard(max_failures=2, block_seconds=600.0)
        app = build_app(guard)
        mock_db = app.state.services.rejections_db
        with TestClient(app) as client:
            self._bad(client)
            self._bad(client)  # trips the block
        categories = [c.kwargs["category"] for c in mock_db.insert_rejection.call_args_list]
        self.assertIn("ip_blocked", categories)
        self.assertEqual(categories.count("ip_blocked"), 1)
        ip_blocked_call = next(
            c for c in mock_db.insert_rejection.call_args_list
            if c.kwargs["category"] == "ip_blocked"
        )
        self.assertEqual(ip_blocked_call.kwargs["status_code"], 429)

    def test_blocked_requests_do_not_flood_the_audit(self):
        guard = IpBlockGuard(max_failures=2, block_seconds=600.0)
        app = build_app(guard)
        mock_db = app.state.services.rejections_db
        with TestClient(app) as client:
            self._bad(client)
            self._bad(client)  # trips the block
            for _ in range(5):
                client.get("/v1/models", headers={"Authorization": "Bearer wrong-key"})
        # Subsequent blocked requests are rejected silently (still exactly one block record).
        categories = [c.kwargs["category"] for c in mock_db.insert_rejection.call_args_list]
        self.assertEqual(categories.count("ip_blocked"), 1)

    def test_successful_auth_resets_counter(self):
        guard = IpBlockGuard(max_failures=3, block_seconds=600.0)
        app = build_app(guard)
        with TestClient(app) as client:
            self._bad(client)
            self._bad(client)
            ok = client.get("/v1/models", headers={"Authorization": "Bearer test-gateway-key"})
            self.assertEqual(ok.status_code, 200)
            # Counter was reset by the success: two more failures must not block.
            self.assertEqual(self._bad(client).status_code, 403)
            self.assertEqual(self._bad(client).status_code, 403)
            still_ok = client.get("/v1/models", headers={"Authorization": "Bearer test-gateway-key"})
            self.assertEqual(still_ok.status_code, 200)

    def test_public_paths_are_never_blocked(self):
        guard = IpBlockGuard(max_failures=1, block_seconds=600.0)
        app = build_app(guard)
        with TestClient(app) as client:
            self._bad(client)  # trips the block for protected paths
            self.assertEqual(client.get("/v1/models").status_code, 429)
            # /health does not require a key and must remain reachable.
            self.assertEqual(client.get("/health").status_code, 200)
            self.assertEqual(client.head("/health").status_code, 200)

    def test_no_guard_means_no_blocking(self):
        app = build_app(None)
        with TestClient(app) as client:
            for _ in range(10):
                self.assertEqual(self._bad(client).status_code, 403)


if __name__ == "__main__":
    unittest.main()
