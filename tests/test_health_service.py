from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from llm_gateway_core.db.write_batcher import (
    WriteBatcherHealthSnapshot,
    WriteBatcherState,
)
from llm_gateway_core.services import health as health_module
from llm_gateway_core.services.accounting import (
    AccountingHealthSnapshot,
    AccountingHealthState,
)
from llm_gateway_core.services.config_updates import (
    ConfigUpdateState,
    ConfigUpdateStatusSnapshot,
)
from llm_gateway_core.services.health import HealthPhase, HealthService
from tests._async_compat import run_async


class _Accounting:
    def __init__(self, snapshot: AccountingHealthSnapshot) -> None:
        self.snapshot = snapshot

    def health_snapshot(self) -> AccountingHealthSnapshot:
        return self.snapshot


class _Writer:
    def __init__(self, snapshot: WriteBatcherHealthSnapshot) -> None:
        self.snapshot = snapshot

    def health_snapshot(self) -> WriteBatcherHealthSnapshot:
        return self.snapshot


class _ImageRetention:
    def __init__(self, snapshot: SimpleNamespace | None = None) -> None:
        self.value = snapshot or SimpleNamespace(
            last_run=time.time(),
            deleted_final=0,
            deleted_temp=0,
            deleted_dirs=0,
            failures=0,
        )

    def snapshot(self) -> SimpleNamespace:
        return self.value


def _accounting_snapshot() -> AccountingHealthSnapshot:
    return AccountingHealthSnapshot(
        state=AccountingHealthState.RUNNING,
        initialized=True,
        accepting=True,
        active_session_limit=128,
    )


def _writer_snapshot() -> WriteBatcherHealthSnapshot:
    return WriteBatcherHealthSnapshot(
        state=WriteBatcherState.RUNNING,
        accepting=True,
        task_running=True,
        capacity=16,
        reserved=0,
        pre_start=0,
        handoff_pending=0,
        queued=0,
        in_flight=0,
        accepted=0,
        committed=0,
        diagnostic_terminal=0,
        overflow=0,
        dead_letter_failures=0,
        last_error_code=None,
    )


def _create_database(path: Path) -> None:
    with sqlite3.connect(path) as connection:
        connection.execute("CREATE TABLE health_probe (id INTEGER PRIMARY KEY)")


def _service(
    paths: tuple[Path, ...],
    *,
    image_retention: _ImageRetention | None = None,
) -> tuple[HealthService, SimpleNamespace, SimpleNamespace, _Accounting, _Writer]:
    runtime = SimpleNamespace(
        status=SimpleNamespace(value="running"),
        current_generation=1,
    )
    config = SimpleNamespace(
        status_snapshot=ConfigUpdateStatusSnapshot(
            state=ConfigUpdateState.RUNNING,
            accepting=True,
            active_updates=0,
            pending_cleanup=0,
            last_failure=None,
        )
    )
    accounting = _Accounting(_accounting_snapshot())
    writer = _Writer(_writer_snapshot())
    service = HealthService(
        runtime_manager=runtime,  # type: ignore[arg-type]
        config_update_coordinator=config,  # type: ignore[arg-type]
        accounting_service=accounting,  # type: ignore[arg-type]
        write_batcher=writer,  # type: ignore[arg-type]
        image_retention_service=image_retention or _ImageRetention(),  # type: ignore[arg-type]
        database_paths=paths,
    )
    return service, runtime, config, accounting, writer


