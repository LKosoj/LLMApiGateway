"""Shared helpers for atomic configuration transaction tests."""

from __future__ import annotations

import errno
import os
import stat
from pathlib import Path
from typing import Callable

import pytest

import llm_gateway_core.config.atomic_config_transaction as config_store
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    ConfigDocument,
    ConfigFile,
    ConfigFileMetadata,
)


_ARTIFACT_GLOB = ".llmgateway-config-txn-*"
_OLD_BYTES = b'{"secret":"old-value"}\n'
_NEW_BYTES = b'{"secret":"new-value"}\r\n'


def _capture_existing(path: Path) -> ConfigDocument:
    source_stat = path.stat()
    return ConfigDocument.from_bytes(
        ConfigFile.MODEL_RULES,
        path,
        path.read_bytes(),
        metadata=ConfigFileMetadata.from_stat(source_stat),
    )


def _missing(path: Path) -> ConfigDocument:
    return ConfigDocument.missing(ConfigFile.MODEL_RULES, path)


def _existing_target(
    tmp_path: Path,
    *,
    content: bytes = _OLD_BYTES,
    mode: int = 0o640,
) -> tuple[Path, ConfigDocument]:
    path = tmp_path / "models_model_rules.json"
    path.write_bytes(content)
    path.chmod(mode)
    return path, _capture_existing(path)


def _artifact_paths(parent: Path) -> list[Path]:
    return sorted(parent.glob(_ARTIFACT_GLOB))


def _assert_no_artifacts(parent: Path) -> None:
    assert _artifact_paths(parent) == []


def _artifact(parent: Path, kind: str) -> Path:
    matches = [path for path in _artifact_paths(parent) if path.name.endswith(f".{kind}")]
    assert len(matches) == 1
    return matches[0]


def _tamper_artifact(artifact: Path, tamper: str, outside: Path) -> None:
    if tamper == "mutated_regular":
        artifact.write_bytes(b"tampered-artifact")
        return

    artifact.unlink()
    if tamper == "symlink":
        artifact.symlink_to(outside)
    elif tamper == "hardlink":
        os.link(outside, artifact)
    else:
        os.mkfifo(artifact)


def _begin_existing(
    tmp_path: Path,
) -> tuple[Path, ConfigDocument, AtomicConfigFileTransaction]:
    path, expected = _existing_target(tmp_path)
    transaction = AtomicConfigFileTransaction.begin(expected, _NEW_BYTES)
    return path, expected, transaction


def _directory_fsync_failure(
    monkeypatch: pytest.MonkeyPatch,
    *,
    fail_on_call: int,
    exception_factory: Callable[[], BaseException] | None = None,
) -> list[int]:
    original_fsync = os.fsync
    directory_calls: list[int] = []

    def injected_fsync(fd: int) -> None:
        if stat.S_ISDIR(os.fstat(fd).st_mode):
            directory_calls.append(fd)
            if len(directory_calls) == fail_on_call:
                if exception_factory is not None:
                    raise exception_factory()
                raise OSError(errno.EIO, "sensitive directory failure")
        original_fsync(fd)

    monkeypatch.setattr(config_store.os, "fsync", injected_fsync)
    return directory_calls
