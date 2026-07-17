from __future__ import annotations

import errno
import os
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from llm_gateway_core.services import single_process
from llm_gateway_core.services.single_process import (
    SingleProcessInvariantError,
    SingleProcessLease,
    validate_single_worker_environment,
)
from tests._async_compat import run_async


REPO_ROOT = Path(__file__).resolve().parents[1]
LOCK_FILENAME = ".llmgateway-single-process.lock"
WORKER_ENV_SOURCES = (
    "GATEWAY_WORKERS",
    "WEB_CONCURRENCY",
    "UVICORN_WORKERS",
)


def _minimal_subprocess_environment(**updates: str) -> dict[str, str]:
    return {
        "PYTHONPATH": str(REPO_ROOT),
        "PYTHONIOENCODING": "utf-8",
        **updates,
    }


def _run_module(
    tmp_path: Path,
    *arguments: str,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [
            sys.executable,
            "-m",
            "llm_gateway_core.services.single_process",
            *arguments,
        ],
        cwd=tmp_path,
        env=environment or _minimal_subprocess_environment(),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def _run_lease_probe(state_dir: Path) -> subprocess.CompletedProcess[str]:
    script = """
import asyncio
import os
from pathlib import Path
from llm_gateway_core.services.single_process import SingleProcessInvariantError, SingleProcessLease

try:
    lease = SingleProcessLease.acquire(Path(os.environ["STATE_DIR"]))
except SingleProcessInvariantError as error:
    print(f"{error.source}:{error.reason_code}")
    raise SystemExit(3)
asyncio.run(lease.close())
print("acquired")
"""
    return subprocess.run(
        [sys.executable, "-c", script],
        cwd=state_dir,
        env=_minimal_subprocess_environment(STATE_DIR=str(state_dir)),
        capture_output=True,
        text=True,
        check=False,
        timeout=10,
    )


def test_environment_validation_is_pure_and_accepts_only_canonical_worker_values() -> None:
    environment = {source: "1" for source in WORKER_ENV_SOURCES}
    original = environment.copy()

    validate_single_worker_environment(environment)
    validate_single_worker_environment({})

    assert environment == original


@pytest.mark.parametrize("source", WORKER_ENV_SOURCES)
@pytest.mark.parametrize(
    "value",
    ["", " ", "\t1", "+1", "01", "1.0", "0", "2", "-1", "١", "１"],
)
def test_worker_environment_rejects_every_noncanonical_value(source: str, value: str) -> None:
    with pytest.raises(SingleProcessInvariantError) as raised:
        validate_single_worker_environment({source: value})

    assert raised.value.source == source
    assert raised.value.reason_code == "invalid-worker-count"
    assert repr(value) not in str(raised.value)


def test_conflicting_worker_sources_fail_closed() -> None:
    with pytest.raises(SingleProcessInvariantError) as raised:
        validate_single_worker_environment(
            {
                "GATEWAY_WORKERS": "1",
                "WEB_CONCURRENCY": "2",
                "UVICORN_WORKERS": "1",
            }
        )

    assert raised.value.source == "WEB_CONCURRENCY"
    assert raised.value.reason_code == "invalid-worker-count"


@pytest.mark.parametrize(
    "arguments",
    [
        "",
        "   ",
        "--bind 127.0.0.1:8000 --timeout 30",
        "-w 1",
        "-w1",
        "--workers 1",
        "--workers=1",
        "--bind :8000 --workers=1 --timeout=30",
    ],
)
def test_gunicorn_environment_accepts_only_unambiguous_single_worker_forms(arguments: str) -> None:
    validate_single_worker_environment({"GUNICORN_CMD_ARGS": arguments})


@pytest.mark.parametrize(
    ("arguments", "reason_code"),
    [
        ("-w", "malformed-worker-option"),
        ("--workers", "malformed-worker-option"),
        ("-w 2", "invalid-worker-count"),
        ("-w01", "invalid-worker-count"),
        ("-w+1", "invalid-worker-count"),
        ("-w=1", "invalid-worker-count"),
        ("--workers=", "invalid-worker-count"),
        ("--workers 01", "invalid-worker-count"),
        ("--workers=2", "invalid-worker-count"),
        ("-w 1 --workers=1", "duplicate-worker-option"),
        ("--workers=1 -w1", "duplicate-worker-option"),
        ("'unterminated", "malformed-worker-option"),
        ("-c config.py", "config-source-not-supported"),
        ("-cconfig.py", "config-source-not-supported"),
        ("--config config.py", "config-source-not-supported"),
        ("--config=config.py", "config-source-not-supported"),
        ("--co config.py", "config-source-not-supported"),
        ("--co=config.py", "config-source-not-supported"),
        ("--con config.py", "config-source-not-supported"),
        ("--con=config.py", "config-source-not-supported"),
        ("--conf config.py", "config-source-not-supported"),
        ("--conf=config.py", "config-source-not-supported"),
        ("--confi config.py", "config-source-not-supported"),
        ("--confi=config.py", "config-source-not-supported"),
    ],
)
def test_gunicorn_environment_rejects_ambiguous_or_hidden_worker_configuration(
    arguments: str,
    reason_code: str,
) -> None:
    with pytest.raises(SingleProcessInvariantError) as raised:
        validate_single_worker_environment({"GUNICORN_CMD_ARGS": arguments})

    assert raised.value.source == "GUNICORN_CMD_ARGS"
    assert raised.value.reason_code == reason_code
    assert repr(arguments) not in str(raised.value)


def test_gunicorn_ambiguous_config_prefix_is_not_misclassified() -> None:
    validate_single_worker_environment({"GUNICORN_CMD_ARGS": "--c hidden.py"})


def test_environment_mapping_failures_are_converted_to_safe_errors() -> None:
    secret = "mapping-secret-sentinel"

    class BrokenEnvironment(dict[str, str]):
        def __contains__(self, key: object) -> bool:
            raise RuntimeError(secret)

    with pytest.raises(SingleProcessInvariantError) as raised:
        validate_single_worker_environment(BrokenEnvironment())

    assert raised.value.reason_code == "invalid-environment"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


@pytest.mark.parametrize("source", ["GATEWAY_WORKERS", "GUNICORN_CMD_ARGS"])
def test_environment_rejects_hostile_string_subclasses_without_using_them(source: str) -> None:
    secret = "hostile-string-secret-sentinel"

    class HostileString(str):
        def __eq__(self, other: object) -> bool:
            raise RuntimeError(secret)

        def __str__(self) -> str:
            raise RuntimeError(secret)

    with pytest.raises(SingleProcessInvariantError) as raised:
        validate_single_worker_environment({source: HostileString("1")})

    assert raised.value.source == source
    assert raised.value.reason_code == "invalid-environment"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)


