"""Crash-safe atomic transactions for gateway configuration files."""

from __future__ import annotations

import ctypes
import errno
import hashlib
import json
import logging
import os
import secrets
import stat
import sys
from dataclasses import dataclass, replace
from enum import StrEnum
from pathlib import Path
from typing import Final, Mapping, Self

from .config_sources import (
    ConfigDocument,
    ConfigFile,
    _FILE_OPEN_FLAGS,
    _canonicalize_path,
    _capture_document_at,
    _capture_document_name_at,
    _open_parent_directory,
    _read_all,
    _stat_identity,
)


__all__ = (
    "AtomicConfigFileTransaction",
    "AtomicConfigTransactionConflictError",
    "AtomicConfigTransactionError",
    "AtomicConfigTransactionIntegrityError",
    "AtomicConfigTransactionState",
    "AtomicConfigTransactionStateError",
)


logger = logging.getLogger(__name__)

_TRANSACTION_TOKEN_ATTEMPTS: Final[int] = 32
_TRANSACTION_PREFIX: Final[str] = ".llmgateway-config-txn-"
_TEMP_FILE_FLAGS: Final[int] = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
_RENAME_NOREPLACE: Final[int] = 1
_RENAME_EXCHANGE: Final[int] = 2
_JOURNAL_VERSION: Final[int] = 1
_JOURNAL_MAX_BYTES: Final[int] = 64 * 1024
# Owner-execute plus any group/other write or execute bit is insecure for a
# gateway config file; group/other *read* bits are allowed (item 2 fix).
_INSECURE_NEW_FILE_MODE_BITS: Final[int] = (
    stat.S_IXUSR | stat.S_IWGRP | stat.S_IXGRP | stat.S_IWOTH | stat.S_IXOTH
)


class AtomicConfigTransactionError(RuntimeError):
    """Credential-safe failure of one atomic config-file transaction."""

    def __init__(self, config_file: ConfigFile, reason: str) -> None:
        self.config_file = config_file
        self.reason = reason
        super().__init__(f"{config_file.value}: {reason}")


class AtomicConfigTransactionConflictError(AtomicConfigTransactionError):
    """The source revision no longer matches the transaction input."""


class AtomicConfigTransactionIntegrityError(AtomicConfigTransactionError):
    """Transaction recovery cannot prove a safe canonical target."""


class AtomicConfigTransactionStateError(AtomicConfigTransactionError):
    """The requested transaction state transition is invalid."""


class AtomicConfigTransactionState(StrEnum):
    """Externally observable lifecycle of an atomic file transaction."""

    BEGUN = "begun"
    PREPARED = "prepared"
    REPLACED = "replaced"
    COMMITTED = "committed"
    ROLLED_BACK = "rolled_back"
    FINALIZED = "finalized"
    ABORTED = "aborted"
    RECOVERY_REQUIRED = "recovery_required"


@dataclass(frozen=True, slots=True)
class _TransactionArtifact:
    """Recorded identity of one fully prepared same-directory artifact."""

    name: str
    device: int
    inode: int
    size: int
    mode: int
    uid: int
    gid: int
    digest: str

    @classmethod
    def from_stat(
        cls,
        name: str,
        source_stat: os.stat_result,
        content: bytes,
    ) -> Self:
        return cls(
            name=name,
            device=source_stat.st_dev,
            inode=source_stat.st_ino,
            size=source_stat.st_size,
            mode=stat.S_IMODE(source_stat.st_mode),
            uid=source_stat.st_uid,
            gid=source_stat.st_gid,
            digest=hashlib.sha256(content).hexdigest(),
        )


