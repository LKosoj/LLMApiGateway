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

    def __init__(self, ttl_seconds: float, max_entries: int | None = None) -> None:
        if max_entries is not None and (
            type(max_entries) is not int or max_entries <= 0
        ):
            raise ValueError("max_entries must be a positive integer or None.")
        self._ttl = float(ttl_seconds)
        self._max_entries = max_entries
        self._entries: dict[Any, tuple[float, Any]] = {}

    def _prune_expired(self, now: float) -> None:
        expired = [
            key for key, (expires_at, _value) in self._entries.items() if expires_at <= now
        ]
        for key in expired:
            self._entries.pop(key, None)

    def _enforce_bound(self) -> None:
        if self._max_entries is None:
            return
        while len(self._entries) > self._max_entries:
            oldest_expiry = min(expires_at for expires_at, _value in self._entries.values())
            oldest_key = next(
                key
                for key, (expires_at, _value) in self._entries.items()
                if expires_at == oldest_expiry
            )
            self._entries.pop(oldest_key)

    async def get_or_compute(
        self,
        key: Any,
        producer: Callable[[], Awaitable[Any]],
    ) -> Any:
        now = time.monotonic()
        self._prune_expired(now)
        cached = self._entries.get(key)
        if cached is not None:
            _expires_at, value = cached
            return value

        value = await producer()
        now = time.monotonic()
        self._prune_expired(now)
        self._entries[key] = (now + self._ttl, value)
        self._enforce_bound()
        return value

    def invalidate(self, key: Any | None = None) -> None:
        if key is None:
            self._entries.clear()
        else:
            self._entries.pop(key, None)