def test_starting_report_does_not_probe_sqlite(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "missing.db"
    service, *_ = _service((database,))
    monkeypatch.setattr(
        health_module,
        "_probe_sqlite_path_sync",
        lambda path: pytest.fail("starting health must not probe SQLite"),
    )

    report = run_async(service.report())

    assert report.phase is HealthPhase.STARTING
    assert not report.ok
    assert report.as_dict()["checks"]["sqlite"]["reason"] == "not_ready"
    assert not database.exists()


def test_ready_report_deduplicates_and_reads_three_sqlite_databases(
    tmp_path: Path,
) -> None:
    databases = tuple(tmp_path / f"db-{index}.sqlite" for index in range(3))
    for database in databases:
        _create_database(database)
    service, *_ = _service((databases[0], databases[0], *databases[1:]))
    service.mark_ready()

    report = run_async(service.report())

    assert service.database_paths == databases
    assert report.ok
    assert report.as_dict()["status"] == "ok"
    sqlite_check = report.as_dict()["checks"]["sqlite"]
    assert sqlite_check["counters"] == {"checked": 3, "failed": 0}


@pytest.mark.parametrize(
    "component",
    ["runtime", "config", "accounting", "writer", "image_retention"],
)
def test_each_mandatory_component_failure_makes_report_unready(
    tmp_path: Path,
    component: str,
) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    service, runtime, config, accounting, writer = _service((database,))
    service.mark_ready()
    if component == "runtime":
        runtime.status = SimpleNamespace(value="stopped")
    elif component == "config":
        config.status_snapshot = replace(config.status_snapshot, accepting=False)
    elif component == "accounting":
        accounting.snapshot = replace(accounting.snapshot, accepting=False)
    elif component == "writer":
        writer.snapshot = replace(writer.snapshot, task_running=False)
    else:
        service._image_retention_service.value = SimpleNamespace(
            last_run=time.time(),
            deleted_final=0,
            deleted_temp=0,
            deleted_dirs=0,
            failures=1,
        )

    report = run_async(service.report())

    assert not report.ok
    assert report.as_dict()["checks"][component]["status"] == "error"


def test_image_retention_never_run_is_path_free_failure(tmp_path: Path) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    retention = _ImageRetention(
        SimpleNamespace(
            last_run=None,
            deleted_final=0,
            deleted_temp=0,
            deleted_dirs=0,
            failures=0,
        )
    )
    service, *_ = _service((database,), image_retention=retention)
    service.mark_ready()

    payload = run_async(service.report()).as_dict()["checks"]["image_retention"]

    assert payload == {
        "status": "error",
        "reason": "retention_never_run",
        "counters": {
            "deleted_final": 0,
            "deleted_temp": 0,
            "deleted_dirs": 0,
            "failures": 0,
        },
    }
    assert str(tmp_path) not in str(payload)


def test_later_clean_image_retention_snapshot_restores_health(tmp_path: Path) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    retention = _ImageRetention(
        SimpleNamespace(
            last_run=time.time(),
            deleted_final=0,
            deleted_temp=0,
            deleted_dirs=0,
            failures=1,
        )
    )
    service, *_ = _service((database,), image_retention=retention)
    service.mark_ready()

    failed = run_async(service.report())
    retention.value = SimpleNamespace(
        last_run=time.time(),
        deleted_final=1,
        deleted_temp=2,
        deleted_dirs=3,
        failures=0,
    )
    recovered = run_async(service.report())

    assert not failed.ok
    assert recovered.ok
    assert recovered.as_dict()["checks"]["image_retention"] == {
        "status": "ok",
        "reason": "retention_ok",
        "counters": {
            "deleted_final": 1,
            "deleted_temp": 2,
            "deleted_dirs": 3,
            "failures": 0,
        },
    }


def test_writer_overflow_is_an_explicit_failure(tmp_path: Path) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    service, *_, writer = _service((database,))
    service.mark_ready()
    writer.snapshot = replace(
        writer.snapshot,
        state=WriteBatcherState.FAILED,
        accepting=False,
        task_running=False,
        overflow=1,
    )

    report = run_async(service.report())

    assert not report.ok
    assert report.as_dict()["checks"]["writer"]["reason"] == "writer_overflow"


@pytest.mark.parametrize("kind", ["missing", "corrupt"])
def test_sqlite_failure_is_safe_and_mode_ro_never_creates_a_file(
    tmp_path: Path,
    kind: str,
) -> None:
    database = tmp_path / "health.sqlite"
    if kind == "corrupt":
        database.write_bytes(b"not a sqlite database")
    service, *_ = _service((database,))
    service.mark_ready()

    report = run_async(service.report())

    assert not report.ok
    sqlite_check = report.as_dict()["checks"]["sqlite"]
    assert sqlite_check == {
        "status": "error",
        "reason": "sqlite_unavailable",
        "counters": {"checked": 1, "failed": 1},
    }
    if kind == "missing":
        assert not database.exists()
    assert str(database) not in str(report.as_dict())


def test_sqlite_probe_has_bounded_overall_timeout(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    service, *_ = _service((database,))
    service.mark_ready()

    def slow_probe(path):
        time.sleep(0.05)
        return True

    monkeypatch.setattr(health_module, "_probe_sqlite_path_sync", slow_probe)
    monkeypatch.setattr(health_module, "SQLITE_PROBE_TIMEOUT_SECONDS", 0.01)

    report = run_async(service.report())

    assert not report.ok
    assert report.as_dict()["checks"]["sqlite"]["reason"] == "sqlite_timeout"


def test_three_sqlite_paths_are_probed_in_parallel(
    tmp_path: Path,
    monkeypatch,
) -> None:
    paths = tuple(tmp_path / f"health-{index}.sqlite" for index in range(3))
    service, *_ = _service(paths)
    service.mark_ready()
    barrier = threading.Barrier(3, timeout=0.25)

    def synchronized_probe(path):
        barrier.wait()
        return True

    monkeypatch.setattr(
        health_module,
        "_probe_sqlite_path_sync",
        synchronized_probe,
    )

    report = run_async(service.report())

    assert report.ok
    assert report.as_dict()["checks"]["sqlite"]["counters"] == {
        "checked": 3,
        "failed": 0,
    }


def test_sqlite_probe_uses_singleflight_and_ttl(tmp_path: Path, monkeypatch) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    service, *_ = _service((database,))
    service.mark_ready()
    original_probe = health_module._probe_sqlite_path_sync
    calls = 0

    def counted_probe(path):
        nonlocal calls
        calls += 1
        time.sleep(0.03)
        return original_probe(path)

    monkeypatch.setattr(health_module, "_probe_sqlite_path_sync", counted_probe)

    async def scenario() -> None:
        reports = await asyncio.gather(*(service.report() for _ in range(8)))
        assert all(report.ok for report in reports)
        assert calls == 1
        assert (await service.report()).ok
        assert calls == 1
        service._cache_expires_at = 0.0
        assert (await service.report()).ok
        assert calls == 2

    run_async(scenario())


def test_many_distinct_sqlite_paths_are_accepted(tmp_path: Path) -> None:
    # The former SQLITE_PROBE_PATH_LIMIT=3 cap has been removed: main.py
    # always passes 6 database paths, and rejecting a 4th distinct one used
    # to raise ValueError out of HealthService.__init__ and crash startup.
    paths = tuple(tmp_path / f"db-{index}" for index in range(6))

    service, *_ = _service(paths)

    assert service.database_paths == paths


def test_empty_sqlite_path_list_is_rejected() -> None:
    # The upper bound was removed, but at least one path is still required.
    with pytest.raises(
        ValueError, match="at least one SQLite database path is required"
    ):
        _service(())


def test_invalid_sqlite_path_fails_readiness_without_fallback() -> None:
    service, *_ = _service((object(),))  # type: ignore[arg-type]
    service.mark_ready()

    report = run_async(service.report())

    assert not report.ok
    assert (
        report.as_dict()["checks"]["sqlite"]["reason"]
        == "sqlite_configuration_invalid"
    )


def test_stopping_and_failed_phases_are_unready(tmp_path: Path) -> None:
    database = tmp_path / "health.sqlite"
    _create_database(database)
    service, *_ = _service((database,))
    service.mark_ready()
    service.mark_stopping()

    stopping = run_async(service.report())
    service.mark_failed()
    failed = run_async(service.report())

    assert stopping.phase is HealthPhase.STOPPING
    assert not stopping.ok
    assert failed.phase is HealthPhase.FAILED
    assert not failed.ok
