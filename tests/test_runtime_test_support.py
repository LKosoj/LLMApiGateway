from __future__ import annotations

import asyncio
import subprocess
import sys
from contextlib import asynccontextmanager
from dataclasses import fields
from pathlib import Path
from types import MappingProxyType
from unittest.mock import Mock, create_autospec, patch

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.write_batcher import WriteBatcher
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.accounting import OperationCostCalculator
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.config_updates import ConfigUpdateCoordinator
from llm_gateway_core.services.fallback_model_evals import FallbackModelEvalService
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from llm_gateway_core.services.openrouter_free_models import OpenRouterFreeModelsService
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.rate_limiter import RateLimiter
from llm_gateway_core.services.request_handler import OperationDispatcher
from llm_gateway_core.services.router_model import RouterModelService
from llm_gateway_core.services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeManagerStatus,
    RuntimeSnapshot,
)
from llm_gateway_core.services.stream_observation import StreamObservationCapacity
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.services.upstream_subscription_quota import (
    UpstreamSubscriptionQuotaService,
)
from llm_gateway_core.services.upload_admission import UploadAdmission
from tests._async_compat import run_async
from tests.runtime_test_support import (
    bind_app_services,
    install_test_runtime_snapshot,
    installed_runtime,
    make_app_services,
    make_runtime_snapshot,
    publish_test_runtime_snapshot,
)


_CONFIG_GRAPH_ATTRIBUTES = (
    "providers_config",
    "fallback_rules",
    "operation_rules",
    "fusion_rules",
    "model_rules",
    "router_rules",
)
_EXTERNAL_BOUNDARY_TYPES = {
    "config_update_coordinator": ConfigUpdateCoordinator,
    "http_client": httpx.AsyncClient,
    "tokens_usage_db": TokensUsageDB,
    "fallback_events_db": FallbackEventsDB,
    "rejections_db": RejectionsDB,
    "api_keys_db": ApiKeysDB,
    "model_rotation_db": ModelRotationDB,
    "write_batcher": WriteBatcher,
    "accounting_service": AccountingService,
}
_REAL_SERVICE_TYPES = {
    "runtime_manager": RuntimeGenerationManager,
    "usd_budget_ledger": UsdBudgetLedger,
    "active_requests_registry": ActiveRequestsRegistry,
    "rate_limiter": RateLimiter,
    "upstream_routing_state": UpstreamRoutingState,
    "upstream_subscription_quota_service": UpstreamSubscriptionQuotaService,
    "openrouter_free_models_service": OpenRouterFreeModelsService,
    "fallback_model_eval_service": FallbackModelEvalService,
    "upload_admission": UploadAdmission,
    "stream_observation_capacity": StreamObservationCapacity,
    "json_response_capacity": StreamObservationCapacity,
    "task_supervisor": TaskSupervisor,
}


def _has_services(app: FastAPI) -> bool:
    return "services" in app.state._state


def test_make_app_services_populates_exact_current_dataclass_contract() -> None:
    services = make_app_services()

    assert tuple(field.name for field in fields(services)) == tuple(field.name for field in fields(AppServices))
    for name, expected_type in _EXTERNAL_BOUNDARY_TYPES.items():
        dependency = getattr(services, name)
        assert isinstance(dependency, expected_type)
        with pytest.raises(AttributeError):
            dependency.not_in_the_spec = object()
    for name, expected_type in _REAL_SERVICE_TYPES.items():
        assert type(getattr(services, name)) is expected_type
    assert services.ip_block_guard is None
    assert services.upstream_subscription_quota_service._client is services.http_client


def test_default_app_services_storage_is_cleaned_without_resource_warning(
    tmp_path: Path,
) -> None:
    storage_path_file = tmp_path / "storage-path"
    completed = subprocess.run(
        [
            sys.executable,
            "-W",
            "error::ResourceWarning",
            "-c",
            (
                "from pathlib import Path\n"
                "import sys\n"
                "from tests.runtime_test_support import make_app_services\n"
                "services = make_app_services()\n"
                "storage_root = services.image_storage.images_root.parent\n"
                "Path(sys.argv[1]).write_text(str(storage_root), encoding='utf-8')\n"
                "assert storage_root.is_dir()\n"
            ),
            str(storage_path_file),
        ],
        cwd=Path(__file__).resolve().parents[1],
        capture_output=True,
        text=True,
        check=False,
    )

    assert completed.returncode == 0
    assert completed.stderr == ""
    assert not Path(storage_path_file.read_text(encoding="utf-8")).exists()


