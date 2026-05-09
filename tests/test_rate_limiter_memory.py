"""Memory-regression tests for the process-local rate limiter."""

from __future__ import annotations

from llm_gateway_core.services.rate_limiter import RateLimiter


class _FakeClock:
    def __init__(self, start: float = 0.0) -> None:
        self._time = start

    def __call__(self) -> float:
        return self._time

    def tick(self, delta: float) -> None:
        self._time += delta


def test_purge_removes_empty_key_windows_after_events_expire():
    clock = _FakeClock()
    limiter = RateLimiter(window_seconds=60.0, time_func=clock)

    for key_id in range(1000):
        limiter.record(key_id)

    assert len(limiter._by_key) == 1000

    clock.tick(61.0)
    for key_id in range(1000):
        assert limiter.check(key_id, rpm_limit=1, tpm_limit=None) is None

    assert len(limiter._by_key) == 0
