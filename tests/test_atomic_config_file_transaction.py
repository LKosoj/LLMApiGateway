from __future__ import annotations

import asyncio
import errno
import logging
import os
import stat
from pathlib import Path

import pytest

import llm_gateway_core.config.atomic_config_transaction as config_store
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionConflictError,
    AtomicConfigTransactionError,
    AtomicConfigTransactionIntegrityError,
    AtomicConfigTransactionState,
    AtomicConfigTransactionStateError,
    ConfigDocument,
    ConfigFile,
)
from tests.atomic_config_test_support import (
    _NEW_BYTES,
    _OLD_BYTES,
    _artifact,
    _artifact_paths,
    _assert_no_artifacts,
    _begin_existing,
    _capture_existing,
    _directory_fsync_failure,
    _missing,
    _tamper_artifact,
)


def test_existing_file_lifecycle_preserves_exact_bytes_mode_owner_and_durability(
    tmp_path: Path,
) -> None:
    path, expected, transaction = _begin_existing(tmp_path)

    assert transaction.state is AtomicConfigTransactionState.BEGUN
    transaction.prepare()

    candidate = _artifact(tmp_path, "candidate")
    rollback = _artifact(tmp_path, "rollback")
    expected_stat = path.stat()
    assert path.read_bytes() == _OLD_BYTES
    assert candidate.read_bytes() == _NEW_BYTES
    assert rollback.read_bytes() == _OLD_BYTES
    assert candidate.stat().st_ino != path.stat().st_ino
    assert rollback.stat().st_ino != path.stat().st_ino
    assert rollback.stat().st_ino != candidate.stat().st_ino
    for artifact in (candidate, rollback):
        artifact_stat = artifact.stat()
        assert stat.S_IMODE(artifact_stat.st_mode) == stat.S_IMODE(expected_stat.st_mode)
        assert artifact_stat.st_uid == expected_stat.st_uid
        assert artifact_stat.st_gid == expected_stat.st_gid
        assert artifact_stat.st_nlink == 1

    transaction.commit()

    committed_stat = path.stat()
    assert transaction.state is AtomicConfigTransactionState.COMMITTED
    assert path.read_bytes() == _NEW_BYTES
    assert stat.S_IMODE(committed_stat.st_mode) == 0o640
    assert committed_stat.st_uid == expected.metadata.uid
    assert committed_stat.st_gid == expected.metadata.gid
    assert len(_artifact_paths(tmp_path)) == 2

    transaction.finalize()

    assert transaction.state is AtomicConfigTransactionState.FINALIZED
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)


def test_existing_file_rollback_restores_exact_bytes_mode_and_owner(tmp_path: Path) -> None:
    path, expected, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()

    transaction.rollback()

    restored_stat = path.stat()
    assert transaction.state is AtomicConfigTransactionState.ROLLED_BACK
    assert path.read_bytes() == _OLD_BYTES
    assert stat.S_IMODE(restored_stat.st_mode) == 0o640
    assert restored_stat.st_uid == expected.metadata.uid
    assert restored_stat.st_gid == expected.metadata.gid
    _assert_no_artifacts(tmp_path)


def test_missing_file_commit_and_finalize_uses_secure_mode(tmp_path: Path) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)

    transaction.prepare()
    assert len(_artifact_paths(tmp_path)) == 2
    transaction.commit()
    transaction.finalize()

    assert path.read_bytes() == _NEW_BYTES
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert transaction.state is AtomicConfigTransactionState.FINALIZED
    _assert_no_artifacts(tmp_path)


def test_missing_file_rollback_restores_absence(tmp_path: Path) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(
        _missing(path),
        _NEW_BYTES,
        new_file_mode=0o400,
    )
    transaction.prepare()
    transaction.commit()
    assert path.exists()

    transaction.rollback()

    assert not path.exists()
    assert transaction.state is AtomicConfigTransactionState.ROLLED_BACK
    _assert_no_artifacts(tmp_path)


@pytest.mark.parametrize(
    "mode",
    [
        pytest.param(-1, id="negative"),
        pytest.param(0o1000, id="above-0o777"),
        pytest.param(True, id="bool"),
        pytest.param(0o000, id="no-owner-read"),
        pytest.param(0o700, id="owner-exec-bit"),
        pytest.param(0o664, id="group-write-bit"),
        pytest.param(0o646, id="other-write-bit"),
        pytest.param(0o610, id="group-exec-bit"),
        pytest.param(0o601, id="other-exec-bit"),
    ],
)
def test_begin_rejects_unsafe_new_file_modes(tmp_path: Path, mode: int) -> None:
    # Group/other *read* bits are permitted (0o644, matching
    # container_preflight.py's convention); only exec bits and group/other
    # write bits (plus a missing owner-read bit) remain insecure.
    with pytest.raises(ValueError, match="new_file_mode must be a secure mode"):
        AtomicConfigFileTransaction.begin(
            _missing(tmp_path / "models_model_rules.json"),
            _NEW_BYTES,
            new_file_mode=mode,
        )


