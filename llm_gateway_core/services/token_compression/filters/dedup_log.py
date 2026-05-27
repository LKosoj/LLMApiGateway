"""Dedup log compressor."""

from ..constants import DEDUP_LINE_MAX, FILTER_DEDUP_LOG


def dedup_log(text: str) -> str:
    """Collapse consecutive duplicate lines; collapse blank runs; cap at DEDUP_LINE_MAX."""
    lines = text.split("\n")
    out: list[str] = []
    prev: str | None = None
    run_count = 0
    blank_streak = 0

    def flush_run() -> None:
        if prev is not None and run_count > 1:
            out.append(f"  ... ({run_count - 1} duplicate lines)")

    for line in lines:
        if not line.strip():
            blank_streak += 1
            flush_run()
            if blank_streak <= 1:
                out.append(line)
            prev = None
            run_count = 0
            continue

        blank_streak = 0
        if line == prev:
            run_count += 1
            continue

        flush_run()
        out.append(line)
        prev = line
        run_count = 1

        if len(out) >= DEDUP_LINE_MAX:
            out.append(f"... (truncated at {DEDUP_LINE_MAX} lines)")
            return "\n".join(out)

    flush_run()
    return "\n".join(out)


dedup_log.filter_name = FILTER_DEDUP_LOG  # type: ignore[attr-defined]
