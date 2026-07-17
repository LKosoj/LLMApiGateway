"""Stopped-service mutations for a fully verified systemd migration plan."""

from __future__ import annotations

import copy
import json
import os
import stat
import uuid
from pathlib import Path, PurePosixPath

from docker._systemd_migration_fs import (
    atomic_copy,
    copy_snapshot_to_new,
    ensure_directory,
    ensure_runtime_directory,
    fsync_directory,
    inventory_tree,
    normalize_directory_metadata,
    open_directory,
    regular_snapshot,
    remove_tree_and_sync,
    rename_noreplace,
    sqlite_backup,
    sync_directories_bottom_up,
    validate_directory_metadata,
    write_bytes_new,
)
from docker._systemd_migration_model import (
    CACHE_MANIFEST_FILENAME,
    DEAD_LETTER_FILENAME,
    ENV_FILE_MODE,
    FHS_DIRECTORY_MODE,
    OUTPUTS_MANIFEST_FILENAME,
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    FileSnapshot,
    ImageMigrator,
    MigrationError,
    MigrationPlan,
    Ownership,
)
from docker._systemd_migration_plan import validate_existing_cache


def default_image_migrator(source: Path, outputs: Path, manifest: Path) -> object:
    from llm_gateway_core.services.image_storage_cli import initialize_volume, migrate_images

    initialize_volume(outputs)
    return migrate_images(source, outputs, manifest)


def apply_plan(
    plan: MigrationPlan,
    ownership: Ownership,
    image_migrator: ImageMigrator,
) -> dict[str, object]:
    report = copy.deepcopy(plan.report)
    _normalize_fhs_directories(plan, report, ownership)
    _apply_environment(plan, report, ownership)
    _ensure_state_root(plan, report, ownership)
    _apply_configs(plan, report, ownership)
    _apply_databases(plan, report, ownership)
    _apply_dead_letter(plan, report, ownership)
    _apply_outputs(plan, report, ownership, image_migrator)
    _apply_cache(plan, report, ownership)
    report["migration_required"] = False
    return report


def _normalize_fhs_directories(
    plan: MigrationPlan,
    report: dict[str, object],
    ownership: Ownership,
) -> None:
    roots = {
        "target-env-dir": (plan.target_env_dir, ownership.env_uid),
        "target-state-dir": (plan.target_state_dir, ownership.service_uid),
        "target-cache-dir": (plan.target_cache_dir, ownership.service_uid),
    }
    for item in report["directory_metadata"]:
        name = item["name"]
        path, uid = roots[name]
        normalize_directory_metadata(
            path,
            uid=uid,
            gid=ownership.service_gid,
            name=name,
            mode=FHS_DIRECTORY_MODE,
        )
        item["status"] = "normalized"


