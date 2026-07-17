#!/usr/bin/env python3
"""Verify that the committed topology assets are reproducible from the npm lock."""

from __future__ import annotations

import hashlib
import subprocess
import sys
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOPOLOGY_DIR = ROOT / "frontend" / "topology"
VENDOR_DIR = ROOT / "static" / "vendor"
ENTRYPOINT = TOPOLOGY_DIR / "entry.mjs"
ESBUILD = TOPOLOGY_DIR / "node_modules" / ".bin" / "esbuild"
ARTIFACT_NAMES = ("topology.bundle.mjs", "topology.bundle.css")


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(output_dir: Path) -> dict[str, str]:
    output_file = output_dir / "topology.bundle.mjs"
    subprocess.run(
        [
            str(ESBUILD),
            str(ENTRYPOINT),
            "--bundle",
            "--format=esm",
            "--target=es2020",
            "--minify",
            f"--outfile={output_file}",
            "--log-level=warning",
        ],
        cwd=TOPOLOGY_DIR,
        check=True,
    )
    return {name: _digest(output_dir / name) for name in ARTIFACT_NAMES}


def main() -> int:
    missing_inputs = [
        path
        for path in (TOPOLOGY_DIR / "package-lock.json", ESBUILD)
        if not path.is_file()
    ]
    if missing_inputs:
        rendered = ", ".join(str(path.relative_to(ROOT)) for path in missing_inputs)
        print(
            f"topology check prerequisites are missing: {rendered}; run "
            "`npm ci` in frontend/topology first",
            file=sys.stderr,
        )
        return 2

    missing_artifacts = [
        VENDOR_DIR / name
        for name in ARTIFACT_NAMES
        if not (VENDOR_DIR / name).is_file()
    ]
    if missing_artifacts:
        rendered = ", ".join(
            str(path.relative_to(ROOT)) for path in missing_artifacts
        )
        print(f"committed topology artifacts are missing: {rendered}", file=sys.stderr)
        return 2

    committed = {name: _digest(VENDOR_DIR / name) for name in ARTIFACT_NAMES}
    with tempfile.TemporaryDirectory(prefix="topology-build-a-") as first_dir:
        first = _build(Path(first_dir))
    with tempfile.TemporaryDirectory(prefix="topology-build-b-") as second_dir:
        second = _build(Path(second_dir))

    if first != second:
        print("topology build is not deterministic across consecutive runs", file=sys.stderr)
        return 1

    drifted = [name for name in ARTIFACT_NAMES if committed[name] != first[name]]
    if drifted:
        print(
            "committed topology artifacts are stale: " + ", ".join(drifted),
            file=sys.stderr,
        )
        print(
            "run `npm run build` in frontend/topology and commit both artifacts",
            file=sys.stderr,
        )
        return 1

    print("topology bundle is reproducible and matches committed artifacts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
