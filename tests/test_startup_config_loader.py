from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

import main
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionIntegrityError,
    ConfigFile,
    ConfigSourceBundle,
)
from llm_gateway_core.config.loader import ConfigError, ConfigLoader
from tests._async_compat import run_async


FILENAMES = {
    ConfigFile.PROVIDERS: "providers.json",
    ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
    ConfigFile.MODEL_RULES: "models_model_rules.json",
    ConfigFile.OPERATION_RULES: "models_operation_rules.json",
    ConfigFile.FUSION_RULES: "models_fusion_rules.json",
    ConfigFile.ROUTER_RULES: "models_router_rules.json",
}
ENV_NAMES = {
    ConfigFile.PROVIDERS: "PROVIDERS_FILENAME",
    ConfigFile.FALLBACK_RULES: "FALLBACK_RULES_FILENAME",
    ConfigFile.MODEL_RULES: "MODEL_RULES_FILENAME",
    ConfigFile.OPERATION_RULES: "OPERATION_RULES_FILENAME",
    ConfigFile.FUSION_RULES: "FUSION_RULES_FILENAME",
    ConfigFile.ROUTER_RULES: "ROUTER_RULES_FILENAME",
}
PAYLOADS = {
    ConfigFile.PROVIDERS: [
        {"primary": {"baseUrl": "https://primary.example", "apikey": "DIRECT-KEY"}}
    ],
    ConfigFile.FALLBACK_RULES: [
        {
            "gateway_model_name": "gateway/chat",
            "fallback_models": [{"provider": "primary", "model": "upstream"}],
        }
    ],
    ConfigFile.MODEL_RULES: {"aliases": {"gateway/alias": "gateway/chat"}},
    ConfigFile.OPERATION_RULES: {},
    ConfigFile.FUSION_RULES: [],
    ConfigFile.ROUTER_RULES: [],
}


def _write_sources(root: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for config_file, payload in PAYLOADS.items():
        path = root / FILENAMES[config_file]
        path.write_text(json.dumps(payload), encoding="utf-8")
        environment[ENV_NAMES[config_file]] = str(path)
    return environment


def _graph(loader: ConfigLoader) -> dict[str, object]:
    return {
        "providers": {
            name: details.model_dump(mode="json")
            for name, details in loader.providers_config.items()
        },
        "fallback_base": loader._fallback_rules_base,  # noqa: SLF001
        "fallback": loader.fallback_rules,
        "model": loader.model_rules,
        "operation": loader.operation_rules,
        "fusion": loader.fusion_rules,
        "router": loader.router_rules,
    }


def test_load_initial_config_uses_one_complete_entry_without_legacy_calls() -> None:
    loader = Mock(spec=ConfigLoader)
    configured_paths = {
        config_file: Path(f"/tmp/{FILENAMES[config_file]}")
        for config_file in ConfigFile
    }
    loader.configured_paths = configured_paths
    events: list[str] = []

    def recover(paths) -> int:
        assert paths is configured_paths
        events.append("recover")
        return 0

    def load_complete() -> ConfigLoader:
        events.append("load")
        return loader

    loader.load_complete.side_effect = load_complete

    with (
        patch("main.ConfigLoader", return_value=loader) as loader_factory,
        patch.object(
            AtomicConfigFileTransaction,
            "recover_pending",
            side_effect=recover,
        ) as recover_pending,
    ):
        result = main._load_initial_config()

    assert result is loader
    assert events == ["recover", "load"]
    loader_factory.assert_called_once_with()
    recover_pending.assert_called_once_with(configured_paths)
    loader.load_complete.assert_called_once_with()
    for method_name in (
        "load_providers",
        "load_fallback_rules",
        "load_model_rules",
        "load_operation_rules",
        "load_fusion_rules",
        "load_router_rules",
        "validate_fallback_operation_consistency",
    ):
        getattr(loader, method_name).assert_not_called()


def test_load_initial_config_propagates_exact_config_error() -> None:
    failure = ConfigError("startup-config-failure")
    loader = Mock(spec=ConfigLoader)
    loader.configured_paths = {
        config_file: Path(f"/tmp/{FILENAMES[config_file]}")
        for config_file in ConfigFile
    }
    loader.load_complete.side_effect = failure

    with (
        patch("main.ConfigLoader", return_value=loader),
        patch.object(AtomicConfigFileTransaction, "recover_pending", return_value=0),
    ):
        with pytest.raises(ConfigError) as error:
            main._load_initial_config()

    assert error.value is failure


def test_load_initial_config_captures_all_env_paths_with_bundle_parity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _write_sources(tmp_path)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main.settings, "fallback_provider", "primary")
    recovered_paths: dict[ConfigFile, Path] = {}
    original_capture = ConfigSourceBundle.capture

    def recover(paths) -> int:
        recovered_paths.update(paths)
        for config_file, env_name in ENV_NAMES.items():
            monkeypatch.setenv(
                env_name,
                str(tmp_path / f"ignored-{FILENAMES[config_file]}"),
            )
        return 0

    with (
        patch.object(
            AtomicConfigFileTransaction,
            "recover_pending",
            side_effect=recover,
        ),
        patch.object(
            ConfigSourceBundle,
            "capture",
            wraps=original_capture,
        ) as capture,
    ):
        startup_loader = main._load_initial_config()

    assert capture.call_count == 1
    assert recovered_paths == {
        config_file: Path(environment[ENV_NAMES[config_file]])
        for config_file in ConfigFile
    }
    assert dict(startup_loader.configured_paths) == recovered_paths
    with pytest.raises(TypeError):
        startup_loader.configured_paths[ConfigFile.PROVIDERS] = tmp_path  # type: ignore[index]
    expected_bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={
            config_file: environment[ENV_NAMES[config_file]]
            for config_file in ConfigFile
        },
    )
    expected_loader = ConfigLoader.from_source_bundle(expected_bundle).load_complete()

    assert startup_loader.source_bundle is not None
    assert {
        config_file: startup_loader.source_bundle[config_file].path
        for config_file in ConfigFile
    } == {
        config_file: Path(environment[ENV_NAMES[config_file]])
        for config_file in ConfigFile
    }
    assert _graph(startup_loader) == _graph(expected_loader)


