#!/usr/bin/env python
"""Bounded readiness gate for the systemd service."""

from __future__ import annotations

import contextlib
import io
import math
import sys
import time
from collections.abc import Callable

from healthcheck import check_health


READINESS_TIMEOUT_SECONDS = 60.0
RETRY_INTERVAL_SECONDS = 1.0


def wait_for_readiness(
    *,
    check: Callable[[], bool] = check_health,
    timeout_seconds: float = READINESS_TIMEOUT_SECONDS,
    retry_interval_seconds: float = RETRY_INTERVAL_SECONDS,
    monotonic: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
) -> bool:
    """Retry the single-attempt health check until ready or the deadline passes."""
    if not math.isfinite(timeout_seconds) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be finite and positive")
    if not math.isfinite(retry_interval_seconds) or retry_interval_seconds <= 0:
        raise ValueError("retry_interval_seconds must be finite and positive")

    deadline = monotonic() + timeout_seconds
    while monotonic() < deadline:
        captured_output = io.StringIO()
        with (
            contextlib.redirect_stdout(captured_output),
            contextlib.redirect_stderr(captured_output),
        ):
            ready = check()
        if ready:
            return True

        remaining = deadline - monotonic()
        if remaining <= 0:
            return False
        sleep(min(retry_interval_seconds, remaining))

    return False


def main() -> int:
    """Run the readiness gate with deterministic, non-sensitive output."""
    try:
        ready = wait_for_readiness()
    except Exception:
        print("systemd-readiness: failed reason=check_error", file=sys.stderr)
        return 1

    if not ready:
        print("systemd-readiness: failed reason=deadline_exceeded", file=sys.stderr)
        return 1

    print("systemd-readiness: ready")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
