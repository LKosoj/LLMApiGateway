import asyncio
import queue
import threading
from collections.abc import Awaitable
from typing import TypeVar


T = TypeVar("T")


def run_async(awaitable: Awaitable[T]) -> T:
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(awaitable)

    result_queue: queue.Queue[tuple[bool, object]] = queue.Queue()

    def _runner() -> None:
        try:
            result_queue.put((True, asyncio.run(awaitable)))
        except BaseException as exc:  # pragma: no cover - passthrough container
            result_queue.put((False, exc))

    thread = threading.Thread(target=_runner, daemon=True)
    thread.start()
    thread.join()

    success, payload = result_queue.get()
    if success:
        return payload  # type: ignore[return-value]
    raise payload  # type: ignore[misc]
