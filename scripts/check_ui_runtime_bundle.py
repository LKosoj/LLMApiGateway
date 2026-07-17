#!/usr/bin/env python3
"""Verify the R5.1 bundle with two independent clean npm-lock builds."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
UI_RUNTIME = ROOT / "frontend" / "ui-runtime"
ARTIFACT = ROOT / "static" / "vendor" / "ui-runtime.bundle.js"
CANONICAL_BUILD_SCRIPT = (
    "esbuild src/entry.mjs --bundle --format=iife "
    "--global-name=GatewayI18nBundle --target=es2020 --minify "
    "--legal-comments=none --outfile=../../static/vendor/ui-runtime.bundle.js"
)
CLEAN_INSTALL_COMMAND = (
    "npm",
    "ci",
    "--ignore-scripts",
    "--no-audit",
    "--no-fund",
)
CANONICAL_BUILD_COMMAND = ("npm", "run", "build")
REMOTE_URL_PATTERN = re.compile(
    r"(?:https?://[A-Za-z0-9]|//[A-Za-z0-9][A-Za-z0-9.-]*\.[A-Za-z]{2,})",
    re.IGNORECASE,
)
NON_NETWORK_URL_LITERALS = ("http://www.w3.org/2000/svg",)
COMMAND_TIMEOUT_SECONDS = 120


class CheckFailure(RuntimeError):
    pass


def _digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def _run(command: tuple[str, ...], cwd: Path) -> None:
    try:
        completed = subprocess.run(
            command,
            cwd=cwd,
            check=False,
            capture_output=True,
            text=True,
            timeout=COMMAND_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckFailure(f"command failed to start or timed out: {' '.join(command)}") from error
    if completed.returncode != 0:
        detail = completed.stderr.strip() or completed.stdout.strip()
        raise CheckFailure(f"command failed: {' '.join(command)}: {detail}")


def _copy_build_inputs(temp_root: Path) -> Path:
    target = temp_root / "frontend" / "ui-runtime"
    target.mkdir(parents=True)
    for name in ("package.json", "package-lock.json"):
        shutil.copy2(UI_RUNTIME / name, target / name)
    shutil.copytree(UI_RUNTIME / "src", target / "src")
    locale_target = temp_root / "static" / "locales"
    locale_target.mkdir(parents=True)
    shutil.copy2(ROOT / "static" / "locales" / "registry.json", locale_target / "registry.json")
    (temp_root / "static" / "vendor").mkdir()
    return target


def _clean_build() -> bytes:
    with tempfile.TemporaryDirectory(prefix="ui-runtime-clean-build-") as temp_dir:
        temp_root = Path(temp_dir)
        target = _copy_build_inputs(temp_root)
        _run(CLEAN_INSTALL_COMMAND, target)
        _run(CANONICAL_BUILD_COMMAND, target)
        return (temp_root / "static" / "vendor" / "ui-runtime.bundle.js").read_bytes()


def _validate_runtime_assets() -> None:
    second_runtime = ROOT / "static" / "i18n.js"
    if second_runtime.exists():
        raise CheckFailure("parallel handwritten runtime is forbidden: static/i18n.js")
    paths = [ARTIFACT, *sorted((UI_RUNTIME / "src").glob("*.mjs"))]
    for path in paths:
        text = path.read_text(encoding="utf-8")
        for literal in NON_NETWORK_URL_LITERALS:
            text = text.replace(literal, "")
        if REMOTE_URL_PATTERN.search(text):
            raise CheckFailure(f"remote runtime URL is forbidden: {path.relative_to(ROOT)}")


def main() -> int:
    prerequisites = [
        UI_RUNTIME / "package.json",
        UI_RUNTIME / "package-lock.json",
        UI_RUNTIME / "src" / "entry.mjs",
        ROOT / "static" / "locales" / "registry.json",
        ARTIFACT,
    ]
    missing = [path for path in prerequisites if not path.is_file()]
    if missing:
        rendered = ", ".join(str(path.relative_to(ROOT)) for path in missing)
        print(f"UI runtime check prerequisites are missing: {rendered}", file=sys.stderr)
        return 2
    try:
        manifest = json.loads((UI_RUNTIME / "package.json").read_text(encoding="utf-8"))
        if manifest.get("scripts", {}).get("build") != CANONICAL_BUILD_SCRIPT:
            raise CheckFailure("package build script does not match the canonical command")
        _validate_runtime_assets()
        first = _clean_build()
        second = _clean_build()
    except (CheckFailure, OSError, UnicodeError, json.JSONDecodeError) as error:
        print(str(error), file=sys.stderr)
        return 1

    if first != second:
        print("UI runtime build is not deterministic across clean installs", file=sys.stderr)
        return 1
    committed = ARTIFACT.read_bytes()
    if committed != first:
        print(
            "committed UI runtime bundle is stale; run `npm run build` in frontend/ui-runtime",
            file=sys.stderr,
        )
        return 1
    print(
        "UI runtime bundle is reproducible from two clean npm-lock installs "
        f"and matches the committed artifact ({_digest(committed)})"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
