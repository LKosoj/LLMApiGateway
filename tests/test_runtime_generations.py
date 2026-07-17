from __future__ import annotations

import asyncio
import unittest
from collections.abc import Callable
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from llm_gateway_core.services.runtime_config import (
    RUNTIME_CLEANUP_DIAGNOSTIC_LIMIT,
    RUNTIME_CLEANUP_MAX_ATTEMPTS,
    RUNTIME_GENERATION_HISTORY_LIMIT,
    RuntimeGenerationConflictError,
    RuntimeGenerationManager,
    RuntimeGenerationStatus,
    RuntimeManagerStateError,
    RuntimeManagerStatus,
    RuntimeSnapshot,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    publish_test_runtime_snapshot,
)


def _snapshot(
    generation: int,
    *,
    clients: dict[str, object] | None = None,
    costs: dict[object, object] | None = None,
    config_loader: object | None = None,
) -> RuntimeSnapshot:
    return RuntimeSnapshot(
        generation=generation,
        config_loader=config_loader if config_loader is not None else _config_loader(),
        operation_dispatcher=Mock(name=f"dispatcher-{generation}"),
        fusion_service=Mock(name=f"fusion-{generation}"),
        router_model_service=Mock(name=f"router-{generation}"),
        provider_models_service=Mock(name=f"models-{generation}"),
        proxy_http_clients=clients or {},
        cost_rate_registry=costs or {},
    )


