"""Fail-closed single-process guards for one local POSIX state directory.

The lease is a safety boundary for local filesystems, not leader election or a
distributed-lock implementation. Callers must provide an existing canonical
absolute state directory and must stop startup when acquisition fails.
"""

from __future__ import annotations

import errno
import logging
import os
import shlex
import stat
import sys
import threading
from collections.abc import Mapping, Sequence
from pathlib import Path

try:
    import fcntl as _fcntl
except ImportError:  # pragma: no cover - exercised through the platform seam
    _fcntl = None


logger = logging.getLogger(__name__)


SINGLE_APPLICATION_WORKER_COUNT = 1

_WORKER_ENV_SOURCES = (
    "GATEWAY_WORKERS",
    "WEB_CONCURRENCY",
    "UVICORN_WORKERS",
)
_GUNICORN_SOURCE = "GUNICORN_CMD_ARGS"
_GUNICORN_CONFIG_OPTION = "--config"
_GUNICORN_CONFIG_MIN_UNIQUE_PREFIX = "--co"
_LOCK_FILENAME = ".llmgateway-single-process.lock"
_LOCK_FILE_MODE = 0o600

_SAFE_ERROR_SOURCES = frozenset(
    {
        *_WORKER_ENV_SOURCES,
        _GUNICORN_SOURCE,
        "arguments",
        "environment",
        "internal",
        "lock-file",
        "platform",
        "state-directory",
    }
)
_SAFE_REASON_CODES = frozenset(
    {
        "config-source-not-supported",
        "duplicate-worker-option",
        "internal-validation-failed",
        "invalid-cli-arguments",
        "invalid-environment",
        "invalid-state-directory",
        "invalid-worker-count",
        "invariant-failed",
        "lease-acquire-failed",
        "lease-close-failed",
        "lease-contended",
        "lock-open-failed",
        "malformed-worker-option",
        "state-directory-close-failed",
        "state-directory-open-failed",
        "unsafe-lock-file",
        "unsupported-platform",
    }
)


class SingleProcessInvariantError(RuntimeError):
    """Credential-safe failure of the single-process invariant."""

    def __init__(self, source: str, reason_code: str) -> None:
        safe_source = source if type(source) is str and source in _SAFE_ERROR_SOURCES else "internal"
        safe_reason = (
            reason_code
            if type(reason_code) is str and reason_code in _SAFE_REASON_CODES
            else "invariant-failed"
        )
        self.source = safe_source
        self.reason_code = safe_reason
        super().__init__(
            f"source={safe_source} reason={safe_reason}: "
            "exactly one application worker is required"
        )


def validate_single_worker_environment(environ: Mapping[str, str]) -> None:
    """Reject any known worker-count intent that is not canonical ASCII ``1``."""
    for source in _WORKER_ENV_SOURCES:
        present, value = _read_environment_value(environ, source)
        if present and value != "1":
            raise SingleProcessInvariantError(source, "invalid-worker-count")

    present, gunicorn_args = _read_environment_value(environ, _GUNICORN_SOURCE)
    if present:
        _parse_gunicorn_worker_option(gunicorn_args)


def _read_environment_value(
    environ: Mapping[str, str],
    source: str,
) -> tuple[bool, str]:
    try:
        if source not in environ:
            return False, ""
        value = environ[source]
    except Exception:
        raise SingleProcessInvariantError(source, "invalid-environment") from None
    if type(value) is not str:
        raise SingleProcessInvariantError(source, "invalid-environment")
    return True, value


