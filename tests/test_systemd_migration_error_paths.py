from __future__ import annotations

import hashlib
import os
import shutil
from pathlib import Path
from types import SimpleNamespace

import pytest

from docker import _systemd_migration_apply as migration_apply
from docker import _systemd_migration_fs as migration_fs
from docker._systemd_migration_model import Artifact, MigrationError, Ownership


def test_sqlite_primary_error_wins_over_cleanup_failure(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "tokens_usage.db"
    source.write_bytes(b"source")
    snapshot = migration_fs.regular_snapshot(source, name=source.name)
    assert snapshot is not None
    cleanup_calls = 0

    def fail_stage(*_args: object) -> None:
        raise MigrationError("source-changed", (source.name,))

    def fail_cleanup(*_args: object) -> None:
        nonlocal cleanup_calls
        cleanup_calls += 1
        raise MigrationError("temporary-cleanup-failed", (source.name,))

    monkeypatch.setattr(migration_fs, "_stage_database", fail_stage)
    monkeypatch.setattr(migration_fs, "_cleanup_sqlite_temporary", fail_cleanup)
    artifact = Artifact(source.name, snapshot, None)
    ownership = Ownership(os.geteuid(), os.geteuid(), os.getegid())

    with pytest.raises(MigrationError) as caught:
        migration_fs.sqlite_backup(artifact, (), tmp_path / "target.db", ownership)

    assert caught.value.reason == "source-changed"
    assert caught.value.names == (source.name,)
    assert cleanup_calls == 1


@pytest.mark.parametrize("failed_sync", ["descriptor", "directory"])
def test_cache_post_rename_sync_failure_is_typed_and_keeps_published_target(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, failed_sync: str
) -> None:
    staging = tmp_path / "staging"
    target = tmp_path / "cache"
    staging.mkdir()

    def rename(parent_descriptor: int, source_name: str, target_name: str) -> None:
        os.rename(
            source_name,
            target_name,
            src_dir_fd=parent_descriptor,
            dst_dir_fd=parent_descriptor,
        )

    def fail_fsync(*_args: object) -> None:
        raise OSError("fsync")

    monkeypatch.setattr(migration_apply, "rename_noreplace", rename)
    if failed_sync == "descriptor":
        monkeypatch.setattr(migration_apply.os, "fsync", fail_fsync)
    else:
        monkeypatch.setattr(migration_apply, "fsync_directory", fail_fsync)

    with pytest.raises(MigrationError) as caught:
        migration_apply._publish_cache(staging, target)

    assert caught.value.reason == "cache-publish-sync-failed"
    assert caught.value.names == (target.name,)
    assert target.is_dir()
    assert not staging.exists()


def test_cache_concurrent_winner_is_synced_cleaned_and_reused(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    source = tmp_path / "source-cache"
    target = tmp_path / "cache"
    source.mkdir()
    manifest = {
        "version": 1,
        "count": 0,
        "tree_sha256": hashlib.sha256(b"[]").hexdigest(),
        "files": [],
    }
    plan = SimpleNamespace(source_cache_dir=source, target_cache_dir=target, cache_manifest=manifest)
    ownership = Ownership(os.geteuid(), os.geteuid(), os.getegid())
    sync_calls: list[str] = []
    publish_attempts = 0
    recording = False
    in_directory_sync = False
    real_fsync = migration_apply.os.fsync
    real_directory_sync = migration_apply.fsync_directory

    def concurrent_winner(_parent: int, staging_name: str, _target_name: str) -> None:
        nonlocal publish_attempts, recording
        publish_attempts += 1
        shutil.copytree(target.parent / staging_name, target)
        recording = True
        raise FileExistsError

    def track_fsync(descriptor: int) -> None:
        if recording and not in_directory_sync:
            sync_calls.append("descriptor")
        real_fsync(descriptor)

    def track_directory_sync(path: Path) -> None:
        nonlocal in_directory_sync, recording
        if recording:
            sync_calls.append("directory")
        in_directory_sync = True
        try:
            real_directory_sync(path)
        finally:
            in_directory_sync = False
            recording = False

    monkeypatch.setattr(migration_apply, "rename_noreplace", concurrent_winner)
    monkeypatch.setattr(migration_apply.os, "fsync", track_fsync)
    monkeypatch.setattr(migration_apply, "fsync_directory", track_directory_sync)

    assert migration_apply._copy_cache(plan, ownership) == "existing"
    assert sync_calls == ["descriptor", "directory"]
    assert publish_attempts == 1
    assert not list(tmp_path.glob(".cache.migration-*"))
    assert migration_apply._copy_cache(plan, ownership) == "existing"
    assert publish_attempts == 1