@dataclass(frozen=True, slots=True)
class _TransactionJournal:
    config_file: ConfigFile
    target_name: str
    expected_exists: bool
    expected_artifact: _TransactionArtifact | None
    candidate_artifact: _TransactionArtifact
    rollback_artifact: _TransactionArtifact | None

    def to_bytes(self) -> bytes:
        payload = {
            "candidate": _artifact_to_journal_value(self.candidate_artifact),
            "config_file": self.config_file.value,
            "expected": _artifact_to_journal_value(self.expected_artifact),
            "expected_exists": self.expected_exists,
            "rollback": _artifact_to_journal_value(self.rollback_artifact),
            "target": self.target_name,
            "version": _JOURNAL_VERSION,
        }
        return json.dumps(
            payload,
            ensure_ascii=True,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("ascii")


class AtomicConfigFileTransaction:
    """Prepare, atomically replace, and optionally restore one config file."""

    __slots__ = (
        "_abort_needs_sync",
        "_candidate_artifact",
        "_candidate_content",
        "_candidate_name",
        "_displaced_artifact",
        "_displaced_name",
        "_expected_document",
        "_finalize_needs_sync",
        "_journal_artifact",
        "_journal_name",
        "_new_file_mode",
        "_parent_fd",
        "_parent_identity",
        "_rollback_applied",
        "_rollback_artifact",
        "_rollback_name",
        "_state",
    )

    def __init__(
        self,
        expected_document: ConfigDocument,
        candidate_content: bytes,
        *,
        new_file_mode: int,
        parent_fd: int,
        parent_identity: tuple[int, int],
    ) -> None:
        self._expected_document = expected_document
        self._candidate_content = candidate_content
        self._new_file_mode = new_file_mode
        self._parent_fd: int | None = parent_fd
        self._parent_identity = parent_identity
        self._state = AtomicConfigTransactionState.BEGUN
        self._candidate_name: str | None = None
        self._candidate_artifact: _TransactionArtifact | None = None
        self._displaced_name: str | None = None
        self._displaced_artifact: _TransactionArtifact | None = None
        self._rollback_name: str | None = None
        self._rollback_artifact: _TransactionArtifact | None = None
        self._journal_name: str | None = None
        self._journal_artifact: _TransactionArtifact | None = None
        self._rollback_applied = False
        self._abort_needs_sync = False
        self._finalize_needs_sync = False

    @classmethod
    def begin(
        cls,
        expected_document: ConfigDocument,
        candidate_content: bytes | bytearray | memoryview,
        *,
        new_file_mode: int = 0o600,
    ) -> Self:
        """Bind one exact source revision and open its symlink-safe parent."""
        if not isinstance(expected_document, ConfigDocument):
            raise TypeError("expected_document must be a ConfigDocument")
        if not isinstance(candidate_content, bytes | bytearray | memoryview):
            raise TypeError("candidate_content must be bytes-like")
        if (
            not isinstance(new_file_mode, int)
            or isinstance(new_file_mode, bool)
            or not 0 <= new_file_mode <= 0o777
            or new_file_mode & _INSECURE_NEW_FILE_MODE_BITS
            or not new_file_mode & stat.S_IRUSR
        ):
            raise ValueError("new_file_mode must be a secure mode")
        if expected_document.exists and expected_document.metadata is None:
            raise AtomicConfigTransactionError(
                expected_document.config_file,
                "expected source metadata is unavailable",
            )
        if _canonicalize_path(expected_document.path) != expected_document.path:
            raise AtomicConfigTransactionError(
                expected_document.config_file,
                "expected source path is invalid",
            )
        _validate_transaction_target_name(expected_document)

        parent_fd: int | None = None
        try:
            parent_fd = _open_parent_directory(expected_document.path)
            parent_stat = os.fstat(parent_fd)
            if not stat.S_ISDIR(parent_stat.st_mode):
                raise OSError(errno.ENOTDIR, "not a directory")
            return cls(
                expected_document,
                bytes(candidate_content),
                new_file_mode=new_file_mode,
                parent_fd=parent_fd,
                parent_identity=(parent_stat.st_dev, parent_stat.st_ino),
            )
        except BaseException as exc:
            if parent_fd is not None:
                os.close(parent_fd)
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionError):
                raise
            raise AtomicConfigTransactionError(
                expected_document.config_file,
                f"transaction begin failed ({_safe_exception_type(exc)})",
            ) from None

    @property
    def config_file(self) -> ConfigFile:
        return self._expected_document.config_file

    @property
    def expected_document(self) -> ConfigDocument:
        return self._expected_document

    @property
    def state(self) -> AtomicConfigTransactionState:
        return self._state

    def prepare(self) -> None:
        """Durably stage candidate bytes and an independent rollback copy."""
        self._require_state(AtomicConfigTransactionState.BEGUN, action="prepare")
        phase = "candidate preparation"
        try:
            mode, uid, gid = self._target_metadata()
            self._candidate_name = self._write_artifact(
                "candidate",
                self._candidate_content,
                mode=mode,
                uid=uid,
                gid=gid,
            )
            if self._expected_document.exists:
                phase = "rollback preparation"
                assert self._expected_document.content is not None
                self._rollback_name = self._write_artifact(
                    "rollback",
                    self._expected_document.content,
                    mode=mode,
                    uid=uid,
                    gid=gid,
                )
            phase = "journal preparation"
            self._write_journal()
            phase = "prepared directory sync"
            os.fsync(self._require_parent_fd())
        except BaseException as exc:
            cleanup_error = self._abort_internal()
            if not isinstance(exc, Exception):
                raise
            if cleanup_error is not None:
                raise AtomicConfigTransactionError(
                    self.config_file,
                    f"prepare cleanup failed ({_safe_exception_type(cleanup_error)})",
                ) from None
            raise AtomicConfigTransactionError(
                self.config_file,
                f"{phase} failed ({_safe_exception_type(exc)})",
            ) from None
        self._state = AtomicConfigTransactionState.PREPARED

    def commit(self) -> None:
        """CAS the expected revision, atomically replace it, and sync the parent."""
        self._require_state(
            AtomicConfigTransactionState.PREPARED,
            AtomicConfigTransactionState.REPLACED,
            action="commit",
        )
        if self._state is AtomicConfigTransactionState.PREPARED:
            phase = "commit validation"
            try:
                self._validate_parent_identity()
                current_document = _capture_document_at(
                    self.config_file,
                    self._expected_document.path,
                    self._require_parent_fd(),
                    required=False,
                )
                if current_document != self._expected_document:
                    raise AtomicConfigTransactionConflictError(
                        self.config_file,
                        "config source changed before commit",
                    )
                phase = "candidate validation"
                candidate_name, candidate_fd = self._open_validated_artifact(
                    self._candidate_name,
                    self._candidate_artifact,
                    self._candidate_content,
                    kind="candidate",
                )
                replace_error: BaseException | None = None
                try:
                    if self._expected_document.exists:
                        phase = "atomic exchange"
                        _renameat2(
                            candidate_name,
                            self._expected_document.path.name,
                            src_dir_fd=self._require_parent_fd(),
                            dst_dir_fd=self._require_parent_fd(),
                            flags=_RENAME_EXCHANGE,
                        )
                        self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
                        self._candidate_name = None
                        self._displaced_name = candidate_name
                        displaced = _capture_document_name_at(
                            self.config_file,
                            self._expected_document.path,
                            self._require_parent_fd(),
                            source_name=candidate_name,
                            required=True,
                        )
                        displaced_identity = _document_as_artifact(displaced)
                        assert displaced_identity is not None
                        self._displaced_artifact = replace(
                            displaced_identity,
                            name=candidate_name,
                        )
                        expected_artifact = _document_as_artifact(
                            self._expected_document
                        )
                        if not _document_matches_artifact(
                            displaced,
                            expected_artifact,
                        ):
                            self._restore_displaced_external()
                            raise AtomicConfigTransactionConflictError(
                                self.config_file,
                                "config source changed during atomic exchange",
                            )
                    else:
                        phase = "atomic no-replace"
                        try:
                            _renameat2(
                                candidate_name,
                                self._expected_document.path.name,
                                src_dir_fd=self._require_parent_fd(),
                                dst_dir_fd=self._require_parent_fd(),
                                flags=_RENAME_NOREPLACE,
                            )
                        except FileExistsError:
                            raise AtomicConfigTransactionConflictError(
                                self.config_file,
                                "config source changed during atomic create",
                            ) from None
                        self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
                        self._candidate_name = None

                    self._validate_parent_identity()
                    self._validate_target_from_artifact(
                        self._candidate_artifact,
                        self._candidate_content,
                        kind="candidate",
                        held_fd=candidate_fd,
                    )
                    self._state = AtomicConfigTransactionState.REPLACED
                    phase = "candidate descriptor close"
                except BaseException as exc:
                    replace_error = exc
                    raise
                finally:
                    _close_fd_preserving_primary(candidate_fd, replace_error)
            except BaseException as exc:
                if self._state is AtomicConfigTransactionState.PREPARED:
                    cleanup_error = self._abort_internal()
                    if not isinstance(exc, Exception):
                        raise
                    if cleanup_error is not None:
                        raise AtomicConfigTransactionError(
                            self.config_file,
                            f"commit cleanup failed ({_safe_exception_type(cleanup_error)})",
                        ) from None
                elif not isinstance(exc, Exception):
                    raise
                if isinstance(exc, AtomicConfigTransactionError):
                    raise
                raise AtomicConfigTransactionError(
                    self.config_file,
                    f"{phase} failed ({_safe_exception_type(exc)})",
                ) from None

        try:
            self._validate_replaced_candidate()
        except BaseException as validation_error:
            self._abort_replaced_after_race(validation_error)
        try:
            os.fsync(self._require_parent_fd())
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise AtomicConfigTransactionError(
                self.config_file,
                f"committed directory sync failed ({_safe_exception_type(exc)})",
            ) from None
        try:
            self._validate_replaced_candidate()
        except BaseException as validation_error:
            self._abort_replaced_after_race(validation_error)
        try:
            self._complete_commit_cleanup()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionError):
                raise
            raise AtomicConfigTransactionError(
                self.config_file,
                f"commit cleanup failed ({_safe_exception_type(exc)})",
            ) from None
        self._state = AtomicConfigTransactionState.COMMITTED

    def rollback(self) -> None:
        """Restore the exact old file or absence; retry after a failed attempt."""
        self._require_state(
            AtomicConfigTransactionState.REPLACED,
            AtomicConfigTransactionState.COMMITTED,
            action="rollback",
        )
        retry_state = self._state
        try:
            if not self._rollback_applied:
                self._validate_parent_identity()
                self._validate_replaced_candidate()
                self._transition_journal("restore")
                if self._expected_document.exists:
                    assert self._expected_document.content is not None
                    rollback_name, rollback_fd = self._open_validated_artifact(
                        self._rollback_name,
                        self._rollback_artifact,
                        self._expected_document.content,
                        kind="rollback",
                    )
                    replace_error: BaseException | None = None
                    try:
                        _renameat2(
                            rollback_name,
                            self._expected_document.path.name,
                            src_dir_fd=self._require_parent_fd(),
                            dst_dir_fd=self._require_parent_fd(),
                            flags=_RENAME_EXCHANGE,
                        )
                        self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
                        displaced = _capture_document_name_at(
                            self.config_file,
                            self._expected_document.path,
                            self._require_parent_fd(),
                            source_name=rollback_name,
                            required=True,
                        )
                        if not _document_matches_artifact(
                            displaced,
                            self._candidate_artifact,
                        ):
                            self._restore_external_rollback_race(
                                rollback_name,
                                displaced,
                                retry_state=retry_state,
                            )
                            raise AtomicConfigTransactionConflictError(
                                self.config_file,
                                "rollback target changed during atomic exchange",
                            )
                        assert self._candidate_artifact is not None
                        self._candidate_name = rollback_name
                        self._candidate_artifact = replace(
                            self._candidate_artifact,
                            name=rollback_name,
                        )
                        self._rollback_name = None
                        self._rollback_applied = True
                        self._state = retry_state
                    except BaseException as exc:
                        replace_error = exc
                        raise
                    finally:
                        _close_fd_preserving_primary(rollback_fd, replace_error)
                else:
                    candidate_artifact = self._candidate_artifact
                    if candidate_artifact is None:
                        raise AtomicConfigTransactionStateError(
                            self.config_file,
                            "candidate identity is unavailable for rollback",
                        )
                    _renameat2(
                        self._expected_document.path.name,
                        candidate_artifact.name,
                        src_dir_fd=self._require_parent_fd(),
                        dst_dir_fd=self._require_parent_fd(),
                        flags=_RENAME_NOREPLACE,
                    )
                    self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
                    displaced = _capture_document_name_at(
                        self.config_file,
                        self._expected_document.path,
                        self._require_parent_fd(),
                        source_name=candidate_artifact.name,
                        required=True,
                    )
                    if not _document_matches_artifact(
                        displaced,
                        candidate_artifact,
                    ):
                        self._restore_external_missing_rollback(
                            displaced,
                            retry_state=retry_state,
                        )
                        raise AtomicConfigTransactionConflictError(
                            self.config_file,
                            "rollback target changed during atomic move",
                        )
                    self._candidate_name = candidate_artifact.name
                    self._rollback_applied = True
                    self._state = retry_state
            try:
                self._validate_rollback_target()
            except BaseException as validation_error:
                self._recover_rollback_after_race(validation_error)
            os.fsync(self._require_parent_fd())
            try:
                self._validate_rollback_target()
            except BaseException as validation_error:
                self._recover_rollback_after_race(validation_error)
            self._complete_rollback_cleanup()
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionError):
                raise
            raise AtomicConfigTransactionError(
                self.config_file,
                f"rollback failed ({_safe_exception_type(exc)})",
            ) from None
        self._rollback_artifact = None
        self._candidate_artifact = None
        self._finish(AtomicConfigTransactionState.ROLLED_BACK)

    def finalize(self) -> None:
        """Discard rollback data only after the external runtime commit succeeds."""
        self._require_state(AtomicConfigTransactionState.COMMITTED, action="finalize")
        if self._rollback_applied:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                "a rolled-back transaction cannot be finalized",
        )
        try:
            if self._journal_name is not None:
                self._transition_journal("finalize")
            if self._rollback_name is not None:
                assert self._rollback_artifact is not None
                _unlink_verified_artifact(
                    self.config_file,
                    self._require_parent_fd(),
                    self._rollback_artifact,
                )
                self._rollback_name = None
                self._rollback_artifact = None
                self._finalize_needs_sync = True
            if self._journal_name is not None:
                assert self._journal_artifact is not None
                _unlink_verified_artifact(
                    self.config_file,
                    self._require_parent_fd(),
                    self._journal_artifact,
                )
                self._journal_name = None
                self._journal_artifact = None
                self._finalize_needs_sync = True
            if self._finalize_needs_sync:
                os.fsync(self._require_parent_fd())
                self._finalize_needs_sync = False
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise AtomicConfigTransactionError(
                self.config_file,
                f"finalize failed ({_safe_exception_type(exc)})",
            ) from None
        self._candidate_artifact = None
        self._finish(AtomicConfigTransactionState.FINALIZED)

    def abort(self) -> None:
        """Remove staged artifacts before any target replacement."""
        self._require_state(
            AtomicConfigTransactionState.BEGUN,
            AtomicConfigTransactionState.PREPARED,
            action="abort",
        )
        cleanup_error = self._abort_internal()
        if cleanup_error is not None:
            raise AtomicConfigTransactionError(
                self.config_file,
                f"abort failed ({_safe_exception_type(cleanup_error)})",
            ) from None

    @classmethod
    def cleanup_orphans(cls, expected_document: ConfigDocument) -> int:
        """Remove owned artifacts while the caller holds exclusive update ownership."""
        if not isinstance(expected_document, ConfigDocument):
            raise TypeError("expected_document must be a ConfigDocument")
        if _canonicalize_path(expected_document.path) != expected_document.path:
            raise AtomicConfigTransactionError(
                expected_document.config_file,
                "orphan cleanup root is invalid",
            )
        _validate_transaction_target_name(expected_document)
        parent_fd: int | None = None
        try:
            parent_fd = _open_parent_directory(expected_document.path)
            prefix = _transaction_artifact_prefix(expected_document.config_file)
            names = [name for name in os.listdir(parent_fd) if name.startswith(prefix)]
            for name in names:
                if not _is_owned_transaction_artifact(expected_document.config_file, name):
                    raise AtomicConfigTransactionError(
                        expected_document.config_file,
                        "orphan artifact name is invalid",
                    )
                artifact_stat = os.stat(name, dir_fd=parent_fd, follow_symlinks=False)
                if not stat.S_ISREG(artifact_stat.st_mode) or artifact_stat.st_nlink != 1:
                    raise AtomicConfigTransactionError(
                        expected_document.config_file,
                        "orphan artifact type is unsafe",
                    )
            for name in names:
                os.unlink(name, dir_fd=parent_fd)
            # Always sync: a retry after a prior unlink-success/fsync-failure
            # must be able to establish durable cleanup even when no names remain.
            os.fsync(parent_fd)
            return len(names)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionError):
                raise
            raise AtomicConfigTransactionError(
                expected_document.config_file,
                f"orphan cleanup failed ({_safe_exception_type(exc)})",
            ) from None
        finally:
            if parent_fd is not None:
                os.close(parent_fd)

    @classmethod
    def recover_pending(
        cls,
        config_paths: Mapping[ConfigFile, Path],
    ) -> int:
        """Recover durable one-file journals before configuration is loaded."""
        recovered = 0
        for config_file, raw_path in config_paths.items():
            if not isinstance(config_file, ConfigFile):
                raise TypeError("config path keys must be ConfigFile values")
            path = _canonicalize_path(raw_path)
            if not path.name or path.name.startswith(_TRANSACTION_PREFIX):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup recovery target is invalid",
                )
            parent_fd: int | None = None
            try:
                parent_fd = _open_parent_directory(path)
                _validate_recovery_parent(config_file, path, parent_fd)
                prefix = _transaction_artifact_prefix(config_file)
                names = sorted(
                    name for name in os.listdir(parent_fd) if name.startswith(prefix)
                )
                invalid = [
                    name
                    for name in names
                    if not _is_owned_transaction_artifact(config_file, name)
                ]
                if invalid:
                    raise AtomicConfigTransactionIntegrityError(
                        config_file,
                        "startup recovery artifact name is invalid",
                    )
                journal_names = [
                    name for name in names if ".journal." in name
                ]
                if len(journal_names) > 1:
                    raise AtomicConfigTransactionIntegrityError(
                        config_file,
                        "multiple startup recovery journals are ambiguous",
                    )
                for journal_name in journal_names:
                    cls._recover_one_journal(
                        config_file,
                        path,
                        parent_fd,
                        journal_name,
                    )
                    _validate_recovery_parent(config_file, path, parent_fd)
                    recovered += 1
            except AtomicConfigTransactionError:
                raise
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    f"startup recovery failed ({_safe_exception_type(exc)})",
                ) from None
            finally:
                if parent_fd is not None:
                    os.close(parent_fd)
        return recovered

    @staticmethod
    def _recover_one_journal(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal_name: str,
    ) -> None:
        journal_stat = os.stat(
            journal_name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            not stat.S_ISREG(journal_stat.st_mode)
            or journal_stat.st_nlink != 1
            or journal_stat.st_size > _JOURNAL_MAX_BYTES
        ):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup journal type is unsafe",
            )
        journal_fd = os.open(journal_name, _FILE_OPEN_FLAGS, dir_fd=parent_fd)
        try:
            content = _read_all(journal_fd)
            after = os.fstat(journal_fd)
        finally:
            os.close(journal_fd)
        if (
            _stat_identity(journal_stat) != _stat_identity(after)
            or len(content) != after.st_size
        ):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup journal changed during capture",
            )
        journal = _parse_transaction_journal(config_file, path.name, content)
        journal_artifact = _TransactionArtifact.from_stat(
            journal_name,
            journal_stat,
            content,
        )
        if journal_name.endswith(".journal.prepared"):
            handler = AtomicConfigFileTransaction._recover_prepared_journal
        elif journal_name.endswith((".journal.commit", ".journal.finalize")):
            handler = AtomicConfigFileTransaction._recover_commit_journal
        elif journal_name.endswith((".journal.restore", ".journal.restore_move")):
            handler = AtomicConfigFileTransaction._recover_restore_journal
        elif journal_name.endswith(".journal.abort"):
            handler = AtomicConfigFileTransaction._recover_abort_journal
        else:
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup journal phase is unsupported",
            )
        handler(
            config_file,
            path,
            parent_fd,
            journal,
            journal_artifact,
        )

    @staticmethod
    def _recover_prepared_journal(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal: _TransactionJournal,
        journal_artifact: _TransactionArtifact,
    ) -> None:
        target = _capture_document_at(
            config_file,
            path,
            parent_fd,
            required=False,
        )
        candidate_path = _capture_document_name_at(
            config_file,
            path,
            parent_fd,
            source_name=journal.candidate_artifact.name,
            required=False,
        )
        target_is_expected = _document_matches_artifact(
            target,
            journal.expected_artifact,
        )
        target_is_candidate = _document_matches_artifact(
            target,
            journal.candidate_artifact,
        )
        path_is_candidate = _document_matches_artifact(
            candidate_path,
            journal.candidate_artifact,
        )
        path_is_expected = _document_matches_artifact(
            candidate_path,
            journal.expected_artifact,
        )

        if target_is_expected and path_is_candidate:
            if journal.rollback_artifact is not None:
                _validate_recorded_artifact(
                    config_file,
                    parent_fd,
                    journal.rollback_artifact,
                    kind="rollback",
                )
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "abort",
            )
            _cleanup_recovery_artifacts(
                config_file,
                path,
                parent_fd,
                (
                    journal.candidate_artifact,
                    journal.rollback_artifact,
                    journal_artifact,
                ),
            )
            return

        if target_is_candidate and (
            path_is_expected
            if journal.expected_exists
            else not candidate_path.exists
        ):
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "commit",
            )
            AtomicConfigFileTransaction._recover_commit_journal(
                config_file,
                path,
                parent_fd,
                journal,
                journal_artifact,
            )
            return

        if target_is_candidate and candidate_path.exists:
            external_artifact = _document_as_artifact(candidate_path)
            if external_artifact is None:
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup displaced target identity is unavailable",
                )
            external_artifact = replace(
                external_artifact,
                name=journal.candidate_artifact.name,
            )
            if journal.rollback_artifact is not None:
                _validate_recorded_artifact(
                    config_file,
                    parent_fd,
                    journal.rollback_artifact,
                    kind="rollback",
                )
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "restore",
            )
            _recovery_renameat2(
                config_file,
                path,
                parent_fd,
                journal.candidate_artifact.name,
                path.name,
                flags=_RENAME_EXCHANGE,
            )
            restored = _capture_document_at(
                config_file,
                path,
                parent_fd,
                required=True,
            )
            staged_candidate = _capture_document_name_at(
                config_file,
                path,
                parent_fd,
                source_name=journal.candidate_artifact.name,
                required=True,
            )
            if (
                not _document_matches_artifact(restored, external_artifact)
                or not _document_matches_artifact(
                    staged_candidate,
                    journal.candidate_artifact,
                )
            ):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup external restore is ambiguous",
                )
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "abort",
            )
            _cleanup_recovery_artifacts(
                config_file,
                path,
                parent_fd,
                (
                    journal.candidate_artifact,
                    journal.rollback_artifact,
                    journal_artifact,
                ),
            )
            return

        raise AtomicConfigTransactionIntegrityError(
            config_file,
            "startup transaction state is ambiguous",
        )

    @staticmethod
    def _recover_commit_journal(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal: _TransactionJournal,
        journal_artifact: _TransactionArtifact,
    ) -> None:
        finalizing = journal_artifact.name.endswith(".journal.finalize")
        target = _capture_document_at(
            config_file,
            path,
            parent_fd,
            required=False,
        )
        if not _document_matches_artifact(target, journal.candidate_artifact):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup committed target is ambiguous",
            )
        displaced = _capture_document_name_at(
            config_file,
            path,
            parent_fd,
            source_name=journal.candidate_artifact.name,
            required=False,
        )
        if displaced.exists:
            if finalizing or not _document_matches_artifact(
                displaced,
                journal.expected_artifact,
            ):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup committed displaced target is ambiguous",
                )
            assert journal.expected_artifact is not None
            _cleanup_recovery_artifacts(
                config_file,
                path,
                parent_fd,
                (
                    replace(
                        journal.expected_artifact,
                        name=journal.candidate_artifact.name,
                    ),
                ),
            )
        rollback_cleanup: _TransactionArtifact | None = None
        if journal.rollback_artifact is not None:
            rollback_path = _capture_document_name_at(
                config_file,
                path,
                parent_fd,
                source_name=journal.rollback_artifact.name,
                required=not finalizing,
            )
            if rollback_path.exists:
                if not _document_matches_artifact(
                    rollback_path,
                    journal.rollback_artifact,
                ):
                    raise AtomicConfigTransactionIntegrityError(
                        config_file,
                        "startup finalized rollback path is ambiguous",
                    )
                rollback_cleanup = journal.rollback_artifact
        if not finalizing:
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "finalize",
            )
        _cleanup_recovery_artifacts(
            config_file,
            path,
            parent_fd,
            (rollback_cleanup, journal_artifact),
        )

    @staticmethod
    def _recover_abort_journal(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal: _TransactionJournal,
        journal_artifact: _TransactionArtifact,
    ) -> None:
        target = _capture_document_at(config_file, path, parent_fd, required=False)
        if _document_matches_artifact(target, journal.candidate_artifact):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup aborted target is still the candidate",
            )
        candidate_cleanup = _match_recovery_artifact_path(
            config_file,
            path,
            parent_fd,
            journal.candidate_artifact.name,
            (journal.candidate_artifact, journal.expected_artifact),
        )
        rollback_cleanup = (
            _match_recovery_artifact_path(
                config_file,
                path,
                parent_fd,
                journal.rollback_artifact.name,
                (journal.rollback_artifact, journal.candidate_artifact),
            )
            if journal.rollback_artifact is not None
            else None
        )
        _cleanup_recovery_artifacts(
            config_file,
            path,
            parent_fd,
            (candidate_cleanup, rollback_cleanup, journal_artifact),
        )

    @staticmethod
    def _recover_restore_journal(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal: _TransactionJournal,
        journal_artifact: _TransactionArtifact,
    ) -> None:
        if journal.rollback_artifact is None:
            AtomicConfigFileTransaction._recover_missing_target_restore(
                config_file, path, parent_fd, journal, journal_artifact
            )
            return
        target = _capture_document_at(config_file, path, parent_fd, required=False)
        rollback_path = _capture_document_name_at(
            config_file, path, parent_fd,
            source_name=journal.rollback_artifact.name,
            required=False,
        )
        candidate_path = _capture_document_name_at(
            config_file, path, parent_fd,
            source_name=journal.candidate_artifact.name,
            required=False,
        )
        candidate_path_is_expected = _document_matches_artifact(
            candidate_path, journal.expected_artifact
        )
        target_is_candidate = _document_matches_artifact(
            target, journal.candidate_artifact
        )
        rollback_path_is_rollback = _document_matches_artifact(
            rollback_path, journal.rollback_artifact
        )

        if (
            target.exists
            and not target_is_candidate
            and not _document_matches_artifact(target, journal.expected_artifact)
            and not _document_matches_artifact(target, journal.rollback_artifact)
            and _document_matches_artifact(
                candidate_path, journal.candidate_artifact
            )
            and rollback_path_is_rollback
        ):
            journal_artifact = _transition_recorded_journal(
                config_file, path, parent_fd, journal_artifact, "abort"
            )
            AtomicConfigFileTransaction._recover_abort_journal(
                config_file, path, parent_fd, journal, journal_artifact
            )
            return

        if (
            target_is_candidate
            and rollback_path_is_rollback
            and candidate_path.exists
            and not candidate_path_is_expected
        ):
            if _document_matches_artifact(
                candidate_path, journal.rollback_artifact
            ):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup displaced target identity is ambiguous",
                )
            external_artifact = _document_as_artifact(candidate_path)
            assert external_artifact is not None
            _recovery_renameat2(
                config_file, path, parent_fd,
                journal.candidate_artifact.name,
                path.name,
                flags=_RENAME_EXCHANGE,
            )
            restored = _capture_document_at(config_file, path, parent_fd, required=True)
            staged_candidate = _capture_document_name_at(
                config_file, path, parent_fd,
                source_name=journal.candidate_artifact.name,
                required=True,
            )
            if (
                not _document_matches_artifact(restored, external_artifact)
                or not _document_matches_artifact(
                    staged_candidate,
                    journal.candidate_artifact,
                )
            ):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup displaced target restore is ambiguous",
                )
            journal_artifact = _transition_recorded_journal(
                config_file, path, parent_fd, journal_artifact, "abort"
            )
            AtomicConfigFileTransaction._recover_abort_journal(
                config_file, path, parent_fd, journal, journal_artifact
            )
            return

        if candidate_path.exists and not candidate_path_is_expected:
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup rollback state is ambiguous",
            )

        if target_is_candidate and rollback_path_is_rollback:
            _recovery_renameat2(
                config_file, path, parent_fd,
                journal.rollback_artifact.name,
                path.name,
                flags=_RENAME_EXCHANGE,
            )
            target = _capture_document_at(config_file, path, parent_fd, required=False)
            rollback_path = _capture_document_name_at(
                config_file, path, parent_fd,
                source_name=journal.rollback_artifact.name,
                required=False,
            )

        if _document_matches_artifact(target, journal.rollback_artifact):
            if not rollback_path.exists:
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup rollback source is missing",
                )
            if not _document_matches_artifact(
                rollback_path,
                journal.candidate_artifact,
            ):
                external_artifact = _document_as_artifact(rollback_path)
                assert external_artifact is not None
                _recovery_renameat2(
                    config_file, path, parent_fd,
                    journal.rollback_artifact.name,
                    path.name,
                    flags=_RENAME_EXCHANGE,
                )
                restored = _capture_document_at(
                    config_file, path, parent_fd, required=True
                )
                staged_rollback = _capture_document_name_at(
                    config_file, path, parent_fd,
                    source_name=journal.rollback_artifact.name,
                    required=True,
                )
                if (
                    not _document_matches_artifact(restored, external_artifact)
                    or not _document_matches_artifact(
                        staged_rollback,
                        journal.rollback_artifact,
                    )
                ):
                    raise AtomicConfigTransactionIntegrityError(
                        config_file,
                        "startup external rollback restore is ambiguous",
                    )
        elif _document_matches_artifact(target, journal.candidate_artifact):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup rollback state is ambiguous",
            )
        journal_artifact = _transition_recorded_journal(
            config_file, path, parent_fd, journal_artifact, "abort"
        )
        AtomicConfigFileTransaction._recover_abort_journal(
            config_file, path, parent_fd, journal, journal_artifact
        )

    @staticmethod
    def _recover_missing_target_restore(
        config_file: ConfigFile,
        path: Path,
        parent_fd: int,
        journal: _TransactionJournal,
        journal_artifact: _TransactionArtifact,
    ) -> None:
        target = _capture_document_at(
            config_file,
            path,
            parent_fd,
            required=False,
        )
        candidate_path = _capture_document_name_at(
            config_file,
            path,
            parent_fd,
            source_name=journal.candidate_artifact.name,
            required=False,
        )
        if journal_artifact.name.endswith(".journal.restore"):
            if not (
                (
                    _document_matches_artifact(
                        target,
                        journal.candidate_artifact,
                    )
                    and not candidate_path.exists
                )
                or (
                    not target.exists
                    and _document_matches_artifact(
                        candidate_path,
                        journal.candidate_artifact,
                    )
                )
            ):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup missing-target rollback state is ambiguous",
                )
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "restore_move",
            )

        if _document_matches_artifact(
            target,
            journal.candidate_artifact,
        ) and not candidate_path.exists:
            _recovery_renameat2(
                config_file,
                path,
                parent_fd,
                path.name,
                journal.candidate_artifact.name,
                flags=_RENAME_NOREPLACE,
            )
            target = _capture_document_at(
                config_file,
                path,
                parent_fd,
                required=False,
            )
            candidate_path = _capture_document_name_at(
                config_file,
                path,
                parent_fd,
                source_name=journal.candidate_artifact.name,
                required=False,
            )
        if not target.exists and _document_matches_artifact(
            candidate_path,
            journal.candidate_artifact,
        ):
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "abort",
            )
            _cleanup_recovery_artifacts(
                config_file,
                path,
                parent_fd,
                (journal.candidate_artifact, journal_artifact),
            )
            return
        if not target.exists and candidate_path.exists:
            external_artifact = _document_as_artifact(candidate_path)
            assert external_artifact is not None
            _recovery_renameat2(
                config_file,
                path,
                parent_fd,
                journal.candidate_artifact.name,
                path.name,
                flags=_RENAME_NOREPLACE,
            )
            restored = _capture_document_at(
                config_file,
                path,
                parent_fd,
                required=True,
            )
            if not _document_matches_artifact(restored, external_artifact):
                raise AtomicConfigTransactionIntegrityError(
                    config_file,
                    "startup external target restore is ambiguous",
                )
            journal_artifact = _transition_recorded_journal(
                config_file,
                path,
                parent_fd,
                journal_artifact,
                "abort",
            )
            # The candidate slot was just consumed by the rename above (it is
            # now the live target), so only the journal itself remains to be
            # cleaned up.
            _cleanup_recovery_artifacts(
                config_file,
                path,
                parent_fd,
                (journal_artifact,),
            )
            return
        raise AtomicConfigTransactionIntegrityError(
            config_file,
            "startup missing-target rollback state is ambiguous",
        )

    def __repr__(self) -> str:
        return f"AtomicConfigFileTransaction(config_file={self.config_file.value!r}, state={self._state.value!r})"

    def __del__(self) -> None:
        parent_fd = getattr(self, "_parent_fd", None)
        if parent_fd is None:
            return
        try:
            os.close(parent_fd)
        except OSError:
            pass
        self._parent_fd = None

    def _target_metadata(self) -> tuple[int, int | None, int | None]:
        metadata = self._expected_document.metadata
        if metadata is None:
            return self._new_file_mode, None, None
        return stat.S_IMODE(metadata.mode), metadata.uid, metadata.gid

    def _write_artifact(
        self,
        kind: str,
        content: bytes,
        *,
        mode: int,
        uid: int | None,
        gid: int | None,
    ) -> str:
        name, artifact_fd = _open_unique_transaction_artifact(
            self.config_file,
            kind,
            self._require_parent_fd(),
        )
        artifact_attribute = "_candidate_name" if kind == "candidate" else "_rollback_name"
        record_attribute = "_candidate_artifact" if kind == "candidate" else "_rollback_artifact"
        setattr(self, artifact_attribute, name)
        primary_error: BaseException | None = None
        try:
            artifact = _prepare_artifact_fd(
                self.config_file,
                kind,
                name,
                artifact_fd,
                content,
                mode=mode,
                uid=uid,
                gid=gid,
            )
            setattr(
                self,
                record_attribute,
                artifact,
            )
            return name
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _close_fd_preserving_primary(artifact_fd, primary_error)

    def _write_journal(self) -> None:
        candidate_artifact = self._candidate_artifact
        if candidate_artifact is None:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                "candidate identity is unavailable for journal",
            )
        expected_artifact = _document_as_artifact(self._expected_document)
        journal = _TransactionJournal(
            config_file=self.config_file,
            target_name=self._expected_document.path.name,
            expected_exists=self._expected_document.exists,
            expected_artifact=expected_artifact,
            candidate_artifact=candidate_artifact,
            rollback_artifact=self._rollback_artifact,
        )
        content = journal.to_bytes()
        name, journal_fd = _open_unique_transaction_artifact(
            self.config_file,
            "journal.prepared",
            self._require_parent_fd(),
        )
        self._journal_name = name
        primary_error: BaseException | None = None
        try:
            self._journal_artifact = _prepare_artifact_fd(
                self.config_file,
                "journal",
                name,
                journal_fd,
                content,
                mode=0o600,
                uid=None,
                gid=None,
            )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _close_fd_preserving_primary(journal_fd, primary_error)

    def _open_validated_artifact(
        self,
        name: str | None,
        artifact: _TransactionArtifact | None,
        expected_content: bytes,
        *,
        kind: str,
    ) -> tuple[str, int]:
        if name is None or artifact is None or artifact.name != name:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                f"prepared {kind} artifact is unavailable",
            )

        artifact_fd: int | None = None
        primary_error: BaseException | None = None
        try:
            artifact_fd = os.open(
                name,
                _FILE_OPEN_FLAGS,
                dir_fd=self._require_parent_fd(),
            )
            before = os.fstat(artifact_fd)
            if not _artifact_stat_matches(artifact, before):
                raise AtomicConfigTransactionError(
                    self.config_file,
                    f"{kind} artifact changed before replace",
                )
            content = _read_all(artifact_fd)
            after = os.fstat(artifact_fd)
            named = os.stat(
                name,
                dir_fd=self._require_parent_fd(),
                follow_symlinks=False,
            )
            if (
                _stat_identity(before) != _stat_identity(after)
                or _stat_identity(after) != _stat_identity(named)
                or not _artifact_stat_matches(artifact, after)
                or content != expected_content
                or hashlib.sha256(content).hexdigest() != artifact.digest
            ):
                raise AtomicConfigTransactionError(
                    self.config_file,
                    f"{kind} artifact changed before replace",
                )
            return name, artifact_fd
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if primary_error is not None and artifact_fd is not None:
                _close_fd_preserving_primary(artifact_fd, primary_error)

    def _validate_target_from_artifact(
        self,
        artifact: _TransactionArtifact | None,
        expected_content: bytes,
        *,
        kind: str,
        held_fd: int | None = None,
    ) -> None:
        if artifact is None:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                f"{kind} target identity is unavailable",
            )
        if held_fd is not None and not _artifact_stat_matches(
            artifact,
            os.fstat(held_fd),
        ):
            raise AtomicConfigTransactionError(
                self.config_file,
                f"{kind} source changed during replace",
            )

        target_artifact = replace(
            artifact,
            name=self._expected_document.path.name,
        )
        _, target_fd = self._open_validated_artifact(
            target_artifact.name,
            target_artifact,
            expected_content,
            kind=f"{kind} target",
        )
        primary_error: BaseException | None = None
        try:
            if held_fd is not None and _stat_identity(os.fstat(held_fd)) != _stat_identity(os.fstat(target_fd)):
                raise AtomicConfigTransactionError(
                    self.config_file,
                    f"{kind} target identity changed during replace",
                )
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            _close_fd_preserving_primary(target_fd, primary_error)

    def _validate_replaced_candidate(self) -> None:
        self._validate_parent_identity()
        self._validate_target_from_artifact(
            self._candidate_artifact,
            self._candidate_content,
            kind="candidate",
        )

    def _validate_rollback_target(self) -> None:
        self._validate_parent_identity()
        if self._expected_document.exists:
            assert self._expected_document.content is not None
            self._validate_target_from_artifact(
                self._rollback_artifact,
                self._expected_document.content,
                kind="rollback",
            )
            return
        try:
            os.stat(
                self._expected_document.path.name,
                dir_fd=self._require_parent_fd(),
                follow_symlinks=False,
            )
        except FileNotFoundError:
            return
        raise AtomicConfigTransactionError(
            self.config_file,
            "rollback target is not absent",
        )

    def _transition_journal(self, phase: str) -> None:
        current_name = self._journal_name
        artifact = self._journal_artifact
        if (
            current_name is None
            or artifact is None
            or ".journal." not in current_name
        ):
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                "transaction journal is unavailable",
            )
        _validate_recorded_artifact(
            self.config_file,
            self._require_parent_fd(),
            artifact,
            kind="journal",
        )
        next_name = f"{current_name.rsplit('.journal.', 1)[0]}.journal.{phase}"
        if next_name != current_name:
            _renameat2(
                current_name,
                next_name,
                src_dir_fd=self._require_parent_fd(),
                dst_dir_fd=self._require_parent_fd(),
                flags=_RENAME_NOREPLACE,
            )
            self._journal_name = next_name
            self._journal_artifact = replace(artifact, name=next_name)
        os.fsync(self._require_parent_fd())

    def _complete_commit_cleanup(self) -> None:
        self._transition_journal("commit")
        if self._displaced_artifact is not None:
            _unlink_verified_artifact(
                self.config_file,
                self._require_parent_fd(),
                self._displaced_artifact,
            )
            self._displaced_name = None
            self._displaced_artifact = None
        os.fsync(self._require_parent_fd())

    def _restore_displaced_external(self) -> None:
        displaced_name = self._displaced_name
        candidate_artifact = self._candidate_artifact
        displaced_artifact = self._displaced_artifact
        if (
            displaced_name is None
            or candidate_artifact is None
            or displaced_artifact is None
        ):
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                "external recovery identity is unavailable",
            )
        try:
            self._transition_journal("restore")
            _renameat2(
                displaced_name,
                self._expected_document.path.name,
                src_dir_fd=self._require_parent_fd(),
                dst_dir_fd=self._require_parent_fd(),
                flags=_RENAME_EXCHANGE,
            )
            restored = _capture_document_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                required=True,
            )
            staged_candidate = _capture_document_name_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                source_name=displaced_name,
                required=True,
            )
            if (
                not _document_matches_artifact(restored, displaced_artifact)
                or not _document_matches_artifact(
                    staged_candidate,
                    candidate_artifact,
                )
            ):
                raise AtomicConfigTransactionIntegrityError(
                    self.config_file,
                    "external recovery result is ambiguous",
                )
            self._candidate_name = displaced_name
            self._displaced_name = None
            self._displaced_artifact = None
            self._transition_journal("abort")
            cleanup_error = self._abort_internal()
            if cleanup_error is not None:
                raise cleanup_error
        except BaseException as exc:
            if self._state is not AtomicConfigTransactionState.ABORTED:
                self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionIntegrityError):
                raise
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                f"external recovery failed ({_safe_exception_type(exc)})",
            ) from None

    def _restore_external_rollback_race(
        self,
        rollback_name: str,
        displaced: ConfigDocument,
        *,
        retry_state: AtomicConfigTransactionState,
    ) -> None:
        displaced_artifact = _document_as_artifact(displaced)
        if displaced_artifact is None or self._rollback_artifact is None:
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                "rollback race identity is unavailable",
            )
        displaced_artifact = replace(displaced_artifact, name=rollback_name)
        try:
            _renameat2(
                rollback_name,
                self._expected_document.path.name,
                src_dir_fd=self._require_parent_fd(),
                dst_dir_fd=self._require_parent_fd(),
                flags=_RENAME_EXCHANGE,
            )
            restored = _capture_document_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                required=True,
            )
            staged_rollback = _capture_document_name_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                source_name=rollback_name,
                required=True,
            )
            if (
                not _document_matches_artifact(restored, displaced_artifact)
                or not _document_matches_artifact(
                    staged_rollback,
                    self._rollback_artifact,
                )
            ):
                raise AtomicConfigTransactionIntegrityError(
                    self.config_file,
                    "rollback race recovery result is ambiguous",
                )
            self._transition_journal("commit")
            self._state = retry_state
        except BaseException as exc:
            self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionIntegrityError):
                raise
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                f"rollback race recovery failed ({_safe_exception_type(exc)})",
            ) from None

    def _restore_external_missing_rollback(
        self,
        displaced: ConfigDocument,
        *,
        retry_state: AtomicConfigTransactionState,
    ) -> None:
        candidate_artifact = self._candidate_artifact
        displaced_artifact = _document_as_artifact(displaced)
        if candidate_artifact is None or displaced_artifact is None:
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                "missing-target rollback identity is unavailable",
            )
        try:
            _renameat2(
                candidate_artifact.name,
                self._expected_document.path.name,
                src_dir_fd=self._require_parent_fd(),
                dst_dir_fd=self._require_parent_fd(),
                flags=_RENAME_NOREPLACE,
            )
            restored = _capture_document_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                required=True,
            )
            if not _document_matches_artifact(restored, displaced_artifact):
                raise AtomicConfigTransactionIntegrityError(
                    self.config_file,
                    "missing-target rollback recovery result is ambiguous",
                )
            self._transition_journal("commit")
            self._state = retry_state
        except BaseException as exc:
            self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
            if not isinstance(exc, Exception):
                raise
            if isinstance(exc, AtomicConfigTransactionIntegrityError):
                raise
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                f"missing-target rollback recovery failed ({_safe_exception_type(exc)})",
            ) from None

    def _complete_rollback_cleanup(self) -> None:
        self._transition_journal("abort")
        for name_attribute, artifact_attribute in (
            ("_candidate_name", "_candidate_artifact"),
            ("_displaced_name", "_displaced_artifact"),
        ):
            artifact = getattr(self, artifact_attribute)
            if artifact is None:
                continue
            _unlink_verified_artifact(
                self.config_file,
                self._require_parent_fd(),
                artifact,
            )
            setattr(self, name_attribute, None)
            setattr(self, artifact_attribute, None)
        if self._journal_artifact is not None:
            _unlink_verified_artifact(
                self.config_file,
                self._require_parent_fd(),
                self._journal_artifact,
            )
            self._journal_name = None
            self._journal_artifact = None
        os.fsync(self._require_parent_fd())

    def _abort_replaced_after_race(self, primary_error: BaseException) -> None:
        try:
            self._validate_held_parent_identity()
            current = _capture_document_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                required=False,
            )
            if _document_matches_artifact(current, self._candidate_artifact):
                raise AtomicConfigTransactionIntegrityError(
                    self.config_file,
                    "canonical parent identity changed after exchange",
                )
            self._candidate_artifact = None
            self._complete_external_win_cleanup()
            self._finish(AtomicConfigTransactionState.ABORTED)
        except BaseException as recovery_error:
            if self._state is not AtomicConfigTransactionState.ABORTED:
                self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
            if not isinstance(primary_error, Exception):
                raise primary_error
            if isinstance(recovery_error, AtomicConfigTransactionIntegrityError):
                raise recovery_error
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                f"replaced-target recovery failed ({_safe_exception_type(recovery_error)})",
            ) from None
        if not isinstance(primary_error, Exception):
            raise primary_error
        raise AtomicConfigTransactionConflictError(
            self.config_file,
            "committed candidate changed before durability confirmation",
        ) from None

    def _recover_rollback_after_race(self, primary_error: BaseException) -> None:
        try:
            self._validate_held_parent_identity()
            current = _capture_document_at(
                self.config_file,
                self._expected_document.path,
                self._require_parent_fd(),
                required=False,
            )
            if _document_matches_artifact(current, self._rollback_artifact):
                raise AtomicConfigTransactionIntegrityError(
                    self.config_file,
                    "canonical parent identity changed during rollback",
                )
            self._rollback_artifact = None
            self._complete_external_win_cleanup()
            self._finish(AtomicConfigTransactionState.ABORTED)
        except BaseException as recovery_error:
            if self._state is not AtomicConfigTransactionState.ABORTED:
                self._state = AtomicConfigTransactionState.RECOVERY_REQUIRED
            if not isinstance(primary_error, Exception):
                raise primary_error
            if isinstance(recovery_error, AtomicConfigTransactionIntegrityError):
                raise recovery_error
            raise AtomicConfigTransactionIntegrityError(
                self.config_file,
                f"rollback recovery failed ({_safe_exception_type(recovery_error)})",
            ) from None
        if not isinstance(primary_error, Exception):
            raise primary_error
        raise AtomicConfigTransactionConflictError(
            self.config_file,
            "rollback target changed during durability confirmation",
        ) from None

    def _complete_external_win_cleanup(self) -> None:
        self._transition_journal("abort")
        for name_attribute, artifact_attribute in (
            ("_candidate_name", "_candidate_artifact"),
            ("_displaced_name", "_displaced_artifact"),
            ("_rollback_name", "_rollback_artifact"),
        ):
            artifact = getattr(self, artifact_attribute)
            name = getattr(self, name_attribute)
            if artifact is None or name is None:
                continue
            _unlink_verified_artifact(
                self.config_file,
                self._require_parent_fd(),
                artifact,
            )
            setattr(self, name_attribute, None)
            setattr(self, artifact_attribute, None)
        if self._journal_artifact is not None:
            _unlink_verified_artifact(
                self.config_file,
                self._require_parent_fd(),
                self._journal_artifact,
            )
            self._journal_name = None
            self._journal_artifact = None
        os.fsync(self._require_parent_fd())

    def _validate_held_parent_identity(self) -> None:
        held_stat = os.fstat(self._require_parent_fd())
        if (held_stat.st_dev, held_stat.st_ino) != self._parent_identity:
            raise AtomicConfigTransactionError(
                self.config_file,
                "held config parent identity changed",
            )

    def _validate_parent_identity(self) -> None:
        self._validate_held_parent_identity()
        reopened_fd: int | None = None
        try:
            reopened_fd = _open_parent_directory(self._expected_document.path)
            reopened_stat = os.fstat(reopened_fd)
            if (reopened_stat.st_dev, reopened_stat.st_ino) != self._parent_identity:
                raise AtomicConfigTransactionError(
                    self.config_file,
                    "config parent identity changed",
                )
        finally:
            if reopened_fd is not None:
                os.close(reopened_fd)

    def _abort_internal(self) -> BaseException | None:
        if self._journal_artifact is not None:
            try:
                self._transition_journal("abort")
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                return exc
        cleanup_error = self._remove_owned_artifacts()
        if cleanup_error is not None:
            return cleanup_error
        try:
            self._finish(AtomicConfigTransactionState.ABORTED)
        except BaseException as exc:
            return exc
        return None

    def _remove_owned_artifacts(self) -> BaseException | None:
        first_error: BaseException | None = None
        for attribute, record_attribute in (
            ("_candidate_name", "_candidate_artifact"),
            ("_rollback_name", "_rollback_artifact"),
            ("_journal_name", "_journal_artifact"),
        ):
            name = getattr(self, attribute)
            if name is None:
                continue
            try:
                os.unlink(name, dir_fd=self._require_parent_fd())
            except FileNotFoundError:
                setattr(self, attribute, None)
                setattr(self, record_attribute, None)
            except BaseException as exc:
                if not isinstance(exc, Exception):
                    raise
                if first_error is None:
                    first_error = exc
            else:
                setattr(self, attribute, None)
                setattr(self, record_attribute, None)
                self._abort_needs_sync = True
        if self._abort_needs_sync:
            try:
                os.fsync(self._require_parent_fd())
            except BaseException as exc:
                if first_error is None:
                    first_error = exc
            else:
                self._abort_needs_sync = False
        return first_error

    def _require_state(
        self,
        *allowed: AtomicConfigTransactionState,
        action: str,
    ) -> None:
        if self._state not in allowed or self._parent_fd is None:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                f"cannot {action} transaction in state {self._state.value}",
            )

    def _require_parent_fd(self) -> int:
        if self._parent_fd is None:
            raise AtomicConfigTransactionStateError(
                self.config_file,
                "transaction parent is closed",
            )
        return self._parent_fd

    def _finish(self, state: AtomicConfigTransactionState) -> None:
        self._state = state
        parent_fd = self._parent_fd
        self._parent_fd = None
        if parent_fd is not None:
            os.close(parent_fd)


