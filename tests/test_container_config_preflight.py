from __future__ import annotations

import errno
import os
import stat
import traceback
from collections.abc import Callable
from pathlib import Path

import pytest

import llm_gateway_core.config.container_preflight as preflight
from llm_gateway_core.config.config_store import ConfigFile, ConfigSourceError


FILENAMES = {
    ConfigFile.PROVIDERS: "providers.json",
    ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
    ConfigFile.OPERATION_RULES: "models_operation_rules.json",
    ConfigFile.FUSION_RULES: "models_fusion_rules.json",
    ConfigFile.MODEL_RULES: "models_model_rules.json",
    ConfigFile.ROUTER_RULES: "models_router_rules.json",
}
FILENAME_ENV = {
    ConfigFile.PROVIDERS: "PROVIDERS_FILENAME",
    ConfigFile.FALLBACK_RULES: "FALLBACK_RULES_FILENAME",
    ConfigFile.OPERATION_RULES: "OPERATION_RULES_FILENAME",
    ConfigFile.FUSION_RULES: "FUSION_RULES_FILENAME",
    ConfigFile.MODEL_RULES: "MODEL_RULES_FILENAME",
    ConfigFile.ROUTER_RULES: "ROUTER_RULES_FILENAME",
}
OPTIONAL_CONTENT = {
    ConfigFile.OPERATION_RULES: b"{}\n",
    ConfigFile.MODEL_RULES: b"{}\n",
    ConfigFile.FUSION_RULES: b"[]\n",
    ConfigFile.ROUTER_RULES: b"[]\n",
}


def _paths(root: Path) -> dict[ConfigFile, Path]:
    return {config_file: root / filename for config_file, filename in FILENAMES.items()}


def _write_mandatory(root: Path) -> None:
    (root / FILENAMES[ConfigFile.PROVIDERS]).write_bytes(b"{}\n")
    (root / FILENAMES[ConfigFile.FALLBACK_RULES]).write_bytes(b"[]\n")


def _prepare(root: Path, *, app_root: Path | None = None) -> None:
    preflight.prepare_container_config(app_root or root.parent, root, _paths(root))


def _configure_cli(
    root: Path,
    monkeypatch: pytest.MonkeyPatch,
    *,
    app_root: Path | None = None,
) -> None:
    monkeypatch.setenv("APP_DIR", os.fspath(app_root or root.parent))
    monkeypatch.setenv("LLMGATEWAY_CONFIG_DIR", os.fspath(root))
    for config_file, filename in FILENAMES.items():
        monkeypatch.setenv(FILENAME_ENV[config_file], os.fspath(root / filename))


