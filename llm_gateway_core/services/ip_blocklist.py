"""Per-IP brute-force guard with persistent bans for repeat offenders.

Counts *consecutive* failed authentications per client IP and temporarily
blocks an IP once it crosses a threshold (e.g. 5 failures in a row -> block for
20 minutes). A successful authentication from the same IP clears its counter.

Temporary counters are process-local. Permanent blocks are loaded from the
rejections database and remain until an administrator releases them.
"""

from __future__ import annotations

import threading
import time
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..db.rejections_db import RejectionsDB

logger = logging.getLogger(__name__)
PERMANENT_BLOCK = -1


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
        rejections_db: RejectionsDB | None = None,
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
        self._db = rejections_db
        self._permanent = {
            item["client_ip"]: item
            for item in (rejections_db.list_permanent_ip_blocks() if rejections_db is not None else [])
        }

    def check_blocked(self, ip: str) -> int | None:
        """Return seconds, PERMANENT_BLOCK for a permanent ban, or None.

        Expired blocks are cleared so the IP gets a clean slate after the window.
        Must be called *before* authenticating so the caller can fail fast with
        HTTP 429 without leaking timing about valid keys.
        """
        if not ip:
            return None
        now = self._time()
        with self._lock:
            if ip in self._permanent:
                return PERMANENT_BLOCK
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

    def scan_permanent_blocks(self) -> None:
        if self._db is None:
            return
        with self._lock:
            items = self._db.promote_repeat_ip_blocks()
            for item in items:
                if item["client_ip"] not in self._permanent:
                    logger.warning("IP %s permanently blocked: %s", item["client_ip"], item["reason"])
            self._permanent = {item["client_ip"]: item for item in items}

    def list_permanent_blocks(self) -> list[dict]:
        with self._lock:
            return [dict(item) for item in self._permanent.values()]

    def unblock_permanent_ip(self, ip: str) -> bool:
        if self._db is None:
            return False
        with self._lock:
            if not self._db.release_permanent_ip_block(ip):
                return False
            self._permanent.pop(ip, None)
            self._by_ip.pop(ip, None)
            logger.info("Permanent IP block released by administrator: %s", ip)
            return True
