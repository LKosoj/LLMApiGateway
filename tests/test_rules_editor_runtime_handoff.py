import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import httpx
from fastapi import FastAPI

from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.middleware.auth import ROLE_MASTER
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.fallback_model_evals import (
    FallbackModelEvalService,
    FallbackModelEvalSnapshot,
)
from llm_gateway_core.services.openrouter_free_models import (
    OpenRouterFreeModelsService,
)
from llm_gateway_core.services.provider_models import ProviderModelsService
from tests._async_compat import run_async
from tests.runtime_test_support import (
    installed_runtime,
    make_runtime_snapshot,
    publish_test_runtime_snapshot,
)


def _loader(provider_name: str, model_name: str) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = {
        provider_name: ProviderDetails(
            baseUrl=f"https://{provider_name}.example/v1",
            apikey=f"{provider_name}-key",
        )
    }
    loader.fallback_rules = {
        f"gateway/{model_name}": {
            "fallback_models": [
                {"provider": provider_name, "model": model_name}
            ]
        }
    }
    loader._fallback_rules_base = {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    return loader


def _client(
    *,
    runtime_middleware: bool = True,
) -> tuple[FastAPI, httpx.AsyncClient]:
    app = FastAPI()

    @app.middleware("http")
    async def bind_master_role(request, call_next):
        request.state.api_key_role = ROLE_MASTER
        return await call_next(request)

    app.include_router(editor_router, prefix="/v1")
    if runtime_middleware:
        app.add_middleware(RuntimeSnapshotMiddleware)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return app, httpx.AsyncClient(transport=transport, base_url="http://test")


def _client_resource(name: str) -> Mock:
    client = Mock(name=name)
    client.aclose = AsyncMock()
    return client


async def _wait_for(predicate) -> None:
    for _attempt in range(40):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


def _eval_snapshot() -> FallbackModelEvalSnapshot:
    return FallbackModelEvalSnapshot(
        updated_at="2026-07-12T00:00:00Z",
        source="test",
        refresh_mode="manualEval",
        ranking_version="test",
        configured_count=1,
        evaluated_count=1,
        models=[],
    )


def test_provider_catalog_holds_request_generation_and_ignores_aliases() -> None:
    async def scenario() -> None:
        app, client = _client()
        shared_client = _client_resource("shared-client")
        old_proxy = _client_resource("old-proxy")
        new_proxy = _client_resource("new-proxy")
        old_catalog = ProviderModelsService()
        new_catalog = ProviderModelsService()
        started = asyncio.Event()
        finish = asyncio.Event()
        captured: dict[str, object] = {}

        async def blocked_get_models(
            provider_name,
            provider_config,
            http_client,
            *,
            auth_headers=None,
        ):
            captured.update(
                provider_name=provider_name,
                provider_config=provider_config,
                http_client=http_client,
                auth_headers=auth_headers,
            )
            started.set()
            await finish.wait()
            return ["model-n"]

        old_catalog.get_models = blocked_get_models  # type: ignore[method-assign]
        first_loader = _loader("provider-n", "model-n")
        async with installed_runtime(
            app,
            config_loader=first_loader,
            snapshot_overrides={
                "provider_models_service": old_catalog,
                "proxy_http_clients": {"provider-n": old_proxy},
            },
            http_client=shared_client,
        ) as first_snapshot:
            services = app.state.services
            app.state.config_loader = _loader("alias-provider", "alias-model")
            app.state.provider_models_service = Mock(name="alias-catalog")
            app.state.proxy_http_clients = {
                "provider-n": Mock(name="alias-proxy")
            }
            app.state.http_client = Mock(name="alias-shared")

            request_task = asyncio.create_task(
                client.get("/v1/config/providers/provider-n/models")
            )
            await started.wait()

            second_snapshot = make_runtime_snapshot(
                generation=2,
                config_loader=_loader("provider-next", "model-next"),
                http_client=shared_client,
                provider_models_service=new_catalog,
                proxy_http_clients={"provider-next": new_proxy},
            )
            publish_test_runtime_snapshot(services.runtime_manager,
                second_snapshot,
                expected_generation=1,
            )
            old_proxy.aclose.assert_not_awaited()

            finish.set()
            response = await request_task
            assert response.status_code == 200
            assert response.json()["models"] == [{"id": "model-n"}]
            assert captured["provider_name"] == "provider-n"
            assert captured["provider_config"] is first_snapshot.config_loader.providers_config["provider-n"]
            assert captured["http_client"] is old_proxy
            assert captured["auth_headers"] == {
                "Authorization": "Bearer provider-n-key"
            }
            await _wait_for(lambda: old_proxy.aclose.await_count == 1)

        await client.aclose()
        new_proxy.aclose.assert_awaited_once()

    run_async(scenario())


def test_openrouter_control_endpoints_use_container_service_not_alias() -> None:
    async def scenario() -> None:
        app, client = _client()
        service = OpenRouterFreeModelsService()
        service.start_manual_full_refresh = AsyncMock(return_value=True)  # type: ignore[method-assign]
        service.get_status = AsyncMock(  # type: ignore[method-assign]
            return_value={"configured": True, "manualRefreshRunning": True}
        )
        alias = SimpleNamespace(
            start_manual_full_refresh=AsyncMock(return_value=False),
            get_status=AsyncMock(return_value={"configured": False}),
        )

        async with installed_runtime(
            app,
            openrouter_free_models_service=service,
        ):
            app.state.openrouter_free_models_service = alias
            status_response = await client.get("/v1/openrouter/free-models")
            run_response = await client.post("/v1/openrouter/free-models/run")

            assert status_response.status_code == 200
            assert run_response.status_code == 200
            assert status_response.json()["configured"] is True
            service.start_manual_full_refresh.assert_awaited_once_with()
            assert service.get_status.await_count == 2
            alias.start_manual_full_refresh.assert_not_awaited()
            alias.get_status.assert_not_awaited()

        await client.aclose()

    run_async(scenario())


def test_fallback_run_owns_exact_request_lease_and_rejected_run_does_not_leak() -> None:
    async def scenario() -> None:
        app, client = _client()
        service = FallbackModelEvalService()
        shared_client = _client_resource("shared-client")
        old_proxy = _client_resource("old-proxy")
        new_proxy = _client_resource("new-proxy")
        first_loader = _loader("provider-n", "model-n")
        started = asyncio.Event()
        finish = asyncio.Event()
        captured: dict[str, object] = {}

        async def blocked_build(**kwargs):
            captured.update(kwargs)
            started.set()
            await finish.wait()
            return _eval_snapshot()

        service._build_snapshot = blocked_build  # type: ignore[method-assign]
        async with installed_runtime(
            app,
            config_loader=first_loader,
            snapshot_overrides={"proxy_http_clients": {"provider-n": old_proxy}},
            http_client=shared_client,
            fallback_model_eval_service=service,
        ) as first_snapshot:
            services = app.state.services
            alias = SimpleNamespace(
                start_eval=AsyncMock(),
                start_eval_with_runtime=AsyncMock(),
                get_status=AsyncMock(),
            )
            app.state.fallback_model_eval_service = alias
            app.state.config_loader = _loader("alias-provider", "alias-model")
            app.state.http_client = Mock(name="alias-shared")
            app.state.proxy_http_clients = {}

            first_response = await client.post("/v1/fallback-model-evals/run")
            assert first_response.status_code == 200
            await started.wait()
            first_task = service._task
            assert first_task is not None

            second_snapshot = make_runtime_snapshot(
                generation=2,
                config_loader=_loader("provider-next", "model-next"),
                http_client=shared_client,
                proxy_http_clients={"provider-next": new_proxy},
            )
            publish_test_runtime_snapshot(services.runtime_manager,
                second_snapshot,
                expected_generation=1,
            )
            assert services.runtime_manager.active_leases[1] == 1
            old_proxy.aclose.assert_not_awaited()

            rejected_response = await client.post(
                "/v1/fallback-model-evals/run"
            )
            assert rejected_response.status_code == 409
            assert services.runtime_manager.active_leases[1] == 1
            assert services.runtime_manager.active_leases[2] == 0
            alias.start_eval.assert_not_awaited()
            alias.start_eval_with_runtime.assert_not_awaited()

            assert captured["providers_config"] is first_snapshot.config_loader.providers_config
            assert captured["fallback_rules"] is first_snapshot.config_loader.fallback_rules
            assert captured["proxy_http_clients"] is first_snapshot.proxy_http_clients
            assert captured["http_client"] is shared_client

            finish.set()
            await first_task
            await _wait_for(lambda: old_proxy.aclose.await_count == 1)
            assert not any(services.runtime_manager.active_leases.values())

        await client.aclose()
        new_proxy.aclose.assert_awaited_once()

    run_async(scenario())


def test_fallback_start_failure_releases_endpoint_child_lease() -> None:
    async def scenario() -> None:
        app, client = _client()
        service = FallbackModelEvalService()
        service._stopping = True
        async with installed_runtime(
            app,
            fallback_model_eval_service=service,
        ):
            services = app.state.services
            response = await client.post("/v1/fallback-model-evals/run")
            assert response.status_code == 500
            assert not any(services.runtime_manager.active_leases.values())
            assert service._task is None
            service._stopping = False

        await client.aclose()

    run_async(scenario())


def test_control_endpoints_fail_closed_without_typed_runtime() -> None:
    async def scenario() -> None:
        app, client = _client(runtime_middleware=False)
        app.state.config_loader = _loader("alias-provider", "alias-model")
        app.state.openrouter_free_models_service = Mock()
        app.state.fallback_model_eval_service = Mock()

        for method, path in (
            (client.get, "/v1/config/providers/alias-provider/models"),
            (client.get, "/v1/openrouter/free-models"),
            (client.get, "/v1/fallback-model-evals"),
            (client.post, "/v1/openrouter/free-models/run"),
            (client.post, "/v1/fallback-model-evals/run"),
        ):
            response = await method(path)
            assert response.status_code == 500
            assert response.json() == {
                "detail": "Internal server error: Runtime dependencies not available."
            }

        await client.aclose()

    run_async(scenario())
