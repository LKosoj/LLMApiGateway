"""Git diff compressor."""

from ..constants import GIT_DIFF_HUNK_MAX_LINES, FILTER_GIT_DIFF


def git_diff(diff: str, max_lines: int = 500) -> str:
    """Compact unified diff: file headers, hunk-level truncation."""
    result: list[str] = []
    current_file = ""
    added = 0
    removed = 0
    in_hunk = False
    hunk_shown = 0
    hunk_skipped = 0
    was_truncated = False
    max_hunk_lines = GIT_DIFF_HUNK_MAX_LINES

    lines = diff.split("\n")

    for line in lines:
        if line.startswith("diff --git"):
            if hunk_skipped > 0:
                result.append(f"  ... ({hunk_skipped} lines truncated)")
                was_truncated = True
                hunk_skipped = 0
            if current_file and (added > 0 or removed > 0):
                result.append(f"  +{added} -{removed}")
            parts = line.split(" b/")
            current_file = " b/".join(parts[1:]) if len(parts) > 1 else "unknown"
            result.append(f"\n{current_file}")
            added = 0
            removed = 0
            in_hunk = False
            hunk_shown = 0
        elif line.startswith("@@"):
            if hunk_skipped > 0:
                result.append(f"  ... ({hunk_skipped} lines truncated)")
                was_truncated = True
                hunk_skipped = 0
            in_hunk = True
            hunk_shown = 0
            result.append(f"  {line}")
        elif in_hunk:
            if line.startswith("+") and not line.startswith("+++"):
                added += 1
                if hunk_shown < max_hunk_lines:
                    result.append(f"  {line}")
                    hunk_shown += 1
                else:
                    hunk_skipped += 1
            elif line.startswith("-") and not line.startswith("---"):
                removed += 1
                if hunk_shown < max_hunk_lines:
                    result.append(f"  {line}")
                    hunk_shown += 1
                else:
                    hunk_skipped += 1
            elif hunk_shown < max_hunk_lines and not line.startswith("\\"):
                if hunk_shown > 0:
                    result.append(f"  {line}")
                    hunk_shown += 1

        if len(result) >= max_lines:
            result.append("\n... (more changes truncated)")
            was_truncated = True
            break

    if hunk_skipped > 0:
        result.append(f"  ... ({hunk_skipped} lines truncated)")
        was_truncated = True

    if current_file and (added > 0 or removed > 0):
        result.append(f"  +{added} -{removed}")

    if was_truncated:
        result.append("[full diff: rtk git diff --no-compact]")

    return "\n".join(result)


git_diff.filter_name = FILTER_GIT_DIFF  # type: ignore[attr-defined]
