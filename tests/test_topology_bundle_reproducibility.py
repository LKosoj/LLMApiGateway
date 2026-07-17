"""Contract tests for the locally vendored topology bundle."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "frontend" / "topology" / "package.json"
LOCK_PATH = ROOT / "frontend" / "topology" / "package-lock.json"
CHECK_SCRIPT_PATH = ROOT / "scripts" / "check_topology_bundle.py"


def _load_check_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location(
        "check_topology_bundle", CHECK_SCRIPT_PATH
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_topology_lock_matches_manifest_and_pins_integrity() -> None:
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    lock = json.loads(LOCK_PATH.read_text(encoding="utf-8"))
    lock_root = lock["packages"][""]

    assert lock["lockfileVersion"] == 3
    assert lock_root["dependencies"] == manifest["dependencies"]
    assert lock_root["devDependencies"] == manifest["devDependencies"]
    assert manifest["scripts"]["check"] == (
        "python3 ../../scripts/check_topology_bundle.py"
    )

    for dependency in (*manifest["dependencies"].values(), *manifest["devDependencies"].values()):
        assert dependency[0].isdigit(), f"dependency must be exactly pinned: {dependency}"

    for name, package in lock["packages"].items():
        if not name:
            continue
        assert package["resolved"].startswith("https://registry.npmjs.org/")
        assert package["integrity"].startswith("sha512-")


def test_topology_bundle_has_no_remote_runtime_fallback() -> None:
    entrypoint = (ROOT / "frontend" / "topology" / "entry.mjs").read_text(
        encoding="utf-8"
    )
    consumer = (ROOT / "static" / "usage-stats.js").read_text(encoding="utf-8")
    forbidden_hosts = ("esm.sh", "unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com")

    assert not any(host in entrypoint for host in forbidden_hosts)
    assert not any(host in consumer for host in forbidden_hosts)
    assert "import('/static/vendor/topology.bundle.mjs')" in consumer


def test_topology_check_rejects_committed_artifact_drift(
    tmp_path: Path, monkeypatch
) -> None:
    checker = _load_check_script()
    topology_dir = tmp_path / "frontend" / "topology"
    vendor_dir = tmp_path / "static" / "vendor"
    esbuild = topology_dir / "node_modules" / ".bin" / "esbuild"
    topology_dir.mkdir(parents=True)
    vendor_dir.mkdir(parents=True)
    esbuild.parent.mkdir(parents=True)
    (topology_dir / "package-lock.json").write_text("{}", encoding="utf-8")
    esbuild.write_text("", encoding="utf-8")
    for name in checker.ARTIFACT_NAMES:
        (vendor_dir / name).write_bytes(b"committed")

    monkeypatch.setattr(checker, "ROOT", tmp_path)
    monkeypatch.setattr(checker, "TOPOLOGY_DIR", topology_dir)
    monkeypatch.setattr(checker, "VENDOR_DIR", vendor_dir)
    monkeypatch.setattr(checker, "ESBUILD", esbuild)
    monkeypatch.setattr(
        checker,
        "_build",
        lambda _output_dir: {name: "generated" for name in checker.ARTIFACT_NAMES},
    )

    assert checker.main() == 1
