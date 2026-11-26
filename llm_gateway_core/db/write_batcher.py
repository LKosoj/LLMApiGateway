from __future__ import annotations

import asyncio
import hashlib
import itertools
import json
import logging
import math
import sqlite3
import threading
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from contextlib import suppress
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from operator import itemgetter
from pathlib import Path
from typing import Any

import aiosqlite

logger = logging.getLogger(__name__)

DEFAULT_WRITE_BATCHER_QUEUE_MAXSIZE = 4096
WRITE_BATCHER_DEAD_LETTER_MAX_FILE_BYTES = 8 * 1024 * 1024
WRITE_BATCHER_DEAD_LETTER_MAX_RECORD_BYTES = 16 * 1024
_DEAD_LETTER_PARAM_LIMIT = 32
_DEAD_LETTER_MAX_PENDING = 256

# One control slot is reserved for this sentinel. Work admission still uses the
# configured capacity exactly.
_STOP = object()

TUNING_PRAGMAS = (
    "PRAGMA temp_store=MEMORY",
    "PRAGMA cache_size=-32000",
    "PRAGMA mmap_size=268435456",
)

PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
    *TUNING_PRAGMAS,
)

RUNTIME_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
    *TUNING_PRAGMAS,
)


def apply_runtime_pragmas(conn: sqlite3.Connection) -> None:
    """Apply per-connection runtime PRAGMAs to a synchronous SQLite connection."""
    for pragma in RUNTIME_PRAGMAS:
        conn.execute(pragma)

_WriteItem = tuple[str, tuple]


class WriteBatcherState(str, Enum):
    NEW = "new"
    RUNNING = "running"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class WriteBatcherError(RuntimeError):
    """Base class for credential-safe writer admission failures."""


class WriteBatcherOverloaded(WriteBatcherError):
    """The bounded writer has no work capacity left."""


class WriteBatcherUnavailable(WriteBatcherError):
    """The writer lifecycle no longer accepts work."""


@dataclass(frozen=True, slots=True)
class WriteBatcherHealthSnapshot:
    state: WriteBatcherState
    accepting: bool
    task_running: bool
    capacity: int
    reserved: int
    pre_start: int
    handoff_pending: int
    queued: int
    in_flight: int
    accepted: int
    committed: int
    diagnostic_terminal: int
    overflow: int
    dead_letter_failures: int
    last_error_code: str | None

    def __post_init__(self) -> None:
        live_depth = self.pre_start + self.handoff_pending + self.queued + self.in_flight
        if self.reserved != live_depth or not 0 <= self.reserved <= self.capacity:
            raise RuntimeError("WriteBatcher reserved-capacity invariant violated.")
        if self.accepted != self.committed + self.diagnostic_terminal + self.reserved:
            raise RuntimeError("WriteBatcher terminal accounting invariant violated.")


