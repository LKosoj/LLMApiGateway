"""Safe filter application."""

import logging
from typing import Callable

logger = logging.getLogger(__name__)


def safe_apply(fn: Callable[[str], str], text: str) -> str:
    """Apply filter function safely; on error or size growth return original text."""
    if not callable(fn):
        return text
    try:
        result = fn(text)
        if not isinstance(result, str):
            return text
        # Safety: never return empty or a result that grew the input
        if len(result) == 0 or len(result) >= len(text):
            return text
        return result
    except Exception as exc:
        name = getattr(fn, "filter_name", getattr(fn, "__name__", "anonymous"))
        logger.warning(
            "[rtk] warning: filter '%s' raised exception — passing through raw output: %s",
            name,
            exc,
        )
        return text
