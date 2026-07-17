"""Typed contracts for response observation and terminal finalization."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import TypeVar, cast

from ..services.stream_observation import SSEEvent
from ..services.task_supervisor import TaskSupervisor


_T = TypeVar("_T")
MaybeAwaitable = _T | Awaitable[_T]


class WireMode(StrEnum):
    JSON = "json"
    SSE = "sse"
    OPAQUE = "opaque"


class TerminalReason(StrEnum):
    COMPLETE = "complete"
    UPSTREAM_ERROR = "upstream_error"
    CLIENT_DISCONNECT = "client_disconnect"
    CANCELLED = "cancelled"
    PROTOCOL_ERROR = "protocol_error"
    EMPTY = "empty"


@dataclass(frozen=True, slots=True, repr=False)
class ResponseStart:
    status_code: int
    headers: tuple[tuple[bytes, bytes], ...]
    wire_mode: WireMode
    charset: str


@dataclass(frozen=True, slots=True, repr=False)
class TerminalSignal:
    reason: TerminalReason
    start: ResponseStart | None = None


@dataclass(frozen=True, slots=True, repr=False)
class FinalizationResult:
    signal: TerminalSignal
    value: object


@dataclass(frozen=True, slots=True, repr=False)
class TransportResult:
    reason: TerminalReason
    response_started: bool
    response_completed: bool


class ResponseFinalizer:
    """Run the first terminal signal once in a supervisor-owned task."""

    __slots__ = (
        "_abort",
        "_callback",
        "_creation_error",
        "_lock",
        "_signal",
        "_supervisor",
        "_task",
    )

    def __init__(
        self,
        supervisor: TaskSupervisor,
        callback: Callable[[TerminalSignal], MaybeAwaitable[object]],
        *,
        abort: Callable[[], MaybeAwaitable[None]] | None = None,
    ) -> None:
        if (
            not isinstance(supervisor, TaskSupervisor)
            or not callable(callback)
            or (abort is not None and not callable(abort))
        ):
            raise TypeError("ResponseFinalizer requires a supervisor and callback.")
        self._supervisor = supervisor
        self._callback = callback
        self._abort = abort
        self._lock = asyncio.Lock()
        self._signal: TerminalSignal | None = None
        self._task: asyncio.Task[FinalizationResult] | None = None
        self._creation_error: BaseException | None = None

    @property
    def signal(self) -> TerminalSignal | None:
        return self._signal

    async def finalize(self, signal: TerminalSignal) -> FinalizationResult:
        if not isinstance(signal, TerminalSignal):
            raise TypeError("signal must be a TerminalSignal")
        async with self._lock:
            if self._creation_error is not None:
                raise self._creation_error
            if self._task is None:
                coroutine = self._run(signal)
                self._signal = signal
                try:
                    self._task = self._supervisor.create_task(
                        coroutine,
                        name="response-finalizer",
                    )
                except BaseException as exc:
                    self._creation_error = exc
                    if self._abort is not None:
                        try:
                            await await_if_needed(self._abort())
                        except BaseException:
                            pass
                    raise
            task = self._task
        return await asyncio.shield(task)

    async def _run(self, signal: TerminalSignal) -> FinalizationResult:
        value = await await_if_needed(self._callback(signal))
        return FinalizationResult(signal=signal, value=value)


JsonClassifier = Callable[[ResponseStart, bytes], MaybeAwaitable[TerminalReason]]
SseClassifier = Callable[
    [ResponseStart, SSEEvent],
    MaybeAwaitable[TerminalReason | None],
]
OpaqueClassifier = Callable[[ResponseStart], MaybeAwaitable[TerminalReason]]
BodyObserver = Callable[[ResponseStart, bytes], MaybeAwaitable[None]]
TransportObserver = Callable[[TransportResult], MaybeAwaitable[None]]


@dataclass(frozen=True, slots=True, repr=False)
class ResponseObservation:
    finalizer: ResponseFinalizer
    classify_json: JsonClassifier
    classify_sse: SseClassifier
    classify_opaque: OpaqueClassifier
    observe_body_chunk: BodyObserver | None = None
    transport_finished: TransportObserver | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.finalizer, ResponseFinalizer):
            raise TypeError("finalizer must be a ResponseFinalizer")
        for callback in (
            self.classify_json,
            self.classify_sse,
            self.classify_opaque,
        ):
            if not callable(callback):
                raise TypeError("response classifiers must be callable")
        if self.observe_body_chunk is not None and not callable(
            self.observe_body_chunk
        ):
            raise TypeError("observe_body_chunk must be callable")
        if self.transport_finished is not None and not callable(
            self.transport_finished
        ):
            raise TypeError("transport_finished must be callable")


async def await_if_needed(value: MaybeAwaitable[_T]) -> _T:
    if inspect.isawaitable(value):
        return await cast(Awaitable[_T], value)
    return cast(_T, value)


__all__ = [
    "FinalizationResult",
    "ResponseFinalizer",
    "ResponseObservation",
    "ResponseStart",
    "TerminalReason",
    "TerminalSignal",
    "TransportResult",
    "WireMode",
]