def _parse_gunicorn_worker_option(raw: str) -> int | None:
    """Return the one explicit Gunicorn worker count, if present."""
    try:
        tokens = shlex.split(raw, posix=True)
    except (TypeError, ValueError):
        raise SingleProcessInvariantError(
            _GUNICORN_SOURCE,
            "malformed-worker-option",
        ) from None

    worker_count: int | None = None
    index = 0
    while index < len(tokens):
        token = tokens[index]
        long_option = token.partition("=")[0]
        is_config_long_option = (
            len(long_option) >= len(_GUNICORN_CONFIG_MIN_UNIQUE_PREFIX)
            and _GUNICORN_CONFIG_OPTION.startswith(long_option)
        )
        if token.startswith("-c") or is_config_long_option:
            raise SingleProcessInvariantError(
                _GUNICORN_SOURCE,
                "config-source-not-supported",
            )

        worker_value: str | None = None
        if token in {"-w", "--workers"}:
            index += 1
            if index >= len(tokens):
                raise SingleProcessInvariantError(
                    _GUNICORN_SOURCE,
                    "malformed-worker-option",
                )
            worker_value = tokens[index]
        elif token.startswith("--workers="):
            worker_value = token.removeprefix("--workers=")
        elif token.startswith("--workers") or token.startswith("-w"):
            worker_value = token[2:]

        if worker_value is not None:
            if worker_count is not None:
                raise SingleProcessInvariantError(
                    _GUNICORN_SOURCE,
                    "duplicate-worker-option",
                )
            if worker_value != "1":
                raise SingleProcessInvariantError(
                    _GUNICORN_SOURCE,
                    "invalid-worker-count",
                )
            worker_count = SINGLE_APPLICATION_WORKER_COUNT
        index += 1
    return worker_count


class SingleProcessLease:
    """Own one non-blocking advisory lock until explicit asynchronous close."""

    def __init__(self, descriptor: int) -> None:
        self._descriptor: int | None = descriptor
        self._close_lock = threading.Lock()

    @classmethod
    def acquire(cls, state_dir: Path) -> SingleProcessLease:
        """Acquire the fixed empty lock file below an existing state directory."""
        _require_supported_platform()
        lock_file_path = state_dir / _LOCK_FILENAME
        directory_descriptor = _open_state_directory(state_dir)
        lock_descriptor: int | None = None
        locked = False
        try:
            lock_descriptor = _open_lock_file(directory_descriptor)
            if _lock_file_needs_mode_repair(lock_descriptor):
                # Take the lock before touching the entry: an owner that is still
                # alive on this inode must win the contention check, not lose its
                # name to a replacement.
                _acquire_descriptor_lock(lock_descriptor)
                locked = True
                _replace_unsafe_lock_file(
                    directory_descriptor,
                    lock_descriptor,
                    display_path=lock_file_path,
                )
                _release_descriptor_quietly(lock_descriptor, locked=True)
                lock_descriptor = None
                locked = False
                lock_descriptor = _open_lock_file(directory_descriptor)
            _validate_lock_file(lock_descriptor, display_path=lock_file_path)
            _acquire_descriptor_lock(lock_descriptor)
            locked = True
            _validate_named_lock(
                directory_descriptor,
                lock_descriptor,
                display_path=lock_file_path,
            )
            try:
                os.close(directory_descriptor)
            except OSError:
                raise SingleProcessInvariantError(
                    "state-directory",
                    "state-directory-close-failed",
                ) from None
            directory_descriptor = -1
            lease = cls(lock_descriptor)
            lock_descriptor = None
            return lease
        finally:
            if directory_descriptor >= 0:
                _close_descriptor_quietly(directory_descriptor)
            if lock_descriptor is not None:
                _release_descriptor_quietly(lock_descriptor, locked=locked)

    @property
    def closed(self) -> bool:
        with self._close_lock:
            return self._descriptor is None

    async def close(self) -> None:
        """Unlock and close exactly once, including under concurrent callers."""
        failure: BaseException | None = None
        with self._close_lock:
            descriptor = self._descriptor
            if descriptor is None:
                return
            self._descriptor = None
            try:
                fcntl_module = _fcntl
                if fcntl_module is None:
                    raise SingleProcessInvariantError(
                        "platform",
                        "unsupported-platform",
                    )
                fcntl_module.flock(descriptor, fcntl_module.LOCK_UN)
            except BaseException as error:
                failure = error
            finally:
                try:
                    os.close(descriptor)
                except BaseException as close_error:
                    if failure is None or (
                        isinstance(failure, Exception)
                        and not isinstance(close_error, Exception)
                    ):
                        failure = close_error
        if failure is None:
            return
        if isinstance(failure, Exception):
            raise SingleProcessInvariantError(
                "lock-file",
                "lease-close-failed",
            ) from None
        raise failure

    def __repr__(self) -> str:
        return f"{type(self).__name__}(closed={self.closed})"