@pytest.mark.parametrize("mode", [0o600, 0o644])
def test_begin_accepts_secure_new_file_modes(tmp_path: Path, mode: int) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(
        _missing(path),
        _NEW_BYTES,
        new_file_mode=mode,
    )

    transaction.prepare()
    transaction.commit()
    transaction.finalize()

    assert stat.S_IMODE(path.stat().st_mode) == mode
    _assert_no_artifacts(tmp_path)


def test_state_machine_rejects_invalid_and_repeated_transitions(tmp_path: Path) -> None:
    _, _, begun = _begin_existing(tmp_path)
    for operation in (begun.commit, begun.rollback, begun.finalize):
        with pytest.raises(AtomicConfigTransactionStateError):
            operation()
    begun.abort()
    with pytest.raises(AtomicConfigTransactionStateError):
        begun.abort()

    second_root = tmp_path / "second"
    second_root.mkdir()
    _, _, prepared = _begin_existing(second_root)
    prepared.prepare()
    with pytest.raises(AtomicConfigTransactionStateError):
        prepared.prepare()
    prepared.abort()

    third_root = tmp_path / "third"
    third_root.mkdir()
    _, _, committed = _begin_existing(third_root)
    committed.prepare()
    committed.commit()
    for operation in (committed.prepare, committed.commit, committed.abort):
        with pytest.raises(AtomicConfigTransactionStateError):
            operation()
    committed.finalize()
    for operation in (committed.finalize, committed.rollback):
        with pytest.raises(AtomicConfigTransactionStateError):
            operation()


def test_prepare_handles_partial_writes_until_exact_bytes_are_durable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    original_write = os.write

    def limited_write(fd: int, content: bytes | memoryview) -> int:
        return original_write(fd, content[:3])

    monkeypatch.setattr(config_store.os, "write", limited_write)

    transaction.prepare()
    transaction.commit()
    transaction.finalize()

    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)


