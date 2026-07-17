"""Bounded spawn-process owner for isolated GPT Researcher jobs."""

from __future__ import annotations

import asyncio
import logging
import multiprocessing
import os
import re
import signal
import socket
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from multiprocessing.process import BaseProcess
from typing import TypeVar

from .deep_research_child import child_main
from .deep_research_protocol import (
    DeepResearchCallbackRequest,
    DeepResearchChildReportedError,
    DeepResearchJob,
    DeepResearchProtocolError,
    DeepResearchResult,
    JsonValue,
    callback_error_message,
    callback_result_message,
    parse_callback_request,
    parse_ready_message,
    parse_terminal_message,
    recv_async_frame,
    run_message,
    send_async_frame,
)


logger = logging.getLogger(__name__)


DEEP_RESEARCH_BOOTSTRAP_TIMEOUT_SECONDS = 5.0
DEEP_RESEARCH_CALLBACK_TIMEOUT_SECONDS = 300.0
DEEP_RESEARCH_TERMINATE_GRACE_SECONDS = 2.0
_ADAPTER_MODULE_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_.]{0,255}$")
_T = TypeVar("_T")


DeepResearchCallback = Callable[
    [DeepResearchCallbackRequest],
    Awaitable[JsonValue],
]


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchCallbacks:
    """Immutable parent-loop callback boundary for one research job."""

    handle: DeepResearchCallback

    def __post_init__(self) -> None:
        if not callable(self.handle):
            raise ValueError("deep-research callback handler must be callable")


class DeepResearchProcessError(RuntimeError):
    def __init__(self, code: str, *, exit_code: int | None = None) -> None:
        self.code = code
        self.exit_code = exit_code
        suffix = "" if exit_code is None else f"; exit_code={exit_code}"
        super().__init__(f"deep-research process failed: {code}{suffix}")


class DeepResearchAdmissionTimeout(DeepResearchProcessError):
    def __init__(self) -> None:
        super().__init__("admission_timeout")


class DeepResearchProcessClosed(DeepResearchProcessError):
    def __init__(self) -> None:
        super().__init__("runner_closed")


class _DeepResearchCallbackDrainError(DeepResearchProtocolError):
    pass


class DeepResearchRunnerState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    CLOSING = "closing"
    CLOSED = "closed"


@dataclass(eq=False, slots=True)
class _ChildOwner:
    process: BaseProcess
    sock: socket.socket
    exit_task: asyncio.Task[int]
    ready: bool = False
    closed: bool = False
    done: asyncio.Event = field(default_factory=asyncio.Event)
    cleanup_task: asyncio.Task[int] | None = None

    async def finish(self) -> int:
        if self.cleanup_task is None:
            self.cleanup_task = asyncio.create_task(
                _finish_child(self),
                name=f"deep-research-cleanup-{self.process.pid}",
            )
        return await _await_cancellation_resistant(self.cleanup_task)


