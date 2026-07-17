from __future__ import annotations

import subprocess
import tempfile
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "frontend" / "editor"
BUNDLE = ROOT / "static" / "editor.js"
ESBUILD = PACKAGE / "node_modules" / ".bin" / "esbuild"
BANNER = "/* Generated from frontend/editor/src; do not edit directly. */"
ALLOWED_EDITOR_ASSETS = frozenset({"editor.css", "editor.js"})


def find_unexpected_editor_assets(static_dir: Path) -> list[str]:
    return sorted(
        path.name
        for path in static_dir.glob("editor*")
        if path.is_file() and path.name not in ALLOWED_EDITOR_ASSETS
    )


def main() -> int:
    if not ESBUILD.is_file():
        raise SystemExit("frontend/editor dependencies are missing; run npm ci")

    bundle = BUNDLE.read_bytes()
    if not bundle.startswith(f"{BANNER}\n".encode()):
        raise SystemExit("static/editor.js is not the generated Rules Editor bundle")

    with tempfile.TemporaryDirectory(prefix="llmgateway-editor-bundle-") as directory:
        candidate = Path(directory) / "editor.js"
        subprocess.run(
            [
                str(ESBUILD),
                "src/index.mjs",
                "--bundle",
                "--format=iife",
                "--target=es2020",
                "--legal-comments=none",
                "--charset=utf8",
                f"--banner:js={BANNER}",
                f"--outfile={candidate}",
            ],
            cwd=PACKAGE,
            check=True,
        )
        if candidate.read_bytes() != bundle:
            raise SystemExit("static/editor.js is stale; run npm run build")

    unexpected = find_unexpected_editor_assets(BUNDLE.parent)
    if unexpected:
        raise SystemExit(f"unexpected Rules Editor chunks: {', '.join(unexpected)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
