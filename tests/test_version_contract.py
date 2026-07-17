from __future__ import annotations

import logging
import os
import re
import shutil
import subprocess
import sys
import tomllib
import zipfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import main
from llm_gateway_core.build_info import build_metadata
from llm_gateway_core.version import __version__
from tests._async_compat import run_async


PROJECT_ROOT = Path(__file__).resolve().parents[1]
VERSION_SCRIPT = PROJECT_ROOT / "scripts" / "check_product_version.py"
VERSIONED_HTML = (
    PROJECT_ROOT / "static" / "rules-editor.html",
    PROJECT_ROOT / "static" / "usage-stats.html",
    PROJECT_ROOT / "static" / "pricing.html",
)


def test_canonical_version_drives_package_and_openapi_metadata() -> None:
    with (PROJECT_ROOT / "pyproject.toml").open("rb") as handle:
        pyproject = tomllib.load(handle)

    assert __version__ == "1.10.0"
    assert re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", __version__)
    assert "version" not in pyproject["project"]
    assert "version" in pyproject["project"]["dynamic"]
    assert pyproject["tool"]["setuptools"]["dynamic"]["version"] == {
        "attr": "llm_gateway_core.version.__version__"
    }
    assert main.app.version == __version__
    assert main.app.openapi()["info"]["version"] == __version__


def test_build_metadata_uses_canonical_version_and_independent_build_fields(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLMGATEWAY_BUILD_VERSION", "must-be-ignored")
    monkeypatch.setenv("LLMGATEWAY_BUILD_SHA", "abc123")
    monkeypatch.setenv("LLMGATEWAY_BUILD_DATE", "2026-07-11T00:00:00Z")
    monkeypatch.setenv("LLMGATEWAY_BUILD_REF", "refs/tags/v1.10.0")

    assert build_metadata() == {
        "version": __version__,
        "sha": "abc123",
        "date": "2026-07-11T00:00:00Z",
        "ref": "refs/tags/v1.10.0",
    }


def test_health_reports_canonical_version_header_before_startup(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLMGATEWAY_BUILD_VERSION", "must-be-ignored")
    client = TestClient(main.app)

    response = client.get("/health")

    assert response.status_code == 503
    assert response.json()["status"] == "error"
    assert response.json()["phase"] == "starting"
    assert response.json()["version"] == __version__
    assert response.headers["X-LLMGateway-Build-Version"] == __version__


def test_startup_log_reports_canonical_version(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(main.settings, "gateway_api_key", "")

    async def attempt_startup() -> None:
        async with main.lifespan(main.app):
            raise AssertionError("lifespan must fail before yielding")

    with caplog.at_level(logging.INFO), pytest.raises(
        RuntimeError, match="GATEWAY_API_KEY"
    ):
        run_async(attempt_startup())

    assert f"LLM Gateway v{__version__}: Application startup..." in caplog.text


def test_product_version_script_prints_and_rejects_missing_or_mismatched_values() -> None:
    printed = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--print"],
        cwd=PROJECT_ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    mismatch = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--expected", "0.0.0"],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    missing = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--expected", ""],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    padded = subprocess.run(
        [sys.executable, str(VERSION_SCRIPT), "--expected", f" {__version__} "],
        cwd=PROJECT_ROOT,
        check=False,
        capture_output=True,
        text=True,
    )

    assert printed.stdout.strip() == __version__
    assert mismatch.returncode == 1
    assert "product version mismatch" in mismatch.stderr
    assert missing.returncode == 1
    assert "must not be empty" in missing.stderr
    assert padded.returncode == 1
    assert "must not contain surrounding whitespace" in padded.stderr


def test_wheel_metadata_and_installed_package_use_canonical_version(
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "source"
    wheel_dir = tmp_path / "wheel"
    install_dir = tmp_path / "installed"
    source_root.mkdir()
    wheel_dir.mkdir()
    shutil.copy2(PROJECT_ROOT / "pyproject.toml", source_root / "pyproject.toml")
    shutil.copy2(PROJECT_ROOT / "README.md", source_root / "README.md")
    shutil.copytree(PROJECT_ROOT / "llm_gateway_core", source_root / "llm_gateway_core")

    built = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "wheel",
            "--no-deps",
            "--no-build-isolation",
            "--wheel-dir",
            str(wheel_dir),
            str(source_root),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert built.returncode == 0, built.stdout + built.stderr
    wheel_path = next(wheel_dir.glob("llmapigateway-*.whl"))
    with zipfile.ZipFile(wheel_path) as archive:
        metadata_name = next(
            name for name in archive.namelist() if name.endswith(".dist-info/METADATA")
        )
        metadata = archive.read(metadata_name).decode("utf-8")
    assert f"Version: {__version__}\n" in metadata

    installed = subprocess.run(
        [
            sys.executable,
            "-m",
            "pip",
            "install",
            "--no-deps",
            "--target",
            str(install_dir),
            str(wheel_path),
        ],
        cwd=tmp_path,
        check=False,
        capture_output=True,
        text=True,
    )
    assert installed.returncode == 0, installed.stdout + installed.stderr
    env = os.environ.copy()
    env["PYTHONPATH"] = str(install_dir)
    verified = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "from importlib.metadata import version; "
                "from llm_gateway_core.version import __version__; "
                f"assert __version__ == version('llmapigateway') == {__version__!r}"
            ),
        ],
        cwd=tmp_path,
        env=env,
        check=False,
        capture_output=True,
        text=True,
    )
    assert verified.returncode == 0, verified.stdout + verified.stderr


