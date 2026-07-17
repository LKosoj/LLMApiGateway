import asyncio
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.db.write_batcher import (
    WriteBatcher,
    WriteBatcherState,
    WriteBatcherUnavailable,
)
from tests._async_compat import run_async


class _ObservedStopBatcher(WriteBatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_started = asyncio.Event()

    async def _stop_impl(self):
        self.stop_started.set()
        await super()._stop_impl()


class _BlockingFlushBatcher(_ObservedStopBatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.flush_started = asyncio.Event()
        self.release_flush = asyncio.Event()

    async def _flush_once(self, batch):
        self.flush_started.set()
        await self.release_flush.wait()
        await super()._flush_once(batch)


class _FatalWriterError(BaseException):
    pass


class _FatalFlushBatcher(WriteBatcher):
    async def _flush_once(self, batch):
        raise _FatalWriterError


class WriteBatcherShutdownTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = Path(self._tmp.name) / "writer.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()

    def tearDown(self):
        self._tmp.cleanup()

    def test_stop_waits_for_accepted_cross_thread_handoff(self):
        async def scenario():
            batcher = _ObservedStopBatcher(self.db_path, flush_interval=60)
            await batcher.start()
            loop = asyncio.get_running_loop()
            callbacks = []

            def capture(callback, *args):
                callbacks.append((callback, args))

            with patch.object(loop, "call_soon_threadsafe", side_effect=capture):
                thread = threading.Thread(
                    target=batcher.enqueue,
                    args=("INSERT INTO items (value) VALUES (?)", ("accepted",)),
                )
                thread.start()
                thread.join(timeout=2)

            self.assertFalse(thread.is_alive())
            self.assertEqual(batcher.health_snapshot().handoff_pending, 1)
            stop_task = asyncio.create_task(batcher.stop())
            await batcher.stop_started.wait()
            self.assertFalse(stop_task.done())
            self.assertEqual(batcher.health_snapshot().state, WriteBatcherState.STOPPING)

            callback, args = callbacks.pop()
            callback(*args)
            await asyncio.wait_for(stop_task, timeout=2)
            self.assertEqual(batcher.health_snapshot().reserved, 0)

        run_async(scenario())
        with sqlite3.connect(self.db_path) as conn:
            values = conn.execute("SELECT value FROM items").fetchall()
        self.assertEqual(values, [("accepted",)])

    def test_cancelled_stop_caller_does_not_cancel_shared_drain(self):
        async def scenario():
            batcher = _BlockingFlushBatcher(self.db_path, flush_interval=60)
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("kept",))
            await batcher.flush_started.wait()

            first = asyncio.create_task(batcher.stop())
            second = asyncio.create_task(batcher.stop())
            await batcher.stop_started.wait()
            with self.assertRaises(WriteBatcherUnavailable):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("late",))

            first.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await first
            self.assertFalse(second.done())

            batcher.release_flush.set()
            await asyncio.wait_for(second, timeout=2)
            await batcher.stop()
            terminal = batcher.health_snapshot()
            self.assertEqual(terminal.state, WriteBatcherState.STOPPED)
            self.assertEqual(terminal.reserved, 0)
            self.assertEqual((terminal.accepted, terminal.committed), (1, 1))

        run_async(scenario())

    def test_consumer_cancellation_terminalizes_in_flight_and_queued(self):
        async def scenario():
            batcher = _BlockingFlushBatcher(
                self.db_path,
                batch_size=1,
                queue_maxsize=2,
            )
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("in-flight",))
            await batcher.flush_started.wait()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("queued",))

            consumer = batcher._task
            self.assertIsNotNone(consumer)
            consumer.cancel()
            await consumer
            failed = batcher.health_snapshot()
            self.assertEqual(failed.state, WriteBatcherState.FAILED)
            self.assertFalse(failed.accepting)
            self.assertEqual(failed.reserved, 0)
            self.assertEqual((failed.accepted, failed.diagnostic_terminal), (2, 2))
            with self.assertRaises(WriteBatcherUnavailable):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("late",))
            await batcher.stop()

        run_async(scenario())

    def test_non_exception_consumer_failure_terminalizes_accepted_work(self):
        async def scenario():
            batcher = _FatalFlushBatcher(self.db_path)
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("fatal",))
            await batcher.stop()

            failed = batcher.health_snapshot()
            self.assertEqual(failed.state, WriteBatcherState.FAILED)
            self.assertEqual(failed.reserved, 0)
            self.assertEqual((failed.accepted, failed.diagnostic_terminal), (1, 1))

        run_async(scenario())

    def test_non_exception_prestart_flush_failure_terminalizes_accepted_work(self):
        async def scenario():
            batcher = _FatalFlushBatcher(self.db_path)
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("fatal",))
            await batcher.stop()

            failed = batcher.health_snapshot()
            self.assertEqual(failed.state, WriteBatcherState.FAILED)
            self.assertEqual(failed.reserved, 0)
            self.assertEqual((failed.accepted, failed.diagnostic_terminal), (1, 1))

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