class WriteBatcher:
    """Bounded, thread-safe batching for non-accounting SQLite writes."""

    def __init__(
        self,
        db_path: Path,
        *,
        batch_size: int = 100,
        flush_interval: float = 0.5,
        batch_window: float = 0.05,
        dead_letter_path: Path | None = None,
        queue_maxsize: int = DEFAULT_WRITE_BATCHER_QUEUE_MAXSIZE,
    ) -> None:
        if (
            isinstance(queue_maxsize, bool)
            or not isinstance(queue_maxsize, int)
            or queue_maxsize <= 0
        ):
            raise ValueError("queue_maxsize must be a positive integer.")

        self._db_path = db_path
        self._dead_letter_path = (
            dead_letter_path
            if dead_letter_path is not None
            else db_path.with_suffix(db_path.suffix + ".dead-letter.jsonl")
        )
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._batch_window = batch_window
        self._capacity = queue_maxsize
        self._queue: asyncio.Queue[object] = asyncio.Queue(maxsize=queue_maxsize + 1)

        self._state_lock = threading.Lock()
        self._dead_letter_lock = threading.Lock()
        self._dead_letter_executor: ThreadPoolExecutor | None = None
        self._dead_letter_pending = 0
        self._dead_letter_closed = False
        self._state = WriteBatcherState.NEW
        self._accepting = True
        self._consumer_failed = False

        self._task: asyncio.Task[None] | None = None
        self._stop_task: asyncio.Task[None] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._handoff_waiter: asyncio.Future[None] | None = None
        self._conn: aiosqlite.Connection | None = None

        self._pre_start_buffer: deque[_WriteItem] = deque()
        self._handoff_items: dict[int, _WriteItem] = {}
        self._next_handoff_id = 0
        self._queued = 0
        self._in_flight = 0

        self._accepted = 0
        self._committed = 0
        self._diagnostic_terminal = 0
        self._overflow = 0
        self._dead_letter_failures = 0
        self._last_error_code: str | None = None

    @property
    def db_path(self) -> Path:
        return self._db_path

    def health_snapshot(self) -> WriteBatcherHealthSnapshot:
        with self._state_lock:
            pre_start = len(self._pre_start_buffer)
            handoff_pending = len(self._handoff_items)
            reserved = pre_start + handoff_pending + self._queued + self._in_flight
            task = self._task
            return WriteBatcherHealthSnapshot(
                state=self._state,
                accepting=self._accepting,
                task_running=task is not None and not task.done(),
                capacity=self._capacity,
                reserved=reserved,
                pre_start=pre_start,
                handoff_pending=handoff_pending,
                queued=self._queued,
                in_flight=self._in_flight,
                accepted=self._accepted,
                committed=self._committed,
                diagnostic_terminal=self._diagnostic_terminal,
                overflow=self._overflow,
                dead_letter_failures=self._dead_letter_failures,
                last_error_code=self._last_error_code,
            )

    async def start(self) -> None:
        """Start the single consumer; repeated calls on its owner loop are no-ops."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._state is WriteBatcherState.RUNNING:
                if self._loop is loop and self._task is not None and not self._task.done():
                    return
                self._fail_locked("consumer_unavailable", consumer_failed=True)
                raise WriteBatcherUnavailable("WriteBatcher consumer is unavailable.")
            if self._state is not WriteBatcherState.NEW:
                raise WriteBatcherUnavailable("WriteBatcher cannot be started in its current state.")

            self._loop = loop
            while self._pre_start_buffer:
                self._queue.put_nowait(self._pre_start_buffer.popleft())
                self._queued += 1
            self._state = WriteBatcherState.RUNNING
            self._task = loop.create_task(self._run(), name="write-batcher")

    def enqueue(self, sql: str, params: tuple) -> None:
        """Synchronously reserve capacity and accept one write from any thread."""
        item = (sql, params)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        overloaded = False
        stranded: tuple[_WriteItem, ...] = ()
        handoff_waiter: asyncio.Future[None] | None = None

        with self._state_lock:
            if not self._accepting or self._state not in {
                WriteBatcherState.NEW,
                WriteBatcherState.RUNNING,
            }:
                raise WriteBatcherUnavailable("WriteBatcher is unavailable.")

            if self._reserved_locked() >= self._capacity:
                self._overflow += 1
                self._last_error_code = "overloaded"
                overloaded = True
            else:
                self._accepted += 1
                if self._state is WriteBatcherState.NEW:
                    self._pre_start_buffer.append(item)
                    return

                loop = self._loop
                if running_loop is loop:
                    self._queue.put_nowait(item)
                    self._queued += 1
                    return

                self._next_handoff_id += 1
                handoff_id = self._next_handoff_id
                self._handoff_items[handoff_id] = item
                try:
                    if loop is None:
                        raise RuntimeError
                    loop.call_soon_threadsafe(self._complete_handoff, handoff_id)
                except RuntimeError:
                    stranded = tuple(self._handoff_items.values())
                    self._handoff_items.clear()
                    self._diagnostic_terminal += len(stranded)
                    self._fail_locked("handoff_unavailable", consumer_failed=True)
                    handoff_waiter = self._take_handoff_waiter_locked()

        if overloaded:
            self._schedule_dead_letter(
                (item,),
                reason="overloaded",
                error_type="WriteBatcherOverloaded",
            )
            logger.error("WriteBatcher admission rejected: overloaded.")
            raise WriteBatcherOverloaded("WriteBatcher capacity is exhausted.")

        if stranded:
            self._resolve_handoff_waiter(handoff_waiter)
            self._schedule_dead_letter(
                stranded,
                reason="handoff_unavailable",
                error_type="RuntimeError",
            )
            logger.error("WriteBatcher cross-thread handoff is unavailable.")
            raise WriteBatcherUnavailable("WriteBatcher cross-thread handoff is unavailable.")

    async def stop(self) -> None:
        """Join one cancellation-safe internal drain shared by every caller."""
        loop = asyncio.get_running_loop()
        with self._state_lock:
            if self._loop is not None and self._loop is not loop:
                raise WriteBatcherUnavailable("WriteBatcher stop must run on its owner loop.")
            if self._loop is None:
                self._loop = loop
            if self._stop_task is None:
                self._stop_task = loop.create_task(
                    self._stop_impl(),
                    name="write-batcher-stop",
                )
            stop_task = self._stop_task
        await asyncio.shield(stop_task)

    async def _stop_impl(self) -> None:
        direct_batch: list[_WriteItem] = []
        handoff_waiter: asyncio.Future[None] | None = None

        with self._state_lock:
            self._accepting = False
            if self._task is None and self._pre_start_buffer:
                direct_batch = list(self._pre_start_buffer)
                self._pre_start_buffer.clear()
                self._in_flight += len(direct_batch)
            if self._state is WriteBatcherState.NEW:
                self._state = WriteBatcherState.STOPPING
            elif self._state is WriteBatcherState.RUNNING:
                self._state = WriteBatcherState.STOPPING

            if self._handoff_items:
                loop = self._loop
                if loop is None:
                    raise RuntimeError("WriteBatcher owner loop is missing.")
                handoff_waiter = loop.create_future()
                self._handoff_waiter = handoff_waiter
            consumer_task = self._task

        try:
            if handoff_waiter is not None:
                await handoff_waiter

            if direct_batch:
                try:
                    await self._flush(direct_batch)
                except asyncio.CancelledError:
                    await self._terminalize_writer_failure(
                        direct_batch,
                        reason="stop_cancelled",
                        error_type="CancelledError",
                    )
                except BaseException as exc:
                    await self._terminalize_writer_failure(
                        direct_batch,
                        reason="direct_flush_failed",
                        error_type=_safe_error_type(exc),
                    )
            elif consumer_task is not None and not consumer_task.done():
                self._queue.put_nowait(_STOP)
                await consumer_task
            elif consumer_task is not None:
                await consumer_task
        finally:
            await self._close_connection()
            with self._state_lock:
                executor = self._dead_letter_executor
                self._dead_letter_executor = None
                self._dead_letter_closed = True
            if executor is not None:
                await asyncio.to_thread(executor.shutdown, wait=True)

        with self._state_lock:
            if self._state is not WriteBatcherState.FAILED:
                self._state = WriteBatcherState.STOPPED
            self._assert_terminal_locked()

    async def _run(self) -> None:
        current_batch: list[_WriteItem] = []
        batch_in_flight = False
        try:
            loop = asyncio.get_running_loop()
            window = max(0.0, min(self._batch_window, self._flush_interval))
            while True:
                item = await self._queue.get()
                if item is _STOP:
                    break

                current_batch = [item]
                stop_after_batch = False
                deadline = loop.time() + window
                while len(current_batch) < self._batch_size:
                    while len(current_batch) < self._batch_size:
                        try:
                            next_item = self._queue.get_nowait()
                        except asyncio.QueueEmpty:
                            break
                        if next_item is _STOP:
                            stop_after_batch = True
                            break
                        current_batch.append(next_item)
                    if stop_after_batch or len(current_batch) >= self._batch_size:
                        break
                    remaining = deadline - loop.time()
                    if remaining <= 0:
                        break
                    try:
                        next_item = await asyncio.wait_for(
                            self._queue.get(),
                            timeout=remaining,
                        )
                    except asyncio.TimeoutError:
                        break
                    if next_item is _STOP:
                        stop_after_batch = True
                        break
                    current_batch.append(next_item)

                self._move_queued_to_in_flight(len(current_batch))
                batch_in_flight = True
                await self._flush(current_batch)
                current_batch = []
                batch_in_flight = False
                if stop_after_batch:
                    break

            with self._state_lock:
                if self._state is not WriteBatcherState.FAILED:
                    self._state = WriteBatcherState.STOPPED
        except asyncio.CancelledError:
            if current_batch and not batch_in_flight:
                self._move_queued_to_in_flight(len(current_batch))
            await self._terminalize_writer_failure(
                current_batch,
                reason="consumer_cancelled",
                error_type="CancelledError",
            )
        except BaseException as exc:
            if current_batch and not batch_in_flight:
                self._move_queued_to_in_flight(len(current_batch))
            await self._terminalize_writer_failure(
                current_batch,
                reason="consumer_failed",
                error_type=_safe_error_type(exc),
            )
        finally:
            await self._close_connection()

    def _complete_handoff(self, handoff_id: int) -> None:
        diagnostic_item: _WriteItem | None = None
        handoff_waiter: asyncio.Future[None] | None = None
        with self._state_lock:
            item = self._handoff_items.pop(handoff_id, None)
            if item is None:
                return
            if self._consumer_failed:
                self._diagnostic_terminal += 1
                diagnostic_item = item
            else:
                self._queue.put_nowait(item)
                self._queued += 1
            handoff_waiter = self._take_handoff_waiter_locked()

        self._resolve_handoff_waiter(handoff_waiter)
        if diagnostic_item is not None:
            self._schedule_dead_letter(
                (diagnostic_item,),
                reason="consumer_failed",
                error_type="RuntimeError",
            )

    def _take_handoff_waiter_locked(self) -> asyncio.Future[None] | None:
        if self._handoff_items or self._handoff_waiter is None:
            return None
        waiter = self._handoff_waiter
        self._handoff_waiter = None
        return waiter

    @staticmethod
    def _resolve_handoff_waiter(waiter: asyncio.Future[None] | None) -> None:
        if waiter is not None and not waiter.done():
            waiter.set_result(None)

    def _move_queued_to_in_flight(self, count: int) -> None:
        with self._state_lock:
            self._queued -= count
            self._in_flight += count

    async def _flush(self, batch: list[_WriteItem]) -> None:
        if batch:
            await self._flush_or_split(batch)

    async def _flush_or_split(self, batch: list[_WriteItem]) -> None:
        try:
            await self._flush_once(batch)
        except Exception as exc:
            if len(batch) == 1:
                error_type = _safe_error_type(exc)
                self._complete_diagnostic(1, reason="write_failed")
                await asyncio.to_thread(
                    self._write_dead_letter_batch,
                    [batch[0]],
                    reason="write_failed",
                    error_type=error_type,
                )
                logger.error(
                    "WriteBatcher terminalized failed row: %s.",
                    error_type,
                )
                return

            midpoint = len(batch) // 2
            logger.warning(
                "WriteBatcher failed to flush %d writes; splitting batch.",
                len(batch),
            )
            await self._flush_or_split(batch[:midpoint])
            await self._flush_or_split(batch[midpoint:])
            return

        self._complete_committed(len(batch))
        logger.debug("WriteBatcher flushed %d writes.", len(batch))

    async def _ensure_connection(self) -> aiosqlite.Connection:
        if self._conn is None:
            conn = await aiosqlite.connect(self._db_path)
            try:
                for pragma in RUNTIME_PRAGMAS:
                    await conn.execute(pragma)
            except BaseException:
                with suppress(Exception):
                    await conn.close()
                raise
            self._conn = conn
        return self._conn

    async def _close_connection(self) -> None:
        conn = self._conn
        self._conn = None
        if conn is not None:
            with suppress(Exception):
                await conn.close()

    async def _flush_once(self, batch: list[_WriteItem]) -> None:
        db = await self._ensure_connection()
        try:
            for sql, group in itertools.groupby(batch, key=itemgetter(0)):
                params_list = [params for _, params in group]
                if len(params_list) == 1:
                    await db.execute(sql, params_list[0])
                else:
                    await db.executemany(sql, params_list)
            await db.commit()
        except BaseException:
            with suppress(Exception):
                await db.rollback()
            await self._close_connection()
            raise

    def _complete_committed(self, count: int) -> None:
        with self._state_lock:
            self._in_flight -= count
            self._committed += count

    def _complete_diagnostic(self, count: int, *, reason: str) -> None:
        with self._state_lock:
            self._in_flight -= count
            self._diagnostic_terminal += count
            self._last_error_code = reason

    async def _terminalize_writer_failure(
        self,
        current_batch: list[_WriteItem],
        *,
        reason: str,
        error_type: str,
    ) -> None:
        queued_items: list[_WriteItem] = []
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is not _STOP:
                queued_items.append(item)

        with self._state_lock:
            in_flight = self._in_flight
            diagnostic_items = list(current_batch[-in_flight:]) if in_flight else []
            diagnostic_items.extend(queued_items)
            diagnostic_items.extend(self._pre_start_buffer)
            diagnostic_items.extend(self._handoff_items.values())

            terminal_count = (
                self._in_flight
                + self._queued
                + len(self._pre_start_buffer)
                + len(self._handoff_items)
            )
            self._in_flight = 0
            self._queued = 0
            self._pre_start_buffer.clear()
            self._handoff_items.clear()
            self._diagnostic_terminal += terminal_count
            self._fail_locked(reason, consumer_failed=True)
            handoff_waiter = self._take_handoff_waiter_locked()

        self._resolve_handoff_waiter(handoff_waiter)
        if diagnostic_items:
            await asyncio.to_thread(
                self._write_dead_letter_batch,
                diagnostic_items,
                reason=reason,
                error_type=error_type,
            )
        logger.error("WriteBatcher writer terminated: %s.", error_type)

    def _reserved_locked(self) -> int:
        return (
            len(self._pre_start_buffer)
            + len(self._handoff_items)
            + self._queued
            + self._in_flight
        )

    def _fail_locked(self, reason: str, *, consumer_failed: bool) -> None:
        self._state = WriteBatcherState.FAILED
        self._accepting = False
        self._consumer_failed = self._consumer_failed or consumer_failed
        self._last_error_code = reason

    def _assert_terminal_locked(self) -> None:
        if self._reserved_locked() != 0:
            raise RuntimeError("WriteBatcher stopped with reserved writes.")
        if self._accepted != self._committed + self._diagnostic_terminal:
            raise RuntimeError("WriteBatcher stopped with unterminated writes.")

    def _schedule_dead_letter(
        self,
        items: list[_WriteItem] | tuple[_WriteItem, ...],
        *,
        reason: str,
        error_type: str,
    ) -> None:
        if not items:
            return
        sync_fallback = False
        with self._state_lock:
            if self._dead_letter_closed:
                sync_fallback = True
            elif self._dead_letter_pending >= _DEAD_LETTER_MAX_PENDING:
                self._dead_letter_failures += len(items)
                self._last_error_code = "dead_letter_write_failed"
                logger.error(
                    "WriteBatcher diagnostic queue saturated; dropping %d item(s).",
                    len(items),
                )
                return
            else:
                if self._dead_letter_executor is None:
                    self._dead_letter_executor = ThreadPoolExecutor(
                        max_workers=1,
                        thread_name_prefix="write-batcher-dl",
                    )
                executor = self._dead_letter_executor
                self._dead_letter_pending += 1

        if sync_fallback:
            self._write_dead_letter_batch(
                items,
                reason=reason,
                error_type=error_type,
            )
            return

        def _run_dead_letter() -> None:
            try:
                self._write_dead_letter_batch(
                    items,
                    reason=reason,
                    error_type=error_type,
                )
            finally:
                with self._state_lock:
                    self._dead_letter_pending -= 1

        try:
            executor.submit(_run_dead_letter)
        except RuntimeError:
            with self._state_lock:
                self._dead_letter_pending -= 1
                self._dead_letter_failures += len(items)
                self._last_error_code = "dead_letter_write_failed"
            logger.error("WriteBatcher diagnostic executor unavailable.")

    def _write_dead_letter_batch(
        self,
        items: list[_WriteItem] | tuple[_WriteItem, ...],
        *,
        reason: str,
        error_type: str,
    ) -> None:
        for item in items:
            self._write_dead_letter(item, reason=reason, error_type=error_type)

    def _write_dead_letter(
        self,
        item: _WriteItem,
        *,
        reason: str,
        error_type: str,
    ) -> bool:
        try:
            sql, params = item
            sql_text = str(sql)
            record = {
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "reason": reason,
                "error_type": _safe_error_type_name(error_type),
                "sql": _redacted_marker(
                    "sql",
                    sql_text.encode("utf-8", errors="replace"),
                    len(sql_text),
                ),
                "params": _redact_params(params),
            }
            line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
            encoded = line.encode("utf-8")
            if len(encoded) > WRITE_BATCHER_DEAD_LETTER_MAX_RECORD_BYTES:
                record["params"] = [f"<redacted params count={len(params)}>"]
                line = json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n"
                encoded = line.encode("utf-8")

            with self._dead_letter_lock:
                self._dead_letter_path.parent.mkdir(parents=True, exist_ok=True)
                current_size = (
                    self._dead_letter_path.stat().st_size
                    if self._dead_letter_path.exists()
                    else 0
                )
                if (
                    len(encoded) > WRITE_BATCHER_DEAD_LETTER_MAX_RECORD_BYTES
                    or current_size + len(encoded)
                    > WRITE_BATCHER_DEAD_LETTER_MAX_FILE_BYTES
                ):
                    raise OSError
                with self._dead_letter_path.open("a", encoding="utf-8") as handle:
                    handle.write(line)
        except Exception:
            with self._state_lock:
                self._dead_letter_failures += 1
                self._last_error_code = "dead_letter_write_failed"
            logger.error("WriteBatcher diagnostic append failed.")
            return False
        return True


def _safe_error_type(exc: BaseException) -> str:
    return _safe_error_type_name(type(exc).__name__)


def _safe_error_type_name(value: str) -> str:
    if value.isascii() and value.isidentifier() and len(value) <= 128:
        return value
    return "Exception"


def _redact_params(params: tuple) -> list[Any]:
    redacted = [_redact_value(value) for value in params[:_DEAD_LETTER_PARAM_LIMIT]]
    if len(params) > _DEAD_LETTER_PARAM_LIMIT:
        redacted.append(f"<truncated items={len(params) - _DEAD_LETTER_PARAM_LIMIT}>")
    return redacted


def _redact_value(value: Any) -> Any:
    if value is None:
        return "<redacted NoneType>"
    if isinstance(value, bool):
        encoded = str(value).encode("ascii")
        return _redacted_marker("bool", encoded, len(encoded))
    if isinstance(value, int):
        encoded = str(value).encode("ascii")
        return _redacted_marker("int", encoded, len(encoded))
    if isinstance(value, float):
        if not math.isfinite(value):
            return "<redacted non-finite float>"
        encoded = repr(value).encode("ascii")
        return _redacted_marker("float", encoded, len(encoded))
    if isinstance(value, str):
        encoded = value.encode("utf-8", errors="replace")
        return _redacted_marker("str", encoded, len(value))
    if isinstance(value, bytes):
        return _redacted_marker("bytes", value, len(value))
    if isinstance(value, (tuple, list, dict, set, frozenset)):
        return f"<redacted {type(value).__name__} len={len(value)}>"
    return f"<redacted {_safe_error_type_name(type(value).__name__)}>"


def _redacted_marker(kind: str, value: bytes, length: int) -> str:
    digest = hashlib.sha256(value).hexdigest()[:12]
    return f"<redacted {kind} len={length} sha256={digest}>"
