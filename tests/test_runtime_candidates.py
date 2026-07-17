from __future__ import annotations

import asyncio
import unittest
from contextlib import ExitStack
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from llm_gateway_core.services import runtime_candidate
from llm_gateway_core.services.accounting import OperationCostCalculator
from llm_gateway_core.services.runtime_candidate import (
    RuntimeCandidate,
    build_runtime_candidate,
)
from llm_gateway_core.services.runtime_config import (
    RUNTIME_CLEANUP_MAX_ATTEMPTS,
    RuntimeGenerationManager,
    RuntimeManagerStatus,
    RuntimeSnapshot,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import install_test_runtime_snapshot


def _config_loader(**overrides: object) -> SimpleNamespace:
    values: dict[str, object] = {
        "providers_config": {},
        "fallback_rules": {},
        "_fallback_rules_base": {},
        "operation_rules": {},
        "fusion_rules": {},
        "model_rules": {},
        "router_rules": {},
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _snapshot(
    generation: int,
    *,
    config_loader: object | None = None,
    clients: dict[str, object] | None = None,
    operation_cost_calculators: dict[tuple[str, str], object] | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config_loader=config_loader or _config_loader(),
        operation_dispatcher=Mock(name=f"dispatcher-{generation}"),
        fusion_service=Mock(name=f"fusion-{generation}"),
        router_model_service=Mock(name=f"router-{generation}"),
        provider_models_service=Mock(name=f"models-{generation}"),
        proxy_http_clients=clients or {},
        cost_rate_registry={},
        operation_cost_calculator_registry=operation_cost_calculators or {},
    )


class RuntimeCandidateBuildTests(unittest.TestCase):
    def test_build_uses_exact_generation_inputs_and_one_frozen_cost_source(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            shared_http_client = Mock(name="shared-http-client")
            model_rules = {"gateway": {"mode": "chat"}}
            providers_config = {"provider": {"models": {}}}
            operation_rules = {"embeddings": {}}
            loader = _config_loader(
                providers_config=providers_config,
                operation_rules=operation_rules,
                model_rules=model_rules,
            )
            proxy_client = Mock(name="proxy-http-client")
            proxy_client.aclose = AsyncMock()
            source_rates = {
                ("provider", "model"): ModelCostRates(1.0, 2.0),
            }
            configured_calculator = OperationCostCalculator("operation", 0.25)
            source_calculators = {
                ("images_generation", "gateway/image"): configured_calculator,
            }

            async def populate(_providers, *, register_client):
                self.assertIs(_providers, providers_config)
                register_client("provider", proxy_client)
                return {"provider": proxy_client}

            with (
                patch.object(runtime_candidate, "populate_proxy_http_clients", populate),
                patch.object(
                    runtime_candidate,
                    "build_model_cost_rate_registry",
                    return_value=source_rates,
                ),
                patch.object(
                    runtime_candidate,
                    "build_operation_cost_calculator_registry",
                    return_value=source_calculators,
                ) as build_operation_calculators,
                patch.object(runtime_candidate, "OperationDispatcher") as dispatcher,
                patch.object(runtime_candidate, "FusionEnsembleService") as fusion,
                patch.object(runtime_candidate, "RouterModelService") as router,
                patch.object(runtime_candidate, "ProviderModelsService") as provider_models,
            ):
                candidate = await build_runtime_candidate(
                    manager=manager,
                    generation=1,
                    config_loader=loader,  # type: ignore[arg-type]
                    shared_http_client=shared_http_client,  # type: ignore[arg-type]
                )

            dispatcher.assert_called_once_with(
                providers_config,
                operation_rules,
                shared_http_client,
                model_rules=model_rules,
            )
            provider_models.assert_called_once_with()
            frozen_rates = fusion.call_args.kwargs["cost_rate_registry"]
            self.assertIs(router.call_args.kwargs["cost_rate_registry"], frozen_rates)
            fusion.assert_called_once_with(loader, cost_rate_registry=frozen_rates)
            router.assert_called_once_with(loader, cost_rate_registry=frozen_rates)
            self.assertEqual(candidate.snapshot.generation, 1)
            self.assertIs(candidate.snapshot.config_loader, loader)
            self.assertIs(candidate.snapshot.proxy_http_clients["provider"], proxy_client)
            self.assertEqual(dict(candidate.snapshot.cost_rate_registry), source_rates)
            build_operation_calculators.assert_called_once_with(operation_rules)
            self.assertEqual(
                dict(candidate.snapshot.operation_cost_calculator_registry),
                source_calculators,
            )

            source_rates[("late", "model")] = ModelCostRates(9.0, 9.0)
            source_calculators[("images_edit", "gateway/late")] = (
                OperationCostCalculator("operation", 0.5)
            )
            self.assertNotIn(("late", "model"), frozen_rates)
            self.assertNotIn(("late", "model"), candidate.snapshot.cost_rate_registry)
            self.assertNotIn(
                ("images_edit", "gateway/late"),
                candidate.snapshot.operation_cost_calculator_registry,
            )
            with self.assertRaises(TypeError):
                frozen_rates[("blocked", "model")] = ModelCostRates(1.0, 1.0)
            with self.assertRaises(TypeError):
                candidate.snapshot.operation_cost_calculator_registry[
                    ("images_edit", "gateway/blocked")
                ] = OperationCostCalculator("operation", 1.0)
            with self.assertRaises(FrozenInstanceError):
                candidate.snapshot = _snapshot(2)  # type: ignore[misc]

            self.assertTrue(await candidate.close_unpublished())
            proxy_client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_partial_factory_failures_preserve_exact_base_exception_identity(self) -> None:
        async def run_failure(primary: BaseException) -> None:
            manager = RuntimeGenerationManager()
            client = Mock()
            client.aclose = AsyncMock()

            async def fail_after_registration(_providers, *, register_client):
                register_client("partial", client)
                raise primary

            with patch.object(
                runtime_candidate,
                "populate_proxy_http_clients",
                fail_after_registration,
            ):
                with self.assertRaises(type(primary)) as raised:
                    await build_runtime_candidate(
                        manager=manager,
                        generation=1,
                        config_loader=_config_loader(),  # type: ignore[arg-type]
                        shared_http_client=Mock(),  # type: ignore[arg-type]
                    )

            self.assertIs(raised.exception, primary)
            client.aclose.assert_awaited_once()
            self.assertEqual(manager.pending_unpublished_generations, ())
            await manager.shutdown()

        async def scenario() -> None:
            for primary in (
                RuntimeError("ordinary secret"),
                asyncio.CancelledError("cancel secret"),
                KeyboardInterrupt("keyboard secret"),
                SystemExit("exit secret"),
            ):
                with self.subTest(exception_type=type(primary).__name__):
                    await run_failure(primary)

        run_async(scenario())

    def test_pricing_and_service_constructor_failures_close_registered_clients(self) -> None:
        async def run_failure(stage: str) -> None:
            manager = RuntimeGenerationManager()
            client = Mock(name=f"{stage}-client")
            client.aclose = AsyncMock()
            primary = RuntimeError(f"unsafe {stage} failure")

            async def populate(_providers, *, register_client):
                register_client("partial", client)
                return {"partial": client}

            patch_targets = {
                "pricing": "build_model_cost_rate_registry",
                "operation-pricing": "build_operation_cost_calculator_registry",
                "dispatcher": "OperationDispatcher",
                "fusion": "FusionEnsembleService",
                "router": "RouterModelService",
                "provider-models": "ProviderModelsService",
                "snapshot": "RuntimeSnapshot",
                "candidate": "RuntimeCandidate",
            }
            with ExitStack() as stack:
                stack.enter_context(
                    patch.object(runtime_candidate, "populate_proxy_http_clients", populate)
                )
                for target in patch_targets.values():
                    replacement = stack.enter_context(patch.object(runtime_candidate, target))
                    if target == patch_targets[stage]:
                        replacement.side_effect = primary

                with self.assertRaises(RuntimeError) as raised:
                    await build_runtime_candidate(
                        manager=manager,
                        generation=1,
                        config_loader=_config_loader(),  # type: ignore[arg-type]
                        shared_http_client=Mock(),  # type: ignore[arg-type]
                    )

            self.assertIs(raised.exception, primary)
            client.aclose.assert_awaited_once()
            self.assertEqual(manager.pending_unpublished_generations, ())
            await manager.shutdown()

        async def scenario() -> None:
            for stage in (
                "pricing",
                "operation-pricing",
                "dispatcher",
                "fusion",
                "router",
                "provider-models",
                "snapshot",
                "candidate",
            ):
                with self.subTest(stage=stage):
                    await run_failure(stage)

        run_async(scenario())

    def test_persistent_close_failure_remains_manager_owned_until_shutdown_retry(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            client = Mock()
            client.aclose = AsyncMock(side_effect=RuntimeError("cleanup secret"))
            primary = RuntimeError("primary secret")

            async def fail_after_registration(_providers, *, register_client):
                register_client("partial", client)
                raise primary

            with patch.object(
                runtime_candidate,
                "populate_proxy_http_clients",
                fail_after_registration,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    await build_runtime_candidate(
                        manager=manager,
                        generation=1,
                        config_loader=_config_loader(),  # type: ignore[arg-type]
                        shared_http_client=Mock(),  # type: ignore[arg-type]
                    )

            self.assertIs(raised.exception, primary)
            self.assertEqual(client.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS)
            self.assertEqual(manager.pending_unpublished_generations, (1,))
            self.assertNotIn("cleanup secret", repr(manager.cleanup_failures))

            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOP_FAILED)
            self.assertEqual(manager.pending_unpublished_generations, (1,))

            client.aclose.side_effect = None
            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.pending_unpublished_generations, ())

        run_async(scenario())

    def test_cleanup_diagnostic_and_candidate_repr_do_not_include_raw_secrets(self) -> None:
        async def scenario() -> None:
            primary = RuntimeError("primary raw secret")
            unsafe_cleanup_type = type(
                "Unsafe\ncleanup-class-secret",
                (RuntimeError,),
                {},
            )
            slot = Mock()
            slot.register_http_client = Mock()
            slot.close_unpublished = AsyncMock(
                side_effect=unsafe_cleanup_type("cleanup raw secret")
            )
            manager = Mock()
            manager.open_unpublished_slot.return_value = slot

            async def fail(_providers, *, register_client):
                register_client("partial", Mock())
                raise primary

            with (
                patch.object(runtime_candidate, "populate_proxy_http_clients", fail),
                self.assertLogs(runtime_candidate.logger, level="ERROR") as captured,
            ):
                with self.assertRaises(RuntimeError) as raised:
                    await build_runtime_candidate(
                        manager=manager,
                        generation=1,
                        config_loader=_config_loader(),  # type: ignore[arg-type]
                        shared_http_client=Mock(),  # type: ignore[arg-type]
                    )

            self.assertIs(raised.exception, primary)
            diagnostics = "\n".join(captured.output)
            self.assertIn("unpublished_slot", diagnostics)
            self.assertIn("BaseException", diagnostics)
            self.assertNotIn("primary raw secret", diagnostics)
            self.assertNotIn("cleanup raw secret", diagnostics)
            self.assertNotIn("cleanup-class-secret", diagnostics)

            secret_loader = _config_loader(providers_config={"api_key": "raw-api-secret"})
            candidate = RuntimeCandidate(
                snapshot=_snapshot(1, config_loader=secret_loader),
                _manager=Mock(),  # type: ignore[arg-type]
                _slot=Mock(),  # type: ignore[arg-type]
            )
            self.assertEqual(repr(candidate), "RuntimeCandidate(generation=1)")
            self.assertNotIn("raw-api-secret", repr(candidate))

        run_async(scenario())


class RuntimeCandidatePublicationTests(unittest.TestCase):
    def test_install_and_publish_transfer_slots_and_close_becomes_noop(self) -> None:
        async def scenario() -> None:
            initial_manager = RuntimeGenerationManager()
            initial_client = Mock()
            initial_client.aclose = AsyncMock()
            initial_slot = initial_manager.open_unpublished_slot(1)
            initial_slot.register_http_client("initial", initial_client)
            initial_snapshot = _snapshot(1, clients={"initial": initial_client})
            initial_candidate = RuntimeCandidate(
                snapshot=initial_snapshot,
                _manager=initial_manager,
                _slot=initial_slot,
            )

            self.assertIs(initial_candidate.install_initial(), initial_snapshot)
            self.assertTrue(await initial_candidate.close_unpublished())
            initial_client.aclose.assert_not_awaited()
            await initial_manager.shutdown()
            initial_client.aclose.assert_awaited_once()

            publish_manager = RuntimeGenerationManager()
            first_calculator = OperationCostCalculator("operation", 0.1)
            second_calculator = OperationCostCalculator("operation", 0.2)
            initial_published_snapshot = _snapshot(
                1,
                operation_cost_calculators={
                    ("images_generation", "gateway/image"): first_calculator,
                },
            )
            install_test_runtime_snapshot(publish_manager, initial_published_snapshot)
            initial_lease = publish_manager.acquire_current()
            published_client = Mock()
            published_client.aclose = AsyncMock()
            publish_slot = publish_manager.open_unpublished_slot(2)
            publish_slot.register_http_client("published", published_client)
            published_snapshot = _snapshot(
                2,
                clients={"published": published_client},
                operation_cost_calculators={
                    ("images_generation", "gateway/image"): second_calculator,
                },
            )
            published_candidate = RuntimeCandidate(
                snapshot=published_snapshot,
                _manager=publish_manager,
                _slot=publish_slot,
            )

            self.assertIs(
                published_candidate.publish(expected_generation=1),
                published_snapshot,
            )
            current_lease = publish_manager.acquire_current()
            self.assertIs(
                initial_lease.snapshot.operation_cost_calculator_registry[
                    ("images_generation", "gateway/image")
                ],
                first_calculator,
            )
            self.assertIs(
                current_lease.snapshot.operation_cost_calculator_registry[
                    ("images_generation", "gateway/image")
                ],
                second_calculator,
            )
            initial_lease.release()
            current_lease.release()
            self.assertTrue(await published_candidate.close_unpublished())
            published_client.aclose.assert_not_awaited()
            await publish_manager.shutdown()
            published_client.aclose.assert_awaited_once()

        run_async(scenario())
