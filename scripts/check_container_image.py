#!/usr/bin/env python3
"""Validate the hermetic runtime image and emit a normalized manifest."""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sys
import tarfile
import tempfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterable, Mapping


EXPECTED_RUNTIME_USER = "10001:10001"
MAX_IMAGE_SIZE_BYTES = 9 * 1024**3 // 4
MAX_APP_PAYLOAD_BYTES = 16 * 1024**2
MAX_METADATA_BYTES = 4 * 1024**2
DOCKER_SAVE_TIMEOUT_SECONDS = 600
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DOCKERIGNORE_PATH = PROJECT_ROOT / ".dockerignore"
SOURCE_CONTEXT_ROOTS = ("llm_gateway_core", "static")

EXPECTED_WORKING_DIR = "/app"
EXPECTED_ENTRYPOINT = ("/app/entrypoint.sh",)
EXPECTED_COMMAND = ("python", "main.py")
EXPECTED_HEALTHCHECK = {
    "Test": ["CMD-SHELL", "python /app/healthcheck.py || exit 1"],
    "Interval": 30_000_000_000,
    "Timeout": 5_000_000_000,
    "StartPeriod": 10_000_000_000,
    "Retries": 3,
}
OCI_VERSION_LABEL = "org.opencontainers.image.version"
BROWSER_BINARY_PATH = "opt/cloakbrowser/chromium-146.0.7680.177.3/chrome"
VENV_PYTHON_PATH = "opt/venv/bin/python"
ESSENTIAL_ENV = {
    "APP_DIR": "/app",
    "CLOAKBROWSER_BINARY_PATH": f"/{BROWSER_BINARY_PATH}",
    "CLOAKBROWSER_CACHE_DIR": "/opt/cloakbrowser",
    "CLOAKBROWSER_AUTO_UPDATE": "false",
    "GATEWAY_OUTPUTS_DIR": "/app/outputs",
    "PYTHONDONTWRITEBYTECODE": "1",
}

APP_ROOT = "app"
PROTECTED_ROOTS = (APP_ROOT, "opt/venv", "opt/cloakbrowser")
MOUNTPOINT_PATHS = frozenset(
    {
        "app/config",
        "app/db",
        "app/logs",
        "app/outputs",
    }
)
RUNTIME_MOUNT_SUBDIRECTORIES = frozenset({"app/outputs/images"})
WRITABLE_RUNTIME_DIRECTORIES = MOUNTPOINT_PATHS | RUNTIME_MOUNT_SUBDIRECTORIES
DYNAMIC_RUNTIME_ROOTS = frozenset(
    {
        *MOUNTPOINT_PATHS,
        "dev",
        "proc",
        "run",
        "sys",
        "tmp",
    }
)
ALLOWED_APP_EXACT_PATHS = frozenset(
    {
        APP_ROOT,
        "app/main.py",
        "app/entrypoint.sh",
        "app/healthcheck.py",
        "app/llm_gateway_core",
        "app/static",
        "app/examples",
        "app/examples/free-tier-providers.md",
        *MOUNTPOINT_PATHS,
        *RUNTIME_MOUNT_SUBDIRECTORIES,
    }
)
FORBIDDEN_APP_COMPONENTS = frozenset(
    {
        ".agents",
        ".attachments",
        ".claude",
        ".cli-proxy",
        ".git",
        ".github",
        ".playwright",
        ".playwright-cli",
        ".pytest_cache",
        ".qwen",
        ".ruff_cache",
        "__pycache__",
        "cache",
        "caches",
        "diagnostic",
        "diagnostics",
        "node_modules",
        "tests",
    }
)
FORBIDDEN_SOURCE_FILE_BASENAMES = frozenset(
    {
        ".coverage",
        ".netrc",
        ".npmrc",
        ".pypirc",
        "credentials.json",
        "secrets.json",
    }
)
FORBIDDEN_SOURCE_FILE_SUFFIXES = frozenset(
    {
        ".db",
        ".key",
        ".log",
        ".p12",
        ".pem",
        ".pfx",
        ".prof",
        ".sqlite",
        ".sqlite3",
        ".trace",
    }
)
FORBIDDEN_CONFIG_BASENAMES = frozenset(
    {
        ".env",
        "providers.json",
        "models_fallback_rules.json",
        "models_operation_rules.json",
        "models_fusion_rules.json",
        "models_router_rules.json",
        "models_model_rules.json",
    }
)
REQUIRED_DIRECTORIES = frozenset(
    {
        APP_ROOT,
        "app/llm_gateway_core",
        "app/static",
        "app/examples",
        *MOUNTPOINT_PATHS,
        *RUNTIME_MOUNT_SUBDIRECTORIES,
        "opt/venv",
        "opt/cloakbrowser",
    }
)
REQUIRED_FILES = frozenset(
    {
        "app/main.py",
        "app/entrypoint.sh",
        "app/healthcheck.py",
        "app/examples/free-tier-providers.md",
    }
)
REQUIRED_EXECUTABLES = frozenset(
    {
        "app/entrypoint.sh",
        "app/healthcheck.py",
        BROWSER_BINARY_PATH,
    }
)


