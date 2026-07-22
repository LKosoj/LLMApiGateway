from __future__ import annotations

import ast
import asyncio
import inspect
import json
from pathlib import Path
from unittest.mock import AsyncMock, Mock, call
import warnings

import httpx
import pytest
from fastapi import FastAPI
from jsonschema import Draft202012Validator
from pydantic import ValidationError

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
    from llm_gateway_core.api.v1 import rules_editor
    from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.config_store import (
    ConfigDocument,
    ConfigFile,
    ConfigSourceBundle,
)
from llm_gateway_core.config.loader import (
    ConfigLoader,
    ProviderDetails,
    SubscriptionQuotaConfig,
)
from llm_gateway_core.middleware.auth import ROLE_MASTER
from llm_gateway_core.services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
    ConfigUpdateResult,
)
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.runtime_config import RuntimeSnapshot
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _bundle(tmp_path: Path, contents: dict[ConfigFile, bytes]) -> ConfigSourceBundle:
    documents: dict[ConfigFile, ConfigDocument] = {}
    for config_file in ConfigFile:
        path = tmp_path / f"{config_file.value}.json"
        if config_file in contents:
            documents[config_file] = ConfigDocument.from_bytes(
                config_file,
                path,
                contents[config_file],
            )
        elif config_file in {ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES}:
            documents[config_file] = ConfigDocument.from_bytes(
                config_file,
                path,
                b"[]\n",
            )
        else:
            documents[config_file] = ConfigDocument.missing(config_file, path)
    return ConfigSourceBundle(documents)


def _loader(
    tmp_path: Path,
    *,
    contents: dict[ConfigFile, bytes] | None = None,
    provider_name: str = "provider-a",
) -> ConfigLoader:
    loader = ConfigLoader.from_source_bundle(_bundle(tmp_path, contents or {}))
    loader.providers_config = {
        provider_name: ProviderDetails(
            baseUrl=f"https://{provider_name}.example/v1",
            apikey=f"{provider_name}-key",
        )
    }
    loader._fallback_rules_base = {}
    loader.fallback_rules = {}
    loader.model_rules = {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.router_rules = {}
    return loader


def _coordinator(
    published_snapshot: RuntimeSnapshot,
    *,
    comments_backup: str | None = None,
) -> ConfigUpdateCoordinator:
    coordinator = object.__new__(ConfigUpdateCoordinator)
    coordinator.check_base = Mock()  # type: ignore[method-assign]
    coordinator.update = AsyncMock(  # type: ignore[method-assign]
        return_value=ConfigUpdateResult(
            snapshot=published_snapshot,
            cleanup_pending=False,
            comments_backup=comments_backup,
        )
    )
    return coordinator


def _client(
    *,
    base_snapshot: RuntimeSnapshot,
    coordinator: ConfigUpdateCoordinator,
) -> tuple[FastAPI, httpx.AsyncClient]:
    app = FastAPI()
    app.state.services = make_app_services(
        config_update_coordinator=coordinator,
    )

    @app.middleware("http")
    async def bind_runtime(request, call_next):
        request.state.api_key_role = ROLE_MASTER
        request.state.runtime_snapshot = base_snapshot
        return await call_next(request)

    app.include_router(editor_router, prefix="/v1")
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return app, httpx.AsyncClient(
        transport=transport,
        base_url="http://test",
    )


def test_raw_config_get_uses_snapshot_bytes_and_revision_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        expected = b"// captured generation\r\n[]\r\n"
        loader = _loader(
            tmp_path / "captured",
            contents={ConfigFile.FALLBACK_RULES: expected},
        )
        snapshot = make_runtime_snapshot(generation=7, config_loader=loader)
        coordinator = _coordinator(snapshot)
        app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )
        app.state.config_loader = _loader(
            tmp_path / "legacy-alias",
            contents={ConfigFile.FALLBACK_RULES: b"[]\n"},
            provider_name="legacy-provider",
        )

        response = await client.get("/v1/config/models-rules")

        assert response.status_code == 200
        assert response.content == expected
        assert response.headers["etag"] == (
            f'"fallback_rules:sha256:'
            f'{loader.source_bundle[ConfigFile.FALLBACK_RULES].digest}"'
        )
        assert response.headers["x-config-generation"] == "7"
        assert response.headers["cache-control"] == "no-store"
        await client.aclose()

    run_async(scenario())