class DeepResearchProcessRunner:
    """Own one spawned process per job and bound concurrent admission."""

    def __init__(
        self,
        *,
        capacity: int = 1,
        admission_timeout_seconds: float = 5.0,
        callback_timeout_seconds: float = DEEP_RESEARCH_CALLBACK_TIMEOUT_SECONDS,
        _adapter_module: str = "llm_gateway_core.agents.deep_research_adapter",
    ) -> None:
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValueError("capacity must be a positive integer")
        if (
            isinstance(admission_timeout_seconds, bool)
            or not isinstance(admission_timeout_seconds, (int, float))
            or admission_timeout_seconds <= 0
            or not float(admission_timeout_seconds) < float("inf")
        ):
            raise ValueError("admission_timeout_seconds must be positive and finite")
        if (
            isinstance(callback_timeout_seconds, bool)
            or not isinstance(callback_timeout_seconds, (int, float))
            or callback_timeout_seconds <= 0
            or not float(callback_timeout_seconds) < float("inf")
        ):
            raise ValueError("callback_timeout_seconds must be positive and finite")
        if _ADAPTER_MODULE_RE.fullmatch(_adapter_module) is None:
            raise ValueError("adapter module path is invalid")
        self._capacity = capacity
        self._admission_timeout_seconds = float(admission_timeout_seconds)
        self._callback_timeout_seconds = float(callback_timeout_seconds)
        self._adapter_module = _adapter_module
        self._context = multiprocessing.get_context("spawn")
        self._semaphore = asyncio.BoundedSemaphore(capacity)
        self._closed_event = asyncio.Event()
        self._owners: set[_ChildOwner] = set()
        self._state = DeepResearchRunnerState.NEW
        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock = asyncio.Lock()
        self._active_runs = 0
        self._active_runs_drained = asyncio.Event()
        self._active_runs_drained.set()
        self._close_task: asyncio.Task[None] | None = None

    @property
    def state(self) -> DeepResearchRunnerState:
        return self._state

    @property
    def active_process_count(self) -> int:
        return len(self._owners)

    @property
    def active_process_ids(self) -> tuple[int, ...]:
        return tuple(sorted(owner.process.pid for owner in self._owners))

    async def start(self) -> None:
        self._bind_loop()
        async with self._lifecycle_lock:
            if self._state is DeepResearchRunnerState.NEW:
                self._state = DeepResearchRunnerState.RUNNING
                return
            if self._state is DeepResearchRunnerState.RUNNING:
                return
            raise DeepResearchProcessClosed()

    async def run(
        self,
        job: DeepResearchJob,
        *,
        callbacks: DeepResearchCallbacks | None = None,
    ) -> DeepResearchResult:
        self._bind_loop()
        registered = False
        slot_acquired = False
        owner: _ChildOwner | None = None
        owner_pid: int | None = None
        try:
            async with self._lifecycle_lock:
                if self._state is not DeepResearchRunnerState.RUNNING:
                    raise DeepResearchProcessClosed()
                self._active_runs += 1
                self._active_runs_drained.clear()
                registered = True

            acquire_task = asyncio.create_task(
                self._acquire_slot(),
                name=f"deep-research-admission-{job.job_id}",
            )
            try:
                await asyncio.shield(acquire_task)
            except asyncio.CancelledError:
                if not acquire_task.done():
                    acquire_task.cancel()
                await _wait_until_done(acquire_task)
                admission_succeeded = (
                    not acquire_task.cancelled()
                    and acquire_task.exception() is None
                )
                _consume_task_result(acquire_task)
                if admission_succeeded:
                    self._semaphore.release()
                raise
            slot_acquired = True
            async with self._lifecycle_lock:
                if self._state is not DeepResearchRunnerState.RUNNING:
                    raise DeepResearchProcessClosed()
                owner = self._spawn_owner(job)
                owner_pid = owner.process.pid
                self._owners.add(owner)

            try:
                ready = await asyncio.wait_for(
                    _recv_async_frame(owner.sock),
                    timeout=DEEP_RESEARCH_BOOTSTRAP_TIMEOUT_SECONDS,
                )
                parse_ready_message(ready, expected_pid=owner.process.pid)
                owner.ready = True
                await _send_async_frame(owner.sock, run_message(job))
                result = await _serve_job_callbacks(
                    owner.sock,
                    job=job,
                    callbacks=callbacks,
                    callback_timeout_seconds=self._callback_timeout_seconds,
                )
            except DeepResearchChildReportedError as exc:
                exit_code = await owner.finish()
                raise DeepResearchProcessError(
                    exc.code,
                    exit_code=exit_code,
                ) from exc
            except _DeepResearchCallbackDrainError as exc:
                exit_code = await owner.finish()
                raise DeepResearchProcessError(
                    "protocol_error",
                    exit_code=exit_code,
                ) from exc
            except (DeepResearchProtocolError, OSError, TimeoutError) as exc:
                exit_code = await owner.finish()
                code = "child_crashed" if exit_code else "protocol_error"
                raise DeepResearchProcessError(code, exit_code=exit_code) from exc

            await owner.finish()
            return result
        except asyncio.CancelledError:
            if owner is not None:
                await owner.finish()
            raise
        finally:
            if owner is not None:
                if not owner.closed:
                    try:
                        await owner.finish()
                    except Exception:
                        logger.exception(
                            "deep-research child cleanup failed for pid=%s; "
                            "releasing admission slot and bookkeeping anyway",
                            owner_pid,
                        )
                self._owners.discard(owner)
                owner.done.set()
            if slot_acquired:
                self._semaphore.release()
            if registered:
                self._active_runs -= 1
                if self._active_runs == 0:
                    self._active_runs_drained.set()

    async def aclose(self) -> None:
        self._bind_loop()
        async with self._lifecycle_lock:
            if self._state is DeepResearchRunnerState.CLOSED:
                return
            if self._state is DeepResearchRunnerState.NEW:
                self._state = DeepResearchRunnerState.CLOSED
                self._closed_event.set()
                return
            if self._close_task is None:
                self._state = DeepResearchRunnerState.CLOSING
                self._closed_event.set()
                self._close_task = asyncio.create_task(
                    self._finish_close(tuple(self._owners)),
                    name="deep-research-runner-close",
                )
            close_task = self._close_task
        await _await_cancellation_resistant(close_task)

    def _spawn_owner(self, job: DeepResearchJob) -> _ChildOwner:
        try:
            parent_sock, child_sock = socket.socketpair()
        except OSError as exc:
            raise DeepResearchProcessError("spawn_failed") from exc
        parent_sock.setblocking(False)
        process = self._context.Process(
            target=child_main,
            args=(child_sock, self._adapter_module),
            name=f"deep-research-{job.job_id}",
            daemon=False,
        )
        try:
            process.start()
        except Exception as exc:
            parent_sock.close()
            child_sock.close()
            raise DeepResearchProcessError("spawn_failed") from exc
        child_sock.close()
        return _ChildOwner(
            process=process,
            sock=parent_sock,
            exit_task=asyncio.create_task(
                _wait_process_exit(process),
                name=f"deep-research-reap-{job.job_id}",
            ),
        )

    async def _finish_close(self, owners: tuple[_ChildOwner, ...]) -> None:
        if owners:
            # An owner may already be closed here: its own run() call can
            # finish (and discard it from self._owners) concurrently with
            # aclose()'s snapshot, leaving this stale reference pointing at a
            # closed process. Reading .pid on a closed BaseProcess raises
            # ValueError, so guard it with owner.closed first.
            owner_pids = tuple(
                None if owner.closed else owner.process.pid for owner in owners
            )
            results = await asyncio.gather(
                *(owner.finish() for owner in owners),
                return_exceptions=True,
            )
            for owner_pid, outcome in zip(owner_pids, results, strict=True):
                # gather(return_exceptions=True) hands back a self-raised
                # CancelledError as a plain result value rather than
                # propagating it, so BaseException (not Exception) is
                # required to catch and log it instead of silently dropping
                # it.
                if isinstance(outcome, BaseException):
                    logger.error(
                        "deep-research child cleanup failed during runner "
                        "shutdown for pid=%s",
                        owner_pid,
                        exc_info=outcome,
                    )
        await self._active_runs_drained.wait()
        self._state = DeepResearchRunnerState.CLOSED

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
        elif self._loop is not loop:
            raise RuntimeError("DeepResearchProcessRunner cannot cross event loops")

    async def _acquire_slot(self) -> None:
        acquire = asyncio.create_task(self._semaphore.acquire())
        closed = asyncio.create_task(self._closed_event.wait())
        try:
            done, _pending = await asyncio.wait(
                {acquire, closed},
                timeout=self._admission_timeout_seconds,
                return_when=asyncio.FIRST_COMPLETED,
            )
            if closed in done or self._state is not DeepResearchRunnerState.RUNNING:
                raise DeepResearchProcessClosed()
            if acquire not in done:
                raise DeepResearchAdmissionTimeout()
            await acquire
        except BaseException:
            if (
                acquire.done()
                and not acquire.cancelled()
                and acquire.exception() is None
            ):
                self._semaphore.release()
            raise
        finally:
            for task in (acquire, closed):
                if not task.done():
                    task.cancel()
            await asyncio.gather(acquire, closed, return_exceptions=True)


