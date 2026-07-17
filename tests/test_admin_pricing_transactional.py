from __future__ import annotations

import hashlib
import json
import warnings
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, Mock

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
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.runtime_snapshot import RuntimeSnapshotMiddleware
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from llm_gateway_core.utils.usage_tracking import build_model_cost_rate_registry
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_app_services,
    make_runtime_snapshot,
)


MASTER_KEY = "pricing-transaction-master"


def _provider_source() -> str:
    return """[
  {
    "primary": {
      // preserve this comment in the durable backup
      "baseUrl": "https://primary.example/v1",
      "apikey": "DIRECT-KEY",
      "available_models": ["chat", "metadata-only", "rate-only", "new"],
      "routing": {"strategy": "round-robin"},
      "models": {
        "chat": {
          "context_length": 131072,
          "upstream_limits": {"rpm": 30},
          "input_rate": 1.0,
          "output_rate": 2.0
        },
        "metadata-only": {
          "max_completion_tokens": 4096,
          "input_rate": 3.0,
          "output_rate": 4.0
        },
        "rate-only": {"input_rate": 5.0, "output_rate": 6.0}
      }
    }
  }
]
"""


def _write_sources(root: Path, providers_source: str | None = None) -> None:
    (root / "providers.json").write_text(
        providers_source or _provider_source(),
        encoding="utf-8",
    )
    (root / "models_fallback_rules.json").write_text(
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat",
                    "fallback_models": [
                        {"provider": "primary", "model": "chat"}
                    ],
                }
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_model_rules.json").write_text("{}\n", encoding="utf-8")
    (root / "models_operation_rules.json").write_text("{}\n", encoding="utf-8")
    (root / "models_fusion_rules.json").write_text("[]\n", encoding="utf-8")
    (root / "models_router_rules.json").write_text("[]\n", encoding="utf-8")


def _master_headers(**extra: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {MASTER_KEY}", **extra}


def _build_app(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    providers_source: str | None = None,
) -> tuple[FastAPI, dict[str, Any]]:
    _write_sources(root, providers_source)
    monkeypatch.setattr(
        "llm_gateway_core.config.loader.settings.fallback_provider",
        "primary",
    )
    monkeypatch.setattr(
        "llm_gateway_core.middleware.auth.settings.gateway_api_key",
        MASTER_KEY,
    )
    loader = ConfigLoader.from_source_bundle(
        ConfigSourceBundle.capture(root)
    ).load_complete()
    observed: dict[str, Any] = {"initial_loader": loader}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        manager = RuntimeGenerationManager()
        shared_client = httpx.AsyncClient()
        initial_snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=loader,
            http_client=shared_client,
            cost_rate_registry=build_model_cost_rate_registry(
                loader.providers_config
            ),
        )
        install_test_runtime_snapshot(manager, initial_snapshot)
        coordinator = ConfigUpdateCoordinator(
            runtime_manager=manager,
            shared_http_client=shared_client,
            initial_snapshot=initial_snapshot,
        )
        services = make_app_services(
            runtime_manager=manager,
            config_update_coordinator=coordinator,
            http_client=shared_client,
        )
        app.state.services = services
        observed.update(
            manager=manager,
            coordinator=coordinator,
            initial_snapshot=initial_snapshot,
            services=services,
        )
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
    return app, observed


def test_get_and_calculate_use_one_request_snapshot_and_exact_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        app.state.config_loader = SimpleNamespace(providers_config={})
        app.state.cost_rate_registry = {}

        response = client.get(
            "/v1/admin/pricing",
            headers=_master_headers(),
        )
        calculation = client.post(
            "/v1/admin/pricing/calculate",
            headers=_master_headers(),
            json={
                "provider": "primary",
                "model": "chat",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 500_000,
            },
        )

    assert response.status_code == 200
    assert response.json()["items"] == [
        {
            "provider": "primary",
            "model": "chat",
            "input_rate": 1.0,
            "output_rate": 2.0,
        },
        {
            "provider": "primary",
            "model": "metadata-only",
            "input_rate": 3.0,
            "output_rate": 4.0,
        },
        {
            "provider": "primary",
            "model": "rate-only",
            "input_rate": 5.0,
            "output_rate": 6.0,
        },
    ]
    digest = hashlib.sha256(_provider_source().encode()).hexdigest()
    assert response.headers["etag"] == f'"providers:sha256:{digest}"'
    assert response.headers["x-config-generation"] == "1"
    assert response.headers["cache-control"] == "no-store"
    assert calculation.status_code == 200
    assert calculation.json() == {
        "cost_usd": 2.0,
        "input_rate": 1.0,
        "output_rate": 2.0,
    }


