"""Hermetic import-order probe for runtime-boundary tests."""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def assert_fresh_import_order(
    repo_root: Path,
    first: str,
    second: str,
    *,
    timeout: int = 20,
) -> None:
    root = repo_root.resolve()
    script = (
        "import importlib, pathlib, sys\n"
        f"root = pathlib.Path({str(root)!r}).resolve()\n"
        "sys.path.insert(0, str(root))\n"
        f"names = ({first!r}, {second!r})\n"
        "assert all(name not in sys.modules for name in names)\n"
        "for name in names:\n"
        "    module = importlib.import_module(name)\n"
        "    origin = pathlib.Path(module.__file__).resolve()\n"
        "    assert origin.is_relative_to(root), (name, origin)\n"
    )
    result = subprocess.run(
        [sys.executable, "-I", "-W", "error::ResourceWarning", "-c", script],
        cwd=root,
        env={},
        capture_output=True,
        text=True,
        timeout=timeout,
        check=False,
    )
    if result.returncode != 0 or result.stderr:
        raise AssertionError(f"isolated import failed (exit={result.returncode}): {result.stderr}")