async def _serve_job_callbacks(
    sock: socket.socket,
    *,
    job: DeepResearchJob,
    callbacks: DeepResearchCallbacks | None,
    callback_timeout_seconds: float,
) -> DeepResearchResult:
    write_lock = asyncio.Lock()
    callback_tasks: set[asyncio.Task[None]] = set()
    seen_message_ids: set[str] = set()
    receive_task = asyncio.create_task(
        _recv_async_frame(sock),
        name=f"deep-research-receive-{job.job_id}",
    )
    try:
        while True:
            done, _pending = await asyncio.wait(
                {receive_task, *callback_tasks},
                return_when=asyncio.FIRST_COMPLETED,
            )
            completed_callbacks = done & callback_tasks
            callback_tasks.difference_update(completed_callbacks)
            for task in completed_callbacks:
                task.result()

            if receive_task not in done:
                continue
            message = receive_task.result()
            if isinstance(message, dict) and message.get("type") == "callback_request":
                callback_request = parse_callback_request(message)
                if callback_request.job_id != job.job_id:
                    raise DeepResearchProtocolError("invalid callback job identity")
                if callback_request.message_id in seen_message_ids:
                    raise DeepResearchProtocolError("duplicate callback message id")
                seen_message_ids.add(callback_request.message_id)
                callback_task = asyncio.create_task(
                    _handle_callback_request(
                        sock,
                        request=callback_request,
                        callbacks=callbacks,
                        callback_timeout_seconds=callback_timeout_seconds,
                        write_lock=write_lock,
                    ),
                    name=(
                        "deep-research-callback-"
                        f"{callback_request.operation.value}-{callback_request.message_id}"
                    ),
                )
                callback_tasks.add(callback_task)
                receive_task = asyncio.create_task(
                    _recv_async_frame(sock),
                    name=f"deep-research-receive-{job.job_id}",
                )
                continue
            newly_completed = {task for task in callback_tasks if task.done()}
            callback_tasks.difference_update(newly_completed)
            for task in newly_completed:
                task.result()
            if callback_tasks:
                raise _DeepResearchCallbackDrainError(
                    "terminal message arrived before callback drain"
                )
            return parse_terminal_message(message, job_id=job.job_id)
    finally:
        if not receive_task.done():
            receive_task.cancel()
        for task in callback_tasks:
            task.cancel()
        await asyncio.gather(
            receive_task,
            *callback_tasks,
            return_exceptions=True,
        )


