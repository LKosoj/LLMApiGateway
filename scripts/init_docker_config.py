#!/usr/bin/env python3
"""Initialize the host directory mounted at /app/config without overwrites."""

from __future__ import annotations

import argparse
import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn


CONFIG_FILENAMES: Final[tuple[str, ...]] = (
    "providers.json",
    "models_fallback_rules.json",
    "models_operation_rules.json",
    "models_fusion_rules.json",
    "models_model_rules.json",
    "models_router_rules.json",
)
_DIRECTORY_MODE: Final[int] = 0o750
_CONTAINER_UID: Final[int] = 10001
_CONTAINER_GID: Final[int] = 10001
_DIRECTORY_OPEN_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_SOURCE_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_TEMP_OPEN_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
_RENAME_NOREPLACE: Final[int] = 1
_UNSUPPORTED_RENAME_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}
)


class DockerConfigInitializationError(RuntimeError):
    """A stable error that never includes configuration contents or paths."""

    def __init__(self, reason: str, *, publication_uncertain: bool = False) -> None:
        self.reason = reason
        self.publication_uncertain = publication_uncertain
        super().__init__(f"docker config initialization failed: {reason}")


class _OwnedFd:
    """One file descriptor owner with explicit transfer-before-close semantics."""

    __slots__ = ("_fd",)

    def __init__(self, fd: int | None = None) -> None:
        self._fd = fd

    def assign(self, fd: int) -> None:
        if self._fd is not None:
            raise RuntimeError("file descriptor owner is already populated")
        self._fd = fd

    def fileno(self) -> int:
        if self._fd is None:
            raise RuntimeError("file descriptor owner is empty")
        return self._fd

    def take(self) -> int | None:
        fd = self._fd
        self._fd = None
        return fd

    def close_error(self) -> OSError | None:
        fd = self.take()
        if fd is None:
            return None
        try:
            os.close(fd)
        except OSError as exc:
            return exc
        return None


@dataclass(frozen=True, slots=True)
class _FrozenFile:
    device: int
    inode: int
    mode: int
    uid: int
    gid: int
    size: int
    digest: bytes


def _error(
    reason: str,
    *,
    publication_uncertain: bool = False,
) -> DockerConfigInitializationError:
    return DockerConfigInitializationError(
        reason,
        publication_uncertain=publication_uncertain,
    )


def _raise(reason: str) -> NoReturn:
    raise _error(reason)


def _raise_primary(primary: BaseException) -> NoReturn:
    raise primary


def _mark_publication_uncertain(primary: BaseException) -> BaseException:
    if isinstance(primary, DockerConfigInitializationError):
        reason = primary.reason
    elif isinstance(primary, Exception):
        reason = "target-file-internal-error"
    else:
        return primary
    bounded = _error(reason, publication_uncertain=True)
    bounded.__suppress_context__ = True
    return bounded


def _finish_close(
    owner: _OwnedFd,
    primary: BaseException | None,
    reason: str,
) -> BaseException | None:
    close_error = owner.close_error()
    if close_error is not None and primary is None:
        return _error(reason)
    return primary


def _validate_identity(uid: int, gid: int) -> None:
    if (
        not isinstance(uid, int)
        or isinstance(uid, bool)
        or uid < 0
        or not isinstance(gid, int)
        or isinstance(gid, bool)
        or gid < 0
    ):
        _raise("identity-invalid")


def _open_directory(path: Path, *, label: str) -> int:
    try:
        source_stat = os.lstat(path)
    except FileNotFoundError:
        _raise(f"{label}-directory-missing")
    except OSError:
        _raise(f"{label}-directory-stat-failed")
    if stat.S_ISLNK(source_stat.st_mode):
        _raise(f"{label}-directory-symlink")
    if not stat.S_ISDIR(source_stat.st_mode):
        _raise(f"{label}-directory-not-directory")

    owner = _OwnedFd()
    try:
        owner.assign(os.open(path, _DIRECTORY_OPEN_FLAGS))
    except OSError:
        _raise(f"{label}-directory-open-failed")
    primary: BaseException | None = None
    try:
        opened_stat = os.fstat(owner.fileno())
        if not stat.S_ISDIR(opened_stat.st_mode):
            _raise(f"{label}-directory-not-directory")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            source_stat.st_dev,
            source_stat.st_ino,
        ):
            _raise(f"{label}-directory-changed")
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error(f"{label}-directory-stat-failed")
    except BaseException as exc:
        primary = exc
    if primary is not None:
        primary = _finish_close(owner, primary, f"{label}-directory-close-failed")
        assert primary is not None
        _raise_primary(primary)
    fd = owner.take()
    assert fd is not None
    return fd