def test_default_database_doubles_have_controlled_empty_behavior() -> None:
    services = make_app_services()

    assert services.api_keys_db.get_by_key("missing") is None
    assert services.api_keys_db.get_by_id(1) is None
    assert services.api_keys_db.list_all() == []
    assert services.api_keys_db.reset_due_budgets() == []
    assert (
        services.rejections_db.insert_rejection(
            request_id=None,
            api_key_id=None,
            path="/test",
            method="GET",
            client_ip=None,
            status_code=401,
            category="unauthorized",
            reason=None,
            auth_source=None,
        )
        is None
    )


def test_make_app_services_never_constructs_file_or_socket_boundaries() -> None:
    with (
        patch("sqlite3.connect", side_effect=AssertionError("sqlite opened")),
        patch("socket.socket", side_effect=AssertionError("socket opened")),
        patch("builtins.open", side_effect=AssertionError("file opened")),
    ):
        services = make_app_services()

    assert isinstance(services.http_client, httpx.AsyncClient)


def test_make_app_services_applies_every_declared_override_by_identity() -> None:
    for field in fields(AppServices):
        if field.name == "ip_block_guard":
            override = None
        elif field.name in {
            "stream_observation_capacity",
            "json_response_capacity",
        }:
            override = StreamObservationCapacity(max_bytes=2_048)
        elif field.name in {
            "stream_event_max_bytes",
            "json_response_max_bytes",
            "upload_admission_timeout_seconds",
        }:
            override = 1_024
        else:
            override = Mock(name=field.name)
        services = make_app_services(**{field.name: override})
        assert getattr(services, field.name) is override


def test_make_app_services_rejects_unknown_and_mandatory_none_overrides() -> None:
    with pytest.raises(TypeError, match="unknown.*unexpected"):
        make_app_services(unexpected=object())

    for field in fields(AppServices):
        if field.name == "ip_block_guard":
            continue
        with pytest.raises(ValueError, match=field.name):
            make_app_services(**{field.name: None})


def test_make_app_services_returns_fresh_stateful_defaults() -> None:
    first = make_app_services()
    second = make_app_services()

    for field in fields(AppServices):
        if field.name in {
            "ip_block_guard",
            "stream_event_max_bytes",
            "json_response_max_bytes",
            "upload_admission_timeout_seconds",
        }:
            continue
        assert getattr(first, field.name) is not getattr(second, field.name)


def test_bind_app_services_publishes_only_typed_container() -> None:
    app = FastAPI()
    before = dict(app.state._state)

    services = bind_app_services(app)

    assert app.state.services is services
    assert app.state._state == {**before, "services": services}


def test_bind_app_services_rejects_existing_binding_without_replacing_it() -> None:
    app = FastAPI()
    existing = object()
    app.state.services = existing

    with pytest.raises(RuntimeError, match="already bound"):
        bind_app_services(app)

    assert app.state.services is existing