class CheckFailure(RuntimeError):
    """A safe, user-facing image contract failure."""


def _load_exact_source_allowlist() -> dict[str, str]:
    try:
        lines = DOCKERIGNORE_PATH.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError) as error:
        raise CheckFailure("exact Docker source allowlist is unavailable") from error

    patterns = [
        line.strip()
        for line in lines
        if line.strip() and not line.lstrip().startswith("#")
    ]
    source_paths: dict[str, bool] = {}
    for index, pattern in enumerate(patterns):
        if not pattern.startswith("!"):
            continue
        relative = pattern.removeprefix("!")
        if not any(
            relative == f"{root}/" or relative.startswith(f"{root}/")
            for root in SOURCE_CONTEXT_ROOTS
        ):
            continue
        if any(character in relative for character in "*?[\\"):
            raise CheckFailure("Docker source allowlist must contain only exact paths")
        is_directory = relative.endswith("/")
        normalized = relative.rstrip("/")
        pure_path = PurePosixPath(normalized)
        if (
            not normalized
            or pure_path.is_absolute()
            or any(part in {"", ".", ".."} for part in pure_path.parts)
            or pure_path.parts[0] not in SOURCE_CONTEXT_ROOTS
            or normalized in source_paths
        ):
            raise CheckFailure("Docker source allowlist contract is invalid")
        target = PROJECT_ROOT.joinpath(*pure_path.parts)
        if target.is_symlink() or (
            is_directory and not target.is_dir()
        ) or (not is_directory and not target.is_file()):
            raise CheckFailure("Docker source allowlist references an invalid path")
        if (
            index + 1 == len(patterns)
            or patterns[index + 1] != f"{normalized}/*"
        ):
            raise CheckFailure("Docker source allowlist is missing an ordered descendant guard")
        source_paths[normalized] = is_directory

    for root in SOURCE_CONTEXT_ROOTS:
        if source_paths.get(root) is not True:
            raise CheckFailure("Docker source allowlist is missing a runtime root")
    for source_path in source_paths:
        parent = PurePosixPath(source_path).parent
        while str(parent) != ".":
            if source_paths.get(str(parent)) is not True:
                raise CheckFailure("Docker source allowlist is missing a parent directory")
            parent = parent.parent
    return {
        f"app/{path}": "directory" if is_directory else "file"
        for path, is_directory in source_paths.items()
    }


@dataclass(frozen=True, slots=True)
class FileEntry:
    kind: str
    mode: int
    uid: int
    gid: int
    size: int = 0
    digest: str = ""
    link_target: str = ""

    def normalized(self, path: str) -> dict[str, int | str]:
        result: dict[str, int | str] = {
            "gid": self.gid,
            "kind": self.kind,
            "mode": f"{self.mode:04o}",
            "path": f"/{path}",
            "size": self.size,
            "uid": self.uid,
        }
        if self.digest:
            result["sha256"] = self.digest
        if self.link_target:
            result["target"] = self.link_target
        return result


