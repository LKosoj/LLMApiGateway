"""Build output compressor."""

import re

from ..constants import FILTER_BUILD_OUTPUT

# Cargo/rustc error continuation: " --> file:line", "  |", "N | code", "  = note: ..."
_RE_CARGO_ERR_CONT = re.compile(r"^\s*(-->|\||\d+\s*\||=)")
_DEPRECATION_KEEP = 3


def build_output(text: str) -> str:
    """Filter verbose build output: keep errors, warnings, summary; strip progress noise."""
    lines = text.split("\n")
    errors: list[str] = []
    warnings: list[str] = []
    deprecations: list[str] = []
    summary: str | None = None
    compiling_count = 0
    downloading_count = 0
    in_cargo_error = False
    in_cargo_warning = False

    for line in lines:
        trimmed = line.strip()

        if in_cargo_error or in_cargo_warning:
            if not trimmed:
                in_cargo_error = False
                in_cargo_warning = False
                continue
            if _RE_CARGO_ERR_CONT.match(line):
                if in_cargo_warning:
                    warnings.append(line)
                else:
                    errors.append(line)
                continue
            in_cargo_error = False
            in_cargo_warning = False

        if not trimmed:
            continue

        if re.match(r"^npm (ERR!|error)", trimmed, re.IGNORECASE) or re.match(r"^yarn error", trimmed, re.IGNORECASE):
            errors.append(line)
            continue

        if re.match(r"^npm warn deprecated", trimmed, re.IGNORECASE):
            deprecations.append(line)
            continue

        if re.match(r"^npm warn", trimmed, re.IGNORECASE) or re.match(r"^yarn warn", trimmed, re.IGNORECASE):
            warnings.append(line)
            continue

        if re.match(r"^error(\[|:)", trimmed, re.IGNORECASE) or trimmed.startswith("error -->"):
            errors.append(line)
            in_cargo_error = True
            continue

        if re.match(r"^warning(\[|:)", trimmed, re.IGNORECASE) or trimmed.startswith("warning -->"):
            warnings.append(line)
            in_cargo_warning = True
            continue

        if re.match(r"^ERROR:", trimmed, re.IGNORECASE):
            errors.append(line)
            continue

        if re.match(r"^\[ERROR\]", trimmed, re.IGNORECASE) or re.match(r"^BUILD FAILED", trimmed, re.IGNORECASE):
            errors.append(line)
            continue

        if re.match(r"^\[WARNING\]", trimmed, re.IGNORECASE):
            warnings.append(line)
            continue

        if re.match(r"^\s*Compiling\s+\S+", trimmed, re.IGNORECASE):
            compiling_count += 1
            continue

        if re.match(r"^\s*Downloading\s+\S+", trimmed, re.IGNORECASE) or re.match(r"^Fetching\s+", trimmed, re.IGNORECASE):
            downloading_count += 1
            continue

        if (
            re.match(r"^(added|removed|changed|audited|installed)\s+\d+\s+package", trimmed, re.IGNORECASE)
            or re.match(r"^\s*Finished\s+", trimmed, re.IGNORECASE)
            or re.match(r"^BUILD SUCCESS", trimmed, re.IGNORECASE)
            or re.match(r"^\d+\s+(vulnerabilities|packages?|warnings?|errors?)", trimmed, re.IGNORECASE)
            or re.match(r"^Successfully (installed|built)", trimmed, re.IGNORECASE)
            or re.match(r"^To address .* issues", trimmed, re.IGNORECASE)
            or re.match(r"^Run `npm (audit|fund)`", trimmed, re.IGNORECASE)
            or re.search(r"packages are looking for funding", trimmed, re.IGNORECASE)
        ):
            summary = f"{summary}\n{line}" if summary else line
            continue

    out = ""

    for d in deprecations[:_DEPRECATION_KEEP]:
        out += f"{d}\n"
    if len(deprecations) > _DEPRECATION_KEEP:
        out += f"... +{len(deprecations) - _DEPRECATION_KEEP} more deprecated packages\n"

    if compiling_count > 0:
        out += f"Compiled {compiling_count} packages\n"
    if downloading_count > 0:
        out += f"Downloaded {downloading_count} packages\n"

    for e in errors:
        out += f"{e}\n"

    for w in warnings[:5]:
        out += f"{w}\n"
    if len(warnings) > 5:
        out += f"... +{len(warnings) - 5} more warnings\n"

    if summary:
        out += f"{summary}\n"

    return out.rstrip("\n") or text


build_output.filter_name = FILTER_BUILD_OUTPUT  # type: ignore[attr-defined]
