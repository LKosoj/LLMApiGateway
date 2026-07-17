"""Regression tests for the config-transaction remediation pass.

One (or a small focused group of) tests per fix:
- Item 1: the missing-target/external-artifact recovery branch in
  ``_recover_missing_target_restore`` cleans up and returns instead of
  falling through to an unconditional "ambiguous" raise (the startup
  poison pill), and a second recovery pass is a clean no-op.
- Item 2: ``AtomicConfigFileTransaction.begin`` accepts a 0o644 new-file
  mode (matching container_preflight.py's convention) while still
  rejecting genuinely insecure group/other write or execute bits.
- Item 3: a denied ``os.fchown`` during artifact preparation is
  best-effort (logged, not fatal).
- Item 4: the ``renameat2`` libc symbol is resolved once and cached.
- Item 5: a symlinked ancestor directory no longer fails config capture,
  and a hard-linked config source is a warning, not a fatal error; the
  final-file symlink-swap protection is unchanged.
- Item 6: an explicit LLMGATEWAY_ENV_FILE accepts the same escape and
  interpolation syntax as the default project .env file, while still
  rejecting duplicate keys, null bytes, and malformed lines.
- Item 7: structured config-editor validation failures carry
  credential-safe error detail (field path), and the JSON envelope only
  gains an "errors" key when that detail is present.
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest
from pydantic import BaseModel, ConfigDict, ValidationError

import llm_gateway_core.config.atomic_config_transaction as atomic_config_transaction
from llm_gateway_core.config.config_store import (
    AtomicConfigFileTransaction,
    ConfigDocument,
    ConfigFile,
    ConfigFileMetadata,
    ConfigSourceBundle,
    ConfigSourceError,
)
from llm_gateway_core.config import environment
from llm_gateway_core.api.v1.config_update_http import config_error_response
from llm_gateway_core.api.v1.rules_editor import (
    _safe_validation_errors,
    _validation_failed,
)
from llm_gateway_core.services.config_updates import (
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)


_ARTIFACT_GLOB = ".llmgateway-config-txn-*"


def _artifact_paths(parent: Path) -> list[Path]:
    return sorted(parent.glob(_ARTIFACT_GLOB))


def _artifact(parent: Path, kind: str) -> Path:
    matches = [path for path in _artifact_paths(parent) if path.name.endswith(f".{kind}")]
    assert len(matches) == 1
    return matches[0]


# --------------------------------------------------------------------------
# Item 1: poison-pill recovery (missing target, external/mismatched artifact)
# --------------------------------------------------------------------------


def test_missing_target_external_artifact_recovery_cleans_up_and_is_idempotent(
    tmp_path: Path,
) -> None:
    path = tmp_path / "models_model_rules.json"
    expected = ConfigDocument.missing(ConfigFile.MODEL_RULES, path)
    candidate_bytes = b'{"candidate":"value"}\n'
    transaction = AtomicConfigFileTransaction.begin(expected, candidate_bytes)
    transaction.prepare()
    candidate = _artifact(tmp_path, "candidate")
    journal = _artifact(tmp_path, "journal.prepared")

    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        # Simulate a completed no-replace commit: candidate -> target name.
        atomic_config_transaction._renameat2(
            candidate.name,
            path.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=atomic_config_transaction._RENAME_NOREPLACE,
        )
        # Simulate having already reached the "restore_move" journal phase
        # (the on-disk state a crash mid-rollback would leave behind).
        restore_move_name = journal.name.replace(
            ".journal.prepared", ".journal.restore_move"
        )
        atomic_config_transaction._renameat2(
            journal.name,
            restore_move_name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=atomic_config_transaction._RENAME_NOREPLACE,
        )
        # Simulate the move-back from rollback()'s missing-original branch.
        atomic_config_transaction._renameat2(
            path.name,
            candidate.name,
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=atomic_config_transaction._RENAME_NOREPLACE,
        )
    finally:
        os.close(parent_fd)

    # Simulate an external race: something else replaced the file sitting at
    # the candidate name after the move-back, so it no longer matches the
    # journal's own recorded candidate identity.
    external_bytes = b'{"external":"after-crash"}\n'
    (tmp_path / candidate.name).write_bytes(external_bytes)
    assert not path.exists()

    recovered = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )

    assert recovered == 1
    assert path.read_bytes() == external_bytes
    assert _artifact_paths(tmp_path) == []

    # Idempotency: startup recovery must not need manual journal deletion.
    recovered_again = AtomicConfigFileTransaction.recover_pending(
        {ConfigFile.MODEL_RULES: path}
    )
    assert recovered_again == 0
    assert path.read_bytes() == external_bytes
    del transaction


# --------------------------------------------------------------------------
# Item 2: new-file mode policy ("боль 600")
# --------------------------------------------------------------------------


def test_begin_accepts_and_applies_0o644_new_file_mode(tmp_path: Path) -> None:
    path = tmp_path / "providers.json"
    expected = ConfigDocument.missing(ConfigFile.PROVIDERS, path)
    transaction = AtomicConfigFileTransaction.begin(
        expected,
        b'{"providers":[]}\n',
        new_file_mode=0o644,
    )
    transaction.prepare()
    transaction.commit()

    assert path.stat().st_mode & 0o777 == 0o644


@pytest.mark.parametrize("insecure_mode", [0o646, 0o664, 0o600 | 0o100])
def test_begin_still_rejects_insecure_new_file_mode(
    tmp_path: Path,
    insecure_mode: int,
) -> None:
    path = tmp_path / "providers.json"
    expected = ConfigDocument.missing(ConfigFile.PROVIDERS, path)
    with pytest.raises(ValueError, match="new_file_mode must be a secure mode"):
        AtomicConfigFileTransaction.begin(
            expected,
            b"{}\n",
            new_file_mode=insecure_mode,
        )


def test_config_updates_begin_call_site_uses_0o644() -> None:
    from llm_gateway_core.services import config_updates

    assert config_updates._NEW_CONFIG_FILE_MODE == 0o644


# --------------------------------------------------------------------------
# Item 3: best-effort fchown
# --------------------------------------------------------------------------


def test_prepare_survives_denied_fchown(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    path = tmp_path / "models_model_rules.json"
    path.write_bytes(b'{"old":true}\n')
    path.chmod(0o640)
    source_stat = path.stat()
    expected = ConfigDocument.from_bytes(
        ConfigFile.MODEL_RULES,
        path,
        path.read_bytes(),
        metadata=ConfigFileMetadata.from_stat(source_stat),
    )

    def denied_fchown(fd: int, uid: int, gid: int) -> None:
        raise PermissionError("fchown not permitted")

    monkeypatch.setattr(atomic_config_transaction.os, "fchown", denied_fchown)

    transaction = AtomicConfigFileTransaction.begin(expected, b'{"new":true}\n')
    with caplog.at_level(logging.WARNING):
        transaction.prepare()  # must not raise despite the denied fchown

    assert "fchown" in caplog.text
    transaction.commit()
    assert path.read_bytes() == b'{"new":true}\n'


# --------------------------------------------------------------------------
# Item 4: cached renameat2 syscall resolution
# --------------------------------------------------------------------------


def test_renameat2_syscall_is_resolved_once_and_reused(tmp_path: Path) -> None:
    atomic_config_transaction._renameat2_syscall = None
    atomic_config_transaction._renameat2_unavailable = False

    first = tmp_path / "a"
    second = tmp_path / "b"
    first.write_bytes(b"one")
    second.write_bytes(b"two")
    parent_fd = os.open(tmp_path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        atomic_config_transaction._renameat2(
            "a",
            "a-renamed",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=atomic_config_transaction._RENAME_NOREPLACE,
        )
        resolved_once = atomic_config_transaction._renameat2_syscall
        assert resolved_once is not None

        atomic_config_transaction._renameat2(
            "b",
            "b-renamed",
            src_dir_fd=parent_fd,
            dst_dir_fd=parent_fd,
            flags=atomic_config_transaction._RENAME_NOREPLACE,
        )
        assert atomic_config_transaction._renameat2_syscall is resolved_once
    finally:
        os.close(parent_fd)

    assert (tmp_path / "a-renamed").read_bytes() == b"one"
    assert (tmp_path / "b-renamed").read_bytes() == b"two"


# --------------------------------------------------------------------------
# Item 5: symlinked directory components / hard-linked source
# --------------------------------------------------------------------------


def test_capture_follows_symlinked_ancestor_directory(tmp_path: Path) -> None:
    (tmp_path / "providers.json").write_bytes(b"{}\n")
    (tmp_path / "models_fallback_rules.json").write_bytes(b"[]\n")
    real_parent = tmp_path / "real"
    real_parent.mkdir()
    (real_parent / "models_model_rules.json").write_bytes(b'{"a":1}\n')
    linked = tmp_path / "linked"
    linked.symlink_to(real_parent, target_is_directory=True)

    bundle = ConfigSourceBundle.capture(
        tmp_path,
        overrides={ConfigFile.MODEL_RULES: "linked/models_model_rules.json"},
    )

    assert bundle[ConfigFile.MODEL_RULES].content == b'{"a":1}\n'


def test_capture_warns_instead_of_rejecting_hard_linked_source(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    providers = tmp_path / "providers.json"
    providers.write_bytes(b"{}\n")
    fallback = tmp_path / "models_fallback_rules.json"
    fallback.write_bytes(b"[]\n")
    # Hard-link to a file outside the tracked config-file set so this only
    # exercises the single-file nlink warning, not the unrelated (and still
    # fatal) cross-config-file inode-collision guard in capture().
    external = tmp_path / "external-hardlink-source.json"
    external.write_bytes(b'{"linked":true}\n')
    model_rules = tmp_path / "models_model_rules.json"
    os.link(external, model_rules)

    with caplog.at_level(logging.WARNING):
        bundle = ConfigSourceBundle.capture(tmp_path)

    assert bundle[ConfigFile.MODEL_RULES].content == b'{"linked":true}\n'
    assert "hard-linked" in caplog.text


def test_capture_still_rejects_final_symlink_swap(tmp_path: Path) -> None:
    providers = tmp_path / "providers.json"
    providers.write_bytes(b"{}\n")
    fallback = tmp_path / "models_fallback_rules.json"
    fallback.write_bytes(b"[]\n")
    target = tmp_path / "target.json"
    target.write_bytes(b'{"outside":true}\n')
    (tmp_path / "models_model_rules.json").symlink_to(target)

    with pytest.raises(ConfigSourceError, match="path is unsafe"):
        ConfigSourceBundle.capture(tmp_path)


# --------------------------------------------------------------------------
# Item 6: LLMGATEWAY_ENV_FILE / default .env parity
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_environment_loader() -> None:
    environment.load_application_environment.cache_clear()
    yield
    environment.load_application_environment.cache_clear()


def test_explicit_env_file_accepts_dotenv_escapes_and_interpolation(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    explicit = tmp_path / "runtime.env"
    explicit.write_text(
        'R35_BASE=base-value\n'
        'R35_ESCAPED="line1\\nline2"\n'
        "R35_INTERPOLATED=${R35_BASE}/suffix\n",
        encoding="utf-8",
    )
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))
    for key in ("R35_BASE", "R35_ESCAPED", "R35_INTERPOLATED"):
        monkeypatch.delenv(key, raising=False)

    selected = environment.load_application_environment()

    assert selected == explicit
    assert os.environ["R35_BASE"] == "base-value"
    assert os.environ["R35_ESCAPED"] == "line1\nline2"
    assert os.environ["R35_INTERPOLATED"] == "base-value/suffix"


@pytest.mark.parametrize(
    ("payload", "reason"),
    [
        ("R35_EARLY=first\n'broken\n", "parse-failed"),
        ("R35_EARLY=first\nR35_BAD=secret\x00value\n", "value-invalid"),
        ("R35_DUPLICATE=first\nR35_DUPLICATE=second\n", "duplicate-key"),
    ],
)
def test_explicit_env_file_still_rejects_strict_grammar_violations(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    payload: str,
    reason: str,
) -> None:
    explicit = tmp_path / "runtime.env"
    explicit.write_text(payload, encoding="utf-8")
    monkeypatch.setenv(environment.ENVIRONMENT_FILE_KEY, os.fspath(explicit))
    for key in ("R35_EARLY", "R35_BAD", "R35_DUPLICATE"):
        monkeypatch.delenv(key, raising=False)
    before = dict(os.environ)

    with pytest.raises(environment.EnvironmentFileError) as caught:
        environment.load_application_environment()

    assert caught.value.reason == reason
    assert dict(os.environ) == before


# --------------------------------------------------------------------------
# Item 7: informative validation-error detail
# --------------------------------------------------------------------------


class _SecretModel(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str


def test_safe_validation_errors_excludes_raw_input_and_context() -> None:
    try:
        _SecretModel.model_validate({"name": "ok", "api_key": "must-not-leak"})
    except ValidationError as exc:
        errors = _safe_validation_errors(exc)
        update_error = _validation_failed(exc)
    else:
        raise AssertionError("expected ValidationError")

    assert len(errors) == 1
    assert errors[0]["type"] == "extra_forbidden"
    assert errors[0]["loc"] == ["api_key"]
    assert set(errors[0]) == {"type", "loc", "msg"}
    assert "must-not-leak" not in repr(errors)

    assert update_error.code is ConfigUpdateErrorCode.VALIDATION_FAILED
    assert update_error.errors == errors


def test_validation_failed_without_exception_has_no_errors() -> None:
    update_error = _validation_failed()

    assert update_error.errors is None


def test_config_error_response_includes_errors_when_present() -> None:
    try:
        _SecretModel.model_validate({"name": "ok", "api_key": "must-not-leak"})
    except ValidationError as exc:
        update_error = _validation_failed(exc)
    else:
        raise AssertionError("expected ValidationError")

    response = config_error_response(update_error)

    assert response.status_code == 400
    assert b"must-not-leak" not in response.body
    body = response.body.decode("utf-8")
    assert '"errors"' in body
    assert '"loc":["api_key"]' in body.replace(" ", "")


def test_config_error_response_omits_errors_key_when_absent() -> None:
    response = config_error_response(
        ConfigUpdateError(ConfigUpdateErrorCode.VALIDATION_FAILED)
    )

    assert response.status_code == 400
    assert response.body == (
        b'{"detail":{"code":"config_validation_failed",'
        b'"message":"Configuration validation failed."}}'
    )
