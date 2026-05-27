"""Auto-detect filter for tool result text."""

import re
from typing import Callable

from .constants import DETECT_WINDOW, READ_NUMBERED_MIN_HIT_RATIO, SMART_TRUNCATE_MIN_LINES
from .filters import (
    git_diff,
    git_status,
    git_log,
    build_output,
    grep,
    find,
    ls,
    tree,
    dedup_log,
    smart_truncate,
    read_numbered,
    search_list,
    READ_NUMBERED_LINE_RE,
    SEARCH_LIST_HEADER_RE,
)

_RE_GIT_DIFF = re.compile(r"^diff --git ", re.MULTILINE)
_RE_GIT_DIFF_HUNK = re.compile(r"^@@ ", re.MULTILINE)
_RE_GIT_STATUS = re.compile(
    r"^On branch |^nothing to commit|^Changes (not |to be )|^Untracked files:",
    re.MULTILINE,
)
_RE_GIT_LOG = re.compile(r"^commit [0-9a-f]{7,40}", re.MULTILINE)
_RE_PORCELAIN = re.compile(r"^[ MADRCU?!][ MADRCU?!] \S", re.MULTILINE)
_RE_BUILD_OUTPUT = re.compile(
    r"^(npm (warn|error|ERR!)|yarn (warn|error)|\s*Compiling\s+\S+|\s*Downloading\s+\S+"
    r"|added \d+ package|\[ERROR\]|BUILD (SUCCESS|FAILED)|\s*Finished\s+"
    r"|Successfully (installed|built)|ERROR:)",
    re.IGNORECASE | re.MULTILINE,
)
_RE_TREE_GLYPH = re.compile(r"[├└]──|│  ")
_RE_LS_ROW = re.compile(r"^[-dlbcps][rwx-]{9}", re.MULTILINE)
_RE_LS_TOTAL = re.compile(r"^total \d+$", re.MULTILINE)


def auto_detect_filter(text: str) -> Callable[[str], str] | None:
    """Detect the appropriate filter for the given text, or return None."""
    head = text[:DETECT_WINDOW] if len(text) > DETECT_WINDOW else text

    if _RE_GIT_DIFF.search(head) or _RE_GIT_DIFF_HUNK.search(head):
        return git_diff
    if _RE_GIT_STATUS.search(head):
        return git_status
    if _count_matches(head, _RE_GIT_LOG) >= 2:
        return git_log

    if _RE_BUILD_OUTPUT.search(head):
        return build_output

    if _is_mostly_porcelain(head):
        return git_status

    lines = head.split("\n")
    non_empty = [line for line in lines if line.strip()]

    first5 = non_empty[:5]
    if any(_is_grep_line(line) for line in first5):
        return grep

    if len(non_empty) >= 3 and all(_is_path_like(line) for line in non_empty):
        return find

    if _RE_TREE_GLYPH.search(head):
        return tree

    if _RE_LS_TOTAL.search(head) or _count_matches(head, _RE_LS_ROW) >= 3:
        return ls

    if SEARCH_LIST_HEADER_RE.search(head):
        return search_list

    all_lines = text.split("\n")
    if len(all_lines) >= SMART_TRUNCATE_MIN_LINES and _is_line_numbered(all_lines):
        return read_numbered

    if len(non_empty) >= 5:
        return dedup_log

    if len(all_lines) >= SMART_TRUNCATE_MIN_LINES:
        return smart_truncate

    return None


def _is_grep_line(line: str) -> bool:
    first = line.find(":")
    if first == -1:
        return False
    second = line.find(":", first + 1)
    if second == -1:
        return False
    lineno = line[first + 1:second]
    return lineno.isdigit()


def _is_path_like(line: str) -> bool:
    t = line.strip()
    if not t:
        return False
    if ":" in t:
        return False
    return t.startswith(".") or t.startswith("/") or "/" in t


def _is_mostly_porcelain(head: str) -> bool:
    lines = [line for line in head.split("\n") if line.strip()]
    if len(lines) < 3:
        return False
    hits = sum(1 for line in lines if _RE_PORCELAIN.match(line))
    return hits / len(lines) >= 0.6


def _is_line_numbered(lines: list[str]) -> bool:
    hits = 0
    non_empty = 0
    for line in lines[:100]:
        if not line:
            continue
        non_empty += 1
        if READ_NUMBERED_LINE_RE.match(line):
            hits += 1
    if non_empty < 5:
        return False
    return hits / non_empty >= READ_NUMBERED_MIN_HIT_RATIO


def _count_matches(text: str, pattern: re.Pattern) -> int:
    return len(pattern.findall(text))
