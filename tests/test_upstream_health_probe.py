import asyncio
import json
from types import SimpleNamespace

from fastapi import HTTPException

from llm_gateway_core.api.v1 import stats as stats_module
from llm_gateway_core.api.v1.stats import (
    _build_health_probe_request,
    _resolve_health_probe_api_keys,
)
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.middleware.auth import ROLE_MASTER
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


class _ProbeClient:
    def __init__(self, *, started: asyncio.Event | None = None, release: asyncio.Event | None = None):
        self.started = started
        self.release = release
        self.calls: list[dict] = []

    async def post(self, url, *, headers, json, timeout):
        self.calls.append(
            {
                "url": url,
                "headers": dict(headers),
                "json": dict(json),
                "timeout": timeout,
            }
        )
        if self.started is not None and len(self.calls) == 1:
            self.started.set()
            assert self.release is not None
            await self.release.wait()
        return SimpleNamespace(status_code=200)


def _health_loader(
    providers: dict[str, ProviderDetails],
    fallback_models: list[dict],
) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = providers
    loader.fallback_rules = {
        "gateway": {
            "fallback_models": fallback_models,
        }
    }
    return loader


def _health_request(services, snapshot):
    return SimpleNamespace(
        app=SimpleNamespace(state=SimpleNamespace(services=services)),
        state=SimpleNamespace(
            api_key_role=ROLE_MASTER,
            runtime_snapshot=snapshot,
        ),
    )


def test_anthropic_health_probe_uses_x_api_key_header():
    provider_config = ProviderDetails(
        baseUrl="https://anthropic.example",
        type="anthropic",
        apikey="anthropic-key",
    )

    target_url, headers, payload = _build_health_probe_request(
        provider_config,
        "claude-model",
        "anthropic-key",
    )

    assert target_url == "https://anthropic.example/v1/messages"
    assert headers["x-api-key"] == "anthropic-key"
    assert "Authorization" not in headers
    assert payload["model"] == "claude-model"


def test_health_probe_uses_only_configured_upstream_key_pool():
    provider_config = ProviderDetails(
        baseUrl="https://provider.example",
        upstream_key_pools={
            "main": {
                "keys": [
                    {"id": "main-a", "apikey": "MAIN-A"},
                    {"id": "main-disabled", "apikey": "MAIN-DISABLED", "enabled": False},
                ]
            },
            "other": {
                "keys": [
                    {"id": "other-a", "apikey": "OTHER-A"},
                ]
            },
        },
    )

    assert _resolve_health_probe_api_keys(provider_config, "main") == ["MAIN-A"]


def test_health_run_materializes_generation_before_first_post():
    async def scenario() -> None:
        first_started = asyncio.Event()
        release_first = asyncio.Event()
        proxy_n = _ProbeClient(started=first_started, release=release_first)
        shared_n = _ProbeClient()
        legacy_client = _ProbeClient()
        upstream_state_n = UpstreamRoutingState()
        loader_n = _health_loader(
            {
                "proxied": ProviderDetails(
                    baseUrl="https://proxy-n.example",
                    apikey="PROXY-N",
                ),
                "shared": ProviderDetails(
                    baseUrl="https://shared-n.example",
                    apikey="SHARED-N",
                ),
            },
            [
                {"provider": "proxied", "model": "model-a"},
                {"provider": "shared", "model": "model-b"},
            ],
        )
        services_n = make_app_services(
            http_client=shared_n,
            upstream_routing_state=upstream_state_n,
        )
        snapshot_n = make_runtime_snapshot(
            config_loader=loader_n,
            http_client=shared_n,
            proxy_http_clients={"proxied": proxy_n},
        )
        request = _health_request(services_n, snapshot_n)
        request.app.state.http_client = legacy_client
        request.app.state.upstream_routing_state = UpstreamRoutingState()
        request.app.state.config_loader = _health_loader({}, [])
        request.app.state.proxy_http_clients = {}

        task = asyncio.create_task(stats_module.run_upstream_health_checks(request))
        await first_started.wait()

        loader_n.providers_config["shared"].baseUrl = "https://mutated.example"
        loader_n.providers_config["shared"].apikey = "MUTATED"
        loader_n.fallback_rules["gateway"]["fallback_models"][1]["model"] = "mutated-model"

        shared_n1 = _ProbeClient()
        proxy_n1 = _ProbeClient()
        upstream_state_n1 = UpstreamRoutingState()
        loader_n1 = _health_loader(
            {
                "proxied": ProviderDetails(
                    baseUrl="https://proxy-n1.example",
                    apikey="PROXY-N1",
                ),
            },
            [{"provider": "proxied", "model": "model-n1"}],
        )
        request.app.state.services = make_app_services(
            http_client=shared_n1,
            upstream_routing_state=upstream_state_n1,
        )
        request.state.runtime_snapshot = make_runtime_snapshot(
            generation=2,
            config_loader=loader_n1,
            http_client=shared_n1,
            proxy_http_clients={"proxied": proxy_n1},
        )

        release_first.set()
        response = await task

        assert json.loads(response.body)["checked"] == 2
        assert len(proxy_n.calls) == 1
        assert len(shared_n.calls) == 1
        assert shared_n.calls[0]["url"] == "https://shared-n.example/chat/completions"
        assert shared_n.calls[0]["headers"]["Authorization"] == "Bearer SHARED-N"
        assert shared_n.calls[0]["json"]["model"] == "model-b"
        assert legacy_client.calls == []
        assert shared_n1.calls == []
        assert proxy_n1.calls == []
        assert len(upstream_state_n.get_status_rows()) == 2
        assert upstream_state_n1.get_status_rows() == []

    run_async(scenario())


def test_invalid_health_target_prevents_all_posts():
    client = _ProbeClient()
    loader = _health_loader(
        {
            "valid": ProviderDetails(
                baseUrl="https://valid.example",
                apikey="VALID",
            ),
            "invalid": ProviderDetails(
                baseUrl="https://invalid.example",
                upstream_key_pools={
                    "configured": {"keys": [{"apikey": "CONFIGURED"}]},
                },
            ),
        },
        [
            {"provider": "valid", "model": "valid-model"},
            {
                "provider": "invalid",
                "model": "invalid-model",
                "upstream_key_pool": "missing",
            },
        ],
    )
    services = make_app_services(http_client=client)
    snapshot = make_runtime_snapshot(
        config_loader=loader,
        http_client=client,
    )
    request = _health_request(services, snapshot)

    async def scenario() -> None:
        try:
            await stats_module.run_upstream_health_checks(request)
        except HTTPException as exc:
            assert exc.status_code == 500
        else:
            raise AssertionError("invalid health target did not fail")

    run_async(scenario())
    assert client.calls == []