def test_preflight_executes_real_write_sync_rename_unlink_and_leaves_no_artifact(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    calls: list[str] = []
    original_fsync = preflight.os.fsync
    original_rename = preflight.os.rename
    original_unlink = preflight.os.unlink

    def recording_fsync(fd: int) -> None:
        calls.append("dir-fsync" if os.path.isdir(f"/proc/self/fd/{fd}") else "file-fsync")
        original_fsync(fd)

    def recording_rename(*args: object, **kwargs: object) -> None:
        calls.append("rename")
        original_rename(*args, **kwargs)

    def recording_unlink(*args: object, **kwargs: object) -> None:
        calls.append("unlink")
        original_unlink(*args, **kwargs)

    monkeypatch.setattr(preflight.os, "fsync", recording_fsync)
    monkeypatch.setattr(preflight.os, "rename", recording_rename)
    monkeypatch.setattr(preflight.os, "unlink", recording_unlink)

    preflight.check_config_directory(root, _paths(root))

    assert calls == ["file-fsync", "rename", "dir-fsync", "unlink", "dir-fsync"]
    assert list(root.iterdir()) == []


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("write", "probe-write-failed"),
        ("fsync", "probe-sync-failed"),
        ("close", "probe-close-failed"),
        ("rename", "probe-rename-failed"),
        ("unlink", "probe-unlink-failed"),
        ("cleanup", "probe-rename-failed"),
    ],
)
def test_preflight_faults_preserve_reason_close_each_fd_once_and_clean_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reason: str,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    original_open = preflight.os.open
    original_close = preflight.os.close
    original_fsync = preflight.os.fsync
    original_rename = preflight.os.rename
    original_unlink = preflight.os.unlink
    open_tokens: dict[int, int] = {}
    next_open_token = 0
    close_fault_injected = False
    unlink_fault_injected = False

    def tracking_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal next_open_token
        fd = original_open(path, flags, *args, **kwargs)
        assert fd not in open_tokens
        next_open_token += 1
        open_tokens[fd] = next_open_token
        return fd

    def tracking_close(fd: int) -> None:
        nonlocal close_fault_injected
        assert open_tokens.pop(fd, None) is not None, "fd closed more than once"
        is_regular = stat.S_ISREG(os.fstat(fd).st_mode)
        original_close(fd)
        if fault == "close" and is_regular and not close_fault_injected:
            close_fault_injected = True
            raise OSError(errno.EIO, "injected close failure")

    def injected_fsync(fd: int) -> None:
        if fault == "fsync" and stat.S_ISREG(os.fstat(fd).st_mode):
            raise OSError(errno.EIO, "injected fsync failure")
        original_fsync(fd)

    def injected_rename(*args: object, **kwargs: object) -> None:
        if fault in {"rename", "cleanup"}:
            raise OSError(errno.EACCES, "injected rename failure")
        original_rename(*args, **kwargs)

    def injected_unlink(*args: object, **kwargs: object) -> None:
        nonlocal unlink_fault_injected
        if fault in {"unlink", "cleanup"} and not unlink_fault_injected:
            unlink_fault_injected = True
            raise OSError(errno.EACCES, "injected unlink failure")
        original_unlink(*args, **kwargs)

    if fault == "write":
        monkeypatch.setattr(
            preflight,
            "_write_all",
            lambda *_args: (_ for _ in ()).throw(OSError(errno.EIO, "write")),
        )
    monkeypatch.setattr(preflight.os, "open", tracking_open)
    monkeypatch.setattr(preflight.os, "close", tracking_close)
    monkeypatch.setattr(preflight.os, "fsync", injected_fsync)
    monkeypatch.setattr(preflight.os, "rename", injected_rename)
    monkeypatch.setattr(preflight.os, "unlink", injected_unlink)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        preflight.check_config_directory(root, _paths(root))

    assert exc_info.value.reason == reason
    assert list(root.iterdir()) == []
    assert open_tokens == {}


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (
            lambda root, paths: paths
            | {ConfigFile.PROVIDERS: root / "nested/providers.json"},
            "config-path-not-direct-child",
        ),
        (
            lambda root, paths: paths
            | {ConfigFile.PROVIDERS: root.parent / "providers.json"},
            "config-path-not-direct-child",
        ),
        (
            lambda _root, paths: paths
            | {ConfigFile.PROVIDERS: Path("providers.json")},
            "config-path-not-absolute",
        ),
        (
            lambda _root, paths: paths
            | {ConfigFile.PROVIDERS: paths[ConfigFile.FALLBACK_RULES]},
            "config-path-collision",
        ),
    ],
)
def test_preflight_rejects_paths_outside_exact_six_direct_children(
    tmp_path: Path,
    mutate: Callable[
        [Path, dict[ConfigFile, Path]],
        dict[ConfigFile, Path],
    ],
    reason: str,
) -> None:
    root = tmp_path / "config"
    root.mkdir()

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        preflight.check_config_directory(root, mutate(root, _paths(root)))

    assert exc_info.value.reason == reason
    assert list(root.iterdir()) == []


def test_prepare_rejects_symlink_application_root_without_touching_target(
    tmp_path: Path,
) -> None:
    real_app = tmp_path / "real-app"
    real_app.mkdir()
    root = real_app / "config"
    root.mkdir()
    _write_mandatory(root)
    linked_app = tmp_path / "app"
    linked_app.symlink_to(real_app, target_is_directory=True)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root, app_root=linked_app)

    assert exc_info.value.reason == "app-root-symlink"
    assert sorted(path.name for path in root.iterdir()) == [
        "models_fallback_rules.json",
        "providers.json",
    ]


def test_prepare_rejects_symlink_config_root_without_touching_target(tmp_path: Path) -> None:
    real_root = tmp_path / "real-config"
    real_root.mkdir()
    _write_mandatory(real_root)
    linked_root = tmp_path / "config"
    linked_root.symlink_to(real_root, target_is_directory=True)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        preflight.prepare_container_config(tmp_path, linked_root, _paths(linked_root))

    assert exc_info.value.reason == "config-root-symlink"
    assert sorted(path.name for path in real_root.iterdir()) == [
        "models_fallback_rules.json",
        "providers.json",
    ]