def test_make_runtime_snapshot_builds_empty_real_generation_graph() -> None:
    shared_http_client = create_autospec(
        httpx.AsyncClient,
        instance=True,
        spec_set=True,
    )

    snapshot = make_runtime_snapshot(http_client=shared_http_client)

    assert tuple(field.name for field in fields(snapshot)) == tuple(field.name for field in fields(RuntimeSnapshot))
    assert snapshot.generation == 1
    assert type(snapshot.config_loader) is ConfigLoader
    assert all(getattr(snapshot.config_loader, attribute) == {} for attribute in _CONFIG_GRAPH_ATTRIBUTES)
    assert type(snapshot.operation_dispatcher) is OperationDispatcher
    assert snapshot.operation_dispatcher._http_client is shared_http_client
    assert snapshot.operation_dispatcher._providers_config is snapshot.config_loader.providers_config
    assert snapshot.operation_dispatcher._operation_rules is snapshot.config_loader.operation_rules
    assert type(snapshot.fusion_service) is FusionEnsembleService
    assert snapshot.fusion_service._config_loader is snapshot.config_loader
    assert type(snapshot.router_model_service) is RouterModelService
    assert snapshot.router_model_service._config_loader is snapshot.config_loader
    assert isinstance(snapshot.fusion_service._cost_rate_registry, MappingProxyType)
    assert snapshot.fusion_service._cost_rate_registry is snapshot.router_model_service._cost_rate_registry
    assert dict(snapshot.fusion_service._cost_rate_registry) == dict(snapshot.cost_rate_registry)
    assert type(snapshot.provider_models_service) is ProviderModelsService
    assert isinstance(snapshot.proxy_http_clients, MappingProxyType)
    assert isinstance(snapshot.cost_rate_registry, MappingProxyType)
    assert isinstance(
        snapshot.operation_cost_calculator_registry,
        MappingProxyType,
    )
    assert dict(snapshot.proxy_http_clients) == {}
    assert dict(snapshot.cost_rate_registry) == {}
    assert dict(snapshot.operation_cost_calculator_registry) == {}


def test_make_runtime_snapshot_calls_no_config_load_method_or_io() -> None:
    load_methods = [name for name in dir(ConfigLoader) if name.startswith("load_")]
    with (
        patch.multiple(
            ConfigLoader,
            **{name: Mock(side_effect=AssertionError(f"{name} called")) for name in load_methods},
        ),
        patch("sqlite3.connect", side_effect=AssertionError("sqlite opened")),
        patch("socket.socket", side_effect=AssertionError("socket opened")),
        patch("builtins.open", side_effect=AssertionError("file opened")),
    ):
        snapshot = make_runtime_snapshot()

    assert snapshot.config_loader.providers_config == {}


def test_make_runtime_snapshot_copies_and_freezes_mapping_overrides() -> None:
    proxy = create_autospec(httpx.AsyncClient, instance=True, spec_set=True)
    proxies = {"proxy": proxy}
    costs = {("provider", "model"): object()}
    calculators = {
        ("images_generation", "gateway/image"): OperationCostCalculator(
            "operation",
            0.25,
        )
    }

    snapshot = make_runtime_snapshot(
        proxy_http_clients=proxies,
        cost_rate_registry=costs,
        operation_cost_calculator_registry=calculators,
    )
    proxies["later"] = proxy
    costs[("later", "model")] = object()
    calculators[("images_edit", "gateway/later")] = OperationCostCalculator(
        "operation",
        0.5,
    )

    assert dict(snapshot.proxy_http_clients) == {"proxy": proxy}
    assert set(snapshot.cost_rate_registry) == {("provider", "model")}
    assert set(snapshot.operation_cost_calculator_registry) == {
        ("images_generation", "gateway/image")
    }
    assert set(snapshot.fusion_service._cost_rate_registry) == {("provider", "model")}
    assert snapshot.fusion_service._cost_rate_registry is snapshot.router_model_service._cost_rate_registry
    with pytest.raises(TypeError):
        snapshot.proxy_http_clients["blocked"] = proxy
    with pytest.raises(TypeError):
        snapshot.cost_rate_registry[("blocked", "model")] = object()
    with pytest.raises(TypeError):
        snapshot.operation_cost_calculator_registry[
            ("images_edit", "gateway/blocked")
        ] = OperationCostCalculator("operation", 1.0)


