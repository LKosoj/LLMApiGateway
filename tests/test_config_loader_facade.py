from __future__ import annotations

import ast
import inspect
from pathlib import Path
from unittest.mock import Mock

from llm_gateway_core.config import loader as loader_module
from llm_gateway_core.config import loading, schemas, validation
from llm_gateway_core.config.config_store import ConfigFile, ConfigSourceBundle
from llm_gateway_core.config.schema_validation import empty_operation_rules
from llm_gateway_core.config.settings import settings


LEGACY_EXPORTS = (
    "ANTHROPIC_API_VERSION",
    "ConfigError",
    "ConfigLoader",
    "FallbackModelRule",
    "FusionModelConfig",
    "ModelsOperationConfig",
    "OperationRoute",
    "ProviderDetails",
    "SubscriptionQuotaConfig",
    "resolve_provider_api_key",
    "resolve_provider_api_key_value",
    "resolve_provider_config_api_key",
    "resolve_provider_config_api_keys",
    "resolve_provider_config_auth_headers",
    "resolve_provider_proxy",
    "settings",
)
PUBLIC_METHOD_PARAMETERS = {
    "from_source_bundle": ("bundle",),
    "bind_persisted_source_bundle": ("self", "bundle"),
    "load_complete": ("self",),
    "load_providers": ("self",),
    "load_model_rules": ("self",),
    "load_fallback_rules": ("self",),
    "load_fusion_rules": ("self",),
    "load_router_rules": ("self",),
    "load_operation_rules": ("self", "filename"),
    "parse_and_validate_providers_payload": ("self", "payload_text", "strict_env"),
    "parse_and_validate_fallback_rules_payload": (
        "self",
        "payload_text",
        "providers_config",
    ),
    "parse_and_validate_model_rules_payload": (
        "self",
        "payload_text",
        "providers_config",
        "fallback_rules",
    ),
    "parse_and_validate_model_rules_payload_with_fallbacks": (
        "self",
        "payload_text",
        "providers_config",
        "fallback_rules",
    ),
    "parse_and_validate_operation_routes_payload": (
        "self",
        "payload_text",
        "providers_config",
        "fallback_rules",
    ),
    "parse_and_validate_fusion_rules_payload": (
        "self",
        "payload_text",
        "providers_config",
    ),
    "parse_and_validate_router_rules_payload": (
        "self",
        "payload_text",
        "fallback_rules",
        "fusion_rules",
    ),
    "validate_fallback_rules_mapping": (
        "self",
        "fallback_rules_to_validate",
        "providers_config",
    ),
    "validate_fusion_rules_mapping": (
        "self",
        "fusion_rules_to_validate",
        "providers_config",
    ),
    "validate_router_rules_mapping": (
        "self",
        "router_rules_to_validate",
        "fallback_rules",
        "fusion_rules",
    ),
    "validate_operation_routes": (
        "self",
        "operation_routes_to_validate",
        "providers_config",
        "fallback_rules",
    ),
    "validate_fallback_operation_consistency": (
        "self",
        "fallback_rules",
        "operation_rules",
    ),
}


def test_loader_keeps_legacy_exports_and_settings_patch_seam(
    monkeypatch,
) -> None:
    for name in LEGACY_EXPORTS:
        assert hasattr(loader_module, name), name

    assert loader_module.settings is settings
    assert validation.settings is settings
    monkeypatch.setattr(loader_module.settings, "fallback_provider", "facade-test")
    assert settings.fallback_provider == "facade-test"


def test_loader_reexports_owner_objects_by_identity() -> None:
    schema_exports = (
        "FallbackModelRule",
        "FusionModelConfig",
        "ModelsOperationConfig",
        "OperationRoute",
        "ProviderDetails",
        "SubscriptionQuotaConfig",
    )
    validation_exports = (
        "ConfigError",
        "resolve_provider_api_key",
        "resolve_provider_api_key_value",
        "resolve_provider_config_api_key",
        "resolve_provider_config_api_keys",
        "resolve_provider_config_auth_headers",
        "resolve_provider_proxy",
    )

    for name in schema_exports:
        assert getattr(loader_module, name) is getattr(schemas, name)
    for name in validation_exports:
        assert getattr(loader_module, name) is getattr(validation, name)
    assert loader_module.ConfigLoader.__bases__ == (loading._ConfigLoaderCore,)


