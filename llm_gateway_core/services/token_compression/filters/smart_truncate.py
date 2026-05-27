"""Smart truncate filter."""

from ..constants import SMART_TRUNCATE_HEAD, SMART_TRUNCATE_TAIL, SMART_TRUNCATE_MIN_LINES, FILTER_SMART_TRUNCATE


def smart_truncate(text: str) -> str:
    """Keep head+tail lines, replace middle with count indicator."""
    lines = text.split("\n")
    if len(lines) < SMART_TRUNCATE_MIN_LINES:
        return text

    head = lines[:SMART_TRUNCATE_HEAD]
    tail = lines[len(lines) - SMART_TRUNCATE_TAIL:]
    cut = len(lines) - len(head) - len(tail)
    return "\n".join([*head, f"... +{cut} lines truncated", *tail])


smart_truncate.filter_name = FILTER_SMART_TRUNCATE  # type: ignore[attr-defined]
