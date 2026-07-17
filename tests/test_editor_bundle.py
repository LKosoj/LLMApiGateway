from __future__ import annotations

import json
import subprocess
from pathlib import Path

from scripts.check_editor_bundle import find_unexpected_editor_assets


ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "frontend" / "editor"
BUNDLE = ROOT / "static" / "editor.js"


def test_editor_bundle_is_generated_from_pinned_source_package() -> None:
    manifest = json.loads((PACKAGE / "package.json").read_text(encoding="utf-8"))

    assert manifest["devDependencies"] == {"esbuild": "0.28.1"}
    assert BUNDLE.read_text(encoding="utf-8").startswith(
        "/* Generated from frontend/editor/src; do not edit directly. */\n"
    )


def test_editor_bundle_is_current_and_has_no_extra_chunks() -> None:
    result = subprocess.run(
        ["python3", str(ROOT / "scripts" / "check_editor_bundle.py")],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stdout + result.stderr
    assert find_unexpected_editor_assets(ROOT / "static") == []


def test_editor_bundle_checker_rejects_a_stale_source_map(tmp_path: Path) -> None:
    (tmp_path / "editor.css").write_text("", encoding="utf-8")
    (tmp_path / "editor.js").write_text("", encoding="utf-8")
    (tmp_path / "editor.js.map").write_text("{}", encoding="utf-8")

    assert find_unexpected_editor_assets(tmp_path) == ["editor.js.map"]


def test_editor_entry_keeps_same_path_and_single_initialization_listener() -> None:
    html = (ROOT / "static" / "rules-editor.html").read_text(encoding="utf-8")
    bundle = BUNDLE.read_text(encoding="utf-8")

    assert '<script src="/static/editor.js"></script>' in html
    assert bundle.count("DOMContentLoaded") == 1
    assert "src/index.mjs" in bundle
