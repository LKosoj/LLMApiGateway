import asyncio
import logging
import threading
from collections import deque
from contextlib import suppress
from pathlib import Path

import aiosqlite

logger = logging.getLogger(__name__)

# Sentinel that signals the background task to stop after draining.
_STOP = object()

# Persistent PRAGMAs — ``journal_mode=WAL`` is stored in the database file,
# so it only needs to run once at initialization. Keeping it here for
# backwards-compatibility with code that still references ``PRAGMAS``.
PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
)

# Runtime PRAGMAs — these are per-connection settings. ``journal_mode`` is
# intentionally excluded: it's persistent (set once at init) and re-running
# it on every query costs an extra round-trip with no behavioral effect.
RUNTIME_PRAGMAS = (
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
)


class WriteBatcher:
    """Batches INSERT/DELETE writes into single transactions.

    * ``enqueue()`` is **thread-safe** so it can be called from any thread
      (e.g. ``ChunkProcessorThread`` in chat_logging).
    * A background ``asyncio.Task`` drains the queue every *flush_interval*
      seconds or when *batch_size* items have accumulated — whichever comes
      first.
    * ``stop()`` performs a graceful drain: every item enqueued before ``stop``
      is guaranteed to be written.
    """

    def __init__(
        self,
        db_path: Path,
        *,
        batch_size: int = 100,
        flush_interval: float = 0.5,
    ) -> None:
        self._db_path = db_path
        self._batch_size = batch_size
        self._flush_interval = flush_interval
        self._queue: asyncio.Queue[object] = asyncio.Queue()
        self._task: asyncio.Task | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._state_lock = threading.Lock()
        self._stopping = False
        self._closed = False
        # Pre-start buffer: thread-safe deque for writes enqueued before the
        # event loop is running. Drained into _queue on start() or discarded
        # on stop() so that no writes are silently lost.
        self._pre_start_buffer: deque[tuple[str, tuple]] = deque()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the background writer task.  Must be called from the event loop."""
        with self._state_lock:
            if self._closed:
                raise RuntimeError("WriteBatcher cannot be restarted after stop.")
            self._stopping = False
        self._loop = asyncio.get_running_loop()
        # Drain pre-start buffer into the queue before the task begins consuming.
        with self._state_lock:
            while self._pre_start_buffer:
                self._queue.put_nowait(self._pre_start_buffer.popleft())
        self._task = asyncio.create_task(self._run(), name="write-batcher")

    def enqueue(self, sql: str, params: tuple) -> None:
        """Thread-safe enqueue of a single write operation.

        Can be called from the event loop thread (direct put) or from any
        other thread (``call_soon_threadsafe``). If the background task has
        not started yet, items are buffered in a thread-safe deque and drained
        once the event loop is running — preventing silent data loss during
        startup races.
        """
        with self._state_lock:
            if self._closed:
                message = "WriteBatcher is closed; cannot enqueue write."
                logger.error(message)
                raise RuntimeError(message)
            if self._stopping:
                message = "WriteBatcher is stopping; cannot enqueue write."
                logger.error(message)
                raise RuntimeError(message)

            # Not yet started — buffer locally.
            if self._loop is None:
                self._pre_start_buffer.append((sql, params))
                return

            loop = self._loop

        item = (sql, params)
        try:
            running_loop = asyncio.get_running_loop()
        except RuntimeError:
            running_loop = None

        if running_loop is loop:
            # Same event loop — put directly (no scheduling delay).
            self._queue.put_nowait(item)
        else:
            # Different thread — schedule via thread-safe callback.
            loop.call_soon_threadsafe(self._queue.put_nowait, item)

    async def stop(self) -> None:
        """Gracefully stop: drain every pending item, then close."""
        if self._task is None:
            # Nothing started — drain pre-start buffer into a one-shot flush.
            with self._state_lock:
                batch = list(self._pre_start_buffer)
                self._pre_start_buffer.clear()
                self._closed = True
            if batch:
                await self._flush(batch)
            return
        with self._state_lock:
            should_signal_stop = not self._stopping and not self._closed
            self._stopping = True

        # Put the stop sentinel so the background loop exits *after* draining.
        if should_signal_stop:
            self._loop.call_soon(self._queue.put_nowait, _STOP)
        with suppress(asyncio.CancelledError):
            await self._task
        self._task = None
        # Clear any residual pre-start buffer (should be empty at this point).
        with self._state_lock:
            self._pre_start_buffer.clear()

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _run(self) -> None:
        while True:
            batch: list[tuple[str, tuple]] = []
            try:
                item = await asyncio.wait_for(
                    self._queue.get(), timeout=self._flush_interval,
                )
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if item is _STOP:
                # Drain everything remaining before _STOP.
                with self._state_lock:
                    self._drain_remaining_queue_into(batch)
                await self._flush(batch)
                with self._state_lock:
                    self._closed = True
                return

            batch.append(item)
            # Collect more items up to batch_size without blocking.
            self._drain_queue_into(batch)
            await self._flush(batch)

    def _drain_queue_into(self, batch: list) -> None:
        while len(batch) < self._batch_size:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STOP:
                # Re-enqueue sentinel so the outer loop sees it next iteration.
                self._queue.put_nowait(_STOP)
                break
            batch.append(item)

    def _drain_remaining_queue_into(self, batch: list) -> None:
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if item is _STOP:
                continue
            batch.append(item)

    async def _flush(self, batch: list[tuple[str, tuple]]) -> None:
        if not batch:
            return
        try:
            async with aiosqlite.connect(self._db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                for sql, params in batch:
                    await db.execute(sql, params)
                await db.commit()
            logger.debug("WriteBatcher: flushed %d writes.", len(batch))
        except Exception:
            logger.exception("WriteBatcher: failed to flush %d writes.", len(batch))
