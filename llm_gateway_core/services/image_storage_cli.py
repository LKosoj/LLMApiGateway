"""Offline administration for the generated-image volume.

The gateway must be stopped while ``migrate`` or ``restore`` runs.  The CLI is
deliberately standard-library-only so the same verified image can initialize
and maintain its project-scoped Compose volume.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import stat
import sys
import tarfile
import uuid
from pathlib import Path
from typing import BinaryIO, Sequence

from llm_gateway_core.services._image_storage_cli_archive import (
    _link_staged_artifact,
    _publish_new_backup_pair,
    _require_new_backup_artifacts,
    _unlink_owned_artifact,
    _validated_archive_members,
)
from llm_gateway_core.services._image_storage_cli_copy import (
    _copy_stream,
    _open_regular_source,
    _prepare_parent_directories,
    _prepare_staging,
    _require_empty_images_target,
    _sync_runtime_directory,
    _sync_staging_directories,
    initialize_volume,
)
from llm_gateway_core.services._image_storage_cli_inventory import (
    ARCHIVE_PREFIX,
    DIRECTORY_MODE,
    FILE_MODE,
    INCOMPLETE_MARKER,
    MANIFEST_VERSION,
    PRIVATE_FILE_MODE,
    RUNTIME_GID,
    RUNTIME_UID,
    ImageInventory,
    ImageStorageCliError,
    InventoryEntry,
    _canonical_path,
    _fsync_directory,
    _hash_open_file,
    _inventory_digest,
    _open_directory_path,
    _reject_symlink_components,
    _require_absolute,
    _require_disjoint_paths,
    _require_distinct_artifacts,
    _require_safe_cli_path,
    _safe_relative_path,
    _same_identity,
    _same_regular_snapshot,
    _write_private_atomic,
    build_inventory,
    read_manifest,
    write_manifest,
)


__all__ = [
    "ARCHIVE_PREFIX",
    "DIRECTORY_MODE",
    "FILE_MODE",
    "INCOMPLETE_MARKER",
    "MANIFEST_VERSION",
    "PRIVATE_FILE_MODE",
    "RUNTIME_GID",
    "RUNTIME_UID",
    "ImageInventory",
    "ImageStorageCliError",
    "InventoryEntry",
    "_canonical_path",
    "_copy_stream",
    "_fsync_directory",
    "_hash_open_file",
    "_inventory_digest",
    "_link_staged_artifact",
    "_open_directory_path",
    "_open_regular_source",
    "_prepare_parent_directories",
    "_prepare_staging",
    "_publish_new_backup_pair",
    "_reject_symlink_components",
    "_require_absolute",
    "_require_disjoint_paths",
    "_require_distinct_artifacts",
    "_require_empty_images_target",
    "_require_new_backup_artifacts",
    "_require_safe_cli_path",
    "_safe_relative_path",
    "_same_identity",
    "_same_regular_snapshot",
    "_sync_runtime_directory",
    "_sync_staging_directories",
    "_unlink_owned_artifact",
    "_validated_archive_members",
    "_write_private_atomic",
    "backup_images",
    "build_inventory",
    "initialize_volume",
    "main",
    "migrate_images",
    "read_manifest",
    "restore_images",
    "write_manifest",
]


def _create_incomplete_marker(outputs_dir: Path) -> Path:
    marker = outputs_dir / INCOMPLETE_MARKER
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(marker, flags, PRIVATE_FILE_MODE)
        try:
            payload = memoryview(b"incomplete\n")
            while payload:
                written = os.write(descriptor, payload)
                if written <= 0:
                    raise ImageStorageCliError("incomplete-marker-failed")
                payload = payload[written:]
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
        _fsync_directory(outputs_dir)
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("incomplete-marker-failed") from error
    return marker


def _existing_incomplete_marker(outputs_dir: Path) -> Path | None:
    marker = outputs_dir / INCOMPLETE_MARKER
    try:
        metadata = marker.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ImageStorageCliError("storage-incomplete") from error
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        raise ImageStorageCliError("storage-incomplete")
    return marker


def _clear_incomplete_marker(marker: Path, outputs_dir: Path) -> None:
    try:
        marker.unlink()
    except OSError as error:
        raise ImageStorageCliError("restore-publish-failed") from error
    try:
        _fsync_directory(outputs_dir)
    except (ImageStorageCliError, OSError) as error:
        try:
            _create_incomplete_marker(outputs_dir)
        except (ImageStorageCliError, OSError):
            pass
        if isinstance(error, ImageStorageCliError):
            raise
        raise ImageStorageCliError("restore-publish-failed") from error


def _recover_completed_publication(
    outputs_dir: Path,
    expected: ImageInventory,
) -> ImageInventory | None:
    marker = _existing_incomplete_marker(outputs_dir)
    if marker is None:
        return None
    images_dir = outputs_dir / "images"
    try:
        images_dir.lstat()
    except FileNotFoundError:
        return None
    except OSError as error:
        raise ImageStorageCliError("storage-incomplete") from error
    actual = build_inventory(images_dir)
    if actual == expected:
        _clear_incomplete_marker(marker, outputs_dir)
        return actual
    if actual.count:
        raise ImageStorageCliError("incomplete-target-mismatch")
    return None


def _verify_and_publish(
    *,
    outputs_dir: Path,
    empty_images_dir: Path,
    staging: Path,
    expected: ImageInventory,
    marker: Path,
) -> ImageInventory:
    actual = build_inventory(staging)
    if actual != expected:
        raise ImageStorageCliError("restored-inventory-mismatch")
    _sync_staging_directories(staging)
    _fsync_directory(outputs_dir)
    try:
        empty_images_dir.rmdir()
        os.replace(staging, empty_images_dir)
        _fsync_directory(outputs_dir)
        _clear_incomplete_marker(marker, outputs_dir)
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("restore-publish-failed") from error
    return actual


def migrate_images(
    source_images: str | os.PathLike[str],
    outputs_dir: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> ImageInventory:
    if os.geteuid() != 0:
        raise ImageStorageCliError("root-required")
    source_images = _require_safe_cli_path(source_images)
    outputs_dir = _require_safe_cli_path(outputs_dir)
    manifest_path = _require_safe_cli_path(manifest_path)
    _require_disjoint_paths(source_images, outputs_dir)
    _require_disjoint_paths(source_images, manifest_path)
    _require_disjoint_paths(outputs_dir, manifest_path)
    _reject_symlink_components(source_images)
    _reject_symlink_components(outputs_dir)
    _reject_symlink_components(manifest_path, allow_missing_leaf=True)
    expected = build_inventory(source_images)
    recovered = _recover_completed_publication(outputs_dir, expected)
    if recovered is not None:
        write_manifest(manifest_path, expected)
        return recovered
    empty_images = _require_empty_images_target(outputs_dir)
    write_manifest(manifest_path, expected)
    marker = _create_incomplete_marker(outputs_dir)
    staging = _prepare_staging(outputs_dir)
    try:
        for entry in expected.files:
            relative = _safe_relative_path(entry.path)
            destination = staging.joinpath(*relative.parts)
            _prepare_parent_directories(staging, relative)
            source = source_images.joinpath(*relative.parts)
            _reject_symlink_components(source)
            with _open_regular_source(source, entry) as stream:
                _copy_stream(stream, destination, expected=entry)
        return _verify_and_publish(
            outputs_dir=outputs_dir,
            empty_images_dir=empty_images,
            staging=staging,
            expected=expected,
            marker=marker,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _verify_archive_inventory(
    archive: tarfile.TarFile,
    expected: ImageInventory,
) -> None:
    members = _validated_archive_members(archive, expected)
    verified: list[InventoryEntry] = []
    for entry in expected.files:
        member = members[entry.path]
        if member.pax_headers.get("LLMGateway.mtime_ns") != str(entry.mtime_ns):
            raise ImageStorageCliError("archive-mtime-mismatch")
        source = archive.extractfile(member)
        if source is None:
            raise ImageStorageCliError("archive-member-unreadable")
        digest = hashlib.sha256()
        total = 0
        with source:
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > entry.size:
                    raise ImageStorageCliError("archive-size-mismatch")
                digest.update(chunk)
        if total != entry.size:
            raise ImageStorageCliError("archive-size-mismatch")
        verified.append(
            InventoryEntry(
                path=entry.path,
                size=total,
                sha256=digest.hexdigest(),
                mtime_ns=entry.mtime_ns,
            )
        )
    actual = ImageInventory(tuple(verified), _inventory_digest(verified))
    if actual != expected:
        raise ImageStorageCliError("archive-inventory-mismatch")


def _verify_written_archive(
    stream: BinaryIO,
    expected: ImageInventory,
) -> None:
    try:
        stream.seek(0)
        with tarfile.open(fileobj=stream, mode="r:") as archive:
            _verify_archive_inventory(archive, expected)
    except ImageStorageCliError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ImageStorageCliError("archive-read-failed") from error


def _write_archive(archive_path: Path, images_dir: Path, inventory: ImageInventory) -> None:
    archive_path = _require_absolute(archive_path)
    _reject_symlink_components(archive_path.parent)
    temporary = archive_path.with_name(f".{archive_path.name}.tmp-{uuid.uuid4().hex}")
    descriptor = -1
    archive_stream: BinaryIO | None = None
    try:
        descriptor = os.open(
            temporary,
            os.O_RDWR | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            PRIVATE_FILE_MODE,
        )
        archive_stream = os.fdopen(descriptor, "w+b", closefd=False)
        with tarfile.open(
            fileobj=archive_stream,
            mode="w",
            format=tarfile.PAX_FORMAT,
        ) as archive:
            for entry in inventory.files:
                relative = _safe_relative_path(entry.path)
                member = tarfile.TarInfo(f"{ARCHIVE_PREFIX}/{entry.path}")
                member.size = entry.size
                member.mode = FILE_MODE
                member.uid = RUNTIME_UID
                member.gid = RUNTIME_GID
                member.mtime = entry.mtime_ns // 1_000_000_000
                member.pax_headers = {"LLMGateway.mtime_ns": str(entry.mtime_ns)}
                with _open_regular_source(
                    images_dir.joinpath(*relative.parts),
                    entry,
                ) as source_stream:
                    archive.addfile(member, source_stream)
        archive_stream.flush()
        os.fsync(descriptor)
        _verify_written_archive(archive_stream, inventory)
        metadata = os.fstat(descriptor)
        current = temporary.lstat()
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or not _same_regular_snapshot(metadata, current)
        ):
            raise ImageStorageCliError("backup-write-failed")
        os.fchmod(descriptor, PRIVATE_FILE_MODE)
        os.fsync(descriptor)
        os.replace(temporary, archive_path)
        _fsync_directory(archive_path.parent)
    except ImageStorageCliError:
        raise
    except (OSError, tarfile.TarError) as error:
        raise ImageStorageCliError("backup-write-failed") from error
    finally:
        if archive_stream is not None:
            try:
                archive_stream.close()
            except OSError:
                pass
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass


def backup_images(
    outputs_dir: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> ImageInventory:
    outputs_dir = _require_safe_cli_path(outputs_dir)
    archive_path = _require_safe_cli_path(archive_path)
    manifest_path = _require_safe_cli_path(manifest_path)
    _require_distinct_artifacts(archive_path, manifest_path)
    _require_disjoint_paths(outputs_dir, archive_path)
    _require_disjoint_paths(outputs_dir, manifest_path)
    _reject_symlink_components(outputs_dir)
    _reject_symlink_components(archive_path.parent)
    _reject_symlink_components(manifest_path.parent)
    _require_new_backup_artifacts((archive_path, manifest_path))
    try:
        (outputs_dir / INCOMPLETE_MARKER).lstat()
    except FileNotFoundError:
        pass
    except OSError as error:
        raise ImageStorageCliError("storage-incomplete") from error
    else:
        raise ImageStorageCliError("storage-incomplete")
    images_dir = outputs_dir / "images"
    inventory = build_inventory(images_dir)
    transaction_id = uuid.uuid4().hex
    staged_archive = archive_path.with_name(
        f".{archive_path.name}.staged-{transaction_id}"
    )
    staged_manifest = manifest_path.with_name(
        f".{manifest_path.name}.staged-{transaction_id}"
    )
    try:
        _write_archive(staged_archive, images_dir, inventory)
        write_manifest(staged_manifest, inventory)
        _publish_new_backup_pair(
            (
                (staged_archive, archive_path),
                (staged_manifest, manifest_path),
            )
        )
    finally:
        for staged in (staged_archive, staged_manifest):
            try:
                staged.unlink(missing_ok=True)
            except OSError:
                pass
    return inventory


def restore_images(
    outputs_dir: str | os.PathLike[str],
    archive_path: str | os.PathLike[str],
    manifest_path: str | os.PathLike[str],
) -> ImageInventory:
    if os.geteuid() != 0:
        raise ImageStorageCliError("root-required")
    outputs_dir = _require_safe_cli_path(outputs_dir)
    archive_path = _require_safe_cli_path(archive_path)
    manifest_path = _require_safe_cli_path(manifest_path)
    _require_distinct_artifacts(archive_path, manifest_path)
    _require_disjoint_paths(outputs_dir, archive_path)
    _require_disjoint_paths(outputs_dir, manifest_path)
    _reject_symlink_components(outputs_dir)
    _reject_symlink_components(archive_path)
    _reject_symlink_components(manifest_path)
    expected = read_manifest(manifest_path)
    if _existing_incomplete_marker(outputs_dir) is not None:
        try:
            with tarfile.open(archive_path, mode="r:*") as recovery_archive:
                _verify_archive_inventory(recovery_archive, expected)
        except ImageStorageCliError:
            raise
        except (OSError, tarfile.TarError) as error:
            raise ImageStorageCliError("archive-read-failed") from error
    recovered = _recover_completed_publication(outputs_dir, expected)
    if recovered is not None:
        return recovered
    empty_images = _require_empty_images_target(outputs_dir)
    marker = _create_incomplete_marker(outputs_dir)
    staging = _prepare_staging(outputs_dir)
    try:
        try:
            archive = tarfile.open(archive_path, mode="r:*")
        except (OSError, tarfile.TarError) as error:
            raise ImageStorageCliError("archive-read-failed") from error
        with archive:
            members = _validated_archive_members(archive, expected)
            for entry in expected.files:
                relative = _safe_relative_path(entry.path)
                destination = staging.joinpath(*relative.parts)
                _prepare_parent_directories(staging, relative)
                source = archive.extractfile(members[entry.path])
                if source is None:
                    raise ImageStorageCliError("archive-member-unreadable")
                with source:
                    _copy_stream(
                        source,
                        destination,
                        expected=entry,
                    )
        return _verify_and_publish(
            outputs_dir=outputs_dir,
            empty_images_dir=empty_images,
            staging=staging,
            expected=expected,
            marker=marker,
        )
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _print_summary(inventory: ImageInventory) -> None:
    print(
        json.dumps(
            {"count": inventory.count, "tree_sha256": inventory.tree_sha256},
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        )
    )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage the generated-image volume")
    commands = parser.add_subparsers(dest="command", required=True)

    init = commands.add_parser("init-volume")
    init.add_argument("--outputs-dir", required=True)

    inventory = commands.add_parser("inventory")
    inventory.add_argument("--images-dir", required=True)
    inventory.add_argument("--manifest", required=True)

    migrate = commands.add_parser("migrate")
    migrate.add_argument("--source-images", required=True)
    migrate.add_argument("--outputs-dir", required=True)
    migrate.add_argument("--manifest", required=True)

    backup = commands.add_parser("backup")
    backup.add_argument("--outputs-dir", required=True)
    backup.add_argument("--archive", required=True)
    backup.add_argument("--manifest", required=True)

    restore = commands.add_parser("restore")
    restore.add_argument("--outputs-dir", required=True)
    restore.add_argument("--archive", required=True)
    restore.add_argument("--manifest", required=True)
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    arguments = _parser().parse_args(argv)
    try:
        if arguments.command == "init-volume":
            initialize_volume(arguments.outputs_dir)
            print('{"status":"initialized"}')
            return 0
        elif arguments.command == "inventory":
            images_dir = _require_safe_cli_path(arguments.images_dir)
            manifest_path = _require_safe_cli_path(arguments.manifest)
            _require_disjoint_paths(images_dir, manifest_path)
            _reject_symlink_components(images_dir)
            _reject_symlink_components(manifest_path, allow_missing_leaf=True)
            result = build_inventory(images_dir)
            write_manifest(manifest_path, result)
        elif arguments.command == "migrate":
            result = migrate_images(
                arguments.source_images,
                arguments.outputs_dir,
                arguments.manifest,
            )
        elif arguments.command == "backup":
            result = backup_images(
                arguments.outputs_dir,
                arguments.archive,
                arguments.manifest,
            )
        elif arguments.command == "restore":
            result = restore_images(
                arguments.outputs_dir,
                arguments.archive,
                arguments.manifest,
            )
        else:  # pragma: no cover - argparse guarantees the command.
            raise ImageStorageCliError("command-invalid")
    except ImageStorageCliError as error:
        print(f"image-storage-cli: reason={error.reason}", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