def test_zero_rates_and_zero_calculated_cost_are_preserved_exactly(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = _provider_source().replace(
        '"input_rate": 1.0,\n          "output_rate": 2.0',
        '"input_rate": 0.0,\n          "output_rate": 0.0',
        1,
    )
    app, _observed = _build_app(
        tmp_path,
        monkeypatch,
        providers_source=source,
    )
    with TestClient(app) as client:
        response = client.get("/v1/admin/pricing", headers=_master_headers())
        calculation = client.post(
            "/v1/admin/pricing/calculate",
            headers=_master_headers(),
            json={
                "provider": "primary",
                "model": "chat",
                "prompt_tokens": 1_000_000,
                "completion_tokens": 1_000_000,
            },
        )

    assert response.status_code == 200
    chat = next(item for item in response.json()["items"] if item["model"] == "chat")
    assert chat["input_rate"] == 0.0
    assert chat["output_rate"] == 0.0
    assert calculation.status_code == 200
    assert calculation.json() == {
        "cost_usd": 0.0,
        "input_rate": 0.0,
        "output_rate": 0.0,
    }


def test_put_publishes_n_plus_one_and_preserves_metadata_and_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = _provider_source().encode()
    app, observed = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            json={
                "items": [
                    {
                        "provider": "primary",
                        "model": "chat",
                        "input_rate": 10.0,
                        "output_rate": 20.0,
                    },
                    {
                        "provider": "primary",
                        "model": "new",
                        "input_rate": 0.0,
                        "output_rate": 7.5,
                    },
                ]
            },
        )
        refreshed = client.get("/v1/admin/pricing", headers=_master_headers())

        assert not hasattr(app.state, "cost_rate_registry")
        assert not hasattr(app.state, "config_loader")

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {
                "provider": "primary",
                "model": "chat",
                "input_rate": 10.0,
                "output_rate": 20.0,
            },
            {
                "provider": "primary",
                "model": "new",
                "input_rate": 0.0,
                "output_rate": 7.5,
            },
        ]
    }
    assert response.headers["x-config-generation"] == "2"
    assert response.headers["etag"] != base.headers["etag"]
    assert response.headers["cache-control"] == "no-store"
    assert refreshed.status_code == 200
    assert refreshed.headers["etag"] == response.headers["etag"]
    assert refreshed.json() == response.json()

    written = json.loads((tmp_path / "providers.json").read_text(encoding="utf-8"))
    models = written[0]["primary"]["models"]
    assert models["chat"] == {
        "context_length": 131072,
        "upstream_limits": {"rpm": 30},
        "input_rate": 10.0,
        "output_rate": 20.0,
    }
    assert models["metadata-only"] == {"max_completion_tokens": 4096}
    assert "rate-only" not in models
    assert models["new"] == {"input_rate": 0.0, "output_rate": 7.5}
    backups = list(tmp_path.glob("providers.json.bak.*"))
    assert len(backups) == 1
    assert backups[0].read_bytes() == original
    initial_snapshot = observed["initial_snapshot"]
    assert initial_snapshot.generation == 1
    assert initial_snapshot.cost_rate_registry[("primary", "chat")].input_rate == 1.0


def test_stale_revision_wins_over_invalid_payload_without_a_second_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        first = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            json={
                "items": [
                    {
                        "provider": "primary",
                        "model": "chat",
                        "input_rate": 8.0,
                        "output_rate": 9.0,
                    }
                ]
            },
        )
        committed = (tmp_path / "providers.json").read_bytes()
        backups = tuple(tmp_path.glob("providers.json.bak.*"))
        stale = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            json={
                "items": [
                    {
                        "provider": "unknown",
                        "model": " bad ",
                        "input_rate": -1,
                        "output_rate": 1,
                    }
                ]
            },
        )

    assert first.status_code == 200
    assert stale.status_code == 409
    assert stale.json() == {
        "detail": {
            "code": "config_revision_conflict",
            "message": "The configuration revision changed.",
        }
    }
    assert (tmp_path / "providers.json").read_bytes() == committed
    assert tuple(tmp_path.glob("providers.json.bak.*")) == backups


@pytest.mark.parametrize(
    "body",
    [
        {
            "items": [
                {
                    "provider": "unknown",
                    "model": "model",
                    "input_rate": 1,
                    "output_rate": 2,
                }
            ]
        },
        {
            "items": [
                {
                    "provider": "primary",
                    "model": "chat",
                    "input_rate": 1,
                    "output_rate": 2,
                },
                {
                    "provider": "primary",
                    "model": "chat",
                    "input_rate": 3,
                    "output_rate": 4,
                },
            ]
        },
    ],
)
def test_invalid_semantics_are_safe_400_without_writing(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: dict[str, Any],
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    original = (tmp_path / "providers.json").read_bytes()
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            json=body,
        )

    assert response.status_code == 400
    assert response.json() == {
        "detail": {
            "code": "config_validation_failed",
            "message": "Configuration validation failed.",
        }
    }
    assert (tmp_path / "providers.json").read_bytes() == original
    assert list(tmp_path.glob("providers.json.bak.*")) == []


