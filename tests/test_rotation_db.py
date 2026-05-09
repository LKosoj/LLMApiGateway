import asyncio
import unittest
import uuid

from tests._async_compat import run_async
from llm_gateway_core.db.model_rotation_db import ModelRotationDB


class RotationDBAsyncTests(unittest.TestCase):
    def setUp(self):
        self.db = ModelRotationDB(db_filename=f"test_rotation_async_{uuid.uuid4().hex}.db")

    def tearDown(self):
        if self.db.db_path.exists():
            self.db.db_path.unlink()

    def test_get_next_model_index_handles_24_concurrent_coroutines(self):
        total_calls = 24
        total_models = 4

        async def run_concurrent_calls():
            start_event = asyncio.Event()

            async def worker():
                await start_event.wait()
                return await self.db.get_next_model_index(
                    "api-key",
                    "gateway-model",
                    total_models,
                )

            tasks = [asyncio.create_task(worker()) for _ in range(total_calls)]
            start_event.set()
            return await asyncio.gather(*tasks)

        with self.assertNoLogs(level="ERROR"):
            results = run_async(run_concurrent_calls())

        self.assertEqual(len(results), total_calls)
        self.assertEqual(
            sorted(results),
            sorted([index % total_models for index in range(total_calls)]),
        )
        self.assertEqual(
            run_async(self.db.get_next_model_index("api-key", "gateway-model", total_models)),
            0,
        )


if __name__ == "__main__":
    unittest.main()
