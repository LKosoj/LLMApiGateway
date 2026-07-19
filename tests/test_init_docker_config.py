from __future__ import annotations

import errno
import os
import stat
import traceback
from pathlib import Path
from types import SimpleNamespace

import pytest

import scripts.init_docker_config as initializer
from scripts.init_docker_config import (
    CONFIG_FILENAMES,
    DockerConfigInitializationError,
    initialize_config_directory,
)


def _single_source(tmp_path: Path) -> tuple[Path, Path]:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    provider = source / "providers.json"
    provider.write_bytes(b'{"provider":"source"}\r\n')
    provider.chmod(0o640)
    return source, target


def test_initializer_cli_uses_fixed_container_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    captured_identity: dict[str, int] = {}

    def capture_identity(
        _source_dir: Path,
        _target_dir: Path,
        *,
        uid: int,
        gid: int,
    ) -> tuple[str, ...]:
        captured_identity.update(uid=uid, gid=gid)
        return ()

    monkeypatch.setattr(initializer, "initialize_config_directory", capture_identity)
    monkeypatch.setattr(
        initializer,
        "_parse_args",
        lambda: SimpleNamespace(source_dir=tmp_path, target_dir=tmp_path / "config"),
    )

    assert initializer.main() == 0
    assert captured_identity == {"uid": 10001, "gid": 10001}
    assert capsys.readouterr().out == "docker-config-init: copied=0\n"


