"""Durable user backups for JSON5 comments removed by structured saves."""

from __future__ import annotations

import errno
import ctypes
import os
import re
import secrets
import stat
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from pathlib import Path
from threading import Lock
from typing import Final, Self

from .config_sources import ConfigDocument, ConfigFile, ConfigFileMetadata


__all__ = (
    "CommentsBackupError",
    "CommentsBackupLifecycle",
    "CommentsBackupPublishResult",
    "CommentsBackupState",
    "CommentsBackupStateError",
)


_DIRECTORY_OPEN_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC
)
_BACKUP_OPEN_FLAGS: Final[int] = (
    os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
)
_VERIFY_OPEN_FLAGS: Final[int] = (
    os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK
)
_BACKUP_MODE: Final[int] = 0o600
_MAX_CREATE_ATTEMPTS: Final[int] = 100
_MAX_BACKUPS: Final[int] = 10
_RENAME_NOREPLACE: Final[int] = 1
_PRIVATE_CLEANUP_PREFIX: Final[str] = ".llmgateway-comments-backup-trash-"
_TIMESTAMP_PATTERN: Final[str] = (
    r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{6}Z"
)

_timestamp_lock = Lock()
_last_timestamp: datetime | None = None


class CommentsBackupState(StrEnum):
    """Explicit lifecycle state for one comments backup."""

    READY = "ready"
    PREPARED = "prepared"
    PUBLISHED = "published"
    ABORTED = "aborted"
    CLEANUP_REQUIRED = "cleanup_required"
    FAILED = "failed"


class _CleanupAction(StrEnum):
    ABORT = "abort"
    ROTATE = "rotate"


class CommentsBackupError(RuntimeError):
    """Credential-safe failure while owning a comments backup."""

    def __init__(self, config_file: ConfigFile, reason: str) -> None:
        self.config_file = config_file
        self.reason = reason
        super().__init__(f"{config_file.value}: {reason}")


class CommentsBackupStateError(CommentsBackupError):
    """An operation was requested from an invalid lifecycle state."""

    def __init__(
        self,
        config_file: ConfigFile,
        *,
        action: str,
        state: CommentsBackupState,
    ) -> None:
        self.action = action
        self.state = state
        super().__init__(
            config_file,
            f"cannot {action} comments backup while state is {state.value}",
        )


@dataclass(frozen=True, slots=True)
class CommentsBackupPublishResult:
    """Verified public basename plus bounded cleanup ownership state."""

    basename: str | None
    cleanup_pending: bool


@dataclass(frozen=True, slots=True)
class _ArtifactIdentity:
    device: int
    inode: int

    @classmethod
    def from_stat(cls, source_stat: os.stat_result) -> Self:
        return cls(device=source_stat.st_dev, inode=source_stat.st_ino)


@dataclass(frozen=True, slots=True)
class _PathIdentity:
    device: int
    inode: int
    file_type: int

    @classmethod
    def from_stat(cls, source_stat: os.stat_result) -> Self:
        return cls(
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
            file_type=stat.S_IFMT(source_stat.st_mode),
        )


