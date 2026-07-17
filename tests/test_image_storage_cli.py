from __future__ import annotations

import io
import json
import os
import stat
import tarfile
from pathlib import Path
from typing import BinaryIO

import pytest

from llm_gateway_core.services import image_storage_cli as storage_cli


def _write_image(path: Path, payload: bytes, *, mtime_ns: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    os.utime(path, ns=(mtime_ns, mtime_ns))


def _empty_outputs(root: Path) -> Path:
    outputs = root / "outputs"
    (outputs / "images").mkdir(parents=True)
    return outputs


def _fail_first_post_rename_sync(
    monkeypatch: pytest.MonkeyPatch,
    outputs: Path,
) -> None:
    original = storage_cli._fsync_directory
    failed = False

    def fail_once(directory: Path) -> None:
        nonlocal failed
        published_files = list((outputs / "images").rglob("*.png"))
        if directory == outputs and published_files and not failed:
            failed = True
            raise storage_cli.ImageStorageCliError("directory-sync-failed")
        original(directory)

    monkeypatch.setattr(storage_cli, "_fsync_directory", fail_once)


def test_inventory_is_sorted_and_captures_hash_size_and_mtime(tmp_path: Path) -> None:
    images = tmp_path / "images"
    _write_image(images / "z.png", b"z", mtime_ns=1_700_000_000_000_000_009)
    _write_image(
        images / "research-a.png",
        b"top",
        mtime_ns=1_700_000_000_000_000_001,
    )
    _write_image(
        images / "research-a" / "a.png",
        b"alpha",
        mtime_ns=1_700_000_000_000_000_003,
    )

    inventory = storage_cli.build_inventory(images)

    assert [entry.path for entry in inventory.files] == [
        "research-a.png",
        "research-a/a.png",
        "z.png",
    ]
    assert inventory.files[1].size == 5
    assert inventory.files[1].mtime_ns == 1_700_000_000_000_000_003
    assert len(inventory.files[1].sha256) == 64
    assert len(inventory.tree_sha256) == 64
    manifest = tmp_path / "manifest.json"
    storage_cli.write_manifest(manifest, inventory)
    assert storage_cli.read_manifest(manifest) == inventory


@pytest.mark.parametrize("kind", ["symlink", "fifo"])
def test_inventory_rejects_links_and_special_files(
    tmp_path: Path,
    kind: str,
) -> None:
    images = tmp_path / "images"
    images.mkdir()
    if kind == "symlink":
        (images / "bad.png").symlink_to(tmp_path / "outside.png")
    else:
        os.mkfifo(images / "bad.png")

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.build_inventory(images)

    assert caught.value.reason == "inventory-entry-unsupported"
    assert str(tmp_path) not in str(caught.value)


def test_inventory_rejects_file_replacement_during_fd_hash(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    image = images / "image.png"
    _write_image(image, b"first", mtime_ns=5)
    moved = images / "moved.png"
    original_read = storage_cli.os.read
    replaced = False

    def replace_before_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and stat.S_ISREG(os.fstat(descriptor).st_mode):
            replaced = True
            os.replace(image, moved)
            _write_image(image, b"other", mtime_ns=5)
        return original_read(descriptor, size)

    monkeypatch.setattr(storage_cli.os, "read", replace_before_read)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.build_inventory(images)

    assert caught.value.reason == "inventory-source-changed"


def test_fsync_directory_rejects_symlink_path(tmp_path: Path) -> None:
    directory = tmp_path / "directory"
    directory.mkdir()
    link = tmp_path / "link"
    link.symlink_to(directory, target_is_directory=True)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli._fsync_directory(link)

    assert caught.value.reason == "directory-sync-failed"


def test_manifest_is_private_and_rejects_tampered_digest(tmp_path: Path) -> None:
    images = tmp_path / "images"
    _write_image(images / "one.png", b"one", mtime_ns=10)
    manifest = tmp_path / "manifest.json"
    inventory = storage_cli.build_inventory(images)

    storage_cli.write_manifest(manifest, inventory)

    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert storage_cli.read_manifest(manifest) == inventory
    payload = json.loads(manifest.read_text(encoding="utf-8"))
    payload["tree_sha256"] = "0" * 64
    manifest.write_text(json.dumps(payload), encoding="utf-8")
    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.read_manifest(manifest)
    assert caught.value.reason == "manifest-digest-mismatch"


def test_manifest_read_rejects_leaf_replacement_after_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    _write_image(images / "image.png", b"image", mtime_ns=9)
    manifest = tmp_path / "manifest.json"
    storage_cli.write_manifest(manifest, storage_cli.build_inventory(images))
    payload = manifest.read_bytes()
    original_read = storage_cli.os.read
    replaced = False

    def replace_before_read(descriptor: int, size: int) -> bytes:
        nonlocal replaced
        if not replaced and stat.S_ISREG(os.fstat(descriptor).st_mode):
            replaced = True
            os.replace(manifest, tmp_path / "moved-manifest.json")
            manifest.write_bytes(payload)
        return original_read(descriptor, size)

    monkeypatch.setattr(storage_cli.os, "read", replace_before_read)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.read_manifest(manifest)

    assert caught.value.reason == "manifest-changed"


@pytest.mark.parametrize("failure", ["mkdir", "second-lstat"])
def test_empty_target_creation_errors_are_target_unavailable(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    failure: str,
) -> None:
    outputs = tmp_path / "outputs"
    outputs.mkdir()
    images = outputs / "images"
    original_mkdir = Path.mkdir
    original_lstat = Path.lstat
    lstat_calls = 0

    def injected_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        if path == images and failure == "mkdir":
            raise OSError("injected mkdir failure")
        original_mkdir(path, *args, **kwargs)

    def injected_lstat(path: Path) -> os.stat_result:
        nonlocal lstat_calls
        if path == images:
            lstat_calls += 1
            if lstat_calls == 1:
                raise FileNotFoundError
            if failure == "second-lstat":
                raise OSError("injected lstat failure")
        return original_lstat(path)

    monkeypatch.setattr(Path, "mkdir", injected_mkdir)
    monkeypatch.setattr(Path, "lstat", injected_lstat)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli._require_empty_images_target(outputs)

    assert caught.value.reason == "target-unavailable"


@pytest.mark.skipif(os.geteuid() != 0, reason="root ownership contract requires root")
def test_init_volume_only_updates_managed_directories_without_inventory_scan(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    image = outputs / "images" / "existing.png"
    _write_image(image, b"existing", mtime_ns=1_700_000_000_000_000_011)
    image.chmod(0o640)
    unknown = outputs / "unmanaged.bin"
    _write_image(unknown, b"unmanaged", mtime_ns=1_700_000_000_000_000_012)
    unknown.chmod(0o604)
    image_metadata = image.stat()
    unknown_metadata = unknown.stat()

    def reject_inventory_scan(_images_dir: Path) -> storage_cli.ImageInventory:
        raise AssertionError("init-volume must not hash existing content")

    monkeypatch.setattr(storage_cli, "build_inventory", reject_inventory_scan)

    first = storage_cli.initialize_volume(outputs)
    second = storage_cli.initialize_volume(outputs)

    assert first is None
    assert second is None
    assert image.read_bytes() == b"existing"
    assert (
        image.stat().st_uid,
        image.stat().st_gid,
        stat.S_IMODE(image.stat().st_mode),
        image.stat().st_mtime_ns,
    ) == (
        image_metadata.st_uid,
        image_metadata.st_gid,
        stat.S_IMODE(image_metadata.st_mode),
        image_metadata.st_mtime_ns,
    )
    assert (
        unknown.stat().st_uid,
        unknown.stat().st_gid,
        stat.S_IMODE(unknown.stat().st_mode),
        unknown.stat().st_mtime_ns,
    ) == (
        unknown_metadata.st_uid,
        unknown_metadata.st_gid,
        stat.S_IMODE(unknown_metadata.st_mode),
        unknown_metadata.st_mtime_ns,
    )
    for directory in (outputs, outputs / "images"):
        metadata = directory.stat()
        assert (metadata.st_uid, metadata.st_gid) == (10001, 10001)
        assert stat.S_IMODE(metadata.st_mode) == 0o770


@pytest.mark.skipif(os.geteuid() != 0, reason="root ownership contract requires root")
def test_init_volume_fsyncs_images_outputs_and_parent_bottom_up(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = tmp_path / "outputs"
    original_fsync = storage_cli.os.fsync
    synced: list[Path] = []

    def record_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            synced.append(Path(os.readlink(f"/proc/self/fd/{descriptor}")))
        original_fsync(descriptor)

    monkeypatch.setattr(storage_cli.os, "fsync", record_fsync)

    storage_cli.initialize_volume(outputs)

    assert synced[-3:] == [outputs / "images", outputs, tmp_path]


def test_init_volume_requires_root(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setattr(storage_cli.os, "geteuid", lambda: 10001)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.initialize_volume(tmp_path / "outputs")

    assert caught.value.reason == "root-required"


@pytest.mark.parametrize("value", ["/", "//", "/tmp/..", "/tmp/"])
def test_init_volume_rejects_unsafe_path_before_mutation(
    value: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mutation_attempts: list[Path] = []

    def reject_mkdir(path: Path, *args: object, **kwargs: object) -> None:
        del args, kwargs
        mutation_attempts.append(path)
        raise AssertionError("unsafe path reached mutation boundary")

    monkeypatch.setattr(storage_cli.os, "geteuid", lambda: 0)
    monkeypatch.setattr(Path, "mkdir", reject_mkdir)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.initialize_volume(value)

    assert caught.value.reason == "path-unsafe"
    assert mutation_attempts == []


def test_migrate_and_restore_require_root_before_storage_mutation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    monkeypatch.setattr(storage_cli.os, "geteuid", lambda: 10001)

    with pytest.raises(storage_cli.ImageStorageCliError) as migrate_error:
        storage_cli.migrate_images(
            tmp_path / "missing-source",
            tmp_path / "missing-outputs",
            tmp_path / "manifest.json",
        )
    with pytest.raises(storage_cli.ImageStorageCliError) as restore_error:
        storage_cli.restore_images(
            tmp_path / "missing-outputs",
            tmp_path / "archive.tar",
            tmp_path / "manifest.json",
        )

    assert migrate_error.value.reason == "root-required"
    assert restore_error.value.reason == "root-required"
    assert list(tmp_path.iterdir()) == []


def test_inventory_rejects_manifest_inside_images_before_write(
    capsys: pytest.CaptureFixture[str],
    tmp_path: Path,
) -> None:
    images = tmp_path / "images"
    _write_image(images / "source.png", b"source", mtime_ns=20)
    manifest = images / "manifest.json"

    exit_code = storage_cli.main(
        [
            "inventory",
            "--images-dir",
            str(images),
            "--manifest",
            str(manifest),
        ]
    )

    assert exit_code == 1
    assert capsys.readouterr().err == "image-storage-cli: reason=path-overlap\n"
    assert not manifest.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_migrate_rejects_intersecting_trees_before_mutation(tmp_path: Path) -> None:
    outputs = _empty_outputs(tmp_path)
    source = outputs / "legacy"
    _write_image(source / "source.png", b"source", mtime_ns=21)
    manifest = tmp_path / "migration.json"

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "path-overlap"
    assert not manifest.exists()
    assert not (outputs / storage_cli.INCOMPLETE_MARKER).exists()
    assert not any((outputs / "images").iterdir())


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_migrate_nonempty_precondition_does_not_rewrite_manifest(tmp_path: Path) -> None:
    source = tmp_path / "legacy"
    _write_image(source / "source.png", b"source", mtime_ns=24)
    outputs = _empty_outputs(tmp_path / "target")
    _write_image(outputs / "images" / "keep.png", b"keep", mtime_ns=25)
    manifest = tmp_path / "migration.json"
    manifest.write_bytes(b"keep-manifest")

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "target-not-empty"
    assert manifest.read_bytes() == b"keep-manifest"


def test_backup_rejects_artifact_inside_outputs_before_write(tmp_path: Path) -> None:
    outputs = _empty_outputs(tmp_path)
    _write_image(outputs / "images" / "source.png", b"source", mtime_ns=22)
    archive = outputs / "backup.tar"
    manifest = tmp_path / "backup.json"

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.backup_images(outputs, archive, manifest)

    assert caught.value.reason == "path-overlap"
    assert not archive.exists()
    assert not manifest.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore requires root")
def test_restore_rejects_source_inside_outputs_before_mutation(tmp_path: Path) -> None:
    outputs = _empty_outputs(tmp_path)
    archive = outputs / "restore.tar"
    archive.write_bytes(b"not-read")
    source = tmp_path / "source"
    _write_image(source / "source.png", b"source", mtime_ns=23)
    manifest = tmp_path / "restore.json"
    storage_cli.write_manifest(manifest, storage_cli.build_inventory(source))

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.restore_images(outputs, archive, manifest)

    assert caught.value.reason == "path-overlap"
    assert not (outputs / storage_cli.INCOMPLETE_MARKER).exists()
    assert not any((outputs / "images").iterdir())


@pytest.mark.parametrize("operation", ["backup", "restore"])
def test_archive_and_manifest_must_be_distinct_before_write(
    operation: str,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    artifact = tmp_path / "same-artifact"

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        if operation == "backup":
            storage_cli.backup_images(outputs, artifact, artifact)
        else:
            storage_cli.restore_images(outputs, artifact, artifact)

    assert caught.value.reason == "artifact-path-conflict"
    assert not artifact.exists()
    assert not (outputs / storage_cli.INCOMPLETE_MARKER).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
def test_migrate_preserves_inventory_and_never_deletes_source(tmp_path: Path) -> None:
    source = tmp_path / "legacy-images"
    _write_image(
        source / "research-1" / "image.png",
        b"legacy",
        mtime_ns=1_700_000_000_000_000_021,
    )
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration-manifest.json"
    expected = storage_cli.build_inventory(source)

    result = storage_cli.migrate_images(source, outputs, manifest)

    assert result == expected
    assert storage_cli.read_manifest(manifest) == expected
    assert storage_cli.build_inventory(outputs / "images") == expected
    assert storage_cli.build_inventory(source) == expected
    assert not (outputs / storage_cli.INCOMPLETE_MARKER).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_migrate_retry_finishes_post_rename_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-images"
    _write_image(source / "image.png", b"source", mtime_ns=25)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration.json"
    expected = storage_cli.build_inventory(source)
    _fail_first_post_rename_sync(monkeypatch, outputs)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "directory-sync-failed"
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert storage_cli.build_inventory(outputs / "images") == expected

    assert storage_cli.migrate_images(source, outputs, manifest) == expected
    assert not (outputs / storage_cli.INCOMPLETE_MARKER).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_marker_is_restored_when_post_unlink_sync_fails(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    from llm_gateway_core.services.image_storage import (
        GeneratedImageStorage,
        ImageStorageError,
    )

    source = tmp_path / "legacy-images"
    _write_image(source / "image.png", b"source", mtime_ns=28)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration.json"
    marker = outputs / storage_cli.INCOMPLETE_MARKER
    original_sync = storage_cli._fsync_directory
    failed = False

    def fail_after_marker_unlink(directory: Path) -> None:
        nonlocal failed
        if (
            directory == outputs
            and (outputs / "images" / "image.png").exists()
            and not marker.exists()
            and not failed
        ):
            failed = True
            raise storage_cli.ImageStorageCliError("directory-sync-failed")
        original_sync(directory)

    monkeypatch.setattr(storage_cli, "_fsync_directory", fail_after_marker_unlink)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "directory-sync-failed"
    assert marker.is_file()
    with pytest.raises(ImageStorageError) as probe_error:
        GeneratedImageStorage(outputs / "images").probe()
    assert probe_error.value.reason == "restore-incomplete"

    storage_cli.migrate_images(source, outputs, manifest)
    assert not marker.exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_migrate_retry_keeps_marker_when_published_inventory_mismatches(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-images"
    _write_image(source / "image.png", b"source", mtime_ns=26)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration.json"
    _fail_first_post_rename_sync(monkeypatch, outputs)

    with pytest.raises(storage_cli.ImageStorageCliError):
        storage_cli.migrate_images(source, outputs, manifest)
    _write_image(outputs / "images" / "image.png", b"tampered", mtime_ns=27)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "incomplete-target-mismatch"
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
def test_migration_mismatch_leaves_marker_and_source_unchanged(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-images"
    _write_image(source / "image.png", b"original", mtime_ns=31)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration-manifest.json"
    expected = storage_cli.build_inventory(source)
    original_copy = storage_cli._copy_stream

    def corrupt_copy(
        stream: object,
        destination: Path,
        *,
        expected: storage_cli.InventoryEntry,
    ) -> None:
        original_copy(  # type: ignore[arg-type]
            stream,
            destination,
            expected=expected,
        )
        destination.write_bytes(b"corrupt")

    monkeypatch.setattr(storage_cli, "_copy_stream", corrupt_copy)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "restored-inventory-mismatch"
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert not any((outputs / "images").iterdir())
    assert storage_cli.build_inventory(source) == expected


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_migrate_rejects_source_path_replacement_after_open(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy-images"
    source_file = source / "image.png"
    _write_image(source_file, b"original", mtime_ns=35)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration.json"
    original_copy = storage_cli._copy_stream
    replaced = False

    def replace_source(
        stream: BinaryIO,
        destination: Path,
        *,
        expected: storage_cli.InventoryEntry,
    ) -> None:
        nonlocal replaced
        if not replaced:
            replaced = True
            os.replace(source_file, source / "moved.png")
            _write_image(source_file, b"replaced", mtime_ns=expected.mtime_ns)
        original_copy(stream, destination, expected=expected)

    monkeypatch.setattr(storage_cli, "_copy_stream", replace_source)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "source-changed"
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert not any((outputs / "images").iterdir())


@pytest.mark.skipif(os.geteuid() != 0, reason="restore metadata requires root")
def test_copy_applies_final_metadata_before_file_fsync(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    destination = tmp_path / "image.png"
    expected = storage_cli.InventoryEntry(
        path="image.png",
        size=5,
        sha256="0" * 64,
        mtime_ns=1_700_000_000_000_000_081,
    )
    original_fsync = storage_cli.os.fsync
    observed: list[tuple[int, int, int, int]] = []

    def inspect_fsync(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISREG(metadata.st_mode):
            observed.append(
                (
                    metadata.st_uid,
                    metadata.st_gid,
                    stat.S_IMODE(metadata.st_mode),
                    metadata.st_mtime_ns,
                )
            )
        original_fsync(descriptor)

    monkeypatch.setattr(storage_cli.os, "fsync", inspect_fsync)

    storage_cli._copy_stream(io.BytesIO(b"image"), destination, expected=expected)

    assert observed == [(10001, 10001, 0o660, expected.mtime_ns)]


@pytest.mark.skipif(os.geteuid() != 0, reason="migration requires root")
def test_staging_directories_sync_bottom_up_before_failed_publish(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source = tmp_path / "legacy"
    _write_image(source / "a" / "b" / "image.png", b"image", mtime_ns=36)
    outputs = _empty_outputs(tmp_path / "target")
    manifest = tmp_path / "migration.json"
    original_fsync = storage_cli.os.fsync
    staging_syncs: list[Path] = []

    def fail_on_staging_root(descriptor: int) -> None:
        metadata = os.fstat(descriptor)
        if stat.S_ISDIR(metadata.st_mode):
            path = Path(os.readlink(f"/proc/self/fd/{descriptor}"))
            if ".image-restore-staging-" in path.as_posix():
                staging_syncs.append(path)
                if path.name.startswith(".image-restore-staging-"):
                    raise OSError("injected staging sync failure")
        original_fsync(descriptor)

    monkeypatch.setattr(storage_cli.os, "fsync", fail_on_staging_root)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.migrate_images(source, outputs, manifest)

    assert caught.value.reason == "volume-metadata-failed"
    assert [path.name for path in staging_syncs] == [
        "b",
        "a",
        staging_syncs[-1].name,
    ]
    assert staging_syncs[-1].name.startswith(".image-restore-staging-")
    assert not any((outputs / "images").iterdir())
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
def test_backup_restore_round_trip_preserves_inventory(tmp_path: Path) -> None:
    source_outputs = _empty_outputs(tmp_path / "source")
    _write_image(
        source_outputs / "images" / "research-2" / "image.png",
        b"round-trip",
        mtime_ns=1_700_000_000_000_000_041,
    )
    archive = tmp_path / "backup" / "images.tar"
    manifest = tmp_path / "backup" / "images.manifest.json"
    archive.parent.mkdir()

    expected = storage_cli.backup_images(source_outputs, archive, manifest)
    restored_outputs = _empty_outputs(tmp_path / "restored")
    restored = storage_cli.restore_images(restored_outputs, archive, manifest)

    assert restored == expected
    assert storage_cli.build_inventory(restored_outputs / "images") == expected
    assert stat.S_IMODE(archive.stat().st_mode) == 0o600
    assert stat.S_IMODE(manifest.stat().st_mode) == 0o600
    assert not (restored_outputs / storage_cli.INCOMPLETE_MARKER).exists()


def test_backup_verifies_written_tar_before_manifest_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    _write_image(outputs / "images" / "image.png", b"image", mtime_ns=46)
    archive_path = tmp_path / "backup.tar"
    manifest = tmp_path / "backup.json"
    original_verify = storage_cli._verify_written_archive

    def corrupt_then_verify(
        stream: BinaryIO,
        expected: storage_cli.ImageInventory,
    ) -> None:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            member = next(item for item in archive.getmembers() if item.isfile())
            offset = member.offset_data
        stream.seek(offset)
        original = stream.read(1)
        stream.seek(offset)
        stream.write(bytes([original[0] ^ 0xFF]))
        stream.flush()
        original_verify(stream, expected)

    monkeypatch.setattr(storage_cli, "_verify_written_archive", corrupt_then_verify)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.backup_images(outputs, archive_path, manifest)

    assert caught.value.reason == "archive-inventory-mismatch"
    assert not archive_path.exists()
    assert not manifest.exists()


def test_backup_refuses_to_overwrite_existing_pair_before_staging(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    image = outputs / "images" / "image.png"
    _write_image(image, b"old", mtime_ns=47)
    archive = tmp_path / "backup.tar"
    manifest = tmp_path / "backup.json"
    previous = storage_cli.backup_images(outputs, archive, manifest)
    previous_archive = archive.read_bytes()
    previous_manifest = manifest.read_bytes()
    _write_image(image, b"new", mtime_ns=48)

    def unexpected_staging(*args: object, **kwargs: object) -> None:
        del args, kwargs
        raise AssertionError("backup staging must not start")

    monkeypatch.setattr(storage_cli, "_write_archive", unexpected_staging)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.backup_images(outputs, archive, manifest)

    assert caught.value.reason == "backup-artifact-exists"
    assert archive.read_bytes() == previous_archive
    assert manifest.read_bytes() == previous_manifest
    assert storage_cli.read_manifest(manifest) == previous
    assert not list(tmp_path.glob(".*.staged-*"))


def test_backup_manifest_staging_failure_leaves_fresh_paths_empty(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    _write_image(outputs / "images" / "image.png", b"image", mtime_ns=49)
    archive = tmp_path / "backup.tar"
    manifest = tmp_path / "backup.json"

    def fail_manifest_staging(
        path: Path,
        inventory: storage_cli.ImageInventory,
    ) -> None:
        del path, inventory
        raise storage_cli.ImageStorageCliError("manifest-stage-failed")

    monkeypatch.setattr(storage_cli, "write_manifest", fail_manifest_staging)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.backup_images(outputs, archive, manifest)

    assert caught.value.reason == "manifest-stage-failed"
    assert not archive.exists()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".*.staged-*"))


def test_backup_second_publication_failure_removes_fresh_first_artifact(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    outputs = _empty_outputs(tmp_path)
    _write_image(outputs / "images" / "image.png", b"image", mtime_ns=50)
    archive = tmp_path / "backup.tar"
    manifest = tmp_path / "backup.json"
    original_link = storage_cli.os.link

    def fail_manifest_publication(
        source: str,
        destination: str,
        **kwargs: object,
    ) -> None:
        if destination == manifest.name:
            raise OSError("injected manifest publication failure")
        original_link(source, destination, **kwargs)

    monkeypatch.setattr(storage_cli.os, "link", fail_manifest_publication)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.backup_images(outputs, archive, manifest)

    assert caught.value.reason == "backup-publish-failed"
    assert not archive.exists()
    assert not manifest.exists()
    assert not list(tmp_path.glob(".*.staged-*"))


def test_repeated_backup_keeps_file_descriptor_count_bounded(tmp_path: Path) -> None:
    outputs = _empty_outputs(tmp_path)
    for index in range(4):
        _write_image(
            outputs / "images" / f"image-{index}.png",
            f"image-{index}".encode(),
            mtime_ns=50 + index,
        )
    before = len(os.listdir("/proc/self/fd"))

    for iteration in range(12):
        archive = tmp_path / f"backup-{iteration}.tar"
        manifest = tmp_path / f"backup-{iteration}.json"
        storage_cli.backup_images(outputs, archive, manifest)

    after = len(os.listdir("/proc/self/fd"))
    assert after <= before + 1


@pytest.mark.skipif(os.geteuid() != 0, reason="restore requires root")
def test_restore_retry_finishes_post_rename_publication(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    source_outputs = _empty_outputs(tmp_path / "source")
    _write_image(
        source_outputs / "images" / "image.png",
        b"source",
        mtime_ns=45,
    )
    archive = tmp_path / "backup.tar"
    manifest = tmp_path / "backup.json"
    expected = storage_cli.backup_images(source_outputs, archive, manifest)
    restored_outputs = _empty_outputs(tmp_path / "restored")
    _fail_first_post_rename_sync(monkeypatch, restored_outputs)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.restore_images(restored_outputs, archive, manifest)

    assert caught.value.reason == "directory-sync-failed"
    assert (restored_outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert storage_cli.restore_images(restored_outputs, archive, manifest) == expected
    assert not (restored_outputs / storage_cli.INCOMPLETE_MARKER).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore requires root")
def test_restore_retry_revalidates_corrupt_archive_for_empty_manifest(
    tmp_path: Path,
) -> None:
    empty_source = tmp_path / "empty-source"
    empty_source.mkdir()
    manifest = tmp_path / "empty.json"
    storage_cli.write_manifest(manifest, storage_cli.build_inventory(empty_source))
    archive = tmp_path / "corrupt.tar"
    archive.write_bytes(b"not a tar archive")
    restored_outputs = _empty_outputs(tmp_path / "restored")
    marker = restored_outputs / storage_cli.INCOMPLETE_MARKER

    for _attempt in range(2):
        with pytest.raises(storage_cli.ImageStorageCliError) as caught:
            storage_cli.restore_images(restored_outputs, archive, manifest)

        assert caught.value.reason == "archive-read-failed"
        assert marker.is_file()
        assert not any((restored_outputs / "images").iterdir())


@pytest.mark.skipif(os.geteuid() != 0, reason="restore requires root")
def test_restore_retry_revalidates_archive_payload_digest(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    mtime_ns = 1_700_000_000_000_000_047
    _write_image(source / "image.png", b"good", mtime_ns=mtime_ns)
    expected = storage_cli.build_inventory(source)
    manifest = tmp_path / "manifest.json"
    storage_cli.write_manifest(manifest, expected)
    archive_path = tmp_path / "corrupt-payload.tar"
    with tarfile.open(archive_path, mode="w", format=tarfile.PAX_FORMAT) as archive:
        member = tarfile.TarInfo("images/image.png")
        member.size = 4
        member.pax_headers = {"LLMGateway.mtime_ns": str(mtime_ns)}
        archive.addfile(member, io.BytesIO(b"evil"))
    restored_outputs = _empty_outputs(tmp_path / "restored")
    marker = restored_outputs / storage_cli.INCOMPLETE_MARKER

    with pytest.raises(storage_cli.ImageStorageCliError) as first:
        storage_cli.restore_images(restored_outputs, archive_path, manifest)
    assert first.value.reason == "restored-inventory-mismatch"
    assert marker.is_file()

    with pytest.raises(storage_cli.ImageStorageCliError) as second:
        storage_cli.restore_images(restored_outputs, archive_path, manifest)
    assert second.value.reason == "archive-inventory-mismatch"
    assert marker.is_file()
    assert not any((restored_outputs / "images").iterdir())


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
def test_restore_refuses_non_empty_target_without_mutation(tmp_path: Path) -> None:
    source_outputs = _empty_outputs(tmp_path / "source")
    _write_image(source_outputs / "images" / "source.png", b"source", mtime_ns=51)
    archive = tmp_path / "images.tar"
    manifest = tmp_path / "manifest.json"
    storage_cli.backup_images(source_outputs, archive, manifest)
    target_outputs = _empty_outputs(tmp_path / "target")
    _write_image(target_outputs / "images" / "keep.png", b"keep", mtime_ns=52)

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.restore_images(target_outputs, archive, manifest)

    assert caught.value.reason == "target-not-empty"
    assert (target_outputs / "images" / "keep.png").read_bytes() == b"keep"
    assert not (target_outputs / storage_cli.INCOMPLETE_MARKER).exists()


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
@pytest.mark.parametrize("member_kind", ["symlink", "hardlink", "fifo", "duplicate"])
def test_restore_rejects_unsafe_archive_members(
    tmp_path: Path,
    member_kind: str,
) -> None:
    source = tmp_path / "source-images"
    _write_image(source / "image.png", b"safe", mtime_ns=61)
    inventory = storage_cli.build_inventory(source)
    manifest = tmp_path / "manifest.json"
    storage_cli.write_manifest(manifest, inventory)
    archive_path = tmp_path / "unsafe.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        member = tarfile.TarInfo("images/image.png")
        member.size = 4
        if member_kind == "symlink":
            member.type = tarfile.SYMTYPE
            member.linkname = "/outside"
            member.size = 0
            archive.addfile(member)
        elif member_kind == "hardlink":
            member.type = tarfile.LNKTYPE
            member.linkname = "images/elsewhere.png"
            member.size = 0
            archive.addfile(member)
        elif member_kind == "fifo":
            member.type = tarfile.FIFOTYPE
            member.size = 0
            archive.addfile(member)
        else:
            archive.addfile(member, io.BytesIO(b"safe"))
            archive.addfile(member, io.BytesIO(b"safe"))
    outputs = _empty_outputs(tmp_path / "target")

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.restore_images(outputs, archive_path, manifest)

    expected_reason = (
        "archive-member-duplicate"
        if member_kind == "duplicate"
        else "archive-member-unsupported"
    )
    assert caught.value.reason == expected_reason
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert not any((outputs / "images").iterdir())


@pytest.mark.skipif(os.geteuid() != 0, reason="restore ownership contract requires root")
def test_restore_rejects_archive_size_mismatch_before_writing(tmp_path: Path) -> None:
    source = tmp_path / "source-images"
    _write_image(source / "image.png", b"safe", mtime_ns=71)
    inventory = storage_cli.build_inventory(source)
    manifest = tmp_path / "manifest.json"
    storage_cli.write_manifest(manifest, inventory)
    archive_path = tmp_path / "oversized.tar"
    with tarfile.open(archive_path, mode="w") as archive:
        member = tarfile.TarInfo("images/image.png")
        member.size = 5
        archive.addfile(member, io.BytesIO(b"extra"))
    outputs = _empty_outputs(tmp_path / "target")

    with pytest.raises(storage_cli.ImageStorageCliError) as caught:
        storage_cli.restore_images(outputs, archive_path, manifest)

    assert caught.value.reason == "archive-size-mismatch"
    assert (outputs / storage_cli.INCOMPLETE_MARKER).is_file()
    assert not any((outputs / "images").iterdir())


def test_cli_errors_are_path_safe(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    relative = Path("relative-images")

    exit_code = storage_cli.main(
        [
            "inventory",
            "--images-dir",
            str(relative),
            "--manifest",
            str(tmp_path / "manifest.json"),
        ]
    )

    captured = capsys.readouterr()
    assert exit_code == 1
    assert captured.out == ""
    assert captured.err == "image-storage-cli: reason=path-not-absolute\n"
    assert str(tmp_path) not in captured.err
