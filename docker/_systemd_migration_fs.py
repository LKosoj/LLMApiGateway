"""Race-aware, bounded-memory filesystem primitives for systemd migration."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import uuid
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

from docker._systemd_migration_model import (
    RUNTIME_DIRECTORY_MODE,
    RUNTIME_FILE_MODE,
    SQLITE_SIDECAR_SUFFIXES,
    Artifact,
    FileSnapshot,
    MigrationError,
    Ownership,
)


_READ_CHUNK_SIZE = 1024 * 1024
_TEMP_NAME = re.compile(r"(?:^\.?.*\.tmp(?:-.*)?$|^\.tmp-.*$|.*\.temp$)")
_RENAME_NOREPLACE = 1


def safe_absolute_directory(
    value: str | os.PathLike[str],
    *,
    label: str,
    allow_missing_leaf: bool = False,
) -> Path:
    try:
        raw = os.fspath(value)
        path = Path(raw)
    except (TypeError, ValueError):
        raise MigrationError("path-invalid", (label,)) from None
    if not raw or "\x00" in raw or not path.is_absolute():
        raise MigrationError("path-not-absolute", (label,))
    if path == Path(path.anchor) or ".." in path.parts or raw.endswith(os.sep) or raw.startswith("//"):
        raise MigrationError("path-unsafe", (label,))

    current = Path(path.anchor)
    parts = path.parts[1:]
    for index, component in enumerate(parts):
        current /= component
        try:
            metadata = current.lstat()
        except FileNotFoundError:
            if allow_missing_leaf and index == len(parts) - 1:
                return path
            raise MigrationError("path-missing", (label,)) from None
        except OSError:
            raise MigrationError("path-unavailable", (label,)) from None
        if stat.S_ISLNK(metadata.st_mode):
            raise MigrationError("path-symlink", (label,))
        if index == len(parts) - 1 and not stat.S_ISDIR(metadata.st_mode):
            raise MigrationError("path-not-directory", (label,))
    return path


def require_disjoint(paths: Sequence[tuple[str, Path]]) -> None:
    canonical = [(label, path.resolve(strict=False)) for label, path in paths]
    conflicts: set[str] = set()
    for index, (first_label, first) in enumerate(canonical):
        for second_label, second in canonical[index + 1 :]:
            if first == second or first in second.parents or second in first.parents:
                conflicts.update((first_label, second_label))
    if conflicts:
        raise MigrationError("path-overlap", tuple(conflicts))


def _same_regular(first: os.stat_result, second: os.stat_result) -> bool:
    return (
        stat.S_ISREG(first.st_mode)
        and stat.S_ISREG(second.st_mode)
        and first.st_nlink == second.st_nlink == 1
        and first.st_dev == second.st_dev
        and first.st_ino == second.st_ino
        and first.st_size == second.st_size
        and first.st_mtime_ns == second.st_mtime_ns
    )


def _snapshot(path: Path, metadata: os.stat_result, digest: str | None) -> FileSnapshot:
    return FileSnapshot(
        path=path,
        device=metadata.st_dev,
        inode=metadata.st_ino,
        size=metadata.st_size,
        mtime_ns=metadata.st_mtime_ns,
        sha256=digest,
    )


def shallow_regular(
    path: Path,
    *,
    name: str,
    target: bool = False,
) -> FileSnapshot | None:
    try:
        metadata = path.lstat()
    except FileNotFoundError:
        return None
    except OSError:
        reason = "unsafe-existing-target" if target else "unsafe-source"
        raise MigrationError(reason, (name,)) from None
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        reason = "unsafe-existing-target" if target else "unsafe-source"
        raise MigrationError(reason, (name,))
    return _snapshot(path, metadata, None)


def _hash_descriptor(descriptor: int, expected_size: int, name: str) -> str:
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(descriptor, _READ_CHUNK_SIZE):
        total += len(chunk)
        if total > expected_size:
            raise MigrationError("source-changed", (name,))
        digest.update(chunk)
    if total != expected_size:
        raise MigrationError("source-changed", (name,))
    return digest.hexdigest()


@contextmanager
def open_snapshot(snapshot: FileSnapshot, *, name: str) -> Iterator[int]:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                snapshot.path,
                os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
            )
            opened = os.fstat(descriptor)
        except OSError:
            raise MigrationError("source-changed", (name,)) from None
        if (
            opened.st_dev != snapshot.device
            or opened.st_ino != snapshot.inode
            or opened.st_size != snapshot.size
            or opened.st_mtime_ns != snapshot.mtime_ns
            or opened.st_nlink != 1
            or not stat.S_ISREG(opened.st_mode)
        ):
            raise MigrationError("source-changed", (name,))
        yield descriptor
        try:
            after = os.fstat(descriptor)
            current = snapshot.path.lstat()
        except OSError:
            raise MigrationError("source-changed", (name,)) from None
        if not _same_regular(opened, after) or not _same_regular(opened, current):
            raise MigrationError("source-changed", (name,))
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def regular_snapshot(
    path: Path,
    *,
    name: str,
    target: bool = False,
    hash_content: bool = True,
) -> FileSnapshot | None:
    initial = shallow_regular(path, name=name, target=target)
    if initial is None:
        return None
    try:
        with open_snapshot(initial, name=name) as descriptor:
            digest = _hash_descriptor(descriptor, initial.size, name) if hash_content else None
    except MigrationError as error:
        if target and error.reason == "source-changed":
            raise MigrationError("unsafe-existing-target", (name,)) from None
        raise
    return FileSnapshot(
        path=initial.path,
        device=initial.device,
        inode=initial.inode,
        size=initial.size,
        mtime_ns=initial.mtime_ns,
        sha256=digest,
    )


def read_snapshot_bytes(snapshot: FileSnapshot, *, name: str, maximum_size: int) -> bytes:
    if snapshot.size > maximum_size:
        raise MigrationError("file-too-large", (name,))
    payload = bytearray()
    with open_snapshot(snapshot, name=name) as descriptor:
        remaining = snapshot.size
        while remaining:
            chunk = os.read(descriptor, min(remaining, 64 * 1024))
            if not chunk:
                raise MigrationError("source-changed", (name,))
            payload.extend(chunk)
            remaining -= len(chunk)
        if os.read(descriptor, 1):
            raise MigrationError("source-changed", (name,))
    return bytes(payload)


def validate_directory_metadata(
    path: Path,
    *,
    uid: int,
    gid: int,
    name: str,
    mode: int,
) -> None:
    try:
        metadata = path.lstat()
    except OSError:
        raise MigrationError("unsafe-existing-target", (name,)) from None
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise MigrationError("unsafe-existing-target", (name,))


def directory_metadata_needs_normalization(
    path: Path,
    *,
    uid: int,
    gid: int,
    name: str,
    mode: int,
) -> bool:
    try:
        metadata = path.lstat()
    except OSError:
        raise MigrationError("unsafe-existing-target", (name,)) from None
    current_mode = stat.S_IMODE(metadata.st_mode)
    if (
        not stat.S_ISDIR(metadata.st_mode)
        or stat.S_ISLNK(metadata.st_mode)
        or metadata.st_uid != uid
        or metadata.st_gid != gid
        or current_mode & 0o022
    ):
        raise MigrationError("unsafe-existing-target", (name,))
    return current_mode != mode


def normalize_directory_metadata(
    path: Path,
    *,
    uid: int,
    gid: int,
    name: str,
    mode: int,
) -> None:
    if not directory_metadata_needs_normalization(
        path, uid=uid, gid=gid, name=name, mode=mode
    ):
        return
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_RDONLY
            | os.O_DIRECTORY
            | getattr(os, "O_NOFOLLOW", 0)
            | getattr(os, "O_CLOEXEC", 0),
        )
        opened = os.fstat(descriptor)
        current = path.lstat()
        if (
            opened.st_dev != current.st_dev
            or opened.st_ino != current.st_ino
            or opened.st_uid != uid
            or opened.st_gid != gid
            or stat.S_IMODE(opened.st_mode) & 0o022
        ):
            raise MigrationError("unsafe-existing-target", (name,))
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
        fsync_directory(path.parent)
        normalized = os.fstat(descriptor)
        if stat.S_IMODE(normalized.st_mode) != mode:
            raise MigrationError("target-metadata-failed", (name,))
    except MigrationError:
        raise
    except OSError:
        raise MigrationError("target-metadata-failed", (name,)) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def validate_target_file(
    path: Path,
    *,
    name: str,
    uid: int,
    gid: int,
    mode: int,
    hash_content: bool = True,
    shallow: bool = False,
) -> FileSnapshot | None:
    result = (
        shallow_regular(path, name=name, target=True)
        if shallow
        else regular_snapshot(path, name=name, target=True, hash_content=hash_content)
    )
    if result is None:
        return None
    metadata = path.lstat()
    if metadata.st_uid != uid or metadata.st_gid != gid or stat.S_IMODE(metadata.st_mode) != mode:
        raise MigrationError("unsafe-existing-target", (name,))
    return result


def fsync_directory(path: Path) -> None:
    descriptor = os.open(
        path,
        os.O_RDONLY | os.O_DIRECTORY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0),
    )
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def unlink_and_sync(path: Path, *, missing_ok: bool = False) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        if missing_ok:
            return
        raise
    fsync_directory(path.parent)


def ensure_directory(path: Path, *, uid: int, gid: int, mode: int) -> None:
    try:
        path.mkdir(mode=mode)
    except FileExistsError:
        validate_directory_metadata(path, uid=uid, gid=gid, name=path.name, mode=mode)
        return
    except OSError:
        raise MigrationError("target-create-failed", (path.name,)) from None
    try:
        os.chown(path, uid, gid)
        path.chmod(mode)
        fsync_directory(path)
        fsync_directory(path.parent)
    except OSError:
        try:
            path.rmdir()
            fsync_directory(path.parent)
        except OSError:
            pass
        raise MigrationError("target-metadata-failed", (path.name,)) from None


def ensure_runtime_directory(path: Path, ownership: Ownership) -> None:
    ensure_directory(
        path,
        uid=ownership.service_uid,
        gid=ownership.service_gid,
        mode=RUNTIME_DIRECTORY_MODE,
    )


def _copy_descriptor(
    source_descriptor: int,
    destination_descriptor: int,
    *,
    expected_size: int,
    expected_sha256: str | None,
    name: str,
) -> None:
    os.lseek(source_descriptor, 0, os.SEEK_SET)
    digest = hashlib.sha256()
    total = 0
    while chunk := os.read(source_descriptor, _READ_CHUNK_SIZE):
        total += len(chunk)
        if total > expected_size:
            raise MigrationError("source-changed", (name,))
        digest.update(chunk)
        view = memoryview(chunk)
        while view:
            written = os.write(destination_descriptor, view)
            if written <= 0:
                raise MigrationError("target-write-failed", (name,))
            view = view[written:]
    if total != expected_size or (expected_sha256 is not None and digest.hexdigest() != expected_sha256):
        raise MigrationError("source-changed", (name,))


def copy_snapshot_to_new(
    snapshot: FileSnapshot,
    destination: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
    preserve_mtime: bool = False,
) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        with open_snapshot(snapshot, name=destination.name) as source_descriptor:
            _copy_descriptor(
                source_descriptor,
                descriptor,
                expected_size=snapshot.size,
                expected_sha256=snapshot.sha256,
                name=destination.name,
            )
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        if preserve_mtime:
            os.utime(descriptor, ns=(snapshot.mtime_ns, snapshot.mtime_ns))
        os.fsync(descriptor)
    except MigrationError:
        raise
    except OSError:
        raise MigrationError("target-write-failed", (destination.name,)) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def write_bytes_new(path: Path, payload: bytes, *, uid: int, gid: int, mode: int) -> None:
    descriptor = -1
    try:
        descriptor = os.open(
            path,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            mode,
        )
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written <= 0:
                raise OSError("write-stalled")
            view = view[written:]
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        raise MigrationError("target-write-failed", (path.name,)) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def atomic_copy(
    artifact: Artifact,
    destination: Path,
    *,
    uid: int,
    gid: int,
    mode: int,
) -> FileSnapshot:
    if artifact.source is None:
        raise MigrationError("source-missing", (artifact.name,))
    temporary = destination.with_name(f".{destination.name}.migration-{uuid.uuid4().hex}")
    try:
        copy_snapshot_to_new(artifact.source, temporary, uid=uid, gid=gid, mode=mode)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            existing = validate_target_file(destination, name=artifact.name, uid=uid, gid=gid, mode=mode)
            if existing is None:
                raise MigrationError("unsafe-existing-target", (artifact.name,))
            return existing
        fsync_directory(destination.parent)
    finally:
        try:
            unlink_and_sync(temporary, missing_ok=True)
        except OSError:
            raise MigrationError("temporary-cleanup-failed", (artifact.name,)) from None
    result = validate_target_file(destination, name=artifact.name, uid=uid, gid=gid, mode=mode)
    if result is None or result.sha256 != artifact.source.sha256:
        raise MigrationError("target-verify-failed", (artifact.name,))
    return result


def inventory_tree(
    root: Path,
    *,
    target: bool,
    ownership: Ownership,
    exclude_name: str | None = None,
) -> dict[str, object]:
    entries: list[dict[str, int | str]] = []

    def visit(directory: Path, relative: PurePosixPath | None = None) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda item: item.name)
        except OSError:
            reason = "unsafe-existing-target" if target else "unsafe-source"
            raise MigrationError(reason, (root.name,)) from None
        for child in children:
            if relative is None and child.name == exclude_name:
                continue
            child_relative = PurePosixPath(child.name) if relative is None else relative / child.name
            metadata = _tree_entry_metadata(child, target=target)
            child_path = Path(child.path)
            if stat.S_ISDIR(metadata.st_mode):
                if target:
                    _validate_runtime_entry(metadata, child.name, ownership, RUNTIME_DIRECTORY_MODE)
                visit(child_path, child_relative)
                continue
            normalized_mode = RUNTIME_DIRECTORY_MODE if stat.S_IMODE(metadata.st_mode) & 0o111 else RUNTIME_FILE_MODE
            if target:
                _validate_runtime_entry(metadata, child.name, ownership, normalized_mode)
            snapshot = regular_snapshot(child_path, name=child.name, target=target)
            if snapshot is None:
                raise MigrationError("source-changed", (child.name,))
            entries.append(
                {
                    "path": child_relative.as_posix(),
                    "size": snapshot.size,
                    "sha256": snapshot.sha256 or "",
                    "mtime_ns": snapshot.mtime_ns,
                    "mode": normalized_mode,
                }
            )

    visit(root)
    encoded = json.dumps(entries, sort_keys=True, separators=(",", ":")).encode()
    return {
        "version": 1,
        "count": len(entries),
        "tree_sha256": hashlib.sha256(encoded).hexdigest(),
        "files": entries,
    }


def _tree_entry_metadata(entry: os.DirEntry[str], *, target: bool) -> os.stat_result:
    try:
        metadata = entry.stat(follow_symlinks=False)
    except OSError:
        reason = "unsafe-existing-target" if target else "unsafe-source"
        raise MigrationError(reason, (entry.name,)) from None
    if stat.S_ISDIR(metadata.st_mode):
        return metadata
    if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
        reason = "unsafe-existing-target" if target else "unsafe-source"
        raise MigrationError(reason, (entry.name,))
    return metadata


def _validate_runtime_entry(metadata: os.stat_result, name: str, ownership: Ownership, mode: int) -> None:
    if (
        metadata.st_uid != ownership.service_uid
        or metadata.st_gid != ownership.service_gid
        or stat.S_IMODE(metadata.st_mode) != mode
    ):
        raise MigrationError("unsafe-existing-target", (name,))


def sync_directories_bottom_up(root: Path) -> None:
    directories: list[Path] = []

    def visit(directory: Path) -> None:
        try:
            with os.scandir(directory) as iterator:
                children = sorted(iterator, key=lambda entry: entry.name)
        except OSError:
            raise MigrationError("target-sync-failed", (directory.name,)) from None
        for child in children:
            try:
                metadata = child.stat(follow_symlinks=False)
            except OSError:
                raise MigrationError("target-sync-failed", (child.name,)) from None
            if stat.S_ISDIR(metadata.st_mode):
                visit(Path(child.path))
            elif not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                raise MigrationError("unsafe-existing-target", (child.name,))
        directories.append(directory)

    visit(root)
    try:
        for directory in directories:
            fsync_directory(directory)
    except OSError:
        raise MigrationError("target-sync-failed", (root.name,)) from None


@contextmanager
def open_directory(path: Path) -> Iterator[int]:
    descriptor = -1
    try:
        try:
            descriptor = os.open(
                path,
                os.O_RDONLY
                | os.O_DIRECTORY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_CLOEXEC", 0),
            )
        except OSError:
            raise MigrationError("path-unavailable", (path.name,)) from None
        yield descriptor
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def rename_noreplace(parent_descriptor: int, source_name: str, target_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise MigrationError("cache-publish-unsupported")
    renameat2.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        parent_descriptor,
        os.fsencode(source_name),
        parent_descriptor,
        os.fsencode(target_name),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return
    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(error_number, os.strerror(error_number), target_name)
    if error_number in {errno.ENOSYS, errno.EINVAL, errno.ENOTSUP}:
        raise MigrationError("cache-publish-unsupported")
    raise MigrationError("cache-publish-failed", (target_name,))


def remove_tree_and_sync(path: Path) -> None:
    try:
        shutil.rmtree(path)
        fsync_directory(path.parent)
    except FileNotFoundError:
        return
    except OSError:
        raise MigrationError("temporary-cleanup-failed", (path.name,)) from None


def _cleanup_sqlite_temporary(temporary: Path, staging: Path, name: str) -> None:
    cleanup_failed = False
    temporary_paths = (
        temporary,
        *(temporary.with_name(f"{temporary.name}{suffix}") for suffix in SQLITE_SIDECAR_SUFFIXES),
    )
    for path in temporary_paths:
        try:
            unlink_and_sync(path, missing_ok=True)
        except (MigrationError, OSError):
            cleanup_failed = True
    try:
        remove_tree_and_sync(staging)
    except (MigrationError, OSError):
        cleanup_failed = True
    if cleanup_failed:
        raise MigrationError("temporary-cleanup-failed", (name,))


def sqlite_backup(
    artifact: Artifact,
    sidecars: Sequence[FileSnapshot],
    destination: Path,
    ownership: Ownership,
) -> None:
    if artifact.source is None:
        return
    staging = destination.parent / f".{artifact.name}.snapshot-{uuid.uuid4().hex}"
    temporary = destination.parent / f".{artifact.name}.migration-{uuid.uuid4().hex}"
    ensure_directory(staging, uid=os.geteuid(), gid=os.getegid(), mode=0o700)
    try:
        _stage_database(artifact.source, sidecars, staging)
        _backup_staged_database(staging / artifact.name, temporary, artifact.name)
        _set_file_metadata(temporary, ownership.service_uid, ownership.service_gid, RUNTIME_FILE_MODE)
        _verify_snapshots_unchanged((artifact.source, *sidecars), artifact.name)
        try:
            os.link(temporary, destination, follow_symlinks=False)
        except FileExistsError:
            existing = validate_target_file(
                destination,
                name=artifact.name,
                uid=ownership.service_uid,
                gid=ownership.service_gid,
                mode=RUNTIME_FILE_MODE,
                hash_content=False,
            )
            if existing is None:
                raise MigrationError("unsafe-existing-target", (artifact.name,))
        fsync_directory(destination.parent)
    except BaseException:
        try:
            _cleanup_sqlite_temporary(temporary, staging, artifact.name)
        except MigrationError:
            pass
        raise
    _cleanup_sqlite_temporary(temporary, staging, artifact.name)


def _stage_database(main: FileSnapshot, sidecars: Sequence[FileSnapshot], staging: Path) -> None:
    snapshots = (main, *(item for item in sidecars if not item.path.name.endswith("-shm")))
    for snapshot in snapshots:
        copy_snapshot_to_new(
            snapshot,
            staging / snapshot.path.name,
            uid=os.geteuid(),
            gid=os.getegid(),
            mode=0o600,
            preserve_mtime=True,
        )
        fsync_directory(staging)
    sync_directories_bottom_up(staging)


def _backup_staged_database(source: Path, destination: Path, name: str) -> None:
    source_connection: sqlite3.Connection | None = None
    target_connection: sqlite3.Connection | None = None
    try:
        source_connection = sqlite3.connect(source)
        target_connection = sqlite3.connect(destination)
        source_connection.backup(target_connection)
        if target_connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
            raise MigrationError("sqlite-backup-invalid", (name,))
    except MigrationError:
        raise
    except sqlite3.Error:
        raise MigrationError("sqlite-backup-failed", (name,)) from None
    finally:
        if target_connection is not None:
            target_connection.close()
        if source_connection is not None:
            source_connection.close()


def _set_file_metadata(path: Path, uid: int, gid: int, mode: int) -> None:
    descriptor = -1
    try:
        descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0))
        os.fchown(descriptor, uid, gid)
        os.fchmod(descriptor, mode)
        os.fsync(descriptor)
    except OSError:
        raise MigrationError("target-metadata-failed", (path.name,)) from None
    finally:
        if descriptor >= 0:
            os.close(descriptor)


def _verify_snapshots_unchanged(snapshots: Sequence[FileSnapshot], name: str) -> None:
    for snapshot in snapshots:
        with open_snapshot(snapshot, name=name):
            pass


def is_temporary(name: str) -> bool:
    return _TEMP_NAME.fullmatch(name) is not None
