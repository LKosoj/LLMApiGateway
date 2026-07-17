"""Archive validation and no-clobber publication primitives."""

from __future__ import annotations

import os
import stat
import tarfile
from pathlib import Path, PurePosixPath
from typing import Sequence

from llm_gateway_core.services._image_storage_cli_inventory import (
    ARCHIVE_PREFIX,
    ImageInventory,
    ImageStorageCliError,
    _open_directory_path,
    _safe_relative_path,
    _same_identity,
)


def _require_new_backup_artifacts(artifacts: Sequence[Path]) -> None:
    for artifact in artifacts:
        with _open_directory_path(
            artifact.parent,
            error_reason="backup-artifact-unavailable",
        ) as parent_descriptor:
            try:
                os.stat(
                    artifact.name,
                    dir_fd=parent_descriptor,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError as error:
                raise ImageStorageCliError("backup-artifact-unavailable") from error
            raise ImageStorageCliError("backup-artifact-exists")


def _link_staged_artifact(staged: Path, destination: Path) -> os.stat_result:
    with _open_directory_path(
        destination.parent,
        error_reason="backup-publish-failed",
    ) as parent_descriptor:
        try:
            metadata = os.stat(
                staged.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ImageStorageCliError("backup-write-failed")
            os.link(
                staged.name,
                destination.name,
                src_dir_fd=parent_descriptor,
                dst_dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileExistsError as error:
            raise ImageStorageCliError("backup-artifact-exists") from error
        except ImageStorageCliError:
            raise
        except OSError as error:
            raise ImageStorageCliError("backup-publish-failed") from error
    return metadata


def _unlink_owned_artifact(path: Path, expected: os.stat_result) -> None:
    with _open_directory_path(
        path.parent,
        error_reason="backup-cleanup-failed",
    ) as parent_descriptor:
        try:
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        except OSError as error:
            raise ImageStorageCliError("backup-cleanup-failed") from error
        if not _same_identity(expected, current):
            raise ImageStorageCliError("backup-cleanup-failed")
        try:
            os.unlink(path.name, dir_fd=parent_descriptor)
            os.fsync(parent_descriptor)
        except OSError as error:
            raise ImageStorageCliError("backup-cleanup-failed") from error


def _publish_new_backup_pair(artifacts: Sequence[tuple[Path, Path]]) -> None:
    published: list[tuple[Path, os.stat_result]] = []
    staged_metadata: list[tuple[Path, os.stat_result]] = []
    try:
        for staged, destination in artifacts:
            metadata = _link_staged_artifact(staged, destination)
            published.append((destination, metadata))
            staged_metadata.append((staged, metadata))
        for staged, metadata in staged_metadata:
            _unlink_owned_artifact(staged, metadata)
    except BaseException as error:
        cleanup_error: BaseException | None = None
        for destination, metadata in reversed(published):
            try:
                _unlink_owned_artifact(destination, metadata)
            except BaseException as artifact_cleanup_error:
                if cleanup_error is None:
                    cleanup_error = artifact_cleanup_error
        if cleanup_error is not None:
            raise ImageStorageCliError("backup-cleanup-failed") from error
        if isinstance(error, ImageStorageCliError):
            raise
        raise ImageStorageCliError("backup-publish-failed") from error


def _validated_archive_members(
    archive: tarfile.TarFile,
    expected: ImageInventory,
) -> dict[str, tarfile.TarInfo]:
    expected_entries = {entry.path: entry for entry in expected.files}
    expected_paths = set(expected_entries)
    members: dict[str, tarfile.TarInfo] = {}
    try:
        archive_members = archive.getmembers()
    except tarfile.TarError as error:
        raise ImageStorageCliError("archive-read-failed") from error
    for member in archive_members:
        if not member.isfile():
            raise ImageStorageCliError("archive-member-unsupported")
        path = PurePosixPath(member.name)
        if (
            path.is_absolute()
            or path.as_posix() != member.name
            or len(path.parts) < 2
            or path.parts[0] != ARCHIVE_PREFIX
        ):
            raise ImageStorageCliError("archive-path-invalid")
        relative = PurePosixPath(*path.parts[1:]).as_posix()
        _safe_relative_path(relative)
        if relative in members:
            raise ImageStorageCliError("archive-member-duplicate")
        if relative not in expected_paths:
            raise ImageStorageCliError("archive-manifest-mismatch")
        expected_entry = expected_entries[relative]
        if member.size != expected_entry.size:
            raise ImageStorageCliError("archive-size-mismatch")
        members[relative] = member
    if set(members) != expected_paths:
        raise ImageStorageCliError("archive-manifest-mismatch")
    return members
