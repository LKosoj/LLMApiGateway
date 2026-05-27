"""Git status formatter."""

import re

from ..constants import STATUS_MAX_FILES, STATUS_MAX_UNTRACKED, FILTER_GIT_STATUS

_PORCELAIN_RE = re.compile(r"^[ MADRCU?!][ MADRCU?!] \S")
_LONG_MATCH_RE = re.compile(r"^\s*(modified|new file|deleted|renamed|both modified):\s+(.+)$")
_BRANCH_RE = re.compile(r"^On branch (\S+)")


def git_status(text: str) -> str:
    """Parse git status output and format compactly."""
    lines = text.split("\n")
    if not lines or (len(lines) == 1 and not lines[0].strip()):
        return "Clean working tree"

    branch = ""
    staged_files: list[str] = []
    modified_files: list[str] = []
    untracked_files: list[str] = []
    staged = 0
    modified = 0
    untracked = 0
    conflicts = 0

    for raw in lines:
        if not raw.strip():
            continue

        m = _BRANCH_RE.match(raw)
        if m:
            branch = m.group(1)
            continue

        if raw.startswith("##"):
            branch = raw[2:].strip()
            continue

        if len(raw) >= 3 and _PORCELAIN_RE.match(raw):
            x = raw[0]
            y = raw[1]
            file_ = raw[3:]

            if raw[:2] == "??":
                untracked += 1
                untracked_files.append(file_)
                continue

            if x in "MADRC":
                staged += 1
                staged_files.append(file_)
            elif x == "U":
                conflicts += 1

            if y in ("M", "D"):
                modified += 1
                modified_files.append(file_)
            continue

        m2 = _LONG_MATCH_RE.match(raw)
        if m2:
            kind = m2.group(1)
            path = m2.group(2).strip()
            if kind == "both modified":
                conflicts += 1
            elif kind in ("modified", "deleted"):
                modified += 1
                modified_files.append(path)
            elif kind in ("new file", "renamed"):
                staged += 1
                staged_files.append(path)
            continue

    out = ""
    if branch:
        out += f"* {branch}\n"

    if staged > 0:
        out += f"+ Staged: {staged} files\n"
        for f in staged_files[:STATUS_MAX_FILES]:
            out += f"   {f}\n"
        if len(staged_files) > STATUS_MAX_FILES:
            out += f"   ... +{len(staged_files) - STATUS_MAX_FILES} more\n"

    if modified > 0:
        out += f"~ Modified: {modified} files\n"
        for f in modified_files[:STATUS_MAX_FILES]:
            out += f"   {f}\n"
        if len(modified_files) > STATUS_MAX_FILES:
            out += f"   ... +{len(modified_files) - STATUS_MAX_FILES} more\n"

    if untracked > 0:
        out += f"? Untracked: {untracked} files\n"
        for f in untracked_files[:STATUS_MAX_UNTRACKED]:
            out += f"   {f}\n"
        if len(untracked_files) > STATUS_MAX_UNTRACKED:
            out += f"   ... +{len(untracked_files) - STATUS_MAX_UNTRACKED} more\n"

    if conflicts > 0:
        out += f"conflicts: {conflicts} files\n"

    if staged == 0 and modified == 0 and untracked == 0 and conflicts == 0:
        out += "clean — nothing to commit\n"

    return out.rstrip("\n")


git_status.filter_name = FILTER_GIT_STATUS  # type: ignore[attr-defined]
