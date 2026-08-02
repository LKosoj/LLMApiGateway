import asyncio
import inspect
import unittest

from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.services.openrouter_free_models import (
    OpenRouterFreeModelsNotConfigured,
    OpenRouterFreeModelsService,
)
from llm_gateway_core.services.runtime_config import RuntimeGenerationManager
from tests._async_compat import run_async
from tests.lite_eval_support import perfect_lite_eval_answer
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    make_runtime_snapshot,
    publish_test_runtime_snapshot,
)


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _RuntimeClient:
    def __init__(
        self,
        *,
        block_first_get: bool = False,
        catalog: list[dict[str, object]] | None = None,
    ) -> None:
        self.block_get_number = 1 if block_first_get else None
        self.catalog = list(catalog or [])
        self.get_started = asyncio.Event()
        self.allow_get = asyncio.Event()
        self.get_count = 0
        self.close_count = 0
        self.get_calls: list[tuple[str, str]] = []
        self.post_calls: list[tuple[str, str]] = []

    async def get(self, url: str, *, headers: dict[str, str]) -> _Response:
        self.get_count += 1
        self.get_calls.append((url, headers["Authorization"]))
        self.get_started.set()
        if self.block_get_number == self.get_count:
            await self.allow_get.wait()
        return _Response({"data": self.catalog})

    async def post(
        self,
        url: str,
        *,
        headers: dict[str, str],
        json: dict[str, object],
        timeout: float,
    ) -> _Response:
        del timeout
        self.post_calls.append((url, headers["Authorization"]))
        messages = json["messages"]
        assert isinstance(messages, list)
        prompt = messages[0]["content"]
        if "Reply with exactly OK" in prompt:
            content = "OK"
        else:
            content = perfect_lite_eval_answer(prompt)
            if not content:
                raise AssertionError(f"unexpected OpenRouter prompt: {prompt!r}")
        return _Response({"choices": [{"message": {"content": content}}]})

    async def aclose(self) -> None:
        self.close_count += 1


def _loader(
    *,
    api_key: str | None = None,
    base_url: str = "https://openrouter.ai/api/v1",
) -> ConfigLoader:
    loader = ConfigLoader()
    loader.providers_config = (
        {
            "openrouter": ProviderDetails(
                baseUrl=base_url,
                apikey=api_key,
            )
        }
        if api_key is not None
        else {}
    )
    loader.fallback_rules = {}
    loader._fallback_rules_base = {}
    loader.operation_rules = {}
    loader.fusion_rules = {}
    loader.model_rules = {}
    loader.router_rules = {}
    return loader


def _eligible_model() -> dict[str, object]:
    return {
        "id": "provider/runtime:free",
        "name": "Runtime Free",
        "created": 1_760_000_000,
        "context_length": 262_144,
        "top_provider": {"max_completion_tokens": 32_768},
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": [
            "tools",
            "structured_outputs",
            "response_format",
            "seed",
            "stop",
        ],
        "architecture": {"output_modalities": ["text"]},
        "expiration_date": None,
    }


async def _wait_until(predicate) -> None:
    for _attempt in range(50):
        if predicate():
            return
        await asyncio.sleep(0)
    raise AssertionError("condition was not reached")


