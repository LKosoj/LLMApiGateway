from __future__ import annotations

import errno
import os
from pathlib import Path

import pytest

import llm_gateway_core.config.atomic_config_transaction as config_store
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    AtomicConfigTransactionIntegrityError,
    ConfigFile,
)
from tests.atomic_config_test_support import (
    _NEW_BYTES,
    _OLD_BYTES,
    _artifact,
    _artifact_paths,
    _assert_no_artifacts,
    _begin_existing,
    _missing,
)


def test_startup_recovery_removes_durable_no_exchange_transaction(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    assert _artifact(tmp_path, "journal.prepared").is_file()

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_rejects_multiple_journals_without_mutation(
    tmp_path: Path,
) -> None:
    path, expected, first = _begin_existing(tmp_path)
    second = AtomicConfigFileTransaction.begin(expected, b'{"second":true}\n')
    first.prepare()
    second.prepare()
    target_before = (path.read_bytes(), path.stat().st_ino)
    artifacts_before = {
        artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
        for artifact in _artifact_paths(tmp_path)
    }

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )

    assert (path.read_bytes(), path.stat().st_ino) == target_before
    assert {
        artifact.name: (artifact.read_bytes(), artifact.stat().st_ino)
        for artifact in _artifact_paths(tmp_path)
    } == artifacts_before
    del first, second


def test_startup_recovery_rejects_canonical_parent_swap_without_new_root_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    config_root = tmp_path / "config"
    config_root.mkdir()
    path, _, transaction = _begin_existing(config_root)
    transaction.prepare()
    journal = _artifact(config_root, "journal.prepared")
    displaced_root = tmp_path / "displaced"
    external_bytes = b'{"external":"new-root"}\n'
    external_identity: tuple[bytes, int] | None = None
    original_renameat2 = config_store._renameat2

    def racing_renameat2(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal external_identity
        if external_identity is None and source == journal.name:
            config_root.rename(displaced_root)
            config_root.mkdir()
            external = config_root / path.name
            external.write_bytes(external_bytes)
            external_identity = (external.read_bytes(), external.stat().st_ino)
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )

    monkeypatch.setattr(config_store, "_renameat2", racing_renameat2)

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )

    assert external_identity is not None
    external = config_root / path.name
    assert (external.read_bytes(), external.stat().st_ino) == external_identity
    del transaction


