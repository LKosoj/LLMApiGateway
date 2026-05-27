"""Read-numbered filter."""

import re

from ..constants import SMART_TRUNCATE_HEAD, SMART_TRUNCATE_TAIL, SMART_TRUNCATE_MIN_LINES, FILTER_READ_NUMBERED

# Matches "  N|content" (Cursor/Codex read_file format)
READ_NUMBERED_LINE_RE = re.compile(r"^\s*\d+\|")


def read_numbered(text: str) -> str:
    """Truncate line-numbered file output keeping head+tail."""
    lines = text.split("\n")
    if len(lines) < SMART_TRUNCATE_MIN_LINES:
        return text

    head = lines[:SMART_TRUNCATE_HEAD]
    tail = lines[len(lines) - SMART_TRUNCATE_TAIL:]
    cut = len(lines) - len(head) - len(tail)

    return "\n".join([
        *head,
        f"... +{cut} lines truncated (file continues)",
        *tail,
    ])


read_numbered.filter_name = FILTER_READ_NUMBERED  # type: ignore[attr-defined]