def test_structured_provider_get_uses_snapshot_and_keeps_subscription_quota(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        loader = _loader(tmp_path / "captured")
        loader.providers_config = {
            "provider-a": ProviderDetails(
                baseUrl="https://provider-a.example/v1",
                apikey="provider-a-key",
                subscription_quota=SubscriptionQuotaConfig(
                    kind="gemini_cli",
                    token_env="GEMINI_TOKEN",
                ),
            )
        }
        snapshot = make_runtime_snapshot(generation=11, config_loader=loader)
        coordinator = _coordinator(snapshot)
        app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )
        app.state.config_loader = _loader(
            tmp_path / "legacy-alias",
            provider_name="legacy-provider",
        )

        response = await client.get("/v1/config/providers/structured")

        assert response.status_code == 200
        payload = response.json()
        assert [item["name"] for item in payload["providers"]] == ["provider-a"]
        assert payload["providers"][0]["subscription_quota"] == {
            "kind": "gemini_cli",
            "token_env": "GEMINI_TOKEN",
        }
        assert response.headers["x-config-generation"] == "11"
        assert response.headers["cache-control"] == "no-store"
        await client.aclose()

    run_async(scenario())


def test_all_config_get_paths_use_one_captured_graph_and_exact_headers(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        loader = _loader(
            tmp_path / "captured",
            contents={
                ConfigFile.PROVIDERS: b"// providers N\n[]\n",
                ConfigFile.FALLBACK_RULES: b"// fallback N\n[]\n",
                ConfigFile.FUSION_RULES: b"[]\n",
                ConfigFile.ROUTER_RULES: b"[]\n",
                ConfigFile.OPERATION_RULES: b"{}\n",
            },
        )
        loader._fallback_rules_base = {
            "chat-n": {"fallback_models": []},
        }
        loader.fallback_rules = dict(loader._fallback_rules_base)
        loader.fusion_rules = {
            "fusion-n": {"panel": [], "main_model": {}},
        }
        loader.router_rules = {
            "router-n": {"selector_model": "chat-n", "targets": []},
        }
        loader.operation_rules = {
            "embeddings": {"embed-n": {"routes": []}},
        }
        snapshot = make_runtime_snapshot(generation=23, config_loader=loader)
        coordinator = _coordinator(snapshot)
        app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )
        app.state.config_loader = _loader(
            tmp_path / "legacy-alias",
            provider_name="legacy-provider",
        )

        cases = [
            (
                "/v1/config/models-rules",
                ConfigFile.FALLBACK_RULES,
                b"// fallback N\n[]\n",
                None,
            ),
            (
                "/v1/config/model-rules",
                ConfigFile.MODEL_RULES,
                b"{\n}\n",
                None,
            ),
            (
                "/v1/config/providers",
                ConfigFile.PROVIDERS,
                b"// providers N\n[]\n",
                None,
            ),
            (
                "/v1/config/models-rules/structured",
                ConfigFile.FALLBACK_RULES,
                None,
                "chat-n",
            ),
            (
                "/v1/config/fusion-rules/structured",
                ConfigFile.FUSION_RULES,
                None,
                "fusion-n",
            ),
            (
                "/v1/config/router-rules/structured",
                ConfigFile.ROUTER_RULES,
                None,
                "router-n",
            ),
            (
                "/v1/config/model-operations/structured",
                ConfigFile.OPERATION_RULES,
                None,
                "embed-n",
            ),
            (
                "/v1/config/providers/structured",
                ConfigFile.PROVIDERS,
                None,
                "provider-a",
            ),
        ]
        for path, config_file, expected_content, expected_marker in cases:
            response = await client.get(path)
            assert response.status_code == 200, path
            if expected_content is not None:
                assert response.content == expected_content, path
            else:
                assert expected_marker in response.text, path
                assert "legacy-provider" not in response.text, path
            document = loader.source_bundle[config_file]
            revision = (
                f"sha256:{document.digest}" if document.exists else "missing"
            )
            assert response.headers["etag"] == (
                f'"{config_file.value}:{revision}"'
            ), path
            assert response.headers["x-config-generation"] == "23", path
            assert response.headers["cache-control"] == "no-store", path

        await client.aclose()

    run_async(scenario())


