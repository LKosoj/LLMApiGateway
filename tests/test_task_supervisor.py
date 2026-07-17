from __future__ import annotations

import asyncio
import inspect
import unittest
from unittest.mock import patch

from llm_gateway_core.services.task_supervisor import (
    TASK_FAILURE_JOURNAL_LIMIT,
    TaskFailure,
    TaskSupervisor,
    TaskSupervisorStateError,
)
from tests._async_compat import run_async


class TaskSupervisorTests(unittest.TestCase):
    def test_successful_task_is_strongly_tracked_until_done(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            finish = asyncio.Event()

            async def worker() -> int:
                await finish.wait()
                return 7

            task = supervisor.create_task(worker(), name="successful-worker")
            self.assertEqual(supervisor.task_count, 1)
            finish.set()

            self.assertEqual(await task, 7)
            await asyncio.sleep(0)
            self.assertEqual(supervisor.task_count, 0)
            self.assertEqual(supervisor.failures, ())
            await supervisor.close()

        run_async(scenario())

    def test_unexpected_failure_is_retrieved_and_recorded_safely(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            loop = asyncio.get_running_loop()
            unhandled_contexts: list[dict[str, object]] = []
            previous_handler = loop.get_exception_handler()
            loop.set_exception_handler(
                lambda _loop, context: unhandled_contexts.append(context)
            )

            async def worker() -> None:
                raise RuntimeError("https://user:secret@proxy.invalid")

            try:
                task = supervisor.create_task(worker(), name="failing-worker")
                await asyncio.wait({task})
                await asyncio.sleep(0)
            finally:
                loop.set_exception_handler(previous_handler)

            self.assertEqual(
                supervisor.failures,
                (TaskFailure("failing-worker", "RuntimeError"),),
            )
            self.assertNotIn("secret", repr(supervisor.failures))
            self.assertEqual(unhandled_contexts, [])
            await supervisor.close()

        run_async(scenario())

    def test_normal_task_cancellation_is_not_a_failure(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def worker() -> None:
                await asyncio.Event().wait()

            task = supervisor.create_task(worker(), name="cancelled-worker")
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            await asyncio.sleep(0)

            self.assertEqual(supervisor.task_count, 0)
            self.assertEqual(supervisor.failures, ())
            await supervisor.close()

        run_async(scenario())

    def test_close_stops_admission_cancels_and_awaits_every_task(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            started = [asyncio.Event(), asyncio.Event()]
            finished = [asyncio.Event(), asyncio.Event()]

            async def worker(index: int) -> None:
                started[index].set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished[index].set()

            tasks = [
                supervisor.create_task(worker(index), name=f"worker-{index}")
                for index in range(2)
            ]
            await asyncio.gather(*(event.wait() for event in started))

            await supervisor.close()

            self.assertFalse(supervisor.accepting)
            self.assertTrue(supervisor.closed)
            self.assertEqual(supervisor.task_count, 0)
            self.assertTrue(all(task.cancelled() for task in tasks))
            self.assertTrue(all(event.is_set() for event in finished))
            self.assertEqual(supervisor.failures, ())

        run_async(scenario())

    def test_concurrent_and_repeated_close_share_one_shutdown(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def worker() -> None:
                await asyncio.Event().wait()

            supervisor.create_task(worker(), name="shared-close-worker")
            await asyncio.gather(supervisor.close(), supervisor.close())
            close_task = supervisor._close_task  # noqa: SLF001
            await supervisor.close()

            self.assertIs(supervisor._close_task, close_task)  # noqa: SLF001
            self.assertTrue(supervisor.closed)
            self.assertEqual(supervisor.task_count, 0)

        run_async(scenario())

    def test_cancelled_close_caller_does_not_cancel_internal_shutdown(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            cancellation_seen = asyncio.Event()
            allow_finish = asyncio.Event()

            async def worker() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cancellation_seen.set()
                    await allow_finish.wait()
                    raise

            supervisor.create_task(worker(), name="shielded-close-worker")
            await asyncio.sleep(0)
            caller = asyncio.create_task(supervisor.close())
            await cancellation_seen.wait()
            caller.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await caller

            self.assertFalse(supervisor.closed)
            self.assertIsNotNone(supervisor._close_task)  # noqa: SLF001
            self.assertFalse(supervisor._close_task.cancelled())  # noqa: SLF001

            allow_finish.set()
            await supervisor.close()
            self.assertTrue(supervisor.closed)
            self.assertEqual(supervisor.task_count, 0)

        run_async(scenario())

    def test_cancelled_internal_close_task_is_replaced_and_cleanup_recovers(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            cleanup_started = asyncio.Event()
            allow_cleanup = asyncio.Event()

            async def worker() -> None:
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    while not allow_cleanup.is_set():
                        try:
                            await allow_cleanup.wait()
                        except asyncio.CancelledError:
                            continue
                    raise

            worker_task = supervisor.create_task(
                worker(),
                name="recoverable-worker",
            )
            first_caller = asyncio.create_task(supervisor.close())
            await cleanup_started.wait()
            first_close_task = supervisor._close_task  # noqa: SLF001
            self.assertIsNotNone(first_close_task)
            first_close_task.cancel()

            with self.assertRaises(asyncio.CancelledError):
                await first_caller
            self.assertTrue(first_close_task.done())
            self.assertFalse(supervisor.closed)
            self.assertEqual(supervisor.task_count, 1)

            retry = asyncio.create_task(supervisor.close())
            await asyncio.sleep(0)
            self.assertIsNot(supervisor._close_task, first_close_task)  # noqa: SLF001
            allow_cleanup.set()
            await retry

            self.assertTrue(worker_task.cancelled())
            self.assertTrue(supervisor.closed)
            self.assertEqual(supervisor.task_count, 0)
            self.assertEqual(supervisor.failures, ())

        run_async(scenario())

    def test_failed_internal_close_task_is_consumed_and_retried(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            original_close_impl = supervisor._close_impl  # noqa: SLF001
            attempts = 0

            async def flaky_close() -> None:
                nonlocal attempts
                attempts += 1
                if attempts == 1:
                    raise RuntimeError("https://user:secret@proxy.invalid")
                await original_close_impl()

            with patch.object(supervisor, "_close_impl", new=flaky_close):
                with self.assertRaisesRegex(RuntimeError, "secret"):
                    await supervisor.close()
                self.assertFalse(supervisor.closed)

                await supervisor.close()

            self.assertTrue(supervisor.closed)
            self.assertEqual(supervisor.task_count, 0)
            self.assertEqual(
                supervisor.failures,
                (TaskFailure("task-supervisor-close", "RuntimeError"),),
            )
            self.assertNotIn("secret", repr(supervisor.failures))

        run_async(scenario())

    def test_create_after_close_rejects_and_closes_coroutine(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()
            await supervisor.close()

            async def worker() -> None:
                return None

            coroutine = worker()
            with self.assertRaises(TaskSupervisorStateError):
                supervisor.create_task(coroutine, name="late-worker")

            self.assertEqual(
                inspect.getcoroutinestate(coroutine),
                inspect.CORO_CLOSED,
            )

        run_async(scenario())

    def test_task_creation_failure_closes_owned_coroutine(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def worker() -> None:
                return None

            coroutine = worker()
            with patch(
                "llm_gateway_core.services.task_supervisor.asyncio.create_task",
                side_effect=RuntimeError("creation failed"),
            ):
                with self.assertRaisesRegex(RuntimeError, "creation failed"):
                    supervisor.create_task(coroutine, name="creation-failure")

            self.assertEqual(
                inspect.getcoroutinestate(coroutine),
                inspect.CORO_CLOSED,
            )
            self.assertEqual(supervisor.task_count, 0)
            await supervisor.close()

        run_async(scenario())

    def test_cross_loop_operations_are_rejected_and_coroutine_is_closed(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def owner_worker() -> None:
                await asyncio.Event().wait()

            supervisor.create_task(owner_worker(), name="owner-worker")

            def use_from_another_loop() -> tuple[TaskSupervisorStateError, str]:
                async def cross_loop_worker() -> None:
                    return None

                coroutine = cross_loop_worker()
                try:
                    run_async(_create_cross_loop(supervisor, coroutine))
                except TaskSupervisorStateError as exc:
                    return exc, inspect.getcoroutinestate(coroutine)
                raise AssertionError("Cross-loop task creation unexpectedly succeeded")

            error, coroutine_state = await asyncio.to_thread(use_from_another_loop)
            self.assertIn("another event loop", str(error))
            self.assertEqual(coroutine_state, inspect.CORO_CLOSED)

            with self.assertRaises(TaskSupervisorStateError):
                await asyncio.to_thread(lambda: run_async(supervisor.close()))

            await supervisor.close()

        run_async(scenario())

    def test_failure_journal_is_bounded_to_latest_records(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def fail(index: int) -> None:
                raise LookupError(index)

            for index in range(TASK_FAILURE_JOURNAL_LIMIT + 3):
                task = supervisor.create_task(fail(index), name=f"failure-{index}")
                await asyncio.wait({task})
                await asyncio.sleep(0)

            self.assertEqual(len(supervisor.failures), TASK_FAILURE_JOURNAL_LIMIT)
            self.assertEqual(supervisor.failures[0].task_name, "failure-3")
            self.assertEqual(
                supervisor.failures[-1],
                TaskFailure(
                    f"failure-{TASK_FAILURE_JOURNAL_LIMIT + 2}",
                    "LookupError",
                ),
            )
            await supervisor.close()

        run_async(scenario())

    def test_invalid_task_name_is_rejected_without_leaking_coroutine(self) -> None:
        async def scenario() -> None:
            supervisor = TaskSupervisor()

            async def worker() -> None:
                return None

            coroutine = worker()
            with self.assertRaisesRegex(ValueError, "credential-safe"):
                supervisor.create_task(
                    coroutine,
                    name="https://user:secret@proxy.invalid",
                )

            self.assertEqual(
                inspect.getcoroutinestate(coroutine),
                inspect.CORO_CLOSED,
            )
            self.assertNotIn("secret", repr(supervisor.failures))
            await supervisor.close()

        run_async(scenario())


async def _create_cross_loop(
    supervisor: TaskSupervisor,
    coroutine,
) -> None:
    supervisor.create_task(coroutine, name="cross-loop-worker")


if __name__ == "__main__":
    unittest.main()