def test_make_runtime_snapshot_applies_declared_overrides_and_rejects_invalid() -> None:
    loader = ConfigLoader()
    dispatcher = Mock(name="dispatcher")
    snapshot = make_runtime_snapshot(
        generation=2,
        config_loader=loader,
        operation_dispatcher=dispatcher,
    )

    assert snapshot.generation == 2
    assert snapshot.config_loader is loader
    assert snapshot.operation_dispatcher is dispatcher
    with pytest.raises(TypeError, match="unknown.*unexpected"):
        make_runtime_snapshot(unexpected=object())
    for name in (
        "operation_dispatcher",
        "fusion_service",
        "router_model_service",
        "provider_models_service",
        "proxy_http_clients",
        "cost_rate_registry",
        "operation_cost_calculator_registry",
    ):
        with pytest.raises(ValueError, match=name):
            make_runtime_snapshot(**{name: None})


def test_runtime_snapshot_mutation_helpers_preserve_manager_contract() -> None:
    async def scenario() -> None:
        manager = RuntimeGenerationManager()
        initial = make_runtime_snapshot(generation=1)
        candidate = make_runtime_snapshot(generation=2)

        assert install_test_runtime_snapshot(manager, initial) is None
        assert (
            publish_test_runtime_snapshot(
                manager,
                candidate,
                expected_generation=1,
            )
            is candidate
        )
        lease = manager.acquire_current()
        assert lease.snapshot is candidate
        lease.release()
        await manager.shutdown()

        assert manager.status is RuntimeManagerStatus.STOPPED
        assert manager.cleanup_task_count == 0

    run_async(scenario())


def test_installed_runtime_installs_before_binding_and_unbinds_before_cleanup() -> None:
    app = FastAPI()
    events: list[str] = []

    class RecordingManager(RuntimeGenerationManager):
        async def shutdown(self) -> None:
            assert not _has_services(app)
            events.append("manager")
            await super().shutdown()

    class RecordingSupervisor(TaskSupervisor):
        async def close(self) -> None:
            assert not _has_services(app)
            events.append("supervisor")
            await super().close()

    class RecordingCoordinator:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            assert not _has_services(app)
            events.append("coordinator")
            self.closed = True

    coordinator = RecordingCoordinator()

    def recording_install(
        manager: RuntimeGenerationManager,
        snapshot: RuntimeSnapshot,
    ) -> None:
        assert not _has_services(app)
        events.append("install")
        install_test_runtime_snapshot(manager, snapshot)

    async def scenario() -> tuple[AppServices, RuntimeSnapshot]:
        with (
            patch(
                "tests.runtime_test_support.RuntimeGenerationManager",
                RecordingManager,
            ),
            patch("tests.runtime_test_support.TaskSupervisor", RecordingSupervisor),
            patch(
                "tests.runtime_test_support.install_test_runtime_snapshot",
                side_effect=recording_install,
            ),
        ):
            async with installed_runtime(
                app,
                config_update_coordinator=coordinator,
            ) as snapshot:
                assert app.state.services.runtime_manager.current_generation == 1
                assert app.state.services.runtime_manager.status is RuntimeManagerStatus.RUNNING
                assert snapshot.operation_dispatcher._http_client is app.state.services.http_client
                events.append("body")
                return app.state.services, snapshot

    services, snapshot = run_async(scenario())

    assert events == ["install", "body", "coordinator", "supervisor", "manager"]
    assert not _has_services(app)
    assert services.config_update_coordinator is coordinator
    assert coordinator.closed
    assert services.runtime_manager.status is RuntimeManagerStatus.STOPPED
    assert services.task_supervisor.closed
    assert services.task_supervisor.task_count == 0
    assert services.runtime_manager.cleanup_task_count == 0
    assert snapshot.generation == 1


def test_installed_runtime_rejects_conflicting_inputs_before_binding() -> None:
    app = FastAPI()

    async def scenario() -> None:
        for overrides in (
            {"runtime_manager": RuntimeGenerationManager()},
            {"task_supervisor": TaskSupervisor()},
        ):
            with pytest.raises(ValueError, match=next(iter(overrides))):
                async with installed_runtime(app, **overrides):
                    raise AssertionError("context entered")
            assert not _has_services(app)

        for reserved in ("generation", "config_loader", "http_client"):
            with pytest.raises(ValueError, match=reserved):
                async with installed_runtime(
                    app,
                    snapshot_overrides={reserved: object()},
                ):
                    raise AssertionError("context entered")
            assert not _has_services(app)

        with pytest.raises(TypeError, match="unknown.*unexpected"):
            async with installed_runtime(
                app,
                snapshot_overrides={"unexpected": object()},
            ):
                raise AssertionError("context entered")
        assert not _has_services(app)

        existing = object()
        app.state.services = existing
        with pytest.raises(RuntimeError, match="already bound"):
            async with installed_runtime(app):
                raise AssertionError("context entered")
        assert app.state.services is existing

    run_async(scenario())


