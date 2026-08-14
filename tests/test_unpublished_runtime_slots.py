from __future__ import annotations

import asyncio
import unittest
from unittest.mock import AsyncMock, Mock

from llm_gateway_core.services.runtime_config import (
    RUNTIME_CLEANUP_MAX_ATTEMPTS,
    RUNTIME_MAX_RETIRING_GENERATIONS,
    RuntimeGenerationConflictError,
    RuntimeGenerationManager,
    RuntimeManagerStateError,
    RuntimeManagerStatus,
    RuntimeSnapshot,
    RuntimeUnpublishedSlot,
)
from tests._async_compat import run_async
from tests.runtime_test_support import (
    install_test_runtime_snapshot,
    publish_test_runtime_snapshot,
)
from tests.test_runtime_generations import _snapshot


class RuntimeUnpublishedSlotTests(unittest.TestCase):
    def assert_current_snapshot(
        self,
        manager: RuntimeGenerationManager,
        expected: RuntimeSnapshot,
    ) -> None:
        self.assertEqual(manager.current_generation, expected.generation)
        lease = manager.acquire_current()
        try:
            self.assertIs(lease.snapshot, expected)
        finally:
            lease.release()

    def test_multiple_slots_have_unique_ids_and_preserve_generation_entries(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            first = manager.open_unpublished_slot(1)
            second = manager.open_unpublished_slot(1)

            self.assertNotEqual(first.slot_id, second.slot_id)
            self.assertEqual(manager.pending_unpublished_generations, (1, 1))
            self.assertEqual(await asyncio.gather(first.close_unpublished(), second.close_unpublished()), [True, True])
            self.assertEqual(manager.pending_unpublished_generations, ())
            await manager.shutdown()

        run_async(scenario())

    def test_registration_rejects_duplicate_names_and_exact_client_aliases(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            first = manager.open_unpublished_slot(1)
            second = manager.open_unpublished_slot(1)
            owned = Mock()
            owned.aclose = AsyncMock()
            unowned = Mock()
            unowned.aclose = AsyncMock()

            first.register_http_client("provider", owned)
            with self.assertRaises(RuntimeGenerationConflictError):
                first.register_http_client("provider", unowned)
            with self.assertRaises(RuntimeGenerationConflictError):
                second.register_http_client("other-provider", owned)

            self.assertTrue(await first.close_unpublished())
            self.assertTrue(await second.close_unpublished())
            owned.aclose.assert_awaited_once()
            unowned.aclose.assert_not_awaited()
            await manager.shutdown()

        run_async(scenario())

    def test_install_rejects_key_and_identity_mismatches_without_transfer(self) -> None:
        async def scenario() -> None:
            for clients in (
                {"renamed": owned},
                {"provider": different},
                {"provider": owned, "extra": different},
            ):
                with self.subTest(keys=tuple(clients)):
                    manager = RuntimeGenerationManager()
                    slot = manager.open_unpublished_slot(1)
                    slot.register_http_client("provider", owned)

                    with self.assertRaises(RuntimeGenerationConflictError):
                        manager.install_initial_candidate(_snapshot(1, clients=clients), slot=slot)

                    self.assertEqual(manager.status, RuntimeManagerStatus.NEW)
                    self.assertIsNone(manager.current_generation)
                    self.assertEqual(manager.pending_unpublished_generations, (1,))
                    owned.aclose.assert_not_awaited()
                    self.assertTrue(await slot.close_unpublished())
                    await manager.shutdown()
                    owned.aclose.reset_mock()

            different.aclose.assert_not_awaited()

        owned = Mock()
        owned.aclose = AsyncMock()
        different = Mock()
        different.aclose = AsyncMock()
        run_async(scenario())

    def test_install_rejects_wrong_generation_manager_and_inactive_slot(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            wrong_generation = manager.open_unpublished_slot(2)
            with self.assertRaises(RuntimeGenerationConflictError):
                manager.install_initial_candidate(_snapshot(1), slot=wrong_generation)

            foreign_manager = RuntimeGenerationManager()
            foreign_slot = foreign_manager.open_unpublished_slot(1)
            with self.assertRaises(RuntimeManagerStateError):
                manager.install_initial_candidate(_snapshot(1), slot=foreign_slot)

            self.assertTrue(await wrong_generation.close_unpublished())
            with self.assertRaises(RuntimeManagerStateError):
                manager.install_initial_candidate(_snapshot(1), slot=wrong_generation)
            self.assertTrue(await foreign_slot.close_unpublished())
            await manager.shutdown()
            await foreign_manager.shutdown()

        run_async(scenario())

    def test_successful_install_transfers_exact_clients_and_close_becomes_noop(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            slot = manager.open_unpublished_slot(1)
            slot.register_http_client("provider", client)
            snapshot = _snapshot(1, clients={"provider": client})

            manager.install_initial_candidate(snapshot, slot=slot)

            self.assertEqual(manager.status, RuntimeManagerStatus.RUNNING)
            self.assertEqual(manager.pending_unpublished_generations, ())
            lease = manager.acquire_current()
            self.assertIs(lease.snapshot, snapshot)
            lease.release()
            self.assertTrue(await slot.close_unpublished())
            client.aclose.assert_not_awaited()
            with self.assertRaises(RuntimeManagerStateError):
                slot.register_http_client("late", client)
            await manager.shutdown()
            client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_transient_close_retries_then_repeated_close_is_noop(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock(side_effect=[RuntimeError("secret"), None])
            manager = RuntimeGenerationManager()
            slot = manager.open_unpublished_slot(1)
            slot.register_http_client("provider", client)

            self.assertTrue(await slot.close_unpublished())
            self.assertTrue(await slot.close_unpublished())

            self.assertEqual(client.aclose.await_count, 2)
            self.assertEqual(manager.pending_unpublished_generations, ())
            self.assertEqual(manager.cleanup_failures[0].resource, "provider")
            self.assertNotIn("secret", repr(manager.cleanup_failures))
            await manager.shutdown()

        run_async(scenario())

    def test_concurrent_close_shares_work_and_retries_only_pending_clients(self) -> None:
        async def scenario() -> None:
            persistent = Mock()
            persistent.aclose = AsyncMock(
                side_effect=[
                    *[RuntimeError("secret") for _ in range(RUNTIME_CLEANUP_MAX_ATTEMPTS)],
                    None,
                ]
            )
            healthy = Mock()
            healthy.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            slot = manager.open_unpublished_slot(1)
            slot.register_http_client("persistent", persistent)
            slot.register_http_client("healthy", healthy)

            self.assertEqual(
                await asyncio.gather(slot.close_unpublished(), slot.close_unpublished()),
                [False, False],
            )
            self.assertEqual(persistent.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS)
            healthy.aclose.assert_awaited_once()
            self.assertEqual(manager.pending_unpublished_generations, (1,))
            self.assertEqual(manager.failed_unpublished_generations, (1,))
            with self.assertRaisesRegex(RuntimeManagerStateError, "cleanup"):
                manager.open_unpublished_slot(2)

            self.assertTrue(await slot.close_unpublished())
            self.assertEqual(manager.failed_unpublished_generations, ())
            self.assertEqual(persistent.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS + 1)
            healthy.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_new_shutdown_retries_persistent_slot_and_repeated_shutdown_recovers(self) -> None:
        async def scenario() -> None:
            client = Mock()
            client.aclose = AsyncMock(
                side_effect=[
                    *[
                        RuntimeError("secret")
                        for _ in range(RUNTIME_CLEANUP_MAX_ATTEMPTS * 2)
                    ],
                    None,
                ]
            )
            manager = RuntimeGenerationManager()
            slot = manager.open_unpublished_slot(1)
            slot.register_http_client("provider", client)

            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOP_FAILED)
            self.assertEqual(manager.pending_unpublished_generations, (1,))
            self.assertEqual(client.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS * 2)

            await manager.shutdown()
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)
            self.assertEqual(manager.pending_unpublished_generations, ())
            self.assertEqual(client.aclose.await_count, RUNTIME_CLEANUP_MAX_ATTEMPTS * 2 + 1)
            self.assertTrue(await slot.close_unpublished())

        run_async(scenario())

    def test_running_shutdown_closes_unpublished_before_current_generation(self) -> None:
        async def scenario() -> None:
            cleanup_order: list[str] = []
            current = Mock()
            current.aclose = AsyncMock(side_effect=lambda: cleanup_order.append("current"))
            unpublished = Mock()
            unpublished.aclose = AsyncMock(
                side_effect=lambda: cleanup_order.append("unpublished")
            )
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"current": current}))
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("candidate", unpublished)

            await manager.shutdown()

            self.assertEqual(cleanup_order, ["unpublished", "current"])
            self.assertEqual(manager.status, RuntimeManagerStatus.STOPPED)

        run_async(scenario())

    def test_publish_candidate_transfers_clients_and_retires_old_generation(self) -> None:
        async def scenario() -> None:
            old_client = Mock()
            old_client.aclose = AsyncMock()
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1, clients={"old": old_client}))
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("candidate", candidate_client)
            candidate = _snapshot(2, clients={"candidate": candidate_client})

            published = manager.publish_candidate(
                candidate,
                expected_generation=1,
                slot=slot,
            )

            self.assertIs(published, candidate)
            self.assertEqual(manager.pending_unpublished_generations, ())
            self.assert_current_snapshot(manager, candidate)
            self.assertTrue(await slot.close_unpublished())
            candidate_client.aclose.assert_not_awaited()

            await manager.shutdown()
            old_client.aclose.assert_awaited_once()
            candidate_client.aclose.assert_awaited_once()

        run_async(scenario())

    def test_publish_candidate_stale_rejection_keeps_slot_owned(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            client = Mock()
            client.aclose = AsyncMock()
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("candidate", client)
            candidate = _snapshot(2, clients={"candidate": client})

            with self.assertRaisesRegex(
                RuntimeGenerationConflictError,
                "changed while the candidate was being built",
            ):
                manager.publish_candidate(
                    candidate,
                    expected_generation=0,
                    slot=slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (2,))
            client.aclose.assert_not_awaited()
            self.assertTrue(await slot.close_unpublished())
            client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_candidate_busy_rejection_keeps_slot_owned(self) -> None:
        async def scenario() -> None:
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            manager = RuntimeGenerationManager()
            install_test_runtime_snapshot(manager, _snapshot(1))
            held_leases = [manager.acquire_current()]
            current = _snapshot(1)
            for generation in range(2, RUNTIME_MAX_RETIRING_GENERATIONS + 2):
                current = _snapshot(generation)
                publish_test_runtime_snapshot(
                    manager,
                    current,
                    expected_generation=generation - 1,
                )
                held_leases.append(manager.acquire_current())

            busy_generation = RUNTIME_MAX_RETIRING_GENERATIONS + 2
            slot = manager.open_unpublished_slot(busy_generation)
            slot.register_http_client("candidate", candidate_client)
            candidate = _snapshot(busy_generation, clients={"candidate": candidate_client})

            with self.assertRaisesRegex(RuntimeManagerStateError, "still retiring"):
                manager.publish_candidate(
                    candidate,
                    expected_generation=busy_generation - 1,
                    slot=slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (busy_generation,))
            candidate_client.aclose.assert_not_awaited()
            self.assertTrue(await slot.close_unpublished())
            candidate_client.aclose.assert_awaited_once()
            for lease in held_leases:
                lease.release()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_candidate_client_mismatch_keeps_slot_owned(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            client = Mock()
            client.aclose = AsyncMock()
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("candidate", client)
            candidate = _snapshot(2, clients={"renamed": client})

            with self.assertRaisesRegex(
                RuntimeGenerationConflictError,
                "do not match the unpublished slot",
            ):
                manager.publish_candidate(
                    candidate,
                    expected_generation=1,
                    slot=slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (2,))
            client.aclose.assert_not_awaited()
            self.assertTrue(await slot.close_unpublished())
            client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_candidate_rejects_foreign_and_forged_slots(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            foreign_manager = RuntimeGenerationManager()
            foreign_client = Mock()
            foreign_client.aclose = AsyncMock()
            foreign_slot = foreign_manager.open_unpublished_slot(2)
            foreign_slot.register_http_client("candidate", foreign_client)
            candidate = _snapshot(2, clients={"candidate": foreign_client})

            with self.assertRaisesRegex(RuntimeManagerStateError, "another manager"):
                manager.publish_candidate(
                    candidate,
                    expected_generation=1,
                    slot=foreign_slot,
                )

            local_client = Mock()
            local_client.aclose = AsyncMock()
            local_slot = manager.open_unpublished_slot(2)
            local_slot.register_http_client("candidate", local_client)
            forged_slot = RuntimeUnpublishedSlot(
                manager,
                local_slot.slot_id,
                object(),
                2,
            )
            local_candidate = _snapshot(2, clients={"candidate": local_client})
            with self.assertRaisesRegex(RuntimeManagerStateError, "identity is invalid"):
                manager.publish_candidate(
                    local_candidate,
                    expected_generation=1,
                    slot=forged_slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (2,))
            foreign_client.aclose.assert_not_awaited()
            local_client.aclose.assert_not_awaited()
            self.assertTrue(await foreign_slot.close_unpublished())
            self.assertTrue(await local_slot.close_unpublished())
            foreign_client.aclose.assert_awaited_once()
            local_client.aclose.assert_awaited_once()
            await foreign_manager.shutdown()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_candidate_inactive_rejection_shares_slot_cleanup(self) -> None:
        async def scenario() -> None:
            close_started = asyncio.Event()
            allow_close = asyncio.Event()

            async def close_candidate() -> None:
                close_started.set()
                await allow_close.wait()

            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            client = Mock()
            client.aclose = AsyncMock(side_effect=close_candidate)
            slot = manager.open_unpublished_slot(2)
            slot.register_http_client("candidate", client)
            candidate = _snapshot(2, clients={"candidate": client})
            close_task = asyncio.create_task(slot.close_unpublished())
            await close_started.wait()

            with self.assertRaisesRegex(RuntimeManagerStateError, "no longer active"):
                manager.publish_candidate(
                    candidate,
                    expected_generation=1,
                    slot=slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (2,))
            allow_close.set()
            self.assertTrue(await close_task)
            self.assertTrue(await slot.close_unpublished())
            client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())

    def test_publish_candidate_rejects_unresolved_other_slot_cleanup(self) -> None:
        async def scenario() -> None:
            manager = RuntimeGenerationManager()
            current = _snapshot(1)
            install_test_runtime_snapshot(manager, current)
            candidate_client = Mock()
            candidate_client.aclose = AsyncMock()
            candidate_slot = manager.open_unpublished_slot(2)
            candidate_slot.register_http_client("candidate", candidate_client)
            candidate = _snapshot(2, clients={"candidate": candidate_client})
            unresolved_client = Mock()
            unresolved_client.aclose = AsyncMock(side_effect=RuntimeError("secret"))
            unresolved_slot = manager.open_unpublished_slot(2)
            unresolved_slot.register_http_client("unresolved", unresolved_client)
            self.assertFalse(await unresolved_slot.close_unpublished())

            with self.assertRaisesRegex(
                RuntimeManagerStateError,
                "Unresolved unpublished runtime cleanup",
            ):
                manager.publish_candidate(
                    candidate,
                    expected_generation=1,
                    slot=candidate_slot,
                )

            self.assert_current_snapshot(manager, current)
            self.assertEqual(manager.pending_unpublished_generations, (2, 2))
            candidate_client.aclose.assert_not_awaited()
            unresolved_client.aclose.side_effect = None
            self.assertTrue(await unresolved_slot.close_unpublished())
            self.assertTrue(await candidate_slot.close_unpublished())
            candidate_client.aclose.assert_awaited_once()
            await manager.shutdown()

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
