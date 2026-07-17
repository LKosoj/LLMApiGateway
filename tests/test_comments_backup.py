from __future__ import annotations

import errno
import os
import re
import stat
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from pathlib import Path

import pytest

import llm_gateway_core.config.comments_backup as comments_backup
from llm_gateway_core.config.comments_backup import (
    CommentsBackupError,
    CommentsBackupLifecycle,
    CommentsBackupPublishResult,
    CommentsBackupState,
    CommentsBackupStateError,
)
from llm_gateway_core.config.config_store import (
    ConfigDocument,
    ConfigFile,
    ConfigFileMetadata,
)


_FIXED_NOW = datetime(2026, 7, 13, 10, 11, 12, 345678, tzinfo=timezone.utc)
_FIXED_SUFFIX = "2026-07-13T10:11:12.345678Z"
_SECRET_BYTES = b'\xef\xbb\xbf{\r\n  "url": "https://example.test/a//b",\r\n  // keep this\r\n  "value": 1\r\n}\r\n'


@pytest.fixture(autouse=True)
def _reset_backup_timestamp() -> None:
    with comments_backup._timestamp_lock:
        comments_backup._last_timestamp = None
    yield
    with comments_backup._timestamp_lock:
        comments_backup._last_timestamp = None


def _existing_document(
    path: Path,
    content: bytes = _SECRET_BYTES,
    *,
    mode: int = 0o640,
) -> ConfigDocument:
    path.write_bytes(content)
    path.chmod(mode)
    source_stat = path.stat()
    return ConfigDocument.from_bytes(
        ConfigFile.MODEL_RULES,
        path,
        content,
        metadata=ConfigFileMetadata.from_stat(source_stat),
    )


def _begin(path: Path, content: bytes = _SECRET_BYTES) -> CommentsBackupLifecycle:
    lifecycle = CommentsBackupLifecycle.begin(_existing_document(path, content))
    assert lifecycle is not None
    return lifecycle


