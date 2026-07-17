"""Process-wide FIFO weighted admission for upload processing."""

from __future__ import annotations

import asyncio
import math
from collections import deque
from dataclasses import dataclass


class UploadAdmissionError(RuntimeError):
    """Base class for credential-safe upload admission failures."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"upload admission failed: reason={reason_code}")


class UploadAdmissionTooLarge(UploadAdmissionError):
    def __init__(self, weight: int, max_bytes: int) -> None:
        self.weight = weight
        self.max_bytes = max_bytes
        super().__init__("weight_too_large")


class UploadAdmissionTimeout(UploadAdmissionError):
    def __init__(self) -> None:
        super().__init__("timeout")


class UploadAdmissionClosed(UploadAdmissionError):
    def __init__(self) -> None:
        super().__init__("closed")


@dataclass(frozen=True, slots=True)
class UploadAdmissionSnapshot:
    max_bytes: int
    active_bytes: int
    active_leases: int
    waiters: int
    high_water_bytes: int
    failures: int
    last_reason_code: str | None
    closed: bool


@dataclass(slots=True)
class _Waiter:
    weight: int
    future: asyncio.Future[UploadAdmissionLease]


class UploadAdmissionLease:
    """Idempotent ownership token for one admitted byte weight."""

    __slots__ = ("_admission", "_released", "weight")

    def __init__(self, admission: UploadAdmission, weight: int) -> None:
        self._admission = admission
        self._released = False
        self.weight = weight

    @property
    def released(self) -> bool:
        return self._released

    def release(self) -> None:
        if self._released:
            return
        self._released = True
        self._admission._release(self.weight)

    def __enter__(self) -> UploadAdmissionLease:
        return self

    def __exit__(self, *_args: object) -> None:
        self.release()


class UploadAdmission:
    """Event-loop-affine strict-FIFO byte capacity."""

    __slots__ = (
        "_active_bytes",
        "_active_leases",
        "_closed",
        "_failures",
        "_high_water_bytes",
        "_last_reason_code",
        "_loop",
        "_max_bytes",
        "_waiters",
    )

    def __init__(self, *, max_bytes: int) -> None:
        self._max_bytes = _positive_int(max_bytes, name="max_bytes")
        self._active_bytes = 0
        self._active_leases = 0
        self._high_water_bytes = 0
        self._failures = 0
        self._last_reason_code: str | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: deque[_Waiter] = deque()

    @property
    def snapshot(self) -> UploadAdmissionSnapshot:
        return UploadAdmissionSnapshot(
            max_bytes=self._max_bytes,
            active_bytes=self._active_bytes,
            active_leases=self._active_leases,
            waiters=len(self._waiters),
            high_water_bytes=self._high_water_bytes,
            failures=self._failures,
            last_reason_code=self._last_reason_code,
            closed=self._closed,
        )

    async def acquire(
        self,
        weight: int,
        *,
        timeout_seconds: float | None = None,
    ) -> UploadAdmissionLease:
        weight = _positive_int(weight, name="weight")
        timeout = _positive_timeout(timeout_seconds)
        loop = self._require_loop()
        if weight > self._max_bytes:
            self._record_failure("weight_too_large")
            raise UploadAdmissionTooLarge(weight, self._max_bytes)
        if self._closed:
            self._record_failure("closed")
            raise UploadAdmissionClosed
        if not self._waiters and self._fits(weight):
            return self._grant(weight)

        waiter = _Waiter(weight=weight, future=loop.create_future())
        self._waiters.append(waiter)
        try:
            if timeout is None:
                return await asyncio.shield(waiter.future)
            async with asyncio.timeout(timeout):
                return await asyncio.shield(waiter.future)
        except TimeoutError:
            self._withdraw(waiter)
            self._record_failure("timeout")
            raise UploadAdmissionTimeout from None
        except asyncio.CancelledError:
            self._withdraw(waiter)
            raise

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        while self._waiters:
            waiter = self._waiters.popleft()
            if not waiter.future.done():
                waiter.future.set_exception(UploadAdmissionClosed())

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("upload admission belongs to another event loop")
        return loop

    def _fits(self, weight: int) -> bool:
        return self._active_bytes + weight <= self._max_bytes

    def _grant(self, weight: int) -> UploadAdmissionLease:
        self._active_bytes += weight
        self._active_leases += 1
        self._high_water_bytes = max(self._high_water_bytes, self._active_bytes)
        return UploadAdmissionLease(self, weight)

    def _release(self, weight: int) -> None:
        if weight > self._active_bytes or self._active_leases <= 0:
            raise RuntimeError("upload admission lease state is inconsistent")
        self._active_bytes -= weight
        self._active_leases -= 1
        self._drain()

    def _drain(self) -> None:
        while self._waiters and not self._closed:
            waiter = self._waiters[0]
            if not self._fits(waiter.weight):
                return
            self._waiters.popleft()
            if waiter.future.done():
                continue
            waiter.future.set_result(self._grant(waiter.weight))

    def _withdraw(self, waiter: _Waiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            if waiter.future.done() and not waiter.future.cancelled():
                try:
                    lease = waiter.future.result()
                except UploadAdmissionError:
                    return
                lease.release()
            return
        waiter.future.cancel()
        self._drain()

    def _record_failure(self, reason_code: str) -> None:
        self._failures += 1
        self._last_reason_code = reason_code


def _positive_int(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


def _positive_timeout(value: float | None) -> float | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError("timeout_seconds must be a positive finite number")
    timeout = float(value)
    if not math.isfinite(timeout) or timeout <= 0:
        raise ValueError("timeout_seconds must be a positive finite number")
    return timeout
