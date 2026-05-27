import asyncio
import sqlite3
import unittest
import uuid
from pathlib import Path

from llm_gateway_core.db.write_batcher import WriteBatcher
from tests._async_compat import run_async


class _BlockingStopBatcher(WriteBatcher):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.stop_drain_seen = asyncio.Event()
        self.stop_flush_waiting = asyncio.Event()
        self.release_stop_flush = asyncio.Event()

    def _drain_remaining_queue_into(self, batch: list) -> None:
        super()._drain_remaining_queue_into(batch)
        self.stop_drain_seen.set()

    async def _flush(self, batch: list[tuple[str, tuple]]) -> None:
        if self.stop_drain_seen.is_set() and not self.release_stop_flush.is_set():
            self.stop_flush_waiting.set()
            await self.release_stop_flush.wait()
        await super()._flush(batch)


class WriteBatcherShutdownTests(unittest.TestCase):
    def setUp(self):
        db_dir = Path("db")
        db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_dir / f"test_batcher_shutdown_{uuid.uuid4().hex}.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)
        self.db_path.with_suffix(self.db_path.suffix + "-wal").unlink(missing_ok=True)
        self.db_path.with_suffix(self.db_path.suffix + "-shm").unlink(missing_ok=True)

    def test_enqueue_after_stop_sentinel_raises_explicit_error(self):
        async def scenario():
            batcher = _BlockingStopBatcher(self.db_path, flush_interval=60)
            await batcher.start()
            stop_task = asyncio.create_task(batcher.stop())
            await batcher.stop_flush_waiting.wait()

            with self.assertRaisesRegex(RuntimeError, "stopping"):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("late",))

            batcher.release_stop_flush.set()
            await stop_task

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT value FROM items").fetchall()
        self.assertEqual(rows, [])

    def test_callback_queued_before_stop_is_flushed(self):
        async def scenario():
            batcher = WriteBatcher(self.db_path, batch_size=1000, flush_interval=60)
            await batcher.start()
            batcher._loop.call_soon(
                batcher._queue.put_nowait,
                ("INSERT INTO items (value) VALUES (?)", ("queued-before-stop",)),
            )
            await batcher.stop()

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT value FROM items").fetchall()
        self.assertEqual([row[0] for row in rows], ["queued-before-stop"])


if __name__ == "__main__":
    unittest.main()