def _backup_paths(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.bak.*"))


def _regular_owned_backups(path: Path) -> list[Path]:
    result: list[Path] = []
    valid_name = re.compile(
        rf"{re.escape(path.name)}\.bak\.\d{{4}}-\d{{2}}-\d{{2}}T"
        r"\d{2}:\d{2}:\d{2}\.\d{6}Z"
    )
    for candidate in _backup_paths(path):
        candidate_stat = candidate.lstat()
        if (
            valid_name.fullmatch(candidate.name)
            and stat.S_ISREG(candidate_stat.st_mode)
            and candidate_stat.st_nlink == 1
        ):
            result.append(candidate)
    return result


def _valid_old_backup(path: Path, second: int) -> Path:
    backup = path.with_name(
        f"{path.name}.bak.2000-01-01T00:00:{second:02d}.000000Z"
    )
    backup.write_bytes(f"old-{second}".encode())
    backup.chmod(0o600)
    return backup


@pytest.mark.parametrize(
    ("content", "has_comments"),
    [
        (b'{"value": 1} // line\n', True),
        (b'/* block */ {"value": 1}\n', True),
        (b"{'value': 1 /* inline */}\n", True),
        (b'{"url": "https://example.test/a//b"}\n', False),
        (b'{"text": "literal /* block */ text"}\n', False),
        (b"{'text': 'literal // line'}\n", False),
        (b'{"text": "escaped \\\" // still string"}\n', False),
        (b"{'text': 'escaped \\' /* still string */'}\n", False),
    ],
)
def test_begin_detects_comments_only_outside_single_and_double_quoted_strings(
    tmp_path: Path,
    content: bytes,
    has_comments: bool,
) -> None:
    path = tmp_path / "models_model_rules.json"
    document = _existing_document(path, content)

    lifecycle = CommentsBackupLifecycle.begin(document)

    assert (lifecycle is not None) is has_comments


def test_begin_skips_missing_empty_and_comment_free_documents(tmp_path: Path) -> None:
    missing_path = tmp_path / "missing.json"
    missing = ConfigDocument.missing(ConfigFile.MODEL_RULES, missing_path)
    empty_path = tmp_path / "empty.json"
    empty = _existing_document(empty_path, b"")
    plain_path = tmp_path / "plain.json"
    plain = _existing_document(plain_path, b'{"url":"https://example.test"}\r\n')
    metadata_free_plain = ConfigDocument.from_bytes(
        ConfigFile.MODEL_RULES,
        tmp_path / "metadata-free-plain.json",
        b'{"value": 1}\n',
    )

    assert CommentsBackupLifecycle.begin(missing) is None
    assert CommentsBackupLifecycle.begin(empty) is None
    assert CommentsBackupLifecycle.begin(plain) is None
    assert CommentsBackupLifecycle.begin(metadata_free_plain) is None


def test_prepare_publish_preserves_exact_bom_crlf_mode_name_and_durability(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    original_open = os.open
    original_fsync = os.fsync
    backup_open_flags: list[int] = []
    fsync_kinds: list[str] = []

    def recording_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if ".bak." in os.fsdecode(name):
            backup_open_flags.append(flags)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    def recording_fsync(fd: int) -> None:
        fsync_kinds.append("directory" if stat.S_ISDIR(os.fstat(fd).st_mode) else "file")
        original_fsync(fd)

    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    monkeypatch.setattr(comments_backup.os, "open", recording_open)
    monkeypatch.setattr(comments_backup.os, "fsync", recording_fsync)

    lifecycle.prepare()
    result = lifecycle.publish()

    assert lifecycle.state is CommentsBackupState.PUBLISHED
    assert lifecycle.cleanup_pending is False
    assert result == CommentsBackupPublishResult(
        basename=f"{path.name}.bak.{_FIXED_SUFFIX}",
        cleanup_pending=False,
    )
    assert result.basename is not None
    backup = path.parent / result.basename
    assert backup.read_bytes() == _SECRET_BYTES
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert backup.stat().st_uid == os.getuid()
    assert backup.stat().st_gid == os.getgid()
    assert backup.stat().st_ino != path.stat().st_ino
    assert backup.stat().st_nlink == 1
    assert re.fullmatch(
        rf"{re.escape(path.name)}\.bak\.\d{{4}}-\d{{2}}-\d{{2}}T"
        r"\d{2}:\d{2}:\d{2}\.\d{6}Z",
        result.basename,
    )
    assert backup_open_flags
    required_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
    assert backup_open_flags[0] & required_flags == required_flags
    assert fsync_kinds[:2] == ["file", "directory"]


def test_prepare_collision_uses_o_excl_and_never_overwrites_existing_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    collision = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    collision.write_bytes(b"external-collision")
    collision_inode = collision.stat().st_ino
    target_identity = (path.read_bytes(), path.stat().st_ino)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)

    lifecycle.prepare()
    result = lifecycle.publish()

    assert collision.read_bytes() == b"external-collision"
    assert collision.stat().st_ino == collision_inode
    assert (path.read_bytes(), path.stat().st_ino) == target_identity
    assert result == CommentsBackupPublishResult(
        basename=f"{path.name}.bak.2026-07-13T10:11:12.345679Z",
        cleanup_pending=False,
    )
    assert result.basename is not None
    assert (path.parent / result.basename).read_bytes() == _SECRET_BYTES
    assert lifecycle.state is CommentsBackupState.PUBLISHED


@pytest.mark.parametrize(
    "fault",
    ["open", "write", "fchmod", "file_fsync", "directory_fsync"],
)
def test_prepare_faults_preserve_source_and_are_abort_cleanup_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    path = tmp_path / "sensitive-model-rules.json"
    lifecycle = _begin(path)
    target_identity = (path.read_bytes(), path.stat().st_ino)
    original_open = os.open
    original_write = os.write
    original_fchmod = os.fchmod
    original_fsync = os.fsync

    def injected_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if fault == "open" and ".bak." in os.fsdecode(name):
            raise OSError(errno.ENOSPC, "raw-open-secret")
        return original_open(name, flags, mode, dir_fd=dir_fd)

    def injected_write(fd: int, content: bytes | memoryview) -> int:
        if fault == "write":
            raise OSError(errno.ENOSPC, "raw-write-secret")
        return original_write(fd, content)

    def injected_fchmod(fd: int, mode: int) -> None:
        if fault == "fchmod":
            raise OSError(errno.EPERM, "raw-mode-secret")
        original_fchmod(fd, mode)

    def injected_fsync(fd: int) -> None:
        is_directory = stat.S_ISDIR(os.fstat(fd).st_mode)
        if (fault == "file_fsync" and not is_directory) or (
            fault == "directory_fsync" and is_directory
        ):
            raise OSError(errno.EIO, "raw-fsync-secret")
        original_fsync(fd)

    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    monkeypatch.setattr(comments_backup.os, "open", injected_open)
    monkeypatch.setattr(comments_backup.os, "write", injected_write)
    monkeypatch.setattr(comments_backup.os, "fchmod", injected_fchmod)
    monkeypatch.setattr(comments_backup.os, "fsync", injected_fsync)

    with pytest.raises(CommentsBackupError) as raised:
        lifecycle.prepare()

    assert (path.read_bytes(), path.stat().st_ino) == target_identity
    assert "sensitive-model-rules" not in str(raised.value)
    assert "raw-" not in str(raised.value)
    assert repr(lifecycle).find("sensitive-model-rules") == -1
    monkeypatch.undo()
    lifecycle.abort()
    assert lifecycle.state is CommentsBackupState.ABORTED
    assert _backup_paths(path) == []


def test_first_artifact_fstat_failure_keeps_descriptor_owner_until_abort(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    original_open = os.open
    original_fstat = os.fstat
    artifact_fd: int | None = None
    failures_remaining = 2

    def record_artifact_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        nonlocal artifact_fd
        opened_fd = original_open(name, flags, mode, dir_fd=dir_fd)
        if ".bak." in os.fsdecode(name):
            artifact_fd = opened_fd
        return opened_fd

    def fail_first_artifact_stats(fd: int) -> os.stat_result:
        nonlocal failures_remaining
        source_stat = original_fstat(fd)
        if failures_remaining and fd == artifact_fd:
            failures_remaining -= 1
            raise OSError(errno.EIO, "raw-fstat-secret")
        return source_stat

    monkeypatch.setattr(comments_backup.os, "open", record_artifact_open)
    monkeypatch.setattr(comments_backup.os, "fstat", fail_first_artifact_stats)

    with pytest.raises(CommentsBackupError) as raised:
        lifecycle.prepare()

    assert "raw-fstat-secret" not in str(raised.value)
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED
    assert lifecycle._artifact_fd is not None
    assert os.fstat(lifecycle._artifact_fd).st_nlink == 1
    assert len(_backup_paths(path)) == 1

    monkeypatch.setattr(comments_backup.os, "fstat", original_fstat)
    lifecycle.abort()
    assert lifecycle.state is CommentsBackupState.ABORTED
    assert _backup_paths(path) == []


def test_prepare_rejects_target_replaced_after_begin_without_creating_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    replacement = tmp_path / "external-replacement"
    replacement.write_bytes(b'{"external": true}\n')
    os.replace(replacement, path)
    external_identity = (path.read_bytes(), path.stat().st_ino)

    with pytest.raises(CommentsBackupError):
        lifecycle.prepare()

    assert lifecycle.state is CommentsBackupState.FAILED
    assert _backup_paths(path) == []
    assert (path.read_bytes(), path.stat().st_ino) == external_identity


def test_prepare_late_target_replacement_aborts_durable_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    original_fsync = os.fsync
    replaced = False

    def replace_after_directory_sync(fd: int) -> None:
        nonlocal replaced
        original_fsync(fd)
        if stat.S_ISDIR(os.fstat(fd).st_mode) and not replaced:
            replaced = True
            replacement = tmp_path / "late-external-replacement"
            replacement.write_bytes(b'{"late_external": true}\n')
            os.replace(replacement, path)

    monkeypatch.setattr(comments_backup.os, "fsync", replace_after_directory_sync)

    with pytest.raises(CommentsBackupError):
        lifecycle.prepare()

    assert replaced is True
    assert lifecycle.state is CommentsBackupState.FAILED
    assert _backup_paths(path) == []
    assert path.read_bytes() == b'{"late_external": true}\n'


def test_abort_is_idempotent_and_retryable_after_partial_cleanup_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    backup = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    original_unlinkat = comments_backup._unlinkat
    attempts = 0

    def transient_unlinkat(parent_fd: int, name: str) -> None:
        nonlocal attempts
        if name.startswith(comments_backup._PRIVATE_CLEANUP_PREFIX):
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EIO, "raw-abort-secret")
        original_unlinkat(parent_fd, name)

    monkeypatch.setattr(comments_backup, "_unlinkat", transient_unlinkat)

    with pytest.raises(CommentsBackupError) as raised:
        lifecycle.abort()
    assert "raw-abort-secret" not in str(raised.value)
    private_backups = list(
        path.parent.glob(f"{comments_backup._PRIVATE_CLEANUP_PREFIX}*")
    )
    assert len(private_backups) == 1
    assert private_backups[0].read_bytes() == _SECRET_BYTES

    lifecycle.abort()
    lifecycle.abort()
    assert lifecycle.state is CommentsBackupState.ABORTED
    assert not backup.exists()


def test_publish_keeps_ten_verified_regular_backups_and_ignores_impostors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    old_backups = [_valid_old_backup(path, second) for second in range(12)]
    outside = tmp_path / "outside-sentinel"
    outside.write_bytes(b"must-survive")
    symlink = path.with_name(f"{path.name}.bak.1997-01-01T00:00:00.000000Z")
    symlink.symlink_to(outside)
    hardlink = path.with_name(f"{path.name}.bak.1996-01-01T00:00:00.000000Z")
    os.link(outside, hardlink)
    directory = path.with_name(f"{path.name}.bak.1995-01-01T00:00:00.000000Z")
    directory.mkdir()
    malformed = path.with_name(f"{path.name}.bak.latest")
    malformed.write_bytes(b"malformed")
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)

    lifecycle.prepare()
    result = lifecycle.publish()

    assert result.basename == f"{path.name}.bak.{_FIXED_SUFFIX}"
    assert result.cleanup_pending is False
    assert len(_regular_owned_backups(path)) == 10
    assert all(not backup.exists() for backup in old_backups[:3])
    assert all(backup.exists() for backup in old_backups[3:])
    assert symlink.is_symlink()
    assert hardlink.exists()
    assert directory.is_dir()
    assert malformed.read_bytes() == b"malformed"
    assert outside.read_bytes() == b"must-survive"


