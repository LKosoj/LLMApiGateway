"""In-memory cache for static HTML templates served by the gateway UI.

The gateway ships a handful of HTML pages (login, stats, rules editor) that
are served on every UI navigation. Previously each request re-read the file
from disk on the event loop — cheap in absolute terms, but wasteful when
hundreds of tabs poll the stats page. We load the contents once at startup
and serve subsequent requests from memory.

Cache invalidation is explicit: if a file changes on disk, the process must
be restarted to pick it up. This is acceptable because the HTML lives in the
repo and changes only via deploys.
"""
from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from threading import Lock
from typing import Iterable

from fastapi import HTTPException

logger = logging.getLogger(__name__)

_templates: dict[str, str] = {}
_lock = Lock()


def preload_templates(paths: Iterable[Path]) -> None:
    """Read all given paths into the in-memory cache.

    Missing files are logged and skipped — the request handler will still
    return a 404 when it asks for the template at request time.
    """
    for path in paths:
        try:
            _templates[str(path)] = path.read_text(encoding="utf-8")
            logger.info("HTML template cached: %s (%d bytes)", path, len(_templates[str(path)]))
        except FileNotFoundError:
            logger.warning("HTML template not found during preload: %s", path)
        except OSError:
            logger.exception("Failed to read HTML template during preload: %s", path)


def _read_and_cache_template(path: Path, key: str) -> str:
    with _lock:
        cached = _templates.get(key)
        if cached is not None:
            return cached
        contents = path.read_text(encoding="utf-8")
        _templates[key] = contents
        return contents


async def get_template(path: Path) -> str:
    """Return the cached template contents, reading once on demand if missing.

    The on-demand read is guarded by a lock so concurrent first-use doesn't
    race, and it runs in a worker thread so async handlers do not block the
    event loop. After startup preload this path is never taken in production,
    but tests that skip the lifespan still work.
    """
    key = str(path)
    cached = _templates.get(key)
    if cached is not None:
        return cached

    try:
        return await asyncio.to_thread(_read_and_cache_template, path, key)
    except FileNotFoundError as exc:
        logger.warning("HTML template not found during on-demand read: %s", path)
        raise HTTPException(status_code=404, detail="HTML template not found.") from exc


def clear_cache() -> None:
    """Drop all cached templates — intended for test teardown."""
    _templates.clear()
