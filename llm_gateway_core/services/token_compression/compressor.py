"""RTK Token Compression main entry point."""

import logging
from typing import Any

from .constants import MIN_COMPRESS_SIZE, RAW_CAP
from .models import CompressionStats
from .autodetect import auto_detect_filter
from .apply_filter import safe_apply

logger = logging.getLogger(__name__)


def compress_messages(body: dict, enabled: bool) -> CompressionStats | None:
    """
    Compress tool_result content in an OpenAI/Claude message body.

    All-or-nothing: rewrites are collected during a scan pass and only applied
    after the loop completes without raising. If anything fails mid-loop, the
    body is left untouched and ``None`` is returned.
    """
    if not enabled:
        return None
    if not body:
        return None

    items = body.get("messages")
    if not isinstance(items, list):
        items = body.get("input")
    if not isinstance(items, list):
        return None

    stats = CompressionStats()
    pending: list[tuple[dict[str, Any], str, str]] = []

    try:
        for msg in items:
            if not msg:
                continue

            if msg.get("type") == "function_call_output":
                output = msg.get("output")
                if isinstance(output, str):
                    pending.append((msg, "output", _compress_text(output, stats)))
                elif isinstance(output, list):
                    for part in output:
                        if part and part.get("type") == "input_text" and isinstance(part.get("text"), str):
                            pending.append((part, "text", _compress_text(part["text"], stats)))
                continue

            if msg.get("role") == "tool" and isinstance(msg.get("content"), str):
                pending.append((msg, "content", _compress_text(msg["content"], stats)))
                continue

            content = msg.get("content")
            if not isinstance(content, list):
                continue

            if msg.get("role") == "tool":
                for part in content:
                    if part and part.get("type") == "text" and isinstance(part.get("text"), str):
                        pending.append((part, "text", _compress_text(part["text"], stats)))
                continue

            for block in content:
                if not block or block.get("type") != "tool_result":
                    continue
                if block.get("is_error") is True:
                    continue

                block_content = block.get("content")
                if isinstance(block_content, str):
                    pending.append((block, "content", _compress_text(block_content, stats)))
                elif isinstance(block_content, list):
                    for part in block_content:
                        if part and part.get("type") == "text" and isinstance(part.get("text"), str):
                            pending.append((part, "text", _compress_text(part["text"], stats)))

    except Exception as exc:
        logger.warning("[RTK] compress_messages error: %s", exc)
        return None

    for container, key, new_text in pending:
        container[key] = new_text

    return stats if stats.hits > 0 else None


def _compress_text(text: str, stats: CompressionStats) -> str:
    """Attempt to compress a single text string; update stats in-place."""
    bytes_in = len(text.encode("utf-8"))
    stats.input_bytes += bytes_in

    if bytes_in < MIN_COMPRESS_SIZE or bytes_in > RAW_CAP:
        stats.output_bytes += bytes_in
        return text

    fn = auto_detect_filter(text)
    if fn is None:
        stats.output_bytes += bytes_in
        return text

    out = safe_apply(fn, text)

    bytes_out = len(out.encode("utf-8"))
    stats.output_bytes += bytes_out
    stats.hits += 1
    filter_name = getattr(fn, "filter_name", getattr(fn, "__name__", "unknown"))
    if filter_name not in stats.filters_applied:
        stats.filters_applied.append(filter_name)

    return out
