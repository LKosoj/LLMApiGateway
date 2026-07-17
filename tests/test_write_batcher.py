import asyncio
import json
import sqlite3
import tempfile
import threading
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.db import write_batcher as write_batcher_module
from llm_gateway_core.db.write_batcher import (
    DEFAULT_WRITE_BATCHER_QUEUE_MAXSIZE,
    WRITE_BATCHER_DEAD_LETTER_MAX_RECORD_BYTES,
    WriteBatcher,
    WriteBatcherOverloaded,
    WriteBatcherState,
    WriteBatcherUnavailable,
)
from tests._async_compat import run_async


class _BlockingFlushBatcher(WriteBatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flush_started = asyncio.Event()
        self.release_flush = asyncio.Event()

    async def _flush_once(self, batch):
        self.flush_started.set()
        await self.release_flush.wait()
        await super()._flush_once(batch)


class WriteBatcherTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "writer.db"
        self.dead_letter_path = Path(self._tmp.name) / "writer.dead-letter.jsonl"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def _values(self):
        with sqlite3.connect(self.db_path) as conn:
            return conn.execute("SELECT value FROM items ORDER BY id").fetchall()

    def test_capacity_validation_and_frozen_lifecycle_snapshot(self):
        for invalid in (0, -1, True, 1.5, "2"):
            with self.subTest(invalid=invalid), self.assertRaisesRegex(
                ValueError,
                "positive integer",
            ):
                WriteBatcher(self.db_path, queue_maxsize=invalid)

        async def scenario():
            batcher = WriteBatcher(self.db_path)
            initial = batcher.health_snapshot()
            self.assertEqual(initial.state, WriteBatcherState.NEW)
            self.assertTrue(initial.accepting)
            self.assertFalse(initial.task_running)
            self.assertEqual(initial.capacity, DEFAULT_WRITE_BATCHER_QUEUE_MAXSIZE)
            with self.assertRaises(FrozenInstanceError):
                initial.accepting = False

            await asyncio.gather(batcher.start(), batcher.start())
            task = batcher._task
            await batcher.start()
            self.assertIs(batcher._task, task)
            self.assertEqual(batcher.health_snapshot().state, WriteBatcherState.RUNNING)
            await batcher.stop()
            await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.state, WriteBatcherState.STOPPED)
            self.assertFalse(terminal.accepting)
            self.assertFalse(terminal.task_running)
            self.assertEqual(terminal.reserved, 0)
            with self.assertRaises(WriteBatcherUnavailable):
                await batcher.start()

        run_async(scenario())

    def test_prestart_buffer_is_bounded_and_drained_after_overload(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                queue_maxsize=2,
                dead_letter_path=self.dead_letter_path,
            )
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("one",))
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("two",))
            with self.assertRaises(WriteBatcherOverloaded):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("three",))

            # An enqueue overflow is transient capacity pressure, not a fatal
            # batcher failure: the batcher keeps accepting writes up to
            # capacity instead of tripping into a permanent FAILED state.
            saturated = batcher.health_snapshot()
            self.assertEqual(saturated.state, WriteBatcherState.NEW)
            self.assertTrue(saturated.accepting)
            self.assertEqual(saturated.pre_start, 2)
            self.assertEqual(saturated.reserved, 2)
            self.assertEqual(saturated.accepted, 2)
            self.assertEqual(saturated.overflow, 1)
            self.assertEqual(saturated.last_error_code, "overloaded")

            await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual(terminal.accepted, terminal.committed)

        run_async(scenario())
        self.assertEqual(self._values(), [("one",), ("two",)])

    def test_same_loop_capacity_counts_in_flight_and_queued(self):
        async def scenario():
            batcher = _BlockingFlushBatcher(
                self.db_path,
                batch_size=1,
                queue_maxsize=2,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("one",))
            await batcher.flush_started.wait()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("two",))

            live = batcher.health_snapshot()
            self.assertEqual((live.in_flight, live.queued, live.reserved), (1, 1, 2))
            with self.assertRaises(WriteBatcherOverloaded):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("three",))

            stop_task = asyncio.create_task(batcher.stop())
            batcher.release_flush.set()
            await stop_task
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.committed), (2, 2))

        run_async(scenario())
        self.assertEqual(self._values(), [("one",), ("two",)])

    def test_cross_thread_capacity_is_reserved_before_callbacks_run(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                queue_maxsize=2,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            loop = asyncio.get_running_loop()
            callbacks = []
            errors = []

            def capture(callback, *args):
                callbacks.append((callback, args))

            def writer():
                for value in ("one", "two", "three"):
                    try:
                        batcher.enqueue(
                            "INSERT INTO items (value) VALUES (?)",
                            (value,),
                        )
                    except WriteBatcherOverloaded as exc:
                        errors.append(exc)

            with patch.object(loop, "call_soon_threadsafe", side_effect=capture):
                thread = threading.Thread(target=writer)
                thread.start()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertEqual(len(callbacks), 2)
            saturated = batcher.health_snapshot()
            self.assertEqual((saturated.handoff_pending, saturated.reserved), (2, 2))

            for callback, args in callbacks:
                callback(*args)
            await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.committed), (2, 2))

        run_async(scenario())
        self.assertEqual(self._values(), [("one",), ("two",)])

    def test_failed_handoff_is_typed_and_terminal(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            loop = asyncio.get_running_loop()
            errors = []

            def writer():
                try:
                    batcher.enqueue(
                        "INSERT INTO items (value) VALUES (?)",
                        ("handoff-secret",),
                    )
                except WriteBatcherUnavailable as exc:
                    errors.append(exc)

            with patch.object(loop, "call_soon_threadsafe", side_effect=RuntimeError):
                thread = threading.Thread(target=writer)
                thread.start()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(len(errors), 1)
            failed = batcher.health_snapshot()
            self.assertEqual(failed.state, WriteBatcherState.FAILED)
            self.assertEqual(failed.reserved, 0)
            self.assertEqual((failed.accepted, failed.diagnostic_terminal), (1, 1))
            await batcher.stop()

        run_async(scenario())
        diagnostic = self.dead_letter_path.read_text(encoding="utf-8")
        self.assertNotIn("handoff-secret", diagnostic)

    def test_good_bad_good_split_has_safe_bounded_diagnostic(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                batch_size=3,
                flush_interval=60,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            batcher.enqueue("INSERT INTO items (id, value) VALUES (?, ?)", (1, "one"))
            batcher.enqueue(
                "INSERT INTO missing_sql_secret (value) VALUES (?)",
                ("param-secret", 987654321),
            )
            batcher.enqueue("INSERT INTO items (id, value) VALUES (?, ?)", (3, "three"))
            with self.assertLogs("llm_gateway_core.db.write_batcher", level="WARNING") as logs:
                await batcher.stop()

            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.state, WriteBatcherState.FAILED)
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.committed), (3, 2))
            self.assertEqual(terminal.diagnostic_terminal, 1)
            return "\n".join(logs.output)

        logs = run_async(scenario())
        self.assertEqual(self._values(), [("one",), ("three",)])
        diagnostic_bytes = self.dead_letter_path.read_bytes()
        diagnostic = diagnostic_bytes.decode("utf-8")
        self.assertLessEqual(
            len(diagnostic_bytes),
            WRITE_BATCHER_DEAD_LETTER_MAX_RECORD_BYTES,
        )
        self.assertEqual(len(diagnostic.splitlines()), 1)
        json.loads(diagnostic)
        for secret in (
            "missing_sql_secret",
            "param-secret",
            "987654321",
            str(self.db_path),
        ):
            self.assertNotIn(secret, diagnostic)
            self.assertNotIn(secret, logs)

    def test_dead_letter_append_failure_is_visible_without_leaking_permit(self):
        self.dead_letter_path.mkdir()

        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            batcher.enqueue("INSERT INTO missing_table VALUES (?)", ("secret",))
            with self.assertLogs("llm_gateway_core.db.write_batcher", level="ERROR") as logs:
                await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.diagnostic_terminal), (1, 1))
            self.assertEqual(terminal.dead_letter_failures, 1)
            self.assertEqual(terminal.last_error_code, "dead_letter_write_failed")
            self.assertNotIn(str(self.dead_letter_path), "\n".join(logs.output))

        run_async(scenario())

    def test_dead_letter_builder_and_file_limit_failures_are_visible(self):
        async def run_failure(path, params):
            batcher = WriteBatcher(self.db_path, dead_letter_path=path)
            await batcher.start()
            batcher.enqueue("INSERT INTO missing_table VALUES (?)", params)
            await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.diagnostic_terminal), (1, 1))
            self.assertEqual(terminal.dead_letter_failures, 1)
            self.assertEqual(terminal.last_error_code, "dead_letter_write_failed")

        huge_integer = 10**5000
        run_async(
            run_failure(
                Path(self._tmp.name) / "builder-failure.jsonl",
                (huge_integer,),
            )
        )

        limited_path = Path(self._tmp.name) / "file-limit.jsonl"
        with patch.object(
            write_batcher_module,
            "WRITE_BATCHER_DEAD_LETTER_MAX_FILE_BYTES",
            1,
        ):
            run_async(run_failure(limited_path, ("secret",)))


if __name__ == "__main__":
    unittest.main()
