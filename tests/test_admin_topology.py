"""Tests for the /v1/topology endpoint.

Test plan:
1. test_topology_master_sees_all_providers
2. test_topology_user_sees_only_own_active_requests
3. test_topology_nodes_contain_gateway_central
4. test_topology_edges_connect_gateway_to_each_provider
5. test_topology_health_reflects_routing_state
6. test_topology_ttl_cache_hits
7. test_topology_anonymous_401
8. test_topology_html_contains_topology_tab
"""
from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_topology import router as topology_router
from llm_gateway_core.middleware.auth import ROLE_USER, api_key_auth
from llm_gateway_core.services.active_requests import (
    ACTIVE_REQUESTS_STATE_KEY,
    ActiveRequestsRegistry,
)
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
import llm_gateway_core.api.v1.admin_topology as topo_module

MASTER_KEY = "test-topology-master"


def _make_provider_details(name: str):  # noqa: ARG001
    return SimpleNamespace(baseUrl="https://fake.example.com", apikey="sk-fake")


def _build_app(
    *,
    provider_names: list[str] | None = None,
    upstream_state: UpstreamRoutingState | None = None,
    registry: ActiveRequestsRegistry | None = None,
) -> FastAPI:
    app = FastAPI()
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(topology_router, prefix="/v1")

    config_loader = MagicMock()
    names = provider_names or ["openai", "anthropic"]
    config_loader.providers_config = {n: _make_provider_details(n) for n in names}
    app.state.config_loader = config_loader

    app.state.upstream_routing_state = upstream_state or UpstreamRoutingState()

    reg = registry or ActiveRequestsRegistry()
    setattr(app.state, ACTIVE_REQUESTS_STATE_KEY, reg)

    return app


class TopologyEndpointTests(unittest.TestCase):
    def setUp(self) -> None:
        self.key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", MASTER_KEY),
        ]
        for p in self.key_patchers:
            p.start()
        # Clear the module-level cache before each test to avoid cross-test contamination.
        topo_module._topology_cache.invalidate()

    def tearDown(self) -> None:
        for p in self.key_patchers:
            p.stop()
        topo_module._topology_cache.invalidate()

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {MASTER_KEY}"}

    def _get_topology(self, client: TestClient, headers: dict) -> dict:
        resp = client.get("/v1/topology", headers=headers)
        self.assertEqual(resp.status_code, 200, resp.text)
        return resp.json()

    # ── tests ─────────────────────────────────────────────────────────────────

    def test_topology_master_sees_all_providers(self):
        """Master gets a node for every configured provider."""
        provider_names = ["openai", "anthropic", "openrouter"]
        app = _build_app(provider_names=provider_names)

        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        provider_node_ids = {
            n["id"] for n in data["nodes"] if n.get("type") == "provider"
        }
        for name in provider_names:
            self.assertIn(f"provider:{name}", provider_node_ids)

    def test_topology_user_sees_only_own_active_requests(self):
        """Virtual key active_requests count includes only that key's requests."""
        user_key_id = 42
        reg = ActiveRequestsRegistry()
        # User's request on openai
        reg.start(
            request_id="req-user-1",
            path="/v1/chat/completions",
            api_key_id=user_key_id,
        )
        reg.update("req-user-1", provider="openai", model="gpt-4o")
        # Another user's request on anthropic
        reg.start(
            request_id="req-other-1",
            path="/v1/chat/completions",
            api_key_id=999,
        )
        reg.update("req-other-1", provider="anthropic", model="claude-3")

        # Build a fresh app with fake auth middleware to bypass real DB lookup.
        from fastapi import Request

        async def fake_auth(request: Request, call_next):
            request.state.api_key_role = ROLE_USER
            request.state.api_key_id = user_key_id
            return await call_next(request)

        # Replace the app middleware stack by building a fresh app that uses our fake auth.
        app2 = FastAPI()
        app2.middleware("http")(fake_auth)
        app2.include_router(topology_router, prefix="/v1")
        config_loader = MagicMock()
        config_loader.providers_config = {
            "openai": _make_provider_details("openai"),
            "anthropic": _make_provider_details("anthropic"),
        }
        app2.state.config_loader = config_loader
        app2.state.upstream_routing_state = UpstreamRoutingState()
        setattr(app2.state, ACTIVE_REQUESTS_STATE_KEY, reg)

        with TestClient(app2) as client:
            resp = client.get("/v1/topology")

        self.assertEqual(resp.status_code, 200, resp.text)
        data = resp.json()
        active_by_provider = data["active_count_by_provider"]
        # Only user's own request (openai) should be counted
        self.assertEqual(active_by_provider.get("openai", 0), 1)
        self.assertEqual(active_by_provider.get("anthropic", 0), 0)

    def test_topology_nodes_contain_gateway_central(self):
        """Response always includes a central gateway node."""
        app = _build_app()
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        gateway_nodes = [n for n in data["nodes"] if n["id"] == "gateway"]
        self.assertEqual(len(gateway_nodes), 1)
        self.assertEqual(gateway_nodes[0]["type"], "central")

    def test_topology_edges_connect_gateway_to_each_provider(self):
        """Every provider node has an edge from gateway."""
        provider_names = ["openai", "anthropic"]
        app = _build_app(provider_names=provider_names)
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        edge_targets = {e["target"] for e in data["edges"]}
        for name in provider_names:
            self.assertIn(f"provider:{name}", edge_targets)
        edge_sources = {e["source"] for e in data["edges"]}
        self.assertEqual(edge_sources, {"gateway"})

    def test_topology_health_reflects_routing_state(self):
        """If upstream_routing_state has error for a provider, topology reflects it."""
        upstream = UpstreamRoutingState()
        upstream.mark_health("badprovider", "model-x", "fp1", "error", "HTTP 500")

        app = _build_app(
            provider_names=["badprovider", "goodprovider"],
            upstream_state=upstream,
        )
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        health_by_provider = data["health_by_provider"]
        self.assertEqual(health_by_provider.get("badprovider"), "error")
        # goodprovider has no state → ok
        self.assertEqual(health_by_provider.get("goodprovider", "ok"), "ok")

    def test_topology_ttl_cache_hits(self):
        """Second call within TTL returns the same cached value."""
        app = _build_app(provider_names=["openai"])

        # Count how many times _build() (the producer) runs.
        build_calls: list[int] = []
        original_get_or_compute = topo_module._topology_cache.get_or_compute

        async def counting_get_or_compute(key, producer):
            async def counted_producer():
                build_calls.append(1)
                return await producer()
            return await original_get_or_compute(key, counted_producer)

        with patch.object(topo_module._topology_cache, "get_or_compute", counting_get_or_compute):
            with TestClient(app) as client:
                r1 = client.get("/v1/topology", headers=self._master_headers())
                r2 = client.get("/v1/topology", headers=self._master_headers())

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Producer should only have been called once (cache hit on second request).
        self.assertEqual(len(build_calls), 1)

    def test_topology_anonymous_401(self):
        """Unauthenticated request returns 401."""
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/v1/topology")
        self.assertEqual(resp.status_code, 401)

    def test_topology_html_contains_topology_tab(self):
        """The usage-stats HTML page contains data-tab='topology'."""
        from llm_gateway_core.config.paths import STATIC_DIR

        html_path = STATIC_DIR / "usage-stats.html"
        self.assertTrue(html_path.exists(), f"HTML not found at {html_path}")
        content = html_path.read_text(encoding="utf-8")
        self.assertIn('data-tab="topology"', content)