def test_malformed_if_match_precedes_malformed_json(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    with TestClient(app) as client:
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": "bad"}),
            content=b"{",
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_request_invalid"


@pytest.mark.parametrize(
    "content",
    [
        b"{",
        b"\xff",
        b'{"items": []}\xef\xbb\xbf',
    ],
)
def test_malformed_pricing_envelope_is_safe_400_without_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    content: bytes,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    original_names = {path.name for path in tmp_path.iterdir()}
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            content=content,
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_request_invalid"
    assert {path.name for path in tmp_path.iterdir()} == original_names


@pytest.mark.parametrize(
    "body",
    [
        None,
        [],
        {},
        {"items": None},
        {"items": {}},
        {"items": [None]},
        {"items": [{}]},
        {
            "items": [
                {
                    "provider": 1,
                    "model": "chat",
                    "input_rate": 1,
                    "output_rate": 2,
                }
            ]
        },
        {
            "items": [
                {
                    "provider": "primary",
                    "model": "chat",
                    "input_rate": True,
                    "output_rate": 2,
                }
            ]
        },
    ],
)
def test_invalid_pricing_envelope_shapes_are_safe_validation_failures(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    body: Any,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    original = (tmp_path / "providers.json").read_bytes()
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        response = client.put(
            "/v1/admin/pricing",
            headers={
                **_master_headers(),
                "If-Match": base.headers["etag"],
                "Content-Type": "application/json",
            },
            content=json.dumps(body).encode(),
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_validation_failed"
    assert (tmp_path / "providers.json").read_bytes() == original


def test_deeply_nested_pricing_envelope_is_safe_400(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    content = ("[" * 10_000 + "]" * 10_000).encode()
    with TestClient(app, raise_server_exceptions=False) as client:
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(),
            content=content,
        )

    assert response.status_code == 400
    assert response.json()["detail"]["code"] == "config_request_invalid"


def test_out_of_band_source_drift_precedes_semantics_and_candidate_work(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, observed = _build_app(tmp_path, monkeypatch)
    semantics = Mock(side_effect=AssertionError("pricing semantics ran"))
    monkeypatch.setattr(
        "llm_gateway_core.api.v1.admin_pricing.patch_provider_pricing",
        semantics,
    )
    with TestClient(app) as client:
        base = client.get("/v1/admin/pricing", headers=_master_headers())
        (tmp_path / "providers.json").write_text(
            _provider_source().replace('"input_rate": 1.0', '"input_rate": 9.0'),
            encoding="utf-8",
        )
        update = AsyncMock(side_effect=AssertionError("candidate work ran"))
        monkeypatch.setattr(observed["coordinator"], "update", update)
        names_before = {path.name for path in tmp_path.iterdir()}
        response = client.put(
            "/v1/admin/pricing",
            headers=_master_headers(**{"If-Match": base.headers["etag"]}),
            json={"items": [{"provider": "unknown"}]},
        )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "config_revision_conflict"
    semantics.assert_not_called()
    update.assert_not_awaited()
    assert {path.name for path in tmp_path.iterdir()} == names_before


def test_put_openapi_keeps_the_explicit_request_and_response_contract(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    app, _observed = _build_app(tmp_path, monkeypatch)
    document = app.openapi()
    operation = document["paths"]["/v1/admin/pricing"]["put"]

    request_body = operation["requestBody"]
    assert request_body["required"] is True
    schema = request_body["content"]["application/json"]["schema"]
    assert schema["type"] == "object"
    assert schema["required"] == ["items"]
    assert schema["properties"]["items"]["type"] == "array"
    item_schema = schema["properties"]["items"]["items"]
    assert set(item_schema) == {"$ref"}
    target: Any = document
    for token in item_schema["$ref"].removeprefix("#/").split("/"):
        target = target[token.replace("~1", "/").replace("~0", "~")]
    assert target["type"] == "object"
    assert set(target["required"]) == {
        "provider",
        "model",
        "input_rate",
        "output_rate",
    }
    assert target["properties"]["provider"]["minLength"] == 1
    assert target["properties"]["model"]["minLength"] == 1
    assert target["properties"]["input_rate"]["minimum"] == 0
    assert target["properties"]["output_rate"]["minimum"] == 0
    assert operation["responses"]["200"]["content"]["application/json"][
        "schema"
    ]["$ref"].endswith("/PricingListResponse")


def test_pricing_module_has_no_legacy_writer_or_runtime_alias_boundary() -> None:
    source = Path("llm_gateway_core/api/v1/admin_pricing.py").read_text(
        encoding="utf-8"
    )

    for forbidden in (
        "_write_text_atomically",
        "_backup_if_has_comments",
        "reload_providers_config",
        "app.state.cost_rate_registry",
        "app.state.config_loader",
        "from .rules_editor import",
    ):
        assert forbidden not in source
