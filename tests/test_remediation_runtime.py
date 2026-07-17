"""Regression tests for the runtime-remediation pass.

One test per behavioral fix:
- TaskSupervisor logs a failing supervised task and surfaces it in the
  health report (task_supervisor.py, health.py).
- The usage-stats cleanup loop survives an exception raised during one
  iteration instead of dying silently (main.py).
- run_cancellation_resistant_thread_worker re-raises an external
  BaseException instead of masking it with InvalidStateError (main.py).
- The /healthz liveness endpoint reports 200 regardless of readiness
  (llm_gateway_core/api/health.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

import main
from llm_gateway_core.api.health import liveness
from llm_gateway_core.db.write_batcher import (
    WriteBatcherHealthSnapshot,
    WriteBatcherState,
)
from llm_gateway_core.services.accounting import (
    AccountingHealthSnapshot,
    AccountingHealthState,
)
from llm_gateway_core.services.config_updates import (
    ConfigUpdateState,
    ConfigUpdateStatusSnapshot,
)
from llm_gateway_core.services.health import HealthService
from llm_gateway_core.services.task_supervisor import TaskSupervisor
from tests._async_compat import run_async


def _make_health_service(
    *,
    task_supervisor: TaskSupervisor,
    database_path: Path,
) -> HealthService:
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE health_probe (id INTEGER PRIMARY KEY)")

    class _Accounting:
        def health_snapshot(self) -> AccountingHealthSnapshot:
            return AccountingHealthSnapshot(
                state=AccountingHealthState.RUNNING,
                initialized=True,
                accepting=True,
                active_session_limit=128,
            )

    class _Writer:
        def health_snapshot(self) -> WriteBatcherHealthSnapshot:
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

    class _ImageRetention:
        def snapshot(self) -> SimpleNamespace:
            return SimpleNamespace(
                last_run=0.0,
                deleted_final=0,
                deleted_temp=0,
                deleted_dirs=0,
                failures=0,
            )

    service = HealthService(
        runtime_manager=SimpleNamespace(
            status=SimpleNamespace(value="running"),
            current_generation=1,
        ),
        config_update_coordinator=SimpleNamespace(
            status_snapshot=ConfigUpdateStatusSnapshot(
                state=ConfigUpdateState.RUNNING,
                accepting=True,
                active_updates=0,
                pending_cleanup=0,
                last_failure=None,
            )
        ),
        accounting_service=_Accounting(),
        write_batcher=_Writer(),
        image_retention_service=_ImageRetention(),
        database_paths=(database_path,),
        task_supervisor=task_supervisor,
    )
    service.mark_ready()
    return service


def test_task_supervisor_failure_is_logged_and_visible_in_health_report(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    async def scenario() -> None:
        supervisor = TaskSupervisor()
        service = _make_health_service(
            task_supervisor=supervisor,
            database_path=tmp_path / "health.sqlite",
        )

        healthy = await service.report()
        assert healthy.ok
        assert healthy.as_dict()["checks"]["task_supervisor"] == {
            "status": "ok",
            "reason": "no_failures",
            "counters": {"task_count": 0, "failure_count": 0},
        }

        async def failing_worker() -> None:
            raise RuntimeError("background task boom")

        with caplog.at_level(
            logging.ERROR,
            logger="llm_gateway_core.services.task_supervisor",
        ):
            task = supervisor.create_task(
                failing_worker(), name="remediation-failure-probe"
            )
            await asyncio.wait({task})
            await asyncio.sleep(0)  # let the done-callback run

        assert any(
            "remediation-failure-probe" in record.getMessage()
            for record in caplog.records
        )
        assert len(supervisor.failures) == 1
        assert supervisor.failures[0].task_name == "remediation-failure-probe"
        assert supervisor.failures[0].exception_type == "RuntimeError"

        unhealthy = await service.report()
        assert not unhealthy.ok
        assert unhealthy.as_dict()["checks"]["task_supervisor"] == {
            "status": "error",
            "reason": "task_failed",
            "counters": {"task_count": 0, "failure_count": 1},
        }

        await supervisor.close()

    run_async(scenario())


def test_usage_cleanup_loop_survives_iteration_exception() -> None:
    async def scenario() -> None:
        call_count = 0
        lock = threading.Lock()

        def flaky_cleanup(*, retention_days: int) -> None:
            nonlocal call_count
            with lock:
                call_count += 1
                current = call_count
            if current == 1:
                raise sqlite3.OperationalError("database is locked")

        tokens_db = SimpleNamespace(cleanup_old_records=flaky_cleanup)
        fallback_db = SimpleNamespace(cleanup_old_records=lambda **_kwargs: None)
        rejections_db = SimpleNamespace(cleanup_old_records=lambda **_kwargs: None)

        task = asyncio.create_task(
            main.run_usage_stats_cleanup_loop(
                tokens_db,
                fallback_db,
                rejections_db,
                retention_days=1,
                interval_seconds=0,
            )
        )
        try:
            for _ in range(2_000):
                with lock:
                    if call_count >= 2:
                        break
                await asyncio.sleep(0.001)
            else:
                raise AssertionError(
                    "cleanup loop did not survive the first iteration failure"
                )
        finally:
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task

    run_async(scenario())


class _ExternalInterrupt(BaseException):
    """A non-cancellation BaseException, e.g. what GeneratorExit looks like

    to this code (asyncio special-cases KeyboardInterrupt/SystemExit inside
    Task.__step by letting them escape the event loop entirely, which would
    make this test meaningless -- a plain BaseException subclass propagates
    through `await` normally, like GeneratorExit does).
    """


def test_thread_worker_reraises_external_interrupt_without_masking_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        release = threading.Event()

        def blocking_then_fail() -> None:
            release.wait(timeout=5)
            raise RuntimeError("late worker failure")

        interrupt = _ExternalInterrupt("external interrupt")
        real_shield = asyncio.shield
        calls = 0

        def fake_shield(awaitable, *args, **kwargs):
            nonlocal calls
            calls += 1
            if calls == 1:
                async def _raise_interrupt():
                    raise interrupt

                return _raise_interrupt()
            return real_shield(awaitable, *args, **kwargs)

        monkeypatch.setattr(main.asyncio, "shield", fake_shield)

        outer_task = asyncio.create_task(
            main.run_cancellation_resistant_thread_worker(blocking_then_fail)
        )
        try:
            with pytest.raises(_ExternalInterrupt) as raised:
                await outer_task
            assert raised.value is interrupt
        finally:
            release.set()

        worker_task = next(
            candidate
            for candidate in asyncio.all_tasks()
            if candidate.get_name() == "periodic-blocking-worker"
        )
        for _ in range(2_000):
            if worker_task.done():
                break
            await asyncio.sleep(0.001)
        else:
            raise AssertionError("worker task never finished")
        assert isinstance(worker_task.exception(), RuntimeError)

    run_async(scenario())


def test_healthz_route_exposes_get_and_head() -> None:
    main.app.openapi_schema = None
    schema = main.app.openapi()
    assert set(schema["paths"]["/healthz"]) == {"get", "head"}


def test_healthz_liveness_handler_returns_ok_independent_of_readiness() -> None:
    # Deliberately does not touch main.app.state.services at all: /health's
    # readiness report would be 503 "starting" in this exact situation (see
    # tests/test_health_endpoints.py), but the liveness handler must not
    # consult readiness (HealthService, app.state.services, or any
    # dependency) at all -- it only reports that the process is alive.
    async def scenario() -> None:
        response = await liveness(None)  # type: ignore[arg-type]
        assert response.status_code == 200
        assert json.loads(response.body) == {"status": "ok"}

    run_async(scenario())