def test_error_constructor_rejects_dynamic_diagnostic_tokens() -> None:
    error = SingleProcessInvariantError("secretidentifier", "secretreason")

    assert error.source == "internal"
    assert error.reason_code == "invariant-failed"
    assert "secretidentifier" not in str(error)
    assert "secretreason" not in repr(error)


def test_module_cli_is_silent_on_success_and_never_echoes_raw_inputs(tmp_path: Path) -> None:
    success = _run_module(
        tmp_path,
        "--check-environment",
        environment=_minimal_subprocess_environment(GATEWAY_WORKERS="1"),
    )
    assert success.returncode == 0
    assert success.stdout == ""
    assert success.stderr == ""

    secret = "cli-secret-sentinel"
    invalid_environment = _run_module(
        tmp_path,
        "--check-environment",
        environment=_minimal_subprocess_environment(
            GUNICORN_CMD_ARGS=f"--workers 2 --access-logfile {secret}"
        ),
    )
    assert invalid_environment.returncode == 1
    assert invalid_environment.stdout == ""
    assert secret not in invalid_environment.stderr
    assert "--workers" not in invalid_environment.stderr
    assert "source=GUNICORN_CMD_ARGS" in invalid_environment.stderr

    invalid_arguments = _run_module(tmp_path, f"--{secret}")
    assert invalid_arguments.returncode == 2
    assert invalid_arguments.stdout == ""
    assert secret not in invalid_arguments.stderr
    assert "source=arguments" in invalid_arguments.stderr


