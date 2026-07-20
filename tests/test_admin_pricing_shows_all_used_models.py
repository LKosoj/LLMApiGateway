"""Integration tests: GET /v1/admin/pricing surfaces every model the runtime
config actually uses (fallback_rules + operation_rules), not just the models
already priced in providers.json, and best-effort persists any OpenRouter
autofill it can resolve from the catalog.

Uses the same real ``ConfigLoader``/``ConfigUpdateCoordinator`` wiring as
``tests/test_admin_pricing_transactional.py``, plus a real (but network-free)
``OpenRouterFreeModelsService`` primed via ``refresh_once()`` against a fake
HTTP client -- mirroring the pattern in ``tests/test_openrouter_free_models
.py`` -- to exercise the actual GET-time autofill classification end to end.
"""

from __future__ import annotations

import json
import warnings
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=(
            r"`langchain-community` is being sunset and is no longer actively "
            r"maintained\. See https://github\.com/langchain-ai/"
            r"langchain-community/issues/674 for details and migration guidance "
            r"toward standalone integration packages\."
        ),
        category=DeprecationWarning,
        module=r"gpt_researcher\.scraper\.arxiv\.arxiv",
    )
    from llm_gateway_core.api.v1.admin_pricing import admin_pricing_router

from llm_gateway_core.config.config_store import ConfigSourceBundle
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.accounting import DEFAULT_OPERATION_COST_USD
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from llm_gateway_core.utils.usage_tracking import build_model_cost_rate_registry
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_app_services,
    make_runtime_snapshot,
)


MASTER_KEY = "pricing-used-models-master"


class _FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://openrouter.example/x")
            raise httpx.HTTPStatusError(
                "unexpected status", request=request,
                response=httpx.Response(self.status_code, request=request),
            )


class _FakeOpenRouterClient:
    """Minimal fake matching tests/test_openrouter_free_models.py's shape."""

    def __init__(self, catalog):
        self.catalog = catalog

    async def get(self, url, headers=None):
        return _FakeResponse({"data": self.catalog})

    async def post(self, url, headers=None, json=None, timeout=None):
        prompt = json["messages"][0]["content"]
        if "Reply with exactly OK" in prompt:
            content = "OK"
        else:
            content = "OK"
        return _FakeResponse(
            {"choices": [{"message": {"content": content}}], "usage": {}}
        )


def _catalog_entry(model_id: str, *, prompt: str, completion: str) -> dict:
    return {
        "id": model_id,
        "name": model_id,
        "created": 1_760_000_000,
        "context_length": 131072,
        "top_provider": {"max_completion_tokens": 4096},
        "pricing": {"prompt": prompt, "completion": completion},
        "supported_parameters": ["tools"],
        "architecture": {"output_modalities": ["text"]},
        "expiration_date": None,
    }


def _primed_openrouter_service(catalog: list[dict]) -> OpenRouterFreeModelsService:
    service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
    service._configured = True
    service._provider_config = ProviderDetails(
        baseUrl="https://openrouter.ai/api/v1", apikey="or-key"
    )
    service._provider_api_key = "or-key"
    service._http_client = _FakeOpenRouterClient(catalog)
    run_async(service.refresh_once())
    return service


