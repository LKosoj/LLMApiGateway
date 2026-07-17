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
from dataclasses import dataclass, field

from .accounting import AccountingValidationError


WINDOW_SECONDS = 60.0
REQUEST_ALREADY_TERMINAL = "request already completed"


@dataclass
class _RateEvent:
    timestamp: float
    tokens: int
    rpm: int
    request_id: str | None = None
    legacy: bool = False


@dataclass
class _KeyWindow:
    events: deque[_RateEvent]
    rpm_count: int = 0
    tpm_tokens: int = 0
    request_events: dict[str, _RateEvent | None] = field(default_factory=dict)


class RateLimiter:
    def __init__(self, window_seconds: float = WINDOW_SECONDS, time_func=time.monotonic) -> None:
        self._window = window_seconds
        self._time = time_func
        self._by_key: dict[int, _KeyWindow] = {}
        self._lock = threading.Lock()

    def _purge(self, key_id: int, state: _KeyWindow, now: float) -> bool:
        cutoff = now - self._window
        while state.events and state.events[0].timestamp < cutoff:
            event = state.events.popleft()
            state.rpm_count -= event.rpm
            state.tpm_tokens -= event.tokens
            if (
                event.request_id is not None
                and state.request_events.get(event.request_id) is event
            ):
                state.request_events.pop(event.request_id, None)
        if not state.events and not state.request_events:
            self._by_key.pop(key_id, None)
        return not state.events

    def check(self, key_id: int, *, rpm_limit: int | None, tpm_limit: int | None) -> str | None:
        """Return an error message when the limit is exceeded; else ``None``.

        Must be called *before* dispatching a request so the caller can fail
        fast with HTTP 429.
        """
        if rpm_limit is None and tpm_limit is None:
            return None
        with self._lock:
            now = self._time()
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

        When ``tokens`` is zero (the typical case at request start, before the
        upstream response is known), the TPM check is intentionally skipped —
        the caller is expected to attribute the real token count retroactively
        via :meth:`add_tokens` once it becomes available. This avoids a false
        sense of enforcement: checking TPM against zero would let a burst of
        concurrent requests through regardless of the real token volume.
        """
        if key_id is None:
            return None
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None:
                state = _KeyWindow(events=deque())
                self._by_key[key_id] = state
            if self._purge(key_id, state, now):
                self._by_key[key_id] = state
            if rpm_limit is not None and rpm_limit > 0 and state.rpm_count >= rpm_limit:
                return f"RPM limit of {rpm_limit} requests/min exceeded"
            # Only enforce TPM when the caller already knows the real token
            # count.  With tokens=0 the check would be a no-op, so skip it
            # to make the deferred-enforcement path explicit.
            if tokens and tpm_limit is not None and tpm_limit > 0 and state.tpm_tokens >= tpm_limit:
                return f"TPM limit of {tpm_limit} tokens/min exceeded"
            state.events.append(
                _RateEvent(timestamp=now, tokens=int(tokens or 0), rpm=1, legacy=True)
            )
            state.rpm_count += 1
            state.tpm_tokens += int(tokens or 0)
        return None

    def admit_request(
        self,
        request_id: str,
        key_id: int,
        *,
        rpm_limit: int | None,
        tpm_limit: int | None,
    ) -> str | None:
        """Atomically admit and record one request identifier."""
        if key_id is None:
            return None
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None:
                state = _KeyWindow(events=deque())
            else:
                self._purge(key_id, state, now)

            if request_id in state.request_events:
                if state.request_events[request_id] is None:
                    return None
                return REQUEST_ALREADY_TERMINAL
            if rpm_limit is not None and rpm_limit > 0 and state.rpm_count >= rpm_limit:
                return f"RPM limit of {rpm_limit} requests/min exceeded"
            if tpm_limit is not None and tpm_limit > 0 and state.tpm_tokens >= tpm_limit:
                return f"TPM limit of {tpm_limit} tokens/min exceeded"

            event = _RateEvent(timestamp=now, tokens=0, rpm=1)
            state.events.append(event)
            state.request_events[request_id] = None
            state.rpm_count += 1
            self._by_key[key_id] = state
        return None

    def attribute_request_tokens(self, request_id: str, key_id: int, tokens: int) -> bool:
        """Attribute terminal tokens once without rechecking the TPM limit."""
        if key_id is None:
            return False
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None:
                return False
            self._purge(key_id, state, now)
            if request_id not in state.request_events:
                return False
            if state.request_events[request_id] is not None:
                return False
            if isinstance(tokens, bool) or not isinstance(tokens, int) or tokens < 0:
                raise AccountingValidationError from None

            event = _RateEvent(
                timestamp=now,
                tokens=tokens,
                rpm=0,
                request_id=request_id,
            )
            state.events.append(event)
            state.request_events[request_id] = event
            state.tpm_tokens += tokens
            self._by_key[key_id] = state
            return True

    def record(self, key_id: int, tokens: int = 0) -> None:
        """Record a request (and optionally its token count) for *key_id*.

        ``tokens`` can be updated retroactively via :meth:`add_tokens` once
        the upstream usage block is known.
        """
        if key_id is None:
            return
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None:
                state = _KeyWindow(events=deque())
                self._by_key[key_id] = state
            if self._purge(key_id, state, now):
                self._by_key[key_id] = state
            state.events.append(
                _RateEvent(timestamp=now, tokens=int(tokens or 0), rpm=1, legacy=True)
            )
            state.rpm_count += 1
            state.tpm_tokens += int(tokens or 0)

    def add_tokens(self, key_id: int, tokens: int, *, tpm_limit: int | None = None) -> str | None:
        """Retroactively add tokens to the most-recent event for *key_id*.

        When *tpm_limit* is provided the new total is checked against it first.
        On limit violation the tokens are **not** added and an error message is
        returned; otherwise ``None`` is returned on success.
        """
        if key_id is None or not tokens:
            return None
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None or not state.events:
                return None
            if self._purge(key_id, state, now):
                return None
            event = next((event for event in reversed(state.events) if event.legacy), None)
            if event is None:
                return None
            if tpm_limit is not None and tpm_limit > 0 and state.tpm_tokens + int(tokens) > tpm_limit:
                return f"TPM limit of {tpm_limit} tokens/min exceeded"
            event.tokens += int(tokens)
            state.tpm_tokens += int(tokens)
        return None

    def get_window_snapshot(self, key_id: int) -> tuple[int, int, float | None]:
        """Return (rpm_count, tpm_tokens, oldest_event_age_seconds) for *key_id*.

        Purges stale events first so the counts reflect the live sliding window.
        ``oldest_event_age_seconds`` is how many seconds ago the oldest event in
        the current window occurred; callers use it to compute
        ``reset_in_seconds = window - oldest_event_age``.  Returns (0, 0, None)
        when *key_id* has no active window (None explicitly signals "empty window").
        """
        with self._lock:
            now = self._time()
            state = self._by_key.get(key_id)
            if state is None:
                return 0, 0, None
            if self._purge(key_id, state, now):
                return 0, 0, None
            oldest_age = now - state.events[0].timestamp if state.events else None
            return state.rpm_count, state.tpm_tokens, oldest_age

    def reset(self, key_id: int | None = None) -> None:
        with self._lock:
            if key_id is None:
                self._by_key.clear()
            else:
                self._by_key.pop(key_id, None)