@pytest.mark.parametrize("missing", [ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES])
def test_prepare_requires_both_mandatory_files_before_any_write_probe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    missing: ConfigFile,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    (root / FILENAMES[missing]).unlink()
    probe_called = False

    def unexpected_probe(*_args: object, **_kwargs: object) -> None:
        nonlocal probe_called
        probe_called = True

    monkeypatch.setattr(preflight, "check_config_directory", unexpected_probe)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "mandatory-config-missing"
    assert probe_called is False
    assert sorted(path.name for path in root.iterdir()) == [FILENAMES[ConfigFile.FALLBACK_RULES if missing is ConfigFile.PROVIDERS else ConfigFile.PROVIDERS]]


@pytest.mark.parametrize("kind", ["directory", "fifo", "symlink"])
def test_prepare_rejects_unsafe_mandatory_file_without_defaults(
    tmp_path: Path,
    kind: str,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    (root / "providers.json").write_bytes(b"{}\n")
    fallback = root / "models_fallback_rules.json"
    if kind == "directory":
        fallback.mkdir()
    elif kind == "fifo":
        os.mkfifo(fallback)
    else:
        outside = tmp_path / "outside.json"
        outside.write_bytes(b"outside-secret")
        fallback.symlink_to(outside)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "config-source-invalid"
    assert not any(path.name.startswith(".llmgateway-") for path in root.iterdir())
    for config_file in OPTIONAL_CONTENT:
        assert not (root / FILENAMES[config_file]).exists()


def test_prepare_maps_unreadable_mandatory_source_to_stable_safe_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config-secret-path"
    root.mkdir()
    _write_mandatory(root)
    secret = "unreadable-source-secret"

    def fail_capture(*_args: object, **_kwargs: object) -> object:
        raise ConfigSourceError(ConfigFile.FALLBACK_RULES, f"could not read {secret}")

    monkeypatch.setattr(preflight.ConfigSourceBundle, "capture", fail_capture)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "config-source-invalid"
    assert secret not in str(exc_info.value)
    assert os.fspath(root) not in str(exc_info.value)


def test_prepare_creates_only_four_optional_canonical_shapes(tmp_path: Path) -> None:
    root = tmp_path / "config with spaces"
    root.mkdir()
    _write_mandatory(root)

    _prepare(root)

    for config_file, expected in OPTIONAL_CONTENT.items():
        assert (root / FILENAMES[config_file]).read_bytes() == expected
    assert (root / "providers.json").read_bytes() == b"{}\n"
    assert (root / "models_fallback_rules.json").read_bytes() == b"[]\n"
    assert len(list(root.iterdir())) == 6
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_prepare_preserves_existing_optional_content_without_overwrite(tmp_path: Path) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    optional = root / "models_operation_rules.json"
    optional.write_bytes(b'{"operator": "owned"}\n')
    before = optional.stat()

    _prepare(root)

    assert optional.read_bytes() == b'{"operator": "owned"}\n'
    assert optional.stat().st_ino == before.st_ino


def test_concurrent_same_content_optional_publisher_is_accepted(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original = preflight._rename_noreplace
    raced = False

    def race_once(root_fd: int, source: str, destination: str) -> None:
        nonlocal raced
        if not raced:
            raced = True
            fd = os.open(
                destination,
                os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
                0o644,
                dir_fd=root_fd,
            )
            try:
                os.write(fd, b"{}\n")
            finally:
                os.close(fd)
            raise FileExistsError(errno.EEXIST, "concurrent publisher")
        original(root_fd, source, destination)

    monkeypatch.setattr(preflight, "_rename_noreplace", race_once)

    _prepare(root)

    assert raced is True
    for config_file, expected in OPTIONAL_CONTENT.items():
        assert (root / FILENAMES[config_file]).read_bytes() == expected
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_concurrent_foreign_optional_publisher_is_preserved_and_rejected(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)

    def race_foreign(root_fd: int, _source: str, destination: str) -> None:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o600,
            dir_fd=root_fd,
        )
        try:
            os.write(fd, b"foreign-content")
        finally:
            os.close(fd)
        raise FileExistsError(errno.EEXIST, "concurrent publisher")

    monkeypatch.setattr(preflight, "_rename_noreplace", race_foreign)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-target-conflict"
    assert (root / "models_operation_rules.json").read_bytes() == b"foreign-content"
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_concurrent_same_content_requires_winner_file_sync(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_fsync = preflight.os.fsync

    def publish_winner(root_fd: int, _source: str, destination: str) -> None:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=root_fd,
        )
        try:
            os.write(fd, b"{}\n")
        finally:
            os.close(fd)
        raise FileExistsError(errno.EEXIST, "concurrent publisher")

    def fail_winner_sync(fd: int) -> None:
        target = Path(f"/proc/self/fd/{fd}").resolve()
        if target.name == "models_operation_rules.json":
            raise OSError(errno.EIO, "winner sync failed")
        original_fsync(fd)

    monkeypatch.setattr(preflight, "_rename_noreplace", publish_winner)
    monkeypatch.setattr(preflight.os, "fsync", fail_winner_sync)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-concurrent-sync-failed"
    assert exc_info.value.publication_uncertain is True
    assert (root / "models_operation_rules.json").read_bytes() == b"{}\n"
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_concurrent_winner_directory_sync_failure_is_publication_uncertain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_fsync = preflight.os.fsync
    winner_synced = False

    def publish_winner(root_fd: int, _source: str, destination: str) -> None:
        fd = os.open(
            destination,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW,
            0o644,
            dir_fd=root_fd,
        )
        try:
            os.write(fd, b"{}\n")
        finally:
            os.close(fd)
        raise FileExistsError(errno.EEXIST, "concurrent publisher")

    def fail_directory_sync(fd: int) -> None:
        nonlocal winner_synced
        target = Path(f"/proc/self/fd/{fd}").resolve()
        if target.name == "models_operation_rules.json":
            original_fsync(fd)
            winner_synced = True
            return
        if winner_synced and target == root.resolve():
            raise OSError(errno.EIO, "directory sync failed")
        original_fsync(fd)

    monkeypatch.setattr(preflight, "_rename_noreplace", publish_winner)
    monkeypatch.setattr(preflight.os, "fsync", fail_directory_sync)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-directory-sync-failed"
    assert exc_info.value.publication_uncertain is True
    assert (root / "models_operation_rules.json").read_bytes() == b"{}\n"
    assert not list(root.glob(".llmgateway-config-default-*"))


@pytest.mark.parametrize("error_number", [errno.ENOSYS, errno.EINVAL, errno.EOPNOTSUPP])
def test_optional_defaults_fail_closed_when_noreplace_rename_is_unsupported(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    error_number: int,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)

    def unsupported(*_args: object) -> None:
        raise OSError(error_number, "unsupported")

    monkeypatch.setattr(preflight, "_rename_noreplace", unsupported)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-rename-unsupported"
    assert not (root / "models_operation_rules.json").exists()
    assert not list(root.glob(".llmgateway-config-default-*"))


@pytest.mark.parametrize(
    ("fault", "reason"),
    [
        ("directory-fsync", "optional-default-directory-sync-failed"),
        ("verify-open", "optional-default-verify-failed"),
        ("verify-read", "optional-default-verify-failed"),
        ("verify-stat", "optional-default-verify-failed"),
        ("verify-content", "optional-default-verify-failed"),
        ("verify-metadata", "optional-default-verify-failed"),
        ("verify-close", "optional-default-verify-failed"),
    ],
)
def test_optional_post_publish_failure_is_uncertain_closes_fds_and_keeps_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    fault: str,
    reason: str,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_rename = preflight._rename_noreplace
    original_open = preflight.os.open
    original_close = preflight.os.close
    original_fsync = preflight.os.fsync
    original_stat = preflight.os.stat
    original_read_all = preflight._read_all
    state: dict[str, object] = {
        "published": False,
        "root_fd": None,
        "fsync_failed": False,
        "open_failed": False,
        "stat_failed": False,
        "close_failed": False,
    }
    open_tokens: dict[int, int] = {}
    next_open_token = 0

    def injected_rename(root_fd: int, source: str, destination: str) -> None:
        original_rename(root_fd, source, destination)
        state["published"] = True
        state["root_fd"] = root_fd
        if fault not in {"verify-content", "verify-metadata"}:
            return
        fd = original_open(
            destination,
            os.O_WRONLY | os.O_NOFOLLOW | os.O_CLOEXEC,
            dir_fd=root_fd,
        )
        try:
            if fault == "verify-content":
                os.ftruncate(fd, 0)
                os.write(fd, b"changed-owned-inode")
            else:
                os.fchmod(fd, 0o600)
        finally:
            original_close(fd)

    def injected_open(path: object, flags: int, *args: object, **kwargs: object) -> int:
        nonlocal next_open_token
        if (
            fault == "verify-open"
            and state["published"]
            and path == "models_operation_rules.json"
            and not state["open_failed"]
        ):
            state["open_failed"] = True
            raise OSError(errno.EIO, "injected verify open failure")
        fd = original_open(path, flags, *args, **kwargs)
        next_open_token += 1
        open_tokens[fd] = next_open_token
        return fd

    def injected_fsync(fd: int) -> None:
        if (
            fault == "directory-fsync"
            and state["published"]
            and fd == state["root_fd"]
            and not state["fsync_failed"]
        ):
            state["fsync_failed"] = True
            raise OSError(errno.EIO, "injected directory fsync failure")
        original_fsync(fd)

    def injected_stat(path: object, *args: object, **kwargs: object) -> os.stat_result:
        if (
            fault == "verify-stat"
            and state["published"]
            and path == "models_operation_rules.json"
            and not state["stat_failed"]
        ):
            state["stat_failed"] = True
            raise OSError(errno.EIO, "injected verify stat failure")
        return original_stat(path, *args, **kwargs)

    def injected_read_all(fd: int) -> bytes:
        if fault == "verify-read" and state["published"]:
            raise OSError(errno.EIO, "injected verify read failure")
        return original_read_all(fd)

    def tracking_close(fd: int) -> None:
        descriptor_stat = os.fstat(fd)
        assert open_tokens.pop(fd, None) is not None
        is_regular = stat.S_ISREG(descriptor_stat.st_mode)
        original_close(fd)
        if (
            fault == "verify-close"
            and state["published"]
            and is_regular
            and not state["close_failed"]
        ):
            state["close_failed"] = True
            raise OSError(errno.EIO, "injected verify close failure")

    monkeypatch.setattr(preflight, "_rename_noreplace", injected_rename)
    monkeypatch.setattr(preflight.os, "open", injected_open)
    monkeypatch.setattr(preflight.os, "close", tracking_close)
    monkeypatch.setattr(preflight.os, "fsync", injected_fsync)
    monkeypatch.setattr(preflight.os, "stat", injected_stat)
    monkeypatch.setattr(preflight, "_read_all", injected_read_all)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == reason
    assert exc_info.value.publication_uncertain is True
    published = root / "models_operation_rules.json"
    assert published.exists()
    if fault == "verify-content":
        assert published.read_bytes() == b"changed-owned-inode"
    else:
        assert published.read_bytes() == b"{}\n"
    assert sorted(path.name for path in root.iterdir()) == [
        "models_fallback_rules.json",
        "models_operation_rules.json",
        "providers.json",
    ]
    assert not list(root.glob(".llmgateway-config-default-*"))
    assert open_tokens == {}


def test_optional_post_publish_runtime_error_has_no_secret_exception_chain(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    secret = "optional-runtime-secret-sentinel"

    def fail_verify(*_args: object) -> None:
        raise RuntimeError(secret)

    monkeypatch.setattr(preflight, "_verify_published_default", fail_verify)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-internal-error"
    assert exc_info.value.publication_uncertain is True
    assert exc_info.value.__cause__ is None
    assert exc_info.value.__context__ is None
    assert exc_info.value.__suppress_context__ is True
    assert secret not in "".join(traceback.format_exception(exc_info.value))
    assert (root / "models_operation_rules.json").read_bytes() == b"{}\n"
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_optional_post_publish_control_flow_propagates_without_public_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    interrupt = KeyboardInterrupt()

    def interrupt_verify(*_args: object) -> None:
        raise interrupt

    monkeypatch.setattr(preflight, "_verify_published_default", interrupt_verify)

    with pytest.raises(KeyboardInterrupt) as exc_info:
        _prepare(root)

    assert exc_info.value is interrupt
    assert (root / "models_operation_rules.json").read_bytes() == b"{}\n"
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_optional_sequential_failure_keeps_verified_and_uncertain_publications(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_verify = preflight._verify_published_default
    calls = 0

    def fail_second(root_fd: int, filename: str, frozen: object) -> None:
        nonlocal calls
        calls += 1
        if calls == 2:
            raise preflight.ConfigDirectoryPreflightError(
                "optional-default-verify-failed"
            )
        original_verify(root_fd, filename, frozen)

    monkeypatch.setattr(preflight, "_verify_published_default", fail_second)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-verify-failed"
    assert exc_info.value.publication_uncertain is True
    assert (root / "models_operation_rules.json").read_bytes() == b"{}\n"
    assert (root / "models_model_rules.json").read_bytes() == b"{}\n"
    assert not (root / "models_fusion_rules.json").exists()
    assert not (root / "models_router_rules.json").exists()
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_optional_next_run_preserves_uncertain_existing_file_without_republication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_rename = preflight._rename_noreplace
    original_fsync = preflight.os.fsync
    published_root_fd: int | None = None
    failed = False
    operation_publications = 0

    def recording_rename(root_fd: int, source: str, destination: str) -> None:
        nonlocal published_root_fd, operation_publications
        original_rename(root_fd, source, destination)
        if destination == "models_operation_rules.json":
            published_root_fd = root_fd
            operation_publications += 1

    def fail_first_post_publish_sync(fd: int) -> None:
        nonlocal failed
        if published_root_fd == fd and not failed:
            failed = True
            raise OSError(errno.EIO, "injected ambiguous publication sync")
        original_fsync(fd)

    monkeypatch.setattr(preflight, "_rename_noreplace", recording_rename)
    monkeypatch.setattr(preflight.os, "fsync", fail_first_post_publish_sync)

    with pytest.raises(preflight.ConfigDirectoryPreflightError) as exc_info:
        _prepare(root)

    assert exc_info.value.reason == "optional-default-directory-sync-failed"
    assert exc_info.value.publication_uncertain is True
    published = root / "models_operation_rules.json"
    first_stat = published.stat()
    first_bytes = published.read_bytes()

    _prepare(root)

    assert operation_publications == 1
    assert published.read_bytes() == first_bytes
    assert published.stat().st_ino == first_stat.st_ino
    for config_file, expected in OPTIONAL_CONTENT.items():
        assert (root / FILENAMES[config_file]).read_bytes() == expected
    assert not list(root.glob(".llmgateway-config-default-*"))


def test_prepare_performs_final_safe_capture_after_optional_publication(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "config"
    root.mkdir()
    _write_mandatory(root)
    original_capture = preflight.ConfigSourceBundle.capture
    captures: list[tuple[bool, ...]] = []

    def recording_capture(*args: object, **kwargs: object) -> object:
        bundle = original_capture(*args, **kwargs)
        captures.append(tuple(bundle[config_file].exists for config_file in ConfigFile))
        return bundle

    monkeypatch.setattr(preflight.ConfigSourceBundle, "capture", recording_capture)

    _prepare(root)

    assert captures == [
        tuple(config_file in {ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES} for config_file in ConfigFile),
        tuple(True for _config_file in ConfigFile),
    ]


def test_cli_reports_missing_mandatory_with_exact_secret_safe_reason(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = tmp_path / "secret config root"
    root.mkdir()
    (root / "providers.json").write_bytes(b"{}\n")
    secret = "cli-secret-sentinel"
    _configure_cli(root, monkeypatch)
    monkeypatch.setenv("SECRET_SENTINEL", secret)

    assert preflight.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "container-config-preflight: reason=mandatory-config-missing\n"
    assert secret not in captured.err
    assert os.fspath(root) not in captured.err
    assert sorted(path.name for path in root.iterdir()) == ["providers.json"]


def test_cli_reports_uncertain_publication_without_secret_values(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "uncertain-publication-secret"

    def fail_prepare(*_args: object, **_kwargs: object) -> None:
        raise preflight.ConfigDirectoryPreflightError(
            "optional-default-concurrent-sync-failed",
            publication_uncertain=True,
        )

    monkeypatch.setattr(preflight, "prepare_container_config", fail_prepare)
    monkeypatch.setenv("SECRET_SENTINEL", secret)

    assert preflight.main() == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == (
        "container-config-preflight: "
        "reason=optional-default-concurrent-sync-failed publication=uncertain\n"
    )
    assert secret not in captured.err


def test_cli_rejects_arguments_without_environment_output(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    secret = "invalid-arguments-secret"
    monkeypatch.setenv("SECRET_SENTINEL", secret)

    assert preflight.main(["--legacy-secret", secret]) == 1

    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err == "container-config-preflight: reason=invalid-arguments\n"
    assert secret not in captured.err
