"""Single-loop ownership and shutdown for process-lifetime asyncio tasks."""

from __future__ import annotations

import asyncio
import inspect
import logging
import re
from collections import deque
from collections.abc import Coroutine
from dataclasses import dataclass
from typing import Any, TypeVar


logger = logging.getLogger(__name__)

TASK_FAILURE_JOURNAL_LIMIT = 64

_SAFE_TASK_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}\Z")
_SAFE_EXCEPTION_TYPE = re.compile(r"[A-Za-z_][A-Za-z0-9_]{0,127}\Z")

_T = TypeVar("_T")


class TaskSupervisorStateError(RuntimeError):
    """The requested operation is invalid for this supervisor or event loop."""


@dataclass(frozen=True, slots=True)
class TaskFailure:
    """Credential-safe diagnostic for an unexpected task failure."""

    task_name: str
    exception_type: str


class TaskSupervisor:
    """Own tasks on one event loop and cancel them together during shutdown."""

    def __init__(self) -> None:
        self._loop: asyncio.AbstractEventLoop | None = None
        self._accepting = True
        self._closed = False
        self._tasks: set[asyncio.Task[Any]] = set()
        self._failures: deque[TaskFailure] = deque(maxlen=TASK_FAILURE_JOURNAL_LIMIT)
        self._close_task: asyncio.Task[None] | None = None

    @property
    def accepting(self) -> bool:
        return self._accepting

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def task_count(self) -> int:
        return len(self._tasks)

    @property
    def failures(self) -> tuple[TaskFailure, ...]:
        return tuple(self._failures)

    def create_task(
        self,
        coroutine: Coroutine[Any, Any, _T],
        *,
        name: str,
    ) -> asyncio.Task[_T]:
        """Schedule a coroutine and transfer its ownership to the supervisor."""
        if not inspect.iscoroutine(coroutine):
            raise TypeError("TaskSupervisor.create_task() requires a coroutine object.")

        try:
            self._bind_loop()
            task_name = self._validate_task_name(name)
            if not self._accepting:
                raise TaskSupervisorStateError("Task supervisor is stopping or closed.")
            task = asyncio.create_task(coroutine, name=task_name)
        except BaseException:
            coroutine.close()
            raise

        self._tasks.add(task)
        task.add_done_callback(
            lambda completed, safe_name=task_name: self._task_done(
                completed,
                safe_name,
            )
        )
        return task

    async def close(self) -> None:
        """Stop admission, cancel all owned tasks, and wait for their completion."""
        loop = self._bind_loop()
        if self._closed:
            return
        if self._close_task is not None and self._close_task.done():
            self._consume_close_outcome(self._close_task)
            self._close_task = None
        if self._close_task is None:
            self._accepting = False
            close_coroutine = self._close_impl()
            try:
                self._close_task = loop.create_task(
                    close_coroutine,
                    name="task-supervisor-close",
                )
            except BaseException:
                close_coroutine.close()
                raise
        await asyncio.shield(self._close_task)

    def _consume_close_outcome(self, close_task: asyncio.Task[None]) -> None:
        if close_task.cancelled():
            return
        try:
            exception = close_task.exception()
        except asyncio.CancelledError:
            return
        if exception is not None:
            self._record_failure("task-supervisor-close", exception)

    def _bind_loop(self) -> asyncio.AbstractEventLoop:
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError as exc:
            raise TaskSupervisorStateError(
                "Task supervisor operations require a running event loop."
            ) from exc
        if self._loop is None:
            self._loop = running_loop
        elif self._loop is not running_loop:
            raise TaskSupervisorStateError(
                "TaskSupervisor cannot be used from another event loop."
            )
        return running_loop

    @staticmethod
    def _validate_task_name(name: str) -> str:
        if not isinstance(name, str) or _SAFE_TASK_NAME.fullmatch(name) is None:
            raise ValueError(
                "Task name must be a credential-safe ASCII identifier of at most 64 characters."
            )
        return name

    def _task_done(self, task: asyncio.Task[Any], task_name: str) -> None:
        self._tasks.discard(task)
        if task.cancelled():
            return
        try:
            exception = task.exception()
        except asyncio.CancelledError:
            return
        except BaseException as exc:
            self._record_failure(task_name, exc)
            return
        if exception is not None:
            self._record_failure(task_name, exception)

    def _record_failure(self, task_name: str, exception: BaseException) -> None:
        exception_type = type(exception).__name__
        if _SAFE_EXCEPTION_TYPE.fullmatch(exception_type) is None:
            exception_type = "BaseException"
        logger.error(
            "Supervised task failed: task=%s exception_type=%s.",
            task_name,
            exception_type,
            exc_info=exception,
        )
        self._failures.append(
            TaskFailure(
                task_name=task_name,
                exception_type=exception_type,
            )
        )

    async def _close_impl(self) -> None:
        tasks = tuple(self._tasks)
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.shield(
                asyncio.gather(*tasks, return_exceptions=True)
            )
        self._tasks.difference_update(tasks)
        self._closed = True
