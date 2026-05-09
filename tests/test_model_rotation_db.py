import asyncio
import sqlite3
import unittest
import uuid

from tests._async_compat import run_async
from llm_gateway_core.db.model_rotation_db import ModelRotationDB


class ModelRotationDBTests(unittest.TestCase):
    def setUp(self):
        self.db = ModelRotationDB(db_filename=f"test_rotation_{uuid.uuid4().hex}.db")

    def tearDown(self):
        if self.db.db_path.exists():
            self.db.db_path.unlink()

    def test_get_next_model_index_is_atomic_under_concurrency(self):
        total_calls = 12
        total_models = 3

        async def run_concurrent():
            barrier = asyncio.Barrier(total_calls)

            async def get_index():
                await barrier.wait()
                return await self.db.get_next_model_index("api-key", "gateway-model", total_models)

            tasks = [asyncio.create_task(get_index()) for _ in range(total_calls)]
            return await asyncio.gather(*tasks)

        with self.assertNoLogs(level="ERROR"):
            results = run_async(run_concurrent())

        self.assertEqual(
            sorted(results),
            sorted([index % total_models for index in range(total_calls)]),
        )
        self.assertEqual(
            run_async(self.db.get_next_model_index("api-key", "gateway-model", total_models)),
            0,
        )

    def test_model_rotation_db_uses_wal_journal_mode(self):
        with sqlite3.connect(self.db.db_path) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()
