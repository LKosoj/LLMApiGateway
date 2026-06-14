"""Unit tests for the per-IP brute-force guard."""

from __future__ import annotations

import unittest

from llm_gateway_core.services.ip_blocklist import IpBlockGuard


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def tick(self, delta: float) -> None:
        self._t += delta


class IpBlockGuardTests(unittest.TestCase):
    def test_rejects_invalid_thresholds(self):
        with self.assertRaises(ValueError):
            IpBlockGuard(max_failures=0)
        with self.assertRaises(ValueError):
            IpBlockGuard(block_seconds=0)

    def test_unknown_ip_is_not_blocked(self):
        guard = IpBlockGuard()
        self.assertIsNone(guard.check_blocked("1.2.3.4"))

    def test_blank_ip_is_ignored(self):
        guard = IpBlockGuard(max_failures=1)
        self.assertIsNone(guard.register_failure(""))
        self.assertIsNone(guard.check_blocked(""))

    def test_blocks_after_threshold_consecutive_failures(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=5, block_seconds=1200.0, time_func=clock)
        ip = "150.109.231.218"
        # First four failures do not trigger a block.
        for _ in range(4):
            self.assertIsNone(guard.register_failure(ip))
            self.assertIsNone(guard.check_blocked(ip))
        # The fifth failure triggers the block and reports its duration once.
        self.assertEqual(guard.register_failure(ip), 1200)
        self.assertEqual(guard.check_blocked(ip), 1200)

    def test_trigger_is_reported_only_once(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=2, block_seconds=600.0, time_func=clock)
        ip = "10.0.0.1"
        self.assertIsNone(guard.register_failure(ip))
        self.assertEqual(guard.register_failure(ip), 600)
        # Further failures while already blocked must not re-trigger.
        self.assertIsNone(guard.register_failure(ip))

    def test_success_resets_consecutive_counter(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=3, block_seconds=600.0, time_func=clock)
        ip = "10.0.0.2"
        guard.register_failure(ip)
        guard.register_failure(ip)
        guard.register_success(ip)
        # Counter reset: three more failures are needed to block.
        self.assertIsNone(guard.register_failure(ip))
        self.assertIsNone(guard.register_failure(ip))
        self.assertEqual(guard.register_failure(ip), 600)

    def test_block_expires_after_window(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=1, block_seconds=1200.0, time_func=clock)
        ip = "10.0.0.3"
        self.assertEqual(guard.register_failure(ip), 1200)
        self.assertEqual(guard.check_blocked(ip), 1200)
        clock.tick(1199.0)
        self.assertEqual(guard.check_blocked(ip), 1)
        clock.tick(2.0)
        self.assertIsNone(guard.check_blocked(ip))

    def test_fresh_counting_after_block_expires(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=2, block_seconds=100.0, time_func=clock)
        ip = "10.0.0.4"
        guard.register_failure(ip)
        self.assertEqual(guard.register_failure(ip), 100)
        clock.tick(101.0)
        # Block expired — one failure should not immediately re-block.
        self.assertIsNone(guard.register_failure(ip))
        self.assertEqual(guard.register_failure(ip), 100)

    def test_success_does_not_lift_active_block(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=1, block_seconds=600.0, time_func=clock)
        ip = "10.0.0.5"
        self.assertEqual(guard.register_failure(ip), 600)
        guard.register_success(ip)
        self.assertEqual(guard.check_blocked(ip), 600)

    def test_reset_single_and_all(self):
        guard = IpBlockGuard(max_failures=1, block_seconds=600.0)
        guard.register_failure("a")
        guard.register_failure("b")
        guard.reset("a")
        self.assertIsNone(guard.check_blocked("a"))
        self.assertEqual(guard.check_blocked("b"), 600)
        guard.reset()
        self.assertIsNone(guard.check_blocked("b"))

    def test_failures_are_per_ip(self):
        clock = _FakeClock()
        guard = IpBlockGuard(max_failures=2, block_seconds=600.0, time_func=clock)
        guard.register_failure("a")
        # Different IP keeps its own counter.
        self.assertIsNone(guard.register_failure("b"))
        self.assertEqual(guard.register_failure("a"), 600)
        self.assertIsNone(guard.check_blocked("b"))


if __name__ == "__main__":
    unittest.main()
