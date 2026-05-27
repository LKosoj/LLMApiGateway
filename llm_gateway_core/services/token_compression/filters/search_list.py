"""Search list compressor."""

import re

from ..constants import SEARCH_LIST_PER_DIR_MAX, SEARCH_LIST_TOTAL_DIR_MAX, FILTER_SEARCH_LIST

SEARCH_LIST_HEADER_RE = re.compile(r"^Result of search in '[^']*' \(total (\d+) files?\):")


def search_list(text: str) -> str:
    """Compact Cursor Glob search result list."""
    lines = text.split("\n")
    header = lines[0]
    rest = lines[1:]

    paths: list[str] = []
    for raw in rest:
        t = raw.strip()
        if not t.startswith("- "):
            continue
        paths.append(t[2:])

    if not paths:
        return text

    by_dir: dict[str, list[str]] = {}
    for p in paths:
        slash = p.rfind("/")
        dir_ = "." if slash == -1 else (p[:slash] or "/")
        name = p if slash == -1 else p[slash + 1:]
        if dir_ not in by_dir:
            by_dir[dir_] = []
        by_dir[dir_].append(name)

    dirs = sorted(by_dir.keys())
    out = f"{header}\n{len(paths)} files in {len(dirs)} dirs:\n\n"

    for dir_ in dirs[:SEARCH_LIST_TOTAL_DIR_MAX]:
        names = by_dir[dir_]
        out += f"{dir_}/ ({len(names)}):\n"
        for n in names[:SEARCH_LIST_PER_DIR_MAX]:
            out += f"  {n}\n"
        if len(names) > SEARCH_LIST_PER_DIR_MAX:
            out += f"  +{len(names) - SEARCH_LIST_PER_DIR_MAX}\n"
        out += "\n"

    if len(dirs) > SEARCH_LIST_TOTAL_DIR_MAX:
        out += f"+{len(dirs) - SEARCH_LIST_TOTAL_DIR_MAX} more dirs\n"

    return out.rstrip("\n")


search_list.filter_name = FILTER_SEARCH_LIST  # type: ignore[attr-defined]