def test_model_rules_get_distinguishes_missing_from_present_empty(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        loader = _loader(
            tmp_path / "captured",
            contents={ConfigFile.MODEL_RULES: b""},
        )
        snapshot = make_runtime_snapshot(generation=5, config_loader=loader)
        coordinator = _coordinator(snapshot)
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.get("/v1/config/model-rules")

        assert response.status_code == 200
        assert response.content == b""
        assert response.headers["etag"].startswith(
            '"model_rules:sha256:'
        )
        assert response.headers["etag"] != '"model_rules:missing"'
        await client.aclose()

    run_async(scenario())


@pytest.mark.parametrize(
    ("path", "config_file", "comments_backup"),
    [
        ("/v1/config/models-rules", ConfigFile.FALLBACK_RULES, False),
        ("/v1/config/model-rules", ConfigFile.MODEL_RULES, True),
        ("/v1/config/providers", ConfigFile.PROVIDERS, False),
        ("/v1/ui/providers-config", ConfigFile.PROVIDERS, False),
    ],
)
def test_raw_writers_delegate_exact_bytes_and_backup_policy(
    tmp_path: Path,
    path: str,
    config_file: ConfigFile,
    comments_backup: bool,
) -> None:
    async def scenario() -> None:
        candidate_bytes = b"\xef\xbb\xbf[]\r\n"
        base_loader = _loader(tmp_path / "base")
        published_loader = _loader(
            tmp_path / "published",
            contents={config_file: candidate_bytes},
        )
        base_snapshot = make_runtime_snapshot(
            generation=3,
            config_loader=base_loader,
        )
        published_snapshot = make_runtime_snapshot(
            generation=4,
            config_loader=published_loader,
        )
        backup_name = "config.json.bak.test" if comments_backup else None
        coordinator = _coordinator(
            published_snapshot,
            comments_backup=backup_name,
        )
        app, client = _client(
            base_snapshot=base_snapshot,
            coordinator=coordinator,
        )
        app.state.config_loader = _loader(tmp_path / "legacy-alias")

        response = await client.post(
            path,
            content=candidate_bytes,
            headers={"content-type": "application/octet-stream"},
        )

        assert response.status_code == 200
        expected_body = {
            "message": (
                f"{published_loader.source_bundle[config_file].path.name} "
                "updated successfully."
            ),
        }
        if backup_name is not None:
            expected_body["comments_backup"] = backup_name
        assert response.json() == expected_body
        coordinator.check_base.assert_called_once_with(  # type: ignore[attr-defined]
            base_snapshot=base_snapshot,
            config_file=config_file,
            expected_revision=None,
        )
        coordinator.update.assert_awaited_once_with(  # type: ignore[attr-defined]
            base_snapshot=base_snapshot,
            config_file=config_file,
            candidate_bytes=candidate_bytes,
            expected_revision=None,
            comments_backup=comments_backup,
        )
        assert response.headers["x-config-generation"] == "4"
        assert response.headers["etag"] == (
            f'"{config_file.value}:sha256:'
            f'{published_loader.source_bundle[config_file].digest}"'
        )
        await client.aclose()

    run_async(scenario())


@pytest.mark.parametrize(
    ("path", "config_file", "request_body", "candidate_bytes", "body_tail"),
    [
        (
            "/v1/config/models-rules/structured",
            ConfigFile.FALLBACK_RULES,
            {"rules": []},
            b"[]\n",
            {"rules": []},
        ),
        (
            "/v1/config/fusion-rules/structured",
            ConfigFile.FUSION_RULES,
            {"rules": []},
            b"[]\n",
            {"rules": []},
        ),
        (
            "/v1/config/router-rules/structured",
            ConfigFile.ROUTER_RULES,
            {"rules": []},
            b"[]\n",
            {"rules": [], "chat_models": [], "fallback_chains": {}},
        ),
        (
            "/v1/config/model-operations/structured",
            ConfigFile.OPERATION_RULES,
            {},
            (
                b'{\n  "embeddings": [],\n  "rerank": [],\n'
                b'  "images_generations": [],\n  "images_edits": []\n}\n'
            ),
            {
                "embeddings": [],
                "rerank": [],
                "images_generations": [],
                "images_edits": [],
            },
        ),
    ],
)
def test_structured_rule_writers_use_canonical_candidate_and_backup(
    tmp_path: Path,
    path: str,
    config_file: ConfigFile,
    request_body: dict[str, object],
    candidate_bytes: bytes,
    body_tail: dict[str, object],
) -> None:
    async def scenario() -> None:
        base_loader = _loader(tmp_path / "base")
        published_loader = _loader(
            tmp_path / "published",
            contents={config_file: candidate_bytes},
        )
        base_snapshot = make_runtime_snapshot(
            generation=17,
            config_loader=base_loader,
        )
        published_snapshot = make_runtime_snapshot(
            generation=18,
            config_loader=published_loader,
        )
        backup_name = f"{config_file.value}.json.bak.test"
        coordinator = _coordinator(
            published_snapshot,
            comments_backup=backup_name,
        )
        _app, client = _client(
            base_snapshot=base_snapshot,
            coordinator=coordinator,
        )

        response = await client.post(path, json=request_body)

        assert response.status_code == 200
        assert response.json() == {
            "message": (
                f"{published_loader.source_bundle[config_file].path.name} "
                "updated successfully."
            ),
            **body_tail,
            "comments_backup": backup_name,
        }
        coordinator.check_base.assert_called_once_with(  # type: ignore[attr-defined]
            base_snapshot=base_snapshot,
            config_file=config_file,
            expected_revision=None,
        )
        update_kwargs = coordinator.update.await_args.kwargs  # type: ignore[attr-defined]
        assert update_kwargs["base_snapshot"] is base_snapshot
        assert update_kwargs["config_file"] is config_file
        assert update_kwargs["candidate_bytes"] == candidate_bytes
        assert update_kwargs["expected_revision"] is None
        assert update_kwargs["comments_backup"] is True
        if config_file is ConfigFile.FALLBACK_RULES:
            assert callable(update_kwargs["preflight"])
        else:
            assert "preflight" not in update_kwargs
        assert response.headers["x-config-generation"] == "18"
        await client.aclose()

    run_async(scenario())


def test_structured_provider_writer_round_trips_all_supported_metadata(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_body = {
            "providers": [
                {
                    "name": "provider-b",
                    "baseUrl": "https://provider-b.example/v1",
                    "apikey": "provider-key",
                    "type": "anthropic",
                    "proxy": "http://proxy.example:8080",
                    "models": {"model-b": {"context_length": 12345}},
                    "available_models": ["model-b"],
                    "subscription_quota": {
                        "kind": "antigravity",
                        "token_env": "ANTIGRAVITY_TOKEN",
                    },
                    "routing": {
                        "strategy": "priority",
                        "session_affinity": True,
                    },
                    "upstream_key_pools": {
                        "pool-b": {
                            "keys": [
                                {"id": "key-b", "apikey": "pool-key"}
                            ]
                        }
                    },
                }
            ]
        }
        structured = rules_editor.StructuredProvidersPayload.model_validate(
            request_body
        )
        candidate_bytes = rules_editor._serialize_structured_providers(
            structured.providers
        ).encode("utf-8")
        base_loader = _loader(tmp_path / "base")
        published_loader = _loader(
            tmp_path / "published",
            contents={ConfigFile.PROVIDERS: candidate_bytes},
        )
        provider_source = json.loads(candidate_bytes)[0]["provider-b"]
        published_loader.providers_config = {
            "provider-b": ProviderDetails.model_validate(provider_source)
        }
        base_snapshot = make_runtime_snapshot(
            generation=31,
            config_loader=base_loader,
        )
        published_snapshot = make_runtime_snapshot(
            generation=32,
            config_loader=published_loader,
        )
        coordinator = _coordinator(
            published_snapshot,
            comments_backup="providers.json.bak.test",
        )
        _app, client = _client(
            base_snapshot=base_snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            "/v1/config/providers/structured",
            json=request_body,
        )

        assert response.status_code == 200
        provider = response.json()["providers"][0]
        assert provider["name"] == "provider-b"
        assert provider["models"] == {
            "model-b": {"context_length": 12345}
        }
        assert provider["available_models"] == ["model-b"]
        assert provider["subscription_quota"] == {
            "kind": "antigravity",
            "token_env": "ANTIGRAVITY_TOKEN",
        }
        assert provider["routing"]["strategy"] == "priority"
        assert provider["routing"]["session_affinity"] is True
        assert provider["upstream_key_pools"]["pool-b"]["keys"][0][
            "id"
        ] == "key-b"
        update_kwargs = coordinator.update.await_args.kwargs  # type: ignore[attr-defined]
        assert update_kwargs == {
            "base_snapshot": base_snapshot,
            "config_file": ConfigFile.PROVIDERS,
            "candidate_bytes": candidate_bytes,
            "expected_revision": None,
            "comments_backup": True,
        }
        await client.aclose()

    run_async(scenario())


def test_structured_grammar_failure_precedes_broken_coordinator(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=_loader(tmp_path),
        )
        coordinator = _coordinator(snapshot)
        coordinator.check_base.side_effect = ConfigUpdateError(  # type: ignore[attr-defined]
            ConfigUpdateErrorCode.UPDATE_BROKEN
        )
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            "/v1/config/providers/structured",
            content=b'{"providers": [',
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json() == {
            "detail": {
                "code": "config_request_invalid",
                "message": "The configuration request is invalid.",
            }
        }
        coordinator.check_base.assert_not_called()  # type: ignore[attr-defined]
        coordinator.update.assert_not_awaited()  # type: ignore[attr-defined]
        await client.aclose()

    run_async(scenario())


@pytest.mark.parametrize(
    "path",
    [
        "/v1/config/models-rules/structured",
        "/v1/config/fusion-rules/structured",
        "/v1/config/router-rules/structured",
        "/v1/config/model-operations/structured",
        "/v1/config/providers/structured",
    ],
)
def test_deeply_nested_structured_envelope_is_safe_request_invalid(
    tmp_path: Path,
    path: str,
) -> None:
    async def scenario() -> None:
        snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=_loader(tmp_path),
        )
        coordinator = _coordinator(snapshot)
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            path,
            content=("[" * 10_000 + "]" * 10_000).encode(),
            headers={"content-type": "application/json"},
        )

        assert response.status_code == 400
        assert response.json() == {
            "detail": {
                "code": "config_request_invalid",
                "message": "The configuration request is invalid.",
            }
        }
        coordinator.check_base.assert_not_called()  # type: ignore[attr-defined]
        coordinator.update.assert_not_awaited()  # type: ignore[attr-defined]
        await client.aclose()

    run_async(scenario())


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        (ConfigUpdateErrorCode.GENERATION_STALE, 409),
        (ConfigUpdateErrorCode.REVISION_CONFLICT, 409),
        (ConfigUpdateErrorCode.UPDATE_UNAVAILABLE, 503),
        (ConfigUpdateErrorCode.UPDATE_BROKEN, 503),
    ],
)
def test_structured_base_failure_precedes_semantic_validation(
    tmp_path: Path,
    error_code: ConfigUpdateErrorCode,
    status_code: int,
) -> None:
    async def scenario() -> None:
        snapshot = make_runtime_snapshot(
            generation=9,
            config_loader=_loader(tmp_path),
        )
        coordinator = _coordinator(snapshot)
        coordinator.check_base.side_effect = ConfigUpdateError(  # type: ignore[attr-defined]
            error_code
        )
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            "/v1/config/providers/structured",
            json={
                "providers": [
                    {
                        "name": "provider-a",
                        "baseUrl": "https://provider-a.example/v1",
                        "unexpected": "semantic-error",
                    }
                ]
            },
        )

        assert response.status_code == status_code
        assert response.json()["detail"]["code"] == error_code.value
        coordinator.check_base.assert_called_once()  # type: ignore[attr-defined]
        coordinator.update.assert_not_awaited()  # type: ignore[attr-defined]
        await client.aclose()

    run_async(scenario())