def _write_all(target_fd: int, content: bytes) -> None:
    remaining = memoryview(content)
    while remaining:
        written = os.write(target_fd, remaining)
        if written <= 0:
            raise OSError(errno.EIO, "short write")
        remaining = remaining[written:]


def _prepare_artifact_fd(
    config_file: ConfigFile,
    kind: str,
    name: str,
    artifact_fd: int,
    content: bytes,
    *,
    mode: int,
    uid: int | None,
    gid: int | None,
) -> _TransactionArtifact:
    _write_all(artifact_fd, content)
    chown_applied = False
    if uid is not None and gid is not None:
        try:
            os.fchown(artifact_fd, uid, gid)
            chown_applied = True
        except OSError:
            logger.warning(
                "%s: best-effort fchown to uid=%s gid=%s failed for %s artifact %s",
                config_file.value,
                uid,
                gid,
                kind,
                name,
            )
    os.fchmod(artifact_fd, mode)
    os.fsync(artifact_fd)
    artifact_stat = os.fstat(artifact_fd)
    if (
        not stat.S_ISREG(artifact_stat.st_mode)
        or artifact_stat.st_nlink != 1
        or artifact_stat.st_size != len(content)
        or stat.S_IMODE(artifact_stat.st_mode) != mode
        or (chown_applied and uid is not None and artifact_stat.st_uid != uid)
        or (chown_applied and gid is not None and artifact_stat.st_gid != gid)
    ):
        raise AtomicConfigTransactionError(
            config_file,
            f"{kind} artifact metadata is invalid",
        )
    return _TransactionArtifact.from_stat(name, artifact_stat, content)


