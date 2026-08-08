"""Shared YouTube transcript client for the web read API and the research agent.

YouTube meters its caption endpoint per source address and answers with a captcha
once an address has spent its quota, so the gateway spreads the load over the two
addresses it has: calls alternate between ``YOUTUBE_PROXY_URL`` and the gateway's
own IP. A burst of concurrent calls spends both quotas at once, so every call also
goes through a shared queue that runs one download at a time and keeps a pause
between them. Without a proxy configured every call goes out directly, as before.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable
from typing import Any, TypeVar

from ..config.settings import settings

logger = logging.getLogger(__name__)

T = TypeVar("T")

_next_call_uses_proxy = True
_queue_lock: asyncio.Lock | None = None
_queue_lock_loop: asyncio.AbstractEventLoop | None = None
_previous_call_finished_at: float | None = None


def build_youtube_transcript_api() -> Any:
    """Return a ``YouTubeTranscriptApi`` bound to whichever address comes up next."""
    global _next_call_uses_proxy

    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_url = (settings.youtube_proxy_url or "").strip()
    if not proxy_url:
        return YouTubeTranscriptApi()

    use_proxy = _next_call_uses_proxy
    _next_call_uses_proxy = not _next_call_uses_proxy
    if not use_proxy:
        logger.debug("Requesting YouTube transcript from the gateway's own address.")
        return YouTubeTranscriptApi()

    from youtube_transcript_api.proxies import GenericProxyConfig

    logger.debug("Requesting YouTube transcript through YOUTUBE_PROXY_URL.")
    return YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
    )


def _get_queue_lock() -> asyncio.Lock:
    """Return the queue's lock, rebuilding it when the running loop has changed.

    An ``asyncio.Lock`` binds to the loop that first awaits it, and the test suite
    runs each case on a fresh loop, so a lock created once at import time would
    fail there.
    """
    global _queue_lock, _queue_lock_loop

    loop = asyncio.get_running_loop()
    if _queue_lock is None or _queue_lock_loop is not loop:
        _queue_lock = asyncio.Lock()
        _queue_lock_loop = loop
    return _queue_lock


async def run_queued_transcript_call(fetch: Callable[[], T]) -> T:
    """Run ``fetch`` in a worker thread as the queue's only call, after the pause."""
    global _previous_call_finished_at

    async with _get_queue_lock():
        if _previous_call_finished_at is not None:
            elapsed = time.monotonic() - _previous_call_finished_at
            remaining = settings.youtube_fetch_interval_seconds - elapsed
            if remaining > 0:
                logger.debug("Holding the YouTube transcript queue for %.1fs.", remaining)
                await asyncio.sleep(remaining)
        try:
            return await asyncio.to_thread(fetch)
        finally:
            _previous_call_finished_at = time.monotonic()


def reset_transcript_queue() -> None:
    """Drop the queue's pause timer and put the alternation back on the proxy."""
    global _previous_call_finished_at, _next_call_uses_proxy

    _previous_call_finished_at = None
    _next_call_uses_proxy = True