@pytest.mark.parametrize("tamper", ["missing", "substituted"])
def test_publish_never_returns_missing_or_substituted_current_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    tamper: str,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    backup = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    if tamper == "substituted":
        substitute = tmp_path / "external-substitute"
        substitute.write_bytes(b"external-substitute")
        os.replace(substitute, backup)
        substitute_identity = (backup.read_bytes(), backup.stat().st_ino)
    else:
        backup.unlink()

    result = lifecycle.publish()

    assert result.basename is None
    assert result.cleanup_pending is True
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED
    if tamper == "substituted":
        assert (backup.read_bytes(), backup.stat().st_ino) == substitute_identity

    retry_result = lifecycle.retry_cleanup()

    assert retry_result == CommentsBackupPublishResult(
        basename=f"{path.name}.bak.2026-07-13T10:11:12.345679Z",
        cleanup_pending=False,
    )
    assert retry_result.basename is not None
    assert (path.parent / retry_result.basename).read_bytes() == _SECRET_BYTES
    assert lifecycle.state is CommentsBackupState.PUBLISHED
    if tamper == "substituted":
        assert (backup.read_bytes(), backup.stat().st_ino) == substitute_identity


@pytest.mark.parametrize("failure", ["unlink", "directory_fsync"])
def test_partial_rotation_failure_is_publish_success_and_retry_cleanup_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure: str,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    for second in range(11):
        _valid_old_backup(path, second)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    original_unlinkat = comments_backup._unlinkat
    original_fsync = os.fsync
    failed = False

    def transient_unlinkat(parent_fd: int, name: str) -> None:
        nonlocal failed
        if (
            failure == "unlink"
            and not failed
            and name.startswith(comments_backup._PRIVATE_CLEANUP_PREFIX)
        ):
            failed = True
            raise OSError(errno.EIO, "raw-rotation-unlink-secret")
        original_unlinkat(parent_fd, name)

    def transient_fsync(fd: int) -> None:
        nonlocal failed
        if failure == "directory_fsync" and not failed and stat.S_ISDIR(os.fstat(fd).st_mode):
            failed = True
            raise OSError(errno.EIO, "raw-rotation-fsync-secret")
        original_fsync(fd)

    monkeypatch.setattr(comments_backup, "_unlinkat", transient_unlinkat)
    monkeypatch.setattr(comments_backup.os, "fsync", transient_fsync)

    result = lifecycle.publish()

    assert failed is True
    assert result.basename == f"{path.name}.bak.{_FIXED_SUFFIX}"
    assert result.cleanup_pending is True
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED
    assert lifecycle.cleanup_pending is True

    retry_result = lifecycle.retry_cleanup()
    repeated_result = lifecycle.retry_cleanup()
    assert retry_result == repeated_result == CommentsBackupPublishResult(
        basename=f"{path.name}.bak.{_FIXED_SUFFIX}",
        cleanup_pending=False,
    )
    assert lifecycle.state is CommentsBackupState.PUBLISHED
    assert lifecycle.cleanup_pending is False
    assert len(_regular_owned_backups(path)) == 10


