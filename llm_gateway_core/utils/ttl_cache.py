"""A minimal TTL cache for async stats endpoints.

The aggregated-usage queries group-by-scan the whole ``tokens_usage`` table
(or a time-windowed slice of it). On dashboards that auto-refresh every few
seconds, each tab triggers a full aggregation, and SQLite starts to cost
real CPU. Caching the result for a few seconds is the right tradeoff: the
UI already shows stale data between polls, and an extra 5-30s of staleness
is imperceptible.

Intentionally simple — no locks around the coroutine execution (concurrent
callers on the same cold key will each run the underlying coroutine once;
the last writer wins). This keeps the hot path branch-free and avoids the
dogpile semantics that tempt complexity here.
"""
from __future__ import annotations

import time
from typing import Any, Awaitable, Callable


class AsyncTtlCache:
    """Async-aware, monotonic-clock-based TTL cache keyed by arbitrary keys."""

    def __init__(self, ttl_seconds: float) -> None:
        self._ttl = float(ttl_seconds)
        self._entries: dict[Any, tuple[float, Any]] = {}

    async def get_or_compute(
        self,
        key: Any,
        producer: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()
        cached = self._entries.get(key)
        if cached is not None:
            expires_at, value = cached
            if expires_at > now:
                return value

        value = await producer()
        self._entries[key] = (now + self._ttl, value)
        return value

    def invalidate(self, key: Any | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)