def _document_as_artifact(
    document: ConfigDocument,
) -> _TransactionArtifact | None:
    if not document.exists:
        return None
    metadata = document.metadata
    content = document.content
    if metadata is None or content is None or document.digest is None:
        raise AtomicConfigTransactionError(
            document.config_file,
            "expected source identity is unavailable",
        )
    return _TransactionArtifact(
        name=document.path.name,
        device=metadata.device,
        inode=metadata.inode,
        size=metadata.size,
        mode=stat.S_IMODE(metadata.mode),
        uid=metadata.uid,
        gid=metadata.gid,
        digest=document.digest,
    )


def _artifact_to_journal_value(
    artifact: _TransactionArtifact | None,
) -> dict[str, int | str] | None:
    if artifact is None:
        return None
    return {
        "device": artifact.device,
        "digest": artifact.digest,
        "gid": artifact.gid,
        "inode": artifact.inode,
        "mode": artifact.mode,
        "name": artifact.name,
        "size": artifact.size,
        "uid": artifact.uid,
    }


def _parse_transaction_journal(
    config_file: ConfigFile,
    target_name: str,
    content: bytes,
) -> _TransactionJournal:
    if not content or len(content) > _JOURNAL_MAX_BYTES:
        raise AtomicConfigTransactionIntegrityError(
            config_file,
            "startup journal size is invalid",
        )

    def strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
        result: dict[str, object] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate journal key")
            result[key] = value
        return result

    try:
        payload = json.loads(content.decode("ascii"), object_pairs_hook=strict_object)
        if not isinstance(payload, dict) or set(payload) != {
            "candidate",
            "config_file",
            "expected",
            "expected_exists",
            "rollback",
            "target",
            "version",
        }:
            raise ValueError("invalid journal fields")
        if payload["version"] != _JOURNAL_VERSION:
            raise ValueError("invalid journal version")
        if payload["config_file"] != config_file.value:
            raise ValueError("invalid journal config identity")
        if payload["target"] != target_name:
            raise ValueError("invalid journal target")
        expected_exists = payload["expected_exists"]
        if not isinstance(expected_exists, bool):
            raise ValueError("invalid expected state")
        expected = _parse_journal_artifact(payload["expected"])
        candidate = _parse_journal_artifact(payload["candidate"])
        rollback = _parse_journal_artifact(payload["rollback"])
        if candidate is None:
            raise ValueError("missing candidate identity")
        if candidate.name == target_name or not candidate.name.endswith(".candidate"):
            raise ValueError("invalid candidate name")
        if not _is_owned_transaction_artifact(config_file, candidate.name):
            raise ValueError("invalid candidate ownership")
        if expected_exists:
            if expected is None or rollback is None:
                raise ValueError("missing expected rollback identity")
            if expected.name != target_name or not rollback.name.endswith(".rollback"):
                raise ValueError("invalid expected rollback name")
            if not _is_owned_transaction_artifact(config_file, rollback.name):
                raise ValueError("invalid rollback ownership")
        elif expected is not None or rollback is not None:
            raise ValueError("unexpected missing-target identity")
        return _TransactionJournal(
            config_file=config_file,
            target_name=target_name,
            expected_exists=expected_exists,
            expected_artifact=expected,
            candidate_artifact=candidate,
            rollback_artifact=rollback,
        )
    except (UnicodeError, ValueError, TypeError, json.JSONDecodeError):
        raise AtomicConfigTransactionIntegrityError(
            config_file,
            "startup journal is invalid",
        ) from None


