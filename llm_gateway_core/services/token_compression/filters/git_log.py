"""Git log compressor."""

import re

from ..constants import SMART_TRUNCATE_HEAD, SMART_TRUNCATE_TAIL, SMART_TRUNCATE_MIN_LINES, FILTER_GIT_LOG

_COMMIT_RE = re.compile(r"^commit [0-9a-f]{7,40}", re.MULTILINE)


def git_log(text: str) -> str:
    """Compact git log output: keep head+tail lines with a git-aware header."""
    lines = text.split("\n")
    if len(lines) < SMART_TRUNCATE_MIN_LINES:
        return text

    commit_count = len(_COMMIT_RE.findall(text))

    head = lines[:SMART_TRUNCATE_HEAD]
    tail = lines[len(lines) - SMART_TRUNCATE_TAIL:]
    cut = len(lines) - len(head) - len(tail)

    header = f"[git log — {commit_count} commits, showing head/tail]\n" if commit_count else ""
    return header + "\n".join([
        *head,
        f"... +{cut} lines truncated",
        *tail,
    ])


git_log.filter_name = FILTER_GIT_LOG  # type: ignore[attr-defined]
