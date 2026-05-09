"""Verify bounded wait for ChunkProcessor during streaming shutdown.

Regression for a hang risk: if the background log worker is stuck on disk/SQLite
I/O, the wait() call must not block the event loop indefinitely.
"""
import asyncio
import time
import unittest
from unittest.mock import patch

from llm_gateway_core.middleware import chat_logging
from tests._async_compat import run_async


class ChunkProcessorWaitTimeoutTests(unittest.TestCase):
    def test_join_timeout_constant_is_finite_and_small(self):
        """Sanity: timeout must be short enough to matter and large enough for normal work."""
        self.assertGreater(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS, 0)
        self.assertLess(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS, 60)

    def test_stuck_worker_does_not_block_finalizer(self):
        """A worker that blocks in its record-write path must not hang the generator.

        We simulate this by patching _write_log_once to sleep long, then
        ensure the outer cleanup returns promptly (wait() honors the timeout
        and returns False while the task continues in the background).
        """
        async def scenario():
            processor = chat_logging.ChunkProcessor(
                req_headers={},
                req_body_str="{}",
                is_real_streaming=False,
                operation="chat",
            )

            def stuck_write_once(self_):
                time.sleep(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS + 2)

            with patch.object(
                chat_logging.ChunkProcessor,
                "_write_log_once",
                stuck_write_once,
            ):
                processor.start()
                await processor.finish()
                t0 = time.monotonic()
                completed = await processor.wait(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS)
                elapsed = time.monotonic() - t0
                still_running = not processor.done()
                # Let the task finish in the background to avoid leaking into other tests.
                try:
                    await asyncio.wait_for(asyncio.shield(processor._task), timeout=10)
                except asyncio.TimeoutError:
                    pass
                return completed, elapsed, still_running

        completed, elapsed, still_running = run_async(scenario())

        self.assertFalse(completed, "wait() should have reported timeout")
        self.assertLess(
            elapsed,
            chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS + 1.0,
            "wait() honored the timeout and did not block indefinitely",
        )
        self.assertTrue(still_running, "task should keep running in the background")


if __name__ == "__main__":
    unittest.main()