async def _handle_callback_request(
    sock: socket.socket,
    *,
    request: DeepResearchCallbackRequest,
    callbacks: DeepResearchCallbacks | None,
    callback_timeout_seconds: float,
    write_lock: asyncio.Lock,
) -> None:
    if callbacks is None:
        response = callback_error_message(
            request,
            "callback_unavailable",
            "RuntimeError",
        )
    else:
        try:
            result = await asyncio.wait_for(
                callbacks.handle(request),
                timeout=callback_timeout_seconds,
            )
            response = callback_result_message(request, result)
        except asyncio.CancelledError:
            raise
        except BaseException as exc:
            response = callback_error_message(
                request,
                "callback_failed",
                _safe_exception_type(exc),
            )
    async with write_lock:
        await _send_async_frame(sock, response)


def _safe_exception_type(exc: BaseException) -> str:
    name = type(exc).__name__
    if not name.isascii() or not name.isidentifier() or len(name) > 64:
        return "Exception"
    return name


async def _send_async_frame(sock: socket.socket, message: object) -> None:
    await send_async_frame(sock, message)


async def _recv_async_frame(sock: socket.socket) -> object:
    return await recv_async_frame(sock)


async def _wait_process_exit(process: BaseProcess) -> int:
    loop = asyncio.get_running_loop()
    if process.exitcode is None:
        completed: asyncio.Future[None] = loop.create_future()

        def mark_completed() -> None:
            if not completed.done():
                completed.set_result(None)

        loop.add_reader(process.sentinel, mark_completed)
        try:
            if process.exitcode is None:
                await completed
        finally:
            loop.remove_reader(process.sentinel)
    # The sentinel is readable only after child termination. A zero-time join
    # can still observe the multiprocessing exit-code cache one tick too early.
    process.join()
    exit_code = process.exitcode
    if exit_code is None:
        raise RuntimeError("deep-research child was not reaped")
    return exit_code


async def _finish_child(owner: _ChildOwner) -> int:
    process_group = owner.process.pid if owner.ready else None
    try:
        _signal_process(owner, signal.SIGTERM)
        try:
            exit_code = await asyncio.wait_for(
                asyncio.shield(owner.exit_task),
                timeout=DEEP_RESEARCH_TERMINATE_GRACE_SECONDS,
            )
        except TimeoutError:
            _signal_process(owner, signal.SIGKILL)
            try:
                exit_code = await asyncio.wait_for(
                    asyncio.shield(owner.exit_task),
                    timeout=DEEP_RESEARCH_TERMINATE_GRACE_SECONDS,
                )
            except TimeoutError as exc:
                raise DeepResearchProcessError("reap_timeout") from exc

        if process_group is not None:
            await _drain_process_group(process_group)
        owner.process.close()
        owner.closed = True
        return exit_code
    finally:
        owner.sock.close()


async def _drain_process_group(process_group: int) -> None:
    if not _process_group_exists(process_group):
        return
    if await _wait_process_group_gone(process_group):
        return
    try:
        os.killpg(process_group, signal.SIGKILL)
    except ProcessLookupError:
        return
    if not await _wait_process_group_gone(process_group):
        raise DeepResearchProcessError("process_group_cleanup_timeout")


async def _wait_process_group_gone(process_group: int) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + DEEP_RESEARCH_TERMINATE_GRACE_SECONDS
    while _process_group_exists(process_group):
        if loop.time() >= deadline:
            return False
        await asyncio.sleep(0.01)
    return True


def _process_group_exists(process_group: int) -> bool:
    try:
        os.killpg(process_group, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


async def _wait_until_done(task: asyncio.Task[_T]) -> bool:
    cancelled = False
    while not task.done():
        try:
            await asyncio.shield(task)
        except asyncio.CancelledError:
            cancelled = True
    return cancelled


def _consume_task_result(task: asyncio.Task[_T]) -> None:
    try:
        task.result()
    except BaseException:
        pass


async def _await_cancellation_resistant(task: asyncio.Task[_T]) -> _T:
    cancelled = await _wait_until_done(task)
    if cancelled:
        raise asyncio.CancelledError
    return task.result()


def _signal_process(owner: _ChildOwner, sig: signal.Signals) -> None:
    try:
        if owner.ready:
            os.killpg(owner.process.pid, sig)
        elif owner.exit_task.done():
            return
        elif sig is signal.SIGTERM:
            owner.process.terminate()
        else:
            owner.process.kill()
    except ProcessLookupError:
        return
