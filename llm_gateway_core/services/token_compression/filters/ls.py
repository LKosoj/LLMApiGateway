"""ls output compressor."""

import re

from ..constants import LS_EXT_SUMMARY_TOP, LS_NOISE_DIRS, FILTER_LS

_LS_DATE_RE = re.compile(
    r"\s+(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)\s+\d{1,2}\s+(\d{4}|\d{2}:\d{2})\s+"
)


def _human_size(n: int) -> str:
    if n >= 1_048_576:
        return f"{n / 1_048_576:.1f}M"
    if n >= 1024:
        return f"{n / 1024:.1f}K"
    return f"{n}B"


def _parse_ls_line(line: str) -> tuple[str, int, str] | None:
    """Return (file_type, size, name) or None."""
    m = _LS_DATE_RE.search(line)
    if not m:
        return None
    name = line[m.end():]
    before_date = line[:m.start()]
    before_parts = [p for p in before_date.split() if p]
    if len(before_parts) < 4:
        return None

    perms = before_parts[0]
    file_type = perms[0]

    size = 0
    for part in reversed(before_parts):
        if part.isdigit():
            size = int(part)
            break

    return file_type, size, name


def ls(text: str) -> str:
    """Compact ls -la output."""
    dirs: list[str] = []
    files: list[tuple[str, str]] = []
    by_ext: dict[str, int] = {}

    for line in text.split("\n"):
        if line.startswith("total ") or not line:
            continue
        parsed = _parse_ls_line(line)
        if not parsed:
            continue
        file_type, size, name = parsed
        if name in (".", ".."):
            continue
        if name in LS_NOISE_DIRS:
            continue

        if file_type == "d":
            dirs.append(name)
        elif file_type in ("-", "l"):
            dot = name.rfind(".")
            ext = name[dot:] if dot > 0 else "no ext"
            by_ext[ext] = by_ext.get(ext, 0) + 1
            files.append((name, _human_size(size)))

    if not dirs and not files:
        return text

    out = ""
    for d in dirs:
        out += f"{d}/\n"
    for name, size_str in files:
        out += f"{name}  {size_str}\n"

    summary = f"\nSummary: {len(files)} files, {len(dirs)} dirs"
    if by_ext:
        sorted_ext = sorted(by_ext.items(), key=lambda x: -x[1])
        parts = [f"{c} {e}" for e, c in sorted_ext[:LS_EXT_SUMMARY_TOP]]
        summary += f" ({', '.join(parts)}"
        if len(sorted_ext) > LS_EXT_SUMMARY_TOP:
            summary += f", +{len(sorted_ext) - LS_EXT_SUMMARY_TOP} more"
        summary += ")"

    return out + summary


ls.filter_name = FILTER_LS  # type: ignore[attr-defined]
