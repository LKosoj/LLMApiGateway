"""In-memory per-IP brute-force guard.

Counts *consecutive* failed authentications per client IP and temporarily
blocks an IP once it crosses a threshold (e.g. 5 failures in a row -> block for
20 minutes). A successful authentication from the same IP clears its counter.

State is process-local, mirroring :class:`~llm_gateway_core.services.rate_limiter.RateLimiter`:
fine for the expected single-process deployment and intentionally simple — a
restart clears all counters and blocks, which is acceptable for a short block
window.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass


@dataclass
class _IpState:
    # Consecutive auth failures not yet escalated to a block.
    failures: int = 0
    # Monotonic deadline until which the IP stays blocked (0.0 = not blocked).
    blocked_until: float = 0.0


class IpBlockGuard:
    """Temporarily block client IPs after repeated failed authentications.

    Thread-safe (a single lock guards all state) so it can be shared across the
    middleware running under multiple worker threads, just like ``RateLimiter``.
    """

    def __init__(
        self,
        *,
        max_failures: int = 5,
        block_seconds: float = 1200.0,
        time_func=time.monotonic,
    ) -> None:
        if max_failures <= 0:
            raise ValueError("max_failures must be greater than 0")
        if block_seconds <= 0:
            raise ValueError("block_seconds must be greater than 0")
        self.max_failures = max_failures
        self.block_seconds = block_seconds
        self._time = time_func
        self._by_ip: dict[str, _IpState] = {}
        self._lock = threading.Lock()

    def check_blocked(self, ip: str) -> int | None:
        """Return remaining block seconds (>= 1) when *ip* is blocked, else ``None``.

        Expired blocks are cleared so the IP gets a clean slate after the window.
        Must be called *before* authenticating so the caller can fail fast with
        HTTP 429 without leaking timing about valid keys.
        """
        if not ip:
            return None
        now = self._time()
        with self._lock:
            state = self._by_ip.get(ip)
            if state is None:
                return None
            if state.blocked_until > now:
                return max(1, int(round(state.blocked_until - now)))
            # The block (if any) has expired — drop the entry for a clean slate.
            if state.blocked_until:
                self._by_ip.pop(ip, None)
            return None

    def register_failure(self, ip: str) -> int | None:
        """Record one failed authentication for *ip*.

        Returns the block duration in seconds when this failure *triggered* a
        new block (so the caller can log/audit it once), otherwise ``None``.
        """
        if not ip:
            return None
        now = self._time()
        with self._lock:
            state = self._by_ip.get(ip)
            if state is None:
                state = _IpState()
                self._by_ip[ip] = state
            if state.blocked_until > now:
                # Already blocked: stay blocked, do not re-trigger.
                return None
            if state.blocked_until:
                # A previous block expired — start counting fresh.
                state.blocked_until = 0.0
            state.failures += 1
            if state.failures >= self.max_failures:
                state.blocked_until = now + self.block_seconds
                state.failures = 0
                return int(self.block_seconds)
            return None

    def register_success(self, ip: str) -> None:
        """Clear the consecutive-failure counter for *ip* after a valid auth.

        An active block is left untouched: blocked requests are rejected before
        authentication, so a success here would be unexpected and must not lift
        an in-progress block.
        """
        if not ip:
            return
        now = self._time()
        with self._lock:
            state = self._by_ip.get(ip)
            if state is None:
                return
            if state.blocked_until > now:
                return
            self._by_ip.pop(ip, None)

    def reset(self, ip: str | None = None) -> None:
        """Clear state for one IP, or all IPs when *ip* is ``None``."""
        with self._lock:
            if ip is None:
                self._by_ip.clear()
            else:
                self._by_ip.pop(ip, None)