def test_loader_keeps_public_method_parameter_contract() -> None:
    loader_type = loader_module.ConfigLoader

    for method_name, parameter_names in PUBLIC_METHOD_PARAMETERS.items():
        method = getattr(loader_type, method_name)
        assert tuple(inspect.signature(method).parameters) == parameter_names

    operation_signature = inspect.signature(loader_type.load_operation_rules)
    assert operation_signature.parameters["filename"].default == "models_operation_rules.json"
    providers_signature = inspect.signature(loader_type.parse_and_validate_providers_payload)
    assert providers_signature.parameters["strict_env"].kind is inspect.Parameter.KEYWORD_ONLY
    assert providers_signature.parameters["strict_env"].default is False


def test_loader_spec_and_initial_state_remain_compatible(tmp_path: Path) -> None:
    loader_type = loader_module.ConfigLoader
    loader = loader_type(
        providers_filename=str(tmp_path / "providers.json"),
        fallback_rules_filename=str(tmp_path / "fallback.json"),
        operation_rules_filename=str(tmp_path / "operation.json"),
        fusion_rules_filename=str(tmp_path / "fusion.json"),
        model_rules_filename=str(tmp_path / "model.json"),
        router_rules_filename=str(tmp_path / "router.json"),
    )
    loader_mock = Mock(spec=loader_type)

    assert callable(loader_mock.load_complete)
    assert callable(loader_mock.validate_fallback_rules_mapping)
    assert dict(loader.configured_paths) == {
        ConfigFile.PROVIDERS: tmp_path / "providers.json",
        ConfigFile.FALLBACK_RULES: tmp_path / "fallback.json",
        ConfigFile.MODEL_RULES: tmp_path / "model.json",
        ConfigFile.OPERATION_RULES: tmp_path / "operation.json",
        ConfigFile.FUSION_RULES: tmp_path / "fusion.json",
        ConfigFile.ROUTER_RULES: tmp_path / "router.json",
    }
    assert loader.providers_config == {}
    assert loader._fallback_rules_base == {}  # noqa: SLF001
    assert loader.fallback_rules == {}
    assert loader.model_rules == {}
    assert loader.operation_rules == empty_operation_rules()
    assert loader.fusion_rules == {}
    assert loader.router_rules == {}


def test_from_source_bundle_returns_facade_subtype(tmp_path: Path) -> None:
    (tmp_path / "providers.json").write_text("[]", encoding="utf-8")
    (tmp_path / "models_fallback_rules.json").write_text("[]", encoding="utf-8")
    bundle = ConfigSourceBundle.capture(tmp_path)

    loader = loader_module.ConfigLoader.from_source_bundle(bundle)

    assert type(loader) is loader_module.ConfigLoader
    assert loader.source_bundle is bundle


def test_production_callers_keep_importing_the_loader_facade() -> None:
    package_root = Path("llm_gateway_core")
    internal_modules = {
        "llm_gateway_core.config.loading",
        "llm_gateway_core.config.schemas",
        "llm_gateway_core.config.validation",
    }
    owner_paths = {
        package_root / "config" / "loader.py",
        package_root / "config" / "loading.py",
        package_root / "config" / "schemas.py",
        package_root / "config" / "validation.py",
    }
    violations: list[str] = []

    for path in package_root.rglob("*.py"):
        if path in owner_paths:
            continue
        package = list(path.with_suffix("").parts[:-1])
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported_modules = {alias.name for alias in node.names}
            elif isinstance(node, ast.ImportFrom):
                if node.level:
                    prefix = package[: len(package) - node.level + 1]
                    imported_modules = {".".join(prefix + (node.module or "").split("."))}
                else:
                    imported_modules = {node.module or ""}
            else:
                continue
            if imported_modules & internal_modules:
                violations.append(f"{path}:{node.lineno}")

    assert violations == []
