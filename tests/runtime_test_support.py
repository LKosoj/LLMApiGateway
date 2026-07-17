"""Safe typed runtime builders shared by endpoint and middleware tests."""

from __future__ import annotations

import atexit
import asyncio
import secrets
import tempfile
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType
from typing import Any
from unittest.mock import create_autospec

import httpx
from fastapi import FastAPI

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.write_batcher import WriteBatcher
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.accounting import AccountingReservation
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.deep_research_process import DeepResearchProcessRunner
from llm_gateway_core.services.fallback_model_evals import FallbackModelEvalService
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from llm_gateway_core.services.health import HealthService
from llm_gateway_core.services.image_retention import (
    ImageRetentionService,
    ImageRetentionSnapshot,
)
from llm_gateway_core.services.image_storage import GeneratedImageStorage
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.services.router_model import RouterModelService
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeSnapshot,
)
from llm_gateway_core.services.stream_observation import (
    STREAM_EVENT_MAX_BYTES,
    StreamObservationCapacity,
)
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.services.upstream_subscription_quota import (
    UpstreamSubscriptionQuotaService,
)
from llm_gateway_core.services.upload_admission import UploadAdmission


_OPTIONAL_APP_SERVICE_FIELDS = frozenset({"ip_block_guard"})
_INSTALLED_RUNTIME_SERVICE_FIELDS = frozenset({"runtime_manager", "task_supervisor"})
_INSTALLED_RUNTIME_SNAPSHOT_FIELDS = frozenset({"generation", "config_loader", "http_client"})
_CONFIG_GRAPH_ATTRIBUTES = (
    "providers_config",
    "fallback_rules",
    "operation_rules",
    "fusion_rules",
    "model_rules",
    "router_rules",
)
_APP_SERVICE_STORAGE_DIRECTORIES: list[tempfile.TemporaryDirectory[str]] = []


def _cleanup_app_service_storage_directories() -> None:
    while _APP_SERVICE_STORAGE_DIRECTORIES:
        _APP_SERVICE_STORAGE_DIRECTORIES.pop().cleanup()


def _field_names(dataclass_type: type[object]) -> frozenset[str]:
    return frozenset(field.name for field in fields(dataclass_type))


def _unknown_overrides(
    overrides: Mapping[str, object],
    *,
    allowed: frozenset[str],
    owner: str,
) -> None:
    unknown = sorted(set(overrides) - allowed)
    if unknown:
        raise TypeError(f"unknown {owner} override(s): {', '.join(unknown)}")


def _reject_mandatory_none(
    overrides: Mapping[str, object],
    *,
    optional: frozenset[str],
) -> None:
    for name, value in overrides.items():
        if value is None and name not in optional:
            raise ValueError(f"{name} cannot be None")


def _strict_instance_double(spec: type[Any]) -> Any:
    return create_autospec(spec, instance=True, spec_set=True)


def _accounting_service_double() -> AccountingService:
    service = _strict_instance_double(AccountingService)

    async def reserve(**kwargs: object) -> AccountingReservation:
        return AccountingReservation(
            reservation_id=secrets.token_hex(16),
            request_id=str(kwargs["request_id"]),
            api_key_id=kwargs.get("api_key_id"),  # type: ignore[arg-type]
            reserved_usd=float(kwargs["estimate_usd"]),
        )

    service.reserve.side_effect = reserve
    service.release.return_value = True
    return service


