"""Unit tests for the sliding-window rate limiter."""

from __future__ import annotations

import unittest

from llm_gateway_core.services.rate_limiter import RateLimiter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def tick(self, delta: float) -> None:
        self._t += delta


class RateLimiterTests(unittest.TestCase):
    def test_no_limit_allows_all(self):
        rl = RateLimiter()
        self.assertIsNone(rl.check(1, rpm_limit=None, tpm_limit=None))

    def test_rpm_blocks_after_threshold(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        for _ in range(3):
            self.assertIsNone(rl.check(42, rpm_limit=3, tpm_limit=None))
            rl.record(42)
        err = rl.check(42, rpm_limit=3, tpm_limit=None)
        self.assertIsNotNone(err)
        self.assertIn("RPM", err)

    def test_rpm_clears_after_window(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        for _ in range(3):
            rl.record(42)
        self.assertIsNotNone(rl.check(42, rpm_limit=3, tpm_limit=None))
        clock.tick(61.0)
        self.assertIsNone(rl.check(42, rpm_limit=3, tpm_limit=None))

    def test_tpm_accumulates_tokens(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.record(7, tokens=400)
        rl.record(7, tokens=400)
        err = rl.check(7, rpm_limit=None, tpm_limit=500)
        self.assertIsNotNone(err)
        self.assertIn("TPM", err)

    def test_add_tokens_updates_last_event(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.record(1, tokens=0)
        rl.add_tokens(1, 250)
        self.assertIsNotNone(rl.check(1, rpm_limit=None, tpm_limit=200))

    def test_reset_single_key(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.record(1)
        rl.record(2)
        rl.reset(1)
        self.assertIsNone(rl.check(1, rpm_limit=1, tpm_limit=None))
        rl.record(2)
        self.assertIsNotNone(rl.check(2, rpm_limit=1, tpm_limit=None))

    def test_reset_all_clears_state(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.record(1)
        rl.record(2)
        rl.reset()
        self.assertIsNone(rl.check(1, rpm_limit=1, tpm_limit=None))
        self.assertIsNone(rl.check(2, rpm_limit=1, tpm_limit=None))


class TryAcquireTests(unittest.TestCase):
    def test_records_when_under_limit(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.try_acquire(1, rpm_limit=2, tpm_limit=None))
        self.assertIsNone(rl.try_acquire(1, rpm_limit=2, tpm_limit=None))
        # 3rd call exceeds RPM=2.
        err = rl.try_acquire(1, rpm_limit=2, tpm_limit=None)
        self.assertIsNotNone(err)
        self.assertIn("RPM", err)

    def test_does_not_record_when_limit_exceeded(self):
        """Atomicity: a rejected try_acquire must NOT bump the counters.

        If it did, a burst of rejected requests would keep extending the
        window and lock the key out indefinitely.
        """
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.try_acquire(1, rpm_limit=1, tpm_limit=None)
        # Next call must be rejected and must not add a 2nd event.
        self.assertIsNotNone(rl.try_acquire(1, rpm_limit=1, tpm_limit=None))
        # Move window forward just past the first event — now the next call
        # should succeed because the rejected one did NOT persist.
        clock.tick(61.0)
        self.assertIsNone(rl.try_acquire(1, rpm_limit=1, tpm_limit=None))

    def test_tpm_is_checked_and_recorded(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        # First call: state is empty — admit and record 400 tokens.
        self.assertIsNone(rl.try_acquire(1, rpm_limit=None, tpm_limit=500, tokens=400))
        # Second call: state is 400, still < 500, admit and record 200 more.
        self.assertIsNone(rl.try_acquire(1, rpm_limit=None, tpm_limit=500, tokens=200))
        # Third call: state is 600 >= 500 — reject.
        err = rl.try_acquire(1, rpm_limit=None, tpm_limit=500, tokens=0)
        self.assertIsNotNone(err)
        self.assertIn("TPM", err)


if __name__ == "__main__":
    unittest.main()