@pytest.mark.parametrize("fault", ["open", "write", "fchmod", "file_fsync"])
def test_prepare_io_faults_leave_original_and_no_owned_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)

    if fault == "open":
        original_open = os.open

        def failing_open(
            name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
            flags: int,
            mode: int = 0o777,
            *,
            dir_fd: int | None = None,
        ) -> int:
            if flags & os.O_EXCL:
                raise OSError(errno.EIO, "sensitive open failure")
            if dir_fd is None:
                return original_open(name, flags, mode)
            return original_open(name, flags, mode, dir_fd=dir_fd)

        monkeypatch.setattr(config_store.os, "open", failing_open)
    elif fault == "write":
        monkeypatch.setattr(
            config_store.os,
            "write",
            lambda _fd, _content: (_ for _ in ()).throw(OSError(errno.ENOSPC, "secret disk path")),
        )
    elif fault == "fchmod":
        monkeypatch.setattr(
            config_store.os,
            "fchmod",
            lambda _fd, _mode: (_ for _ in ()).throw(OSError(errno.EPERM, "secret mode")),
        )
    else:
        original_fsync = os.fsync

        def failing_file_fsync(fd: int) -> None:
            if stat.S_ISREG(os.fstat(fd).st_mode):
                raise OSError(errno.EIO, "secret file sync")
            original_fsync(fd)

        monkeypatch.setattr(config_store.os, "fsync", failing_file_fsync)

    with pytest.raises(AtomicConfigTransactionError):
        transaction.prepare()

    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_prepare_fchown_failure_is_best_effort_and_transaction_still_completes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A denied os.fchown (e.g. non-root writing to a root-owned file) must
    not fail the transaction; it is logged and the transaction proceeds."""
    path, _, transaction = _begin_existing(tmp_path)
    monkeypatch.setattr(
        config_store.os,
        "fchown",
        lambda _fd, _uid, _gid: (_ for _ in ()).throw(OSError(errno.EPERM, "secret owner")),
    )

    with caplog.at_level(logging.WARNING, logger=config_store.__name__):
        transaction.prepare()

    assert transaction.state is AtomicConfigTransactionState.PREPARED
    assert "fchown" in caplog.text
    assert "secret owner" not in caplog.text

    transaction.commit()
    transaction.finalize()

    assert path.read_bytes() == _NEW_BYTES
    assert transaction.state is AtomicConfigTransactionState.FINALIZED
    _assert_no_artifacts(tmp_path)


def test_zero_length_write_is_rejected_and_cleaned_up(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    monkeypatch.setattr(config_store.os, "write", lambda _fd, _content: 0)

    with pytest.raises(AtomicConfigTransactionError, match="candidate preparation"):
        transaction.prepare()

    assert path.read_bytes() == _OLD_BYTES
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_prepare_directory_fsync_failure_cleans_and_syncs_artifact_removal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    directory_calls = _directory_fsync_failure(monkeypatch, fail_on_call=1)

    with pytest.raises(AtomicConfigTransactionError, match="prepared directory sync"):
        transaction.prepare()

    assert len(directory_calls) == 3
    assert path.read_bytes() == _OLD_BYTES
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_commit_replace_failure_aborts_without_changing_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    original_inode = path.stat().st_ino
    transaction.prepare()
    original_renameat2 = config_store._renameat2

    def fail_exchange(*args: object, **kwargs: object) -> None:
        if kwargs.get("flags") == config_store._RENAME_EXCHANGE:
            raise OSError(errno.EIO, "secret replace")
        original_renameat2(*args, **kwargs)

    monkeypatch.setattr(config_store, "_renameat2", fail_exchange)

    with pytest.raises(AtomicConfigTransactionError, match="atomic exchange"):
        transaction.commit()

    assert path.read_bytes() == _OLD_BYTES
    assert path.stat().st_ino == original_inode
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_commit_directory_fsync_failure_remains_rollback_capable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    _directory_fsync_failure(monkeypatch, fail_on_call=2)
    transaction.prepare()

    with pytest.raises(AtomicConfigTransactionError, match="committed directory sync"):
        transaction.commit()

    assert transaction.state is AtomicConfigTransactionState.REPLACED
    assert path.read_bytes() == _NEW_BYTES
    with pytest.raises(AtomicConfigTransactionStateError, match="state replaced"):
        transaction.finalize()

    original_renameat2 = config_store._renameat2

    def reject_second_exchange(*args: object, **kwargs: object) -> None:
        if kwargs.get("flags") == config_store._RENAME_EXCHANGE:
            raise AssertionError("REPLACED retry must not exchange again")
        original_renameat2(*args, **kwargs)

    monkeypatch.setattr(config_store, "_renameat2", reject_second_exchange)
    transaction.commit()
    assert transaction.state is AtomicConfigTransactionState.COMMITTED

    monkeypatch.undo()
    transaction.rollback()
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_precommit_existing_file_drift_is_a_conflict_and_preserves_external_bytes(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    external = b'{"external":true}\n'
    path.write_bytes(external)

    with pytest.raises(AtomicConfigTransactionConflictError):
        transaction.commit()

    assert path.read_bytes() == external
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_external_writer_between_compare_and_replace_survives_as_canonical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    external = tmp_path / "external-update"
    external_bytes = b'{"external":"won-race"}\n'
    external.write_bytes(external_bytes)
    external.chmod(0o600)
    original_renameat2 = config_store._renameat2
    raced = False

    def racing_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal raced
        if (
            not raced
            and source == candidate.name
            and target == path.name
            and flags == config_store._RENAME_EXCHANGE
        ):
            raced = True
            os.replace(external, path)
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", racing_renameat2)

    with pytest.raises(AtomicConfigTransactionConflictError):
        transaction.commit()

    assert raced is True
    assert path.read_bytes() == external_bytes
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_prepare_persists_strict_journal_before_first_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    original_inode = path.stat().st_ino
    transaction.prepare()
    journal = _artifact(tmp_path, "journal.prepared")
    original_renameat2 = config_store._renameat2
    observed_journal = False

    def stop_at_exchange(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal observed_journal
        if flags == config_store._RENAME_EXCHANGE:
            observed_journal = journal.is_file() and b'"version":1' in journal.read_bytes()
            raise OSError(errno.EIO, "stop before exchange")
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", stop_at_exchange)

    with pytest.raises(AtomicConfigTransactionError, match="atomic exchange"):
        transaction.commit()

    assert observed_journal is True
    assert path.read_bytes() == _OLD_BYTES
    assert path.stat().st_ino == original_inode
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


def test_unsupported_renameat2_fails_closed_without_target_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    original_inode = path.stat().st_ino
    monkeypatch.setattr(
        config_store,
        "_renameat2",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            OSError(errno.ENOSYS, "renameat2 unavailable")
        ),
    )

    with pytest.raises(AtomicConfigTransactionError, match="commit cleanup failed"):
        transaction.commit()

    assert path.read_bytes() == _OLD_BYTES
    assert path.stat().st_ino == original_inode
    assert transaction.state is AtomicConfigTransactionState.PREPARED
    assert len(_artifact_paths(tmp_path)) == 3
    assert _artifact(tmp_path, "journal.prepared").is_file()
    artifacts_before = {
        artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
        for artifact in _artifact_paths(tmp_path)
    }
    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )
    assert path.read_bytes() == _OLD_BYTES
    assert path.stat().st_ino == original_inode
    assert {
        artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
        for artifact in _artifact_paths(tmp_path)
    } == artifacts_before
    monkeypatch.undo()
    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered == 1
    assert path.read_bytes() == _OLD_BYTES
    assert path.stat().st_ino == original_inode
    _assert_no_artifacts(tmp_path)


def test_precommit_missing_file_drift_is_a_conflict_and_preserves_external_file(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    external = b'{"external":true}\n'
    path.write_bytes(external)

    with pytest.raises(AtomicConfigTransactionConflictError):
        transaction.commit()

    assert path.read_bytes() == external
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(tmp_path)


@pytest.mark.parametrize(
    "tamper",
    ["mutated_regular", "symlink", "hardlink", "fifo"],
)
def test_commit_rejects_tampered_candidate_before_replace(
    tmp_path: Path,
    tamper: str,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    _tamper_artifact(candidate, tamper, outside)

    with pytest.raises(AtomicConfigTransactionError, match="candidate"):
        transaction.commit()

    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    assert outside.read_bytes() == b"outside-sentinel"
    _assert_no_artifacts(tmp_path)


@pytest.mark.parametrize(
    "tamper",
    ["mutated_regular", "symlink", "hardlink", "fifo"],
)
def test_rollback_rejects_tampered_artifact_before_replace(
    tmp_path: Path,
    tamper: str,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    rollback = _artifact(tmp_path, "rollback")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    _tamper_artifact(rollback, tamper, outside)

    with pytest.raises(AtomicConfigTransactionError, match="rollback"):
        transaction.rollback()

    assert transaction.state is AtomicConfigTransactionState.COMMITTED
    assert path.read_bytes() == _NEW_BYTES
    assert outside.read_bytes() == b"outside-sentinel"
    assert rollback.exists() or rollback.is_symlink()
    rollback.unlink()
    del transaction


def test_candidate_path_swap_inside_exchange_fails_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, expected, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    original_renameat2 = config_store._renameat2
    raced = False

    def racing_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal raced
        if not raced and source == candidate.name:
            raced = True
            candidate.unlink()
            candidate.symlink_to(outside)
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", racing_renameat2)

    with pytest.raises(AtomicConfigTransactionError):
        transaction.commit()

    assert raced is True
    assert transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED
    assert path.is_symlink()
    assert outside.read_bytes() == b"outside-sentinel"
    assert _artifact_paths(tmp_path)
    del expected, transaction


def test_rollback_path_swap_inside_exchange_fails_recovery_required(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, expected, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    rollback = _artifact(tmp_path, "rollback")
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    original_renameat2 = config_store._renameat2
    raced = False

    def racing_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal raced
        if not raced and source == rollback.name:
            raced = True
            rollback.unlink()
            rollback.symlink_to(outside)
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", racing_renameat2)

    with pytest.raises(AtomicConfigTransactionError):
        transaction.rollback()

    assert raced is True
    assert transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED
    assert path.is_symlink()
    assert outside.read_bytes() == b"outside-sentinel"
    assert _artifact_paths(tmp_path)
    del expected, transaction


def test_parent_rename_inside_exchange_fails_recovery_required_without_new_root_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    path, _, transaction = _begin_existing(config_root)
    transaction.prepare()
    candidate = _artifact(config_root, "candidate")
    displaced_root = tmp_path / "displaced"
    original_renameat2 = config_store._renameat2
    external_path: Path | None = None
    raced = False

    def racing_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal external_path, raced
        if not raced and source == candidate.name:
            raced = True
            config_root.rename(displaced_root)
            config_root.mkdir()
            external_path = config_root / path.name
            external_path.write_bytes(b"external-canonical-root")
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", racing_renameat2)

    with pytest.raises(AtomicConfigTransactionError):
        transaction.commit()

    assert raced is True
    assert external_path is not None
    assert external_path.read_bytes() == b"external-canonical-root"
    assert (displaced_root / path.name).read_bytes() == _NEW_BYTES
    assert transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED
    assert _artifact_paths(displaced_root)
    _assert_no_artifacts(config_root)
    del transaction


@pytest.mark.parametrize("drift", ["target_symlink", "parent_rename"])
def test_replaced_retry_revalidates_target_and_canonical_root_before_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    drift: str,
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    path, _, transaction = _begin_existing(config_root)
    _directory_fsync_failure(monkeypatch, fail_on_call=2)
    transaction.prepare()
    with pytest.raises(AtomicConfigTransactionError, match="committed directory sync"):
        transaction.commit()
    assert transaction.state is AtomicConfigTransactionState.REPLACED

    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    if drift == "target_symlink":
        path.unlink()
        path.symlink_to(outside)
        restored_path = path
        external_path = outside
    else:
        displaced_root = tmp_path / "displaced"
        config_root.rename(displaced_root)
        config_root.mkdir()
        external_path = config_root / path.name
        external_path.write_bytes(b"external-canonical-root")
        restored_path = displaced_root / path.name

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        transaction.commit()

    assert transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED
    assert restored_path.read_bytes() in {
        b"outside-sentinel",
        _NEW_BYTES,
    }
    assert external_path.read_bytes() in {
        b"outside-sentinel",
        b"external-canonical-root",
    }
    assert _artifact_paths(restored_path.parent)
    del transaction


def test_replaced_retry_revalidates_target_after_directory_fsync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    _directory_fsync_failure(monkeypatch, fail_on_call=2)
    transaction.prepare()
    with pytest.raises(AtomicConfigTransactionError, match="committed directory sync"):
        transaction.commit()
    assert transaction.state is AtomicConfigTransactionState.REPLACED

    monkeypatch.undo()
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside-sentinel")
    original_fsync = os.fsync
    raced = False

    def racing_fsync(fd: int) -> None:
        nonlocal raced
        original_fsync(fd)
        if not raced and stat.S_ISDIR(os.fstat(fd).st_mode):
            raced = True
            path.unlink()
            path.symlink_to(outside)

    monkeypatch.setattr(config_store.os, "fsync", racing_fsync)

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        transaction.commit()

    assert raced is True
    assert transaction.state is AtomicConfigTransactionState.RECOVERY_REQUIRED
    assert path.is_symlink()
    assert path.read_bytes() == b"outside-sentinel"
    assert outside.read_bytes() == b"outside-sentinel"
    assert _artifact_paths(tmp_path)
    del transaction


def test_parent_directory_drift_fails_closed_without_touching_new_root(tmp_path: Path) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    path, _, transaction = _begin_existing(config_root)
    transaction.prepare()

    displaced_root = tmp_path / "displaced"
    config_root.rename(displaced_root)
    config_root.mkdir()
    external_path = config_root / path.name
    external_path.write_bytes(b"external")

    with pytest.raises(AtomicConfigTransactionError, match="parent identity changed"):
        transaction.commit()

    assert external_path.read_bytes() == b"external"
    assert (displaced_root / path.name).read_bytes() == _OLD_BYTES
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    _assert_no_artifacts(displaced_root)


def test_rollback_replace_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    original_renameat2 = config_store._renameat2
    attempts = 0

    def transient_renameat2(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        if kwargs.get("flags") == config_store._RENAME_EXCHANGE:
            attempts += 1
        if attempts == 1:
            attempts += 1
            raise OSError(errno.EIO, "secret rollback replace")
        original_renameat2(*args, **kwargs)

    monkeypatch.setattr(config_store, "_renameat2", transient_renameat2)

    with pytest.raises(AtomicConfigTransactionError, match="rollback failed"):
        transaction.rollback()
    assert transaction.state is AtomicConfigTransactionState.COMMITTED
    assert path.read_bytes() == _NEW_BYTES

    transaction.rollback()
    assert path.read_bytes() == _OLD_BYTES
    assert transaction.state is AtomicConfigTransactionState.ROLLED_BACK
    _assert_no_artifacts(tmp_path)


def test_rollback_retry_fsyncs_same_journal_phase_before_exchange(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    rollback = _artifact(tmp_path, "rollback")
    original_fsync = os.fsync
    original_renameat2 = config_store._renameat2
    failed_restore_sync = False
    durable_restore_sync = False

    def injected_fsync(fd: int) -> None:
        nonlocal failed_restore_sync, durable_restore_sync
        restore_exists = any(
            artifact.name.endswith(".journal.restore")
            for artifact in _artifact_paths(tmp_path)
        )
        if restore_exists and stat.S_ISDIR(os.fstat(fd).st_mode):
            if not failed_restore_sync:
                failed_restore_sync = True
                raise OSError(errno.EIO, "restore directory sync failure")
            durable_restore_sync = True
        original_fsync(fd)

    def guarded_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        if (
            source == rollback.name
            and target == path.name
            and flags == config_store._RENAME_EXCHANGE
        ):
            assert durable_restore_sync is True
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store.os, "fsync", injected_fsync)
    monkeypatch.setattr(config_store, "_renameat2", guarded_renameat2)

    with pytest.raises(AtomicConfigTransactionError):
        transaction.rollback()
    transaction.rollback()

    assert failed_restore_sync is True
    assert durable_restore_sync is True
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_rollback_directory_fsync_failure_is_retryable_without_second_replace(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    directory_calls = _directory_fsync_failure(monkeypatch, fail_on_call=6)
    transaction.prepare()
    transaction.commit()

    with pytest.raises(AtomicConfigTransactionError, match="rollback failed"):
        transaction.rollback()
    assert transaction.state is AtomicConfigTransactionState.COMMITTED
    assert path.read_bytes() == _OLD_BYTES

    transaction.rollback()
    assert len(directory_calls) == 9
    assert transaction.state is AtomicConfigTransactionState.ROLLED_BACK
    _assert_no_artifacts(tmp_path)


def test_missing_file_rollback_noreplace_failure_is_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    transaction.commit()
    original_renameat2 = config_store._renameat2
    attempts = 0

    def transient_renameat2(*args: object, **kwargs: object) -> None:
        nonlocal attempts
        if (
            args
            and args[0] == path.name
            and kwargs.get("flags") == config_store._RENAME_NOREPLACE
        ):
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EIO, "secret target move")
        original_renameat2(*args, **kwargs)

    monkeypatch.setattr(config_store, "_renameat2", transient_renameat2)

    with pytest.raises(AtomicConfigTransactionError, match="rollback failed"):
        transaction.rollback()
    assert path.read_bytes() == _NEW_BYTES
    assert transaction.state is AtomicConfigTransactionState.COMMITTED

    transaction.rollback()
    assert not path.exists()
    assert transaction.state is AtomicConfigTransactionState.ROLLED_BACK
    _assert_no_artifacts(tmp_path)


def test_abort_unlink_failure_is_retryable_and_never_changes_target(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate_name = _artifact(tmp_path, "candidate").name
    original_unlink = os.unlink
    attempts = 0

    def transient_unlink(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal attempts
        if name == candidate_name:
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EIO, "secret artifact unlink")
        original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(config_store.os, "unlink", transient_unlink)

    with pytest.raises(AtomicConfigTransactionError, match="abort failed"):
        transaction.abort()
    assert transaction.state is AtomicConfigTransactionState.PREPARED
    assert path.read_bytes() == _OLD_BYTES
    assert len(_artifact_paths(tmp_path)) == 1

    transaction.abort()
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_abort_crash_after_first_unlink_leaves_recoverable_abort_journal(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SimulatedCrash(BaseException):
        pass

    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    original_fsync = os.fsync
    original_unlink = os.unlink
    abort_sync_attempts = 0
    durable_abort = False
    unlink_attempts = 0

    def fail_then_sync_abort(fd: int) -> None:
        nonlocal abort_sync_attempts, durable_abort
        if stat.S_ISDIR(os.fstat(fd).st_mode) and any(
            artifact.name.endswith(".journal.abort")
            for artifact in _artifact_paths(tmp_path)
        ):
            abort_sync_attempts += 1
            if abort_sync_attempts == 1:
                raise OSError(errno.EIO, "abort transition sync failure")
            durable_abort = True
        original_fsync(fd)

    def crash_after_unlink(
        name: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal unlink_attempts
        unlink_attempts += 1
        assert durable_abort is True
        original_unlink(name, dir_fd=dir_fd)
        raise SimulatedCrash

    monkeypatch.setattr(config_store.os, "fsync", fail_then_sync_abort)
    monkeypatch.setattr(config_store.os, "unlink", crash_after_unlink)

    with pytest.raises(AtomicConfigTransactionError, match="abort failed"):
        transaction.abort()
    assert abort_sync_attempts == 1
    assert unlink_attempts == 0
    assert len(_artifact_paths(tmp_path)) == 3
    assert _artifact(tmp_path, "journal.abort").is_file()

    with pytest.raises(SimulatedCrash):
        transaction.abort()
    monkeypatch.undo()

    assert abort_sync_attempts == 2
    assert unlink_attempts == 1
    assert _artifact(tmp_path, "journal.abort").is_file()
    assert _artifact(tmp_path, "rollback").is_file()
    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered == 1
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_finalize_unlink_and_directory_fsync_failures_are_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first_root = tmp_path / "unlink"
    first_root.mkdir()
    path, _, transaction = _begin_existing(first_root)
    transaction.prepare()
    transaction.commit()
    rollback_name = _artifact(first_root, "rollback").name
    original_unlink = os.unlink
    attempts = 0

    def transient_unlink(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal attempts
        if name == rollback_name:
            attempts += 1
            if attempts == 1:
                raise OSError(errno.EIO, "secret final unlink")
        original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(config_store.os, "unlink", transient_unlink)
    with pytest.raises(AtomicConfigTransactionError, match="finalize failed"):
        transaction.finalize()
    transaction.finalize()
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(first_root)

    monkeypatch.undo()
    second_root = tmp_path / "fsync"
    second_root.mkdir()
    path, _, transaction = _begin_existing(second_root)
    directory_calls = _directory_fsync_failure(monkeypatch, fail_on_call=5)
    transaction.prepare()
    transaction.commit()
    with pytest.raises(AtomicConfigTransactionError, match="finalize failed"):
        transaction.finalize()
    assert transaction.state is AtomicConfigTransactionState.COMMITTED
    assert len(_artifact_paths(second_root)) == 2
    assert _artifact(second_root, "journal.finalize").is_file()
    assert _artifact(second_root, "rollback").is_file()
    transaction.finalize()
    assert len(directory_calls) == 7
    assert transaction.state is AtomicConfigTransactionState.FINALIZED
    assert path.read_bytes() == _NEW_BYTES


@pytest.mark.parametrize("terminal", [asyncio.CancelledError, KeyboardInterrupt])
def test_prepare_terminal_exceptions_preserve_identity_and_cleanup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    terminal: type[BaseException],
) -> None:
    path, _, transaction = _begin_existing(tmp_path)

    def raise_terminal(_fd: int, _content: bytes | memoryview) -> int:
        raise terminal()

    monkeypatch.setattr(config_store.os, "write", raise_terminal)

    with pytest.raises(terminal):
        transaction.prepare()

    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_terminal_write_error_is_not_masked_by_descriptor_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    original_close = os.close
    artifact_fd: int | None = None
    close_failed = False

    def terminal_write(fd: int, _content: bytes | memoryview) -> int:
        nonlocal artifact_fd
        artifact_fd = fd
        raise asyncio.CancelledError

    def close_then_fail(fd: int) -> None:
        nonlocal close_failed
        if fd == artifact_fd and not close_failed:
            close_failed = True
            original_close(fd)
            raise OSError(errno.EIO, "secondary close failure")
        original_close(fd)

    monkeypatch.setattr(config_store.os, "write", terminal_write)
    monkeypatch.setattr(config_store.os, "close", close_then_fail)

    with pytest.raises(asyncio.CancelledError):
        transaction.prepare()

    assert close_failed is True
    assert artifact_fd is not None
    with pytest.raises(OSError) as closed:
        os.fstat(artifact_fd)
    assert closed.value.errno == errno.EBADF
    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_post_replace_terminal_directory_sync_failure_can_be_rolled_back(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    _directory_fsync_failure(
        monkeypatch,
        fail_on_call=2,
        exception_factory=asyncio.CancelledError,
    )
    transaction.prepare()

    with pytest.raises(asyncio.CancelledError):
        transaction.commit()

    assert transaction.state is AtomicConfigTransactionState.REPLACED
    assert path.read_bytes() == _NEW_BYTES
    transaction.rollback()
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)


def test_artifact_open_uses_exclusive_symlink_safe_close_on_exec_flags(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _, _, transaction = _begin_existing(tmp_path)
    original_open = os.open
    artifact_flags: list[int] = []

    def recording_open(
        name: str | bytes | os.PathLike[str] | os.PathLike[bytes],
        flags: int,
        mode: int = 0o777,
        *,
        dir_fd: int | None = None,
    ) -> int:
        if flags & os.O_EXCL:
            artifact_flags.append(flags)
        if dir_fd is None:
            return original_open(name, flags, mode)
        return original_open(name, flags, mode, dir_fd=dir_fd)

    monkeypatch.setattr(config_store.os, "open", recording_open)
    transaction.prepare()
    transaction.abort()

    assert len(artifact_flags) == 3
    for flags in artifact_flags:
        assert flags & os.O_CREAT
        assert flags & os.O_EXCL
        assert flags & os.O_NOFOLLOW
        assert flags & os.O_CLOEXEC


def test_artifact_name_exhaustion_does_not_remove_preexisting_collision(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    token = "a" * 32
    collision = tmp_path / f".llmgateway-config-txn-model_rules-{token}.candidate"
    collision.write_bytes(b"foreign-sentinel")
    monkeypatch.setattr(config_store.secrets, "token_hex", lambda _size: token)

    with pytest.raises(AtomicConfigTransactionError, match="candidate preparation"):
        transaction.prepare()

    assert transaction.state is AtomicConfigTransactionState.ABORTED
    assert path.read_bytes() == _OLD_BYTES
    assert collision.read_bytes() == b"foreign-sentinel"


def test_repr_and_wrapped_io_errors_do_not_disclose_path_payload_or_raw_message(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret_path = tmp_path / "super-secret-file-name.json"
    secret_path.write_bytes(_OLD_BYTES)
    expected = _capture_existing(secret_path)
    transaction = AtomicConfigFileTransaction.begin(expected, _NEW_BYTES)
    unsafe_error = type("Unsafe\nException", (OSError,), {})

    def fail_write(_fd: int, _content: bytes | memoryview) -> int:
        raise unsafe_error("raw-secret-message")

    monkeypatch.setattr(config_store.os, "write", fail_write)
    rendered = repr(transaction)
    assert "super-secret" not in rendered
    assert "old-value" not in rendered
    assert "new-value" not in rendered

    with pytest.raises(AtomicConfigTransactionError) as caught:
        transaction.prepare()

    rendered_error = str(caught.value)
    assert "BaseException" in rendered_error
    assert "super-secret" not in rendered_error
    assert "old-value" not in rendered_error
    assert "new-value" not in rendered_error
    assert "raw-secret-message" not in rendered_error
    _assert_no_artifacts(tmp_path)


def test_begin_rejects_noncanonical_and_reserved_transaction_target_names(
    tmp_path: Path,
) -> None:
    noncanonical = ConfigDocument.missing(
        ConfigFile.MODEL_RULES,
        tmp_path / "nested" / ".." / "rules.json",
    )
    with pytest.raises(AtomicConfigTransactionError, match="path is invalid"):
        AtomicConfigFileTransaction.begin(noncanonical, _NEW_BYTES)

    reserved = ConfigDocument.missing(
        ConfigFile.MODEL_RULES,
        tmp_path / ".llmgateway-config-txn-model_rules-target",
    )
    with pytest.raises(AtomicConfigTransactionError, match="reserved transaction prefix"):
        AtomicConfigFileTransaction.begin(reserved, _NEW_BYTES)


def test_cleanup_orphans_removes_only_exact_owned_names_and_syncs_directory(
    tmp_path: Path,
) -> None:
    target = _missing(tmp_path / "models_model_rules.json")
    owned = [
        tmp_path / f".llmgateway-config-txn-model_rules-{'a' * 32}.candidate",
        tmp_path / f".llmgateway-config-txn-model_rules-{'b' * 32}.rollback",
    ]
    for artifact in owned:
        artifact.write_bytes(b"owned")
    other_config = tmp_path / f".llmgateway-config-txn-providers-{'c' * 32}.candidate"
    other_config.write_bytes(b"other")
    ordinary = tmp_path / "ordinary.json"
    ordinary.write_bytes(b"ordinary")

    removed = AtomicConfigFileTransaction.cleanup_orphans(target)

    assert removed == 2
    assert all(not artifact.exists() for artifact in owned)
    assert other_config.read_bytes() == b"other"
    assert ordinary.read_bytes() == b"ordinary"


def test_cleanup_orphans_rejects_invalid_owned_prefix_without_partial_removal(
    tmp_path: Path,
) -> None:
    target = _missing(tmp_path / "models_model_rules.json")
    valid = tmp_path / f".llmgateway-config-txn-model_rules-{'a' * 32}.candidate"
    invalid = tmp_path / ".llmgateway-config-txn-model_rules-not-owned.candidate"
    valid.write_bytes(b"valid")
    invalid.write_bytes(b"invalid")

    with pytest.raises(AtomicConfigTransactionError, match="name is invalid"):
        AtomicConfigFileTransaction.cleanup_orphans(target)

    assert valid.read_bytes() == b"valid"
    assert invalid.read_bytes() == b"invalid"


@pytest.mark.parametrize("unsafe_kind", ["directory", "symlink", "hardlink"])
def test_cleanup_orphans_rejects_unsafe_artifact_types_without_deleting_them(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    target = _missing(tmp_path / "models_model_rules.json")
    artifact = tmp_path / f".llmgateway-config-txn-model_rules-{'a' * 32}.candidate"
    outside = tmp_path / "outside"
    outside.write_bytes(b"outside")
    if unsafe_kind == "directory":
        artifact.mkdir()
    elif unsafe_kind == "symlink":
        artifact.symlink_to(outside)
    else:
        os.link(outside, artifact)

    with pytest.raises(AtomicConfigTransactionError, match="type is unsafe"):
        AtomicConfigFileTransaction.cleanup_orphans(target)

    assert artifact.exists() or artifact.is_symlink()
    assert outside.read_bytes() == b"outside"


def test_cleanup_orphans_rejects_noncanonical_root_and_reserved_target(tmp_path: Path) -> None:
    noncanonical = ConfigDocument.missing(
        ConfigFile.MODEL_RULES,
        tmp_path / "nested" / ".." / "rules.json",
    )
    with pytest.raises(AtomicConfigTransactionError, match="root is invalid"):
        AtomicConfigFileTransaction.cleanup_orphans(noncanonical)

    reserved = ConfigDocument.missing(
        ConfigFile.MODEL_RULES,
        tmp_path / ".llmgateway-config-txn-model_rules-target",
    )
    with pytest.raises(AtomicConfigTransactionError, match="reserved transaction prefix"):
        AtomicConfigFileTransaction.cleanup_orphans(reserved)


def test_cleanup_orphan_unlink_failure_is_safe_and_retryable(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _missing(tmp_path / "models_model_rules.json")
    artifact = tmp_path / f".llmgateway-config-txn-model_rules-{'a' * 32}.candidate"
    artifact.write_bytes(b"owned")
    original_unlink = os.unlink
    attempts = 0

    def transient_unlink(name: str, *, dir_fd: int | None = None) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(errno.EIO, "secret orphan unlink")
        original_unlink(name, dir_fd=dir_fd)

    monkeypatch.setattr(config_store.os, "unlink", transient_unlink)
    with pytest.raises(AtomicConfigTransactionError, match="orphan cleanup failed"):
        AtomicConfigFileTransaction.cleanup_orphans(target)
    assert artifact.read_bytes() == b"owned"

    assert AtomicConfigFileTransaction.cleanup_orphans(target) == 1
    assert not artifact.exists()


def test_cleanup_orphan_fsync_failure_can_be_durably_retried_after_unlink(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _missing(tmp_path / "models_model_rules.json")
    artifact = tmp_path / f".llmgateway-config-txn-model_rules-{'a' * 32}.candidate"
    artifact.write_bytes(b"owned")
    original_fsync = os.fsync
    attempts = 0

    def transient_fsync(fd: int) -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise OSError(errno.EIO, "secret orphan sync")
        original_fsync(fd)

    monkeypatch.setattr(config_store.os, "fsync", transient_fsync)
    with pytest.raises(AtomicConfigTransactionError, match="orphan cleanup failed"):
        AtomicConfigFileTransaction.cleanup_orphans(target)
    assert not artifact.exists()

    assert AtomicConfigFileTransaction.cleanup_orphans(target) == 0
    assert attempts == 2