def make_app_services(**overrides: object) -> AppServices:
    """Build the complete process-lifetime container without external I/O."""
    field_names = _field_names(AppServices)
    _unknown_overrides(overrides, allowed=field_names, owner="AppServices")
    _reject_mandatory_none(overrides, optional=_OPTIONAL_APP_SERVICE_FIELDS)

    values = dict(overrides)
    if "http_client" not in values:
        values["http_client"] = _strict_instance_double(httpx.AsyncClient)
    http_client = values["http_client"]
    if "api_keys_db" not in values:
        api_keys_db = _strict_instance_double(ApiKeysDB)
        api_keys_db.get_by_key.return_value = None
        api_keys_db.get_by_id.return_value = None
        api_keys_db.list_all.return_value = []
        api_keys_db.reset_due_budgets.return_value = []
        values["api_keys_db"] = api_keys_db
    if "rejections_db" not in values:
        rejections_db = _strict_instance_double(RejectionsDB)
        rejections_db.insert_rejection.return_value = None
        values["rejections_db"] = rejections_db

    default_factories = {
        "runtime_manager": RuntimeGenerationManager,
        "config_update_coordinator": lambda: _strict_instance_double(
            ConfigUpdateCoordinator
        ),
        "tokens_usage_db": lambda: _strict_instance_double(TokensUsageDB),
        "fallback_events_db": lambda: _strict_instance_double(FallbackEventsDB),
        "model_rotation_db": lambda: _strict_instance_double(ModelRotationDB),
        "write_batcher": lambda: _strict_instance_double(WriteBatcher),
        "accounting_service": _accounting_service_double,
        "usd_budget_ledger": UsdBudgetLedger,
        "active_requests_registry": ActiveRequestsRegistry,
        "rate_limiter": RateLimiter,
        "ip_block_guard": lambda: None,
        "upstream_routing_state": UpstreamRoutingState,
        "upstream_subscription_quota_service": lambda: UpstreamSubscriptionQuotaService(
            http_client=http_client  # type: ignore[arg-type]
        ),
        "openrouter_free_models_service": OpenRouterFreeModelsService,
        "fallback_model_eval_service": FallbackModelEvalService,
        "deep_research_process_runner": DeepResearchProcessRunner,
        "upload_admission": lambda: UploadAdmission(max_bytes=256 * 1024 * 1024),
        "stream_observation_capacity": StreamObservationCapacity,
        "json_response_capacity": lambda: StreamObservationCapacity(
            max_bytes=32 * 1024 * 1024
        ),
    }
    for name, factory in default_factories.items():
        if name not in values:
            values[name] = factory()
    if "image_storage" not in values:
        temp_directory = tempfile.TemporaryDirectory(
            prefix="llmgateway_app_services_outputs_"
        )
        if not _APP_SERVICE_STORAGE_DIRECTORIES:
            atexit.register(_cleanup_app_service_storage_directories)
        _APP_SERVICE_STORAGE_DIRECTORIES.append(temp_directory)
        images_root = Path(temp_directory.name) / "images"
        images_root.mkdir()
        values["image_storage"] = GeneratedImageStorage(images_root)
    if "image_retention_service" not in values:
        storage = values["image_storage"]
        if isinstance(storage, GeneratedImageStorage):
            image_retention_service = ImageRetentionService(
                storage,
                retention_days=10,
            )
            image_retention_service.run()
        else:
            image_retention_service = _strict_instance_double(
                ImageRetentionService
            )
            image_retention_service.snapshot.return_value = ImageRetentionSnapshot(
                last_run=0.0
            )
        values["image_retention_service"] = image_retention_service
    if "health_service" not in values:
        values["health_service"] = HealthService(
            runtime_manager=values["runtime_manager"],  # type: ignore[arg-type]
            config_update_coordinator=values["config_update_coordinator"],  # type: ignore[arg-type]
            accounting_service=values["accounting_service"],  # type: ignore[arg-type]
            write_batcher=values["write_batcher"],  # type: ignore[arg-type]
            image_retention_service=values["image_retention_service"],  # type: ignore[arg-type]
            database_paths=(Path("/nonexistent/llmgateway-test-health.db"),),
        )
    if "stream_event_max_bytes" not in values:
        capacity = values["stream_observation_capacity"]
        values["stream_event_max_bytes"] = min(
            STREAM_EVENT_MAX_BYTES,
            capacity.snapshot.max_bytes,  # type: ignore[union-attr]
        )
    if "upload_admission_timeout_seconds" not in values:
        values["upload_admission_timeout_seconds"] = 5.0
    if "json_response_max_bytes" not in values:
        capacity = values["json_response_capacity"]
        values["json_response_max_bytes"] = min(
            8 * 1024 * 1024,
            capacity.snapshot.max_bytes,  # type: ignore[union-attr]
        )
    if "task_supervisor" not in values:
        values["task_supervisor"] = TaskSupervisor()
    if set(values) != field_names:
        raise RuntimeError("AppServices fields changed; update runtime test defaults")
    return AppServices(**values)  # type: ignore[arg-type]


def _services_are_bound(app: FastAPI) -> bool:
    try:
        app.state.services
    except AttributeError:
        return False
    return True


def _bind_services(app: FastAPI, services: AppServices) -> None:
    if _services_are_bound(app):
        raise RuntimeError("app.state.services is already bound")
    app.state.services = services


def bind_app_services(app: FastAPI, **overrides: object) -> AppServices:
    """Publish one complete container and no legacy state aliases."""
    if _services_are_bound(app):
        raise RuntimeError("app.state.services is already bound")
    services = make_app_services(**overrides)
    _bind_services(app, services)
    return services


def _empty_config_loader() -> ConfigLoader:
    loader = ConfigLoader()
    for attribute in _CONFIG_GRAPH_ATTRIBUTES:
        setattr(loader, attribute, {})
    loader._fallback_rules_base = {}
    return loader


