import asyncio
import sqlite3
import unittest
import uuid
from pathlib import Path

from tests._async_compat import run_async
from llm_gateway_core.db.write_batcher import WriteBatcher


class WriteBatcherTests(unittest.TestCase):
    def setUp(self):
        db_dir = Path("db")
        db_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = db_dir / f"test_batcher_{uuid.uuid4().hex}.db"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()

    def tearDown(self):
        self.db_path.unlink(missing_ok=True)

    def test_enqueue_and_flush_writes_to_database(self):
        async def scenario():
            batcher = WriteBatcher(self.db_path, flush_interval=0.1)
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("hello",))
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("world",))
            await batcher.stop()

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            rows = conn.execute("SELECT value FROM items ORDER BY id").fetchall()
        self.assertEqual([r[0] for r in rows], ["hello", "world"])

    def test_stop_drains_all_pending_writes(self):
        async def scenario():
            batcher = WriteBatcher(self.db_path, batch_size=1000, flush_interval=60)
            await batcher.start()
            for i in range(50):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", (f"item-{i}",))
            await batcher.stop()

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 50)

    def test_enqueue_is_threadsafe(self):
        """Verify enqueue can be called from a background thread."""
        import threading

        async def scenario():
            batcher = WriteBatcher(self.db_path, flush_interval=0.1)
            await batcher.start()

            def background_writer():
                for i in range(10):
                    batcher.enqueue("INSERT INTO items (value) VALUES (?)", (f"thread-{i}",))

            t = threading.Thread(target=background_writer)
            t.start()
            await asyncio.to_thread(t.join)
            # Yield so call_soon_threadsafe callbacks are processed.
            await asyncio.sleep(0)
            await batcher.stop()

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 10)

    def test_batch_groups_writes_into_single_transaction(self):
        async def scenario():
            batcher = WriteBatcher(self.db_path, batch_size=5, flush_interval=60)
            await batcher.start()
            for i in range(5):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", (f"batch-{i}",))
            # Give the batcher time to process the batch
            await asyncio.sleep(0.2)
            await batcher.stop()

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            count = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
        self.assertEqual(count, 5)


if __name__ == "__main__":
    unittest.main()