def _config_loader(**overrides: object) -> SimpleNamespace:
    values = {
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


class _PydanticLikeMutableNode:
    model_fields = {"payload": object()}

    def __init__(self, payload: object) -> None:
        self.payload = payload


async def _wait_for_status(
    manager: RuntimeGenerationManager,
    generation: int,
    expected: RuntimeGenerationStatus,
) -> None:
    for _attempt in range(20):
        if manager.generation_statuses.get(generation) is expected:
            return
        await asyncio.sleep(0)
    raise AssertionError(
        f"Generation {generation} did not reach {expected.value}: "
        f"{manager.generation_statuses.get(generation)}"
    )


def _rejection_signature(call: Callable[[], object]) -> tuple[type[Exception], str]:
    try:
        call()
    except (RuntimeGenerationConflictError, RuntimeManagerStateError) as exc:
        return type(exc), str(exc)
    raise AssertionError("Runtime publication unexpectedly succeeded.")


def _manager_publish_state(manager: RuntimeGenerationManager) -> tuple[object, ...]:
    return (
        manager.status,
        manager.current_generation,
        tuple(manager.generation_statuses.items()),
        tuple(manager.active_leases.items()),
        manager.retired_generations,
        manager.failed_generations,
        manager.pending_unpublished_generations,
        manager.failed_unpublished_generations,
        manager.cleanup_task_count,
        tuple(
            (generation, id(record), id(record.snapshot))
            for generation, record in manager._generations.items()  # noqa: SLF001
        ),
    )


def _candidate_state(candidate: RuntimeSnapshot) -> tuple[object, ...]:
    return (
        candidate.generation,
        id(candidate.config_loader),
        id(candidate.operation_dispatcher),
        id(candidate.fusion_service),
        id(candidate.router_model_service),
        id(candidate.provider_models_service),
        tuple(
            (provider, id(client))
            for provider, client in candidate.proxy_http_clients.items()
        ),
        tuple(
            (model, id(rates))
            for model, rates in candidate.cost_rate_registry.items()
        ),
    )


class RuntimeSnapshotTests(unittest.TestCase):
    def test_process_lifetime_services_are_not_part_of_a_generation(self) -> None:
        self.assertNotIn("services", RuntimeSnapshot.__annotations__)

    def test_snapshot_copies_and_freezes_replaceable_mappings(self) -> None:
        clients: dict[str, object] = {"provider": Mock()}
        costs = {("provider", "model"): ModelCostRates(1.0, 2.0)}

        snapshot = _snapshot(1, clients=clients, costs=costs)
        clients["later"] = Mock()
        costs["later"] = Mock()

        self.assertEqual(set(snapshot.proxy_http_clients), {"provider"})
        self.assertEqual(set(snapshot.cost_rate_registry), {("provider", "model")})
        with self.assertRaises(TypeError):
            snapshot.proxy_http_clients["blocked"] = Mock()  # type: ignore[index]

    def test_generation_must_be_positive(self) -> None:
        with self.assertRaisesRegex(ValueError, "positive"):
            _snapshot(0)


class RuntimeGenerationManagerTests(unittest.TestCase):
    def _assert_publish_rejection_parity(
        self,
        manager: RuntimeGenerationManager,
        candidate: RuntimeSnapshot,
        *,
        expected_generation: int,
        caller_owned_clients: tuple[Mock, ...] = (),
    ) -> None:
        manager_before = _manager_publish_state(manager)
        candidate_before = _candidate_state(candidate)

        preflight = _rejection_signature(
            lambda: manager.validate_publish(
                candidate,
                expected_generation=expected_generation,
            )
        )
        self.assertEqual(_manager_publish_state(manager), manager_before)
        self.assertEqual(_candidate_state(candidate), candidate_before)

        commit = _rejection_signature(
            lambda: publish_test_runtime_snapshot(manager,
                candidate,
                expected_generation=expected_generation,
            )
        )
        self.assertEqual(commit, preflight)
        self.assertEqual(_manager_publish_state(manager), manager_before)
        self.assertEqual(_candidate_state(candidate), candidate_before)
        for client in caller_owned_clients:
            client.aclose.assert_not_awaited()

    def test_install_acquire_and_idempotent_release(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            snapshot = _snapshot(1)
            install_test_runtime_snapshot(manager, snapshot)

            lease = manager.acquire_current()
            self.assertIs(lease.snapshot, snapshot)
            self.assertEqual(manager.active_leases[1], 1)
            lease.release()
            lease.release()

            self.assertTrue(lease.released)
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_initial_install_requires_new_manager_and_generation_one(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            with self.assertRaises(RuntimeGenerationConflictError):
                install_test_runtime_snapshot(manager, _snapshot(2))
            install_test_runtime_snapshot(manager, _snapshot(1))
            with self.assertRaises(RuntimeManagerStateError):
                install_test_runtime_snapshot(manager, _snapshot(1))
            await manager.shutdown()

        run_async(scenario())

    def test_publish_without_leases_closes_old_generation_once(self) -> None:
        async def scenario() -> None:
            old_client = Mock()
            old_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"old": old_client}))

            candidate = _snapshot(2)
            self.assertIs(
                publish_test_runtime_snapshot(manager, candidate, expected_generation=1),
                candidate,
            )
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)

            old_client.aclose.assert_awaited_once()
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSED,
            )
            self.assertEqual(manager.current_generation, 2)
            await manager.shutdown()

        run_async(scenario())

    def test_retired_generation_waits_for_every_request_and_background_lease(self) -> None:
        async def scenario() -> None:
            old_client = Mock()
            old_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"old": old_client}))
            request_lease = manager.acquire_current()
            background_lease = manager.retain(1)

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await asyncio.sleep(0)
            old_client.aclose.assert_not_awaited()

            request_lease.release()
            await asyncio.sleep(0)
            old_client.aclose.assert_not_awaited()

            background_lease.release()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            old_client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_retain_rejects_unknown_or_already_closing_generation(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            with self.assertRaises(RuntimeManagerStateError):
                manager.retain(99)

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            with self.assertRaises(RuntimeManagerStateError):
                manager.retain(1)
            await manager.shutdown()

        run_async(scenario())

    def test_only_a_living_lease_can_retain_a_retired_generation(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            source = manager.acquire_current()

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            with self.assertRaises(RuntimeManagerStateError):
                manager.retain(1)

            retained = source.retain()
            self.assertEqual(manager.active_leases[1], 2)
            source.release()
            retained.release()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)

            with self.assertRaises(RuntimeManagerStateError):
                source.retain()
            await manager.shutdown()

        run_async(scenario())

    def test_living_lease_cannot_spawn_child_after_shutdown_starts(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            lease = manager.acquire_current()

            shutdown_task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0)
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPING)
            with self.assertRaises(RuntimeManagerStateError):
                lease.retain()

            lease.release()
            await shutdown_task
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)

        run_async(scenario())

    def test_publish_rejects_shared_generation_owned_dependencies(self) -> None:
        async def scenario() -> None:
            shared_client = Mock()
            shared_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            current = _snapshot(1, clients={"old": shared_client})
            install_test_runtime_snapshot(manager, current)

            generation_owned_fields = (
                "config_loader",
                "operation_dispatcher",
                "fusion_service",
                "router_model_service",
                "provider_models_service",
            )
            for field_name in generation_owned_fields:
                with self.subTest(field=field_name):
                    candidate = replace(
                        _snapshot(2),
                        **{field_name: getattr(current, field_name)},
                    )
                    with self.assertRaises(RuntimeGenerationConflictError):
                        publish_test_runtime_snapshot(manager, candidate, expected_generation=1)
                    self.assertEqual(manager.current_generation, 1)

            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager,
                    _snapshot(2, clients={"renamed": shared_client}),
                    expected_generation=1,
                )

            candidate = _snapshot(2)
            publish_test_runtime_snapshot(manager, candidate, expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()

        run_async(scenario())

    def test_validate_publish_is_non_mutating_and_keeps_candidate_caller_owned(self) -> None:
        async def scenario() -> None:
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            candidate = _snapshot(2, clients={"candidate": candidate_client})

            manager.validate_publish(candidate, expected_generation=1)

            self.assertEqual(manager.current_generation, 1)
            self.assertEqual(set(manager._generations), {1})  # noqa: SLF001
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.ACTIVE,
            )
            self.assertEqual(manager.cleanup_task_count, 0)
            candidate_client.aclose.assert_not_awaited()

            publish_test_runtime_snapshot(manager, candidate, expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()
            candidate_client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_validate_publish_and_publish_reject_with_identical_validation(self) -> None:
        async def scenario() -> None:
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            candidate = _snapshot(3, clients={"candidate": candidate_client})

            with self.assertRaises(RuntimeGenerationConflictError) as preflight:
                manager.validate_publish(candidate, expected_generation=1)
            with self.assertRaises(RuntimeGenerationConflictError) as commit:
                publish_test_runtime_snapshot(manager, candidate, expected_generation=1)

            self.assertEqual(str(commit.exception), str(preflight.exception))
            self.assertEqual(manager.current_generation, 1)
            self.assertEqual(set(manager._generations), {1})  # noqa: SLF001
            candidate_client.aclose.assert_not_awaited()
            await manager.shutdown()

        run_async(scenario())

    def test_validate_publish_matches_publish_while_cleanup_is_busy(self) -> None:
        async def scenario() -> None:
            close_started = asyncio.Event()
            allow_close = asyncio.Event()

            async def close_old_client() -> None:
                close_started.set()
                await allow_close.wait()

            old_client = Mock()
            old_client.aclose = AsyncMock(side_effect=close_old_client)
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"old": old_client}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await close_started.wait()

            candidate = _snapshot(3, clients={"candidate": candidate_client})
            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=2,
                caller_owned_clients=(candidate_client,),
            )

            allow_close.set()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()
            candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_validate_publish_matches_publish_with_unresolved_cleanup(self) -> None:
        async def scenario() -> None:
            unresolved = Mock()
            unresolved.aclose = AsyncMock(side_effect=RuntimeError("unsafe detail"))
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"unresolved": unresolved}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            candidate = _snapshot(3, clients={"candidate": candidate_client})
            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=2,
                caller_owned_clients=(candidate_client,),
            )

            await manager.shutdown()
            candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_validate_publish_matches_publish_with_unresolved_unpublished_slot(self) -> None:
        async def scenario() -> None:
            unresolved = Mock()
            unresolved.aclose = AsyncMock(side_effect=RuntimeError("unsafe detail"))
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("unresolved", unresolved)
            self.assertFalse(await slot.close_unpublished())

            candidate = _snapshot(2, clients={"candidate": candidate_client})
            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=1,
                caller_owned_clients=(candidate_client,),
            )

            unresolved.aclose.side_effect = None
            self.assertTrue(await slot.close_unpublished())
            publish_test_runtime_snapshot(manager, candidate, expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()
            candidate_client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_validate_publish_matches_publish_after_manager_stops(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            await manager.shutdown()
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            candidate = _snapshot(2, clients={"candidate": candidate_client})

            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=1,
                caller_owned_clients=(candidate_client,),
            )

        run_async(scenario())

    def test_validate_publish_matches_publish_on_foreign_loop(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            candidate = _snapshot(2, clients={"candidate": candidate_client})
            manager_before = _manager_publish_state(manager)
            candidate_before = _candidate_state(candidate)

            async def invoke(action: Callable[[], object]) -> None:
                action()

            def reject_on_foreign_loop(
                action: Callable[[], object],
            ) -> tuple[type[Exception], str]:
                return _rejection_signature(lambda: run_async(invoke(action)))

            preflight = await asyncio.to_thread(
                reject_on_foreign_loop,
                lambda: manager.validate_publish(
                    candidate,
                    expected_generation=1,
                ),
            )
            commit = await asyncio.to_thread(
                reject_on_foreign_loop,
                lambda: publish_test_runtime_snapshot(
                    manager,
                    candidate,
                    expected_generation=1,
                ),
            )

            self.assertEqual(commit, preflight)
            self.assertEqual(_manager_publish_state(manager), manager_before)
            self.assertEqual(_candidate_state(candidate), candidate_before)
            candidate_client.aclose.assert_not_awaited()
            await manager.shutdown()
            candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_validate_publish_matches_publish_for_shared_service(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            candidate = replace(
                _snapshot(2, clients={"candidate": candidate_client}),
                fusion_service=current.fusion_service,
            )

            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=1,
                caller_owned_clients=(candidate_client,),
            )
            await manager.shutdown()
            candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_validate_publish_matches_publish_for_shared_proxy_client(self) -> None:
        async def scenario() -> None:
            shared_client = Mock()
            shared_client.aclose = AsyncMock()
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"current": shared_client}))
            candidate = _snapshot(
                2,
                clients={"renamed": shared_client, "candidate": candidate_client},
            )

            self._assert_publish_rejection_parity(
                manager,
                candidate,
                expected_generation=1,
                caller_owned_clients=(shared_client, candidate_client),
            )
            await manager.shutdown()
            shared_client.aclose.assert_awaited_once()
            candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_publish_revalidates_stale_generation_after_successful_preflight(self) -> None:
        async def scenario() -> None:
            stale_client = Mock()
            stale_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            stale_candidate = _snapshot(2, clients={"stale": stale_client})

            manager.validate_publish(stale_candidate, expected_generation=1)
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)

            with self.assertRaisesRegex(
                RuntimeGenerationConflictError,
                "changed while the candidate was being built",
            ):
                publish_test_runtime_snapshot(manager, stale_candidate, expected_generation=1)

            self.assertEqual(manager.current_generation, 2)
            stale_client.aclose.assert_not_awaited()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_rejects_nested_mutable_aliases_in_every_config_graph(self) -> None:
        async def scenario() -> None:
            config_attributes = (
                "providers_config",
                "fallback_rules",
                "_fallback_rules_base",
                "operation_rules",
                "fusion_rules",
                "model_rules",
                "router_rules",
            )
            for current_attribute in config_attributes:
                for candidate_attribute in config_attributes:
                    with self.subTest(
                        current=current_attribute,
                        candidate=candidate_attribute,
                    ):
                        shared: list[str] = []
                        current_loader = _config_loader(
                            **{current_attribute: {"current": [{"shared": shared}]}}
                        )
                        candidate_loader = _config_loader(
                            **{candidate_attribute: {"candidate": (shared,)}}
                        )
                        manager = RuntimeGenerationManager()
                        install_test_runtime_snapshot(manager, _snapshot(1, config_loader=current_loader))

                        with self.assertRaises(RuntimeGenerationConflictError):
                            publish_test_runtime_snapshot(manager,
                                _snapshot(2, config_loader=candidate_loader),
                                expected_generation=1,
                            )
                        await manager.shutdown()

        run_async(scenario())

    def test_publish_rejects_nested_fallback_rules_base_alias(self) -> None:
        async def scenario() -> None:
            shared: list[str] = []
            current_loader = _config_loader(
                _fallback_rules_base={"current": {"nested": shared}}
            )
            candidate_loader = _config_loader(
                _fallback_rules_base={"candidate": (shared,)}
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, config_loader=current_loader))
            candidate = _snapshot(2, config_loader=candidate_loader)

            with self.assertRaises(RuntimeGenerationConflictError):
                manager.validate_publish(candidate, expected_generation=1)
            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager, candidate, expected_generation=1)

            self.assertEqual(manager.current_generation, 1)
            self.assertEqual(set(manager._generations), {1})  # noqa: SLF001
            await manager.shutdown()

        run_async(scenario())

    def test_publish_rejects_supported_mutable_graph_node_aliases(self) -> None:
        async def scenario() -> None:
            mutable_nodes = (
                {"nested": "mapping"},
                ["list"],
                {"set"},
                bytearray(b"mutable"),
                _PydanticLikeMutableNode(["payload"]),
            )
            for shared_node in mutable_nodes:
                with self.subTest(node_type=type(shared_node).__name__):
                    current_loader = _config_loader(
                        providers_config={"current": {"shared": shared_node}}
                    )
                    candidate_loader = _config_loader(
                        providers_config={"candidate": [shared_node]}
                    )
                    manager = RuntimeGenerationManager()
                    install_test_runtime_snapshot(manager, _snapshot(1, config_loader=current_loader))

                    with self.assertRaises(RuntimeGenerationConflictError):
                        publish_test_runtime_snapshot(manager,
                            _snapshot(2, config_loader=candidate_loader),
                            expected_generation=1,
                        )
                    await manager.shutdown()

            shared_payload: list[str] = []
            current_loader = _config_loader(
                providers_config={
                    "node": _PydanticLikeMutableNode(shared_payload),
                }
            )
            candidate_loader = _config_loader(
                providers_config={
                    "node": _PydanticLikeMutableNode(shared_payload),
                }
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, config_loader=current_loader))
            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager,
                    _snapshot(2, config_loader=candidate_loader),
                    expected_generation=1,
                )
            await manager.shutdown()

        run_async(scenario())

    def test_publish_allows_shared_immutable_values_and_frozen_cost_rates(self) -> None:
        async def scenario() -> None:
            shared_rates = ModelCostRates(input_rate=1.0, output_rate=2.0)
            shared_tuple = ("immutable", 3)
            current_loader = _config_loader(
                providers_config={
                    "rates": shared_rates,
                    "tuple": shared_tuple,
                    "scalar": "same",
                }
            )
            candidate_loader = _config_loader(
                fallback_rules={
                    "rates": shared_rates,
                    "tuple": shared_tuple,
                    "scalar": "same",
                }
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, config_loader=current_loader))

            publish_test_runtime_snapshot(manager,
                _snapshot(2, config_loader=candidate_loader),
                expected_generation=1,
            )
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()

        run_async(scenario())

    def test_stale_and_nonsequential_candidates_remain_caller_owned(self) -> None:
        async def scenario() -> None:
            stale_client = Mock()
            stale_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)

            stale = _snapshot(3, clients={"stale": stale_client})
            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager, stale, expected_generation=1)
            stale_client.aclose.assert_not_awaited()

            gap = _snapshot(4, clients={"gap": stale_client})
            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager, gap, expected_generation=2)
            stale_client.aclose.assert_not_awaited()
            self.assertEqual(manager.current_generation, 2)
            await manager.shutdown()

        run_async(scenario())

    def test_publish_burst_keeps_only_one_retired_generation_and_candidates_owned(
        self,
    ) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            long_lease = manager.acquire_current()
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)

            stale_client = Mock()
            stale_client.aclose = AsyncMock()
            with self.assertRaises(RuntimeGenerationConflictError):
                publish_test_runtime_snapshot(manager,
                    _snapshot(3, clients={"stale": stale_client}),
                    expected_generation=1,
                )
            stale_client.aclose.assert_not_awaited()

            rejected_candidates: list[tuple[RuntimeSnapshot, Mock]] = []
            for index in range(100):
                candidate_client = Mock()
                candidate_client.aclose = AsyncMock()
                candidate = _snapshot(
                    3,
                    clients={f"candidate-{index}": candidate_client},
                )
                with self.assertRaises(RuntimeManagerStateError):
                    publish_test_runtime_snapshot(manager, candidate, expected_generation=2)
                rejected_candidates.append((candidate, candidate_client))

            self.assertEqual(set(manager._generations), {1, 2})  # noqa: SLF001
            self.assertEqual(manager.cleanup_task_count, 0)
            self.assertEqual(manager.current_generation, 2)
            for _candidate, candidate_client in rejected_candidates:
                candidate_client.aclose.assert_not_awaited()

            long_lease.release()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            accepted_candidate, accepted_client = rejected_candidates[-1]
            publish_test_runtime_snapshot(manager, accepted_candidate, expected_generation=2)
            await _wait_for_status(manager, 2, RuntimeGenerationStatus.CLOSED)
            self.assertEqual(set(manager._generations), {3})  # noqa: SLF001

            await manager.shutdown()
            accepted_client.aclose.assert_awaited_once()
            for _candidate, candidate_client in rejected_candidates[:-1]:
                candidate_client.aclose.assert_not_awaited()

        run_async(scenario())

    def test_publish_waits_for_in_progress_cleanup_without_mutating_candidate(self) -> None:
        async def scenario() -> None:
            close_started = asyncio.Event()
            allow_close = asyncio.Event()

            async def close_old_client() -> None:
                close_started.set()
                await allow_close.wait()

            old_client = Mock()
            old_client.aclose = AsyncMock(side_effect=close_old_client)
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            candidate = _snapshot(3, clients={"candidate": candidate_client})
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"old": old_client}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await close_started.wait()

            with self.assertRaises(RuntimeManagerStateError):
                publish_test_runtime_snapshot(manager, candidate, expected_generation=2)
            self.assertEqual(manager.current_generation, 2)
            self.assertEqual(set(manager._generations), {1, 2})  # noqa: SLF001
            self.assertEqual(manager.cleanup_task_count, 1)
            candidate_client.aclose.assert_not_awaited()

            allow_close.set()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)
            publish_test_runtime_snapshot(manager, candidate, expected_generation=2)
            await _wait_for_status(manager, 2, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()
            candidate_client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_cleanup_failure_is_safe_and_does_not_skip_other_clients(self) -> None:
        async def scenario() -> None:
            failing = Mock()
            failing.aclose = AsyncMock(side_effect=RuntimeError("secret proxy URL"))
            healthy = Mock()
            healthy.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager,
                _snapshot(1, clients={"failing-provider": failing, "healthy": healthy})
            )

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            healthy.aclose.assert_awaited_once()
            self.assertEqual(failing.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS)
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSE_FAILED,
            )
            self.assertEqual(len(manager.cleanup_failures), 1)
            failure = manager.cleanup_failures[0]
            self.assertEqual(failure.resource, "failing-provider")
            self.assertEqual(failure.exception_type, "RuntimeError")
            self.assertNotIn("secret", repr(failure))
            await manager.shutdown()

        run_async(scenario())

    def test_cleanup_failure_sanitizes_dynamic_exception_class_name(self) -> None:
        async def scenario() -> None:
            unsafe_type = type("Unsafe\nruntime-secret", (RuntimeError,), {})
            failing = Mock()
            failing.aclose = AsyncMock(side_effect=unsafe_type())
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"failing": failing}))

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            self.assertEqual(manager.cleanup_failures[0].exception_type, "BaseException")
            self.assertNotIn("runtime-secret", repr(manager.cleanup_failures))
            await manager.shutdown()

        run_async(scenario())

    def test_transient_cleanup_failure_is_retried_then_closed(self) -> None:
        async def scenario() -> None:
            transient = Mock()
            transient.aclose = AsyncMock(
                side_effect=[RuntimeError("secret first failure"), None]
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"transient": transient}))

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)

            self.assertEqual(transient.aclose.await_count, 2)
            self.assertEqual(len(manager.cleanup_failures), 1)
            await manager.shutdown()

        run_async(scenario())

    def test_cleanup_cancellation_is_retried_and_never_reported_as_closed(self) -> None:
        async def scenario() -> None:
            transient = Mock()
            transient.aclose = AsyncMock(
                side_effect=[asyncio.CancelledError("secret cancellation"), None]
            )
            persistent = Mock()
            persistent.aclose = AsyncMock(
                side_effect=asyncio.CancelledError("persistent secret")
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager,
                _snapshot(
                    1,
                    clients={"transient": transient, "persistent": persistent},
                )
            )

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            self.assertEqual(transient.aclose.await_count, 2)
            self.assertEqual(persistent.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS)
            self.assertEqual(
                set(manager._failed_generations[1].clients),  # noqa: SLF001
                {"persistent"},
            )
            await manager.shutdown()

        run_async(scenario())

    def test_failed_cleanup_blocks_publish_until_explicit_recovery(self) -> None:
        async def scenario() -> None:
            failing_then_healthy = Mock()
            failing_then_healthy.aclose = AsyncMock(
                side_effect=[
                    RuntimeError("secret one"),
                    RuntimeError("secret two"),
                    RuntimeError("secret three"),
                    None,
                ]
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager,
                _snapshot(1, clients={"recoverable": failing_then_healthy})
            )
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            lease = manager.acquire_current()
            lease.release()
            with self.assertRaisesRegex(RuntimeManagerStateError, "cleanup"):
                publish_test_runtime_snapshot(manager, _snapshot(3), expected_generation=2)

            self.assertTrue(await manager.retry_failed_cleanup())
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSED,
            )
            self.assertEqual(manager.failed_generations, ())
            publish_test_runtime_snapshot(manager, _snapshot(3), expected_generation=2)
            await _wait_for_status(manager, 2, RuntimeGenerationStatus.CLOSED)
            await manager.shutdown()

        run_async(scenario())

    def test_unresolved_cleanup_generations_unions_both_halves(self) -> None:
        async def scenario() -> None:
            persistent = Mock()
            persistent.aclose = AsyncMock(side_effect=RuntimeError("secret retired"))
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"persistent": persistent}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            failing_slot_client = Mock()
            failing_slot_client.aclose = AsyncMock(side_effect=RuntimeError("secret slot"))
            slot = manager.open_unpublished_slot(3)
            slot.register_http_client("proxy", failing_slot_client)
            self.assertFalse(await slot.close_unpublished())

            self.assertEqual(manager.failed_generations, (1,))
            self.assertEqual(manager.failed_unpublished_generations, (3,))
            self.assertEqual(set(manager.unresolved_cleanup_generations), {1, 3})
            self.assertEqual(
                manager.unresolved_cleanup_generations,
                manager.failed_unpublished_generations + manager.failed_generations,
            )

            await manager.shutdown()

        run_async(scenario())

    def test_unresolved_clients_survive_history_eviction_and_diagnostics_are_bounded(
        self,
    ) -> None:
        async def scenario() -> None:
            persistent = Mock()
            persistent.aclose = AsyncMock(side_effect=RuntimeError("unsafe detail"))
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"persistent": persistent}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSE_FAILED)

            for generation in range(10_000, 10_000 + RUNTIME_GENERATION_HISTORY_LIMIT + 1):
                manager._remember_terminal_status(  # noqa: SLF001
                    generation,
                    RuntimeGenerationStatus.CLOSED,
                )
            for index in range(RUNTIME_CLEANUP_DIAGNOSTIC_LIMIT + 5):
                manager._record_cleanup_failure(  # noqa: SLF001
                    1,
                    f"resource-{index}",
                    "RuntimeError",
                )

            self.assertIn(1, manager.failed_generations)
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSE_FAILED,
            )
            self.assertEqual(
                set(manager._failed_generations[1].clients),  # noqa: SLF001
                {"persistent"},
            )
            self.assertEqual(
                len(manager.cleanup_failures),
                RUNTIME_CLEANUP_DIAGNOSTIC_LIMIT,
            )
            self.assertIn(1, manager.retired_generations)

        run_async(scenario())

    def test_cleanup_task_cancelled_before_start_preserves_client_for_recovery(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"owned": client}))

            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)
            cleanup_task = next(iter(manager._cleanup_tasks))  # noqa: SLF001
            cleanup_task.cancel()
            await asyncio.sleep(0)
            await asyncio.sleep(0)

            client.aclose.assert_not_awaited()
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSE_FAILED,
            )
            self.assertEqual(
                set(manager._failed_generations[1].clients),  # noqa: SLF001
                {"owned"},
            )
            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_cleanup_task_cancelled_during_close_is_supervised_and_retried(self) -> None:
        async def scenario() -> None:
            close_started = asyncio.Event()
            call_count = 0

            async def close_client() -> None:
                nonlocal call_count
                call_count += 1
                if call_count == 1:
                    close_started.set()
                    await asyncio.Event().wait()

            client = Mock()
            client.aclose = AsyncMock(side_effect=close_client)
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"cancelled": client}))
            publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)

            await close_started.wait()
            cleanup_task = next(iter(manager._cleanup_tasks))  # noqa: SLF001
            cleanup_task.cancel()
            await _wait_for_status(manager, 1, RuntimeGenerationStatus.CLOSED)

            self.assertEqual(client.aclose.await_count, 2)
            self.assertEqual(manager.cleanup_task_count, 0)
            await manager.shutdown()

        run_async(scenario())

    def test_closed_history_is_bounded_and_live_records_are_compacted(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))

            final_generation = RUNTIME_GENERATION_HISTORY_LIMIT + 5
            for generation in range(2, final_generation + 1):
                publish_test_runtime_snapshot(manager, _snapshot(generation), expected_generation=generation - 1)
                await _wait_for_status(
                    manager,
                    generation - 1,
                    RuntimeGenerationStatus.CLOSED,
                )

            self.assertNotIn(1, manager.generation_statuses)
            self.assertLessEqual(
                len(manager.generation_statuses),
                RUNTIME_GENERATION_HISTORY_LIMIT + 1,
            )
            self.assertEqual(set(manager.active_leases), {final_generation})
            self.assertEqual(set(manager._generations), {final_generation})  # noqa: SLF001

            await manager.shutdown()
            self.assertLessEqual(
                len(manager.generation_statuses),
                RUNTIME_GENERATION_HISTORY_LIMIT,
            )

        run_async(scenario())

    def test_failed_cross_loop_release_remains_retryable_on_owner_loop(self) -> None:
        async def release_on_another_loop(lease) -> None:
            lease.release()

        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            lease = manager.acquire_current()

            def cross_loop_release() -> RuntimeManagerStateError:
                try:
                    run_async(release_on_another_loop(lease))
                except RuntimeManagerStateError as exc:
                    return exc
                raise AssertionError("Cross-loop release unexpectedly succeeded")

            error = await asyncio.to_thread(cross_loop_release)
            self.assertIn("another event loop", str(error))
            self.assertFalse(lease.released)
            self.assertEqual(manager.active_leases[1], 1)

            lease.release()
            self.assertTrue(lease.released)
            self.assertEqual(manager.active_leases[1], 0)
            await manager.shutdown()

        run_async(scenario())

    def test_shutdown_waits_for_lease_then_closes_current_generation(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"current": client}))
            lease = manager.acquire_current()

            shutdown_task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0)
            self.assertFalse(shutdown_task.done())
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPING)
            client.aclose.assert_not_awaited()

            lease.release()
            await shutdown_task
            client.aclose.assert_awaited_once()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.cleanup_task_count, 0)

        run_async(scenario())

    def test_persistent_shutdown_cleanup_reports_stop_failed_then_recovers(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock(
                side_effect=[
                    RuntimeError("unsafe")
                    for _attempt in range(RUNTIME_CLEANUP_MAX_ATTEMPTS * 2)
                ]
                + [None]
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"persistent": client}))

            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOP_FAILED)
            self.assertEqual(manager.failed_generations, (1,))
            self.assertEqual(
                manager.generation_statuses[1],
                RuntimeGenerationStatus.CLOSE_FAILED,
            )
            self.assertTrue(
                any(
                    failure.resource == "runtime_shutdown"
                    and failure.exception_type == "UnresolvedHttpClients"
                    for failure in manager.cleanup_failures
                )
            )

            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.failed_generations, ())
            self.assertEqual(client.aclose.await_count, 7)

        run_async(scenario())

    def test_concurrent_and_repeated_shutdown_share_one_cleanup(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"current": client}))

            await asyncio.gather(manager.shutdown(), manager.shutdown())
            await manager.shutdown()

            client.aclose.assert_awaited_once()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)

        run_async(scenario())

    def test_new_work_is_rejected_after_shutdown_begins(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            lease = manager.acquire_current()
            shutdown_task = asyncio.create_task(manager.shutdown())
            await asyncio.sleep(0)

            with self.assertRaises(RuntimeManagerStateError):
                manager.acquire_current()
            with self.assertRaises(RuntimeManagerStateError):
                manager.retain(1)
            with self.assertRaises(RuntimeManagerStateError):
                lease.retain()
            with self.assertRaises(RuntimeManagerStateError):
                publish_test_runtime_snapshot(manager, _snapshot(2), expected_generation=1)

            lease.release()
            await shutdown_task

        run_async(scenario())

    def test_empty_manager_shutdown_is_idempotent(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            await manager.shutdown()
            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