def make_runtime_snapshot(
    *,
    generation: int = 1,
    config_loader: ConfigLoader | None = None,
    http_client: httpx.AsyncClient | None = None,
    **overrides: object,
) -> RuntimeSnapshot:
    """Build one self-contained generation without loading repository config."""
    field_names = _field_names(RuntimeSnapshot)
    overridable = field_names - {"generation", "config_loader"}
    _unknown_overrides(overrides, allowed=overridable, owner="RuntimeSnapshot")
    _reject_mandatory_none(overrides, optional=frozenset())

    loader = config_loader if config_loader is not None else _empty_config_loader()
    shared_http_client = http_client if http_client is not None else _strict_instance_double(httpx.AsyncClient)
    values: dict[str, object] = {
        "generation": generation,
        "config_loader": loader,
        **overrides,
    }
    if "cost_rate_registry" not in values:
        values["cost_rate_registry"] = {}
    frozen_cost_rate_registry = MappingProxyType(
        dict(values["cost_rate_registry"])
    )
    values["cost_rate_registry"] = frozen_cost_rate_registry
    if "operation_cost_calculator_registry" not in values:
        values["operation_cost_calculator_registry"] = {}
    values["operation_cost_calculator_registry"] = MappingProxyType(
        dict(values["operation_cost_calculator_registry"])
    )
    if "operation_dispatcher" not in values:
        values["operation_dispatcher"] = OperationDispatcher(
            loader.providers_config,
            loader.operation_rules,
            shared_http_client,
            loader.model_rules,
        )
    if "fusion_service" not in values:
        values["fusion_service"] = FusionEnsembleService(
            loader,
            cost_rate_registry=frozen_cost_rate_registry,
        )
    if "router_model_service" not in values:
        values["router_model_service"] = RouterModelService(
            loader,
            cost_rate_registry=frozen_cost_rate_registry,
        )
    if "provider_models_service" not in values:
        values["provider_models_service"] = ProviderModelsService()
    if "proxy_http_clients" not in values:
        values["proxy_http_clients"] = {}
    if set(values) != field_names:
        raise RuntimeError("RuntimeSnapshot fields changed; update runtime test defaults")
    return RuntimeSnapshot(**values)  # type: ignore[arg-type]


def install_test_runtime_snapshot(
    manager: RuntimeGenerationManager,
    snapshot: RuntimeSnapshot,
) -> None:
    """Install a raw runtime snapshot through the canonical test-only seam."""
    manager._install_initial_for_testing(snapshot)


def publish_test_runtime_snapshot(
    manager: RuntimeGenerationManager,
    candidate: RuntimeSnapshot,
    *,
    expected_generation: int,
) -> RuntimeSnapshot:
    """Publish a raw runtime snapshot through the canonical test-only seam."""
    return manager._publish_for_testing(
        candidate,
        expected_generation=expected_generation,
    )


def _remove_owned_services_binding(
    app: FastAPI,
    services: AppServices,
    *,
    binding_expected: bool,
) -> None:
    try:
        current = app.state.services
    except AttributeError as exc:
        if binding_expected:
            raise RuntimeError("app.state.services binding disappeared") from exc
        return
    if current is services:
        del app.state.services
        return
    if not binding_expected:
        return
    raise RuntimeError("app.state.services binding was replaced")


@asynccontextmanager
async def installed_runtime(
    app: FastAPI,
    *,
    config_loader: ConfigLoader | None = None,
    snapshot_overrides: Mapping[str, object] | None = None,
    **service_overrides: object,
) -> AsyncIterator[RuntimeSnapshot]:
    """Install and fully retire a fresh typed runtime on the current loop."""
    asyncio.get_running_loop()
    if _services_are_bound(app):
        raise RuntimeError("app.state.services is already bound")

    forbidden_services = sorted(set(service_overrides) & _INSTALLED_RUNTIME_SERVICE_FIELDS)
    if forbidden_services:
        raise ValueError("installed_runtime owns service override(s): " + ", ".join(forbidden_services))

    candidate_overrides = dict(snapshot_overrides or {})
    forbidden_snapshot = sorted(set(candidate_overrides) & _INSTALLED_RUNTIME_SNAPSHOT_FIELDS)
    if forbidden_snapshot:
        raise ValueError("installed_runtime owns snapshot override(s): " + ", ".join(forbidden_snapshot))

    manager = RuntimeGenerationManager()
    supervisor = TaskSupervisor()
    services = make_app_services(
        runtime_manager=manager,
        task_supervisor=supervisor,
        **service_overrides,
    )
    snapshot = make_runtime_snapshot(
        generation=1,
        config_loader=config_loader,
        http_client=services.http_client,
        **candidate_overrides,
    )
    primary_error: BaseException | None = None
    binding_installed = False
    try:
        install_test_runtime_snapshot(manager, snapshot)
        _bind_services(app, services)
        binding_installed = True
        yield snapshot
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        cleanup_errors: list[BaseException] = []
        try:
            _remove_owned_services_binding(
                app,
                services,
                binding_expected=binding_installed,
            )
        except BaseException as exc:
            cleanup_errors.append(exc)

        for cleanup in (
            services.config_update_coordinator.close,
            supervisor.close,
            manager.shutdown,
        ):
            try:
                await cleanup()
            except BaseException as exc:
                cleanup_errors.append(exc)

        if primary_error is None and cleanup_errors:
            raise cleanup_errors[0]
