"""Focused manifest and generated-bundle contracts for R5.1."""

from __future__ import annotations

import json
import importlib.util
from pathlib import Path
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
UI_RUNTIME = ROOT / "frontend" / "ui-runtime"
CHECKER = ROOT / "scripts" / "check_ui_runtime_bundle.py"


def _load_checker() -> ModuleType:
    spec = importlib.util.spec_from_file_location("check_ui_runtime_bundle", CHECKER)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_r5_1_manifest_and_lock_use_exact_local_dependencies() -> None:
    manifest = json.loads((UI_RUNTIME / "package.json").read_text(encoding="utf-8"))
    lock = json.loads((UI_RUNTIME / "package-lock.json").read_text(encoding="utf-8"))
    lock_root = lock["packages"][""]

    assert lock["lockfileVersion"] == 3
    assert lock_root["dependencies"] == manifest["dependencies"]
    assert lock_root["devDependencies"] == manifest["devDependencies"]
    assert set(manifest["dependencies"]) == {
        "i18next",
        "i18next-icu",
        "intl-messageformat",
    }
    for version in (
        *manifest["dependencies"].values(),
        *manifest["devDependencies"].values(),
    ):
        assert version[0].isdigit(), f"dependency must be exactly pinned: {version}"
    for name, package in lock["packages"].items():
        if not name:
            continue
        assert package["resolved"].startswith("https://registry.npmjs.org/")
        assert package["integrity"].startswith("sha512-")


def test_r5_1_bundle_is_single_local_generated_artifact() -> None:
    manifest = json.loads((UI_RUNTIME / "package.json").read_text(encoding="utf-8"))
    build = manifest["scripts"]["build"]
    bundle = ROOT / "static" / "vendor" / "ui-runtime.bundle.js"

    assert "--format=iife" in build
    assert "static/vendor/ui-runtime.bundle.js" in build
    assert bundle.is_file()
    text = bundle.read_text(encoding="utf-8")
    assert "gatewayI18n" in text
    assert not any(
        host in text
        for host in ("unpkg.com", "cdn.jsdelivr.net", "cdnjs.cloudflare.com", "esm.sh")
    )
    assert not (ROOT / "static" / "i18n.js").exists()


def test_r5_1_reproducibility_checker_uses_canonical_clean_npm_builds() -> None:
    checker = _load_checker()
    manifest = json.loads((UI_RUNTIME / "package.json").read_text(encoding="utf-8"))

    assert manifest["scripts"]["build"] == checker.CANONICAL_BUILD_SCRIPT
    assert checker.CLEAN_INSTALL_COMMAND[:2] == ("npm", "ci")
    assert checker.CANONICAL_BUILD_COMMAND == ("npm", "run", "build")
    assert checker.NON_NETWORK_URL_LITERALS == ("http://www.w3.org/2000/svg",)
    assert checker.REMOTE_URL_PATTERN.search("https://cdn.example/runtime.js")
    assert checker.REMOTE_URL_PATTERN.search("//cdn.example/runtime.js")
    assert checker.REMOTE_URL_PATTERN.search(r"/path/.test(value)//ignored") is None
