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


def test_abandoned_in_flight_requests_expire_after_ttl_without_breaking_long_requests():
    clock = _FakeClock()
    limiter = RateLimiter(
        window_seconds=60.0,
        time_func=clock,
        in_flight_ttl_seconds=3600.0,
    )

    assert limiter.admit_request("req-long", 1, rpm_limit=10, tpm_limit=1000) is None
    assert limiter.admit_request("req-abandoned", 2, rpm_limit=10, tpm_limit=1000) is None

    # Past the 60s sliding window, but still within the 3600s in-flight TTL:
    # long requests must still be attributable.
    clock.tick(120.0)
    assert limiter.attribute_request_tokens("req-long", 1, 250) is True
    assert 2 in limiter._by_key
    assert "req-abandoned" in limiter._by_key[2].request_events

    # Past the in-flight TTL, abandoned entries and their empty key windows are purged.
    clock.tick(3601.0)
    assert limiter.get_window_snapshot(1) == (0, 0, None)
    assert limiter.get_window_snapshot(2) == (0, 0, None)
    assert len(limiter._by_key) == 0
