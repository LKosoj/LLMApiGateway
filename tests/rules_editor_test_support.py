from __future__ import annotations

from collections.abc import Iterator
from contextlib import asynccontextmanager, contextmanager
from types import SimpleNamespace
import warnings

import httpx
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=(
            r"^`langchain-community` is being sunset and is no longer actively "
            r"maintained\. See https://github\.com/langchain-ai/"
            r"langchain-community/issues/674 for details and migration guidance "
            r"toward standalone integration packages\.$"
        ),
        category=DeprecationWarning,
        module=r"^gpt_researcher\.scraper\.arxiv\.arxiv$",
    )
    from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.middleware.auth import ROLE_MASTER
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_app_services,
    make_runtime_snapshot,
)


async def _default_model_catalog(request: httpx.Request) -> httpx.Response:
    return httpx.Response(
        200,
        json={
            "data": [
                {"id": "provider-model"},
                {"id": "large-context-model"},
            ]
        },
        request=request,
    )


@contextmanager
def transactional_rules_editor_client(
    config_loader: ConfigLoader,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> Iterator[tuple[TestClient, SimpleNamespace]]:
    """Run Rules Editor routes against the real coordinator and generation manager."""
    runtime = SimpleNamespace()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = RuntimeGenerationManager()
        shared_client = httpx.AsyncClient(
            transport=transport or httpx.MockTransport(_default_model_catalog)
        )
        initial_snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=config_loader,
            http_client=shared_client,
        )
        install_test_runtime_snapshot(manager, initial_snapshot)
        coordinator = ConfigUpdateCoordinator(
            runtime_manager=manager,
            shared_http_client=shared_client,
            initial_snapshot=initial_snapshot,
        )
        app.state.services = make_app_services(
            runtime_manager=manager,
            config_update_coordinator=coordinator,
            http_client=shared_client,
        )
        runtime.manager = manager
        runtime.coordinator = coordinator
        runtime.initial_snapshot = initial_snapshot
        runtime.shared_client = shared_client
        try:
            yield
        finally:
            await coordinator.close()
            await manager.shutdown()
            await shared_client.aclose()

    app = FastAPI(lifespan=lifespan)

    @app.middleware("http")
    async def bind_master_role(request, call_next):
        request.state.api_key_role = ROLE_MASTER
        return await call_next(request)

    @app.get("/_test/runtime-generation")
    async def capture_runtime_generation(request: Request):
        snapshot = request.state.runtime_snapshot
        runtime.observed_snapshot = snapshot
        return {"generation": snapshot.generation}

    app.include_router(editor_router, prefix="/v1")
    app.add_middleware(RuntimeSnapshotMiddleware)
    with TestClient(app) as client:
        runtime.app = app
        yield client, runtime