def _parse_journal_artifact(value: object) -> _TransactionArtifact | None:
    if value is None:
        return None
    if not isinstance(value, dict) or set(value) != {
        "device",
        "digest",
        "gid",
        "inode",
        "mode",
        "name",
        "size",
        "uid",
    }:
        raise ValueError("invalid journal artifact")
    name = value["name"]
    digest = value["digest"]
    numeric_names = ("device", "gid", "inode", "mode", "size", "uid")
    if (
        not isinstance(name, str)
        or not name
        or "/" in name
        or name in {".", ".."}
        or not isinstance(digest, str)
        or len(digest) != 64
        or any(character not in "0123456789abcdef" for character in digest)
        or any(
            not isinstance(value[field_name], int)
            or isinstance(value[field_name], bool)
            or value[field_name] < 0
            for field_name in numeric_names
        )
    ):
        raise ValueError("invalid journal artifact value")
    return _TransactionArtifact(
        name=name,
        device=value["device"],
        inode=value["inode"],
        size=value["size"],
        mode=value["mode"],
        uid=value["uid"],
        gid=value["gid"],
        digest=digest,
    )


def _unlink_verified_artifact(
    config_file: ConfigFile,
    parent_fd: int,
    artifact: _TransactionArtifact,
) -> None:
    _validate_recorded_artifact(
        config_file,
        parent_fd,
        artifact,
        kind="cleanup",
    )
    os.unlink(artifact.name, dir_fd=parent_fd)


