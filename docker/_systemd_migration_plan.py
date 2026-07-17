"""Read-only shallow inventory and stopped-service migration planning."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path

from docker._systemd_migration_env import environment_assignments
from docker._systemd_migration_fs import (
    directory_metadata_needs_normalization,
    inventory_tree,
    is_temporary,
    read_snapshot_bytes,
    regular_snapshot,
    require_disjoint,
    safe_absolute_directory,
    shallow_regular,
    validate_directory_metadata,
    validate_target_file,
)
from docker._systemd_migration_model import (
    CACHE_MANIFEST_FILENAME,
    CONFIG_FILENAMES,
    DEAD_LETTER_FILENAME,
    ENV_FILE_MODE,
    FHS_DIRECTORY_MODE,
    KNOWN_DB_FILENAMES,
    LOCK_FILENAME,
    MANDATORY_CONFIG_FILENAMES,
    OUTPUTS_INCOMPLETE_MARKER,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    SQLITE_SIDECAR_SUFFIXES,
    Artifact,
    FileSnapshot,
    MigrationError,
    MigrationPlan,
    Ownership,
)


_MAX_CACHE_MANIFEST_BYTES = 16 * 1024 * 1024


@dataclass(frozen=True, slots=True)
class _Roots:
    source: Path
    target_env: Path
    target_state: Path
    target_cache: Path
    source_cache: Path | None


def build_plan(
    *,
    source_root: str | os.PathLike[str],
    target_env_dir: str | os.PathLike[str],
    target_state_dir: str | os.PathLike[str],
    target_cache_dir: str | os.PathLike[str],
    source_cache_dir: str | os.PathLike[str] | None,
    ownership: Ownership,
    full: bool,
) -> MigrationPlan:
    roots = _resolve_roots(
        source_root=source_root,
        target_env_dir=target_env_dir,
        target_state_dir=target_state_dir,
        target_cache_dir=target_cache_dir,
        source_cache_dir=source_cache_dir,
    )
    directory_metadata = _validate_fhs_roots(roots, ownership)
    environment, environment_report = _plan_environment(roots, ownership)
    configs, config_report = _plan_configs(roots, ownership)
    databases, sidecars, dead_letter, state_report = _plan_state(roots, ownership, full=full)
    outputs_report = _plan_outputs(roots, ownership, full=full)
    cache_manifest, cache_report = _plan_cache(roots, ownership, full=full)
    report: dict[str, object] = {
        "version": 1,
        "migration_required": False,
        "directory_metadata": [
            {"name": name, "status": "ready"} for name in directory_metadata
        ],
        "environment": environment_report,
        "configs": config_report,
        "state": state_report,
        "outputs": outputs_report,
        "cloakbrowser_cache": cache_report,
        "legacy_logs": _plan_logs(roots),
    }
    report["migration_required"] = _report_requires_migration(report)
    return MigrationPlan(
        source_root=roots.source,
        target_env_dir=roots.target_env,
        target_state_dir=roots.target_state,
        target_cache_dir=roots.target_cache,
        source_cache_dir=roots.source_cache,
        environment=environment,
        configs=configs,
        databases=databases,
        database_sidecars=sidecars,
        dead_letter=dead_letter,
        cache_manifest=cache_manifest,
        report=report,
    )


def _resolve_roots(
    *,
    source_root: str | os.PathLike[str],
    target_env_dir: str | os.PathLike[str],
    target_state_dir: str | os.PathLike[str],
    target_cache_dir: str | os.PathLike[str],
    source_cache_dir: str | os.PathLike[str] | None,
) -> _Roots:
    source = safe_absolute_directory(source_root, label="source-root")
    target_env = safe_absolute_directory(target_env_dir, label="target-env-dir", allow_missing_leaf=True)
    target_state = safe_absolute_directory(
        target_state_dir,
        label="target-state-dir",
        allow_missing_leaf=True,
    )
    target_cache = safe_absolute_directory(
        target_cache_dir,
        label="target-cache-dir",
        allow_missing_leaf=True,
    )
    source_cache = (
        safe_absolute_directory(source_cache_dir, label="source-cache-dir")
        if source_cache_dir is not None
        else None
    )
    paths = [
        ("source-root", source),
        ("target-env-dir", target_env),
        ("target-state-dir", target_state),
        ("target-cache-dir", target_cache),
    ]
    if source_cache is not None:
        paths.append(("source-cache-dir", source_cache))
    require_disjoint(paths)
    return _Roots(source, target_env, target_state, target_cache, source_cache)


def _validate_fhs_roots(roots: _Roots, ownership: Ownership) -> tuple[str, ...]:
    repairs: list[str] = []
    for name, path, uid in (
        ("target-env-dir", roots.target_env, ownership.env_uid),
        ("target-state-dir", roots.target_state, ownership.service_uid),
        ("target-cache-dir", roots.target_cache, ownership.service_uid),
    ):
        if _path_present(path) and directory_metadata_needs_normalization(
            path,
            uid=uid,
            gid=ownership.service_gid,
            name=name,
            mode=FHS_DIRECTORY_MODE,
        ):
            repairs.append(name)
    return tuple(repairs)


def _plan_environment(roots: _Roots, ownership: Ownership) -> tuple[Artifact, dict[str, object]]:
    source = regular_snapshot(roots.source / ".env", name=".env")
    target = validate_target_file(
        roots.target_env / "gateway.env",
        name="gateway.env",
        uid=ownership.env_uid,
        gid=ownership.service_gid,
        mode=ENV_FILE_MODE,
    )
    if source is None and target is None:
        raise MigrationError("environment-missing")
    effective = target or source
    if effective is None:
        raise MigrationError("environment-missing")
    report = {
        "status": "existing" if target else "ready",
        "assignments": environment_assignments(effective),
    }
    return Artifact("gateway.env", source, target), report


def _plan_configs(roots: _Roots, ownership: Ownership) -> tuple[tuple[Artifact, ...], list[dict[str, object]]]:
    config_dir = roots.target_state / "config"
    config_exists = _path_present(config_dir)
    if config_exists:
        validate_directory_metadata(
            config_dir,
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            name="config",
            mode=RUNTIME_DIRECTORY_MODE,
        )
    artifacts = tuple(
        _config_artifact(name, roots, config_dir, config_exists, ownership)
        for name in CONFIG_FILENAMES
    )
    missing = [
        artifact.name
        for artifact in artifacts
        if artifact.name in MANDATORY_CONFIG_FILENAMES
        and artifact.source is None
        and artifact.target is None
    ]
    if missing:
        raise MigrationError("mandatory-config-missing", missing)
    return artifacts, [_artifact_report(artifact) for artifact in artifacts]


def _config_artifact(
    name: str,
    roots: _Roots,
    config_dir: Path,
    config_exists: bool,
    ownership: Ownership,
) -> Artifact:
    source = regular_snapshot(roots.source / name, name=name)
    target = None
    if config_exists:
        target = validate_target_file(
            config_dir / name,
            name=name,
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            mode=RUNTIME_FILE_MODE,
        )
    return Artifact(name, source, target)


def _plan_state(
    roots: _Roots,
    ownership: Ownership,
    *,
    full: bool,
) -> tuple[
    tuple[Artifact, ...],
    tuple[tuple[str, tuple[FileSnapshot, ...]], ...],
    Artifact,
    list[dict[str, object]],
]:
    source_db = roots.source / "db"
    source_names, target_names = _validate_state_names(source_db, roots.target_state)
    artifacts: list[Artifact] = []
    sidecar_groups: list[tuple[str, tuple[FileSnapshot, ...]]] = []
    report: list[dict[str, object]] = []
    for name in KNOWN_DB_FILENAMES:
        artifact = _database_artifact(name, source_db, roots.target_state, ownership, full=full)
        artifacts.append(artifact)
        report.append(_artifact_report(artifact))
        sidecars = _database_sidecars(name, source_db, source_names, full=full)
        sidecar_groups.append((name, sidecars))
        report.extend(_sidecar_report(name, source_names, target_names, roots.target_state, ownership, full=full))
    dead_letter = _state_artifact(
        DEAD_LETTER_FILENAME,
        source_db,
        roots.target_state,
        ownership,
        full=full,
        hash_content=True,
    )
    if dead_letter.source is not None or dead_letter.target is not None:
        report.append(_artifact_report(dead_letter))
    report.extend(_skipped_state_report(source_names, target_names))
    return tuple(artifacts), tuple(sidecar_groups), dead_letter, report


def _database_artifact(
    name: str,
    source_db: Path,
    target_state: Path,
    ownership: Ownership,
    *,
    full: bool,
) -> Artifact:
    return _state_artifact(
        name,
        source_db,
        target_state,
        ownership,
        full=full,
        hash_content=False,
    )


def _state_artifact(
    name: str,
    source_db: Path,
    target_state: Path,
    ownership: Ownership,
    *,
    full: bool,
    hash_content: bool,
) -> Artifact:
    source = (
        regular_snapshot(source_db / name, name=name, hash_content=hash_content)
        if full
        else shallow_regular(source_db / name, name=name)
    )
    target = validate_target_file(
        target_state / name,
        name=name,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=RUNTIME_FILE_MODE,
        hash_content=hash_content,
        shallow=not full,
    )
    return Artifact(name, source, target)


def _database_sidecars(
    database: str,
    source_db: Path,
    source_names: list[str],
    *,
    full: bool,
) -> tuple[FileSnapshot, ...]:
    snapshots: list[FileSnapshot] = []
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        name = f"{database}{suffix}"
        if name not in source_names:
            continue
        snapshot = (
            regular_snapshot(source_db / name, name=name, hash_content=False)
            if full
            else shallow_regular(source_db / name, name=name)
        )
        if snapshot is None:
            raise MigrationError("source-changed", (name,))
        snapshots.append(snapshot)
    return tuple(snapshots)


def _sidecar_report(
    database: str,
    source_names: list[str],
    target_names: list[str],
    target_state: Path,
    ownership: Ownership,
    *,
    full: bool,
) -> list[dict[str, str]]:
    report: list[dict[str, str]] = []
    for suffix in SQLITE_SIDECAR_SUFFIXES:
        name = f"{database}{suffix}"
        if name in target_names:
            validate_target_file(
                target_state / name,
                name=name,
                uid=ownership.service_uid,
                gid=ownership.service_gid,
                mode=RUNTIME_FILE_MODE,
                hash_content=False,
                shallow=not full,
            )
        if name in source_names:
            report.append({"name": name, "status": "included-by-sqlite-backup"})
        elif name in target_names:
            report.append({"name": name, "status": "existing"})
    return report


def _validate_state_names(source_db: Path, target_state: Path) -> tuple[list[str], list[str]]:
    source_names = _state_entry_names(source_db, label="source-state")
    target_names = _state_entry_names(target_state, label="target-state")
    source_allowed = set(KNOWN_DB_FILENAMES) | {DEAD_LETTER_FILENAME, LOCK_FILENAME}
    target_allowed = source_allowed | {"config", "outputs", ".migration"}
    orphaned: list[str] = []
    for database in KNOWN_DB_FILENAMES:
        for suffix in SQLITE_SIDECAR_SUFFIXES:
            sidecar = f"{database}{suffix}"
            if sidecar in source_names:
                source_allowed.add(sidecar)
                if database not in source_names:
                    orphaned.append(sidecar)
            if sidecar in target_names:
                target_allowed.add(sidecar)
                if database not in target_names:
                    orphaned.append(sidecar)
    if orphaned:
        raise MigrationError("orphan-sqlite-sidecar", orphaned)
    unknown = [name for name in source_names if name not in source_allowed and not is_temporary(name)]
    unknown.extend(name for name in target_names if name not in target_allowed and not is_temporary(name))
    if unknown:
        raise MigrationError("unknown-state", unknown)
    return source_names, target_names


def _state_entry_names(directory: Path, *, label: str) -> list[str]:
    try:
        metadata = directory.lstat()
    except FileNotFoundError:
        return []
    except OSError:
        raise MigrationError("state-unavailable", (label,)) from None
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise MigrationError("state-unavailable", (label,))
    try:
        with os.scandir(directory) as iterator:
            return sorted(entry.name for entry in iterator)
    except OSError:
        raise MigrationError("state-unavailable", (label,)) from None


def _skipped_state_report(source_names: list[str], target_names: list[str]) -> list[dict[str, str]]:
    report: list[dict[str, str]] = []
    for name in sorted(set(source_names) | set(target_names)):
        if name == LOCK_FILENAME:
            report.append({"name": name, "status": "ephemeral-lock-skip"})
        elif is_temporary(name):
            report.append({"name": name, "status": "temporary-skip"})
    return report


def _plan_outputs(roots: _Roots, ownership: Ownership, *, full: bool) -> dict[str, object]:
    source_images = roots.source / "outputs" / "images"
    source_present = _source_directory_present(source_images, "source-outputs")
    target_outputs = roots.target_state / "outputs"
    target_present = _path_present(target_outputs)
    recoverable = False
    if target_present:
        recoverable = _target_outputs_recoverable(target_outputs, ownership)
    if target_present and not recoverable:
        return _existing_outputs_report(target_outputs, ownership, full=full)
    if source_present:
        return _ready_outputs_report(source_images, full=full)
    if target_present:
        raise MigrationError("outputs-target-incomplete")
    return {"status": "source-missing"}


def _source_directory_present(path: Path, label: str) -> bool:
    if not _path_present(path):
        return False
    safe_absolute_directory(path, label=label)
    return True


def _target_outputs_recoverable(outputs: Path, ownership: Ownership) -> bool:
    validate_directory_metadata(
        outputs,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        name="outputs",
        mode=RUNTIME_DIRECTORY_MODE,
    )
    marker = outputs / OUTPUTS_INCOMPLETE_MARKER
    marker_present = shallow_regular(marker, name=marker.name, target=True) is not None
    images = outputs / "images"
    if not _path_present(images):
        return True
    validate_directory_metadata(
        images,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        name="images",
        mode=RUNTIME_DIRECTORY_MODE,
    )
    return marker_present or _directory_empty(images)


def _existing_outputs_report(outputs: Path, ownership: Ownership, *, full: bool) -> dict[str, object]:
    if not full:
        return {"status": "existing"}
    inventory_tree(outputs, target=True, ownership=ownership)
    inventory = _image_inventory(outputs / "images")
    return {
        "status": "existing",
        "count": inventory.count,
        "tree_sha256": inventory.tree_sha256,
    }


def _ready_outputs_report(source_images: Path, *, full: bool) -> dict[str, object]:
    if not full:
        return {"status": "ready"}
    inventory = _image_inventory(source_images)
    return {
        "status": "ready",
        "count": inventory.count,
        "tree_sha256": inventory.tree_sha256,
    }


def _image_inventory(images: Path) -> object:
    try:
        from llm_gateway_core.services.image_storage_cli import build_inventory

        return build_inventory(images)
    except Exception:
        raise MigrationError("outputs-inventory-failed") from None


def _plan_cache(
    roots: _Roots,
    ownership: Ownership,
    *,
    full: bool,
) -> tuple[dict[str, object] | None, dict[str, object]]:
    if _path_present(roots.target_cache):
        report = validate_existing_cache(
            roots.target_cache,
            ownership,
            full=full,
            allow_root_mode_drift=True,
        )
        return None, report
    if roots.source_cache is None:
        return None, {"status": "not-requested"}
    if _path_present(roots.source_cache / CACHE_MANIFEST_FILENAME):
        raise MigrationError("unsafe-source", (CACHE_MANIFEST_FILENAME,))
    if not full:
        return None, {"status": "ready"}
    manifest = inventory_tree(roots.source_cache, target=False, ownership=ownership)
    return manifest, {"status": "ready", "manifest": manifest}


def validate_existing_cache(
    target_cache: Path,
    ownership: Ownership,
    *,
    full: bool = True,
    allow_root_mode_drift: bool = False,
) -> dict[str, object]:
    safe_absolute_directory(target_cache, label="target-cache-dir")
    if allow_root_mode_drift:
        directory_metadata_needs_normalization(
            target_cache,
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            name="target-cache-dir",
            mode=FHS_DIRECTORY_MODE,
        )
    else:
        validate_directory_metadata(
            target_cache,
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            name="target-cache-dir",
            mode=FHS_DIRECTORY_MODE,
        )
    manifest = validate_target_file(
        target_cache / CACHE_MANIFEST_FILENAME,
        name=CACHE_MANIFEST_FILENAME,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=RUNTIME_FILE_MODE,
        shallow=not full,
    )
    if manifest is None:
        raise MigrationError("unsafe-existing-target", (CACHE_MANIFEST_FILENAME,))
    if not full:
        return {"status": "existing"}
    actual = inventory_tree(
        target_cache,
        target=True,
        ownership=ownership,
        exclude_name=CACHE_MANIFEST_FILENAME,
    )
    stored = _read_cache_manifest(manifest)
    if stored != actual:
        raise MigrationError("unsafe-existing-target", (CACHE_MANIFEST_FILENAME,))
    return {"status": "existing", "manifest": actual}


def _read_cache_manifest(snapshot: FileSnapshot) -> object:
    try:
        payload = read_snapshot_bytes(
            snapshot,
            name=CACHE_MANIFEST_FILENAME,
            maximum_size=_MAX_CACHE_MANIFEST_BYTES,
        )
        return json.loads(payload.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError):
        raise MigrationError("unsafe-existing-target", (CACHE_MANIFEST_FILENAME,)) from None
    except MigrationError as error:
        if error.reason == "file-too-large":
            raise MigrationError("unsafe-existing-target", (CACHE_MANIFEST_FILENAME,)) from None
        raise


def _plan_logs(roots: _Roots) -> dict[str, object]:
    logs = roots.source / "logs"
    if not _path_present(logs):
        return {"status": "report-only", "entry_count": 0}
    safe_absolute_directory(logs, label="logs")
    try:
        with os.scandir(logs) as iterator:
            count = sum(1 for _ in iterator)
    except OSError:
        raise MigrationError("logs-unavailable") from None
    return {"status": "report-only", "entry_count": count}


def _artifact_report(artifact: Artifact) -> dict[str, object]:
    status = "existing" if artifact.target else "ready" if artifact.source else "source-missing"
    report: dict[str, object] = {"name": artifact.name, "status": status}
    if artifact.source is not None and artifact.source.sha256 is not None:
        report["source_sha256"] = artifact.source.sha256
    if artifact.target is not None and artifact.target.sha256 is not None:
        report["target_sha256"] = artifact.target.sha256
    return report


def _report_requires_migration(report: dict[str, object]) -> bool:
    statuses = [item["status"] for item in report["directory_metadata"]]
    statuses.append(report["environment"]["status"])
    statuses.extend(item["status"] for item in report["configs"])
    statuses.extend(item["status"] for item in report["state"])
    statuses.extend((report["outputs"]["status"], report["cloakbrowser_cache"]["status"]))
    return "ready" in statuses


def _path_present(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise MigrationError("path-unavailable", (path.name,)) from None
    return True


def _directory_empty(path: Path) -> bool:
    try:
        with os.scandir(path) as iterator:
            return next(iterator, None) is None
    except OSError:
        raise MigrationError("outputs-inventory-failed") from None
