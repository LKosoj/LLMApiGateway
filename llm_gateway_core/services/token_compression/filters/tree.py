"""Tree output compressor."""

from ..constants import TREE_MAX_LINES, FILTER_TREE


def tree(text: str) -> str:
    """Remove tree summary line and trailing blanks; cap overly long trees."""
    lines = text.split("\n")
    filtered: list[str] = []
    for line in lines:
        if "director" in line and "file" in line:
            continue
        if not line.strip() and not filtered:
            continue
        filtered.append(line)

    while filtered and not filtered[-1].strip():
        filtered.pop()

    if len(filtered) > TREE_MAX_LINES:
        cut = len(filtered) - TREE_MAX_LINES
        return "\n".join(filtered[:TREE_MAX_LINES]) + f"\n... +{cut} more lines"

    return "\n".join(filtered)


tree.filter_name = FILTER_TREE  # type: ignore[attr-defined]