def _validate_recovery_parent(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
) -> None:
    held_stat = os.fstat(parent_fd)
    held_identity = (held_stat.st_dev, held_stat.st_ino)
    reopened_fd: int | None = None
    try:
        reopened_fd = _open_parent_directory(path)
        reopened_stat = os.fstat(reopened_fd)
        if (
            not stat.S_ISDIR(held_stat.st_mode)
            or (reopened_stat.st_dev, reopened_stat.st_ino) != held_identity
        ):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                "startup recovery parent identity changed",
            )
    finally:
        if reopened_fd is not None:
            os.close(reopened_fd)


def _recovery_renameat2(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    source: str,
    target: str,
    *,
    flags: int,
) -> None:
    _validate_recovery_parent(config_file, path, parent_fd)
    _renameat2(
        source,
        target,
        src_dir_fd=parent_fd,
        dst_dir_fd=parent_fd,
        flags=flags,
    )
    _validate_recovery_parent(config_file, path, parent_fd)


def _sync_recovery_parent(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
) -> None:
    _validate_recovery_parent(config_file, path, parent_fd)
    os.fsync(parent_fd)
    _validate_recovery_parent(config_file, path, parent_fd)


def _transition_recorded_journal(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    journal_artifact: _TransactionArtifact,
    phase: str,
) -> _TransactionArtifact:
    _validate_recovery_parent(config_file, path, parent_fd)
    _validate_recorded_artifact(
        config_file,
        parent_fd,
        journal_artifact,
        kind="journal",
    )
    next_name = (
        f"{journal_artifact.name.rsplit('.journal.', 1)[0]}.journal.{phase}"
    )
    if next_name != journal_artifact.name:
        _recovery_renameat2(
            config_file,
            path,
            parent_fd,
            journal_artifact.name,
            next_name,
            flags=_RENAME_NOREPLACE,
        )
        journal_artifact = replace(journal_artifact, name=next_name)
    _sync_recovery_parent(config_file, path, parent_fd)
    return journal_artifact