@pytest.mark.parametrize(
    "primary",
    [
        RuntimeError("body failed"),
        asyncio.CancelledError("body cancelled"),
        SystemExit("body exited"),
        KeyboardInterrupt("body interrupted"),
    ],
    ids=("runtime-error", "cancelled", "system-exit", "keyboard-interrupt"),
)
def test_installed_runtime_preserves_primary_base_exception_identity(
    primary: BaseException,
) -> None:
    app = FastAPI()
    events: list[str] = []

    class FailingSupervisor(TaskSupervisor):
        async def close(self) -> None:
            events.append("supervisor")
            raise RuntimeError("supervisor cleanup failed")

    class FailingCoordinator:
        async def close(self) -> None:
            assert not _has_services(app)
            events.append("coordinator")
            raise RuntimeError("coordinator cleanup failed")

    class FailingManager(RuntimeGenerationManager):
        async def shutdown(self) -> None:
            assert not _has_services(app)
            events.append("manager")
            raise RuntimeError("manager cleanup failed")

    async def scenario() -> None:
        with (
            patch("tests.runtime_test_support.TaskSupervisor", FailingSupervisor),
            patch("tests.runtime_test_support.RuntimeGenerationManager", FailingManager),
        ):
            async with installed_runtime(
                app,
                config_update_coordinator=FailingCoordinator(),
            ):
                raise primary

    with pytest.raises(BaseException) as raised:
        run_async(scenario())

    assert raised.value is primary
    assert events == ["coordinator", "supervisor", "manager"]
    assert not _has_services(app)


def test_installed_runtime_raises_first_cleanup_error_after_attempting_all() -> None:
    app = FastAPI()
    primary = RuntimeError("coordinator cleanup failed")
    events: list[str] = []

    class FailingCoordinator:
        async def close(self) -> None:
            assert not _has_services(app)
            events.append("coordinator")
            raise primary

    class FailingSupervisor(TaskSupervisor):
        async def close(self) -> None:
            events.append("supervisor")
            raise SystemExit("later supervisor cleanup failed")

    class RecordingManager(RuntimeGenerationManager):
        async def shutdown(self) -> None:
            events.append("manager")
            raise KeyboardInterrupt("later manager cleanup failed")

    async def scenario() -> None:
        with (
            patch("tests.runtime_test_support.TaskSupervisor", FailingSupervisor),
            patch("tests.runtime_test_support.RuntimeGenerationManager", RecordingManager),
        ):
            async with installed_runtime(
                app,
                config_update_coordinator=FailingCoordinator(),
            ):
                pass

    with pytest.raises(RuntimeError) as raised:
        run_async(scenario())

    assert raised.value is primary
    assert events == ["coordinator", "supervisor", "manager"]
    assert not _has_services(app)