class OpenRouterRuntimeLeaseTests(unittest.TestCase):
    def test_periodic_and_manual_runs_keep_generation_client_until_both_finish(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            catalog = [_eligible_model()]
            client_n = _RuntimeClient(
                block_first_get=True,
                catalog=catalog,
            )
            client_n1 = _RuntimeClient(catalog=catalog)
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(
                        api_key="secret-n",
                        base_url="https://openrouter.ai/api/runtime-n",
                    ),
                    proxy_http_clients={"openrouter": client_n},
                )
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            try:
                await service.start_runtime(
                    runtime_manager=manager,
                    shared_http_client=shared_client,
                )
                await client_n.get_started.wait()

                self.assertTrue(await service.start_manual_full_refresh())
                manual_task = service._manual_refresh_task
                self.assertIsNotNone(manual_task)
                self.assertEqual(manager.active_leases[1], 2)

                publish_test_runtime_snapshot(manager,
                    make_runtime_snapshot(
                        generation=2,
                        config_loader=_loader(
                            api_key="secret-n1",
                            base_url="https://openrouter.ai/api/runtime-n1",
                        ),
                        proxy_http_clients={"openrouter": client_n1},
                    ),
                    expected_generation=1,
                )
                self.assertEqual(client_n.close_count, 0)

                client_n.allow_get.set()
                await manual_task
                await _wait_until(lambda: client_n.close_count == 1)

                self.assertEqual(client_n.get_count, 2)
                self.assertNotIn(1, manager.active_leases)
                await service.refresh_once()
                self.assertEqual(client_n1.get_count, 1)
                self.assertEqual(client_n.close_count, 1)
                self.assertEqual(len(client_n.post_calls), 12)
                self.assertEqual(len(client_n1.post_calls), 1)
                self.assertEqual(
                    set(client_n.get_calls),
                    {
                        (
                            "https://openrouter.ai/api/runtime-n/models",
                            "Bearer secret-n",
                        )
                    },
                )
                self.assertEqual(
                    set(client_n.post_calls),
                    {
                        (
                            "https://openrouter.ai/api/runtime-n/chat/completions",
                            "Bearer secret-n",
                        )
                    },
                )
                self.assertEqual(
                    client_n1.get_calls,
                    [
                        (
                            "https://openrouter.ai/api/runtime-n1/models",
                            "Bearer secret-n1",
                        )
                    ],
                )
                self.assertEqual(
                    set(client_n1.post_calls),
                    {
                        (
                            "https://openrouter.ai/api/runtime-n1/chat/completions",
                            "Bearer secret-n1",
                        )
                    },
                )
                status = await service.get_status()
                self.assertEqual(
                    status["snapshot"]["baseUrl"],
                    "https://openrouter.ai/api/runtime-n1",
                )
            finally:
                await service.stop()
                await manager.shutdown()

            self.assertEqual(client_n1.close_count, 1)
            self._assert_no_pending_tasks()

        run_async(scenario())

    def test_stop_cancels_blocked_runtime_run_and_releases_its_lease(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            runtime_client = _RuntimeClient(block_first_get=True)
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(api_key="secret"),
                    proxy_http_clients={"openrouter": runtime_client},
                )
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            await service.start_runtime(
                runtime_manager=manager,
                shared_http_client=shared_client,
            )
            await runtime_client.get_started.wait()
            self.assertEqual(manager.active_leases[1], 1)

            await service.stop()

            self.assertEqual(manager.active_leases.get(1, 0), 0)
            await manager.shutdown()
            self.assertEqual(runtime_client.close_count, 1)
            self._assert_no_pending_tasks()

        run_async(scenario())

    def test_stop_cancels_blocked_manual_runtime_run_and_releases_its_lease(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            runtime_client = _RuntimeClient()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(api_key="secret"),
                    proxy_http_clients={"openrouter": runtime_client},
                )
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            await service.start_runtime(
                runtime_manager=manager,
                shared_http_client=shared_client,
            )
            await _wait_until(
                lambda: runtime_client.get_count == 1
                and manager.active_leases.get(1, 0) == 0
            )

            runtime_client.block_get_number = 2
            runtime_client.get_started = asyncio.Event()
            self.assertTrue(await service.start_manual_full_refresh())
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await runtime_client.get_started.wait()
            self.assertEqual(manager.active_leases[1], 1)

            await service.stop()

            self.assertTrue(manual_task.done())
            self.assertTrue(manual_task.cancelled())
            self.assertEqual(manager.active_leases.get(1, 0), 0)
            await manager.shutdown()
            self.assertEqual(runtime_client.close_count, 1)
            self._assert_no_pending_tasks()

        run_async(scenario())

    def test_new_runs_observe_provider_addition_and_removal(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            added_client = _RuntimeClient()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(generation=1, config_loader=_loader())
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            try:
                await service.start_runtime(
                    runtime_manager=manager,
                    shared_http_client=shared_client,
                )
                await _wait_until(lambda: manager.active_leases.get(1, 0) == 0)

                publish_test_runtime_snapshot(manager,
                    make_runtime_snapshot(
                        generation=2,
                        config_loader=_loader(api_key="added-secret"),
                        proxy_http_clients={"openrouter": added_client},
                    ),
                    expected_generation=1,
                )
                await service.refresh_once()
                self.assertEqual(added_client.get_count, 1)
                self.assertTrue((await service.get_status())["configured"])

                await _wait_until(lambda: manager.cleanup_task_count == 0)
                publish_test_runtime_snapshot(manager,
                    make_runtime_snapshot(generation=3, config_loader=_loader()),
                    expected_generation=2,
                )
                await service.refresh_once()
                status = await service.get_status()
                self.assertFalse(status["configured"])
                self.assertIsNone(status["lastError"])
                with self.assertRaises(OpenRouterFreeModelsNotConfigured):
                    await service.start_manual_full_refresh()
                self.assertEqual(manager.active_leases.get(3, 0), 0)
            finally:
                await service.stop()
                await manager.shutdown()

            self.assertEqual(added_client.close_count, 1)
            self._assert_no_pending_tasks()

        run_async(scenario())

    def test_runtime_start_task_factory_failure_releases_preacquired_lease(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(api_key="SUPER_SECRET"),
                )
            )
            service = OpenRouterFreeModelsService()
            preview = service._acquire_runtime_run(
                runtime_manager=manager,
                shared_http_client=shared_client,
            )
            self.assertNotIn("SUPER_SECRET", repr(preview))
            self.assertNotIn("SUPER_SECRET", repr(preview.context))
            preview.lease.release()

            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            observed_coroutines: list[object] = []

            def failing_factory(_loop, coroutine, **_kwargs):
                observed_coroutines.append(coroutine)
                raise RuntimeError("task factory failed")

            loop.set_task_factory(failing_factory)
            try:
                with self.assertRaisesRegex(RuntimeError, "task factory failed"):
                    await service.start_runtime(
                        runtime_manager=manager,
                        shared_http_client=shared_client,
                    )
            finally:
                loop.set_task_factory(previous_factory)

            self.assertEqual(manager.active_leases.get(1, 0), 0)
            self.assertEqual(len(observed_coroutines), 1)
            self.assertEqual(
                inspect.getcoroutinestate(observed_coroutines[0]),
                inspect.CORO_CLOSED,
            )
            await service.stop()
            await manager.shutdown()
            self._assert_no_pending_tasks()

        run_async(scenario())

    def test_runtime_manual_task_factory_failure_releases_preacquired_lease(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(api_key="secret"),
                )
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            await service.start_runtime(
                runtime_manager=manager,
                shared_http_client=shared_client,
            )
            await _wait_until(lambda: manager.active_leases.get(1, 0) == 0)

            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            observed_coroutines: list[object] = []

            def failing_factory(_loop, coroutine, **_kwargs):
                observed_coroutines.append(coroutine)
                raise RuntimeError("manual task factory failed")

            loop.set_task_factory(failing_factory)
            try:
                with self.assertRaisesRegex(
                    RuntimeError,
                    "manual task factory failed",
                ):
                    await service.start_manual_full_refresh()
            finally:
                loop.set_task_factory(previous_factory)

            self.assertEqual(manager.active_leases.get(1, 0), 0)
            self.assertFalse(service._manual_refresh_running)
            self.assertIsNone(service._manual_refresh_task)
            self.assertEqual(len(observed_coroutines), 1)
            self.assertEqual(
                inspect.getcoroutinestate(observed_coroutines[0]),
                inspect.CORO_CLOSED,
            )
            await service.stop()
            await manager.shutdown()
            self._assert_no_pending_tasks()

        run_async(scenario())

    @unittest.skipUnless(
        hasattr(asyncio, "eager_task_factory"),
        "Python 3.12 eager task factory is required",
    )
    def test_runtime_manual_eager_task_releases_lease_after_committed_run(self):
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_client = _RuntimeClient()
            install_test_runtime_snapshot(manager,
                make_runtime_snapshot(
                    generation=1,
                    config_loader=_loader(api_key="secret"),
                )
            )
            service = OpenRouterFreeModelsService(refresh_interval_seconds=3600)
            await service.start_runtime(
                runtime_manager=manager,
                shared_http_client=shared_client,
            )
            await _wait_until(lambda: manager.active_leases.get(1, 0) == 0)

            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                self.assertTrue(await service.start_manual_full_refresh())
            finally:
                loop.set_task_factory(previous_factory)
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await manual_task

            self.assertEqual(manager.active_leases.get(1, 0), 0)
            self.assertFalse(service._manual_refresh_running)
            self.assertIsNone(service._manual_refresh_task)
            await service.stop()
            await manager.shutdown()
            self._assert_no_pending_tasks()

        run_async(scenario())

    def _assert_no_pending_tasks(self) -> None:
        current_task = asyncio.current_task()
        pending = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not current_task
            and not task.done()
            and task.get_name().startswith("openrouter-free-models")
        ]
        self.assertEqual(pending, [])