def _cleanup_recovery_artifacts(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    artifacts: tuple[_TransactionArtifact | None, ...],
) -> None:
    present = tuple(artifact for artifact in artifacts if artifact is not None)
    for artifact in present:
        _validate_recovery_parent(config_file, path, parent_fd)
        _validate_recorded_artifact(
            config_file,
            parent_fd,
            artifact,
            kind="recovery cleanup",
        )
        os.unlink(artifact.name, dir_fd=parent_fd)
        _validate_recovery_parent(config_file, path, parent_fd)
    _sync_recovery_parent(config_file, path, parent_fd)


def _match_recovery_artifact_path(
    config_file: ConfigFile,
    path: Path,
    parent_fd: int,
    name: str,
    allowed: tuple[_TransactionArtifact | None, ...],
) -> _TransactionArtifact | None:
    document = _capture_document_name_at(
        config_file,
        path,
        parent_fd,
        source_name=name,
        required=False,
    )
    if not document.exists:
        return None
    for artifact in allowed:
        if artifact is not None and _document_matches_artifact(document, artifact):
            return replace(artifact, name=name)
    raise AtomicConfigTransactionIntegrityError(
        config_file,
        "startup recovery artifact path is ambiguous",
    )


def _artifact_stat_matches(
    artifact: _TransactionArtifact,
    source_stat: os.stat_result,
) -> bool:
    return (
        stat.S_ISREG(source_stat.st_mode)
        and source_stat.st_nlink == 1
        and source_stat.st_dev == artifact.device
        and source_stat.st_ino == artifact.inode
        and source_stat.st_size == artifact.size
        and stat.S_IMODE(source_stat.st_mode) == artifact.mode
        and source_stat.st_uid == artifact.uid
        and source_stat.st_gid == artifact.gid
    )