def test_lease_is_empty_noninheritable_and_repr_does_not_expose_path(tmp_path: Path) -> None:
    state_dir = tmp_path / "state-secret-sentinel"
    state_dir.mkdir()

    lease = SingleProcessLease.acquire(state_dir)
    descriptor = lease._descriptor
    assert descriptor is not None
    assert os.get_inheritable(descriptor) is False
    assert state_dir.name not in repr(lease)
    lock_stat = (state_dir / LOCK_FILENAME).stat()
    assert lock_stat.st_size == 0
    assert lock_stat.st_nlink == 1
    assert stat.S_IMODE(lock_stat.st_mode) == 0o600

    run_async(lease.close())
    assert lease.closed is True
    assert repr(lease) == "SingleProcessLease(closed=True)"


def test_lock_descriptor_is_closed_by_real_exec_even_when_close_fds_is_false(tmp_path: Path) -> None:
    lease = SingleProcessLease.acquire(tmp_path)
    descriptor = lease._descriptor
    assert descriptor is not None
    script = """
import errno
import os

try:
    os.fstat(int(os.environ["LEASE_DESCRIPTOR"]))
except OSError as error:
    raise SystemExit(0 if error.errno == errno.EBADF else 2)
raise SystemExit(3)
"""

    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=tmp_path,
        env=_minimal_subprocess_environment(LEASE_DESCRIPTOR=str(descriptor)),
        capture_output=True,
        text=True,
        check=False,
        close_fds=False,
        timeout=10,
    )

    assert completed.returncode == 0
    assert completed.stdout == ""
    assert completed.stderr == ""
    run_async(lease.close())


def test_two_process_contention_is_immediate_and_release_allows_next_owner(tmp_path: Path) -> None:
    lease = SingleProcessLease.acquire(tmp_path)

    contended = _run_lease_probe(tmp_path)
    assert contended.returncode == 3
    assert contended.stdout.strip() == "lock-file:lease-contended"
    assert contended.stderr == ""

    run_async(lease.close())
    acquired = _run_lease_probe(tmp_path)
    assert acquired.returncode == 0
    assert acquired.stdout.strip() == "acquired"
    assert acquired.stderr == ""