def _require_supported_platform() -> None:
    required_os_flags = ("O_CLOEXEC", "O_DIRECTORY", "O_NOFOLLOW", "O_NONBLOCK")
    required_fcntl_constants = ("LOCK_EX", "LOCK_NB", "LOCK_UN")
    try:
        fcntl_module = _fcntl
        supported = (
            os.name == "posix"
            and fcntl_module is not None
            and callable(getattr(fcntl_module, "flock", None))
            and all(
                isinstance(getattr(fcntl_module, name, None), int)
                for name in required_fcntl_constants
            )
            and all(isinstance(getattr(os, name, None), int) for name in required_os_flags)
            and all(
                callable(getattr(os, name, None))
                for name in ("close", "fstat", "open", "set_inheritable", "stat", "unlink")
            )
            and os.open in os.supports_dir_fd
            and os.stat in os.supports_dir_fd
            and os.unlink in os.supports_dir_fd
            and os.stat in os.supports_follow_symlinks
        )
    except Exception:
        supported = False
    if not supported:
        raise SingleProcessInvariantError("platform", "unsupported-platform")


def _open_state_directory(state_dir: Path) -> int:
    try:
        raw_path = os.fspath(state_dir)
        path = Path(raw_path)
    except (TypeError, ValueError):
        raise SingleProcessInvariantError(
            "state-directory",
            "invalid-state-directory",
        ) from None
    if not isinstance(raw_path, str) or path.anchor != os.sep or ".." in path.parts:
        raise SingleProcessInvariantError(
            "state-directory",
            "invalid-state-directory",
        )

    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
    descriptor: int | None = None
    try:
        descriptor = os.open(os.sep, flags)
        for component in path.parts[1:]:
            next_descriptor = os.open(component, flags, dir_fd=descriptor)
            try:
                os.close(descriptor)
            except OSError:
                _close_descriptor_quietly(next_descriptor)
                raise
            descriptor = next_descriptor
        return descriptor
    except NotImplementedError:
        if descriptor is not None:
            _close_descriptor_quietly(descriptor)
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            _close_descriptor_quietly(descriptor)
        raise SingleProcessInvariantError(
            "state-directory",
            "state-directory-open-failed",
        ) from None


def _open_lock_file(directory_descriptor: int) -> int:
    flags = os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
    descriptor: int | None = None
    try:
        descriptor = os.open(
            _LOCK_FILENAME,
            flags,
            _LOCK_FILE_MODE,
            dir_fd=directory_descriptor,
        )
        os.set_inheritable(descriptor, False)
        return descriptor
    except NotImplementedError:
        if descriptor is not None:
            _close_descriptor_quietly(descriptor)
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except (OSError, TypeError, ValueError):
        if descriptor is not None:
            _close_descriptor_quietly(descriptor)
        raise SingleProcessInvariantError("lock-file", "lock-open-failed") from None


def _lock_file_needs_mode_repair(descriptor: int) -> bool:
    """Report a lock file that is safe in every respect except its mode bits."""
    try:
        file_stat = os.fstat(descriptor)
    except OSError:
        return False
    return (
        stat.S_ISREG(file_stat.st_mode)
        and file_stat.st_size == 0
        and file_stat.st_nlink == 1
        and stat.S_IMODE(file_stat.st_mode) != _LOCK_FILE_MODE
    )