def _write_sources(root: Path) -> None:
    (root / "providers.json").write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://example.com/v1",
                        "apikey": "dummy",
                        "models": {"chat": {"input_rate": 1.0, "output_rate": 2.0}},
                    }
                },
                {
                    "openrouter": {
                        "baseUrl": "https://openrouter.ai/api/v1",
                        "apikey": "or-key",
                    }
                },
                {
                    "cohere": {
                        "baseUrl": "https://api.cohere.ai/v1",
                        "apikey": "cohere-key",
                    }
                },
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_fallback_rules.json").write_text(
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat",
                    "fallback_models": [{"provider": "primary", "model": "chat"}],
                },
                {
                    "gateway_model_name": "gateway/or-chat",
                    "fallback_models": [
                        {"provider": "openrouter", "model": "vendor/or-chat-model"}
                    ],
                },
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_operation_rules.json").write_text(
        json.dumps(
            {
                "embeddings": [
                    {
                        "gateway_model_name": "gateway/embed",
                        "routes": [
                            {
                                "provider": "openrouter",
                                "model": "vendor/or-embed-model",
                                "target_path": "/embeddings",
                            }
                        ],
                    }
                ],
                "rerank": [
                    {
                        "gateway_model_name": "gateway/rerank",
                        "routes": [
                            {
                                "provider": "cohere",
                                "model": "rerank-v3",
                                "target_path": "/rerank",
                            }
                        ],
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    (root / "models_fusion_rules.json").write_text("[]\n", encoding="utf-8")
    (root / "models_router_rules.json").write_text("[]\n", encoding="utf-8")
    (root / "models_model_rules.json").write_text("{}\n", encoding="utf-8")


def _master_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER_KEY}"}


def _build_app(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    openrouter_service: OpenRouterFreeModelsService | None = None,
) -> FastAPI:
    _write_sources(root)
    monkeypatch.setattr(
        "llm_gateway_core.config.loader.settings.fallback_provider", "primary"
    )
    monkeypatch.setattr(
        "llm_gateway_core.middleware.auth.settings.gateway_api_key", MASTER_KEY
    )
    loader = ConfigLoader.from_source_bundle(
        ConfigSourceBundle.capture(root)
    ).load_complete()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = RuntimeGenerationManager()
        shared_client = httpx.AsyncClient()
        initial_snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=loader,
            http_client=shared_client,
            cost_rate_registry=build_model_cost_rate_registry(loader.providers_config),
        )
        install_test_runtime_snapshot(manager, initial_snapshot)
        coordinator = ConfigUpdateCoordinator(
            runtime_manager=manager,
            shared_http_client=shared_client,
            initial_snapshot=initial_snapshot,
        )
        overrides = dict(
            runtime_manager=manager,
            config_update_coordinator=coordinator,
            http_client=shared_client,
        )
        if openrouter_service is not None:
            overrides["openrouter_free_models_service"] = openrouter_service
        services = make_app_services(**overrides)
        app.state.services = services
        try:
            yield
        finally:
            del app.state.services
            await coordinator.close()
            await services.task_supervisor.close()
            await manager.shutdown()
            await shared_client.aclose()

    app = FastAPI(lifespan=lifespan)
    app.add_middleware(RuntimeSnapshotMiddleware)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.include_router(admin_pricing_router, prefix="/v1")
    return app


def _item(items: list[dict], provider: str, model: str) -> dict:
    for entry in items:
        if entry["provider"] == provider and entry["model"] == model:
            return entry
    raise AssertionError(f"no item for ({provider!r}, {model!r}) in {items!r}")


def test_get_pricing_surfaces_used_models_awaiting_catalog(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OpenRouter catalog primed: used-but-unpriced openrouter models are
    reported as awaiting the catalog, the non-openrouter operation model gets
    the operation default, and providers.json is left untouched."""
    app = _build_app(tmp_path, monkeypatch)
    original_providers_json = (tmp_path / "providers.json").read_bytes()

    with TestClient(app) as client:
        response = client.get("/v1/admin/pricing", headers=_master_headers())

    assert response.status_code == 200
    items = response.json()["items"]

    configured = _item(items, "primary", "chat")
    assert configured["source"] == "configured"
    assert configured["input_rate"] == 1.0
    assert configured["output_rate"] == 2.0

    or_chat = _item(items, "openrouter", "vendor/or-chat-model")
    assert or_chat["source"] == "awaiting_openrouter_catalog"
    assert or_chat["input_rate"] is None
    assert or_chat["output_rate"] is None
    assert or_chat["default_cost_per_request"] is None

    or_embed = _item(items, "openrouter", "vendor/or-embed-model")
    assert or_embed["source"] == "awaiting_openrouter_catalog"

    rerank = _item(items, "cohere", "rerank-v3")
    assert rerank["source"] == "operation_default"
    assert rerank["default_cost_per_request"] == DEFAULT_OPERATION_COST_USD
    assert rerank["input_rate"] is None
    assert rerank["output_rate"] is None

    # Nothing could be autofilled without a catalog, so the file is untouched.
    assert (tmp_path / "providers.json").read_bytes() == original_providers_json


def test_get_pricing_autofills_openrouter_model_and_persists_then_reclassifies_as_configured(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A catalog hit for a used openrouter model is auto-filled and written to
    providers.json on the first GET; a second GET then reports the same rate
    with source=configured, proving the write round-trips."""
    catalog = [
        _catalog_entry("vendor/or-chat-model", prompt="0.000005", completion="0.000015"),
        # gateway/embed's model has no catalog entry -> stays an operation
        # default rather than being silently priced at $0.
    ]
    service = _primed_openrouter_service(catalog)
    app = _build_app(tmp_path, monkeypatch, openrouter_service=service)

    with TestClient(app) as client:
        first = client.get("/v1/admin/pricing", headers=_master_headers())
        assert first.status_code == 200
        first_items = first.json()["items"]
        autofilled = _item(first_items, "openrouter", "vendor/or-chat-model")
        assert autofilled["source"] == "openrouter_autofill"
        assert autofilled["input_rate"] == pytest.approx(5.0)
        assert autofilled["output_rate"] == pytest.approx(15.0)

        embed_row = _item(first_items, "openrouter", "vendor/or-embed-model")
        assert embed_row["source"] == "operation_default"
        assert embed_row["default_cost_per_request"] == DEFAULT_OPERATION_COST_USD

        # Persisted to disk immediately (best-effort, same GET request).
        written = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
        openrouter_entry = next(iter(
            entry["openrouter"] for entry in written if "openrouter" in entry
        ))
        assert openrouter_entry["models"]["vendor/or-chat-model"] == {
            "input_rate": 5.0,
            "output_rate": 15.0,
        }
        # The already-configured "primary/chat" rate must be preserved
        # untouched by the autofill write (full-replace semantics apply to
        # the whole desired list, not just the new addition).
        primary_entry = next(iter(
            entry["primary"] for entry in written if "primary" in entry
        ))
        assert primary_entry["models"]["chat"] == {"input_rate": 1.0, "output_rate": 2.0}

        second = client.get("/v1/admin/pricing", headers=_master_headers())
        assert second.status_code == 200
        second_items = second.json()["items"]
        reclassified = _item(second_items, "openrouter", "vendor/or-chat-model")
        assert reclassified["source"] == "configured"
        assert reclassified["input_rate"] == pytest.approx(5.0)
        assert reclassified["output_rate"] == pytest.approx(15.0)
