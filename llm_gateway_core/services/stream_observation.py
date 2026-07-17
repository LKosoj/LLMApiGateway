"""Bounded stream observation capacity and canonical SSE framing."""

from __future__ import annotations

import asyncio
import json
import re
from collections import deque
from dataclasses import dataclass


STREAM_OBSERVATION_BUFFER_MAX_ITEMS = 32
STREAM_OBSERVATION_BUFFER_MAX_BYTES = 33_554_432
STREAM_EVENT_MAX_BYTES = 1_048_576

_SAFE_REASON_RE = re.compile(r"[a-z0-9_.-]{1,64}\Z")


class StreamObservationError(RuntimeError):
    """Base error for bounded stream observation state."""


class StreamObservationTooLarge(StreamObservationError):
    """One reservation cannot fit within the process-wide byte capacity."""

    def __init__(self, observed_bytes: int, max_bytes: int) -> None:
        self.reason_code = "acquire_too_large"
        self.observed_bytes = observed_bytes
        self.max_bytes = max_bytes
        super().__init__(
            "stream observation reservation is too large: "
            f"observed_bytes={observed_bytes}, max_bytes={max_bytes}"
        )


class StreamObservationCapacityExhausted(StreamObservationError):
    """A fail-fast reservation cannot fit in the currently available capacity."""

    def __init__(
        self,
        requested_bytes: int,
        *,
        active_items: int,
        active_bytes: int,
        max_items: int,
        max_bytes: int,
        waiters: int,
    ) -> None:
        self.reason_code = "capacity_exhausted"
        self.requested_bytes = requested_bytes
        self.active_items = active_items
        self.active_bytes = active_bytes
        self.max_items = max_items
        self.max_bytes = max_bytes
        self.waiters = waiters
        super().__init__(
            "stream observation capacity is exhausted: "
            f"requested_bytes={requested_bytes}, active_items={active_items}, "
            f"active_bytes={active_bytes}, max_items={max_items}, "
            f"max_bytes={max_bytes}, waiters={waiters}"
        )


class StreamObservationStateError(StreamObservationError):
    """The requested capacity or lease operation is invalid in its current state."""

    def __init__(self, reason: str) -> None:
        self.reason_code = reason
        super().__init__(f"stream observation state error: reason={reason}")


@dataclass(frozen=True, slots=True)
class StreamObservationSnapshot:
    """Credential-safe process-wide capacity counters."""

    max_items: int
    max_bytes: int
    active_items: int
    active_bytes: int
    waiters: int
    high_water_items: int
    high_water_bytes: int
    failures: int
    last_reason_code: str | None


@dataclass(slots=True)
class _CapacityWaiter:
    byte_count: int
    future: asyncio.Future[StreamObservationLease]


class StreamObservationLease:
    """Exact ownership token for one item and its retained bytes."""

    __slots__ = ("_capacity", "_owns_item", "_remaining_bytes")

    def __init__(
        self,
        capacity: StreamObservationCapacity,
        byte_count: int,
    ) -> None:
        self._capacity = capacity
        self._owns_item = True
        self._remaining_bytes = byte_count

    @property
    def remaining_bytes(self) -> int:
        return self._remaining_bytes

    @property
    def released(self) -> bool:
        return self._remaining_bytes == 0 and not self._owns_item

    def release_item(self) -> None:
        """Release this lease's queue/admission slot while retaining its bytes."""
        if not self._owns_item:
            return
        self._capacity._release_item()
        self._owns_item = False

    def absorb(self, other: StreamObservationLease) -> None:
        """Transfer ``other`` bytes into this lease without double-counting."""
        if other is self:
            raise self._capacity._state_error("lease_self_absorb")
        if other._capacity is not self._capacity:
            raise self._capacity._state_error("lease_capacity_mismatch")
        if other.released:
            raise self._capacity._state_error("lease_already_released")

        self.release_item()
        other.release_item()
        self._remaining_bytes += other._remaining_bytes
        other._remaining_bytes = 0

    def consume(self, byte_count: int) -> None:
        """Release exactly ``byte_count`` bytes from this lease."""
        _validate_positive_int(byte_count, name="byte_count")
        if byte_count > self._remaining_bytes:
            raise self._capacity._state_error("lease_bytes_exceeded")

        releases_item = byte_count == self._remaining_bytes and self._owns_item
        self._capacity._release_bytes(byte_count, releases_item=releases_item)
        self._remaining_bytes -= byte_count
        if releases_item:
            self._owns_item = False

    def release_all(self) -> None:
        """Idempotently release every byte still owned by this lease."""
        if self.released:
            return

        byte_count = self._remaining_bytes
        if byte_count:
            self._capacity._release_bytes(
                byte_count,
                releases_item=self._owns_item,
            )
        elif self._owns_item:
            self._capacity._release_item()
        self._remaining_bytes = 0
        self._owns_item = False