def _replace_unsafe_lock_file(
    directory_descriptor: int,
    lock_descriptor: int,
    *,
    display_path: Path,
) -> None:
    """Unlink the exclusively held lock entry so the next open recreates it at 0600."""
    try:
        descriptor_stat = os.fstat(lock_descriptor)
        named_stat = os.stat(
            _LOCK_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except NotImplementedError:
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except OSError as exc:
        logger.error(
            "Single-process lock file replacement failed: path=%s errno=%s.",
            display_path,
            exc.errno,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file") from None
    if (
        named_stat.st_dev != descriptor_stat.st_dev
        or named_stat.st_ino != descriptor_stat.st_ino
    ):
        logger.error(
            "Single-process lock file changed before replacement: path=%s.",
            display_path,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file")
    try:
        os.unlink(_LOCK_FILENAME, dir_fd=directory_descriptor)
    except NotImplementedError:
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except OSError as exc:
        logger.error(
            "Single-process lock file replacement failed: path=%s errno=%s.",
            display_path,
            exc.errno,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file") from None
    logger.warning(
        "Replaced single-process lock file with unsafe mode: path=%s mode=%o.",
        display_path,
        stat.S_IMODE(descriptor_stat.st_mode),
    )


def _validate_lock_file(descriptor: int, *, display_path: Path) -> os.stat_result:
    try:
        file_stat = os.fstat(descriptor)
    except OSError as exc:
        logger.error(
            "Single-process lock file is unsafe: path=%s errno=%s.",
            display_path,
            exc.errno,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file") from None
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or stat.S_IMODE(file_stat.st_mode) != _LOCK_FILE_MODE
        or file_stat.st_size != 0
        or file_stat.st_nlink != 1
    ):
        logger.error(
            "Single-process lock file is unsafe: path=%s mode=%o size=%s nlink=%s.",
            display_path,
            file_stat.st_mode,
            file_stat.st_size,
            file_stat.st_nlink,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file")
    return file_stat


def _acquire_descriptor_lock(descriptor: int) -> None:
    fcntl_module = _fcntl
    if fcntl_module is None:
        raise SingleProcessInvariantError("platform", "unsupported-platform")
    try:
        fcntl_module.flock(descriptor, fcntl_module.LOCK_EX | fcntl_module.LOCK_NB)
    except NotImplementedError:
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except OSError as exc:
        reason = "lease-contended" if exc.errno in {errno.EACCES, errno.EAGAIN} else "lease-acquire-failed"
        raise SingleProcessInvariantError("lock-file", reason) from None


def _validate_named_lock(
    directory_descriptor: int,
    lock_descriptor: int,
    *,
    display_path: Path,
) -> None:
    descriptor_stat = _validate_lock_file(lock_descriptor, display_path=display_path)
    try:
        named_stat = os.stat(
            _LOCK_FILENAME,
            dir_fd=directory_descriptor,
            follow_symlinks=False,
        )
    except NotImplementedError:
        raise SingleProcessInvariantError("platform", "unsupported-platform") from None
    except OSError as exc:
        logger.error(
            "Single-process lock file is unsafe: path=%s errno=%s.",
            display_path,
            exc.errno,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file") from None
    if (
        not stat.S_ISREG(named_stat.st_mode)
        or stat.S_IMODE(named_stat.st_mode) != _LOCK_FILE_MODE
        or named_stat.st_size != 0
        or named_stat.st_nlink != 1
        or named_stat.st_dev != descriptor_stat.st_dev
        or named_stat.st_ino != descriptor_stat.st_ino
    ):
        logger.error(
            "Single-process lock file is unsafe: path=%s mode=%o size=%s nlink=%s "
            "dev=%s ino=%s (expected dev=%s ino=%s).",
            display_path,
            named_stat.st_mode,
            named_stat.st_size,
            named_stat.st_nlink,
            named_stat.st_dev,
            named_stat.st_ino,
            descriptor_stat.st_dev,
            descriptor_stat.st_ino,
        )
        raise SingleProcessInvariantError("lock-file", "unsafe-lock-file")


def _close_descriptor_quietly(descriptor: int) -> None:
    try:
        os.close(descriptor)
    except OSError:
        pass


def _release_descriptor_quietly(descriptor: int, *, locked: bool) -> None:
    if locked and _fcntl is not None:
        try:
            _fcntl.flock(descriptor, _fcntl.LOCK_UN)
        except OSError:
            pass
    _close_descriptor_quietly(descriptor)


def _run_cli(argv: Sequence[str]) -> int:
    if tuple(argv) != ("--check-environment",):
        error = SingleProcessInvariantError("arguments", "invalid-cli-arguments")
        sys.stderr.write(f"{error}\n")
        return 2
    try:
        validate_single_worker_environment(os.environ)
    except SingleProcessInvariantError as error:
        sys.stderr.write(f"{error}\n")
        return 1
    except Exception:
        error = SingleProcessInvariantError(
            "environment",
            "internal-validation-failed",
        )
        sys.stderr.write(f"{error}\n")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(_run_cli(sys.argv[1:]))