def _normalized_tar_path(raw_path: str) -> str:
    path = PurePosixPath(raw_path)
    if path.is_absolute():
        raise CheckFailure("image layer contains an absolute archive path")
    parts = tuple(part for part in path.parts if part not in ("", "."))
    if not parts:
        return ""
    if ".." in parts:
        raise CheckFailure("image layer contains an unsafe archive path")
    return "/".join(parts)


def _is_beneath(path: str, root: str) -> bool:
    return path == root or path.startswith(f"{root}/")


def _is_tracked(path: str) -> bool:
    return any(_is_beneath(path, root) for root in PROTECTED_ROOTS)


def _is_allowed_app_path(
    path: str,
    source_allowlist: Mapping[str, str],
) -> bool:
    if path in ALLOWED_APP_EXACT_PATHS:
        return True
    if path not in source_allowlist:
        return False
    pure_path = PurePosixPath(path)
    source_parts = pure_path.parts[2:]
    for part in source_parts:
        suffix = PurePosixPath(part).suffix
        if part in FORBIDDEN_APP_COMPONENTS:
            return False
        if part.startswith("."):
            return False
        if part in FORBIDDEN_CONFIG_BASENAMES:
            return False
        if part.startswith(".env."):
            return False
        if part in FORBIDDEN_SOURCE_FILE_BASENAMES:
            return False
        if suffix in FORBIDDEN_SOURCE_FILE_SUFFIXES:
            return False
        if suffix in {".pyc", ".pyd", ".pyo"}:
            return False
        if suffix == ".py" and (
            part.startswith("test_") or part.endswith("_test.py")
        ):
            return False
    return True


def _normalized_link_target(path: str, target: str, *, hardlink: bool) -> str:
    if not target or "\0" in target:
        raise CheckFailure("protected image link target is invalid")
    if target.startswith("/"):
        candidate = target
    elif hardlink:
        candidate = f"/{target}"
    else:
        candidate = f"/{PurePosixPath(path).parent}/{target}"
    return f"/{candidate.lstrip('/')}"


def _metadata_contains_sentinel(member: tarfile.TarInfo, sentinels: tuple[bytes, ...]) -> bool:
    values = [member.name, member.linkname, member.uname, member.gname]
    for key, value in sorted(member.pax_headers.items()):
        values.extend((key, value))
    return any(
        sentinel in value.encode("utf-8", errors="surrogateescape")
        for value in values
        for sentinel in sentinels
    )


def _delete_path(entries: dict[str, FileEntry], path: str) -> None:
    prefix = f"{path}/"
    for existing in tuple(entries):
        if existing == path or existing.startswith(prefix):
            del entries[existing]


def _whiteout_target(path: str) -> tuple[str, bool] | None:
    pure_path = PurePosixPath(path)
    name = pure_path.name
    if name == ".wh..wh..opq":
        parent = str(pure_path.parent)
        return ("" if parent == "." else parent, True)
    if not name.startswith(".wh."):
        return None
    target = pure_path.with_name(name.removeprefix(".wh."))
    return str(target), False


def _contains_sentinel(stream: BinaryIO, sentinels: tuple[bytes, ...]) -> tuple[int, str, bool]:
    digest = hashlib.sha256()
    size = 0
    found = False
    overlap = max((len(sentinel) for sentinel in sentinels), default=1) - 1
    tail = b""
    while chunk := stream.read(1024 * 1024):
        digest.update(chunk)
        size += len(chunk)
        window = tail + chunk
        if any(sentinel in window for sentinel in sentinels):
            found = True
        tail = window[-overlap:] if overlap else b""
    return size, digest.hexdigest(), found


def _entry_from_member(
    layer_tar: tarfile.TarFile,
    member: tarfile.TarInfo,
    sentinels: tuple[bytes, ...],
) -> FileEntry:
    mode = member.mode & 0o7777
    if member.isfile():
        member_file = layer_tar.extractfile(member)
        if member_file is None:
            raise CheckFailure("image layer contains an unreadable regular file")
        size, digest, found = _contains_sentinel(member_file, sentinels)
        if found:
            raise CheckFailure("forbidden sentinel bytes detected in an image layer")
        return FileEntry("file", mode, member.uid, member.gid, size, digest)
    if member.isdir():
        return FileEntry("directory", mode, member.uid, member.gid)
    if member.issym():
        return FileEntry("symlink", mode, member.uid, member.gid, link_target=member.linkname)
    if member.islnk():
        return FileEntry("hardlink", mode, member.uid, member.gid, link_target=member.linkname)
    return FileEntry("special", mode, member.uid, member.gid)


