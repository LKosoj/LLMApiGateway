"""Fail-closed, symlink-safe preparation of container configuration files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import os
import secrets
import stat
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Final, NoReturn

from .config_store import ConfigFile, ConfigSourceBundle, ConfigSourceError


_CONFIG_ROOT_ENV: Final[str] = "LLMGATEWAY_CONFIG_DIR"
_DEFAULT_CONFIG_ROOT: Final[str] = "/app/config"
_DIRECTORY_OPEN_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_READ_OPEN_FLAGS: Final[int] = os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
_CREATE_OPEN_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
_PROBE_CONTENT: Final[bytes] = b"llmgateway-config-directory-preflight\n"
_RENAME_NOREPLACE: Final[int] = 1
_UNSUPPORTED_RENAME_ERRNOS: Final[frozenset[int]] = frozenset(
    {errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP, errno.ENOTSUP}
)
_MANDATORY_FILES: Final[frozenset[ConfigFile]] = frozenset(
    {ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES}
)
_OPTIONAL_DEFAULTS: Final[tuple[tuple[ConfigFile, bytes], ...]] = (
    (ConfigFile.OPERATION_RULES, b"{}\n"),
    (ConfigFile.MODEL_RULES, b"{}\n"),
    (ConfigFile.FUSION_RULES, b"[]\n"),
    (ConfigFile.ROUTER_RULES, b"[]\n"),
)


class ConfigDirectoryPreflightError(RuntimeError):
    """A credential-safe container configuration failure."""

    def __init__(self, reason: str, *, publication_uncertain: bool = False) -> None:
        self.reason = reason
        self.publication_uncertain = publication_uncertain
        super().__init__(f"container config directory preflight failed: {reason}")


class _OwnedFd:
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
) -> ConfigDirectoryPreflightError:
    return ConfigDirectoryPreflightError(
        reason,
        publication_uncertain=publication_uncertain,
    )


def _raise(reason: str) -> NoReturn:
    raise _error(reason)


def _raise_primary(primary: BaseException) -> NoReturn:
    raise primary


def _mark_publication_uncertain(primary: BaseException) -> BaseException:
    if isinstance(primary, ConfigDirectoryPreflightError):
        reason = primary.reason
    elif isinstance(primary, Exception):
        reason = "optional-default-internal-error"
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


def _validate_paths(
    config_root: Path,
    configured_paths: Mapping[ConfigFile, Path],
) -> dict[ConfigFile, Path]:
    if set(configured_paths) != set(ConfigFile):
        _raise("config-path-set-invalid")

    normalized: dict[ConfigFile, Path] = {}
    seen: set[Path] = set()
    for config_file in ConfigFile:
        path = Path(configured_paths[config_file])
        if not path.is_absolute():
            _raise("config-path-not-absolute")
        if ".." in path.parts or path.parent != config_root:
            _raise("config-path-not-direct-child")
        if path in seen:
            _raise("config-path-collision")
        seen.add(path)
        normalized[config_file] = path
    return normalized


def _open_validated_directory(directory: Path, reason_prefix: str) -> int:
    if not directory.is_absolute() or ".." in directory.parts:
        _raise(f"{reason_prefix}-invalid")
    try:
        path_stat = os.lstat(directory)
    except FileNotFoundError:
        _raise(f"{reason_prefix}-missing")
    except OSError:
        _raise(f"{reason_prefix}-stat-failed")
    if stat.S_ISLNK(path_stat.st_mode):
        _raise(f"{reason_prefix}-symlink")
    if not stat.S_ISDIR(path_stat.st_mode):
        _raise(f"{reason_prefix}-not-directory")

    current = _OwnedFd()
    try:
        current.assign(os.open("/", _DIRECTORY_OPEN_FLAGS))
        for component in directory.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current.fileno())
            close_error = current.close_error()
            if close_error is not None:
                os.close(next_fd)
                _raise(f"{reason_prefix}-close-failed")
            current.assign(next_fd)
    except ConfigDirectoryPreflightError:
        current.close_error()
        raise
    except OSError as exc:
        current.close_error()
        if exc.errno in {errno.ELOOP, errno.ENOTDIR}:
            _raise(f"{reason_prefix}-unsafe")
        _raise(f"{reason_prefix}-open-failed")

    primary: BaseException | None = None
    try:
        opened_stat = os.fstat(current.fileno())
        if not stat.S_ISDIR(opened_stat.st_mode):
            _raise(f"{reason_prefix}-not-directory")
        if (opened_stat.st_dev, opened_stat.st_ino) != (
            path_stat.st_dev,
            path_stat.st_ino,
        ):
            _raise(f"{reason_prefix}-changed")
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error(f"{reason_prefix}-stat-failed")
    except BaseException as exc:
        primary = exc
    if primary is not None:
        primary = _finish_close(current, primary, f"{reason_prefix}-close-failed")
        assert primary is not None
        _raise_primary(primary)
    fd = current.take()
    assert fd is not None
    return fd


def _open_validated_root(config_root: Path) -> int:
    return _open_validated_directory(config_root, "config-root")


def _validate_application_root(application_root: Path) -> None:
    owner = _OwnedFd(_open_validated_directory(application_root, "app-root"))
    primary = _finish_close(owner, None, "app-root-close-failed")
    if primary is not None:
        _raise_primary(primary)


def _validate_config_root(config_root: Path) -> None:
    owner = _OwnedFd(_open_validated_root(config_root))
    primary = _finish_close(owner, None, "config-root-close-failed")
    if primary is not None:
        _raise_primary(primary)


def _validate_existing_files(root_fd: int, paths: Mapping[ConfigFile, Path]) -> None:
    for config_file in ConfigFile:
        try:
            source_stat = os.stat(
                paths[config_file].name,
                dir_fd=root_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            continue
        except OSError:
            _raise("config-file-stat-failed")
        if not stat.S_ISREG(source_stat.st_mode):
            _raise("config-file-not-regular")


def _write_all(fd: int, content: bytes) -> None:
    offset = 0
    while offset < len(content):
        written = os.write(fd, content[offset:])
        if written <= 0:
            raise OSError("config write made no progress")
        offset += written


def _read_all(fd: int) -> bytes:
    chunks: list[bytes] = []
    while True:
        chunk = os.read(fd, 1024 * 1024)
        if not chunk:
            return b"".join(chunks)
        chunks.append(chunk)


def _cleanup_names(root_fd: int, names: Sequence[str]) -> bool:
    removed = False
    cleanup_failed = False
    for name in names:
        cleaned = False
        for _attempt in range(2):
            try:
                os.unlink(name, dir_fd=root_fd)
                removed = True
                cleaned = True
                break
            except FileNotFoundError:
                cleaned = True
                break
            except OSError:
                continue
        if not cleaned:
            cleanup_failed = True
    if removed:
        try:
            os.fsync(root_fd)
        except OSError:
            cleanup_failed = True
    return not cleanup_failed


def check_config_directory(
    config_root: str | os.PathLike[str],
    configured_paths: Mapping[ConfigFile, Path],
) -> None:
    """Prove that all six configs share one writable, rename-capable directory."""
    root = Path(config_root)
    paths = _validate_paths(root, configured_paths)
    root_owner = _OwnedFd(_open_validated_root(root))
    probe_owner = _OwnedFd()
    token = secrets.token_hex(16)
    initial_name = f".llmgateway-config-preflight-{token}.write"
    renamed_name = f".llmgateway-config-preflight-{token}.renamed"
    probe_exists = False
    primary: BaseException | None = None
    phase = "probe-create-failed"
    try:
        _validate_existing_files(root_owner.fileno(), paths)
        probe_owner.assign(
            os.open(
                initial_name,
                _CREATE_OPEN_FLAGS,
                0o600,
                dir_fd=root_owner.fileno(),
            )
        )
        probe_exists = True
        phase = "probe-write-failed"
        _write_all(probe_owner.fileno(), _PROBE_CONTENT)
        phase = "probe-sync-failed"
        os.fsync(probe_owner.fileno())
        close_error = probe_owner.close_error()
        if close_error is not None:
            _raise("probe-close-failed")

        phase = "probe-rename-failed"
        os.rename(
            initial_name,
            renamed_name,
            src_dir_fd=root_owner.fileno(),
            dst_dir_fd=root_owner.fileno(),
        )
        phase = "directory-sync-failed"
        os.fsync(root_owner.fileno())
        phase = "probe-unlink-failed"
        os.unlink(renamed_name, dir_fd=root_owner.fileno())
        probe_exists = False
        phase = "directory-sync-failed"
        os.fsync(root_owner.fileno())
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error(phase)
    except BaseException as exc:
        primary = exc

    primary = _finish_close(probe_owner, primary, "probe-close-failed")
    if probe_exists and not _cleanup_names(
        root_owner.fileno(),
        (initial_name, renamed_name),
    ):
        if primary is None:
            primary = _error("probe-cleanup-failed")
    primary = _finish_close(root_owner, primary, "config-root-close-failed")
    if primary is not None:
        _raise_primary(primary)


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


def _verify_published_default(root_fd: int, filename: str, frozen: _FrozenFile) -> None:
    owner = _OwnedFd()
    try:
        owner.assign(os.open(filename, _READ_OPEN_FLAGS, dir_fd=root_fd))
    except OSError:
        _raise("optional-default-verify-failed")
    primary: BaseException | None = None
    try:
        before = os.fstat(owner.fileno())
        if not stat.S_ISREG(before.st_mode) or not _matches_frozen(before, frozen):
            _raise("optional-default-verify-failed")
        content = _read_all(owner.fileno())
        after = os.fstat(owner.fileno())
        current = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        if (
            not _matches_frozen(after, frozen)
            or not _matches_frozen(current, frozen)
            or hashlib.sha256(content).digest() != frozen.digest
        ):
            _raise("optional-default-verify-failed")
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error("optional-default-verify-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(owner, primary, "optional-default-verify-failed")
    if primary is not None:
        _raise_primary(primary)


def _verify_concurrent_default(root_fd: int, filename: str, expected: bytes) -> None:
    owner = _OwnedFd()
    try:
        owner.assign(os.open(filename, _READ_OPEN_FLAGS, dir_fd=root_fd))
    except OSError:
        _raise("optional-default-target-conflict")
    primary: BaseException | None = None
    try:
        before = os.fstat(owner.fileno())
        if not stat.S_ISREG(before.st_mode) or before.st_nlink != 1:
            _raise("optional-default-target-conflict")
        content = _read_all(owner.fileno())
        try:
            os.fsync(owner.fileno())
        except OSError:
            raise _error(
                "optional-default-concurrent-sync-failed",
                publication_uncertain=True,
            ) from None
        after = os.fstat(owner.fileno())
        current = os.stat(filename, dir_fd=root_fd, follow_symlinks=False)
        if (
            before.st_dev,
            before.st_ino,
            before.st_mode,
            before.st_size,
            before.st_mtime_ns,
            before.st_ctime_ns,
        ) != (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) or (
            after.st_dev,
            after.st_ino,
            after.st_mode,
            after.st_size,
            after.st_mtime_ns,
            after.st_ctime_ns,
        ) != (
            current.st_dev,
            current.st_ino,
            current.st_mode,
            current.st_size,
            current.st_mtime_ns,
            current.st_ctime_ns,
        ) or content != expected:
            _raise("optional-default-target-conflict")
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error("optional-default-target-conflict")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(owner, primary, "optional-default-target-conflict")
    if primary is not None:
        _raise_primary(primary)


def _publish_optional_default(root_fd: int, filename: str, content: bytes) -> None:
    temp_name = f".llmgateway-config-default-{secrets.token_hex(16)}"
    temp_owner = _OwnedFd()
    temp_exists = False
    published = False
    frozen: _FrozenFile | None = None
    primary: BaseException | None = None
    phase = "optional-default-create-failed"
    try:
        temp_owner.assign(
            os.open(temp_name, _CREATE_OPEN_FLAGS, 0o644, dir_fd=root_fd)
        )
        temp_exists = True
        phase = "optional-default-write-failed"
        _write_all(temp_owner.fileno(), content)
        phase = "optional-default-metadata-failed"
        os.fchmod(temp_owner.fileno(), 0o644)
        phase = "optional-default-sync-failed"
        os.fsync(temp_owner.fileno())
        frozen = _freeze_file(os.fstat(temp_owner.fileno()), content)
        close_error = temp_owner.close_error()
        if close_error is not None:
            _raise("optional-default-close-failed")
        try:
            _rename_noreplace(root_fd, temp_name, filename)
        except FileExistsError:
            _verify_concurrent_default(root_fd, filename, content)
            published = True
            phase = "optional-default-directory-sync-failed"
            os.fsync(root_fd)
        except OSError as exc:
            if exc.errno in _UNSUPPORTED_RENAME_ERRNOS:
                _raise("optional-default-rename-unsupported")
            _raise("optional-default-rename-failed")
        else:
            temp_exists = False
            published = True
            phase = "optional-default-directory-sync-failed"
            os.fsync(root_fd)
            assert frozen is not None
            _verify_published_default(root_fd, filename, frozen)
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error(phase)
    except BaseException as exc:
        primary = exc
    primary = _finish_close(temp_owner, primary, "optional-default-close-failed")
    if primary is not None and published:
        primary = _mark_publication_uncertain(primary)
    if temp_exists and not _cleanup_names(root_fd, (temp_name,)) and primary is None:
        primary = _error("optional-default-cleanup-failed")
    if primary is not None:
        _raise_primary(primary)


def _capture_sources(
    application_root: Path,
    configured_paths: Mapping[ConfigFile, Path],
) -> ConfigSourceBundle:
    try:
        return ConfigSourceBundle.capture(
            application_root,
            overrides=configured_paths,
        )
    except ConfigSourceError as exc:
        if (
            exc.config_file in _MANDATORY_FILES
            and exc.reason == "required config source is missing"
        ):
            _raise("mandatory-config-missing")
        _raise("config-source-invalid")
    except (OSError, TypeError, ValueError):
        _raise("config-source-invalid")


def _materialize_optional_defaults(
    config_root: Path,
    configured_paths: Mapping[ConfigFile, Path],
    initial_sources: ConfigSourceBundle,
) -> None:
    root_owner = _OwnedFd(_open_validated_root(config_root))
    primary: BaseException | None = None
    try:
        for config_file, content in _OPTIONAL_DEFAULTS:
            if initial_sources[config_file].exists:
                continue
            _publish_optional_default(
                root_owner.fileno(),
                configured_paths[config_file].name,
                content,
            )
    except ConfigDirectoryPreflightError as exc:
        primary = exc
    except OSError:
        primary = _error("optional-default-internal-io-failed")
    except BaseException as exc:
        primary = exc
    primary = _finish_close(root_owner, primary, "config-root-close-failed")
    if primary is not None:
        _raise_primary(primary)


def prepare_container_config(
    application_root: str | os.PathLike[str],
    config_root: str | os.PathLike[str],
    configured_paths: Mapping[ConfigFile, Path],
) -> None:
    """Validate mandatory sources, create optional files, and recapture all six."""
    app_root = Path(application_root)
    root = Path(config_root)
    paths = _validate_paths(root, configured_paths)
    _validate_application_root(app_root)
    _validate_config_root(root)
    initial_sources = _capture_sources(app_root, paths)
    check_config_directory(root, paths)
    _materialize_optional_defaults(root, paths, initial_sources)
    final_sources = _capture_sources(app_root, paths)
    if not all(final_sources[config_file].exists for config_file in ConfigFile):
        _raise("config-source-invalid")


def main(argv: Sequence[str] = ()) -> int:
    if argv:
        print("container-config-preflight: reason=invalid-arguments", file=sys.stderr)
        return 1
    try:
        from .loader import ConfigLoader
    except Exception:
        print("container-config-preflight: reason=internal-error", file=sys.stderr)
        return 1

    try:
        loader = ConfigLoader()
        prepare_container_config(
            loader.project_root,
            Path(os.getenv(_CONFIG_ROOT_ENV, _DEFAULT_CONFIG_ROOT)),
            loader.configured_paths,
        )
    except ConfigDirectoryPreflightError as exc:
        publication = " publication=uncertain" if exc.publication_uncertain else ""
        print(
            f"container-config-preflight: reason={exc.reason}{publication}",
            file=sys.stderr,
        )
        return 1
    except (TypeError, ValueError):
        print("container-config-preflight: reason=config-path-invalid", file=sys.stderr)
        return 1
    except Exception:
        print("container-config-preflight: reason=internal-error", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