def test_ui_version_markers_have_no_hardcoded_product_version() -> None:
    for html_path in VERSIONED_HTML:
        html = html_path.read_text(encoding="utf-8")
        assert 'data-product-version' in html
        assert "v1.10" not in html

    ui_core = (PROJECT_ROOT / "frontend" / "ui-runtime" / "src" / "ui-core.mjs").read_text(
        encoding="utf-8"
    )
    runtime_entry = (
        PROJECT_ROOT / "frontend" / "ui-runtime" / "src" / "entry.mjs"
    ).read_text(encoding="utf-8")
    assert not (PROJECT_ROOT / "static" / "ui-core.js").exists()
    assert 'querySelectorAll("[data-product-version]")' in ui_core
    assert "if (elements.length === 0)" in ui_core
    assert 'fetchFunction("/health"' in ui_core
    assert "X-LLMGateway-Build-Version" in ui_core
    assert "gateway:product-version-error" in ui_core
    assert "console.error" in ui_core
    assert "1.10" not in ui_core
    assert "loadProductVersion" in runtime_entry


def test_docker_version_contract_is_required_and_never_becomes_runtime_config() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")
    compose = (PROJECT_ROOT / "docker-compose.yml").read_text(encoding="utf-8")

    assert "AS version-contract" in dockerfile
    assert "ARG LLMGATEWAY_EXPECTED_PRODUCT_VERSION\n" in dockerfile
    assert "check_product_version.py --expected" in dockerfile
    assert 'org.opencontainers.image.version="${LLMGATEWAY_EXPECTED_PRODUCT_VERSION}"' in dockerfile
    assert "LLMGATEWAY_BUILD_VERSION" not in dockerfile
    assert not re.search(r"^ENV .*LLMGATEWAY_EXPECTED_PRODUCT_VERSION", dockerfile, re.MULTILINE)
    assert dockerfile.index("AS version-contract") < dockerfile.index("apt-get update")

    expected_contract = (
        "${LLMGATEWAY_EXPECTED_PRODUCT_VERSION:?derive it with "
        "python3 scripts/check_product_version.py --print}"
    )
    assert expected_contract in compose
    assert "LLMGATEWAY_EXPECTED_PRODUCT_VERSION: 1.10.0" not in compose
