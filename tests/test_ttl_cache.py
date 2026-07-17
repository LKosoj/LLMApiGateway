"""Unit tests for AsyncTtlCache.

Verifies cache hits within TTL, refetch after expiry, per-key isolation,
and invalidation semantics.
"""
import asyncio
import unittest
from unittest.mock import patch

from llm_gateway_core.utils.ttl_cache import AsyncTtlCache
from tests._async_compat import run_async


class AsyncTtlCacheTests(unittest.TestCase):
    def test_max_entries_requires_positive_exact_integer(self):
        for invalid in (True, False, 0, -1, 1.5, "2"):
            with self.subTest(invalid=invalid):
                with self.assertRaisesRegex(ValueError, "positive integer"):
                    AsyncTtlCache(ttl_seconds=10.0, max_entries=invalid)

        cache = AsyncTtlCache(ttl_seconds=10.0, max_entries=1)
        self.assertEqual(cache._max_entries, 1)

    def test_first_call_invokes_producer(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return "v1"

        result = run_async(cache.get_or_compute("k", producer))
        self.assertEqual(result, "v1")
        self.assertEqual(calls, 1)

    def test_repeat_call_within_ttl_uses_cache(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return calls

        async def run():
            v1 = await cache.get_or_compute("k", producer)
            v2 = await cache.get_or_compute("k", producer)
            return v1, v2

        v1, v2 = run_async(run())
        self.assertEqual(v1, 1)
        self.assertEqual(v2, 1)
        self.assertEqual(calls, 1)

    def test_call_after_ttl_expiry_refetches(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return calls

        async def run():
            with patch("llm_gateway_core.utils.ttl_cache.time.monotonic", return_value=1000.0):
                v1 = await cache.get_or_compute("k", producer)
            with patch("llm_gateway_core.utils.ttl_cache.time.monotonic", return_value=1011.0):
                v2 = await cache.get_or_compute("k", producer)
            return v1, v2

        v1, v2 = run_async(run())
        self.assertEqual(v1, 1)
        self.assertEqual(v2, 2)

    def test_different_keys_cached_independently(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = []

        async def producer_for(key):
            calls.append(key)
            return f"val-{key}"

        async def run():
            a = await cache.get_or_compute("a", lambda: producer_for("a"))
            b = await cache.get_or_compute("b", lambda: producer_for("b"))
            a2 = await cache.get_or_compute("a", lambda: producer_for("a"))
            return a, b, a2

        a, b, a2 = run_async(run())
        self.assertEqual(a, "val-a")
        self.assertEqual(b, "val-b")
        self.assertEqual(a2, "val-a")
        self.assertEqual(calls, ["a", "b"])

    def test_invalidate_single_key(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            return calls

        async def run():
            await cache.get_or_compute("k", producer)
            cache.invalidate("k")
            return await cache.get_or_compute("k", producer)

        result = run_async(run())
        self.assertEqual(result, 2)
        self.assertEqual(calls, 2)

    def test_invalidate_all(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)

        async def run():
            await cache.get_or_compute("a", lambda: _immediate("x"))
            await cache.get_or_compute("b", lambda: _immediate("y"))
            cache.invalidate()
            return cache._entries

        entries = run_async(run())
        self.assertEqual(entries, {})

    def test_lookup_prunes_all_expired_entries(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)

        async def run():
            with patch(
                "llm_gateway_core.utils.ttl_cache.time.monotonic",
                return_value=100.0,
            ):
                await cache.get_or_compute("a", lambda: _immediate("a"))
                await cache.get_or_compute("b", lambda: _immediate("b"))
            with patch(
                "llm_gateway_core.utils.ttl_cache.time.monotonic",
                return_value=111.0,
            ):
                await cache.get_or_compute("c", lambda: _immediate("c"))

        run_async(run())
        self.assertEqual(set(cache._entries), {"c"})

    def test_bound_evicts_oldest_entry_when_expiry_is_equal(self):
        cache = AsyncTtlCache(ttl_seconds=10.0, max_entries=2)

        async def run():
            with patch(
                "llm_gateway_core.utils.ttl_cache.time.monotonic",
                return_value=100.0,
            ):
                await cache.get_or_compute("a", lambda: _immediate("a"))
                await cache.get_or_compute("b", lambda: _immediate("b"))
                await cache.get_or_compute("c", lambda: _immediate("c"))

        run_async(run())
        self.assertEqual(list(cache._entries), ["b", "c"])

    def test_bound_holds_after_concurrent_slow_producers(self):
        cache = AsyncTtlCache(ttl_seconds=10.0, max_entries=2)

        async def run():
            release = asyncio.Event()
            all_started = asyncio.Event()
            started = 0

            async def producer(value):
                nonlocal started
                started += 1
                if started == 5:
                    all_started.set()
                await release.wait()
                return value

            tasks = [
                asyncio.create_task(cache.get_or_compute(key, lambda key=key: producer(key)))
                for key in range(5)
            ]
            await all_started.wait()
            release.set()
            await asyncio.gather(*tasks)

        run_async(run())
        self.assertEqual(len(cache._entries), 2)

    def test_producer_exception_is_not_cached(self):
        cache = AsyncTtlCache(ttl_seconds=10.0)
        calls = 0

        async def producer():
            nonlocal calls
            calls += 1
            if calls == 1:
                raise RuntimeError("failed")
            return "ok"

        async def run():
            with self.assertRaisesRegex(RuntimeError, "failed"):
                await cache.get_or_compute("k", producer)
            return await cache.get_or_compute("k", producer)

        self.assertEqual(run_async(run()), "ok")
        self.assertEqual(calls, 2)


async def _immediate(value):
    return value


if __name__ == "__main__":
    unittest.main()