def test_startup_recovery_accepts_crash_after_successful_exchange(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_resumes_commit_cleanup_after_rollback_unlink_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    rollback = _artifact(tmp_path, "rollback")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)
    original_unlink = os.unlink
    crashed = False

    def crash_after_rollback_unlink(
        name: str,
        *,
        dir_fd: int | None = None,
    ) -> None:
        nonlocal crashed
        original_unlink(name, dir_fd=dir_fd)
        if name == rollback.name and not crashed:
            crashed = True
            raise OSError(errno.EIO, "crash after rollback unlink")

    monkeypatch.setattr(config_store.os, "unlink", crash_after_rollback_unlink)
    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )
    monkeypatch.undo()

    assert crashed is True
    assert path.read_bytes() == _NEW_BYTES
    assert _artifact(tmp_path, "journal.finalize").is_file()
    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered == 1
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_exchanges_external_displaced_target_back(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    external = tmp_path / "external"
    external_bytes = b'{"external":"crash-race"}\n'
    external.write_bytes(external_bytes)
    os.replace(external, path)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == external_bytes
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_restore_resumes_after_external_compensation_exchange_crash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    external = tmp_path / "external"
    external_bytes = b'{"external":"compensation-crash"}\n'
    external.write_bytes(external_bytes)
    os.replace(external, path)
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)
    original_renameat2 = config_store._renameat2
    crashed = False

    def crash_after_compensation_exchange(
        source: str,
        target: str,
        *,
        src_dir_fd: int,
        dst_dir_fd: int,
        flags: int,
    ) -> None:
        nonlocal crashed
        original_renameat2(
            source,
            target,
            src_dir_fd=src_dir_fd,
            dst_dir_fd=dst_dir_fd,
            flags=flags,
        )
        if (
            not crashed
            and source == candidate.name
            and target == path.name
            and flags == config_store._RENAME_EXCHANGE
        ):
            crashed = True
            raise OSError(errno.EIO, "crash after compensation exchange")

    monkeypatch.setattr(config_store, "_renameat2", crash_after_compensation_exchange)
    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )
    monkeypatch.undo()

    assert crashed is True
    assert path.read_bytes() == external_bytes
    assert _artifact(tmp_path, "candidate").read_bytes() == _NEW_BYTES
    assert _artifact(tmp_path, "journal.restore").is_file()
    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered == 1
    assert path.read_bytes() == external_bytes
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_accepts_crash_after_missing_target_noreplace(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_rejects_partial_journal_without_mutation(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    journal = _artifact(tmp_path, "journal.prepared")
    journal.write_bytes(b'{"version":')
    before = {
        artifact.name: artifact.read_bytes()
        for artifact in _artifact_paths(tmp_path)
    }

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )

    assert path.read_bytes() == _OLD_BYTES
    assert {
        artifact.name: artifact.read_bytes()
        for artifact in _artifact_paths(tmp_path)
    } == before
    del transaction


def test_startup_recovery_accepts_file_commit_before_runtime_publish(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    assert _artifact(tmp_path, "journal.commit").is_file()

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_partial_abort_after_commit_conflict(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    journal = _artifact(tmp_path, "journal.prepared")
    external = tmp_path / "external-conflict"
    external_bytes = b'{"external":"abort-crash"}\n'
    external.write_bytes(external_bytes)
    os.replace(external, path)
    abort = journal.with_name(
        journal.name.removesuffix(".journal.prepared") + ".journal.abort"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            abort.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
        candidate.unlink()
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == external_bytes
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_finalize_after_rollback_was_deleted(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    rollback = _artifact(tmp_path, "rollback")
    journal = _artifact(tmp_path, "journal.commit")
    finalize = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.finalize"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            finalize.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
        rollback.unlink()
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _NEW_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_existing_rollback_after_exchange(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    rollback = _artifact(tmp_path, "rollback")
    restore_name = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore_name.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
        config_store._renameat2(
            rollback.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_existing_rollback_before_exchange(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    restore = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == _OLD_BYTES
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_restores_external_displaced_during_rollback(
    tmp_path: Path,
) -> None:
    path, _, transaction = _begin_existing(tmp_path)
    transaction.prepare()
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    rollback = _artifact(tmp_path, "rollback")
    external = tmp_path / "external-rollback"
    external_bytes = b'{"external":"rollback-race"}\n'
    external.write_bytes(external_bytes)
    os.replace(external, path)
    restore_name = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore_name.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
        config_store._renameat2(
            rollback.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_EXCHANGE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == external_bytes
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_missing_target_rollback_after_move(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    restore = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore_move"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
        config_store._renameat2(
            path.name,
            candidate.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert not path.exists()
    _assert_no_artifacts(tmp_path)
    del transaction


def test_startup_recovery_finishes_missing_target_rollback_before_move(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    restore = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore_move"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert not path.exists()
    _assert_no_artifacts(tmp_path)
    del transaction


@pytest.mark.parametrize("external_location", ["canonical", "candidate"])
def test_startup_missing_target_restore_preserves_ambiguous_external_bytes(
    tmp_path: Path,
    external_location: str,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    restore = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    external_bytes = b'{"external":"must-survive"}\n'
    if external_location == "canonical":
        external = tmp_path / "external"
        external.write_bytes(external_bytes)
        os.replace(external, path)
        external_path = path
    else:
        path.unlink()
        candidate.write_bytes(external_bytes)
        external_path = candidate
    before = {
        artifact.name: artifact.read_bytes()
        for artifact in _artifact_paths(tmp_path)
    }

    with pytest.raises(AtomicConfigTransactionIntegrityError):
        AtomicConfigFileTransaction.recover_pending(
            {ConfigFile.MODEL_RULES: path}
        )

    assert external_path.read_bytes() == external_bytes
    assert {
        artifact.name: artifact.read_bytes()
        for artifact in _artifact_paths(tmp_path)
    } == before
    del transaction


def test_startup_missing_target_restore_recovers_racing_external_without_raising(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = tmp_path / "models_model_rules.json"
    transaction = AtomicConfigFileTransaction.begin(_missing(path), _NEW_BYTES)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    transaction.commit()
    journal = _artifact(tmp_path, "journal.commit")
    restore = journal.with_name(
        journal.name.removesuffix(".journal.commit") + ".journal.restore"
    )
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        config_store._renameat2(
            journal.name,
            restore.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=config_store._RENAME_NOREPLACE,
        )
        os.fsync(parent_fd)
    finally:
        os.close(parent_fd)
    external = tmp_path / "external-race"
    external_bytes = b'{"external":"canonical-writer"}\n'
    external.write_bytes(external_bytes)
    external_inode = external.stat().st_ino
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
            and source == path.name
            and target == candidate.name
            and flags == config_store._RENAME_NOREPLACE
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

    # The external writer's replace races the internal restore_move rename,
    # so the candidate slot ends up holding the external content instead of
    # the recorded candidate identity. Recovery must recognize this as an
    # already-resolved external race, restore it to the canonical path, and
    # return normally instead of raising the old unconditional "ambiguous"
    # poison pill (which used to leave a journal behind forever).
    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert raced is True
    assert recovered == 1
    assert path.read_bytes() == external_bytes
    assert path.stat().st_ino == external_inode
    assert not candidate.exists()
    _assert_no_artifacts(tmp_path)

    # Idempotency: a second startup recovery pass must be a clean no-op,
    # never requiring manual journal deletion to bring the app back up.
    recovered_again = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered_again == 0
    assert path.read_bytes() == external_bytes
    del transaction
