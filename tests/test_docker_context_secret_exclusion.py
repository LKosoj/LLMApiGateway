from __future__ import annotations

import io
import json
import shutil
import subprocess
import tarfile
import uuid
from pathlib import Path, PurePosixPath

import pytest


PROJECT_ROOT = Path(__file__).resolve().parents[1]
EXACT_RUNTIME_CONTEXT_ROOTS = (
    "llm_gateway_core",
    "static",
    "examples",
    "docker",
    "scripts",
)
ROOT_CONFIGS = (
    "providers.json",
    "models_fallback_rules.json",
    "models_operation_rules.json",
    "models_fusion_rules.json",
    "models_router_rules.json",
    "models_model_rules.json",
)
ROOT_DEV_STATE_DIRECTORIES = (
    ".claude",
    ".cli-proxy",
    ".agents",
    ".attachments",
    ".playwright",
    ".playwright-cli",
    ".pytest_cache",
    ".ruff_cache",
    ".qwen",
)
NESTED_NON_RUNTIME_DIRECTORIES = (
    "tests",
    "node_modules",
    "diagnostic",
    "diagnostics",
    "cache",
    "caches",
)
SECRET_LIKE_FILENAMES = (
    ".env",
    ".env.local",
    ".netrc",
    ".npmrc",
    ".pypirc",
    "credentials.json",
    "secrets.json",
    "private.key",
    "private.pem",
    "private.p12",
    "private.pfx",
)
PRIVATE_IMAGE_STORAGE_CLI_MODULES = (
    "llm_gateway_core/services/_image_storage_cli_archive.py",
    "llm_gateway_core/services/_image_storage_cli_copy.py",
    "llm_gateway_core/services/_image_storage_cli_inventory.py",
)
ADVERSARIAL_CONTEXT_PATHS = (
    "static/dev-diagnostic.txt",
    "llm_gateway_core/debug_dump.txt",
    "llm_gateway_core/id_rsa",
    "llm_gateway_core/service-account.json",
    "static/playwright-report/index.html",
    "llm_gateway_core/.arbitrary-hidden",
    "static/.arbitrary-hidden",
    "llm_gateway_core/rogue.py",
    "static/rogue.html",
    "docker/id_rsa",
    "docker/nested/service-account.json",
    "docker/rogue.sh",
    "scripts/nested/id_rsa",
    "scripts/rogue.py",
    "examples/arbitrary-child.md",
    "static/login.html/nested/id_rsa",
    "static/login.html/nested/rogue.js",
    "llm_gateway_core/__init__.py/nested/service-account.json",
    "llm_gateway_core/__init__.py/nested/rogue.py",
    "llm_gateway_core/services/_image_storage_cli_inventory.py/nested/rogue.py",
    "llm_gateway_core/services/_image_storage_cli_rogue.py",
)


def _run_docker(*args: str, timeout: int = 120) -> subprocess.CompletedProcess[bytes]:
    completed = subprocess.run(
        ["docker", *args],
        cwd=PROJECT_ROOT,
        capture_output=True,
        check=False,
        timeout=timeout,
    )
    if completed.returncode != 0:
        pytest.fail(
            f"docker {' '.join(args[:2])} failed with rc={completed.returncode}: "
            f"{completed.stderr.decode(errors='replace')[-1000:]}"
        )
    return completed


def test_runtime_config_directory_is_anchored_in_git_and_docker_ignores() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    gitignore = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()

    assert dockerignore.count("/config/") == 1
    assert dockerignore.count("/.*/") == 1
    assert dockerignore.count("**/.*/") == 1
    assert gitignore.count("/config/") == 1
    for filename in ROOT_CONFIGS:
        assert dockerignore.count(f"/{filename}") == 1
        assert dockerignore.count(f"**/{filename}") == 1