def test_publish_rejects_same_inode_content_tampering_and_recreates_exact_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    backup = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    original_inode = backup.stat().st_ino
    backup.write_bytes(b"same-inode-tampering")
    assert backup.stat().st_ino == original_inode

    result = lifecycle.publish()

    assert result == CommentsBackupPublishResult(
        basename=None,
        cleanup_pending=True,
    )
    retry_result = lifecycle.retry_cleanup()
    assert retry_result.basename == (
        f"{path.name}.bak.2026-07-13T10:11:12.345679Z"
    )
    assert retry_result.cleanup_pending is False
    assert retry_result.basename is not None
    assert (path.parent / retry_result.basename).read_bytes() == _SECRET_BYTES
    assert backup.read_bytes() == b"same-inode-tampering"


def test_publish_final_revalidation_detects_backup_removed_during_rotation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    backup = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    original_listdir = os.listdir
    removed = False

    def remove_current_then_list(directory_fd: int) -> list[str]:
        nonlocal removed
        if not removed:
            removed = True
            backup.unlink()
        return original_listdir(directory_fd)

    monkeypatch.setattr(comments_backup.os, "listdir", remove_current_then_list)

    result = lifecycle.publish()

    assert removed is True
    assert result == CommentsBackupPublishResult(
        basename=None,
        cleanup_pending=True,
    )
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED


def test_abort_restores_foreign_substitute_raced_before_private_move(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    backup = path.with_name(f"{path.name}.bak.{_FIXED_SUFFIX}")
    original_renameat2 = comments_backup._renameat2
    substituted = False

    def substitute_then_rename(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal substituted
        if source == backup.name and not substituted:
            substituted = True
            substitute = tmp_path / "foreign-substitute"
            substitute.write_bytes(b"foreign-must-survive")
            os.replace(substitute, backup)
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(comments_backup, "_renameat2", substitute_then_rename)

    lifecycle.abort()

    assert substituted is True
    assert lifecycle.state is CommentsBackupState.ABORTED
    assert backup.read_bytes() == b"foreign-must-survive"
    assert not list(
        path.parent.glob(f"{comments_backup._PRIVATE_CLEANUP_PREFIX}*")
    )


def test_terminal_parent_close_preserves_owner_until_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    parent_fd = lifecycle._parent_fd
    assert parent_fd is not None
    parent_identity = (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino)
    original_close = os.close
    terminal = KeyboardInterrupt()
    injected = False

    def terminal_close(fd: int) -> None:
        nonlocal injected
        if fd == parent_fd and not injected:
            injected = True
            raise terminal
        original_close(fd)

    monkeypatch.setattr(comments_backup.os, "close", terminal_close)

    with pytest.raises(KeyboardInterrupt) as raised:
        lifecycle.publish()

    assert raised.value is terminal
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED
    assert lifecycle.cleanup_pending is True
    assert lifecycle._parent_fd == parent_fd
    assert (os.fstat(parent_fd).st_dev, os.fstat(parent_fd).st_ino) == parent_identity

    result = lifecycle.retry_cleanup()
    assert result.cleanup_pending is False
    assert lifecycle.state is CommentsBackupState.PUBLISHED


def test_repeated_verify_close_failure_keeps_local_descriptor_retry_owned(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    parent_fd = lifecycle._parent_fd
    assert parent_fd is not None
    original_close = os.close
    target_fd: int | None = None
    failures_remaining = 2

    def fail_same_verify_close(fd: int) -> None:
        nonlocal target_fd, failures_remaining
        if (
            fd != parent_fd
            and target_fd is None
            and stat.S_ISREG(os.fstat(fd).st_mode)
        ):
            target_fd = fd
        if fd == target_fd and failures_remaining:
            failures_remaining -= 1
            raise OSError(errno.EIO, "raw-local-close-secret")
        original_close(fd)

    monkeypatch.setattr(comments_backup.os, "close", fail_same_verify_close)

    first_result = lifecycle.publish()

    assert first_result.cleanup_pending is True
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED
    assert target_fd is not None
    assert target_fd in lifecycle._pending_fds
    assert os.fstat(target_fd).st_nlink == 1

    first_retry = lifecycle.retry_cleanup()
    assert first_retry.cleanup_pending is True
    assert target_fd in lifecycle._pending_fds

    retry_result = lifecycle.retry_cleanup()
    assert retry_result.cleanup_pending is False
    assert lifecycle.state is CommentsBackupState.PUBLISHED
    assert target_fd not in lifecycle._pending_fds


def test_unlink_then_close_failure_still_requires_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    parent_fd = lifecycle._parent_fd
    assert parent_fd is not None
    original_unlinkat = comments_backup._unlinkat
    original_close = os.close
    original_fsync = os.fsync
    unlinked = False
    close_failed = False
    directory_syncs = 0

    def recording_unlinkat(directory_fd: int, name: str) -> None:
        nonlocal unlinked
        original_unlinkat(directory_fd, name)
        unlinked = True

    def fail_verify_close_once(fd: int) -> None:
        nonlocal close_failed
        if unlinked and fd != parent_fd and not close_failed:
            close_failed = True
            raise OSError(errno.EIO, "raw-close-secret")
        original_close(fd)

    def record_directory_sync(fd: int) -> None:
        nonlocal directory_syncs
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_syncs += 1
        original_fsync(fd)

    monkeypatch.setattr(comments_backup, "_unlinkat", recording_unlinkat)
    monkeypatch.setattr(comments_backup.os, "close", fail_verify_close_once)
    monkeypatch.setattr(comments_backup.os, "fsync", record_directory_sync)

    with pytest.raises(CommentsBackupError):
        lifecycle.abort()

    assert unlinked is True
    assert close_failed is True
    assert directory_syncs == 0
    assert lifecycle.state is CommentsBackupState.CLEANUP_REQUIRED

    lifecycle.abort()
    assert directory_syncs == 1
    assert lifecycle.state is CommentsBackupState.ABORTED


def test_rotation_substitution_removes_other_candidates_and_keeps_exactly_ten(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    old_backups = [_valid_old_backup(path, second) for second in range(12)]
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()
    original_renameat2 = comments_backup._renameat2
    substituted = False

    def substitute_first_candidate(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal substituted
        if source == old_backups[0].name and not substituted:
            substituted = True
            substitute = tmp_path / "foreign-retention-candidate"
            substitute.write_bytes(b"foreign-retention-data")
            substitute.chmod(0o600)
            os.replace(substitute, old_backups[0])
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(
        comments_backup,
        "_renameat2",
        substitute_first_candidate,
    )

    result = lifecycle.publish()

    assert substituted is True
    assert result.cleanup_pending is False
    assert lifecycle.state is CommentsBackupState.PUBLISHED
    assert old_backups[0].read_bytes() == b"foreign-retention-data"
    assert all(not candidate.exists() for candidate in old_backups[1:4])
    assert len(_regular_owned_backups(path)) == 10


def test_state_machine_rejects_transitions_that_could_delete_published_backup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)

    with pytest.raises(CommentsBackupStateError):
        lifecycle.publish()
    with pytest.raises(CommentsBackupStateError):
        lifecycle.retry_cleanup()
    lifecycle.prepare()
    with pytest.raises(CommentsBackupStateError):
        lifecycle.prepare()
    result = lifecycle.publish()
    with pytest.raises(CommentsBackupStateError):
        lifecycle.publish()
    with pytest.raises(CommentsBackupStateError):
        lifecycle.abort()

    assert result.basename is not None
    assert (path.parent / result.basename).read_bytes() == _SECRET_BYTES


def test_publish_result_is_immutable_and_repr_does_not_disclose_path_or_payload(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_root = tmp_path / "credential-root"
    secret_root.mkdir()
    path = secret_root / "models_model_rules.json"
    lifecycle = _begin(path)
    monkeypatch.setattr(comments_backup, "_now_utc", lambda: _FIXED_NOW)
    lifecycle.prepare()

    result = lifecycle.publish()

    with pytest.raises((FrozenInstanceError, AttributeError)):
        result.basename = "changed"  # type: ignore[misc]
    assert "credential-root" not in repr(result)
    assert "keep this" not in repr(result)
    assert "credential-root" not in repr(lifecycle)
    assert "keep this" not in repr(lifecycle)