def test_structured_semantic_failure_is_safe_and_does_not_build_candidate(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        snapshot = make_runtime_snapshot(
            generation=2,
            config_loader=_loader(tmp_path),
        )
        coordinator = _coordinator(snapshot)
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            "/v1/config/providers/structured",
            json={
                "providers": [
                    {
                        "name": "provider-a",
                        "baseUrl": "https://provider-a.example/v1",
                        "secret-extra": "must-not-leak",
                    }
                ]
            },
        )

        assert response.status_code == 400
        assert response.json() == {
            "detail": {
                "code": "config_validation_failed",
                "message": "Configuration validation failed.",
                "errors": [
                    {
                        "type": "extra_forbidden",
                        "loc": ["providers", 0, "secret-extra"],
                        "msg": "Extra inputs are not permitted",
                    }
                ],
            }
        }
        assert "must-not-leak" not in response.text
        coordinator.check_base.assert_called_once()  # type: ignore[attr-defined]
        coordinator.update.assert_not_awaited()  # type: ignore[attr-defined]
        await client.aclose()

    run_async(scenario())


def test_raw_writer_passes_strong_if_match_to_both_cas_checks(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        base_loader = _loader(tmp_path / "base")
        candidate = b"[]\n"
        published_loader = _loader(
            tmp_path / "published",
            contents={ConfigFile.PROVIDERS: candidate},
        )
        base_snapshot = make_runtime_snapshot(
            generation=40,
            config_loader=base_loader,
        )
        published_snapshot = make_runtime_snapshot(
            generation=41,
            config_loader=published_loader,
        )
        coordinator = _coordinator(published_snapshot)
        _app, client = _client(
            base_snapshot=base_snapshot,
            coordinator=coordinator,
        )
        digest = base_loader.source_bundle[ConfigFile.PROVIDERS].digest
        expected_revision = ConfigRevision(ConfigFile.PROVIDERS, digest)

        response = await client.post(
            "/v1/config/providers",
            content=candidate,
            headers={
                "if-match": f'"providers:sha256:{digest}"',
                "content-type": "text/plain",
            },
        )

        assert response.status_code == 200
        coordinator.check_base.assert_called_once_with(  # type: ignore[attr-defined]
            base_snapshot=base_snapshot,
            config_file=ConfigFile.PROVIDERS,
            expected_revision=expected_revision,
        )
        assert coordinator.update.await_args.kwargs[  # type: ignore[attr-defined]
            "expected_revision"
        ] == expected_revision
        await client.aclose()

    run_async(scenario())


@pytest.mark.parametrize(
    ("error_code", "status_code"),
    [
        (ConfigUpdateErrorCode.VALIDATION_FAILED, 400),
        (ConfigUpdateErrorCode.GENERATION_STALE, 409),
        (ConfigUpdateErrorCode.REVISION_CONFLICT, 409),
        (ConfigUpdateErrorCode.GENERATION_BUSY, 409),
        (ConfigUpdateErrorCode.COMMIT_FAILED, 500),
        (ConfigUpdateErrorCode.UPDATE_UNAVAILABLE, 503),
        (ConfigUpdateErrorCode.UPDATE_BROKEN, 503),
    ],
)
def test_writer_maps_coordinator_failures_to_safe_contract(
    tmp_path: Path,
    error_code: ConfigUpdateErrorCode,
    status_code: int,
) -> None:
    async def scenario() -> None:
        snapshot = make_runtime_snapshot(
            generation=6,
            config_loader=_loader(tmp_path),
        )
        coordinator = _coordinator(snapshot)
        coordinator.update.side_effect = ConfigUpdateError(error_code)  # type: ignore[attr-defined]
        _app, client = _client(
            base_snapshot=snapshot,
            coordinator=coordinator,
        )

        response = await client.post(
            "/v1/config/providers",
            content=b"secret-candidate",
        )

        assert response.status_code == status_code
        assert response.json()["detail"]["code"] == error_code.value
        assert "secret-candidate" not in response.text
        await client.aclose()

    run_async(scenario())


def test_structured_fallback_preflight_uses_candidate_catalog_and_clients(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        request_body = {
            "rules": [
                {
                    "gateway_model_name": "gateway-shared",
                    "fallback_models": [
                        {"provider": "shared", "model": "model-a"},
                        {"provider": "shared", "model": "model-b"},
                    ],
                },
                {
                    "gateway_model_name": "gateway-proxy",
                    "fallback_models": [
                        {"provider": "proxied", "model": "model-c"}
                    ],
                    "context_overflow_fallback": {
                        "provider": "proxied",
                        "model": "model-d",
                    },
                },
            ]
        }
        payload = rules_editor.StructuredRulesPayload.model_validate(
            request_body
        )
        candidate_bytes = rules_editor._serialize_structured_rules(
            payload.rules
        ).encode("utf-8")
        base_loader = _loader(tmp_path / "base", provider_name="captured")
        candidate_loader = _loader(
            tmp_path / "candidate",
            contents={ConfigFile.FALLBACK_RULES: candidate_bytes},
        )
        candidate_loader.providers_config = {
            "shared": ProviderDetails(
                baseUrl="https://shared.example/v1",
                apikey="shared-key",
            ),
            "proxied": ProviderDetails(
                baseUrl="https://proxied.example/v1",
                apikey="proxied-key",
                proxy="http://proxy.example:8080",
            ),
        }
        candidate_loader._fallback_rules_base = {
            "gateway-shared": {
                "fallback_models": [
                    {"provider": "shared", "model": "model-a"},
                    {"provider": "shared", "model": "model-b"},
                ]
            },
            "gateway-proxy": {
                "fallback_models": [
                    {"provider": "proxied", "model": "model-c"}
                ],
                "context_overflow_fallback": {
                    "provider": "proxied",
                    "model": "model-d",
                },
            },
        }
        candidate_loader.fallback_rules = dict(
            candidate_loader._fallback_rules_base
        )
        candidate_catalog = ProviderModelsService()
        candidate_catalog.get_models = AsyncMock(  # type: ignore[method-assign]
            side_effect=[
                ["model-a", "model-b"],
                ["model-c", "model-d"],
            ]
        )
        captured_catalog = ProviderModelsService()
        captured_catalog.get_models = AsyncMock()  # type: ignore[method-assign]
        proxy_client = Mock(spec=httpx.AsyncClient, name="candidate-proxy")
        base_snapshot = make_runtime_snapshot(
            generation=51,
            config_loader=base_loader,
            provider_models_service=captured_catalog,
        )
        candidate_snapshot = make_runtime_snapshot(
            generation=52,
            config_loader=candidate_loader,
            provider_models_service=candidate_catalog,
            proxy_http_clients={"proxied": proxy_client},
        )
        coordinator = _coordinator(candidate_snapshot)

        async def update_with_preflight(**kwargs):
            await kwargs["preflight"](candidate_snapshot)
            return ConfigUpdateResult(
                snapshot=candidate_snapshot,
                cleanup_pending=False,
            )

        coordinator.update.side_effect = update_with_preflight  # type: ignore[attr-defined]
        app, client = _client(
            base_snapshot=base_snapshot,
            coordinator=coordinator,
        )
        shared_client = app.state.services.http_client
        app.state.provider_models_service = Mock(name="legacy-catalog")
        app.state.http_client = Mock(name="legacy-shared-client")
        app.state.proxy_http_clients = {
            "proxied": Mock(name="legacy-proxy-client")
        }

        response = await client.post(
            "/v1/config/models-rules/structured",
            json=request_body,
        )

        assert response.status_code == 200
        # Parallel gather doesn't guarantee call order — compare as a set.
        expected_calls = [
            call(
                "shared",
                candidate_loader.providers_config["shared"],
                shared_client,
                auth_headers={"Authorization": "Bearer shared-key"},
            ),
            call(
                "proxied",
                candidate_loader.providers_config["proxied"],
                proxy_client,
                auth_headers={"Authorization": "Bearer proxied-key"},
            ),
        ]
        actual_calls = candidate_catalog.get_models.await_args_list  # type: ignore[attr-defined]
        assert len(actual_calls) == len(expected_calls)
        for expected in expected_calls:
            assert expected in actual_calls
        captured_catalog.get_models.assert_not_awaited()  # type: ignore[attr-defined]
        await client.aclose()

    run_async(scenario())


def test_validate_candidate_provider_models_names_missing_pair_in_exception(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        candidate_loader = _loader(
            tmp_path / "candidate", provider_name="shared"
        )
        candidate_loader._fallback_rules_base = {
            "gateway-shared": {
                "fallback_models": [
                    {"provider": "shared", "model": "model-a"},
                ]
            }
        }
        candidate_loader.fallback_rules = dict(
            candidate_loader._fallback_rules_base
        )
        candidate_catalog = ProviderModelsService()
        candidate_catalog.get_models = AsyncMock(  # type: ignore[method-assign]
            return_value=["some-other-model"]
        )
        candidate_snapshot = make_runtime_snapshot(
            generation=99,
            config_loader=candidate_loader,
            provider_models_service=candidate_catalog,
        )
        request = Mock()
        request.headers = {}
        request.state = Mock()
        request.state.api_key_record = None
        request.state.role = ROLE_MASTER

        with pytest.raises(ValueError) as raised:
            await rules_editor._validate_candidate_provider_models(
                request,
                candidate_snapshot,
                Mock(spec=httpx.AsyncClient),
            )

        assert "model-a" in str(raised.value)
        assert "shared" in str(raised.value)

    run_async(scenario())


def test_validate_candidate_reuses_base_cache_for_unchanged_provider(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        base_loader = _loader(tmp_path / "base", provider_name="shared")
        base_loader._fallback_rules_base = {}
        base_loader.fallback_rules = {}
        candidate_loader = _loader(
            tmp_path / "candidate", provider_name="shared"
        )
        candidate_loader._fallback_rules_base = {
            "gateway-shared": {
                "fallback_models": [
                    {"provider": "shared", "model": "model-a"},
                ]
            }
        }
        candidate_loader.fallback_rules = dict(
            candidate_loader._fallback_rules_base
        )

        from llm_gateway_core.services.provider_models import (
            ProviderModelsCacheEntry,
        )

        warm_entry = ProviderModelsCacheEntry(
            models=["model-a", "model-b"],
            entries={"model-a": {"id": "model-a"}, "model-b": {"id": "model-b"}},
            fetched_at=100.0,
        )
        base_catalog = ProviderModelsService(time_func=lambda: 100.0)
        base_catalog.install_cache_entry("shared", warm_entry)
        candidate_catalog = ProviderModelsService(time_func=lambda: 100.0)

        async def refuse_fetch(*_a, **_kw):
            raise AssertionError(
                "candidate service must reuse the base cache and not fetch"
            )

        candidate_catalog._fetch_model_entries = refuse_fetch  # type: ignore[method-assign]

        base_snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=base_loader,
            provider_models_service=base_catalog,
        )
        candidate_snapshot = make_runtime_snapshot(
            generation=2,
            config_loader=candidate_loader,
            provider_models_service=candidate_catalog,
        )
        request = Mock()
        request.headers = {}
        request.state = Mock()
        request.state.api_key_record = None
        request.state.role = ROLE_MASTER

        await rules_editor._validate_candidate_provider_models(
            request,
            candidate_snapshot,
            Mock(spec=httpx.AsyncClient),
            base_snapshot=base_snapshot,
        )
        # Warm entry was grafted onto the candidate cache, so get_models
        # returns from memory without hitting the (booby-trapped) fetcher.
        assert candidate_catalog._cache["shared"] is warm_entry

    run_async(scenario())


def test_validate_candidate_loads_all_providers_concurrently(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        candidate_loader = _loader(
            tmp_path / "candidate", provider_name="shared"
        )
        candidate_loader.providers_config = {
            "alpha": ProviderDetails(
                baseUrl="https://alpha.example/v1", apikey="alpha-key"
            ),
            "beta": ProviderDetails(
                baseUrl="https://beta.example/v1", apikey="beta-key"
            ),
        }
        candidate_loader._fallback_rules_base = {
            "gw": {
                "fallback_models": [
                    {"provider": "alpha", "model": "m"},
                    {"provider": "beta", "model": "m"},
                ]
            }
        }
        candidate_loader.fallback_rules = dict(
            candidate_loader._fallback_rules_base
        )

        both_started = asyncio.Event()
        started = 0

        async def slow_get(name, cfg, http_client, auth_headers=None):
            nonlocal started
            started += 1
            if started == 2:
                both_started.set()
            # Wait for both providers to be in-flight before returning.
            # Sequential code would deadlock here; parallel gather will not.
            await asyncio.wait_for(both_started.wait(), timeout=1.0)
            return ["m"]

        candidate_catalog = ProviderModelsService()
        candidate_catalog.get_models = AsyncMock(  # type: ignore[method-assign]
            side_effect=slow_get
        )
        candidate_snapshot = make_runtime_snapshot(
            generation=1,
            config_loader=candidate_loader,
            provider_models_service=candidate_catalog,
        )
        request = Mock()
        request.headers = {}
        request.state = Mock()
        request.state.api_key_record = None
        request.state.role = ROLE_MASTER

        await rules_editor._validate_candidate_provider_models(
            request,
            candidate_snapshot,
            Mock(spec=httpx.AsyncClient),
        )
        assert candidate_catalog.get_models.await_count == 2  # type: ignore[attr-defined]

    run_async(scenario())


def test_structured_request_openapi_contract_remains_explicit() -> None:
    app = FastAPI()
    app.include_router(editor_router, prefix="/v1")
    paths = app.openapi()["paths"]

    raw_body = paths["/v1/config/models-rules"]["post"]["requestBody"]
    assert raw_body == {
        "required": True,
        "content": {
            "text/plain": {
                "schema": {"title": "Payload Text", "type": "string"}
            }
        },
    }
    for path, property_name in [
        ("/v1/config/models-rules/structured", "rules"),
        ("/v1/config/fusion-rules/structured", "rules"),
        ("/v1/config/router-rules/structured", "rules"),
        ("/v1/config/providers/structured", "providers"),
    ]:
        request_body = paths[path]["post"]["requestBody"]
        assert request_body["required"] is True
        schema = request_body["content"]["application/json"]["schema"]
        assert property_name in schema["properties"]
        Draft202012Validator.check_schema(schema)
        serialized_schema = json.dumps(schema, sort_keys=True)
        assert '"$defs"' not in serialized_schema
        assert '"$ref"' not in serialized_schema
    provider_schema = paths["/v1/config/providers/structured"]["post"][
        "requestBody"
    ]["content"]["application/json"]["schema"]
    provider_item_schema = provider_schema["properties"]["providers"]["items"]
    assert provider_item_schema["additionalProperties"] is False
    assert "subscription_quota" in provider_item_schema["properties"]
    operation_schema = paths[
        "/v1/config/model-operations/structured"
    ]["post"]["requestBody"]["content"]["application/json"]["schema"]
    assert operation_schema == {
        "type": "object",
        "additionalProperties": True,
    }


def test_config_writers_have_no_legacy_storage_or_runtime_ownership() -> None:
    writer_names = {
        "save_models_rules",
        "save_model_rules",
        "save_models_rules_structured",
        "save_fusion_rules_structured",
        "save_router_rules_structured",
        "save_operation_rules_structured",
        "save_providers_structured",
        "save_providers_config",
    }
    module_tree = ast.parse(inspect.getsource(rules_editor))
    writers = {
        node.name: node
        for node in module_tree.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name in writer_names
    }
    assert set(writers) == writer_names
    forbidden_calls = {
        "_write_text_atomically",
        "_backup_if_has_comments",
        "load_model_rules",
        "reload_operation_rules",
        "create_proxy_http_clients",
        "close_http_clients",
        "OperationDispatcher",
        "clear",
        "publish",
        "replace",
    }
    for writer_name, writer in writers.items():
        for node in ast.walk(writer):
            if not isinstance(node, ast.Call):
                continue
            function = node.func
            call_name = (
                function.id
                if isinstance(function, ast.Name)
                else function.attr
                if isinstance(function, ast.Attribute)
                else None
            )
            assert call_name not in forbidden_calls, (
                writer_name,
                call_name,
            )
        assignment_targets = [
            target
            for node in ast.walk(writer)
            if isinstance(node, (ast.Assign, ast.AnnAssign, ast.AugAssign))
            for target in (
                node.targets if isinstance(node, ast.Assign) else [node.target]
            )
        ]
        assert all(
            "request.app.state" not in ast.unparse(target)
            for target in assignment_targets
        ), writer_name


def test_unsupported_provider_auth_is_preserved_for_explicit_rejection(
    tmp_path: Path,
) -> None:
    item = rules_editor.StructuredProviderItem.model_validate(
        {
            "name": "provider-a",
            "baseUrl": "https://provider-a.example/v1",
            "apikey": "provider-key",
            "auth": {"type": "bearer", "token": "secret-token"},
        }
    )
    payload_text = rules_editor._serialize_structured_providers([item])

    assert json.loads(payload_text)[0]["provider-a"]["auth"] == {
        "type": "bearer",
        "token": "secret-token",
    }
    loader = _loader(tmp_path)
    with pytest.raises(ValidationError, match="auth.*no longer supported"):
        loader.parse_and_validate_providers_payload(payload_text)


def test_structured_provider_schema_preserves_quota_and_rejects_unknown_fields() -> None:
    item = rules_editor.StructuredProviderItem.model_validate(
        {
            "name": "provider-a",
            "baseUrl": "https://provider-a.example/v1",
            "subscription_quota": {
                "kind": "github_copilot",
                "token_env": "COPILOT_TOKEN",
            },
        }
    )

    serialized = json.loads(rules_editor._serialize_structured_providers([item]))
    assert serialized[0]["provider-a"]["subscription_quota"] == {
        "kind": "github_copilot",
        "token_env": "COPILOT_TOKEN",
    }
    with pytest.raises(ValidationError):
        rules_editor.StructuredProviderItem.model_validate(
            {
                "name": "provider-a",
                "baseUrl": "https://provider-a.example/v1",
                "unexpected": "must-not-be-ignored",
            }
        )