def test_installed_runtime_cleans_up_after_bind_failure_without_deleting_foreign_state() -> None:
    app = FastAPI()
    bind_error = RuntimeError("bind failed")
    foreign_services = object()
    events: list[str] = []
    managers: list[RuntimeGenerationManager] = []
    supervisors: list[TaskSupervisor] = []

    class RecordingManager(RuntimeGenerationManager):
        def __init__(self) -> None:
            super().__init__()
            managers.append(self)

        async def shutdown(self) -> None:
            events.append("manager")
            await super().shutdown()

    class RecordingSupervisor(TaskSupervisor):
        def __init__(self) -> None:
            super().__init__()
            supervisors.append(self)

        async def close(self) -> None:
            events.append("supervisor")
            await super().close()

    class RecordingCoordinator:
        async def close(self) -> None:
            events.append("coordinator")

    def fail_bind(_app: FastAPI, _services: AppServices) -> None:
        events.append("bind")
        _app.state.services = foreign_services
        raise bind_error

    def recording_install(
        manager: RuntimeGenerationManager,
        snapshot: RuntimeSnapshot,
    ) -> None:
        events.append("install")
        install_test_runtime_snapshot(manager, snapshot)

    async def scenario() -> None:
        with (
            patch("tests.runtime_test_support.RuntimeGenerationManager", RecordingManager),
            patch("tests.runtime_test_support.TaskSupervisor", RecordingSupervisor),
            patch(
                "tests.runtime_test_support.install_test_runtime_snapshot",
                side_effect=recording_install,
            ),
            patch("tests.runtime_test_support._bind_services", side_effect=fail_bind),
        ):
            async with installed_runtime(
                app,
                config_update_coordinator=RecordingCoordinator(),
            ):
                raise AssertionError("context entered")

    with pytest.raises(RuntimeError) as raised:
        run_async(scenario())

    assert raised.value is bind_error
    assert events == ["install", "bind", "coordinator", "supervisor", "manager"]
    assert app.state.services is foreign_services
    assert len(managers) == 1
    assert managers[0].status is RuntimeManagerStatus.STOPPED
    assert len(supervisors) == 1
    assert supervisors[0].closed


def test_sequential_installed_contexts_have_fresh_loop_owners_and_no_tasks() -> None:
    app = FastAPI()

    async def scenario() -> AppServices:
        current = asyncio.current_task()
        pending_before = {task for task in asyncio.all_tasks() if task is not current}
        async with installed_runtime(app):
            services = app.state.services
            assert services.runtime_manager._loop is asyncio.get_running_loop()
        pending_after = {task for task in asyncio.all_tasks() if task is not current}
        assert pending_after == pending_before
        return services

    first = run_async(scenario())
    second = run_async(scenario())

    assert first.runtime_manager is not second.runtime_manager
    assert first.task_supervisor is not second.task_supervisor
    assert first.runtime_manager._loop is not second.runtime_manager._loop
    assert first.task_supervisor._loop is not second.task_supervisor._loop
    for services in (first, second):
        services.config_update_coordinator.close.assert_awaited_once_with()
        assert services.runtime_manager.status is RuntimeManagerStatus.STOPPED
        assert services.runtime_manager.cleanup_task_count == 0
        assert services.task_supervisor.closed
        assert services.task_supervisor.task_count == 0
    assert not _has_services(app)


def test_testclient_enters_helper_on_its_lifespan_loop_only() -> None:
    app = FastAPI()
    owners: list[tuple[RuntimeGenerationManager, TaskSupervisor, asyncio.AbstractEventLoop]] = []

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        async with installed_runtime(_app) as snapshot:
            services = _app.state.services
            owners.append(
                (
                    services.runtime_manager,
                    services.task_supervisor,
                    asyncio.get_running_loop(),
                )
            )
            _app.state.snapshot_for_endpoint = snapshot
            try:
                yield
            finally:
                del _app.state.snapshot_for_endpoint

    app.router.lifespan_context = lifespan

    @app.get("/probe")
    async def probe(request: Request) -> dict[str, object]:
        services = request.app.state.services
        return {
            "generation": request.app.state.snapshot_for_endpoint.generation,
            "manager_running": services.runtime_manager.status is RuntimeManagerStatus.RUNNING,
            "same_loop": services.runtime_manager._loop is asyncio.get_running_loop(),
        }

    for _attempt in range(2):
        with TestClient(app) as client:
            assert client.get("/probe").json() == {
                "generation": 1,
                "manager_running": True,
                "same_loop": True,
            }
        assert not _has_services(app)

    first, second = owners
    assert first[0] is not second[0]
    assert first[1] is not second[1]
    assert first[2] is not second[2]
    for manager, supervisor, _loop in owners:
        assert manager.status is RuntimeManagerStatus.STOPPED
        assert manager.cleanup_task_count == 0
        assert supervisor.closed
        assert supervisor.task_count == 0