def test_dockerfile_uses_pinned_hermetic_runtime_contract() -> None:
    dockerfile = (PROJECT_ROOT / "Dockerfile").read_text(encoding="utf-8")

    assert dockerfile.splitlines()[0] == (
        "# syntax=docker.io/docker/dockerfile:1.7-labs@sha256:"
        "b99fecfe00268a8b556fad7d9c37ee25d716ae08a5d7320e6d51c4dd83246894"
    )
    pinned_base = (
        "python:3.12-slim@sha256:"
        "46cb7cc2877e60fbd5e21a9ae6115c30ace7a077b9f8772da879e4590c18c2e3"
    )
    assert dockerfile.count(f"FROM {pinned_base}") == 3
    assert "COPY . /app" not in dockerfile
    assert "COPY . " not in dockerfile
    assert "COPY requirements-container.txt" in dockerfile
    assert "--require-hashes" in dockerfile
    assert "--only-binary=:all:" in dockerfile
    assert dockerfile.count("--no-binary=") == 1
    assert "--no-binary=docopt,langdetect,sgmllib3k" in dockerfile
    assert "--no-build-isolation" in dockerfile
    assert 'line.startswith("setuptools==")' in dockerfile
    assert "setuptools==83.0.0" not in dockerfile
    assert "pip install --no-cache-dir --upgrade" not in dockerfile
    assert "snapshot.debian.org/archive/debian/20260421T000000Z/" in dockerfile
    assert "snapshot.debian.org/archive/debian-security/20260421T000000Z/" in dockerfile
    assert "CLOAKBROWSER_AUTO_UPDATE=false" in dockerfile
    assert "RUN --mount=type=tmpfs,target=/tmp" in dockerfile
    assert (
        'CLOAKBROWSER_BINARY_PATH="/opt/cloakbrowser/'
        'chromium-146.0.7680.177.3/chrome"'
    ) in dockerfile
    assert "USER 10001:10001" in dockerfile


def test_dockerignore_is_default_deny_with_only_runtime_inputs_allowed() -> None:
    dockerignore = (PROJECT_ROOT / ".dockerignore").read_text(encoding="utf-8")

    patterns = [
        line
        for line in dockerignore.splitlines()
        if line and not line.startswith("#")
    ]
    assert patterns[0] == "**"
    assert "!main.py" in patterns
    assert "!requirements-container.txt" in patterns
    exact_negations = [pattern for pattern in patterns if pattern.startswith("!")]
    assert "!main.py" in exact_negations
    assert "!requirements-container.txt" in exact_negations
    for root in EXACT_RUNTIME_CONTEXT_ROOTS:
        assert f"!{root}/" in exact_negations
    assert all(
        not any(character in pattern for character in "*?[")
        for pattern in exact_negations
    )
    normalized_negations: dict[str, bool] = {}
    for pattern in exact_negations:
        relative = pattern.removeprefix("!")
        is_directory = relative.endswith("/")
        normalized = relative.rstrip("/")
        assert normalized not in normalized_negations
        target = PROJECT_ROOT / normalized
        assert target.is_dir() if is_directory else target.is_file()
        normalized_negations[normalized] = is_directory
    for source_path, is_directory in normalized_negations.items():
        negation = f"!{source_path}/" if is_directory else f"!{source_path}"
        negation_index = patterns.index(negation)
        assert patterns[negation_index + 1] == f"{source_path}/*"
        parent = PurePosixPath(source_path).parent
        while str(parent) != ".":
            assert normalized_negations.get(str(parent)) is True
            parent = parent.parent
    assert "!examples/free-tier-providers.md" in patterns
    assert "!docker/entrypoint.sh" in patterns
    assert "!docker/healthcheck.py" in patterns
    assert "!docker/install_cloakbrowser_asset.py" in patterns
    assert "!scripts/check_product_version.py" in patterns
    for module in PRIVATE_IMAGE_STORAGE_CLI_MODULES:
        assert f"!{module}" in patterns
        assert f"{module}/*" in patterns
    assert not any(pattern.startswith("!tests") for pattern in patterns)
    for directory_name in NESTED_NON_RUNTIME_DIRECTORIES:
        assert f"**/{directory_name}/" in patterns
    for pattern in (
        "**/test_*.py",
        "**/*_test.py",
        "**/*.pyd",
        "**/.coverage",
        "**/*.log",
        "**/*.db",
        "**/*.sqlite",
        "**/*.sqlite3",
        "**/.env",
        "**/.env.*",
        "**/credentials.json",
        "**/secrets.json",
        "**/*.key",
        "**/*.pem",
        "**/*.p12",
        "**/*.pfx",
    ):
        assert pattern in patterns