def _apply_environment(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    if plan.environment.target is not None:
        return
    ensure_directory(
        plan.target_env_dir,
        uid=ownership.env_uid,
        gid=ownership.service_gid,
        mode=FHS_DIRECTORY_MODE,
    )
    atomic_copy(
        plan.environment,
        plan.target_env_dir / "gateway.env",
        uid=ownership.env_uid,
        gid=ownership.service_gid,
        mode=ENV_FILE_MODE,
    )
    report["environment"]["status"] = "copied"


def _ensure_state_root(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    artifacts = (*plan.configs, *plan.databases, plan.dead_letter)
    artifact_write = any(item.source is not None and item.target is None for item in artifacts)
    if not artifact_write and report["outputs"]["status"] != "ready":
        return
    ensure_directory(
        plan.target_state_dir,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=FHS_DIRECTORY_MODE,
    )


def _apply_configs(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    pending = [item for item in plan.configs if item.source is not None and item.target is None]
    if not pending:
        return
    config_dir = plan.target_state_dir / "config"
    ensure_runtime_directory(config_dir, ownership)
    reports = {item["name"]: item for item in report["configs"]}
    for artifact in pending:
        target = atomic_copy(
            artifact,
            config_dir / artifact.name,
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            mode=RUNTIME_FILE_MODE,
        )
        reports[artifact.name]["status"] = "copied"
        reports[artifact.name]["target_sha256"] = target.sha256


def _apply_databases(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    sidecars = dict(plan.database_sidecars)
    reports = {item["name"]: item for item in report["state"]}
    for artifact in plan.databases:
        if artifact.source is None or artifact.target is not None:
            continue
        sqlite_backup(
            artifact,
            sidecars.get(artifact.name, ()),
            plan.target_state_dir / artifact.name,
            ownership,
        )
        reports[artifact.name]["status"] = "copied"


def _apply_dead_letter(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    artifact = plan.dead_letter
    if artifact.source is None or artifact.target is not None:
        return
    atomic_copy(
        artifact,
        plan.target_state_dir / DEAD_LETTER_FILENAME,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=RUNTIME_FILE_MODE,
    )
    reports = {item["name"]: item for item in report["state"]}
    reports[DEAD_LETTER_FILENAME]["status"] = "copied"


def _apply_outputs(
    plan: MigrationPlan,
    report: dict[str, object],
    ownership: Ownership,
    image_migrator: ImageMigrator,
) -> None:
    if report["outputs"]["status"] != "ready":
        return
    migration_dir = plan.target_state_dir / ".migration"
    ensure_runtime_directory(migration_dir, ownership)
    result = image_migrator(
        plan.source_root / "outputs" / "images",
        plan.target_state_dir / "outputs",
        migration_dir / OUTPUTS_MANIFEST_FILENAME,
    )
    report["outputs"] = {
        "status": "migrated-via-image-storage-cli",
        "count": result.count,
        "tree_sha256": result.tree_sha256,
    }


def _apply_cache(plan: MigrationPlan, report: dict[str, object], ownership: Ownership) -> None:
    status = _copy_cache(plan, ownership)
    if status in {"copied", "existing"}:
        cache_report = validate_existing_cache(plan.target_cache_dir, ownership)
        cache_report["status"] = status
        report["cloakbrowser_cache"] = cache_report


def _copy_cache(plan: MigrationPlan, ownership: Ownership) -> str:
    if plan.source_cache_dir is None or plan.cache_manifest is None:
        return "not-requested"
    if _entry_exists(plan.target_cache_dir):
        validate_existing_cache(plan.target_cache_dir, ownership)
        return "existing"
    _validate_trusted_parent(plan.target_cache_dir.parent, ownership.env_uid)
    staging = plan.target_cache_dir.with_name(
        f".{plan.target_cache_dir.name}.migration-{uuid.uuid4().hex}"
    )
    ensure_directory(
        staging,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=FHS_DIRECTORY_MODE,
    )
    published = False
    try:
        _populate_cache(staging, plan, ownership)
        _verify_staged_cache(staging, plan, ownership)
        _validate_trusted_parent(plan.target_cache_dir.parent, ownership.env_uid)
        published = _publish_cache(staging, plan.target_cache_dir)
        _validate_trusted_parent(plan.target_cache_dir.parent, ownership.env_uid)
        validate_existing_cache(plan.target_cache_dir, ownership)
        return "copied" if published else "existing"
    finally:
        if not published and _entry_exists(staging):
            remove_tree_and_sync(staging)


def _populate_cache(staging: Path, plan: MigrationPlan, ownership: Ownership) -> None:
    if plan.source_cache_dir is None or plan.cache_manifest is None:
        raise MigrationError("cache-plan-invalid")
    for entry in plan.cache_manifest["files"]:
        relative = _safe_relative(entry["path"])
        _ensure_cache_parents(staging, relative, ownership)
        source = plan.source_cache_dir.joinpath(*relative.parts)
        snapshot = regular_snapshot(source, name=relative.name)
        if snapshot is None or not _snapshot_matches_entry(snapshot, entry):
            raise MigrationError("source-changed", (relative.name,))
        copy_snapshot_to_new(
            snapshot,
            staging.joinpath(*relative.parts),
            uid=ownership.service_uid,
            gid=ownership.service_gid,
            mode=int(entry["mode"]),
            preserve_mtime=True,
        )
    manifest_payload = (
        json.dumps(plan.cache_manifest, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()
    write_bytes_new(
        staging / CACHE_MANIFEST_FILENAME,
        manifest_payload,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=RUNTIME_FILE_MODE,
    )
    sync_directories_bottom_up(staging)


def _ensure_cache_parents(staging: Path, relative: PurePosixPath, ownership: Ownership) -> None:
    current = staging
    for component in relative.parts[:-1]:
        current /= component
        if _entry_exists(current):
            validate_directory_metadata(
                current,
                uid=ownership.service_uid,
                gid=ownership.service_gid,
                name=component,
                mode=RUNTIME_DIRECTORY_MODE,
            )
        else:
            ensure_runtime_directory(current, ownership)


def _verify_staged_cache(staging: Path, plan: MigrationPlan, ownership: Ownership) -> None:
    actual = inventory_tree(
        staging,
        target=True,
        ownership=ownership,
        exclude_name=CACHE_MANIFEST_FILENAME,
    )
    if actual != plan.cache_manifest:
        raise MigrationError("cache-verify-failed")


def _publish_cache(staging: Path, target: Path) -> bool:
    published = True
    with open_directory(target.parent) as parent_descriptor:
        try:
            rename_noreplace(parent_descriptor, staging.name, target.name)
        except FileExistsError:
            published = False
        try:
            os.fsync(parent_descriptor)
        except OSError:
            raise MigrationError("cache-publish-sync-failed", (target.name,)) from None
    try:
        fsync_directory(target.parent)
    except OSError:
        raise MigrationError("cache-publish-sync-failed", (target.name,)) from None
    return published


def _validate_trusted_parent(parent: Path, env_uid: int) -> None:
    try:
        metadata = parent.lstat()
    except OSError:
        raise MigrationError("untrusted-cache-parent") from None
    if (
        stat.S_ISLNK(metadata.st_mode)
        or not stat.S_ISDIR(metadata.st_mode)
        or metadata.st_uid != env_uid
        or stat.S_IMODE(metadata.st_mode) & 0o022
    ):
        raise MigrationError("untrusted-cache-parent")


def _safe_relative(value: object) -> PurePosixPath:
    if not isinstance(value, str):
        raise MigrationError("cache-plan-invalid")
    path = PurePosixPath(value)
    if (
        not value
        or "\x00" in value
        or "\\" in value
        or path.is_absolute()
        or path.as_posix() != value
        or any(part in {"", ".", ".."} for part in path.parts)
    ):
        raise MigrationError("cache-plan-invalid")
    return path


def _snapshot_matches_entry(snapshot: FileSnapshot, entry: dict[str, object]) -> bool:
    return (
        snapshot.size == entry["size"]
        and snapshot.sha256 == entry["sha256"]
        and snapshot.mtime_ns == entry["mtime_ns"]
    )


def _entry_exists(path: Path) -> bool:
    try:
        path.lstat()
    except FileNotFoundError:
        return False
    except OSError:
        raise MigrationError("path-unavailable", (path.name,)) from None
    return True
