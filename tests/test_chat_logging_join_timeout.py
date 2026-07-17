"""Verify bounded wait for ChunkProcessor during streaming shutdown.

Regression for a hang risk: if the background log worker is stuck on disk/SQLite
I/O, the wait() call must not block the event loop indefinitely.
"""
import asyncio
import threading
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from llm_gateway_core.middleware import chat_logging
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services


_CONFIG_LOADER = SimpleNamespace(fallback_rules={})
_COST_RATE_REGISTRY = MappingProxyType({})


class ChunkProcessorWaitTimeoutTests(unittest.TestCase):
    def test_join_timeout_constant_is_finite_and_small(self):
        """Sanity: timeout must be short enough to matter and large enough for normal work."""
        self.assertGreater(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS, 0)
        self.assertLess(chat_logging.CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS, 60)

    def test_timed_out_worker_remains_supervisor_owned_and_is_drained(self):
        async def scenario():
            services = make_app_services()
            processor = chat_logging.ChunkProcessor(
                req_headers={},
                req_body_str="{}",
                is_real_streaming=False,
                services=services,
                config_loader=_CONFIG_LOADER,
                cost_rate_registry=_COST_RATE_REGISTRY,
                operation="chat",
            )
            started = threading.Event()
            release = threading.Event()

            def stuck_write_once(self_):
                started.set()
                release.wait()

            with patch.object(
                chat_logging.ChunkProcessor,
                "_write_log_once",
                stuck_write_once,
            ):
                processor.start()
                await processor.finish()
                self.assertTrue(await asyncio.to_thread(started.wait, 1.0))
                completed = await processor.wait(0.01)
                still_running = not processor.done()
                owned_while_blocked = services.task_supervisor.task_count
                release.set()
                self.assertTrue(await processor.wait(1.0))
                await asyncio.sleep(0)
                return (
                    completed,
                    still_running,
                    owned_while_blocked,
                    services.task_supervisor.task_count,
                )

        completed, still_running, owned_while_blocked, terminal_tasks = run_async(
            scenario()
        )

        self.assertFalse(completed, "wait() should have reported timeout")
        self.assertTrue(still_running)
        self.assertEqual(owned_while_blocked, 1)
        self.assertEqual(terminal_tasks, 0)


if __name__ == "__main__":
    unittest.main()