def test_load_initial_config_recovers_valid_commit_journal_before_capture(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    environment = _write_sources(tmp_path)
    for name, value in environment.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(main.settings, "fallback_provider", "primary")
    bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={
            config_file: environment[ENV_NAMES[config_file]]
            for config_file in ConfigFile
        },
    )
    candidate = json.dumps(
        {"aliases": {"gateway/recovered": "gateway/chat"}},
        separators=(",", ":"),
    ).encode()
    transaction = AtomicConfigFileTransaction.begin(
        bundle[ConfigFile.MODEL_RULES],
        candidate,
    )
    transaction.prepare()
    transaction.commit()
    assert tuple(tmp_path.glob(".llmgateway-config-txn-*.journal.commit"))

    loader = main._load_initial_config()

    assert loader.model_rules["aliases"] == {
        "gateway/recovered": "gateway/chat"
    }
    assert (tmp_path / FILENAMES[ConfigFile.MODEL_RULES]).read_bytes() == candidate
    assert tuple(tmp_path.glob(".llmgateway-config-txn-*")) == ()
    del transaction


def test_recovery_integrity_error_prevents_load_and_preserves_safe_identity() -> None:
    failure = AtomicConfigTransactionIntegrityError(
        ConfigFile.MODEL_RULES,
        "startup journal state is ambiguous",
    )
    loader = Mock(spec=ConfigLoader)
    loader.configured_paths = {
        config_file: Path(f"/secret-root/{FILENAMES[config_file]}")
        for config_file in ConfigFile
    }

    with (
        patch("main.ConfigLoader", return_value=loader),
        patch.object(
            AtomicConfigFileTransaction,
            "recover_pending",
            side_effect=failure,
        ),
    ):
        with pytest.raises(AtomicConfigTransactionIntegrityError) as raised:
            main._load_initial_config()

    assert raised.value is failure
    assert str(raised.value) == (
        "model_rules: startup journal state is ambiguous"
    )
    assert "secret-root" not in str(raised.value)
    loader.load_complete.assert_not_called()


def test_lifespan_config_failure_precedes_client_and_runtime_candidate(
    tmp_path: Path,
) -> None:
    failure = ConfigError("startup-config-failure")
    shared_client_factory = Mock()
    snapshot_builder = Mock()

    with (
        patch("main.ensure_gateway_api_key_configured"),
        patch("main.OUTPUTS_IMAGES_DIR", tmp_path / "outputs" / "images"),
        patch("main.preload_templates"),
        patch("main._load_initial_config", side_effect=failure),
        patch("main.create_shared_http_client", shared_client_factory),
        patch("main._build_initial_snapshot", snapshot_builder),
    ):
        lifespan_context = main.lifespan(main.app)
        with pytest.raises(ConfigError) as error:
            run_async(lifespan_context.__aenter__())

    assert error.value is failure
    shared_client_factory.assert_not_called()
    snapshot_builder.assert_not_called()