def test_initializer_copies_only_existing_files_exactly_without_touching_sources(
    tmp_path: Path,
) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    fixtures = {
        "providers.json": b'\xef\xbb\xbf{"provider":"secret"}\r\n',
        "models_fallback_rules.json": b"[]\n",
        "models_operation_rules.json": b"{}\r\n",
    }
    source_stats: dict[str, os.stat_result] = {}
    for index, (filename, content) in enumerate(fixtures.items()):
        path = source / filename
        path.write_bytes(content)
        path.chmod(0o640 + index)
        source_stats[filename] = path.stat()

    copied = initialize_config_directory(
        source,
        target,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert copied == tuple(fixtures)
    assert stat.S_IMODE(target.stat().st_mode) == 0o750
    assert (target.stat().st_uid, target.stat().st_gid) == (os.getuid(), os.getgid())
    for filename, content in fixtures.items():
        source_path = source / filename
        target_path = target / filename
        assert source_path.read_bytes() == content
        assert target_path.read_bytes() == content
        assert stat.S_IMODE(target_path.stat().st_mode) == stat.S_IMODE(
            source_stats[filename].st_mode
        )
        assert (target_path.stat().st_uid, target_path.stat().st_gid) == (
            os.getuid(),
            os.getgid(),
        )
        assert source_path.stat().st_ino == source_stats[filename].st_ino
    assert sorted(path.name for path in target.iterdir()) == sorted(fixtures)
    assert all(not path.name.startswith(".llmgateway-config-init-") for path in target.iterdir())
    assert set(fixtures).issubset(CONFIG_FILENAMES)


def test_initializer_never_overwrites_existing_destination_bytes(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()
    target.mkdir()
    (source / "providers.json").write_bytes(b"new secret\n")
    destination = target / "providers.json"
    destination.write_bytes(b"keep exact bytes\r\n")
    destination.chmod(0o640)

    copied = initialize_config_directory(
        source,
        target,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert copied == ()
    assert destination.read_bytes() == b"keep exact bytes\r\n"
    assert stat.S_IMODE(destination.stat().st_mode) == 0o640
    assert [path.name for path in target.iterdir()] == ["providers.json"]


def test_initializer_preserves_missing_files_for_legacy_entrypoint_policy(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    source.mkdir()

    copied = initialize_config_directory(
        source,
        target,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    assert copied == ()
    assert list(target.iterdir()) == []


def test_initializer_rejects_source_symlink_without_copying_target(tmp_path: Path) -> None:
    source = tmp_path / "source"
    target = tmp_path / "target"
    outside = tmp_path / "outside.json"
    source.mkdir()
    outside.write_bytes(b"outside secret")
    (source / "providers.json").symlink_to(outside)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "source-file-not-regular"
    assert outside.read_bytes() == b"outside secret"
    assert list(target.iterdir()) == []


def test_initializer_rejects_symlink_target_directory(tmp_path: Path) -> None:
    source = tmp_path / "source"
    real_target = tmp_path / "real-target"
    linked_target = tmp_path / "target"
    source.mkdir()
    real_target.mkdir()
    linked_target.symlink_to(real_target, target_is_directory=True)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            linked_target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-directory-symlink"
    assert list(real_target.iterdir()) == []


def test_initializer_uses_linux_rename_noreplace_without_link_or_rename_fallback() -> None:
    source = Path("scripts/init_docker_config.py").read_text(encoding="utf-8")

    assert "renameat2" in source
    assert "RENAME_NOREPLACE" in source
    assert "os.link(" not in source
    assert "os.rename(" not in source


def test_initializer_publishes_the_frozen_temp_inode_and_exact_metadata(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    frozen: dict[str, int] = {}
    original_rename = initializer._rename_noreplace

    def recording_rename(root_fd: int, source_name: str, destination_name: str) -> None:
        source_stat = os.stat(source_name, dir_fd=root_fd, follow_symlinks=False)
        frozen.update(
            dev=source_stat.st_dev,
            ino=source_stat.st_ino,
            mode=stat.S_IMODE(source_stat.st_mode),
            uid=source_stat.st_uid,
            gid=source_stat.st_gid,
        )
        original_rename(root_fd, source_name, destination_name)

    monkeypatch.setattr(initializer, "_rename_noreplace", recording_rename)

    copied = initialize_config_directory(
        source,
        target,
        uid=os.getuid(),
        gid=os.getgid(),
    )

    destination_stat = (target / "providers.json").stat()
    assert copied == ("providers.json",)
    assert (
        destination_stat.st_dev,
        destination_stat.st_ino,
        stat.S_IMODE(destination_stat.st_mode),
        destination_stat.st_uid,
        destination_stat.st_gid,
    ) == (
        frozen["dev"],
        frozen["ino"],
        frozen["mode"],
        frozen["uid"],
        frozen["gid"],
    )


def test_initializer_rejects_raced_existing_target_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)

    def race_target(root_fd: int, _source_name: str, destination_name: str) -> None:
        fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.write(fd, b"concurrent-target")
        finally:
            os.close(fd)
        raise FileExistsError(errno.EEXIST, "raced target")

    monkeypatch.setattr(initializer, "_rename_noreplace", race_target)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-file-raced"
    assert (target / "providers.json").read_bytes() == b"concurrent-target"
    assert not list(target.glob(".llmgateway-config-init-*"))


@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
def test_initializer_fails_closed_when_rename_noreplace_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    source, target = _single_source(tmp_path)

    def unsupported(*_args: object) -> None:
        raise OSError(error_number, "unsupported")

    monkeypatch.setattr(initializer, "_rename_noreplace", unsupported)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-file-rename-unsupported"
    assert not (target / "providers.json").exists()
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_rejects_post_rename_destination_substitution(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    original_rename = initializer._rename_noreplace

    def substitute_target(root_fd: int, source_name: str, destination_name: str) -> None:
        original_rename(root_fd, source_name, destination_name)
        os.unlink(destination_name, dir_fd=root_fd)
        fd = os.open(
            destination_name,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.write(fd, b"substituted-target")
        finally:
            os.close(fd)

    monkeypatch.setattr(initializer, "_rename_noreplace", substitute_target)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
    )

    assert exc_info.value.reason == "target-file-verify-failed"
    assert exc_info.value.publication_uncertain is True
    assert (target / "providers.json").read_bytes() == b"substituted-target"
    assert not list(target.glob(".llmgateway-config-init-*"))


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("directory-fsync", "target-directory-sync-failed"),
        ("verify", "target-file-verify-failed"),
    ],
)
def test_initializer_post_publish_failure_preserves_uncertain_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reason: str,
) -> None:
    source, target = _single_source(tmp_path)
    original_rename = initializer._rename_noreplace
    original_fsync = initializer.os.fsync
    published = False
    target_fd: int | None = None
    fsync_failed = False

    def recording_rename(fd: int, source_name: str, destination: str) -> None:
        nonlocal published, target_fd
        original_rename(fd, source_name, destination)
        if destination == "providers.json":
            published = True
            target_fd = fd

    def injected_fsync(fd: int) -> None:
        nonlocal fsync_failed
        if fault == "directory-fsync" and published and fd == target_fd:
            if not fsync_failed:
                fsync_failed = True
                raise OSError(errno.EIO, "injected publication sync failure")
        original_fsync(fd)

    def injected_verify(_fd: int, _filename: str, _frozen: object) -> None:
        if fault == "verify":
            raise DockerConfigInitializationError("target-file-verify-failed")

    monkeypatch.setattr(initializer, "_rename_noreplace", recording_rename)
    monkeypatch.setattr(initializer.os, "fsync", injected_fsync)
    monkeypatch.setattr(initializer, "_verify_published", injected_verify)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == reason
    assert exc_info.value.publication_uncertain is True
    assert (target / "providers.json").read_bytes() == b'{"provider":"source"}\r\n'
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_failure_boundary_preserves_foreign_without_public_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    original_unlink = initializer.os.unlink
    original_open = initializer.os.open
    original_rename = initializer._rename_noreplace
    boundary_reached = False
    post_failure_renames: list[tuple[str, str]] = []
    post_failure_unlinks: list[str] = []

    def fail_after_foreign_substitution(
        target_fd: int,
        filename: str,
        _frozen: object,
    ) -> None:
        nonlocal boundary_reached
        original_unlink(filename, dir_fd=target_fd)
        foreign_fd = original_open(
            filename,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=target_fd,
        )
        try:
            os.write(foreign_fd, b"initializer-foreign-at-former-boundary")
        finally:
            os.close(foreign_fd)
        boundary_reached = True
        raise DockerConfigInitializationError("target-file-verify-failed")

    def tracking_rename(target_fd: int, source_name: str, destination: str) -> None:
        if boundary_reached:
            post_failure_renames.append((source_name, destination))
        original_rename(target_fd, source_name, destination)

    def tracking_unlink(path: object, *args: object, **kwargs: object) -> None:
        if boundary_reached:
            post_failure_unlinks.append(str(path))
        original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(
        initializer,
        "_verify_published",
        fail_after_foreign_substitution,
    )
    monkeypatch.setattr(initializer, "_rename_noreplace", tracking_rename)
    monkeypatch.setattr(initializer.os, "unlink", tracking_unlink)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-file-verify-failed"
    assert exc_info.value.publication_uncertain is True
    assert boundary_reached is True
    assert (target / "providers.json").read_bytes() == (
        b"initializer-foreign-at-former-boundary"
    )
    assert post_failure_renames == []
    assert post_failure_unlinks == []
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_post_publish_runtime_error_has_no_public_exception_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    secret = "initializer-runtime-secret-sentinel"
    original = RuntimeError(secret)

    def fail_verify(*_args: object) -> None:
        raise original

    monkeypatch.setattr(initializer, "_verify_published", fail_verify)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-file-internal-error"
    assert exc_info.value.publication_uncertain is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert exc_info.value.__suppress_context__ is True
    assert secret not in str(exc_info.value)
    assert secret not in repr(exc_info.value)
    assert secret not in "".join(traceback.format_exception(exc_info.value))
    assert (target / "providers.json").read_bytes() == b'{"provider":"source"}\r\n'
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_post_publish_control_flow_propagates_without_public_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    interrupt = KeyboardInterrupt()

    def interrupt_verify(*_args: object) -> None:
        raise interrupt

    monkeypatch.setattr(initializer, "_verify_published", interrupt_verify)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value is interrupt
    assert (target / "providers.json").read_bytes() == b'{"provider":"source"}\r\n'
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_cli_reports_uncertain_publication_without_paths_or_contents(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _single_source(tmp_path)

    def fail_verify(*_args: object) -> None:
        raise DockerConfigInitializationError("target-file-verify-failed")

    # The fixed container identity needs root to fchown; run as the current
    # user so the mocked verify failure is what main() actually reports.
    monkeypatch.setattr(initializer, "_CONTAINER_UID", os.getuid())
    monkeypatch.setattr(initializer, "_CONTAINER_GID", os.getgid())
    monkeypatch.setattr(initializer, "_verify_published", fail_verify)
    monkeypatch.setattr(
        initializer,
        "_parse_args",
        lambda: SimpleNamespace(source_dir=source, target_dir=target),
    )

    assert initializer.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "docker-config-init: reason=target-file-verify-failed "
        "publication=uncertain\n"
    )
    assert os.fspath(source) not in captured.err
    assert b'{"provider":"source"}' not in captured.err.encode()
    assert (target / "providers.json").read_bytes() == b'{"provider":"source"}\r\n'
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_cli_bounds_post_publish_runtime_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _single_source(tmp_path)
    secret = "initializer-cli-runtime-secret-sentinel"

    def fail_verify(*_args: object) -> None:
        raise RuntimeError(secret)

    # The fixed container identity needs root to fchown; run as the current
    # user so the mocked verify failure is what main() actually reports.
    monkeypatch.setattr(initializer, "_CONTAINER_UID", os.getuid())
    monkeypatch.setattr(initializer, "_CONTAINER_GID", os.getgid())
    monkeypatch.setattr(initializer, "_verify_published", fail_verify)
    monkeypatch.setattr(
        initializer,
        "_parse_args",
        lambda: SimpleNamespace(source_dir=source, target_dir=target),
    )

    assert initializer.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "docker-config-init: reason=target-file-internal-error "
        "publication=uncertain\n"
    )
    assert secret not in captured.err
    assert os.fspath(source) not in captured.err
    assert b'{"provider":"source"}' not in captured.err.encode()
    assert (target / "providers.json").exists()
    assert not list(target.glob(".llmgateway-config-init-*"))


def test_initializer_cli_bounds_pre_publish_runtime_error_without_marker(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    source, target = _single_source(tmp_path)
    secret = "initializer-prepublish-runtime-secret-sentinel"

    def fail_before_publish(*_args: object, **_kwargs: object) -> tuple[str, ...]:
        raise RuntimeError(secret)

    monkeypatch.setattr(initializer, "initialize_config_directory", fail_before_publish)
    monkeypatch.setattr(
        initializer,
        "_parse_args",
        lambda: SimpleNamespace(source_dir=source, target_dir=target),
    )

    assert initializer.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "docker-config-init: reason=internal-error\n"
    assert "publication=" not in captured.err
    assert secret not in captured.err
    assert os.fspath(source) not in captured.err


def test_initializer_next_run_validates_uncertain_existing_name_without_overwrite(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source, target = _single_source(tmp_path)
    original_rename = initializer._rename_noreplace
    original_fsync = initializer.os.fsync
    target_fd: int | None = None
    failed = False
    provider_publications = 0

    def recording_rename(fd: int, source_name: str, destination: str) -> None:
        nonlocal target_fd, provider_publications
        original_rename(fd, source_name, destination)
        if destination == "providers.json":
            target_fd = fd
            provider_publications += 1

    def fail_first_post_publish_sync(fd: int) -> None:
        nonlocal failed
        if fd == target_fd and not failed:
            failed = True
            raise OSError(errno.EIO, "injected ambiguous publication sync")
        original_fsync(fd)

    monkeypatch.setattr(initializer, "_rename_noreplace", recording_rename)
    monkeypatch.setattr(initializer.os, "fsync", fail_first_post_publish_sync)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == "target-directory-sync-failed"
    assert exc_info.value.publication_uncertain is True
    published = target / "providers.json"
    first_stat = published.stat()
    first_bytes = published.read_bytes()

    assert initialize_config_directory(
        source,
        target,
        uid=os.getuid(),
        gid=os.getgid(),
    ) == ()

    assert provider_publications == 1
    assert published.read_bytes() == first_bytes
    assert published.stat().st_ino == first_stat.st_ino
    assert not list(target.glob(".llmgateway-config-init-*"))


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("write", "target-file-write-failed"),
        ("fsync", "target-file-sync-failed"),
        ("close", "target-file-close-failed"),
        ("rename", "target-file-rename-failed"),
        ("cleanup", "target-file-rename-failed"),
    ],
)
def test_initializer_faults_preserve_primary_reason_without_double_close_or_temp_artifacts(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reason: str,
) -> None:
    source, target = _single_source(tmp_path)
    original_close = initializer.os.close
    original_fsync = initializer.os.fsync
    original_unlink = initializer.os.unlink
    original_rename = initializer._rename_noreplace
    close_fault_injected = False
    regular_close_count = 0
    unlink_fault_injected = False

    def tracking_close(fd: int) -> None:
        nonlocal close_fault_injected, regular_close_count
        try:
            is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
        except OSError as exc:
            raise AssertionError("attempted to close an unowned fd") from exc
        if is_regular:
            regular_close_count += 1
        original_close(fd)
        if fault == "close" and regular_close_count == 2 and not close_fault_injected:
            close_fault_injected = True
            raise OSError(errno.EIO, "injected close failure")

    def injected_fsync(fd: int) -> None:
        if fault == "fsync" and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected fsync failure")
        original_fsync(fd)

    def injected_rename(*_args: object) -> None:
        if fault in {"rename", "cleanup"}:
            raise OSError(errno.EACCES, "injected rename failure")
        original_rename(*_args)

    def injected_unlink(*args: object, **kwargs: object) -> None:
        nonlocal unlink_fault_injected
        if fault == "cleanup" and not unlink_fault_injected:
            unlink_fault_injected = True
            raise OSError(errno.EACCES, "injected unlink failure")
        original_unlink(*args, **kwargs)

    if fault == "write":
        monkeypatch.setattr(
            initializer,
            "_write_all",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, "write")),
        )
    monkeypatch.setattr(initializer.os, "close", tracking_close)
    monkeypatch.setattr(initializer.os, "fsync", injected_fsync)
    monkeypatch.setattr(initializer, "_rename_noreplace", injected_rename)
    monkeypatch.setattr(initializer.os, "unlink", injected_unlink)

    with pytest.raises(DockerConfigInitializationError) as exc_info:
        initialize_config_directory(
            source,
            target,
            uid=os.getuid(),
            gid=os.getgid(),
        )

    assert exc_info.value.reason == reason
    assert not list(target.glob(".llmgateway-config-init-*"))
