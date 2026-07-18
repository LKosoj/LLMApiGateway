from __future__ import annotations

import asyncio
import os
import threading
import unittest
from contextlib import AsyncExitStack, ExitStack, contextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.datastructures import State

import main
from llm_gateway_core.services.config_updates import (
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from llm_gateway_core.services.health import HealthPhase
from llm_gateway_core.services.runtime_config import (
    RUNTIME_CLEANUP_MAX_ATTEMPTS,
    RuntimeGenerationManager,
    RuntimeManagerStatus,
)
from llm_gateway_core.services.stream_observation import (
    StreamObservationCapacity,
    StreamObservationStateError,
)
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from llm_gateway_core.services.upload_admission import (
    UploadAdmission,
    UploadAdmissionClosed,
)
from tests._async_compat import run_async


_LEGACY_STATE_ALIASES = (
    "config_loader",
    "operation_rules",
    "tokens_usage_db",
    "fallback_events_db",
    "rejections_db",
    "write_batcher",
    "accounting_service",
    "api_keys_db",
    "usd_budget_ledger",
    "active_requests_registry",
    "rate_limiter",
    "ip_block_guard",
    "upstream_routing_state",
    "model_rotation_db",
    "http_client",
    "upstream_subscription_quota_service",
    "proxy_http_clients",
    "operation_dispatcher",
    "fusion_service",
    "router_model_service",
    "provider_models_service",
    "cost_rate_registry",
    "openrouter_free_models_service",
    "fallback_model_eval_service",
)


async def _parked_loop(*_args, **_kwargs) -> None:
    await asyncio.Event().wait()


async def _wait_for_thread_event(event: threading.Event) -> None:
    for _attempt in range(2_000):
        if event.is_set():
            return
        await asyncio.sleep(0.001)
    raise AssertionError("Blocking worker did not start")


async def _exercise_blocked_periodic_task(
    task,
    supervisor: TaskSupervisor,
    started: threading.Event,
    release: threading.Event,
    mutations: list[str],
) -> None:
    next_cleanup_started = asyncio.Event()
    stack = AsyncExitStack()

    async def next_cleanup() -> None:
        next_cleanup_started.set()

    stack.push_async_callback(next_cleanup)
    stack.push_async_callback(supervisor.close)
    drain_task = None
    try:
        await _wait_for_thread_event(started)
        task.cancel()
        await asyncio.sleep(0)
        task.cancel()
        drain_task = asyncio.create_task(stack.aclose())
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        if drain_task.done():
            raise AssertionError("Supervisor closed before the blocking worker finished")
        if next_cleanup_started.is_set():
            raise AssertionError("LIFO cleanup advanced before the blocking worker finished")
    finally:
        release.set()
        if drain_task is not None:
            await drain_task

    if not next_cleanup_started.is_set():
        raise AssertionError("LIFO cleanup did not continue after worker completion")
    mutation_count = len(mutations)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    if len(mutations) != mutation_count:
        raise AssertionError("Worker mutated state after supervisor close completed")


@contextmanager
def _lifespan_environment():
    events: list[str] = []
    state_sets: list[str] = []
    shared_clients: list[Mock] = []
    proxy_clients: list[Mock] = []
    batchers: list[Mock] = []
    accounting_services: list[Mock] = []
    accounting_calls: list[tuple[object, object, object, object]] = []
    openrouter_services: list[Mock] = []
    fallback_services: list[Mock] = []
    deep_research_runners: list[object] = []
    single_process_leases: list[Mock] = []
    coordinators: list[object] = []
    coordinator_calls: list[tuple[object, object, object]] = []
    startup_events: list[str] = []

    config_loader = Mock(name="config-loader")
    config_loader.providers_config = {}
    config_loader.fallback_rules = {}
    config_loader.operation_rules = {}
    config_loader.fusion_rules = {}
    config_loader.model_rules = {}
    config_loader.router_rules = {}

    def shared_client_factory() -> Mock:
        client = Mock(name=f"shared-{len(shared_clients) + 1}")
        client.aclose = AsyncMock(side_effect=lambda: events.append("shared"))
        shared_clients.append(client)
        return client

    async def proxy_client_factory(
        _providers,
        *,
        register_client,
    ) -> dict[str, Mock]:
        startup_events.append("runtime:proxy")
        client = Mock(name=f"proxy-{len(proxy_clients) + 1}")
        client.aclose = AsyncMock(side_effect=lambda: events.append("proxy"))
        proxy_clients.append(client)
        register_client("proxy", client)
        return {"proxy": client}

    def batcher_factory(_path, *, queue_maxsize) -> Mock:
        assert queue_maxsize == main.settings.write_batcher_queue_maxsize
        batcher = Mock(name=f"batcher-{len(batchers) + 1}")
        batcher.start = AsyncMock()
        batcher.stop = AsyncMock(side_effect=lambda: events.append("write-batcher"))
        batchers.append(batcher)
        return batcher

    def accounting_factory(
        source: object,
        sink: object,
        *,
        budget_ledger: object,
        rate_limiter: object,
    ) -> Mock:
        service = Mock(name=f"accounting-{len(accounting_services) + 1}")
        service.start = AsyncMock(
            side_effect=lambda: startup_events.append("accounting:start")
        )
        service.stop = AsyncMock(side_effect=lambda: events.append("accounting"))
        service.reset_due_budgets = AsyncMock()
        accounting_services.append(service)
        accounting_calls.append((source, sink, budget_ledger, rate_limiter))
        return service

    def openrouter_factory() -> Mock:
        service = Mock(name=f"openrouter-{len(openrouter_services) + 1}")
        service.start_runtime = AsyncMock()
        service.stop = AsyncMock(side_effect=lambda: events.append("openrouter"))
        service.get_status = AsyncMock(return_value={"configured": False})
        service.get_capability_index = AsyncMock(return_value={})
        openrouter_services.append(service)
        return service

    def fallback_factory() -> Mock:
        service = Mock(name=f"fallback-{len(fallback_services) + 1}")
        service.stop = AsyncMock(side_effect=lambda: events.append("fallback"))
        fallback_services.append(service)
        return service

    def single_process_lease_factory(state_dir) -> Mock:
        lease = Mock(name=f"single-process-{len(single_process_leases) + 1}")
        lease.state_dir = state_dir
        lease.close = AsyncMock(side_effect=lambda: events.append("single-process"))
        single_process_leases.append(lease)
        return lease

    class RecordingManager(RuntimeGenerationManager):
        async def shutdown(self) -> None:
            events.append("manager")
            await super().shutdown()

    class RecordingSupervisor(TaskSupervisor):
        def create_task(self, coroutine, *, name):
            startup_events.append(f"task:{name}")
            return super().create_task(coroutine, name=name)

        async def close(self) -> None:
            events.append("supervisor")
            await super().close()

    class RecordingDeepResearchRunner:
        def __init__(self, *, capacity, admission_timeout_seconds) -> None:
            self.capacity = capacity
            self.admission_timeout_seconds = admission_timeout_seconds
            self.closed = False
            deep_research_runners.append(self)

        async def start(self) -> None:
            startup_events.append("deep-research:start")

        async def aclose(self) -> None:
            if not self.closed:
                events.append("deep-research")
                self.closed = True

    class RecordingCoordinator:
        def __init__(
            self,
            *,
            runtime_manager,
            shared_http_client,
            initial_snapshot,
        ) -> None:
            self.runtime_manager = runtime_manager
            self.shared_http_client = shared_http_client
            self.initial_snapshot = initial_snapshot
            self.close_error: BaseException | None = None
            coordinator_calls.append(
                (runtime_manager, shared_http_client, initial_snapshot)
            )
            coordinators.append(self)
            startup_events.append("coordinator")

        async def close(self) -> None:
            events.append("coordinator")
            if self.close_error is not None:
                raise self.close_error

    class RecordingStateBinding(main._TemporaryStateBinding):
        def _set(self, name: str, value: object) -> None:
            state_sets.append(name)
            startup_events.append(f"state:{name}")
            super()._set(name, value)

        async def rollback(self) -> None:
            events.append("state")
            await super().rollback()

    verification = AsyncMock(
        side_effect=lambda **_kwargs: startup_events.append("verification")
    )
    single_process_validation = Mock()
    with ExitStack() as stack:
        stack.enter_context(
            patch(
                "main.validate_single_worker_environment",
                single_process_validation,
            )
        )
        single_process_acquire = stack.enter_context(
            patch(
                "main.SingleProcessLease.acquire",
                side_effect=single_process_lease_factory,
            )
        )
        stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-key"))
        stack.enter_context(patch.object(main.settings, "ip_block_enabled", False))
        stack.enter_context(patch("main.preload_templates"))
        stack.enter_context(patch("main._load_initial_config", return_value=config_loader))
        stack.enter_context(
            patch("main.create_shared_http_client", side_effect=shared_client_factory)
        )
        proxy_builder = stack.enter_context(
            patch(
                "llm_gateway_core.services.runtime_candidate.populate_proxy_http_clients",
                side_effect=proxy_client_factory,
            )
        )
        for dependency in (
            "TokensUsageDB",
            "FallbackEventsDB",
            "RejectionsDB",
            "ApiKeysDB",
            "UsdBudgetLedger",
            "ActiveRequestsRegistry",
            "RateLimiter",
            "ModelRotationDB",
            "UpstreamRoutingState",
            "UpstreamSubscriptionQuotaService",
        ):
            stack.enter_context(patch(f"main.{dependency}"))
        for dependency in (
            "OperationDispatcher",
            "FusionEnsembleService",
            "RouterModelService",
            "ProviderModelsService",
        ):
            stack.enter_context(
                patch(f"llm_gateway_core.services.runtime_candidate.{dependency}")
            )
        stack.enter_context(patch("main.WriteBatcher", side_effect=batcher_factory))
        stack.enter_context(
            patch("main.AccountingService", side_effect=accounting_factory)
        )
        stack.enter_context(
            patch("main.OpenRouterFreeModelsService", side_effect=openrouter_factory)
        )
        stack.enter_context(
            patch("main.FallbackModelEvalService", side_effect=fallback_factory)
        )
        stack.enter_context(patch("main.RuntimeGenerationManager", RecordingManager))
        stack.enter_context(patch("main.TaskSupervisor", RecordingSupervisor))
        stack.enter_context(
            patch("main.DeepResearchProcessRunner", RecordingDeepResearchRunner)
        )
        stack.enter_context(patch("main.ConfigUpdateCoordinator", RecordingCoordinator))
        stack.enter_context(patch("main._TemporaryStateBinding", RecordingStateBinding))
        stack.enter_context(
            patch(
                "llm_gateway_core.services.runtime_candidate.build_model_cost_rate_registry",
                return_value={},
            )
        )
        stack.enter_context(
            patch("main.run_startup_model_verification", verification)
        )
        stack.enter_context(patch("main.run_usage_stats_cleanup_loop", _parked_loop))
        stack.enter_context(
            patch("main.run_deep_research_images_cleanup_loop", _parked_loop)
        )
        stack.enter_context(patch("main.run_budget_reset_loop", _parked_loop))
        stack.enter_context(patch("main.run_free_llm_catalog_loop", _parked_loop))
        yield SimpleNamespace(
            events=events,
            state_sets=state_sets,
            shared_clients=shared_clients,
            proxy_clients=proxy_clients,
            batchers=batchers,
            accounting_services=accounting_services,
            accounting_calls=accounting_calls,
            openrouter_services=openrouter_services,
            fallback_services=fallback_services,
            deep_research_runners=deep_research_runners,
            single_process_leases=single_process_leases,
            coordinators=coordinators,
            coordinator_calls=coordinator_calls,
            startup_events=startup_events,
            single_process_validation=single_process_validation,
            single_process_acquire=single_process_acquire,
            proxy_builder=proxy_builder,
            verification=verification,
            config_loader=config_loader,
        )


class LifespanAppServicesTests(unittest.TestCase):
    def test_image_storage_probe_fails_before_http_or_database_startup(self) -> None:
        primary = RuntimeError("generated image storage unavailable")

        async def scenario() -> None:
            app = FastAPI()
            with patch.object(
                main.GeneratedImageStorage,
                "probe",
                side_effect=primary,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    async with main.lifespan(app):
                        self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)
            self.assertFalse(hasattr(app.state, "services"))

        with _lifespan_environment() as env:
            run_async(scenario())

        self.assertEqual(env.shared_clients, [])
        self.assertEqual(env.batchers, [])
        self.assertEqual(len(env.single_process_leases), 1)
        env.single_process_leases[0].close.assert_awaited_once_with()

    def test_first_image_retention_run_completes_before_services_are_ready(self) -> None:
        observed_services: list[object] = []
        original_run = main.ImageRetentionService.run

        def tracking_run(service):
            observed_services.append(service)
            return original_run(service)

        async def scenario() -> None:
            app = FastAPI()
            with patch.object(main.ImageRetentionService, "run", tracking_run):
                async with main.lifespan(app):
                    services = app.state.services
                    self.assertEqual(observed_services, [services.image_retention_service])
                    self.assertIsNotNone(
                        services.image_retention_service.snapshot().last_run
                    )
                    self.assertIs(services.health_service.phase, HealthPhase.READY)

        with _lifespan_environment():
            run_async(scenario())

    def test_thread_worker_preserves_normal_result_and_error_semantics(self) -> None:
        primary = ValueError("normal worker failure")

        def fail() -> None:
            raise primary

        async def scenario() -> None:
            self.assertEqual(
                await main.run_cancellation_resistant_thread_worker(lambda: 42),
                42,
            )
            with self.assertRaises(ValueError) as raised:
                await main.run_cancellation_resistant_thread_worker(fail)
            self.assertIs(raised.exception, primary)

        run_async(scenario())

    def test_cancelled_thread_worker_consumes_safe_failure_and_keeps_first_cancel(self) -> None:
        async def scenario() -> None:
            started = threading.Event()
            release = threading.Event()

            def fail_after_release() -> None:
                started.set()
                release.wait()
                raise RuntimeError("SECRET_WORKER_DETAIL")

            task = asyncio.create_task(
                main.run_cancellation_resistant_thread_worker(fail_after_release)
            )
            await _wait_for_thread_event(started)
            task.cancel("first cancellation")
            await asyncio.sleep(0)
            task.cancel("second cancellation")
            await asyncio.sleep(0)
            release.set()
            with self.assertLogs(main.logger, level="ERROR") as captured:
                with self.assertRaises(asyncio.CancelledError) as raised:
                    await task
            self.assertEqual(raised.exception.args, ("first cancellation",))
            diagnostics = "\n".join(captured.output)
            self.assertIn("RuntimeError", diagnostics)
            self.assertNotIn("SECRET_WORKER_DETAIL", diagnostics)

        run_async(scenario())

    def test_thread_worker_closes_to_thread_coroutine_when_task_creation_fails(self) -> None:
        primary = RuntimeError("task factory failed")

        async def scenario() -> None:
            async def worker_coroutine() -> None:
                return None

            coroutine = worker_coroutine()
            loop = asyncio.get_running_loop()
            original_factory = loop.get_task_factory()

            def failing_factory(_loop, _coroutine, **_kwargs):
                raise primary

            with patch(
                "main.asyncio.to_thread",
                new=Mock(return_value=coroutine),
            ):
                loop.set_task_factory(failing_factory)
                try:
                    with self.assertRaises(RuntimeError) as raised:
                        await main.run_cancellation_resistant_thread_worker(lambda: None)
                finally:
                    loop.set_task_factory(original_factory)
            self.assertIs(raised.exception, primary)
            self.assertIsNone(coroutine.cr_frame)

        run_async(scenario())

    def test_usage_cleanup_waits_for_blocking_worker_before_lifo_continues(self) -> None:
        async def scenario() -> None:
            started = threading.Event()
            release = threading.Event()
            mutations: list[str] = []

            def blocking_cleanup(*, retention_days: int) -> None:
                self.assertEqual(retention_days, main.USAGE_STATS_RETENTION_DAYS)
                started.set()
                release.wait()
                mutations.append("usage")

            tokens_db = Mock()
            tokens_db.cleanup_old_records = blocking_cleanup
            fallback_db = Mock()
            rejections_db = Mock()
            supervisor = TaskSupervisor()
            task = main.start_usage_stats_cleanup_task(
                tokens_db,
                fallback_db,
                rejections_db,
                supervisor=supervisor,
            )

            await _exercise_blocked_periodic_task(
                task,
                supervisor,
                started,
                release,
                mutations,
            )
            self.assertEqual(mutations, ["usage"])
            fallback_db.cleanup_old_records.assert_not_called()
            rejections_db.cleanup_old_records.assert_not_called()

        run_async(scenario())

    def test_image_cleanup_waits_for_blocking_worker_before_lifo_continues(self) -> None:
        async def scenario() -> None:
            started = threading.Event()
            release = threading.Event()
            mutations: list[str] = []

            def blocking_cleanup():
                started.set()
                release.wait()
                mutations.append("images")
                return SimpleNamespace(
                    deleted_final=1,
                    deleted_temp=0,
                    deleted_dirs=0,
                    failures=0,
                )

            supervisor = TaskSupervisor()
            image_retention_service = Mock()
            image_retention_service.run = blocking_cleanup
            task = main.start_deep_research_images_cleanup_task(
                image_retention_service,
                supervisor=supervisor,
                interval_seconds=0,
            )
            await _exercise_blocked_periodic_task(
                task,
                supervisor,
                started,
                release,
                mutations,
            )
            self.assertEqual(mutations, ["images"])

        run_async(scenario())

    def test_budget_reset_runs_only_through_accounting_service(self) -> None:
        async def scenario() -> None:
            accounting_service = Mock()
            accounting_service.reset_due_budgets = AsyncMock()
            supervisor = TaskSupervisor()
            task = main.start_budget_reset_task(
                accounting_service,
                supervisor=supervisor,
            )
            for _attempt in range(100):
                if accounting_service.reset_due_budgets.await_count:
                    break
                await asyncio.sleep(0)
            self.assertEqual(accounting_service.reset_due_budgets.await_count, 1)
            moment = accounting_service.reset_due_budgets.await_args.kwargs["now"]
            self.assertIsNotNone(moment.tzinfo)
            self.assertEqual(moment.utcoffset().total_seconds(), 0)
            await supervisor.close()
            self.assertTrue(task.done())

        run_async(scenario())

    def test_free_llm_catalog_task_runs_refresh_through_service(self) -> None:
        async def scenario() -> None:
            service = Mock()
            service.refresh_once = AsyncMock()
            service.get_status = AsyncMock(return_value={"updatedAt": None})
            fake_http_client = Mock()
            supervisor = TaskSupervisor()
            task = main.start_free_llm_catalog_task(
                service,
                http_client=fake_http_client,
                supervisor=supervisor,
            )
            for _attempt in range(100):
                if service.refresh_once.await_count:
                    break
                await asyncio.sleep(0)
            self.assertEqual(service.refresh_once.await_count, 1)
            service.refresh_once.assert_awaited_once_with(fake_http_client)
            await supervisor.close()
            self.assertTrue(task.done())

        # conftest.py defaults FREE_LLM_CATALOG_ENABLED to "false" for tests;
        # this test exercises the enabled fetch path.
        with patch.object(main.settings, "free_llm_catalog_enabled", True):
            run_async(scenario())

    def test_resource_stack_drain_finishes_every_callback_before_cancellation(self) -> None:
        async def scenario() -> None:
            events: list[str] = []
            started = asyncio.Event()
            allow_finish = asyncio.Event()
            stack = AsyncExitStack()

            async def first_cleanup() -> None:
                events.append("first")

            async def blocking_cleanup() -> None:
                events.append("blocking")
                started.set()
                await allow_finish.wait()

            stack.push_async_callback(first_cleanup)
            stack.push_async_callback(blocking_cleanup)
            drain = asyncio.create_task(main._drain_resource_stack(stack))
            await started.wait()
            drain.cancel()
            allow_finish.set()
            with self.assertRaises(asyncio.CancelledError):
                await drain
            self.assertEqual(events, ["blocking", "first"])

        run_async(scenario())

    def test_stream_capacity_close_wakes_waiters_and_drain_requires_zero(self) -> None:
        async def scenario() -> None:
            capacity = StreamObservationCapacity(max_items=1, max_bytes=1)
            lease = await capacity.acquire(1)
            waiter = asyncio.create_task(capacity.acquire(1))
            for _attempt in range(100):
                if capacity.snapshot.waiters == 1:
                    break
                await asyncio.sleep(0)
            self.assertEqual(capacity.snapshot.waiters, 1)

            with self.assertRaisesRegex(RuntimeError, "did not drain"):
                await main._verify_stream_observation_capacity_drained(capacity)

            await main._close_stream_observation_capacity(capacity)
            with self.assertRaises(StreamObservationStateError) as raised:
                await waiter
            self.assertEqual(raised.exception.reason_code, "lifespan_shutdown")

            lease.release_all()
            await main._verify_stream_observation_capacity_drained(capacity)
            snapshot = capacity.snapshot
            self.assertEqual(
                (snapshot.active_items, snapshot.active_bytes, snapshot.waiters),
                (0, 0, 0),
            )

        run_async(scenario())

    def test_upload_admission_close_wakes_waiters_and_drain_requires_zero(self) -> None:
        async def scenario() -> None:
            admission = UploadAdmission(max_bytes=1)
            lease = await admission.acquire(1)
            waiter = asyncio.create_task(admission.acquire(1))
            for _attempt in range(100):
                if admission.snapshot.waiters == 1:
                    break
                await asyncio.sleep(0)
            self.assertEqual(admission.snapshot.waiters, 1)

            with self.assertRaisesRegex(RuntimeError, "did not drain"):
                await main._verify_upload_admission_drained(admission)

            await main._close_upload_admission(admission)
            with self.assertRaises(UploadAdmissionClosed):
                await waiter

            lease.release()
            await main._verify_upload_admission_drained(admission)
            snapshot = admission.snapshot
            self.assertEqual(
                (snapshot.active_bytes, snapshot.active_leases, snapshot.waiters),
                (0, 0, 0),
            )

        run_async(scenario())

    def test_periodic_helpers_use_supervisor_with_stable_safe_names(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            tasks = (
                main.start_usage_stats_cleanup_task(
                    Mock(),
                    Mock(),
                    Mock(),
                    supervisor=supervisor,
                ),
                main.start_deep_research_images_cleanup_task(
                    Mock(),
                    supervisor=supervisor,
                ),
                main.start_budget_reset_task(
                    Mock(reset_due_budgets=AsyncMock()),
                    supervisor=supervisor,
                ),
            )
            self.assertEqual(
                tuple(task.get_name() for task in tasks),
                (
                    "usage-stats-cleanup",
                    "deep-research-images-cleanup",
                    "budget-reset",
                ),
            )
            self.assertEqual(supervisor.task_count, 3)
            await supervisor.close()
            self.assertEqual(supervisor.task_count, 0)

        run_async(scenario())

    def test_success_publishes_only_services_last_and_unwinds_exact_lifo(self) -> None:
        async def scenario() -> None:
            app = FastAPI()
            previous_services = object()
            conflicting_aliases = {
                name: object() for name in _LEGACY_STATE_ALIASES
            }
            app.state.services = previous_services
            for name, value in conflicting_aliases.items():
                setattr(app.state, name, value)
            initial_state_keys = set(app.state._state)
            async with main.lifespan(app):
                services = app.state.services
                self.assertIsNot(services, previous_services)
                env.single_process_validation.assert_called_once_with(os.environ)
                state_dir = env.single_process_acquire.call_args.args[0]
                self.assertTrue(state_dir.is_absolute())
                self.assertEqual(state_dir.resolve(strict=True), state_dir)
                self.assertTrue(state_dir.is_dir())
                env.openrouter_services[0].start_runtime.assert_awaited_once_with(
                    runtime_manager=services.runtime_manager,
                    shared_http_client=services.http_client,
                )
                for name, value in conflicting_aliases.items():
                    self.assertIs(getattr(app.state, name), value)
                self.assertEqual(
                    set(app.state._state),
                    initial_state_keys | {main._LIFESPAN_OWNER_ATTRIBUTE},
                )
                self.assertEqual(services.runtime_manager.current_generation, 1)
                self.assertEqual(
                    services.runtime_manager.pending_unpublished_generations,
                    (),
                )
                self.assertEqual(services.task_supervisor.task_count, 5)
                self.assertIs(
                    services.config_update_coordinator,
                    env.coordinators[0],
                )
                lease = services.runtime_manager.acquire_current()
                self.assertIs(lease.snapshot.config_loader, env.config_loader)
                self.assertEqual(len(env.coordinator_calls), 1)
                coordinator_manager, coordinator_client, coordinator_snapshot = (
                    env.coordinator_calls[0]
                )
                self.assertIs(coordinator_manager, services.runtime_manager)
                self.assertIs(coordinator_client, services.http_client)
                self.assertIs(coordinator_snapshot, lease.snapshot)
                verification_kwargs = env.verification.await_args.kwargs
                self.assertIs(
                    verification_kwargs["providers_config"],
                    lease.snapshot.config_loader.providers_config,
                )
                self.assertIs(
                    verification_kwargs["provider_models_service"],
                    lease.snapshot.provider_models_service,
                )
                lease.release()
                self.assertEqual(
                    env.startup_events,
                    [
                        "accounting:start",
                        "runtime:proxy",
                        "coordinator",
                        "verification",
                        "task:usage-stats-cleanup",
                        "task:deep-research-images-cleanup",
                        "task:budget-reset",
                        "task:capability-autofill",
                        "task:free-llm-catalog",
                        "deep-research:start",
                        "state:services",
                    ],
                )
                self.assertEqual(len(env.accounting_calls), 1)
                source, sink, budget_ledger, rate_limiter = env.accounting_calls[0]
                self.assertIs(source, services.tokens_usage_db)
                self.assertIs(sink, services.api_keys_db)
                self.assertIs(budget_ledger, services.usd_budget_ledger)
                self.assertIs(rate_limiter, services.rate_limiter)
                self.assertIs(services.accounting_service, env.accounting_services[0])
            self.assertIs(app.state.services, previous_services)
            for name, value in conflicting_aliases.items():
                self.assertIs(getattr(app.state, name), value)
            self.assertEqual(set(app.state._state), initial_state_keys)
            self.assertTrue(services.task_supervisor.closed)

        with _lifespan_environment() as env:
            run_async(scenario())

        self.assertEqual(
            env.events,
            [
                "deep-research",
                "state",
                "coordinator",
                "supervisor",
                "fallback",
                "openrouter",
                "manager",
                "proxy",
                "accounting",
                "write-batcher",
                "shared",
                "single-process",
            ],
        )
        env.single_process_leases[0].close.assert_awaited_once_with()
        self.assertEqual(env.state_sets, ["services"])

    def test_lifespan_closes_capacities_before_supervisor_then_verifies(self) -> None:
        async def scenario() -> None:
            app = FastAPI()
            event_limit = 12_345
            close_capacity = main._close_stream_observation_capacity
            verify_capacity = main._verify_stream_observation_capacity_drained
            close_upload = main._close_upload_admission
            verify_upload = main._verify_upload_admission_drained

            async def recording_close(capacity) -> None:
                env.events.append("stream-capacity-close")
                await close_capacity(capacity)

            async def recording_verify(capacity) -> None:
                env.events.append("stream-capacity-verify")
                await verify_capacity(capacity)

            async def recording_upload_close(admission) -> None:
                env.events.append("upload-admission-close")
                await close_upload(admission)

            async def recording_upload_verify(admission) -> None:
                env.events.append("upload-admission-verify")
                await verify_upload(admission)

            with (
                patch(
                    "main._close_stream_observation_capacity",
                    recording_close,
                ),
                patch(
                    "main._verify_stream_observation_capacity_drained",
                    recording_verify,
                ),
                patch("main._close_upload_admission", recording_upload_close),
                patch(
                    "main._verify_upload_admission_drained",
                    recording_upload_verify,
                ),
                patch.object(
                    main.settings,
                    "stream_event_max_bytes",
                    event_limit,
                ),
            ):
                async with main.lifespan(app):
                    services = app.state.services
                    self.assertEqual(
                        services.stream_event_max_bytes,
                        event_limit,
                    )
                    self.assertEqual(
                        services.upload_admission_timeout_seconds,
                        main.settings.upload_admission_timeout_seconds,
                    )

            self.assertTrue(services.task_supervisor.closed)
            for capacity in (
                services.stream_observation_capacity,
                services.json_response_capacity,
            ):
                snapshot = capacity.snapshot
                self.assertEqual(
                    (snapshot.active_items, snapshot.active_bytes, snapshot.waiters),
                    (0, 0, 0),
                )
            upload_snapshot = services.upload_admission.snapshot
            self.assertEqual(
                (
                    upload_snapshot.active_bytes,
                    upload_snapshot.active_leases,
                    upload_snapshot.waiters,
                ),
                (0, 0, 0),
            )

        with _lifespan_environment() as env:
            run_async(scenario())

        stream_shutdown_events = [
            event
            for event in env.events
            if event
            in {
                "stream-capacity-close",
                "upload-admission-close",
                "supervisor",
                "stream-capacity-verify",
                "upload-admission-verify",
            }
        ]
        self.assertEqual(
            stream_shutdown_events,
            [
                "upload-admission-close",
                "stream-capacity-close",
                "stream-capacity-close",
                "supervisor",
                "upload-admission-verify",
                "stream-capacity-verify",
                "stream-capacity-verify",
            ],
        )

    def test_shutdown_leaves_foreign_services_replacement_untouched(self) -> None:
        async def scenario() -> None:
            app = FastAPI()
            previous_services = object()
            foreign_services = object()
            app.state.services = previous_services
            async with main.lifespan(app):
                self.assertIsNot(app.state.services, previous_services)
                app.state.services = foreign_services
            self.assertIs(app.state.services, foreign_services)

        with _lifespan_environment() as env:
            run_async(scenario())

        self.assertEqual(env.events.count("state"), 1)
        env.shared_clients[0].aclose.assert_awaited_once()
        env.proxy_clients[0].aclose.assert_awaited_once()

    def test_coordinator_construction_failure_is_safe_and_drains_prior_resources(
        self,
    ) -> None:
        primary = ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)

        async def scenario() -> None:
            app = FastAPI()
            with patch("main.ConfigUpdateCoordinator", side_effect=primary):
                with self.assertRaises(ConfigUpdateError) as raised:
                    async with main.lifespan(app):
                        self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)
            self.assertNotIn("secret", str(raised.exception))
            self.assertFalse(hasattr(app.state, "services"))

        with _lifespan_environment() as env:
            run_async(scenario())

        self.assertEqual(
            env.events,
            [
                "supervisor",
                "fallback",
                "openrouter",
                "manager",
                "proxy",
                "accounting",
                "write-batcher",
                "shared",
                "single-process",
            ],
        )

    def test_coordinator_close_failure_is_safe_and_does_not_stop_lifo_drain(
        self,
    ) -> None:
        async def scenario() -> None:
            app = FastAPI()
            async with main.lifespan(app):
                env.coordinators[0].close_error = RuntimeError(
                    "coordinator cleanup secret"
                )

        with _lifespan_environment() as env:
            with self.assertLogs(main.logger, level="ERROR") as captured:
                run_async(scenario())

        self.assertEqual(
            env.events,
            [
                "deep-research",
                "state",
                "coordinator",
                "supervisor",
                "fallback",
                "openrouter",
                "manager",
                "proxy",
                "accounting",
                "write-batcher",
                "shared",
                "single-process",
            ],
        )
        diagnostics = "\n".join(captured.output)
        self.assertIn("config-update-coordinator", diagnostics)
        self.assertIn("RuntimeError", diagnostics)
        self.assertNotIn("coordinator cleanup secret", diagnostics)

    def test_accounting_stop_failure_propagates_after_complete_lifo_drain(self) -> None:
        primary = RuntimeError("accounting stop secret")

        async def failing_stop() -> None:
            env.events.append("accounting")
            raise primary

        async def scenario() -> None:
            app = FastAPI()
            with self.assertRaises(RuntimeError) as raised:
                async with main.lifespan(app):
                    env.accounting_services[0].stop.side_effect = failing_stop
            self.assertIs(raised.exception, primary)
            self.assertFalse(hasattr(app.state, "services"))

        with _lifespan_environment() as env:
            with self.assertLogs(main.logger, level="ERROR") as captured:
                run_async(scenario())

        self.assertEqual(
            env.events[-4:],
            ["accounting", "write-batcher", "shared", "single-process"],
        )
        diagnostics = "\n".join(captured.output)
        self.assertIn("lifespan-resource-stack", diagnostics)
        self.assertIn("RuntimeError", diagnostics)
        self.assertNotIn("accounting stop secret", diagnostics)

    def test_partial_proxy_construction_preserves_primary_exception_identity(self) -> None:
        async def scenario() -> None:
            for primary in (
                RuntimeError("unsafe regular failure"),
                asyncio.CancelledError("unsafe cancellation"),
                KeyboardInterrupt("unsafe interrupt"),
                SystemExit("unsafe exit"),
            ):
                with self.subTest(exception_type=type(primary).__name__):
                    manager = RuntimeGenerationManager()
                    config_loader = Mock()
                    config_loader.providers_config = {}
                    client = Mock()
                    client.aclose = AsyncMock()

                    async def fail_after_registration(
                        _providers,
                        *,
                        register_client,
                    ) -> dict[str, Mock]:
                        register_client("partial", client)
                        raise primary

                    with patch(
                        "llm_gateway_core.services.runtime_candidate.populate_proxy_http_clients",
                        side_effect=fail_after_registration,
                    ):
                        with self.assertRaises(type(primary)) as raised:
                            await main._build_initial_snapshot(
                                manager,
                                config_loader,
                                Mock(),
                            )

                    self.assertIs(raised.exception, primary)
                    client.aclose.assert_awaited_once()
                    self.assertEqual(manager.pending_unpublished_generations, ())
                    await manager.shutdown()
                    self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)

        run_async(scenario())

    def test_failed_partial_proxy_cleanup_stays_manager_owned_until_recovery(
        self,
    ) -> None:
        primary = RuntimeError("unsafe construction failure")

        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            config_loader = Mock()
            config_loader.providers_config = {}
            client = Mock()
            client.aclose = AsyncMock(side_effect=RuntimeError("cleanup secret"))

            async def fail_after_registration(
                _providers,
                *,
                register_client,
            ) -> dict[str, Mock]:
                register_client("partial", client)
                raise primary

            with patch(
                "llm_gateway_core.services.runtime_candidate.populate_proxy_http_clients",
                side_effect=fail_after_registration,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    await main._build_initial_snapshot(manager, config_loader, Mock())

            self.assertIs(raised.exception, primary)
            self.assertEqual(
                client.aclose.await_count,
                RUNTIME_CLEANUP_MAX_ATTEMPTS,
            )
            self.assertEqual(manager.pending_unpublished_generations, (1,))

            await manager.shutdown()

            self.assertEqual(manager.status, RuntimeManagerStatus.STOP_FAILED)
            self.assertEqual(manager.pending_unpublished_generations, (1,))
            self.assertEqual(
                client.aclose.await_count,
                RUNTIME_CLEANUP_MAX_ATTEMPTS * 3,
            )
            self.assertNotIn("cleanup secret", repr(manager.cleanup_failures))

            client.aclose.side_effect = None
            await manager.shutdown()

            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.pending_unpublished_generations, ())

        run_async(scenario())

    def test_candidate_proxy_remains_manager_owned_when_install_fails(self) -> None:
        primary = RuntimeError("unsafe install failure")

        class FailingManager(RuntimeGenerationManager):
            def install_initial_candidate(self, snapshot, *, slot) -> None:
                raise primary

        async def scenario() -> None:
            app = FastAPI()
            with patch("main.RuntimeGenerationManager", FailingManager):
                with self.assertRaises(RuntimeError) as raised:
                    async with main.lifespan(app):
                        self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)
            self.assertFalse(hasattr(app.state, "services"))

        with _lifespan_environment() as env:
            run_async(scenario())

        env.proxy_clients[0].aclose.assert_awaited_once()
        env.shared_clients[0].aclose.assert_awaited_once()
        self.assertEqual(env.events.count("proxy"), 1)

    def test_late_primary_error_survives_cleanup_failures_and_full_drain(self) -> None:
        primary = SystemExit("primary secret")

        async def fail_fallback_stop() -> None:
            env.events.append("fallback")
            raise RuntimeError("cleanup secret")

        async def scenario() -> None:
            app = FastAPI()
            with self.assertRaises(SystemExit) as raised:
                async with main.lifespan(app):
                    env.fallback_services[0].stop.side_effect = fail_fallback_stop
                    raise primary
            self.assertIs(raised.exception, primary)
            self.assertFalse(hasattr(app.state, "services"))

        with _lifespan_environment() as env:
            with self.assertLogs(main.logger, level="ERROR") as captured:
                run_async(scenario())

        diagnostics = "\n".join(captured.output)
        self.assertIn("fallback-model-eval-service", diagnostics)
        self.assertIn("RuntimeError", diagnostics)
        self.assertNotIn("cleanup secret", diagnostics)
        self.assertEqual(
            env.events[-6:],
            [
                "manager",
                "proxy",
                "accounting",
                "write-batcher",
                "shared",
                "single-process",
            ],
        )

    def test_representative_startup_faults_leave_no_state_or_client_leak(self) -> None:
        async def run_fault(stage: str, primary: BaseException) -> None:
            app = FastAPI()
            if stage == "shared-client":
                patcher = patch("main.create_shared_http_client", side_effect=primary)
            elif stage == "tokens-db":
                patcher = patch("main.TokensUsageDB", side_effect=primary)
            elif stage == "write-batcher":
                env.batchers.clear()

                def failing_batcher(_path, *, queue_maxsize) -> Mock:
                    assert queue_maxsize == main.settings.write_batcher_queue_maxsize
                    batcher = Mock()
                    batcher.start = AsyncMock(side_effect=primary)
                    batcher.stop = AsyncMock()
                    env.batchers.append(batcher)
                    return batcher

                patcher = patch("main.WriteBatcher", side_effect=failing_batcher)
            elif stage == "accounting":
                env.accounting_services.clear()

                def failing_accounting(*_args, **_kwargs) -> Mock:
                    service = Mock()
                    service.start = AsyncMock(side_effect=primary)
                    service.stop = AsyncMock()
                    env.accounting_services.append(service)
                    return service

                patcher = patch(
                    "main.AccountingService",
                    side_effect=failing_accounting,
                )
            elif stage == "proxy":
                patcher = patch(
                    "llm_gateway_core.services.runtime_candidate.populate_proxy_http_clients",
                    AsyncMock(side_effect=primary),
                )
            elif stage == "openrouter":
                env.openrouter_services.clear()

                def failing_openrouter() -> Mock:
                    service = Mock()
                    service.start_runtime = AsyncMock(side_effect=primary)
                    service.stop = AsyncMock()
                    env.openrouter_services.append(service)
                    return service

                patcher = patch(
                    "main.OpenRouterFreeModelsService",
                    side_effect=failing_openrouter,
                )
            elif stage == "build":
                patcher = patch(
                    "llm_gateway_core.services.runtime_candidate.OperationDispatcher",
                    side_effect=primary,
                )
            else:
                patcher = patch(
                    "main.run_startup_model_verification",
                    AsyncMock(side_effect=primary),
                )

            with patcher:
                with self.assertRaises(type(primary)) as raised:
                    async with main.lifespan(app):
                        self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)
            self.assertFalse(hasattr(app.state, "services"))

        for stage in (
            "shared-client",
            "tokens-db",
            "write-batcher",
            "accounting",
            "proxy",
            "build",
            "openrouter",
            "verification",
        ):
            with self.subTest(stage=stage), _lifespan_environment() as env:
                run_async(run_fault(stage, RuntimeError(f"unsafe {stage}")))
                if stage != "shared-client":
                    env.shared_clients[0].aclose.assert_awaited_once()
                if stage == "write-batcher":
                    env.batchers[0].stop.assert_awaited_once()
                if stage == "accounting":
                    env.accounting_services[0].stop.assert_awaited_once()
                if stage == "openrouter":
                    env.openrouter_services[0].stop.assert_awaited_once()
                self.assertEqual(len(env.single_process_leases), 1)
                env.single_process_leases[0].close.assert_awaited_once_with()
                self.assertEqual(env.events[-1], "single-process")

    def test_each_supervised_task_creation_failure_cancels_prior_tasks(self) -> None:
        for failure_index in (1, 2, 3):
            with self.subTest(failure_index=failure_index):
                primary = RuntimeError(f"unsafe task creation {failure_index}")
                supervisors: list[TaskSupervisor] = []

                class FailNthTaskSupervisor(TaskSupervisor):
                    def __init__(self) -> None:
                        super().__init__()
                        self.created = 0
                        supervisors.append(self)

                    def create_task(self, coroutine, *, name):
                        self.created += 1
                        if self.created == failure_index:
                            coroutine.close()
                            raise primary
                        return super().create_task(coroutine, name=name)

                async def scenario() -> None:
                    app = FastAPI()
                    with patch("main.TaskSupervisor", FailNthTaskSupervisor):
                        with self.assertRaises(RuntimeError) as raised:
                            async with main.lifespan(app):
                                self.fail("startup unexpectedly succeeded")
                    self.assertIs(raised.exception, primary)
                    self.assertFalse(hasattr(app.state, "services"))

                with _lifespan_environment() as env:
                    run_async(scenario())

                self.assertEqual(len(supervisors), 1)
                self.assertTrue(supervisors[0].closed)
                self.assertEqual(supervisors[0].task_count, 0)
                env.shared_clients[0].aclose.assert_awaited_once()
                env.proxy_clients[0].aclose.assert_awaited_once()

    def test_services_bind_failure_restores_prior_value_and_drains_once(self) -> None:
        primary = KeyboardInterrupt("unsafe services setattr")

        async def scenario() -> None:
            class FailingStateBinding(main._TemporaryStateBinding):
                def _set(self, name: str, value: object) -> None:
                    super()._set(name, value)
                    raise primary

            app = FastAPI()
            previous_services = object()
            conflicting_aliases = {
                name: object() for name in _LEGACY_STATE_ALIASES
            }
            app.state.services = previous_services
            for name, value in conflicting_aliases.items():
                setattr(app.state, name, value)
            with patch("main._TemporaryStateBinding", FailingStateBinding):
                with self.assertRaises(KeyboardInterrupt) as raised:
                    async with main.lifespan(app):
                        self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)
            self.assertIs(app.state.services, previous_services)
            for name, value in conflicting_aliases.items():
                self.assertIs(getattr(app.state, name), value)

        with _lifespan_environment() as env:
            run_async(scenario())

        self.assertEqual(
            env.events,
            [
                "deep-research",
                "state",
                "coordinator",
                "supervisor",
                "fallback",
                "openrouter",
                "manager",
                "proxy",
                "accounting",
                "write-batcher",
                "shared",
                "single-process",
            ],
        )
        env.shared_clients[0].aclose.assert_awaited_once()
        env.proxy_clients[0].aclose.assert_awaited_once()
        env.batchers[0].stop.assert_awaited_once()
        env.openrouter_services[0].stop.assert_awaited_once()
        env.fallback_services[0].stop.assert_awaited_once()
        env.single_process_leases[0].close.assert_awaited_once()

    def test_startup_base_exception_identity_is_preserved(self) -> None:
        async def scenario(primary: BaseException) -> None:
            app = FastAPI()
            env.verification.side_effect = primary
            with self.assertRaises(type(primary)) as raised:
                async with main.lifespan(app):
                    self.fail("startup unexpectedly succeeded")
            self.assertIs(raised.exception, primary)

        for primary in (
            asyncio.CancelledError("cancelled secret"),
            KeyboardInterrupt("keyboard secret"),
        ):
            with self.subTest(exception_type=type(primary).__name__):
                with _lifespan_environment() as env:
                    run_async(scenario(primary))
                    env.single_process_leases[0].close.assert_awaited_once_with()
                    self.assertEqual(env.events[-1], "single-process")

    def test_body_cancellation_preserves_identity_and_releases_lease_last(self) -> None:
        primary = asyncio.CancelledError("body cancellation")

        async def scenario() -> None:
            app = FastAPI()
            with self.assertRaises(asyncio.CancelledError) as raised:
                async with main.lifespan(app):
                    raise primary
            self.assertIs(raised.exception, primary)

        with _lifespan_environment() as env:
            run_async(scenario())

        env.single_process_leases[0].close.assert_awaited_once_with()
        self.assertEqual(env.events[-1], "single-process")

    def test_lease_close_failure_is_safe_last_and_never_masks_primary(self) -> None:
        expected_events = [
            "deep-research",
            "state",
            "coordinator",
            "supervisor",
            "fallback",
            "openrouter",
            "manager",
            "proxy",
            "accounting",
            "write-batcher",
            "shared",
            "single-process",
        ]
        for primary in (None, SystemExit("primary shutdown failure")):
            with self.subTest(primary_type=type(primary).__name__):
                async def scenario() -> None:
                    app = FastAPI()
                    if primary is None:
                        async with main.lifespan(app):
                            env.single_process_leases[0].close.side_effect = failing_close
                        return
                    with self.assertRaises(SystemExit) as raised:
                        async with main.lifespan(app):
                            env.single_process_leases[0].close.side_effect = failing_close
                            raise primary
                    self.assertIs(raised.exception, primary)

                async def failing_close() -> None:
                    env.events.append("single-process")
                    raise RuntimeError("lease cleanup secret")

                with _lifespan_environment() as env:
                    with self.assertLogs(main.logger, level="ERROR") as captured:
                        run_async(scenario())

                self.assertEqual(env.events, expected_events)
                env.single_process_leases[0].close.assert_awaited_once_with()
                diagnostics = "\n".join(captured.output)
                self.assertIn("single-process-lease", diagnostics)
                self.assertIn("RuntimeError", diagnostics)
                self.assertNotIn("lease cleanup secret", diagnostics)

    def test_sequential_lifespans_are_fresh_and_overlap_is_rejected(self) -> None:
        async def scenario() -> None:
            app = FastAPI()
            managers = []
            supervisors = []
            accounting_services = []
            for _index in range(2):
                async with main.lifespan(app):
                    managers.append(app.state.services.runtime_manager)
                    supervisors.append(app.state.services.task_supervisor)
                    accounting_services.append(app.state.services.accounting_service)
                self.assertFalse(hasattr(app.state, "services"))
            self.assertIsNot(managers[0], managers[1])
            self.assertIsNot(supervisors[0], supervisors[1])
            self.assertIsNot(accounting_services[0], accounting_services[1])

            async with main.lifespan(app):
                acquired_before_overlap = len(env.shared_clients)
                with self.assertRaisesRegex(RuntimeError, "already active"):
                    main._claim_lifespan_owner(app.state)
                self.assertEqual(len(env.shared_clients), acquired_before_overlap)

        with _lifespan_environment() as env:
            run_async(scenario())

    def test_sequential_test_clients_get_fresh_cross_loop_owners(self) -> None:
        managers = []
        supervisors = []
        accounting_services = []
        with _lifespan_environment():
            for _index in range(2):
                with TestClient(main.app) as client:
                    managers.append(client.app.state.services.runtime_manager)
                    supervisors.append(client.app.state.services.task_supervisor)
                    accounting_services.append(
                        client.app.state.services.accounting_service
                    )
                self.assertFalse(hasattr(main.app.state, "services"))

        self.assertIsNot(managers[0], managers[1])
        self.assertIsNot(supervisors[0], supervisors[1])
        self.assertIsNot(accounting_services[0], accounting_services[1])
        self.assertTrue(all(supervisor.closed for supervisor in supervisors))

    def test_cross_thread_lifespan_owner_claim_is_atomic(self) -> None:
        owner_read_barrier = threading.Barrier(2)

        class RacingState(State):
            def __getattr__(self, key: str):
                if (
                    key == main._LIFESPAN_OWNER_ATTRIBUTE
                    and key not in self._state
                ):
                    try:
                        owner_read_barrier.wait(timeout=0.2)
                    except threading.BrokenBarrierError:
                        pass
                return super().__getattr__(key)

        app = FastAPI()
        app.state = RacingState()
        release_owner = threading.Event()
        outcomes: list[tuple[str, str]] = []
        outcomes_lock = threading.Lock()
        outcome_changed = threading.Event()

        def record(label: str, outcome: str) -> None:
            with outcomes_lock:
                outcomes.append((label, outcome))
                outcome_changed.set()

        def worker(label: str) -> None:
            claim: tuple[object, bool, object] | None = None
            try:
                claim = main._claim_lifespan_owner(app.state)
                record(label, "entered")
                release_owner.wait()
            except RuntimeError as exc:
                if str(exc) == "Application lifespan is already active.":
                    record(label, "rejected")
                else:
                    record(label, f"unexpected:{type(exc).__name__}")
            finally:
                if claim is not None:
                    main._release_lifespan_owner(app.state, *claim)

        threads = [
            threading.Thread(target=worker, args=(label,))
            for label in ("first", "second")
        ]
        for thread in threads:
            thread.start()
        try:
            for _attempt in range(200):
                with outcomes_lock:
                    if len(outcomes) == 2:
                        break
                    outcome_changed.clear()
                outcome_changed.wait(timeout=0.05)
            with outcomes_lock:
                observed = [outcome for _label, outcome in outcomes]
            self.assertCountEqual(observed, ["entered", "rejected"])
        finally:
            release_owner.set()
            for thread in threads:
                thread.join(timeout=5)
        self.assertTrue(all(not thread.is_alive() for thread in threads))
        self.assertFalse(hasattr(app.state, main._LIFESPAN_OWNER_ATTRIBUTE))

        claim = main._claim_lifespan_owner(app.state)
        self.assertIs(
            getattr(app.state, main._LIFESPAN_OWNER_ATTRIBUTE),
            claim[0],
        )
        main._release_lifespan_owner(app.state, *claim)
        self.assertFalse(hasattr(app.state, main._LIFESPAN_OWNER_ATTRIBUTE))


if __name__ == "__main__":
    unittest.main()