class StreamObservationCapacity:
    """Event-loop-affine FIFO admission for retained stream bytes."""

    __slots__ = (
        "_active_bytes",
        "_active_items",
        "_capacity_bytes",
        "_capacity_items",
        "_closed",
        "_failures",
        "_high_water_bytes",
        "_high_water_items",
        "_last_failure_reason",
        "_loop",
        "_waiting_bytes",
        "_waiters",
    )

    def __init__(
        self,
        *,
        max_items: int = STREAM_OBSERVATION_BUFFER_MAX_ITEMS,
        max_bytes: int = STREAM_OBSERVATION_BUFFER_MAX_BYTES,
    ) -> None:
        _validate_positive_int(max_items, name="max_items")
        _validate_positive_int(max_bytes, name="max_bytes")
        self._capacity_items = max_items
        self._capacity_bytes = max_bytes
        self._active_items = 0
        self._active_bytes = 0
        self._waiting_bytes = 0
        self._high_water_items = 0
        self._high_water_bytes = 0
        self._failures = 0
        self._last_failure_reason: str | None = None
        self._closed = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._waiters: deque[_CapacityWaiter] = deque()

    @property
    def snapshot(self) -> StreamObservationSnapshot:
        return StreamObservationSnapshot(
            max_items=self._capacity_items,
            max_bytes=self._capacity_bytes,
            active_items=self._active_items,
            active_bytes=self._active_bytes,
            waiters=len(self._waiters),
            high_water_items=self._high_water_items,
            high_water_bytes=self._high_water_bytes,
            failures=self._failures,
            last_reason_code=self._last_failure_reason,
        )

    async def acquire(self, byte_count: int) -> StreamObservationLease:
        """Wait in FIFO order until one item and ``byte_count`` bytes fit."""
        _validate_positive_int(byte_count, name="byte_count")
        loop = self._require_loop()
        if byte_count > self._capacity_bytes:
            self._record_failure("acquire_too_large")
            raise StreamObservationTooLarge(byte_count, self._capacity_bytes)
        if self._closed:
            raise self._state_error("capacity_closed")
        if not self._waiters and self._fits(byte_count):
            return self._grant(byte_count)

        future: asyncio.Future[StreamObservationLease] = loop.create_future()
        waiter = _CapacityWaiter(byte_count=byte_count, future=future)
        self._waiters.append(waiter)
        self._waiting_bytes += byte_count
        try:
            return await future
        except asyncio.CancelledError:
            self._cancel_waiter(waiter)
            raise

    def try_acquire(self, byte_count: int) -> StreamObservationLease:
        """Acquire immediately without bypassing queued FIFO waiters."""
        _validate_positive_int(byte_count, name="byte_count")
        self._require_loop()
        if byte_count > self._capacity_bytes:
            self._record_failure("acquire_too_large")
            raise StreamObservationTooLarge(byte_count, self._capacity_bytes)
        if self._closed:
            raise self._state_error("capacity_closed")
        if self._waiters or not self._fits(byte_count):
            self._record_failure("capacity_exhausted")
            raise StreamObservationCapacityExhausted(
                byte_count,
                active_items=self._active_items,
                active_bytes=self._active_bytes,
                max_items=self._capacity_items,
                max_bytes=self._capacity_bytes,
                waiters=len(self._waiters),
            )
        return self._grant(byte_count)

    def close(self, *, reason: str = "capacity_closed") -> None:
        """Close admission and wake every queued waiter with a safe state error."""
        if self._closed:
            return
        self._require_loop()
        safe_reason = _safe_reason(reason, default="capacity_closed")
        self._closed = True
        self._last_failure_reason = safe_reason
        while self._waiters:
            waiter = self._waiters.popleft()
            self._waiting_bytes -= waiter.byte_count
            if not waiter.future.done():
                waiter.future.set_exception(StreamObservationStateError(safe_reason))

    def record_failure(self, reason_code: str) -> None:
        """Record a credential-safe parser or decompression diagnostic code."""
        self._require_loop()
        self._record_failure(reason_code)

    def _require_loop(self) -> asyncio.AbstractEventLoop:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise self._state_error("event_loop_required") from exc
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise self._state_error("event_loop_mismatch")
        return loop

    def _fits(self, byte_count: int) -> bool:
        return (
            self._active_items < self._capacity_items
            and self._active_bytes + byte_count <= self._capacity_bytes
        )

    def _grant(self, byte_count: int) -> StreamObservationLease:
        self._active_items += 1
        self._active_bytes += byte_count
        self._high_water_items = max(self._high_water_items, self._active_items)
        self._high_water_bytes = max(self._high_water_bytes, self._active_bytes)
        return StreamObservationLease(self, byte_count)

    def _release_bytes(self, byte_count: int, *, releases_item: bool) -> None:
        self._require_loop()
        if byte_count > self._active_bytes or (releases_item and self._active_items == 0):
            raise self._state_error("capacity_counter_underflow")
        self._active_bytes -= byte_count
        if releases_item:
            self._active_items -= 1
        self._drain_waiters()

    def _release_item(self) -> None:
        self._require_loop()
        if self._active_items == 0:
            raise self._state_error("capacity_counter_underflow")
        self._active_items -= 1
        self._drain_waiters()

    def _drain_waiters(self) -> None:
        while self._waiters:
            waiter = self._waiters[0]
            if waiter.future.cancelled():
                self._waiters.popleft()
                self._waiting_bytes -= waiter.byte_count
                continue
            if waiter.future.done():
                self._waiters.popleft()
                self._waiting_bytes -= waiter.byte_count
                continue
            if self._closed or not self._fits(waiter.byte_count):
                return

            self._waiters.popleft()
            self._waiting_bytes -= waiter.byte_count
            waiter.future.set_result(self._grant(waiter.byte_count))

    def _cancel_waiter(self, waiter: _CapacityWaiter) -> None:
        try:
            self._waiters.remove(waiter)
        except ValueError:
            if waiter.future.done() and not waiter.future.cancelled():
                try:
                    lease = waiter.future.result()
                except StreamObservationError:
                    pass
                else:
                    lease.release_all()
        else:
            self._waiting_bytes -= waiter.byte_count
            waiter.future.cancel()
        self._drain_waiters()

    def _record_failure(self, reason: str) -> None:
        self._failures += 1
        self._last_failure_reason = _safe_reason(reason, default="state_error")

    def _state_error(self, reason: str) -> StreamObservationStateError:
        safe_reason = _safe_reason(reason, default="state_error")
        self._record_failure(safe_reason)
        return StreamObservationStateError(safe_reason)


