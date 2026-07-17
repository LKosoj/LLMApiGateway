"""Tests for the /v1/admin/upstream-quotas endpoint."""
from __future__ import annotations

import asyncio
import os
import unittest
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_subscription_quotas import (
    get_upstream_quotas,
    router as upstream_quota_router,
)
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.upstream_subscription_quota import (
    SubscriptionQuotaSnapshot,
)
from tests._async_compat import run_async
from tests.runtime_test_support import (
    installed_runtime,
    make_app_services,
    make_runtime_snapshot,
)


MASTER_KEY = "test-master-key-upstream"


def _make_config_loader(providers_config=None) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = providers_config if providers_config is not None else {}
    loader.fallback_rules = {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    loader._fallback_rules_base = {}
    return loader


class _FakeQuotaService:
    def __init__(self, snapshots):
        self._snapshots = snapshots
        self.calls = []

    async def fetch_all(self, *, providers_config):
        self.calls.append(providers_config)
        return self._snapshots


def _build_app(quota_service=None, config_loader=None, api_keys_db=None) -> FastAPI:
    service = quota_service if quota_service is not None else _FakeQuotaService([])
    loader = config_loader if config_loader is not None else _make_config_loader()

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        overrides = {"upstream_subscription_quota_service": service}
        if api_keys_db is not None:
            overrides["api_keys_db"] = api_keys_db
        async with installed_runtime(app, config_loader=loader, **overrides):
            yield

    app = FastAPI(lifespan=lifespan)
    app.state.upstream_subscription_quota_service = _FakeQuotaService([])
    app.state.config_loader = _make_config_loader({"legacy": object()})
    app.include_router(auth_router)
    app.include_router(upstream_quota_router, prefix="/v1")
    app.add_middleware(RuntimeSnapshotMiddleware)
    app.add_middleware(ApiKeyAuthMiddleware)
    return app


class TestUpstreamQuotasEndpointMaster200(unittest.TestCase):
    def setUp(self):
        self.patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_endpoint_master_200(self):
        snapshot = SubscriptionQuotaSnapshot(
            provider="copilot",
            kind="github_copilot",
            plan="business",
            reset_date="2026-06-01",
            categories={},
            fetched_at=1234567890.0,
            error=None,
        )
        service = _FakeQuotaService([snapshot])
        loader = _make_config_loader({"captured": object()})
        app = _build_app(quota_service=service, config_loader=loader)

        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/upstream-quotas",
                headers={"Authorization": f"Bearer {MASTER_KEY}"},
            )
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertIn("snapshots", data)
        self.assertEqual(len(data["snapshots"]), 1)
        self.assertEqual(data["snapshots"][0]["provider"], "copilot")
        self.assertEqual(data["snapshots"][0]["kind"], "github_copilot")
        self.assertEqual(service.calls, [loader.providers_config])


class TestUpstreamQuotasEndpointVirtualKey403(unittest.TestCase):
    def setUp(self):
        import tempfile
        from pathlib import Path
        from llm_gateway_core.db.api_keys_db import ApiKeysDB
        import llm_gateway_core.db.api_keys_db as api_keys_db_module

        self._tmp = tempfile.TemporaryDirectory()
        os.makedirs(Path(self._tmp.name) / "db", exist_ok=True)

        self._path_patcher = patch.object(
            api_keys_db_module,
            "__file__",
            str(Path(self._tmp.name) / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        self._path_patcher.start()

        self.db = ApiKeysDB(db_filename="test_upstream_quota.db")
        self.rec = self.db.create(name="virtual-user")

        self.key_patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.key_patcher.start()

    def tearDown(self):
        self.key_patcher.stop()
        self._path_patcher.stop()
        self._tmp.cleanup()

    def test_endpoint_virtual_key_403(self):
        app = _build_app(api_keys_db=self.db)

        with TestClient(app) as client:
            resp = client.get(
                "/v1/admin/upstream-quotas",
                headers={"Authorization": f"Bearer {self.rec.api_key}"},
            )
        self.assertEqual(resp.status_code, 403)


class TestUpstreamQuotasEndpointNoAuth401(unittest.TestCase):
    def setUp(self):
        self.patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
        )
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_endpoint_no_auth_401(self):
        app = _build_app()
        with TestClient(app) as client:
            resp = client.get("/v1/admin/upstream-quotas")
        self.assertEqual(resp.status_code, 401)


class TestUpstreamQuotasRuntimeCapture(unittest.TestCase):
    def test_endpoint_keeps_captured_services_and_snapshot_across_await(self):
        async def scenario() -> None:
            started = asyncio.Event()
            release = asyncio.Event()
            providers_n = {"generation-n": object()}
            loader_n = _make_config_loader(providers_n)

            class BlockingService(_FakeQuotaService):
                async def fetch_all(self, *, providers_config):
                    self.calls.append(providers_config)
                    started.set()
                    await release.wait()
                    return self._snapshots

            service_n = BlockingService([])
            request = SimpleNamespace(
                app=SimpleNamespace(
                    state=SimpleNamespace(
                        services=make_app_services(
                            upstream_subscription_quota_service=service_n
                        )
                    )
                ),
                state=SimpleNamespace(
                    runtime_snapshot=make_runtime_snapshot(config_loader=loader_n)
                ),
            )

            task = asyncio.create_task(get_upstream_quotas(request))
            await started.wait()
            request.app.state.services = make_app_services(
                upstream_subscription_quota_service=_FakeQuotaService([])
            )
            request.state.runtime_snapshot = make_runtime_snapshot(
                generation=2,
                config_loader=_make_config_loader({"generation-n1": object()}),
            )
            release.set()

            self.assertEqual(await task, {"snapshots": []})
            self.assertEqual(service_n.calls, [providers_n])

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