def _scan_layer(
    layer_file: BinaryIO,
    entries: dict[str, FileEntry],
    sentinels: tuple[bytes, ...],
    source_allowlist: Mapping[str, str],
) -> None:
    try:
        with tarfile.open(fileobj=layer_file, mode="r:*") as layer_tar:
            whiteouts: list[tuple[str, bool]] = []
            ordinary_entries: list[tuple[str, FileEntry]] = []
            for member in layer_tar:
                if _metadata_contains_sentinel(member, sentinels):
                    raise CheckFailure("forbidden sentinel bytes detected in an image layer")
                path = _normalized_tar_path(member.name)
                entry = _entry_from_member(layer_tar, member, sentinels)
                if not path:
                    continue

                whiteout = _whiteout_target(path)
                if whiteout is not None:
                    whiteouts.append(whiteout)
                    continue

                if _is_beneath(path, APP_ROOT) and not _is_allowed_app_path(
                    path,
                    source_allowlist,
                ):
                    raise CheckFailure("image layer contains a forbidden application path")

                if entry.kind in {"symlink", "hardlink"}:
                    entry = FileEntry(
                        entry.kind,
                        entry.mode,
                        entry.uid,
                        entry.gid,
                        link_target=_normalized_link_target(
                            path,
                            entry.link_target,
                            hardlink=entry.kind == "hardlink",
                        ),
                    )
                ordinary_entries.append((path, entry))

            # OCI whiteouts remove only lower-layer entries. Apply all of them
            # before this layer's ordinary entries, regardless of tar order.
            for target, opaque in whiteouts:
                if opaque:
                    prefix = f"{target}/" if target else ""
                    for existing in tuple(entries):
                        if existing.startswith(prefix) and existing != target:
                            del entries[existing]
                else:
                    _delete_path(entries, target)

            for path, entry in ordinary_entries:
                if entry.kind != "directory":
                    _delete_path(entries, path)
                entries[path] = entry
    except tarfile.TarError as error:
        raise CheckFailure("image contains an invalid layer archive") from error


def _load_json_member(
    image_tar: tarfile.TarFile,
    member_name: str,
    sentinels: tuple[bytes, ...],
) -> object:
    try:
        member = image_tar.getmember(member_name)
    except KeyError as error:
        raise CheckFailure("docker image archive is missing required metadata") from error
    member_file = image_tar.extractfile(member)
    if member_file is None:
        raise CheckFailure("docker image archive metadata is unreadable")
    try:
        content = member_file.read(MAX_METADATA_BYTES + 1)
        if len(content) > MAX_METADATA_BYTES:
            raise CheckFailure("docker image archive metadata exceeds the size limit")
        if any(sentinel in content for sentinel in sentinels):
            raise CheckFailure("forbidden sentinel bytes detected in image metadata")
        return json.loads(content)
    except (UnicodeError, json.JSONDecodeError) as error:
        raise CheckFailure("docker image archive contains invalid metadata") from error


def _runtime_environment(config: Mapping[str, object]) -> dict[str, str]:
    raw_environment = config.get("Env")
    if not isinstance(raw_environment, list) or not all(
        isinstance(item, str) for item in raw_environment
    ):
        raise CheckFailure("runtime image environment contract is invalid")
    environment: dict[str, str] = {}
    for item in raw_environment:
        key, separator, value = item.partition("=")
        if not separator or key in environment:
            raise CheckFailure("runtime image environment contract is invalid")
        environment[key] = value
    if any(environment.get(key) != value for key, value in ESSENTIAL_ENV.items()):
        raise CheckFailure("runtime image environment contract is invalid")
    path = environment.get("PATH")
    if not path or path.split(":", 1)[0] != "/opt/venv/bin":
        raise CheckFailure("runtime image environment contract is invalid")
    return {"PATH": path, **ESSENTIAL_ENV}


