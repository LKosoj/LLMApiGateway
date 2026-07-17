"""Target and copy primitives for generated-image storage administration."""

from __future__ import annotations

import os
import stat
import uuid
from contextlib import contextmanager
from pathlib import Path, PurePosixPath
from typing import BinaryIO, Iterator

from llm_gateway_core.services._image_storage_cli_inventory import (
    DIRECTORY_MODE,
    FILE_MODE,
    RUNTIME_GID,
    RUNTIME_UID,
    ImageStorageCliError,
    InventoryEntry,
    _fsync_directory,
    _open_directory_path,
    _reject_symlink_components,
    _require_absolute,
    _require_safe_cli_path,
    _same_regular_snapshot,
)


def _require_empty_images_target(outputs_dir: Path) -> Path:
    images_dir = outputs_dir / "images"
    try:
        metadata = images_dir.lstat()
    except FileNotFoundError:
        try:
            images_dir.mkdir(mode=DIRECTORY_MODE)
            metadata = images_dir.lstat()
        except OSError as error:
            raise ImageStorageCliError("target-unavailable") from error
    except OSError as error:
        raise ImageStorageCliError("target-unavailable") from error
    if stat.S_ISLNK(metadata.st_mode) or not stat.S_ISDIR(metadata.st_mode):
        raise ImageStorageCliError("target-invalid")
    try:
        with os.scandir(images_dir) as iterator:
            if next(iterator, None) is not None:
                raise ImageStorageCliError("target-not-empty")
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("target-unavailable") from error
    return images_dir


def _sync_staging_directories(root: Path) -> None:
    directories: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError as error:
            raise ImageStorageCliError("volume-scan-failed") from error
        for child in children:
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError as error:
                raise ImageStorageCliError("volume-scan-failed") from error
            child_path = directory / child.name
            if stat.S_ISDIR(metadata.st_mode):
                visit(child_path)
            elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise ImageStorageCliError("volume-entry-unsupported")
        directories.append(directory)

    visit(root)
    for directory in directories:
        _sync_runtime_directory(directory)


def _sync_runtime_directory(directory: Path) -> None:
    try:
        with _open_directory_path(
            directory,
            error_reason="volume-metadata-failed",
        ) as descriptor:
            metadata = os.fstat(descriptor)
            if not stat.S_ISDIR(metadata.st_mode):
                raise ImageStorageCliError("volume-entry-unsupported")
            os.fchown(descriptor, RUNTIME_UID, RUNTIME_GID)
            os.fchmod(descriptor, DIRECTORY_MODE)
            os.fsync(descriptor)
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("volume-metadata-failed") from error


def initialize_volume(outputs_dir: str | os.PathLike[str]) -> None:
    if os.geteuid() != 0:
        raise ImageStorageCliError("root-required")
    outputs_dir = _require_safe_cli_path(outputs_dir)
    _reject_symlink_components(outputs_dir, allow_missing_leaf=True)
    try:
        outputs_dir.mkdir(parents=False, mode=DIRECTORY_MODE, exist_ok=True)
        images_dir = outputs_dir / "images"
        images_dir.mkdir(mode=DIRECTORY_MODE, exist_ok=True)
    except OSError as error:
        raise ImageStorageCliError("volume-create-failed") from error
    _reject_symlink_components(images_dir)
    _sync_runtime_directory(images_dir)
    _sync_runtime_directory(outputs_dir)
    _fsync_directory(outputs_dir.parent)


@contextmanager
def _open_regular_source(
    path: Path,
    expected: InventoryEntry,
) -> Iterator[BinaryIO]:
    path = _require_absolute(path)
    _reject_symlink_components(path)
    descriptor = -1
    stream: BinaryIO | None = None
    try:
        with _open_directory_path(
            path.parent,
            error_reason="source-open-failed",
        ) as parent_descriptor:
            descriptor = os.open(
                path.name,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not stat.S_ISREG(opened.st_mode)
                or opened.st_nlink != 1
                or opened.st_size != expected.size
                or opened.st_mtime_ns != expected.mtime_ns
                or not _same_regular_snapshot(opened, current)
            ):
                raise ImageStorageCliError("source-changed")
            stream = os.fdopen(descriptor, "rb", closefd=False)
            yield stream
            after_read = os.fstat(descriptor)
            current = os.stat(
                path.name,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if (
                not _same_regular_snapshot(opened, after_read)
                or not _same_regular_snapshot(opened, current)
            ):
                raise ImageStorageCliError("source-changed")
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("source-open-failed") from error
    finally:
        if stream is not None:
            stream.close()
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _copy_stream(
    source: BinaryIO,
    destination: Path,
    *,
    expected: InventoryEntry,
) -> None:
    descriptor = -1
    total = 0
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            FILE_MODE,
        )
        while chunk := source.read(1024 * 1024):
            if total + len(chunk) > expected.size:
                raise ImageStorageCliError("restore-size-mismatch")
            view = memoryview(chunk)
            while view:
                written = os.write(descriptor, view)
                if written <= 0:
                    raise ImageStorageCliError("restore-write-stalled")
                view = view[written:]
            total += len(chunk)
        if total != expected.size:
            raise ImageStorageCliError("restore-size-mismatch")
        os.fchown(descriptor, RUNTIME_UID, RUNTIME_GID)
        os.fchmod(descriptor, FILE_MODE)
        os.utime(
            descriptor,
            ns=(expected.mtime_ns, expected.mtime_ns),
        )
        metadata = os.fstat(descriptor)
        if (
            not stat.S_ISREG(metadata.st_mode)
            or metadata.st_nlink != 1
            or metadata.st_size != expected.size
            or metadata.st_mtime_ns != expected.mtime_ns
            or metadata.st_uid != RUNTIME_UID
            or metadata.st_gid != RUNTIME_GID
            or stat.S_IMODE(metadata.st_mode) != FILE_MODE
        ):
            raise ImageStorageCliError("restore-metadata-mismatch")
        os.fsync(descriptor)
    except ImageStorageCliError:
        raise
    except OSError as error:
        raise ImageStorageCliError("restore-write-failed") from error
    finally:
        if descriptor >= 0:
            try:
                os.close(descriptor)
            except OSError:
                pass


def _prepare_staging(outputs_dir: Path) -> Path:
    staging = outputs_dir / f".image-restore-staging-{uuid.uuid4().hex}"
    try:
        staging.mkdir(mode=DIRECTORY_MODE)
    except OSError as error:
        raise ImageStorageCliError("staging-create-failed") from error
    return staging


def _prepare_parent_directories(staging: Path, relative: PurePosixPath) -> None:
    current = staging
    for component in relative.parts[:-1]:
        current /= component
        try:
            current.mkdir(mode=DIRECTORY_MODE)
        except FileExistsError:
            metadata = current.lstat()
            if not stat.S_ISDIR(metadata.st_mode) or stat.S_ISLNK(metadata.st_mode):
                raise ImageStorageCliError("restore-parent-invalid") from None
        except OSError as error:
            raise ImageStorageCliError("restore-parent-failed") from error