def _validate_recorded_artifact(
    config_file: ConfigFile,
    parent_fd: int,
    artifact: _TransactionArtifact,
    *,
    kind: str,
) -> bytes:
    artifact_fd: int | None = None
    try:
        artifact_fd = os.open(
            artifact.name,
            _FILE_OPEN_FLAGS,
            dir_fd=parent_fd,
        )
        before = os.fstat(artifact_fd)
        content = _read_all(artifact_fd)
        after = os.fstat(artifact_fd)
        named = os.stat(
            artifact.name,
            dir_fd=parent_fd,
            follow_symlinks=False,
        )
        if (
            _stat_identity(before) != _stat_identity(after)
            or _stat_identity(after) != _stat_identity(named)
            or not _artifact_stat_matches(artifact, after)
            or hashlib.sha256(content).hexdigest() != artifact.digest
        ):
            raise AtomicConfigTransactionIntegrityError(
                config_file,
                f"{kind} artifact identity is invalid",
            )
        return content
    except AtomicConfigTransactionError:
        raise
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise AtomicConfigTransactionIntegrityError(
            config_file,
            f"{kind} artifact could not be verified ({_safe_exception_type(exc)})",
        ) from None
    finally:
        if artifact_fd is not None:
            os.close(artifact_fd)


def _document_matches_artifact(
    document: ConfigDocument,
    artifact: _TransactionArtifact | None,
) -> bool:
    if artifact is None:
        return not document.exists
    metadata = document.metadata
    return (
        document.exists
        and metadata is not None
        and document.digest == artifact.digest
        and metadata.device == artifact.device
        and metadata.inode == artifact.inode
        and metadata.size == artifact.size
        and stat.S_IMODE(metadata.mode) == artifact.mode
        and metadata.uid == artifact.uid
        and metadata.gid == artifact.gid
        and metadata.link_count == 1
    )


def _close_fd_preserving_primary(
    fd: int,
    primary_error: BaseException | None,
) -> None:
    try:
        os.close(fd)
    except BaseException:
        if primary_error is None:
            raise


def _open_unique_transaction_artifact(
    config_file: ConfigFile,
    kind: str,
    parent_fd: int,
) -> tuple[str, int]:
    for _attempt in range(_TRANSACTION_TOKEN_ATTEMPTS):
        token = secrets.token_hex(16)
        name = f"{_transaction_artifact_prefix(config_file)}{token}.{kind}"
        try:
            artifact_fd = os.open(
                name,
                _TEMP_FILE_FLAGS,
                0o600,
                dir_fd=parent_fd,
            )
        except FileExistsError:
            continue
        return name, artifact_fd
    raise OSError(errno.EEXIST, "unique transaction artifact is unavailable")


def _transaction_artifact_prefix(config_file: ConfigFile) -> str:
    return f"{_TRANSACTION_PREFIX}{config_file.value}-"


def _validate_transaction_target_name(expected_document: ConfigDocument) -> None:
    if expected_document.path.name.startswith(_TRANSACTION_PREFIX):
        raise AtomicConfigTransactionError(
            expected_document.config_file,
            "config target name uses the reserved transaction prefix",
        )


def _is_owned_transaction_artifact(config_file: ConfigFile, name: str) -> bool:
    prefix = _transaction_artifact_prefix(config_file)
    if not name.startswith(prefix):
        return False
    token_and_kind = name.removeprefix(prefix)
    token, separator, kind = token_and_kind.partition(".")
    return (
        separator == "."
        and kind
        in {
            "candidate",
            "rollback",
            "recovery",
            "journal.prepared",
            "journal.restore",
            "journal.restore_move",
            "journal.commit",
            "journal.abort",
            "journal.finalize",
        }
        and len(token) == 32
        and all(character in "0123456789abcdef" for character in token)
    )


_renameat2_syscall: ctypes._FuncPointer | None = None
_renameat2_unavailable = False


def _resolve_renameat2_syscall() -> ctypes._FuncPointer:
    global _renameat2_syscall, _renameat2_unavailable
    if _renameat2_syscall is not None:
        return _renameat2_syscall
    if _renameat2_unavailable or not sys.platform.startswith("linux"):
        _renameat2_unavailable = True
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    libc = ctypes.CDLL(None, use_errno=True)
    renameat2 = getattr(libc, "renameat2", None)
    if renameat2 is None:
        _renameat2_unavailable = True
        raise OSError(errno.ENOSYS, "renameat2 is unavailable")
    renameat2.argtypes = (
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    )
    renameat2.restype = ctypes.c_int
    _renameat2_syscall = renameat2
    return renameat2


def _renameat2(
    source: str,
    target: str,
    *,
    src_dir_fd: int,
    dst_dir_fd: int,
    flags: int,
) -> None:
    renameat2 = _resolve_renameat2_syscall()
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


def _safe_exception_type(exc: BaseException) -> str:
    exception_type = type(exc).__name__
    if not exception_type.isascii() or not exception_type.isidentifier() or len(exception_type) > 128:
        return "BaseException"
    return exception_type