class SSEFramingError(RuntimeError):
    """Base class for credential-safe SSE framing failures."""

    def __init__(self, reason: str, *, observed_bytes: int | None = None) -> None:
        self.reason_code = reason
        self.observed_bytes = observed_bytes
        detail = f", observed_bytes={observed_bytes}" if observed_bytes is not None else ""
        super().__init__(f"SSE framing failed: reason={reason}{detail}")


class SSEEventTooLarge(SSEFramingError):
    """The current raw SSE event exceeded its configured byte limit."""

    def __init__(self, observed_bytes: int, max_bytes: int) -> None:
        self.max_bytes = max_bytes
        super().__init__("event_too_large", observed_bytes=observed_bytes)


class SSEInvalidEncoding(SSEFramingError):
    """A complete SSE event is not valid UTF-8."""

    def __init__(self, observed_bytes: int) -> None:
        super().__init__("invalid_utf8", observed_bytes=observed_bytes)


class SSEMalformedEvent(SSEFramingError):
    """The SSE stream uses an unsupported line boundary."""

    def __init__(self, observed_bytes: int) -> None:
        super().__init__("malformed_line_ending", observed_bytes=observed_bytes)


class SSEFramerStateError(SSEFramingError):
    """The framer cannot accept the requested operation in its current state."""