class CommentsBackupLifecycle:
    """Create, publish, rotate, or abort one exact-byte user backup."""

    __slots__ = (
        "_artifact_identity",
        "_artifact_fd",
        "_backup_name",
        "_cleanup_action",
        "_config_file",
        "_content",
        "_expected_metadata",
        "_parent_fd",
        "_parent_identity",
        "_path",
        "_pending_fds",
        "_moved_expected_identity",
        "_moved_name",
        "_moved_target",
        "_published_name",
        "_rotation_needs_sync",
        "_state",
    )

    def __init__(
        self,
        *,
        document: ConfigDocument,
        parent_fd: int,
        parent_identity: tuple[int, int],
    ) -> None:
        assert document.content is not None
        assert document.metadata is not None
        self._config_file = document.config_file
        self._path = document.path
        self._content = document.content
        self._expected_metadata = document.metadata
        self._parent_fd: int | None = parent_fd
        self._parent_identity = parent_identity
        self._state = CommentsBackupState.READY
        self._backup_name: str | None = None
        self._artifact_identity: _ArtifactIdentity | None = None
        self._artifact_fd: int | None = None
        self._pending_fds: list[int] = []
        self._published_name: str | None = None
        self._moved_name: str | None = None
        self._moved_target: str | None = None
        self._moved_expected_identity: _ArtifactIdentity | None = None
        self._cleanup_action: _CleanupAction | None = None
        self._rotation_needs_sync = False

    @classmethod
    def begin(cls, document: ConfigDocument) -> Self | None:
        """Bind the exact source bytes when they contain a JSON5 comment."""
        if not isinstance(document, ConfigDocument):
            raise TypeError("document must be a ConfigDocument")
        if not document.exists or not document.content:
            return None
        try:
            if not _json5_has_comments(document.content):
                return None
            if document.metadata is None:
                raise ValueError("source metadata is unavailable")
            if _canonicalize_path(document.path) != document.path:
                raise ValueError("non-canonical path")
            if not document.path.is_absolute() or not document.path.name:
                raise ValueError("invalid target name")
            parent_fd = _open_parent_directory(document.path)
            try:
                parent_stat = os.fstat(parent_fd)
                if not stat.S_ISDIR(parent_stat.st_mode):
                    raise OSError(errno.ENOTDIR, "not a directory")
                return cls(
                    document=document,
                    parent_fd=parent_fd,
                    parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
                )
            except BaseException:
                os.close(parent_fd)
                raise
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, CommentsBackupError):
                raise
            raise CommentsBackupError(
                document.config_file,
                f"comments backup begin failed ({_safe_exception_type(exc)})",
            ) from None

    @property
    def state(self) -> CommentsBackupState:
        return self._state

    @property
    def cleanup_pending(self) -> bool:
        return self._state is CommentsBackupState.CLEANUP_REQUIRED

    def prepare(self) -> None:
        """Durably create an exact-byte mode-0600 backup before replacement."""
        self._require_state(CommentsBackupState.READY, action="prepare")
        backup_fd: int | None = None
        phase = "creation"
        try:
            self._validate_parent_identity()
            phase = "target validation"
            self._validate_expected_target()
            phase = "creation"
            backup_name, backup_fd = self._create_unique_backup()
            self._backup_name = backup_name
            self._artifact_fd = backup_fd
            self._track_fd(backup_fd)
            backup_stat = os.fstat(backup_fd)
            self._validate_regular_single_link(backup_stat)
            self._artifact_identity = _ArtifactIdentity.from_stat(backup_stat)

            phase = "permission"
            os.fchmod(backup_fd, _BACKUP_MODE)
            phase = "write"
            _write_all(backup_fd, self._content)
            final_stat = os.fstat(backup_fd)
            self._validate_owned_stat(final_stat)
            if final_stat.st_size != len(self._content):
                raise OSError(errno.EIO, "incomplete backup")
            phase = "file sync"
            os.fsync(backup_fd)
            self._close_artifact_fd()
            backup_fd = None
            phase = "directory sync"
            self._sync_parent()
            phase = "target revalidation"
            self._validate_expected_target()
        except BaseException as exc:
            identity_error: BaseException | None = None
            try:
                self._ensure_artifact_identity()
            except BaseException as caught:
                identity_error = caught
            close_error: BaseException | None = None
            if identity_error is None and self._artifact_fd is not None:
                try:
                    self._close_artifact_fd()
                except BaseException as caught:
                    close_error = caught
            if identity_error is not None:
                self._state = CommentsBackupState.CLEANUP_REQUIRED
                self._cleanup_action = _CleanupAction.ABORT
                cleanup_error = identity_error
            else:
                cleanup_error = self._cleanup_failed_prepare()
            if not isinstance(exc, Exception):
                raise
            if cleanup_error is not None and not isinstance(
                cleanup_error,
                Exception,
            ):
                raise cleanup_error
            if close_error is not None and not isinstance(close_error, Exception):
                raise close_error
            effective_error = cleanup_error or close_error or exc
            raise CommentsBackupError(
                self._config_file,
                f"comments backup {phase} failed "
                f"({_safe_exception_type(effective_error)})",
            ) from None
        self._state = CommentsBackupState.PREPARED

    def publish(self) -> CommentsBackupPublishResult:
        """Verify the backup and rotate old backups after runtime publication."""
        self._require_state(CommentsBackupState.PREPARED, action="publish")
        self._cleanup_action = _CleanupAction.ROTATE
        self._state = CommentsBackupState.CLEANUP_REQUIRED
        try:
            current_is_owned = self._current_backup_is_owned()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return self._publish_result()
        if not current_is_owned:
            self._published_name = None
            self._backup_name = None
            self._artifact_identity = None
            return self._publish_result()
        self._published_name = self._backup_name
        if self._try_rotation_cleanup():
            if self._try_finish_published():
                self._state = CommentsBackupState.PUBLISHED
                self._cleanup_action = None
        return self._publish_result()

    def abort(self) -> None:
        """Remove only the exact owned backup before runtime publication."""
        if self._state is CommentsBackupState.ABORTED:
            return
        if self._state is CommentsBackupState.FAILED:
            self._state = CommentsBackupState.CLEANUP_REQUIRED
            self._cleanup_action = _CleanupAction.ABORT
            self._finish_abort()
            return
        if self._state is CommentsBackupState.READY:
            self._state = CommentsBackupState.CLEANUP_REQUIRED
            self._cleanup_action = _CleanupAction.ABORT
            self._finish_abort()
            return
        if self._state is CommentsBackupState.CLEANUP_REQUIRED:
            if self._cleanup_action is not _CleanupAction.ABORT:
                self._raise_state_error("abort")
        elif self._state is not CommentsBackupState.PREPARED:
            self._raise_state_error("abort")

        self._cleanup_action = _CleanupAction.ABORT
        self._state = CommentsBackupState.CLEANUP_REQUIRED
        try:
            self._complete_abort_cleanup()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise CommentsBackupError(
                self._config_file,
                f"comments backup abort failed ({_safe_exception_type(exc)})",
            ) from None
        self._finish_abort()

    def retry_cleanup(self) -> CommentsBackupPublishResult:
        """Retry the one explicitly owned abort or rotation cleanup action."""
        if self._state in {
            CommentsBackupState.PUBLISHED,
            CommentsBackupState.ABORTED,
        }:
            return self._publish_result()
        self._require_state(
            CommentsBackupState.CLEANUP_REQUIRED,
            action="retry cleanup",
        )
        if self._cleanup_action is _CleanupAction.ABORT:
            self.abort()
            return self._publish_result()
        if self._cleanup_action is not _CleanupAction.ROTATE:
            self._raise_state_error("retry cleanup")
        if self._published_name is None and not self._recreate_published_backup():
            return self._publish_result()
        if self._try_rotation_cleanup():
            if self._try_finish_published():
                self._state = CommentsBackupState.PUBLISHED
                self._cleanup_action = None
        return self._publish_result()

    def _finish_abort(self) -> None:
        try:
            self._drain_pending_fds()
            self._close_parent()
        except BaseException as exc:
            self._state = CommentsBackupState.CLEANUP_REQUIRED
            self._cleanup_action = _CleanupAction.ABORT
            if not isinstance(exc, Exception):
                raise
            raise CommentsBackupError(
                self._config_file,
                f"comments backup abort failed ({_safe_exception_type(exc)})",
            ) from None
        self._state = CommentsBackupState.ABORTED
        self._cleanup_action = None

    def _try_finish_published(self) -> bool:
        try:
            self._drain_pending_fds()
            self._close_parent()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return False
        return True

    def _create_unique_backup(self) -> tuple[str, int]:
        parent_fd = self._require_parent_fd()
        for _ in range(_MAX_CREATE_ATTEMPTS):
            backup_name = f"{self._path.name}.bak.{_next_timestamp()}"
            try:
                return backup_name, os.open(
                    backup_name,
                    _BACKUP_OPEN_FLAGS,
                    _BACKUP_MODE,
                    dir_fd=parent_fd,
                )
            except FileExistsError:
                continue
        raise OSError(errno.EEXIST, "backup namespace exhausted")

    def _cleanup_failed_prepare(self) -> BaseException | None:
        if self._backup_name is None or self._artifact_identity is None:
            try:
                self._drain_pending_fds()
                self._close_parent()
            except BaseException as exc:
                self._state = CommentsBackupState.CLEANUP_REQUIRED
                self._cleanup_action = _CleanupAction.ABORT
                return exc
            self._state = CommentsBackupState.FAILED
            return None
        self._cleanup_action = _CleanupAction.ABORT
        self._state = CommentsBackupState.CLEANUP_REQUIRED
        try:
            self._complete_abort_cleanup()
            self._close_parent()
        except BaseException as exc:
            return exc
        self._state = CommentsBackupState.FAILED
        self._cleanup_action = None
        return None

    def _complete_abort_cleanup(self) -> None:
        self._ensure_artifact_identity()
        self._close_artifact_fd()
        self._drain_pending_fds()
        self._resume_path_cleanup()
        removed = self._remove_owned_backup()
        if removed:
            self._rotation_needs_sync = True
        if self._rotation_needs_sync:
            self._sync_parent()
            self._rotation_needs_sync = False

    def _try_rotation_cleanup(self) -> bool:
        try:
            self._rotate_backups()
            return True
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            return False

    def _recreate_published_backup(self) -> bool:
        """Retry a lost post-publish backup under a new exclusive basename."""
        backup_fd: int | None = None
        try:
            self._validate_parent_identity()
            backup_name, backup_fd = self._create_unique_backup()
            self._backup_name = backup_name
            self._artifact_fd = backup_fd
            self._track_fd(backup_fd)
            backup_stat = os.fstat(backup_fd)
            self._validate_regular_single_link(backup_stat)
            self._artifact_identity = _ArtifactIdentity.from_stat(backup_stat)
            os.fchmod(backup_fd, _BACKUP_MODE)
            _write_all(backup_fd, self._content)
            final_stat = os.fstat(backup_fd)
            self._validate_owned_stat(final_stat)
            if final_stat.st_size != len(self._content):
                raise OSError(errno.EIO, "incomplete backup")
            os.fsync(backup_fd)
            self._close_artifact_fd()
            backup_fd = None
            self._sync_parent()
            if not self._current_backup_is_owned():
                return False
            self._published_name = self._backup_name
            return True
        except BaseException as exc:
            identity_error: BaseException | None = None
            try:
                self._ensure_artifact_identity()
            except BaseException as caught:
                identity_error = caught
            close_error: BaseException | None = None
            if identity_error is None and self._artifact_fd is not None:
                try:
                    self._close_artifact_fd()
                except BaseException as caught:
                    close_error = caught
            cleanup_error: BaseException | None = None
            if identity_error is None:
                try:
                    self._close_artifact_fd()
                    self._drain_pending_fds()
                    if self._remove_owned_backup():
                        self._rotation_needs_sync = True
                    if self._rotation_needs_sync:
                        self._sync_parent()
                        self._rotation_needs_sync = False
                except BaseException as caught:
                    cleanup_error = caught
            else:
                cleanup_error = identity_error
            if not isinstance(exc, Exception):
                raise
            if cleanup_error is not None and not isinstance(
                cleanup_error,
                Exception,
            ):
                raise cleanup_error
            if close_error is not None and not isinstance(close_error, Exception):
                raise close_error
            return False

    def _rotate_backups(self) -> None:
        self._drain_pending_fds()
        self._resume_path_cleanup()
        self._validate_parent_identity()
        pattern = re.compile(
            rf"{re.escape(self._path.name)}\.bak\.({_TIMESTAMP_PATTERN})\Z"
        )
        verified = self._verified_rotation_entries(pattern)

        verified.sort(key=lambda item: item[1])
        excess = len(verified) - _MAX_BACKUPS
        removed_count = 0
        if excess > 0:
            rotation_candidates = (
                item for item in verified if item[0] != self._published_name
            )
            for name, _, identity in rotation_candidates:
                if self._detach_owned_path(name, identity):
                    removed_count += 1
                    if removed_count == excess:
                        break
            if removed_count != excess:
                raise OSError(errno.ESTALE, "backup retention changed")
        if self._rotation_needs_sync:
            self._sync_parent()
            self._rotation_needs_sync = False

        final_entries = self._verified_rotation_entries(pattern)
        if len(final_entries) > _MAX_BACKUPS:
            raise OSError(errno.ESTALE, "backup retention is incomplete")
        if self._published_name is not None and not self._current_backup_is_owned():
            self._published_name = None
            self._backup_name = None
            self._artifact_identity = None
            raise OSError(errno.ESTALE, "published backup changed")

    def _verified_rotation_entries(
        self,
        pattern: re.Pattern[str],
    ) -> list[tuple[str, str, _ArtifactIdentity]]:
        parent_fd = self._require_parent_fd()
        verified: list[tuple[str, str, _ArtifactIdentity]] = []
        for name in os.listdir(parent_fd):
            match = pattern.fullmatch(name)
            if match is None:
                continue
            try:
                source_stat = os.stat(
                    name,
                    dir_fd=parent_fd,
                    follow_symlinks=False,
                )
            except FileNotFoundError:
                continue
            if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
                continue
            verified.append(
                (name, match.group(1), _ArtifactIdentity.from_stat(source_stat))
            )
        return verified

    def _validate_expected_target(self) -> None:
        self._validate_parent_identity()
        parent_fd = self._require_parent_fd()
        target_fd: int | None = None
        try:
            target_fd = os.open(
                self._path.name,
                _VERIFY_OPEN_FLAGS,
                dir_fd=parent_fd,
            )
            self._track_fd(target_fd)
            before = os.fstat(target_fd)
            if not _metadata_matches_stat(self._expected_metadata, before):
                raise OSError(errno.ESTALE, "config target changed")
            content = _read_all(target_fd)
            after = os.fstat(target_fd)
            if (
                _stable_file_identity(before) != _stable_file_identity(after)
                or not _metadata_matches_stat(self._expected_metadata, after)
                or content != self._content
            ):
                raise OSError(errno.ESTALE, "config target changed")
            current_stat = os.stat(
                self._path.name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
            if _stable_file_identity(current_stat) != _stable_file_identity(after):
                raise OSError(errno.ESTALE, "config target changed")
        finally:
            if target_fd is not None:
                self._close_tracked_fd(target_fd)

    def _current_backup_is_owned(self) -> bool:
        if self._backup_name is None or self._artifact_identity is None:
            return False
        self._validate_parent_identity()
        verify_fd: int | None = None
        try:
            verify_fd = os.open(
                self._backup_name,
                _VERIFY_OPEN_FLAGS,
                dir_fd=self._require_parent_fd(),
            )
            self._track_fd(verify_fd)
            before = os.fstat(verify_fd)
            if not self._stat_is_owned(before, self._artifact_identity):
                return False
            content = _read_all(verify_fd)
            after = os.fstat(verify_fd)
            if not self._stat_is_owned(after, self._artifact_identity):
                return False
            if _stable_file_identity(before) != _stable_file_identity(after):
                return False
            if (
                stat.S_IMODE(after.st_mode) != _BACKUP_MODE
                or after.st_size != len(self._content)
                or content != self._content
            ):
                return False
            current_stat = os.stat(
                self._backup_name,
                dir_fd=self._require_parent_fd(),
                follow_symlinks=False,
            )
            return (
                self._stat_is_owned(current_stat, self._artifact_identity)
                and _stable_file_identity(current_stat)
                == _stable_file_identity(after)
            )
        except OSError as exc:
            if exc.errno in {errno.ENOENT, errno.ELOOP}:
                return False
            raise
        finally:
            if verify_fd is not None:
                self._close_tracked_fd(verify_fd)

    def _remove_owned_backup(self) -> bool:
        if self._backup_name is None or self._artifact_identity is None:
            return False
        name = self._backup_name
        identity = self._artifact_identity
        removed = self._detach_owned_path(name, identity)
        self._backup_name = None
        self._artifact_identity = None
        return removed

    def _resume_path_cleanup(self) -> None:
        if self._moved_name is not None:
            self._resolve_moved_path()

    def _detach_owned_path(
        self,
        name: str,
        identity: _ArtifactIdentity,
    ) -> bool:
        """Move a public name aside before deleting only its expected inode."""
        self._resume_path_cleanup()
        self._validate_parent_identity()
        parent_fd = self._require_parent_fd()
        verify_fd: int | None = None
        try:
            verify_fd = os.open(name, _VERIFY_OPEN_FLAGS, dir_fd=parent_fd)
            self._track_fd(verify_fd)
            opened_stat = os.fstat(verify_fd)
            if not self._stat_is_owned(opened_stat, identity):
                return False
            current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not self._stat_is_owned(current_stat, identity):
                return False

            for _ in range(_MAX_CREATE_ATTEMPTS):
                moved_name = (
                    f"{_PRIVATE_CLEANUP_PREFIX}{secrets.token_hex(16)}"
                )
                try:
                    _renameat2(
                        name,
                        moved_name,
                        src_dir_fd=parent_fd,
                        dst_dir_fd=parent_fd,
                        flags=_RENAME_NOREPLACE,
                    )
                except OSError as exc:
                    if exc.errno == errno.EEXIST:
                        continue
                    if exc.errno == errno.ENOENT:
                        return False
                    raise
                self._moved_name = moved_name
                self._moved_target = name
                self._moved_expected_identity = identity
                self._rotation_needs_sync = True
                return self._resolve_moved_path()
            raise OSError(errno.EEXIST, "private cleanup namespace exhausted")
        except FileNotFoundError:
            return False
        finally:
            if verify_fd is not None:
                self._close_tracked_fd(verify_fd)

    def _resolve_moved_path(self) -> bool:
        moved_name = self._moved_name
        target_name = self._moved_target
        expected_identity = self._moved_expected_identity
        if (
            moved_name is None
            or target_name is None
            or expected_identity is None
        ):
            raise OSError(errno.EINVAL, "moved cleanup ownership is incomplete")

        self._validate_parent_identity()
        parent_fd = self._require_parent_fd()
        try:
            moved_stat = os.stat(
                moved_name,
                dir_fd=parent_fd,
                follow_symlinks=False,
            )
        except FileNotFoundError:
            raise OSError(errno.ESTALE, "moved cleanup path disappeared") from None

        if self._stat_is_owned(moved_stat, expected_identity):
            self._unlink_private_owned(moved_name, expected_identity)
            self._clear_moved_path()
            return True

        foreign_identity = _PathIdentity.from_stat(moved_stat)
        _renameat2(
            moved_name,
            target_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=_RENAME_NOREPLACE,
        )
        self._rotation_needs_sync = True
        self._clear_moved_path()
        restored_stat = os.stat(
            target_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if not _path_matches(restored_stat, foreign_identity):
            raise OSError(errno.ESTALE, "foreign cleanup path changed")
        return False

    def _unlink_private_owned(
        self,
        name: str,
        identity: _ArtifactIdentity,
    ) -> None:
        parent_fd = self._require_parent_fd()
        verify_fd: int | None = None
        try:
            verify_fd = os.open(name, _VERIFY_OPEN_FLAGS, dir_fd=parent_fd)
            self._track_fd(verify_fd)
            opened_stat = os.fstat(verify_fd)
            if not self._stat_is_owned(opened_stat, identity):
                raise OSError(errno.ESTALE, "private cleanup identity changed")
            current_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
            if not self._stat_is_owned(current_stat, identity):
                raise OSError(errno.ESTALE, "private cleanup path changed")
            _unlinkat(parent_fd, name)
            self._rotation_needs_sync = True
            self._clear_moved_path()
        finally:
            if verify_fd is not None:
                self._close_tracked_fd(verify_fd)

    def _clear_moved_path(self) -> None:
        self._moved_name = None
        self._moved_target = None
        self._moved_expected_identity = None

    def _validate_owned_stat(self, source_stat: os.stat_result) -> None:
        identity = self._artifact_identity
        if identity is None or not self._stat_is_owned(source_stat, identity):
            raise OSError(errno.ESTALE, "backup identity changed")

    @staticmethod
    def _validate_regular_single_link(source_stat: os.stat_result) -> None:
        if not stat.S_ISREG(source_stat.st_mode) or source_stat.st_nlink != 1:
            raise OSError(errno.EINVAL, "backup is not an owned regular file")

    @classmethod
    def _stat_is_owned(
        cls,
        source_stat: os.stat_result,
        identity: _ArtifactIdentity,
    ) -> bool:
        try:
            cls._validate_regular_single_link(source_stat)
        except OSError:
            return False
        return (
            source_stat.st_dev == identity.device
            and source_stat.st_ino == identity.inode
        )

    def _validate_parent_identity(self) -> None:
        parent_fd = self._require_parent_fd()
        held_stat = os.fstat(parent_fd)
        if (
            not stat.S_ISDIR(held_stat.st_mode)
            or (held_stat.st_dev, held_stat.st_ino) != self._parent_identity
        ):
            raise OSError(errno.ESTALE, "held parent identity changed")
        reopened_fd = _open_parent_directory(self._path)
        self._track_fd(reopened_fd)
        try:
            reopened_stat = os.fstat(reopened_fd)
            if (
                not stat.S_ISDIR(reopened_stat.st_mode)
                or (reopened_stat.st_dev, reopened_stat.st_ino)
                != self._parent_identity
            ):
                raise OSError(errno.ESTALE, "parent identity changed")
        finally:
            self._close_tracked_fd(reopened_fd)

    def _sync_parent(self) -> None:
        self._validate_parent_identity()
        os.fsync(self._require_parent_fd())
        self._validate_parent_identity()

    def _publish_result(self) -> CommentsBackupPublishResult:
        return CommentsBackupPublishResult(
            basename=self._published_name,
            cleanup_pending=self.cleanup_pending,
        )

    def _require_state(
        self,
        *allowed: CommentsBackupState,
        action: str,
    ) -> None:
        if self._state not in allowed:
            self._raise_state_error(action)

    def _raise_state_error(self, action: str) -> None:
        raise CommentsBackupStateError(
            self._config_file,
            action=action,
            state=self._state,
        )

    def _require_parent_fd(self) -> int:
        if self._parent_fd is None:
            raise OSError(errno.EBADF, "backup parent is closed")
        return self._parent_fd

    def _track_fd(self, descriptor: int) -> None:
        if descriptor not in self._pending_fds:
            self._pending_fds.append(descriptor)

    def _ensure_artifact_identity(self) -> None:
        artifact_fd = self._artifact_fd
        if artifact_fd is None or self._artifact_identity is not None:
            return
        artifact_stat = os.fstat(artifact_fd)
        self._validate_regular_single_link(artifact_stat)
        self._artifact_identity = _ArtifactIdentity.from_stat(artifact_stat)

    def _close_artifact_fd(self) -> None:
        artifact_fd = self._artifact_fd
        if artifact_fd is None:
            return
        try:
            self._close_tracked_fd(artifact_fd)
        except BaseException:
            if artifact_fd not in self._pending_fds:
                self._artifact_fd = None
            raise
        self._artifact_fd = None

    def _close_tracked_fd(self, descriptor: int) -> None:
        try:
            os.close(descriptor)
        except BaseException:
            if not _descriptor_is_open(descriptor):
                self._pending_fds.remove(descriptor)
            raise
        self._pending_fds.remove(descriptor)

    def _drain_pending_fds(self) -> None:
        for descriptor in tuple(self._pending_fds):
            if descriptor == self._artifact_fd:
                continue
            self._close_tracked_fd(descriptor)

    def _close_parent(self) -> None:
        parent_fd = self._parent_fd
        if parent_fd is None:
            return
        try:
            os.close(parent_fd)
        except BaseException:
            try:
                os.fstat(parent_fd)
            except OSError as probe_error:
                if probe_error.errno == errno.EBADF:
                    self._parent_fd = None
            raise
        self._parent_fd = None


def _json5_has_comments(content: bytes) -> bool:
    text = content.decode("utf-8-sig")
    quote: str | None = None
    escaped = False
    for index, character in enumerate(text):
        if quote is not None:
            if escaped:
                escaped = False
            elif character == "\\":
                escaped = True
            elif character == quote:
                quote = None
            continue
        if character in {'"', "'"}:
            quote = character
            continue
        if character == "/" and index + 1 < len(text):
            if text[index + 1] in {"/", "*"}:
                return True
    return False


def _now_utc() -> datetime:
    return datetime.now(UTC)


def _next_timestamp() -> str:
    global _last_timestamp
    timestamp = _now_utc()
    if timestamp.tzinfo is None:
        raise ValueError("backup clock must be timezone-aware")
    timestamp = timestamp.astimezone(UTC)
    with _timestamp_lock:
        if _last_timestamp is not None and timestamp <= _last_timestamp:
            timestamp = _last_timestamp + timedelta(microseconds=1)
        _last_timestamp = timestamp
    return timestamp.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _write_all(destination_fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(destination_fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "backup write made no progress")
        remaining = remaining[written:]


def _read_all(source_fd: int) -> bytes:
    chunks: list[bytes] = []
    while chunk := os.read(source_fd, 1024 * 1024):
        chunks.append(chunk)
    return b"".join(chunks)


def _canonicalize_path(source_path: Path) -> Path:
    raw_path = os.fspath(source_path)
    if not isinstance(raw_path, str):
        raise TypeError("config source must be a text path")
    normalized_path = os.path.normpath(os.path.abspath(raw_path))
    return Path(f"/{normalized_path.lstrip('/')}")


def _open_parent_directory(path: Path) -> int:
    current_fd = os.open("/", _DIRECTORY_OPEN_FLAGS)
    try:
        for component in path.parent.parts[1:]:
            next_fd = os.open(component, _DIRECTORY_OPEN_FLAGS, dir_fd=current_fd)
            os.close(current_fd)
            current_fd = next_fd
        return current_fd
    except BaseException:
        os.close(current_fd)
        raise


def _path_matches(
    source_stat: os.stat_result,
    identity: _PathIdentity,
) -> bool:
    return (
        source_stat.st_dev == identity.device
        and source_stat.st_ino == identity.inode
        and stat.S_IFMT(source_stat.st_mode) == identity.file_type
    )


def _stable_file_identity(source_stat: os.stat_result) -> tuple[int, ...]:
    return (
        source_stat.st_mode,
        source_stat.st_uid,
        source_stat.st_gid,
        source_stat.st_dev,
        source_stat.st_ino,
        source_stat.st_size,
        source_stat.st_mtime_ns,
        source_stat.st_ctime_ns,
        source_stat.st_nlink,
    )


def _metadata_matches_stat(
    metadata: ConfigFileMetadata,
    source_stat: os.stat_result,
) -> bool:
    return (
        metadata.mode == source_stat.st_mode
        and metadata.uid == source_stat.st_uid
        and metadata.gid == source_stat.st_gid
        and metadata.device == source_stat.st_dev
        and metadata.inode == source_stat.st_ino
        and metadata.size == source_stat.st_size
        and metadata.mtime_ns == source_stat.st_mtime_ns
        and metadata.ctime_ns == source_stat.st_ctime_ns
        and metadata.link_count == source_stat.st_nlink
    )


def _descriptor_is_open(descriptor: int) -> bool:
    try:
        os.fstat(descriptor)
    except OSError as exc:
        if exc.errno == errno.EBADF:
            return False
        raise
    return True


def _renameat2(
    source: str,
    target: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
    flags: int,
) -> None:
    if not sys.platform.startswith("linux"):
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    result = renameat2(
        src_dir_fd,
        os.fsencode(source),
        dst_dir_fd,
        os.fsencode(target),
        flags,
    )
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _unlinkat(parent_fd: int, name: str) -> None:
    libc = ctypes.CDLL(None, use_errno=True)
    unlinkat = getattr(libc, "unlinkat", None)
    if unlinkat is None:
        raise OSError(errno.ENOSYS, "unlinkat is unavailable")
    unlinkat.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int)
    unlinkat.restype = ctypes.c_int
    result = unlinkat(parent_fd, os.fsencode(name), 0)
    if result != 0:
        error_number = ctypes.get_errno()
        raise OSError(error_number, os.strerror(error_number))


def _safe_exception_type(exc: BaseException) -> str:
    name = type(exc).__name__
    if (
        not name
        or len(name) > 64
        or not name.isascii()
        or not all(character.isalnum() or character == "_" for character in name)
    ):
        return "BaseException"
    return name
