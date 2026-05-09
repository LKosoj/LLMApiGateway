"""In-memory sliding-window rate limiter for virtual API keys.

Tracks requests-per-minute (RPM) and tokens-per-minute (TPM) over a rolling
60-second window, per key id. State is process-local: fine for the expected
deployment shape (single gateway process) and intentionally simple — if the
process restarts, the window starts empty.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass


WINDOW_SECONDS = 60.0


@dataclass
class _KeyWindow:
    # (timestamp, tokens) entries, one per recorded event
    events: deque
    rpm_count: int = 0
    tpm_tokens: int = 0


class RateLimiter:
    def __init__(self, window_seconds: float = WINDOW_SECONDS, time_func=time.monotonic) -> None:
        self._window = window_seconds
        self._time = time_func
        self._by_key: dict[int, _KeyWindow] = {}
        self._lock = threading.Lock()

    def _purge(self, key_id: int, state: _KeyWindow, now: float) -> bool:
        cutoff = now - self._window
        while state.events and state.events[0][0] < cutoff:
            _ts, tokens = state.events.popleft()
            state.rpm_count -= 1
            state.tpm_tokens -= tokens
        if not state.events:
            self._by_key.pop(key_id, None)
            return True
        return False

    def check(self, key_id: int, *, rpm_limit: int | None, tpm_limit: int | None) -> str | None:
        """Return an error message when the limit is exceeded; else ``None``.

        Must be called *before* dispatching a request so the caller can fail
        fast with HTTP 429.
        """
        if rpm_limit is None and tpm_limit is None:
            return None
        now = self._time()
        with self._lock:
            state = self._by_key.get(key_id)
            if state is None:
                return None
            if self._purge(key_id, state, now):
                return None
            if rpm_limit is not None and rpm_limit > 0 and state.rpm_count >= rpm_limit:
                return f"RPM limit of {rpm_limit} requests/min exceeded"
            if tpm_limit is not None and tpm_limit > 0 and state.tpm_tokens >= tpm_limit:
                return f"TPM limit of {tpm_limit} tokens/min exceeded"
        return None

    def try_acquire(
        self,
        key_id: int,
        *,
        rpm_limit: int | None,
        tpm_limit: int | None,
        tokens: int = 0,
    ) -> str | None:
        """Atomic check-and-record under a single lock.

        Returns an error message when the limit would be exceeded (without
        recording), else records the event and returns ``None``. This closes
        the TOCTOU gap between :meth:`check` and :meth:`record` when concurrent
        requests race on the same ``key_id``.
        """
        if key_id is None:
            return None
        now = self._time()
        with self._lock:
            state = self._by_key.get(key_id)
            if state is None:
                state = _KeyWindow(events=deque())
                self._by_key[key_id] = state
            if self._purge(key_id, state, now):
                self._by_key[key_id] = state
            if rpm_limit is not None and rpm_limit > 0 and state.rpm_count >= rpm_limit:
                return f"RPM limit of {rpm_limit} requests/min exceeded"
            if tpm_limit is not None and tpm_limit > 0 and state.tpm_tokens >= tpm_limit:
                return f"TPM limit of {tpm_limit} tokens/min exceeded"
            state.events.append((now, int(tokens or 0)))
            state.rpm_count += 1
            state.tpm_tokens += int(tokens or 0)
        return None

    def record(self, key_id: int, tokens: int = 0) -> None:
        """Record a request (and optionally its token count) for *key_id*.

        ``tokens`` can be updated retroactively via :meth:`add_tokens` once
        the upstream usage block is known.
        """
        if key_id is None:
            return
        now = self._time()
        with self._lock:
            state = self._by_key.get(key_id)
            if state is None:
                state = _KeyWindow(events=deque())
                self._by_key[key_id] = state
            if self._purge(key_id, state, now):
                self._by_key[key_id] = state
            state.events.append((now, int(tokens or 0)))
            state.rpm_count += 1
            state.tpm_tokens += int(tokens or 0)

    def add_tokens(self, key_id: int, tokens: int) -> None:
        """Retroactively add tokens to the most-recent event for *key_id*."""
        if key_id is None or not tokens:
            return
        now = self._time()
        with self._lock:
            state = self._by_key.get(key_id)
            if state is None or not state.events:
                return
            if self._purge(key_id, state, now):
                return
            ts, prev_tokens = state.events[-1]
            state.events[-1] = (ts, prev_tokens + int(tokens))
            state.tpm_tokens += int(tokens)

    def reset(self, key_id: int | None = None) -> None:
        with self._lock:
            if key_id is None:
                self._by_key.clear()
            else:
                self._by_key.pop(key_id, None)
