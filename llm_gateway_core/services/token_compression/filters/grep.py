"""Grep output compressor."""

from ..constants import GREP_PER_FILE_MAX, FILTER_GREP


def grep(text: str) -> str:
    """Compact grep output: group by file, cap matches per file."""
    by_file: dict[str, list[tuple[str, str]]] = {}
    total = 0

    for line in text.split("\n"):
        first = line.find(":")
        if first == -1:
            continue
        second = line.find(":", first + 1)
        if second == -1:
            continue
        file_ = line[:first]
        line_num_str = line[first + 1:second]
        content = line[second + 1:]
        if not line_num_str.isdigit():
            continue
        total += 1
        if file_ not in by_file:
            by_file[file_] = []
        by_file[file_].append((line_num_str, content))

    if total == 0:
        return text

    files = sorted(by_file.keys())
    out = f"{total} matches in {len(files)}F:\n\n"

    for file_ in files:
        matches = by_file[file_]
        out += f"[file] {file_} ({len(matches)}):\n"
        for line_num, content in matches[:GREP_PER_FILE_MAX]:
            out += f"  {line_num.rjust(4)}: {content.strip()}\n"
        if len(matches) > GREP_PER_FILE_MAX:
            out += f"  +{len(matches) - GREP_PER_FILE_MAX}\n"
        out += "\n"

    return out


grep.filter_name = FILTER_GREP  # type: ignore[attr-defined]
