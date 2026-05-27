"""Find output compressor."""

from ..constants import FIND_PER_DIR_MAX, FIND_TOTAL_DIR_MAX, FILTER_FIND


def find(text: str) -> str:
    """Group find output by parent dir, cap files per dir."""
    lines = [line for line in text.split("\n") if line.strip()]
    if not lines:
        return text

    by_dir: dict[str, list[str]] = {}

    for path in lines:
        last_slash = path.rfind("/")
        if last_slash == -1:
            dir_ = "."
            basename = path
        else:
            dir_ = path[:last_slash] or "/"
            basename = path[last_slash + 1:]
        if dir_ not in by_dir:
            by_dir[dir_] = []
        by_dir[dir_].append(basename)

    dirs = sorted(by_dir.keys())
    out = f"{len(lines)} files in {len(dirs)} dirs:\n\n"

    show_dirs = dirs[:FIND_TOTAL_DIR_MAX]
    for dir_ in show_dirs:
        files = by_dir[dir_]
        out += f"{dir_}/ ({len(files)}):\n"
        for f in files[:FIND_PER_DIR_MAX]:
            out += f"  {f}\n"
        if len(files) > FIND_PER_DIR_MAX:
            out += f"  +{len(files) - FIND_PER_DIR_MAX}\n"
        out += "\n"

    if len(dirs) > FIND_TOTAL_DIR_MAX:
        out += f"+{len(dirs) - FIND_TOTAL_DIR_MAX} more dirs\n"

    return out


find.filter_name = FILTER_FIND  # type: ignore[attr-defined]
