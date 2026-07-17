"""Unit tests for the sliding-window rate limiter."""

from __future__ import annotations

import threading
import unittest

from llm_gateway_core.services.accounting import AccountingValidationError
from llm_gateway_core.services.rate_limiter import REQUEST_ALREADY_TERMINAL, RateLimiter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._t = start

    def __call__(self) -> float:
        return self._t

    def tick(self, delta: float) -> None:
        self._t += delta


class _SequenceClock:
    def __init__(self, *values: float) -> None:
        self._values = iter(values)
        self._lock = threading.Lock()

    def __call__(self) -> float:
        with self._lock:
            return next(self._values)


class _DelayedThreadLock:
    def __init__(self, delayed_thread: str) -> None:
        self._lock = threading.Lock()
        self._delayed_thread = delayed_thread
        self.delayed_ready = threading.Event()
        self.release_delayed = threading.Event()

    def __enter__(self):
        if threading.current_thread().name == self._delayed_thread:
            self.delayed_ready.set()
            self.release_delayed.wait()
        self._lock.acquire()
        return self

    def __exit__(self, exc_type, exc_value, traceback) -> None:
        self._lock.release()


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
        # Third call: state is 600 >= 500 — reject (tokens must be >0 for TPM check).
        err = rl.try_acquire(1, rpm_limit=None, tpm_limit=500, tokens=1)
        self.assertIsNotNone(err)
        self.assertIn("TPM", err)


class RequestRateLimitTests(unittest.TestCase):
    def test_terminal_tokens_are_attributed_to_the_matching_request(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=3, tpm_limit=100))
        self.assertIsNone(rl.admit_request("request-b", 1, rpm_limit=3, tpm_limit=100))

        self.assertTrue(rl.attribute_request_tokens("request-b", 1, 60))
        self.assertTrue(rl.attribute_request_tokens("request-a", 1, 30))

        self.assertEqual(rl.get_window_snapshot(1)[:2], (2, 90))

    def test_terminal_attribution_can_push_window_over_tpm_limit(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=100))
        self.assertTrue(rl.attribute_request_tokens("request-a", 1, 90))
        self.assertIsNone(rl.admit_request("request-b", 1, rpm_limit=None, tpm_limit=100))

        self.assertTrue(rl.attribute_request_tokens("request-b", 1, 20))

        self.assertEqual(rl.get_window_snapshot(1)[:2], (2, 110))
        error = rl.admit_request("request-c", 1, rpm_limit=None, tpm_limit=100)
        self.assertIsNotNone(error)
        self.assertIn("TPM", error)
        self.assertEqual(rl.get_window_snapshot(1)[:2], (2, 110))

    def test_duplicate_and_missing_terminal_attribution_are_no_ops(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=100))

        self.assertTrue(rl.attribute_request_tokens("request-a", 1, 25))
        self.assertFalse(rl.attribute_request_tokens("request-a", 1, 25))
        self.assertFalse(rl.attribute_request_tokens("missing", 1, 25))

        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 25))

    def test_duplicate_admission_does_not_record_a_second_request(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)

        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=1, tpm_limit=None))
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=1, tpm_limit=None))

        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 0))

    def test_terminal_duplicate_admission_returns_explicit_sentinel(self):
        rl = RateLimiter()
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=None))
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=None))
        self.assertTrue(rl.attribute_request_tokens("request-a", 1, 0))

        self.assertEqual(
            rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=None),
            REQUEST_ALREADY_TERMINAL,
        )
        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 0))

    def test_rejected_admission_does_not_mutate_window(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=1, tpm_limit=None))

        error = rl.admit_request("request-b", 1, rpm_limit=1, tpm_limit=None)

        self.assertIsNotNone(error)
        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 0))

    def test_terminal_after_admission_window_expiry_is_attributed_at_terminal_time(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=1, tpm_limit=100))
        clock.tick(61.0)

        self.assertTrue(rl.attribute_request_tokens("request-a", 1, 25))

        self.assertEqual(rl.get_window_snapshot(1)[:2], (0, 25))
        self.assertFalse(rl.attribute_request_tokens("request-a", 1, 25))

    def test_invalid_terminal_tokens_do_not_terminalize_request(self):
        invalid_values = (True, -1, 1.0, "1")
        for index, invalid in enumerate(invalid_values):
            with self.subTest(invalid=invalid):
                rl = RateLimiter()
                request_id = f"request-{index}"
                self.assertIsNone(
                    rl.admit_request(request_id, 1, rpm_limit=None, tpm_limit=None)
                )

                with self.assertRaises(AccountingValidationError):
                    rl.attribute_request_tokens(request_id, 1, invalid)

                self.assertTrue(rl.attribute_request_tokens(request_id, 1, 5))
                self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 5))

    def test_legacy_add_tokens_uses_latest_legacy_event(self):
        clock = _FakeClock()
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        rl.record(1)
        clock.tick(1.0)
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=None))

        self.assertIsNone(rl.add_tokens(1, 30))
        clock.tick(60.0)

        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 0))

    def test_concurrent_duplicate_terminal_is_attributed_once(self):
        rl = RateLimiter()
        self.assertIsNone(rl.admit_request("request-a", 1, rpm_limit=None, tpm_limit=None))
        barrier = threading.Barrier(3)
        results: list[bool | None] = [None, None]

        def attribute(index: int) -> None:
            barrier.wait()
            results[index] = rl.attribute_request_tokens("request-a", 1, 25)

        threads = [threading.Thread(target=attribute, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, [True, False])
        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 25))

    def test_concurrent_timestamp_reads_keep_events_chronological_for_purge(self):
        clock = _SequenceClock(0.0, 1.0, 60.5)
        rl = RateLimiter(window_seconds=60.0, time_func=clock)
        gate = _DelayedThreadLock("delayed-record")
        rl._lock = gate
        delayed = threading.Thread(target=rl.record, args=(1,), name="delayed-record")
        immediate = threading.Thread(target=rl.record, args=(1,), name="immediate-record")

        delayed.start()
        self.assertTrue(gate.delayed_ready.wait(timeout=1.0))
        immediate.start()
        immediate.join(timeout=1.0)
        gate.release_delayed.set()
        delayed.join(timeout=1.0)

        self.assertFalse(immediate.is_alive())
        self.assertFalse(delayed.is_alive())
        self.assertEqual(rl.get_window_snapshot(1)[:2], (1, 0))


if __name__ == "__main__":
    unittest.main()