@dataclass(frozen=True, slots=True)
class SSEEvent:
    data: str
    event: str | None = None
    done: bool = False


def parse_sse_json(event: SSEEvent) -> object:
    """Parse one SSE data field as strict JSON."""
    if not isinstance(event, SSEEvent):
        raise TypeError("event must be an SSEEvent")
    return json.loads(event.data, parse_constant=_reject_json_constant)


def _reject_json_constant(_value: str) -> object:
    raise ValueError("non-finite JSON constants are not supported")


@dataclass(frozen=True, slots=True)
class SSEFrameBatch:
    events: tuple[SSEEvent, ...]
    consumed_bytes: int
    trailing_frame_bytes: int = 0


class SSEFramer:
    """Incrementally frame UTF-8 Server-Sent Events from raw bytes."""

    __slots__ = (
        "_buffer",
        "_failed",
        "_finished",
        "_line_start",
        "_max_event_bytes",
        "_search_start",
        "_terminal",
        "_terminal_trailing_bytes",
    )

    def __init__(self, *, max_event_bytes: int = STREAM_EVENT_MAX_BYTES) -> None:
        _validate_positive_int(max_event_bytes, name="max_event_bytes")
        self._max_event_bytes = max_event_bytes
        self._buffer = bytearray()
        self._failed = False
        self._finished = False
        self._terminal = False
        self._terminal_trailing_bytes = 0
        self._line_start = 0
        self._search_start = 0

    @property
    def buffered_bytes(self) -> int:
        return len(self._buffer)

    @property
    def terminal(self) -> bool:
        return self._terminal

    @property
    def terminal_trailing_bytes(self) -> int:
        return self._terminal_trailing_bytes

    def feed(self, data: bytes) -> SSEFrameBatch:
        """Consume complete SSE events and retain at most one incomplete event."""
        if not isinstance(data, bytes):
            raise TypeError("data must be bytes")
        if self._failed:
            raise SSEFramerStateError("framer_failed")
        if self._terminal:
            self._terminal_trailing_bytes += len(data)
            return SSEFrameBatch(events=(), consumed_bytes=len(data))
        if self._finished:
            raise SSEFramerStateError("framer_finished")

        total_owned_bytes = len(self._buffer) + len(data)
        self._buffer.extend(data)
        events, trailing_frame_bytes = self._extract_complete_events()
        return SSEFrameBatch(
            events=tuple(events),
            consumed_bytes=total_owned_bytes - len(self._buffer),
            trailing_frame_bytes=trailing_frame_bytes,
        )

    def finish(self) -> SSEFrameBatch:
        """Flush one valid EOF-terminated event and make finish idempotent."""
        if self._failed:
            raise SSEFramerStateError("framer_failed")
        if self._terminal or self._finished:
            return SSEFrameBatch(events=(), consumed_bytes=0)

        consumed_bytes = len(self._buffer)
        events: list[SSEEvent] = []
        if self._buffer:
            self._validate_partial_line(at_eof=True)
            self._enforce_event_limit(len(self._buffer))
            event = self._parse_event(bytes(self._buffer))
            if event is not None:
                events.append(event)
                self._terminal = event.done
            self._buffer.clear()
        self._finished = True
        return SSEFrameBatch(events=tuple(events), consumed_bytes=consumed_bytes)

    def abort(self) -> int:
        """Idempotently discard and return the number of retained raw bytes."""
        retained_bytes = len(self._buffer)
        self._buffer.clear()
        self._line_start = 0
        self._search_start = 0
        self._finished = True
        return retained_bytes

    def _extract_complete_events(self) -> tuple[list[SSEEvent], int]:
        events: list[SSEEvent] = []
        consumed_end = 0
        last_event_end = 0
        while not self._terminal:
            boundary = self._find_event_boundary()
            if boundary is None:
                self._enforce_event_limit(len(self._buffer) - consumed_end)
                break

            frame_end, delimiter_end = boundary
            self._enforce_event_limit(frame_end - consumed_end)
            event = self._parse_event(
                bytes(self._buffer[consumed_end:frame_end])
            )
            consumed_end = delimiter_end
            self._line_start = delimiter_end
            self._search_start = delimiter_end
            if event is None:
                continue
            events.append(event)
            last_event_end = consumed_end
            if event.done:
                self._terminal = True
                self._terminal_trailing_bytes += len(self._buffer) - consumed_end
                consumed_end = len(self._buffer)
        if consumed_end:
            del self._buffer[:consumed_end]
            self._line_start = max(self._line_start - consumed_end, 0)
            self._search_start = max(self._search_start - consumed_end, 0)
        return events, consumed_end - last_event_end

    def _find_event_boundary(self) -> tuple[int, int] | None:
        while True:
            newline = self._buffer.find(b"\n", self._search_start)
            if newline < 0:
                scan_start = max(self._search_start - 1, self._line_start)
                carriage_return = self._buffer.find(b"\r", scan_start)
                while carriage_return >= 0:
                    next_position = carriage_return + 1
                    if next_position == len(self._buffer):
                        break
                    if self._buffer[next_position] != 10:
                        self._fail(SSEMalformedEvent(len(self._buffer)))
                    carriage_return = self._buffer.find(
                        b"\r",
                        next_position + 1,
                    )
                self._search_start = max(len(self._buffer) - 1, self._line_start)
                return None
            content_end = (
                newline - 1
                if newline > self._line_start and self._buffer[newline - 1] == 13
                else newline
            )
            if self._buffer.find(b"\r", self._line_start, content_end) >= 0:
                self._fail(SSEMalformedEvent(len(self._buffer)))
            if content_end == self._line_start:
                return self._line_start, newline + 1
            self._line_start = newline + 1
            self._search_start = self._line_start

    def _validate_partial_line(self, *, at_eof: bool) -> None:
        carriage_return = self._buffer.find(b"\r")
        while carriage_return >= 0:
            next_position = carriage_return + 1
            if next_position == len(self._buffer):
                if at_eof:
                    self._fail(SSEMalformedEvent(len(self._buffer)))
                return
            if self._buffer[next_position] != 10:
                self._fail(SSEMalformedEvent(len(self._buffer)))
            carriage_return = self._buffer.find(b"\r", next_position + 1)

    def _parse_event(self, raw_event: bytes) -> SSEEvent | None:
        try:
            decoded = raw_event.decode("utf-8", errors="strict")
        except UnicodeDecodeError:
            self._fail(SSEInvalidEncoding(len(raw_event)))

        data_lines: list[str] = []
        event_name: str | None = None
        for line in decoded.replace("\r\n", "\n").split("\n"):
            if line.startswith(":"):
                continue
            field, separator, value = line.partition(":")
            if separator and value.startswith(" "):
                value = value[1:]
            if field == "data":
                data_lines.append(value)
            elif field == "event":
                event_name = value or None

        if not data_lines:
            return None
        data = "\n".join(data_lines)
        return SSEEvent(data=data, event=event_name, done=data == "[DONE]")

    def _enforce_event_limit(self, observed_bytes: int) -> None:
        if observed_bytes > self._max_event_bytes:
            self._fail(SSEEventTooLarge(observed_bytes, self._max_event_bytes))

    def _fail(self, error: SSEFramingError) -> None:
        self._failed = True
        raise error


def _validate_positive_int(value: int, *, name: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _safe_reason(reason: str, *, default: str) -> str:
    if isinstance(reason, str) and _SAFE_REASON_RE.fullmatch(reason):
        return reason
    return default