def _validate_runtime_config(config: object) -> dict[str, object]:
    if not isinstance(config, dict):
        raise CheckFailure("docker image archive config is incomplete")
    runtime = config.get("config")
    if not isinstance(runtime, dict):
        raise CheckFailure("docker image archive config is incomplete")
    if runtime.get("User") != EXPECTED_RUNTIME_USER:
        raise CheckFailure("runtime image user contract is invalid")
    if runtime.get("WorkingDir") != EXPECTED_WORKING_DIR:
        raise CheckFailure("runtime image working-directory contract is invalid")
    if runtime.get("Entrypoint") != list(EXPECTED_ENTRYPOINT):
        raise CheckFailure("runtime image entrypoint contract is invalid")
    if runtime.get("Cmd") != list(EXPECTED_COMMAND):
        raise CheckFailure("runtime image command contract is invalid")
    if runtime.get("Healthcheck") != EXPECTED_HEALTHCHECK:
        raise CheckFailure("runtime image healthcheck contract is invalid")
    labels = runtime.get("Labels")
    if not isinstance(labels, dict):
        raise CheckFailure("runtime image OCI version label is invalid")
    version = labels.get(OCI_VERSION_LABEL)
    if not isinstance(version, str) or not version.strip():
        raise CheckFailure("runtime image OCI version label is invalid")
    return {
        "cmd": list(EXPECTED_COMMAND),
        "entrypoint": list(EXPECTED_ENTRYPOINT),
        "environment": _runtime_environment(runtime),
        "healthcheck": EXPECTED_HEALTHCHECK,
        "oci_version": version,
        "user": EXPECTED_RUNTIME_USER,
        "working_dir": EXPECTED_WORKING_DIR,
    }


def _validate_required_entries(entries: Mapping[str, FileEntry]) -> None:
    for path in sorted(REQUIRED_DIRECTORIES):
        entry = entries.get(path)
        if entry is None or entry.kind != "directory":
            raise CheckFailure("required image directory is missing or invalid")
    for path in sorted(REQUIRED_FILES):
        entry = entries.get(path)
        if entry is None or entry.kind != "file":
            raise CheckFailure("required application file is missing or invalid")
    browser = entries.get(BROWSER_BINARY_PATH)
    if browser is None or browser.kind != "file":
        raise CheckFailure("bundled browser executable is missing or invalid")
    python = entries.get(VENV_PYTHON_PATH)
    if python is None or python.kind not in {"file", "symlink", "hardlink"}:
        raise CheckFailure("virtual-environment Python is missing or invalid")
    for path in REQUIRED_EXECUTABLES:
        entry = entries[path]
        if not entry.mode & 0o111:
            raise CheckFailure("required image executable is not executable")
    resolved_python = _resolve_runtime_path(VENV_PYTHON_PATH, entries)
    if (
        resolved_python is None
        or resolved_python.kind != "file"
        or not resolved_python.mode & 0o111
        or resolved_python.uid != 0
        or resolved_python.gid != 0
        or resolved_python.mode & 0o022
    ):
        raise CheckFailure("virtual-environment Python is not executable")


def _validate_exact_source_entries(
    entries: Mapping[str, FileEntry],
    source_allowlist: Mapping[str, str],
) -> None:
    for path, expected_kind in source_allowlist.items():
        entry = entries.get(path)
        if entry is None or entry.kind != expected_kind:
            raise CheckFailure("exact Docker source path is missing or has an invalid type")


def _resolve_runtime_path(
    path: str,
    entries: Mapping[str, FileEntry],
) -> FileEntry | None:
    pending = [part for part in path.lstrip("/").split("/") if part]
    resolved: list[str] = []
    visited_links: set[str] = set()
    while pending:
        component = pending.pop(0)
        if component == ".":
            continue
        if component == "..":
            if resolved:
                resolved.pop()
            continue
        candidate = "/".join((*resolved, component))
        if any(_is_beneath(candidate, root) for root in DYNAMIC_RUNTIME_ROOTS):
            raise CheckFailure(
                "protected image link resolves into a writable mount or dynamic runtime root"
            )
        entry = entries.get(candidate)
        if entry is None or entry.kind not in {"symlink", "hardlink"}:
            resolved.append(component)
            continue
        if candidate in visited_links:
            raise CheckFailure("protected image contains a link cycle")
        visited_links.add(candidate)
        pending = [
            part for part in entry.link_target.lstrip("/").split("/") if part
        ] + pending
        resolved = []
    return entries.get("/".join(resolved))


