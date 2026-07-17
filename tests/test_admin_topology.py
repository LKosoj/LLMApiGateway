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

import json
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_topology import (
    get_topology,
    router as topology_router,
)
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware, ROLE_MASTER, ROLE_USER
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async
from tests.runtime_test_support import (
    installed_runtime,
    make_app_services,
    make_runtime_snapshot,
)
import llm_gateway_core.api.v1.admin_topology as topo_module

MASTER_KEY = "test-topology-master"


def _make_provider_details(name: str):  # noqa: ARG001
    return SimpleNamespace(baseUrl="https://fake.example.com", apikey="sk-fake")


def _make_config_loader(
    provider_names: list[str] | None = None,
    fallback_rules: dict | None = None,
) -> ConfigLoader:
    loader = ConfigLoader()
    names = provider_names or ["openai", "anthropic"]
    loader.providers_config = {n: _make_provider_details(n) for n in names}
    loader.fallback_rules = fallback_rules or {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    loader._fallback_rules_base = {}
    return loader


def _build_app(
    *,
    provider_names: list[str] | None = None,
    fallback_rules: dict | None = None,
    upstream_state: UpstreamRoutingState | None = None,
    registry: ActiveRequestsRegistry | None = None,
) -> FastAPI:
    config_loader = _make_config_loader(provider_names, fallback_rules)
    routing_state = upstream_state or UpstreamRoutingState()
    reg = registry or ActiveRequestsRegistry()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        async with installed_runtime(
            app,
            config_loader=config_loader,
            active_requests_registry=reg,
            upstream_routing_state=routing_state,
        ):
            yield

    app = FastAPI(lifespan=lifespan)
    app.include_router(auth_router)
    app.include_router(topology_router, prefix="/v1")
    app.add_middleware(RuntimeSnapshotMiddleware)
    app.add_middleware(ApiKeyAuthMiddleware)

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

        from fastapi import Request

        async def fake_auth(request: Request, call_next):
            request.state.api_key_role = ROLE_USER
            request.state.api_key_id = user_key_id
            return await call_next(request)

        config_loader = _make_config_loader(["openai", "anthropic"])
        routing_state = UpstreamRoutingState()

        @asynccontextmanager
        async def lifespan(app: FastAPI) -> AsyncIterator[None]:
            async with installed_runtime(
                app,
                config_loader=config_loader,
                active_requests_registry=reg,
                upstream_routing_state=routing_state,
            ):
                yield

        app2 = FastAPI(lifespan=lifespan)
        app2.include_router(topology_router, prefix="/v1")
        app2.add_middleware(RuntimeSnapshotMiddleware)
        app2.middleware("http")(fake_auth)

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

    def test_topology_routes_gateway_model_through_fallback_chain(self):
        """A fallback rule produces a model node and ordered model->provider edges."""
        fallback_rules = {
            "gpt-4o": {
                "fallback_models": [
                    {"provider": "openai", "model": "gpt-4o"},
                    {"provider": "anthropic", "model": "claude-3-5-sonnet"},
                ],
            }
        }
        app = _build_app(
            provider_names=["openai", "anthropic"], fallback_rules=fallback_rules
        )
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        # Model node exists.
        model_nodes = {n["id"] for n in data["nodes"] if n.get("type") == "model"}
        self.assertIn("model:gpt-4o", model_nodes)

        edges_by_id = {e["id"]: e for e in data["edges"]}

        # gateway -> model alias edge.
        alias = edges_by_id["gateway->model:gpt-4o"]
        self.assertEqual(alias["source"], "gateway")
        self.assertEqual(alias["target"], "model:gpt-4o")
        self.assertEqual(alias["type"], "alias")

        # Ordered model -> provider fallback edges with 1-based priority.
        first = edges_by_id["model:gpt-4o->provider:openai"]
        second = edges_by_id["model:gpt-4o->provider:anthropic"]
        self.assertEqual(first["type"], "fallback")
        self.assertEqual(first["data"]["priority"], 1)
        self.assertEqual(second["data"]["priority"], 2)
        self.assertIn("gpt-4o", first["data"]["models"])

    def test_topology_collapses_same_provider_targets(self):
        """Two targets of one model on the same provider collapse to one edge."""
        fallback_rules = {
            "router": {
                "fallback_models": [
                    {"provider": "openai", "model": "gpt-4o"},
                    {"provider": "openai", "model": "gpt-4o-mini"},
                ],
            }
        }
        app = _build_app(provider_names=["openai"], fallback_rules=fallback_rules)
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        fallback_edges = [
            e
            for e in data["edges"]
            if e["source"] == "model:router" and e.get("type") == "fallback"
        ]
        self.assertEqual(len(fallback_edges), 1)
        edge = fallback_edges[0]
        self.assertEqual(edge["data"]["priority"], 1)
        self.assertEqual(sorted(edge["data"]["models"]), ["gpt-4o", "gpt-4o-mini"])
        self.assertEqual(edge["data"]["steps"], [1, 2])

    def test_topology_context_overflow_edge_is_marked(self):
        """context_overflow_fallback yields a distinct context_overflow edge."""
        fallback_rules = {
            "gpt-4o": {
                "fallback_models": [{"provider": "openai", "model": "gpt-4o"}],
                "context_overflow_fallback": {
                    "provider": "anthropic",
                    "model": "claude-3-5-sonnet",
                },
            }
        }
        app = _build_app(
            provider_names=["openai", "anthropic"], fallback_rules=fallback_rules
        )
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        ctx_edge = next(
            (e for e in data["edges"] if e["id"] == "model:gpt-4o->provider:anthropic:ctx"),
            None,
        )
        self.assertIsNotNone(ctx_edge)
        self.assertEqual(ctx_edge["type"], "context_overflow")
        self.assertEqual(ctx_edge["data"]["kind"], "context_overflow")

    def test_topology_phantom_provider_is_surfaced(self):
        """A provider referenced by a rule but missing from config is shown as invalid."""
        fallback_rules = {
            "gpt-4o": {
                "fallback_models": [
                    {"provider": "openai", "model": "gpt-4o"},
                    {"provider": "ghost", "model": "phantom-1"},
                ],
            }
        }
        app = _build_app(provider_names=["openai"], fallback_rules=fallback_rules)
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        ghost = next((n for n in data["nodes"] if n["id"] == "provider:ghost"), None)
        self.assertIsNotNone(ghost)
        self.assertFalse(ghost["data"]["configured"])
        self.assertEqual(ghost["data"]["health"], "invalid")
        self.assertEqual(data["health_by_provider"].get("ghost"), "invalid")

    def test_topology_active_request_attributed_to_route_edge(self):
        """An active request marks its exact model->provider edge active."""
        reg = ActiveRequestsRegistry()
        reg.start(
            request_id="req-1", path="/v1/chat/completions", api_key_id=7
        )
        reg.update("req-1", gateway_model="gpt-4o", provider="openai", model="gpt-4o")

        fallback_rules = {
            "gpt-4o": {
                "fallback_models": [{"provider": "openai", "model": "gpt-4o"}],
            }
        }
        app = _build_app(
            provider_names=["openai"], fallback_rules=fallback_rules, registry=reg
        )
        with TestClient(app) as client:
            data = self._get_topology(client, self._master_headers())

        edges_by_id = {e["id"]: e for e in data["edges"]}
        route = edges_by_id["model:gpt-4o->provider:openai"]
        self.assertEqual(route["data"]["active"], 1)
        self.assertTrue(route["animated"])
        self.assertTrue(edges_by_id["gateway->model:gpt-4o"]["animated"])

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

    def test_topology_cache_isolated_by_runtime_generation_and_typed_dependencies(self):
        captured_state = UpstreamRoutingState()
        captured_state.mark_health("captured-n", "model", "fp", "error", "HTTP 500")
        services_n = make_app_services(upstream_routing_state=captured_state)
        loader_n = _make_config_loader(["captured-n"])
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    services=services_n,
                    config_loader=_make_config_loader(["legacy"]),
                    upstream_routing_state=UpstreamRoutingState(),
                    active_requests_registry=MagicMock(),
                )
            ),
            state=SimpleNamespace(
                api_key_role=ROLE_MASTER,
                runtime_snapshot=make_runtime_snapshot(
                    generation=1,
                    config_loader=loader_n,
                ),
            ),
        )

        first = json.loads(run_async(get_topology(request)).body)

        request.app.state.services = make_app_services()
        request.state.runtime_snapshot = make_runtime_snapshot(
            generation=2,
            config_loader=_make_config_loader(["captured-n1"]),
        )
        second = json.loads(run_async(get_topology(request)).body)

        first_providers = {node["id"] for node in first["nodes"] if node["type"] == "provider"}
        second_providers = {
            node["id"] for node in second["nodes"] if node["type"] == "provider"
        }
        self.assertEqual(first_providers, {"provider:captured-n"})
        self.assertEqual(first["health_by_provider"], {"captured-n": "error"})
        self.assertEqual(second_providers, {"provider:captured-n1"})
        self.assertEqual(
            set(topo_module._topology_cache._entries),
            {
                (1, ROLE_MASTER, None),
                (2, ROLE_MASTER, None),
            },
        )
        self.assertEqual(topo_module._topology_cache._max_entries, 128)

    def test_topology_rejects_malformed_user_identity_before_dependencies(self):
        registry = MagicMock(spec=ActiveRequestsRegistry)
        services = make_app_services(active_requests_registry=registry)

        async def fail_cache(*_args, **_kwargs):
            raise AssertionError("cache must not be consulted")

        with patch.object(topo_module._topology_cache, "get_or_compute", fail_cache):
            for malformed in (None, 0, -1, True, 1.0, "1"):
                with self.subTest(api_key_id=malformed):
                    request = SimpleNamespace(
                        app=SimpleNamespace(state=SimpleNamespace(services=services)),
                        state=SimpleNamespace(
                            api_key_role=ROLE_USER,
                            api_key_id=malformed,
                        ),
                    )
                    with self.assertRaises(HTTPException) as raised:
                        run_async(get_topology(request))
                    self.assertEqual(raised.exception.status_code, 401)
                    self.assertEqual(
                        raised.exception.detail,
                        "Authentication required.",
                    )

        registry.list_records.assert_not_called()

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

    def test_topology_container_fills_viewport_height(self):
        """The routing map container scales with the viewport (no fixed 520px box).

        Regression guard: the container used to be pinned at 520px, leaving a large
        empty gap below it on tall screens while clipping bigger graphs. It must now
        use a responsive height that fills the spare viewport space but never drops
        below the previous 520px floor — without changing the graph layout itself.
        """
        from llm_gateway_core.config.paths import STATIC_DIR

        html = (STATIC_DIR / "usage-stats.html").read_text(encoding="utf-8")
        css = (STATIC_DIR / "usage-stats.css").read_text(encoding="utf-8")

        # The inline style on the container must not hard-code a fixed height.
        container_line = next(
            (ln for ln in html.splitlines() if 'id="topology-container"' in ln),
            "",
        )
        self.assertTrue(container_line, "topology-container div not found in HTML")
        self.assertNotIn("height:", container_line.replace(" ", ""))

        # The CSS rule must drive height responsively (clamp + viewport units) and
        # keep a 520px minimum so it is never worse than the old fixed box.
        rule_start = css.index("#topology-container {")
        rule = css[rule_start:css.index("}", rule_start)]
        self.assertIn("clamp(", rule)
        self.assertIn("100dvh", rule)
        self.assertIn("520px", rule)