def test_close_is_idempotent_under_true_two_thread_contention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    lease = SingleProcessLease.acquire(tmp_path)
    descriptor = lease._descriptor
    assert descriptor is not None
    original_flock = single_process._fcntl.flock
    original_close = single_process.os.close
    counts = {"unlock": 0, "close": 0}
    counts_lock = threading.Lock()

    def counted_flock(file_descriptor: int, operation: int) -> None:
        if file_descriptor == descriptor and operation == single_process._fcntl.LOCK_UN:
            with counts_lock:
                counts["unlock"] += 1
        original_flock(file_descriptor, operation)

    def counted_close(file_descriptor: int) -> None:
        if file_descriptor == descriptor:
            with counts_lock:
                counts["close"] += 1
        original_close(file_descriptor)

    monkeypatch.setattr(single_process._fcntl, "flock", counted_flock)
    monkeypatch.setattr(single_process.os, "close", counted_close)
    start = threading.Barrier(3)
    failures: list[BaseException] = []

    def close_worker() -> None:
        start.wait()
        try:
            run_async(lease.close())
        except BaseException as error:
            failures.append(error)

    threads = [threading.Thread(target=close_worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    start.wait()
    for thread in threads:
        thread.join(timeout=10)

    assert all(not thread.is_alive() for thread in threads)
    assert failures == []
    assert counts == {"unlock": 1, "close": 1}
    assert lease.closed is True
    with pytest.raises(OSError) as closed_descriptor:
        os.fstat(descriptor)
    assert closed_descriptor.value.errno == errno.EBADF
    run_async(lease.close())
    assert counts == {"unlock": 1, "close": 1}


@pytest.mark.parametrize("failure_stage", ["unlock", "close"])
def test_close_safe_wraps_ordinary_cleanup_errors_after_attempting_close(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    lease = SingleProcessLease.acquire(tmp_path)
    descriptor = lease._descriptor
    assert descriptor is not None
    secret = "ordinary-cleanup-secret-sentinel"
    original_flock = single_process._fcntl.flock
    original_close = single_process.os.close
    close_attempts: list[int] = []

    def injected_flock(file_descriptor: int, operation: int) -> None:
        if (
            failure_stage == "unlock"
            and file_descriptor == descriptor
            and operation == single_process._fcntl.LOCK_UN
        ):
            raise RuntimeError(secret)
        original_flock(file_descriptor, operation)

    def injected_close(file_descriptor: int) -> None:
        if file_descriptor == descriptor:
            close_attempts.append(file_descriptor)
            original_close(file_descriptor)
            if failure_stage == "close":
                raise RuntimeError(secret)
            return
        original_close(file_descriptor)

    monkeypatch.setattr(single_process._fcntl, "flock", injected_flock)
    monkeypatch.setattr(single_process.os, "close", injected_close)
    with pytest.raises(SingleProcessInvariantError) as raised:
        run_async(lease.close())

    assert close_attempts == [descriptor]
    assert lease.closed is True
    assert raised.value.reason_code == "lease-close-failed"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
    assert raised.value.__suppress_context__ is True


@pytest.mark.parametrize("failure_stage", ["unlock", "close"])
def test_close_rethrows_terminal_cleanup_failure_only_after_close_attempt(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    failure_stage: str,
) -> None:
    lease = SingleProcessLease.acquire(tmp_path)
    descriptor = lease._descriptor
    assert descriptor is not None

    class TerminalCleanupFailure(BaseException):
        pass

    terminal = TerminalCleanupFailure("terminal-cleanup-sentinel")
    original_flock = single_process._fcntl.flock
    original_close = single_process.os.close
    close_attempts: list[int] = []

    def injected_flock(file_descriptor: int, operation: int) -> None:
        if (
            failure_stage == "unlock"
            and file_descriptor == descriptor
            and operation == single_process._fcntl.LOCK_UN
        ):
            raise terminal
        original_flock(file_descriptor, operation)

    def injected_close(file_descriptor: int) -> None:
        if file_descriptor == descriptor:
            close_attempts.append(file_descriptor)
            original_close(file_descriptor)
            if failure_stage == "close":
                raise terminal
            return
        original_close(file_descriptor)

    monkeypatch.setattr(single_process._fcntl, "flock", injected_flock)
    monkeypatch.setattr(single_process.os, "close", injected_close)
    with pytest.raises(TerminalCleanupFailure) as raised:
        run_async(lease.close())

    assert raised.value is terminal
    assert close_attempts == [descriptor]
    assert lease.closed is True


@pytest.mark.parametrize(
    "unsafe_kind",
    ["symlink", "fifo", "directory", "nonempty", "hardlink", "wrong-mode"],
)
def test_lease_rejects_unsafe_lock_entries_without_blocking(
    tmp_path: Path,
    unsafe_kind: str,
) -> None:
    lock_path = tmp_path / LOCK_FILENAME
    if unsafe_kind == "symlink":
        target = tmp_path / "secret-target"
        target.write_text("do-not-touch", encoding="utf-8")
        lock_path.symlink_to(target)
    elif unsafe_kind == "fifo":
        os.mkfifo(lock_path)
    elif unsafe_kind == "directory":
        lock_path.mkdir()
    elif unsafe_kind == "nonempty":
        lock_path.write_text("do-not-touch", encoding="utf-8")
    elif unsafe_kind == "hardlink":
        target = tmp_path / "hardlink-target"
        target.touch()
        lock_path.hardlink_to(target)
    else:
        lock_path.touch(mode=0o644)

    with pytest.raises(SingleProcessInvariantError) as raised:
        SingleProcessLease.acquire(tmp_path)

    assert raised.value.source == "lock-file"
    assert raised.value.reason_code in {"lock-open-failed", "unsafe-lock-file"}
    assert str(tmp_path) not in str(raised.value)


def test_lease_rejects_symlinked_parent_and_noncanonical_directory(tmp_path: Path) -> None:
    real_parent = tmp_path / "real"
    state_dir = real_parent / "state"
    state_dir.mkdir(parents=True)
    alias = tmp_path / "alias"
    alias.symlink_to(real_parent, target_is_directory=True)

    with pytest.raises(SingleProcessInvariantError) as symlinked:
        SingleProcessLease.acquire(alias / "state")
    assert symlinked.value.reason_code == "state-directory-open-failed"

    with pytest.raises(SingleProcessInvariantError) as relative:
        SingleProcessLease.acquire(Path("relative-state"))
    assert relative.value.reason_code == "invalid-state-directory"

    with pytest.raises(SingleProcessInvariantError) as traversing:
        SingleProcessLease.acquire(tmp_path / "real" / ".." / "real" / "state")
    assert traversing.value.reason_code == "invalid-state-directory"


def test_lease_permission_and_platform_failures_are_safe(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_open = single_process.os.open
    original_supports_dir_fd = single_process.os.supports_dir_fd

    def denied_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if path == os.sep:
            raise PermissionError("permission-secret-sentinel")
        return original_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(single_process.os, "open", denied_open)
    monkeypatch.setattr(
        single_process.os,
        "supports_dir_fd",
        frozenset({*original_supports_dir_fd, denied_open}),
    )
    with pytest.raises(SingleProcessInvariantError) as denied:
        SingleProcessLease.acquire(tmp_path)
    assert denied.value.reason_code == "state-directory-open-failed"
    assert "permission-secret-sentinel" not in repr(denied.value)

    monkeypatch.setattr(single_process.os, "open", original_open)
    monkeypatch.setattr(single_process, "_fcntl", None)
    with pytest.raises(SingleProcessInvariantError) as unsupported:
        SingleProcessLease.acquire(tmp_path)
    assert unsupported.value.source == "platform"
    assert unsupported.value.reason_code == "unsupported-platform"


def test_platform_preflight_requires_fcntl_constants_and_os_capability_sets(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class IncompleteFcntl:
        flock = single_process._fcntl.flock
        LOCK_EX = single_process._fcntl.LOCK_EX
        LOCK_UN = single_process._fcntl.LOCK_UN

    monkeypatch.setattr(single_process, "_fcntl", IncompleteFcntl())
    with pytest.raises(SingleProcessInvariantError) as incomplete_fcntl:
        SingleProcessLease.acquire(tmp_path)
    assert incomplete_fcntl.value.reason_code == "unsupported-platform"

    monkeypatch.setattr(single_process, "_fcntl", __import__("fcntl"))
    monkeypatch.setattr(single_process.os, "supports_dir_fd", frozenset())
    with pytest.raises(SingleProcessInvariantError) as missing_dirfd:
        SingleProcessLease.acquire(tmp_path)
    assert missing_dirfd.value.reason_code == "unsupported-platform"


@pytest.mark.parametrize("unsupported_operation", ["open-at", "stat-follow"])
def test_not_implemented_dirfd_operations_fail_as_unsupported_platform(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    unsupported_operation: str,
) -> None:
    secret = "not-implemented-secret-sentinel"
    original_open = single_process.os.open
    original_stat = single_process.os.stat

    def unsupported_open(path: str, flags: int, *args: object, **kwargs: object) -> int:
        if unsupported_operation == "open-at" and kwargs.get("dir_fd") is not None:
            raise NotImplementedError(secret)
        return original_open(path, flags, *args, **kwargs)

    def unsupported_stat(path: str, *args: object, **kwargs: object) -> os.stat_result:
        if unsupported_operation == "stat-follow" and kwargs.get("dir_fd") is not None:
            raise NotImplementedError(secret)
        return original_stat(path, *args, **kwargs)

    monkeypatch.setattr(single_process.os, "open", unsupported_open)
    monkeypatch.setattr(single_process.os, "stat", unsupported_stat)
    monkeypatch.setattr(
        single_process.os,
        "supports_dir_fd",
        frozenset({unsupported_open, unsupported_stat}),
    )
    monkeypatch.setattr(
        single_process.os,
        "supports_follow_symlinks",
        frozenset({unsupported_stat}),
    )

    with pytest.raises(SingleProcessInvariantError) as raised:
        SingleProcessLease.acquire(tmp_path)

    assert raised.value.source == "platform"
    assert raised.value.reason_code == "unsupported-platform"
    assert secret not in str(raised.value)
    assert secret not in repr(raised.value)