def _validate_link_targets(entries: Mapping[str, FileEntry]) -> None:
    for path, entry in entries.items():
        if not _is_tracked(path) or entry.kind not in {"symlink", "hardlink"}:
            continue
        _resolve_runtime_path(path, entries)


def _validate_permissions(entries: Mapping[str, FileEntry]) -> None:
    for path, entry in sorted(entries.items()):
        if not _is_tracked(path):
            continue
        if path in WRITABLE_RUNTIME_DIRECTORIES:
            if (
                entry.kind != "directory"
                or entry.uid != 10001
                or entry.gid != 10001
                or entry.mode != 0o770
            ):
                raise CheckFailure("writable mountpoint contract is invalid")
            continue
        if entry.kind == "special":
            raise CheckFailure("protected image contains a special file")
        if entry.uid != 0 or entry.gid != 0:
            raise CheckFailure("protected image path is not root-owned")
        if entry.kind != "symlink" and entry.mode & 0o022:
            raise CheckFailure("protected image path is writable by the runtime user")
        if _is_beneath(path, APP_ROOT) and entry.kind not in {"directory", "file"}:
            raise CheckFailure("application path has an unsupported file type")
    _validate_link_targets(entries)


def _root_summary(entries: Mapping[str, FileEntry], root: str) -> dict[str, int | str]:
    normalized = [
        entry.normalized(path)
        for path, entry in sorted(entries.items())
        if _is_beneath(path, root)
    ]
    encoded = json.dumps(
        normalized,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return {
        "entry_count": len(normalized),
        "manifest_sha256": hashlib.sha256(encoded).hexdigest(),
        "payload_bytes": sum(
            entry.size
            for path, entry in entries.items()
            if _is_beneath(path, root) and entry.kind == "file"
        ),
    }


def _normalized_manifest(
    entries: Mapping[str, FileEntry],
    *,
    runtime_config: Mapping[str, object],
) -> dict[str, object]:
    app_entries = [
        entry.normalized(path)
        for path, entry in sorted(entries.items())
        if _is_beneath(path, APP_ROOT)
    ]
    app_payload_bytes = sum(
        entry.size
        for path, entry in entries.items()
        if _is_beneath(path, APP_ROOT) and entry.kind == "file"
    )
    return {
        "app_entries": app_entries,
        "app_payload_bytes": app_payload_bytes,
        "protected_roots": {
            f"/{root}": _root_summary(entries, root) for root in PROTECTED_ROOTS
        },
        "runtime_config": dict(runtime_config),
        "schema_version": 2,
    }


def inspect_image_archive(
    archive_path: Path,
    *,
    sentinels: Iterable[bytes] = (),
) -> dict[str, object]:
    """Inspect a ``docker image save`` archive without extracting it."""

    sentinel_tuple = tuple(sentinels)
    if any(not sentinel for sentinel in sentinel_tuple):
        raise ValueError("sentinels must not be empty")

    source_allowlist = _load_exact_source_allowlist()
    entries: dict[str, FileEntry] = {}
    try:
        with tarfile.open(archive_path, mode="r:*") as image_tar:
            for member in image_tar.getmembers():
                if _metadata_contains_sentinel(member, sentinel_tuple):
                    raise CheckFailure("forbidden sentinel bytes detected in image metadata")

            manifest = _load_json_member(image_tar, "manifest.json", sentinel_tuple)
            if not isinstance(manifest, list) or len(manifest) != 1:
                raise CheckFailure("docker image archive must contain exactly one image")
            image_record = manifest[0]
            if not isinstance(image_record, dict):
                raise CheckFailure("docker image archive manifest has an invalid image record")
            config_name = image_record.get("Config")
            layer_names = image_record.get("Layers")
            if not isinstance(config_name, str) or not isinstance(layer_names, list):
                raise CheckFailure("docker image archive manifest is incomplete")
            if not layer_names or not all(isinstance(name, str) for name in layer_names):
                raise CheckFailure("docker image archive has no valid layers")
            if len(layer_names) != len(set(layer_names)):
                raise CheckFailure("docker image archive repeats a layer")

            config = _load_json_member(image_tar, config_name, sentinel_tuple)
            runtime_config = _validate_runtime_config(config)

            for layer_name in layer_names:
                try:
                    layer_member = image_tar.getmember(layer_name)
                except KeyError as error:
                    raise CheckFailure("docker image archive is missing a declared layer") from error
                if not layer_member.isfile():
                    raise CheckFailure("docker image archive layer is not a regular file")
                layer_file = image_tar.extractfile(layer_member)
                if layer_file is None:
                    raise CheckFailure("docker image archive layer is unreadable")
                _scan_layer(layer_file, entries, sentinel_tuple, source_allowlist)
    except (OSError, tarfile.TarError) as error:
        raise CheckFailure("docker image archive is unreadable") from error

    _validate_exact_source_entries(entries, source_allowlist)
    _validate_permissions(entries)
    _validate_required_entries(entries)
    app_payload_bytes = sum(
        entry.size
        for path, entry in entries.items()
        if _is_beneath(path, APP_ROOT) and entry.kind == "file"
    )
    if app_payload_bytes > MAX_APP_PAYLOAD_BYTES:
        raise CheckFailure("application payload exceeds the configured size budget")
    return _normalized_manifest(entries, runtime_config=runtime_config)


def _save_image(image: str, archive_path: Path) -> None:
    try:
        completed = subprocess.run(
            ["docker", "image", "save", "--output", str(archive_path), image],
            check=False,
            capture_output=True,
            timeout=DOCKER_SAVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckFailure("docker image save failed to start or timed out") from error
    if completed.returncode != 0:
        raise CheckFailure("docker image save failed")


def _inspect_image_size(image: str) -> int:
    try:
        completed = subprocess.run(
            ["docker", "image", "inspect", "--format", "{{.Size}}", image],
            check=False,
            capture_output=True,
            timeout=DOCKER_SAVE_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.TimeoutExpired) as error:
        raise CheckFailure("docker image inspect failed to start or timed out") from error
    if completed.returncode != 0:
        raise CheckFailure("docker image inspect failed")
    try:
        size = int(completed.stdout.strip())
    except ValueError as error:
        raise CheckFailure("docker image inspect returned an invalid size") from error
    if size < 0:
        raise CheckFailure("docker image inspect returned an invalid size")
    return size


def _validate_image_size(image_size: int) -> None:
    if image_size > MAX_IMAGE_SIZE_BYTES:
        raise CheckFailure("image exceeds the configured size budget")


def _parse_args(argv: list[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", help="local Docker image name or ID")
    parser.add_argument(
        "--manifest-out",
        type=Path,
        help="write the deterministic normalized JSON manifest to this path",
    )
    parser.add_argument(
        "--sentinel",
        action="append",
        default=[],
        help="UTF-8 byte sequence that must not appear in any image layer (repeatable)",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = _parse_args(argv)
    if any(not value for value in args.sentinel):
        print("container image check failed: sentinels must not be empty", file=sys.stderr)
        return 2
    try:
        _validate_image_size(_inspect_image_size(args.image))
        with tempfile.TemporaryDirectory(prefix="llmgateway-image-check-") as temp_dir:
            archive_path = Path(temp_dir) / "image.tar"
            _save_image(args.image, archive_path)
            manifest = inspect_image_archive(
                archive_path,
                sentinels=(value.encode("utf-8") for value in args.sentinel),
            )
        rendered = json.dumps(manifest, indent=2, sort_keys=True) + "\n"
        if args.manifest_out is None:
            sys.stdout.write(rendered)
        else:
            args.manifest_out.write_text(rendered, encoding="utf-8")
            print("container image manifest written")
    except CheckFailure as error:
        print(f"container image check failed: {error}", file=sys.stderr)
        return 1
    except (OSError, UnicodeError):
        print("container image check failed: local I/O error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