def _ensure_target_directory(path: Path, uid: int, gid: int) -> int:
    try:
        os.mkdir(path, _DIRECTORY_MODE)
    except FileExistsError:
        pass
    except OSError:
        _raise("target-directory-create-failed")
    owner = _OwnedFd(_open_directory(path, label="target"))
    primary: BaseException | None = None
    try:
        os.fchmod(owner.fileno(), _DIRECTORY_MODE)
        os.fchown(owner.fileno(), uid, gid)
        os.fsync(owner.fileno())
    except OSError:
        primary = _error("target-directory-metadata-failed")
    except BaseException as exc:
        primary = exc
    if primary is not None:
        primary = _finish_close(owner, primary, "target-directory-close-failed")
        assert primary is not None
        _raise_primary(primary)
    fd = owner.take()
    assert fd is not None
    return fd


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _read_source(source_fd: int, filename: str) -> tuple[bytes, int]:
    owner = _OwnedFd()
    try:
        owner.assign(os.open(filename, _SOURCE_OPEN_FLAGS, dir_fd=source_fd))
    except OSError:
        _raise("source-file-open-failed")
    primary: BaseException | None = None
    result: tuple[bytes, int] | None = None
    try:
        before = os.fstat(owner.fileno())
        if not stat.S_ISREG(before.st_mode):
            _raise("source-file-not-regular")
        content = _read_all(owner.fileno())
        after = os.fstat(owner.fileno())
        if (
            before.st_dev,
            before.st_ino,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ):
            _raise("source-file-changed")
        result = content, stat.S_IMODE(before.st_mode)
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error("source-file-read-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(owner, primary, "source-file-close-failed")
    if primary is not None:
        _raise_primary(primary)
    assert result is not None
    return result


def _normalize_existing(target_fd: int, filename: str, uid: int, gid: int) -> bool:
    try:
        destination_stat = os.stat(filename, dir_fd=target_fd, follow_symlinks=False)
    except FileNotFoundError:
        return False
    except OSError:
        _raise("target-file-stat-failed")
    if not stat.S_ISREG(destination_stat.st_mode):
        _raise("target-file-not-regular")

    owner = _OwnedFd()
    try:
        owner.assign(os.open(filename, _SOURCE_OPEN_FLAGS, dir_fd=target_fd))
    except OSError:
        _raise("target-file-open-failed")
    primary: BaseException | None = None
    try:
        opened_stat = os.fstat(owner.fileno())
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            destination_stat.st_dev,
            destination_stat.st_ino,
        ):
            _raise("target-file-changed")
        os.fchown(owner.fileno(), uid, gid)
        os.fsync(owner.fileno())
        current_stat = os.stat(filename, dir_fd=target_fd, follow_symlinks=False)
        if (current_stat.st_dev, current_stat.st_ino) != (
            opened_stat.st_dev,
            opened_stat.st_ino,
        ):
            _raise("target-file-changed")
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error("target-file-metadata-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(owner, primary, "target-file-close-failed")
    if primary is not None:
        _raise_primary(primary)
    return True


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("initialization write made no progress")
        offset += written


def _rename_noreplace(root_fd: int, source_name: str, destination_name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    try:
        renameat2 = libc.renameat2
    except AttributeError:
        raise OSError(errno.ENOSYS, "renameat2 unavailable") from None
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        root_fd,
        os.fsencode(source_name),
        root_fd,
        os.fsencode(destination_name),
        _RENAME_NOREPLACE,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _freeze_file(source_stat: os.stat_result, content: bytes) -> _FrozenFile:
    return _FrozenFile(
        device=source_stat.st_dev,
        inode=source_stat.st_ino,
        mode=stat.S_IMODE(source_stat.st_mode),
        uid=source_stat.st_uid,
        gid=source_stat.st_gid,
        size=source_stat.st_size,
        digest=hashlib.sha256(content).digest(),
    )


def _matches_frozen(source_stat: os.stat_result, frozen: _FrozenFile) -> bool:
    return (
        source_stat.st_dev,
        source_stat.st_ino,
        stat.S_IMODE(source_stat.st_mode),
        source_stat.st_uid,
        source_stat.st_gid,
        source_stat.st_size,
    ) == (
        frozen.device,
        frozen.inode,
        frozen.mode,
        frozen.uid,
        frozen.gid,
        frozen.size,
    )


def _verify_published(target_fd: int, filename: str, frozen: _FrozenFile) -> None:
    owner = _OwnedFd()
    try:
        owner.assign(os.open(filename, _SOURCE_OPEN_FLAGS, dir_fd=target_fd))
    except OSError:
        _raise("target-file-verify-failed")
    primary: BaseException | None = None
    try:
        before = os.fstat(owner.fileno())
        if not stat.S_ISREG(before.st_mode) or not _matches_frozen(before, frozen):
            _raise("target-file-verify-failed")
        content = _read_all(owner.fileno())
        after = os.fstat(owner.fileno())
        current = os.stat(filename, dir_fd=target_fd, follow_symlinks=False)
        if (
            not _matches_frozen(after, frozen)
            or not _matches_frozen(current, frozen)
            or hashlib.sha256(content).digest() != frozen.digest
        ):
            _raise("target-file-verify-failed")
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error("target-file-verify-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(owner, primary, "target-file-verify-failed")
    if primary is not None:
        _raise_primary(primary)


def _cleanup_name(target_fd: int, name: str) -> bool:
    removed = False
    for _attempt in range(2):
        try:
            os.unlink(name, dir_fd=target_fd)
            removed = True
            break
        except FileNotFoundError:
            return True
        except OSError:
            continue
    if not removed:
        return False
    try:
        os.fsync(target_fd)
    except OSError:
        return False
    return True


def _publish_copy(
    target_fd: int,
    filename: str,
    content: bytes,
    mode: int,
    uid: int,
    gid: int,
) -> None:
    temp_name = f".llmgateway-config-init-{secrets.token_hex(16)}"
    temp_owner = _OwnedFd()
    temp_exists = False
    published = False
    frozen: _FrozenFile | None = None
    primary: BaseException | None = None
    phase = "target-file-temp-create-failed"
    try:
        temp_owner.assign(os.open(temp_name, _TEMP_OPEN_FLAGS, mode, dir_fd=target_fd))
        temp_exists = True
        phase = "target-file-write-failed"
        _write_all(temp_owner.fileno(), content)
        phase = "target-file-metadata-failed"
        os.fchown(temp_owner.fileno(), uid, gid)
        os.fchmod(temp_owner.fileno(), mode)
        phase = "target-file-sync-failed"
        os.fsync(temp_owner.fileno())
        phase = "target-file-freeze-failed"
        frozen = _freeze_file(os.fstat(temp_owner.fileno()), content)
        close_error = temp_owner.close_error()
        if close_error is not None:
            _raise("target-file-close-failed")

        try:
            _rename_noreplace(target_fd, temp_name, filename)
        except FileExistsError:
            _raise("target-file-raced")
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_RENAME_ERRNOS:
                _raise("target-file-rename-unsupported")
            _raise("target-file-rename-failed")
        temp_exists = False
        published = True
        phase = "target-directory-sync-failed"
        os.fsync(target_fd)
        assert frozen is not None
        _verify_published(target_fd, filename, frozen)
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error(phase)
    except BaseException as exc:
        primary = exc

    primary = _finish_close(temp_owner, primary, "target-file-close-failed")
    if primary is not None and published:
        primary = _mark_publication_uncertain(primary)
    if temp_exists and not _cleanup_name(target_fd, temp_name) and primary is None:
        primary = _error("target-file-cleanup-failed")
    if primary is not None:
        _raise_primary(primary)


def initialize_config_directory(
    source_dir: str | os.PathLike[str],
    target_dir: str | os.PathLike[str],
    *,
    uid: int,
    gid: int,
) -> tuple[str, ...]:
    """Copy present legacy configs into one directory without overwriting bytes."""
    _validate_identity(uid, gid)
    source_owner = _OwnedFd(_open_directory(Path(source_dir), label="source"))
    target_owner = _OwnedFd()
    copied: list[str] = []
    primary: BaseException | None = None
    try:
        target_owner.assign(_ensure_target_directory(Path(target_dir), uid, gid))
        for filename in CONFIG_FILENAMES:
            if _normalize_existing(target_owner.fileno(), filename, uid, gid):
                continue
            try:
                source_stat = os.stat(
                    filename,
                    dir_fd=source_owner.fileno(),
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            except OSError:
                _raise("source-file-stat-failed")
            if not stat.S_ISREG(source_stat.st_mode):
                _raise("source-file-not-regular")
            content, mode = _read_source(source_owner.fileno(), filename)
            _publish_copy(
                target_owner.fileno(),
                filename,
                content,
                mode,
                uid,
                gid,
            )
            copied.append(filename)
        os.fsync(target_owner.fileno())
    except DockerConfigInitializationError as exc:
        primary = exc
    except OSError:
        primary = _error("target-directory-sync-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(target_owner, primary, "target-directory-close-failed")
    primary = _finish_close(source_owner, primary, "source-directory-close-failed")
    if primary is not None:
        _raise_primary(primary)
    return tuple(copied)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Initialize the single Docker configuration directory without overwrites."
    )
    parser.add_argument("--source-dir", default=".")
    parser.add_argument("--target-dir", default="./config")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    try:
        copied = initialize_config_directory(
            args.source_dir,
            args.target_dir,
            uid=_CONTAINER_UID,
            gid=_CONTAINER_GID,
        )
    except DockerConfigInitializationError as exc:
        publication = " publication=uncertain" if exc.publication_uncertain else ""
        print(
            f"docker-config-init: reason={exc.reason}{publication}",
            file=sys.stderr,
        )
        return 1
    except Exception:
        print("docker-config-init: reason=internal-error", file=sys.stderr)
        return 1
    print(f"docker-config-init: copied={len(copied)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
