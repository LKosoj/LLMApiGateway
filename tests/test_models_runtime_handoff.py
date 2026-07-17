from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from llm_gateway_core.api.v1 import models as models_module
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


class _CatalogResponse:
    def __init__(self, payload: dict) -> None:
        self.status_code = 200
        self.text = ""
        self._payload = payload

    def json(self) -> dict:
        return self._payload


class _CatalogClient:
    def __init__(
        self,
        payload: dict,
        *,
        started: asyncio.Event | None = None,
        release: asyncio.Event | None = None,
    ) -> None:
        self._payload = payload
        self._started = started
        self._release = release
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get(self, url: str, *, headers: dict[str, str]) -> _CatalogResponse:
        self.calls.append((url, dict(headers)))
        if self._started is not None:
            self._started.set()
            assert self._release is not None
            await self._release.wait()
        return _CatalogResponse(self._payload)


def _loader(label: str) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = {
        "fallback": ProviderDetails(
            baseUrl=f"https://{label}.example",
            apikey=f"KEY-{label}",
        )
    }
    loader.fallback_rules = {
        f"gateway/{label}": {
            "fallback_models": [
                {
                    "provider": "fallback",
                    "model": f"upstream/{label}",
                }
            ],
            "rotate_models": False,
        }
    }
    loader.operation_rules = {
        "embeddings": {
            f"operation/{label}": {
                "routes": [],
            }
        }
    }
    loader.fusion_rules = {}
    loader.router_rules = {}
    loader.model_rules = {}
    loader._fallback_rules_base = loader.fallback_rules
    return loader


def _request(
    *,
    services: object,
    snapshot: object,
    legacy_client: object,
    legacy_loader: ConfigLoader,
) -> SimpleNamespace:
    return SimpleNamespace(
        headers={},
        app=SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                http_client=legacy_client,
                config_loader=legacy_loader,
                operation_rules=legacy_loader.operation_rules,
            )
        ),
        state=SimpleNamespace(runtime_snapshot=snapshot),
    )


def test_models_list_uses_typed_process_client_and_generation_rules() -> None:
    async def scenario() -> None:
        client_n = _CatalogClient({"data": [{"id": "provider/n"}]})
        legacy_client = _CatalogClient({"data": [{"id": "provider/legacy"}]})
        loader_n = _loader("n")
        legacy_loader = _loader("legacy")
        services = make_app_services(http_client=client_n)
        snapshot = make_runtime_snapshot(
            config_loader=loader_n,
            http_client=client_n,
        )
        request = _request(
            services=services,
            snapshot=snapshot,
            legacy_client=legacy_client,
            legacy_loader=legacy_loader,
        )

        with patch.object(models_module.settings, "fallback_provider", "fallback"):
            response = await models_module.get_models(request)

        model_ids = {entry["id"] for entry in response["data"]}
        assert model_ids == {"gateway/n", "operation/n", "provider/n"}
        assert client_n.calls == [
            (
                "https://n.example/models",
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer KEY-n",
                },
            )
        ]
        assert legacy_client.calls == []

    run_async(scenario())


def test_get_model_uses_typed_process_client_and_generation_provider() -> None:
    async def scenario() -> None:
        client_n = _CatalogClient({"id": "provider/n", "object": "model"})
        legacy_client = _CatalogClient({"id": "provider/legacy", "object": "model"})
        loader_n = _loader("n")
        services = make_app_services(http_client=client_n)
        snapshot = make_runtime_snapshot(
            config_loader=loader_n,
            http_client=client_n,
        )
        request = _request(
            services=services,
            snapshot=snapshot,
            legacy_client=legacy_client,
            legacy_loader=_loader("legacy"),
        )

        with patch.object(models_module.settings, "fallback_provider", "fallback"):
            response = await models_module.get_model("provider/n", request)

        assert response == {"id": "provider/n", "object": "model"}
        assert client_n.calls == [
            (
                "https://n.example/models/provider/n",
                {
                    "Content-Type": "application/json",
                    "Authorization": "Bearer KEY-n",
                },
            )
        ]
        assert legacy_client.calls == []

    run_async(scenario())


def test_models_endpoints_do_not_fall_back_to_legacy_aliases() -> None:
    async def scenario() -> None:
        client = _CatalogClient({"data": []})
        loader = _loader("typed")
        services = make_app_services(http_client=client)
        snapshot = make_runtime_snapshot(
            config_loader=loader,
            http_client=client,
        )
        legacy_state = SimpleNamespace(
            http_client=client,
            config_loader=loader,
            operation_rules=loader.operation_rules,
        )

        missing_services = SimpleNamespace(
            headers={},
            app=SimpleNamespace(state=legacy_state),
            state=SimpleNamespace(runtime_snapshot=snapshot),
        )
        with pytest.raises(AttributeError):
            await models_module.get_models(missing_services)

        legacy_state.services = services
        missing_snapshot = SimpleNamespace(
            headers={},
            app=SimpleNamespace(state=legacy_state),
            state=SimpleNamespace(),
        )
        with pytest.raises(AttributeError):
            await models_module.get_model("gateway/typed", missing_snapshot)

    run_async(scenario())


def test_blocked_models_list_keeps_generation_n_after_runtime_replacement() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        client_n = _CatalogClient(
            {"data": []},
            started=started,
            release=release,
        )
        client_n1 = _CatalogClient({"data": [{"id": "provider/n1"}]})
        loader_n = _loader("n")
        loader_n.model_rules = {"aliases": {"alias/n": "gateway/n"}}
        loader_n1 = _loader("n1")
        loader_n1.model_rules = {"aliases": {"alias/n1": "gateway/n1"}}
        services = make_app_services(http_client=client_n)
        snapshot_n = make_runtime_snapshot(
            config_loader=loader_n,
            http_client=client_n,
        )
        snapshot_n1 = make_runtime_snapshot(
            generation=2,
            config_loader=loader_n1,
            http_client=client_n1,
        )
        request = _request(
            services=services,
            snapshot=snapshot_n,
            legacy_client=client_n,
            legacy_loader=loader_n,
        )

        with patch.object(models_module.settings, "fallback_provider", "fallback"):
            task = asyncio.create_task(models_module.get_models(request))
            await started.wait()

            request.app.state.http_client = client_n1
            request.app.state.config_loader = loader_n1
            request.app.state.operation_rules = loader_n1.operation_rules
            request.state.runtime_snapshot = snapshot_n1
            loader_n.model_rules = {"aliases": {"alias/late": "gateway/n"}}
            release.set()
            response = await task

        model_ids = {entry["id"] for entry in response["data"]}
        assert model_ids == {"alias/n", "gateway/n", "operation/n"}
        assert client_n1.calls == []

    run_async(scenario())