def test_synthetic_docker_context_excludes_config_paths_and_secret_bytes_from_every_layer(
    tmp_path: Path,
) -> None:
    if shutil.which("docker") is None:
        pytest.fail("docker executable is required for the layer sentinel")
    _run_docker("info", "--format", "{{.ServerVersion}}", timeout=30)

    context = tmp_path / "context"
    context.mkdir()
    shutil.copyfile(PROJECT_ROOT / ".dockerignore", context / ".dockerignore")
    (context / "Dockerfile.sentinel").write_text(
        "FROM scratch\nCOPY . /payload/\n",
        encoding="utf-8",
    )
    (context / "main.py").write_text("allowed sentinel\n", encoding="utf-8")
    allowed_files = {
        "payload/main.py",
        "payload/llm_gateway_core/version.py",
        "payload/static/theme.js",
    }
    (context / "llm_gateway_core").mkdir()
    (context / "llm_gateway_core" / "version.py").write_text(
        "runtime module\n",
        encoding="utf-8",
    )
    (context / "static").mkdir()
    (context / "static" / "theme.js").write_text(
        "runtime asset\n",
        encoding="utf-8",
    )
    (context / "arbitrary-checkout-file.txt").write_text(
        "must remain outside the context\n",
        encoding="utf-8",
    )
    config_dir = context / "config"
    config_dir.mkdir()
    canaries: list[bytes] = []
    for index, filename in enumerate(ROOT_CONFIGS):
        canary = f"R1_11_B_ROOT_SECRET_{index}_{uuid.uuid4().hex}".encode()
        canaries.append(canary)
        (context / filename).write_bytes(canary)
        nested_dir = context / "llm_gateway_core" / f"package-{index}" / "deep"
        nested_dir.mkdir(parents=True)
        nested_canary = (
            f"R1_11_B_NESTED_CONFIG_SECRET_{index}_{uuid.uuid4().hex}".encode()
        )
        canaries.append(nested_canary)
        (nested_dir / filename).write_bytes(nested_canary)
    nested_canary = f"R1_11_B_DIRECTORY_SECRET_{uuid.uuid4().hex}".encode()
    canaries.append(nested_canary)
    (config_dir / "providers.json").write_bytes(nested_canary)
    for index, directory_name in enumerate(ROOT_DEV_STATE_DIRECTORIES):
        dev_dir = context / directory_name
        dev_dir.mkdir()
        dev_canary = f"R1_11_B_DEV_STATE_{index}_{uuid.uuid4().hex}".encode()
        canaries.append(dev_canary)
        (dev_dir / "state.marker").write_bytes(dev_canary)
        nested_dev_dir = context / "static" / "dev-state" / directory_name
        nested_dev_dir.mkdir(parents=True)
        nested_dev_canary = (
            f"R1_11_B_NESTED_DEV_STATE_{index}_{uuid.uuid4().hex}".encode()
        )
        canaries.append(nested_dev_canary)
        (nested_dev_dir / "state.marker").write_bytes(nested_dev_canary)
    for root_name in ("llm_gateway_core", "static"):
        for index, directory_name in enumerate(NESTED_NON_RUNTIME_DIRECTORIES):
            directory = context / root_name / directory_name
            directory.mkdir(parents=True, exist_ok=True)
            canary = (
                f"R3_3_NESTED_NON_RUNTIME_{root_name}_{index}_{uuid.uuid4().hex}"
            ).encode()
            canaries.append(canary)
            (directory / "sentinel.bin").write_bytes(canary)
        for index, filename in enumerate(SECRET_LIKE_FILENAMES):
            canary = (
                f"R3_3_NESTED_SECRET_FILE_{root_name}_{index}_{uuid.uuid4().hex}"
            ).encode()
            canaries.append(canary)
            (context / root_name / filename).write_bytes(canary)
        for index, filename in enumerate(
            (
                "test_runtime.py",
                "runtime_test.py",
                "native.pyd",
                ".coverage",
                "debug.log",
                "state.db",
                "state.sqlite3",
            )
        ):
            canary = (
                f"R3_3_NESTED_DIAGNOSTIC_FILE_{root_name}_{index}_{uuid.uuid4().hex}"
            ).encode()
            canaries.append(canary)
            (context / root_name / filename).write_bytes(canary)
    for index, relative_path in enumerate(ADVERSARIAL_CONTEXT_PATHS):
        canary = f"R3_3_ADVERSARIAL_RUNTIME_{index}_{uuid.uuid4().hex}".encode()
        canaries.append(canary)
        target = context / relative_path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(canary)

    image = f"llmgateway-context-sentinel:{uuid.uuid4().hex}"
    archive = tmp_path / "image.tar"
    try:
        _run_docker(
            "build",
            "--file",
            str(context / "Dockerfile.sentinel"),
            "--tag",
            image,
            str(context),
            timeout=180,
        )
        _run_docker("save", "--output", str(archive), image, timeout=60)

        with tarfile.open(archive, mode="r") as image_tar:
            manifest_file = image_tar.extractfile("manifest.json")
            assert manifest_file is not None
            manifest = json.load(manifest_file)
            assert len(manifest) == 1
            layer_names = manifest[0]["Layers"]
            assert layer_names
            allowed_seen: set[str] = set()
            for layer_name in layer_names:
                extracted = image_tar.extractfile(layer_name)
                assert extracted is not None
                layer_bytes = extracted.read()
                for canary in canaries:
                    assert canary not in layer_bytes
                normalized_names: set[str] = set()
                with tarfile.open(fileobj=io.BytesIO(layer_bytes), mode="r:*") as layer_tar:
                    for member in layer_tar.getmembers():
                        normalized_name = member.name.removeprefix("./").rstrip("/")
                        normalized_names.add(normalized_name)
                        if normalized_name in allowed_files:
                            allowed_seen.add(normalized_name)
                        assert not normalized_name.endswith(
                            "/arbitrary-checkout-file.txt"
                        )
                        if not member.isfile():
                            continue
                        member_file = layer_tar.extractfile(member)
                        assert member_file is not None
                        member_bytes = member_file.read()
                        for canary in canaries:
                            assert canary not in member_bytes
                for filename in ROOT_CONFIGS:
                    assert all(
                        PurePosixPath(name).name != filename
                        for name in normalized_names
                    )
                for directory_name in ROOT_DEV_STATE_DIRECTORIES:
                    assert all(
                        directory_name not in PurePosixPath(name).parts
                        for name in normalized_names
                    )
                for directory_name in NESTED_NON_RUNTIME_DIRECTORIES:
                    assert all(
                        directory_name not in PurePosixPath(name).parts
                        for name in normalized_names
                    )
                for filename in SECRET_LIKE_FILENAMES:
                    assert all(
                        PurePosixPath(name).name != filename
                        for name in normalized_names
                    )
                assert all(
                    "/config/" not in f"/{name}/" and name != "config"
                    for name in normalized_names
                )
            assert allowed_seen == allowed_files
    finally:
        subprocess.run(
            ["docker", "image", "rm", "--force", image],
            cwd=PROJECT_ROOT,
            capture_output=True,
            check=False,
            timeout=30,
        )
