import asyncio
import inspect
import unittest
from unittest.mock import AsyncMock, Mock

from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.services.fallback_model_evals import (
    FallbackModelEvalAlreadyRunning,
    FallbackModelEvalService,
    FallbackModelEvalSnapshot,
    FallbackModelEvalStateError,
)
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_runtime_snapshot,
    publish_test_runtime_snapshot,
)


def _loader(provider_name: str, model_name: str) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = {
        provider_name: ProviderDetails(
            baseUrl=f"https://{provider_name}.example/v1",
            apikey=f"{provider_name}-key",
        )
    }
    loader.fallback_rules = {
        f"gateway/{model_name}": {
            "fallback_models": [
                {"provider": provider_name, "model": model_name}
            ]
        }
    }
    loader._fallback_rules_base = {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    return loader


def _eval_snapshot() -> FallbackModelEvalSnapshot:
    return FallbackModelEvalSnapshot(
        updated_at="2026-07-12T00:00:00Z",
        source="test",
        refresh_mode="manualEval",
        ranking_version="test",
        configured_count=0,
        evaluated_count=0,
        models=[],
    )


async def _wait_for(predicate) -> None:
    for _attempt in range(20):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class FallbackRuntimeLeaseTests(unittest.TestCase):
    def test_loop_mismatch_releases_new_loop_lease(self) -> None:
        service = FallbackModelEvalService()

        async def bind_first_loop() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()
            service._build_snapshot = AsyncMock(  # type: ignore[method-assign]
                return_value=_eval_snapshot()
            )
            await service.start_eval_with_runtime(
                runtime_lease=lease,
                http_client=Mock(),
            )
            task = service._task
            self.assertIsNotNone(task)
            await task
            self.assertTrue(lease.released)
            await manager.shutdown()

        async def reject_on_second_loop() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()

            with self.assertRaisesRegex(
                FallbackModelEvalStateError,
                "another event loop",
            ):
                await service.start_eval_with_runtime(
                    runtime_lease=lease,
                    http_client=Mock(),
                )

            self.assertTrue(lease.released)
            self.assertFalse(any(manager.active_leases.values()))
            self.assertIsNone(service._task)
            await manager.shutdown()

        run_async(bind_first_loop())
        run_async(reject_on_second_loop())

    def test_released_retired_lease_is_rejected_before_task_or_build(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            released_lease = manager.acquire_current()
            released_lease.release()
            publish_test_runtime_snapshot(manager,
                make_runtime_snapshot(generation=2),
                expected_generation=1,
            )
            service = FallbackModelEvalService()
            build_snapshot = AsyncMock(return_value=_eval_snapshot())
            service._build_snapshot = build_snapshot  # type: ignore[method-assign]

            with self.assertRaisesRegex(
                FallbackModelEvalStateError,
                "runtime lease is unavailable",
            ):
                await service.start_eval_with_runtime(
                    runtime_lease=released_lease,
                    http_client=Mock(),
                )

            build_snapshot.assert_not_awaited()
            self.assertTrue(released_lease.released)
            self.assertIsNone(service._task)
            self.assertFalse(service._running)
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def test_detached_eval_owns_retired_generation_until_task_finishes(self) -> None:
        async def scenario() -> None:
            old_proxy = Mock()
            old_proxy.aclose = AsyncMock()
            new_proxy = Mock()
            new_proxy.aclose = AsyncMock()
            shared_client = Mock()
            shared_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            first = make_runtime_snapshot(
                generation=1,
                config_loader=_loader("provider-n", "model-n"),
                proxy_http_clients={"provider-n": old_proxy},
            )
            install_test_runtime_snapshot(manager, first)
            request_lease = manager.acquire_current()
            child_lease = request_lease.retain()
            service = FallbackModelEvalService()
            started = asyncio.Event()
            finish = asyncio.Event()
            captured: dict[str, object] = {}

            async def blocked_build(**kwargs):
                captured.update(kwargs)
                started.set()
                await finish.wait()
                return _eval_snapshot()

            service._build_snapshot = blocked_build  # type: ignore[method-assign]
            await service.start_eval_with_runtime(
                runtime_lease=child_lease,
                http_client=shared_client,
            )
            task = service._task
            self.assertIsNotNone(task)
            await started.wait()

            second = make_runtime_snapshot(
                generation=2,
                config_loader=_loader("provider-next", "model-next"),
                proxy_http_clients={"provider-next": new_proxy},
            )
            publish_test_runtime_snapshot(manager, second, expected_generation=1)
            request_lease.release()
            self.assertEqual(manager.active_leases[1], 1)
            old_proxy.aclose.assert_not_awaited()

            self.assertIs(captured["providers_config"], first.config_loader.providers_config)
            self.assertIs(captured["fallback_rules"], first.config_loader.fallback_rules)
            self.assertIs(captured["proxy_http_clients"], first.proxy_http_clients)
            self.assertIs(captured["http_client"], shared_client)
            self.assertNotIn("provider-next", captured["providers_config"])

            finish.set()
            await task
            await _wait_for(lambda: old_proxy.aclose.await_count == 1)

            self.assertTrue(child_lease.released)
            self.assertFalse(any(manager.active_leases.values()))
            old_proxy.aclose.assert_awaited_once()
            shared_client.aclose.assert_not_awaited()
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()
            new_proxy.aclose.assert_awaited_once()

        run_async(scenario())

    def test_already_running_releases_rejected_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader("provider", "model"),
                )
            )
            base_lease = manager.acquire_current()
            running_lease = base_lease.retain()
            rejected_lease = base_lease.retain()
            service = FallbackModelEvalService()
            started = asyncio.Event()

            async def blocked_build(**_kwargs):
                started.set()
                await asyncio.Event().wait()

            service._build_snapshot = blocked_build  # type: ignore[method-assign]
            await service.start_eval_with_runtime(
                runtime_lease=running_lease,
                http_client=Mock(),
            )
            await started.wait()

            with self.assertRaises(FallbackModelEvalAlreadyRunning):
                await service.start_eval_with_runtime(
                    runtime_lease=rejected_lease,
                    http_client=Mock(),
                )

            self.assertTrue(rejected_lease.released)
            self.assertFalse(running_lease.released)
            self.assertEqual(manager.active_leases[1], 2)
            await service.stop()
            base_lease.release()
            self.assertFalse(any(manager.active_leases.values()))
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def test_cancelled_lock_admission_releases_lease_without_task(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()
            service = FallbackModelEvalService()
            await service._lock.acquire()
            try:
                caller = asyncio.create_task(
                    service.start_eval_with_runtime(
                        runtime_lease=lease,
                        http_client=Mock(),
                    )
                )
                await asyncio.sleep(0)
                caller.cancel()
            finally:
                service._lock.release()

            with self.assertRaises(asyncio.CancelledError):
                await caller
            self.assertTrue(lease.released)
            self.assertIsNone(service._task)
            self.assertFalse(service._running)
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def test_stopping_rejects_and_releases_new_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            base_lease = manager.acquire_current()
            running_lease = base_lease.retain()
            rejected_lease = base_lease.retain()
            service = FallbackModelEvalService()
            started = asyncio.Event()
            cancelled = asyncio.Event()
            allow_cancel = asyncio.Event()

            async def cancellation_blocked_build(**_kwargs):
                started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancelled.set()
                    await allow_cancel.wait()
                    raise

            service._build_snapshot = cancellation_blocked_build  # type: ignore[method-assign]
            await service.start_eval_with_runtime(
                runtime_lease=running_lease,
                http_client=Mock(),
            )
            await started.wait()
            stop_task = asyncio.create_task(service.stop())
            await cancelled.wait()

            with self.assertRaisesRegex(FallbackModelEvalStateError, "stopping"):
                await service.start_eval_with_runtime(
                    runtime_lease=rejected_lease,
                    http_client=Mock(),
                )

            self.assertTrue(rejected_lease.released)
            allow_cancel.set()
            await stop_task
            base_lease.release()
            self.assertTrue(running_lease.released)
            self.assertFalse(any(manager.active_leases.values()))
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def test_task_factory_failure_closes_coroutine_and_releases_lease(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()
            service = FallbackModelEvalService()
            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            observed_coroutines: list[object] = []

            def failing_factory(_loop, coroutine, **_kwargs):
                observed_coroutines.append(coroutine)
                raise RuntimeError("task factory failed")

            loop.set_task_factory(failing_factory)
            try:
                with self.assertRaisesRegex(RuntimeError, "task factory failed"):
                    await service.start_eval_with_runtime(
                        runtime_lease=lease,
                        http_client=Mock(),
                    )
            finally:
                loop.set_task_factory(previous_factory)

            self.assertEqual(len(observed_coroutines), 1)
            self.assertEqual(
                inspect.getcoroutinestate(observed_coroutines[0]),
                inspect.CORO_CLOSED,
            )
            self.assertTrue(lease.released)
            self.assertIsNone(service._task)
            self.assertFalse(service._running)
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    @unittest.skipUnless(
        hasattr(asyncio, "eager_task_factory"),
        "Python 3.12 eager task factory is required",
    )
    def test_eager_failure_runs_only_after_commit_and_reports_safe_error(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()
            service = FallbackModelEvalService()
            observed_commit: list[bool] = []
            secret = "https://user:credential@proxy.invalid"

            async def failing_build(**_kwargs):
                observed_commit.append(
                    service._running
                    and service._task is asyncio.current_task()
                )
                raise RuntimeError(secret)

            service._build_snapshot = failing_build  # type: ignore[method-assign]
            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                await service.start_eval_with_runtime(
                    runtime_lease=lease,
                    http_client=Mock(),
                )
            finally:
                loop.set_task_factory(previous_factory)

            await _wait_for(lambda: lease.released)
            status = await service.get_status()
            self.assertEqual(observed_commit, [True])
            self.assertNotIn("credential", status["lastError"])
            self.assertIn("RuntimeError", status["lastError"])
            self.assertFalse(status["running"])
            self.assertIsNone(service._task)
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def test_old_task_cannot_clear_replacement_task_reference(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, make_runtime_snapshot(generation=1))
            lease = manager.acquire_current()
            service = FallbackModelEvalService()
            started = asyncio.Event()
            finish = asyncio.Event()

            async def blocked_build(**_kwargs):
                started.set()
                await finish.wait()
                return _eval_snapshot()

            service._build_snapshot = blocked_build  # type: ignore[method-assign]
            await service.start_eval_with_runtime(
                runtime_lease=lease,
                http_client=Mock(),
            )
            old_task = service._task
            self.assertIsNotNone(old_task)
            await started.wait()
            replacement_task = asyncio.create_task(
                asyncio.Event().wait(),
                name="fallback-model-eval-replacement-test",
            )
            service._task = replacement_task
            service._running = True

            finish.set()
            await old_task
            self.assertIs(service._task, replacement_task)
            self.assertTrue(service._running)
            self.assertTrue(lease.released)

            replacement_task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await replacement_task
            service._task = None
            service._running = False
            self._assert_no_pending_eval_tasks()
            await manager.shutdown()

        run_async(scenario())

    def _assert_no_pending_eval_tasks(self) -> None:
        pending = [
            task
            for task in asyncio.all_tasks()
            if task is not asyncio.current_task()
            and not task.done()
            and task.get_name() == "fallback-model-eval"
        ]
        self.assertEqual(pending, [])
