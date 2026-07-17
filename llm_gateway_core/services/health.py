"""Local readiness aggregation for the public health endpoint."""

from __future__ import annotations

import asyncio
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path, PurePath
from typing import TYPE_CHECKING
from urllib.parse import quote

from ..db.write_batcher import WriteBatcher, WriteBatcherState
from ..version import __version__
from .accounting import AccountingHealthState
from .accounting_service import AccountingService
from .config_updates import ConfigUpdateCoordinator, ConfigUpdateState

if TYPE_CHECKING:
    from .image_retention import ImageRetentionService
    from .runtime_config import RuntimeGenerationManager
    from .task_supervisor import TaskSupervisor


SQLITE_PROBE_TTL_SECONDS = 1.0
SQLITE_CONNECT_TIMEOUT_SECONDS = 0.2
SQLITE_PROBE_TIMEOUT_SECONDS = 0.5


class HealthPhase(StrEnum):
    STARTING = "starting"
    READY = "ready"
    STOPPING = "stopping"
    FAILED = "failed"


@dataclass(frozen=True, slots=True)
class HealthCheck:
    name: str
    ok: bool
    reason: str
    counters: tuple[tuple[str, int], ...] = ()

    def as_dict(self) -> dict[str, object]:
        payload: dict[str, object] = {
            "status": "ok" if self.ok else "error",
            "reason": self.reason,
        }
        if self.counters:
            payload["counters"] = dict(self.counters)
        return payload


@dataclass(frozen=True, slots=True)
class HealthReport:
    phase: HealthPhase
    checks: tuple[HealthCheck, ...]

    @property
    def ok(self) -> bool:
        return self.phase is HealthPhase.READY and all(check.ok for check in self.checks)

    def as_dict(self) -> dict[str, object]:
        return {
            "status": "ok" if self.ok else "error",
            "phase": self.phase.value,
            "version": __version__,
            "checks": {check.name: check.as_dict() for check in self.checks},
        }


@dataclass(frozen=True, slots=True)
class _SqliteProbeResult:
    ok: bool
    reason: str
    checked: int
    failed: int


def starting_health_report() -> HealthReport:
    return HealthReport(
        phase=HealthPhase.STARTING,
        checks=(HealthCheck("lifecycle", False, "starting"),),
    )


def failed_health_report() -> HealthReport:
    return HealthReport(
        phase=HealthPhase.FAILED,
        checks=(HealthCheck("lifecycle", False, "failed"),),
    )


def _canonical_database_paths(
    paths: Iterable[Path | str],
) -> tuple[tuple[Path, ...], bool]:
    canonical: list[Path] = []
    seen: set[Path] = set()
    invalid = False
    for raw_path in paths:
        if not isinstance(raw_path, (str, PurePath)):
            invalid = True
            continue
        path = Path(raw_path).expanduser().resolve(strict=False)
        if path in seen:
            continue
        seen.add(path)
        canonical.append(path)
    if not canonical and not invalid:
        raise ValueError("at least one SQLite database path is required")
    return tuple(canonical), invalid


def _sqlite_uri(path: Path) -> str:
    return f"file:{quote(str(path), safe='/')}?mode=ro"


def _probe_sqlite_path_sync(path: Path) -> bool:
    connection: sqlite3.Connection | None = None
    try:
        connection = sqlite3.connect(
            _sqlite_uri(path),
            uri=True,
            timeout=SQLITE_CONNECT_TIMEOUT_SECONDS,
        )
        connection.execute("PRAGMA query_only = ON")
        query_only = connection.execute("PRAGMA query_only").fetchone()
        schema_version = connection.execute("PRAGMA schema_version").fetchone()
        return query_only == (1,) and schema_version is not None
    except (OSError, sqlite3.Error):
        return False
    finally:
        if connection is not None:
            connection.close()


class HealthService:
    """Aggregate process-local readiness without contacting providers."""

    def __init__(
        self,
        *,
        runtime_manager: RuntimeGenerationManager,
        config_update_coordinator: ConfigUpdateCoordinator,
        accounting_service: AccountingService,
        write_batcher: WriteBatcher,
        image_retention_service: ImageRetentionService,
        database_paths: Iterable[Path | str],
        task_supervisor: TaskSupervisor | None = None,
    ) -> None:
        self._runtime_manager = runtime_manager
        self._config_update_coordinator = config_update_coordinator
        self._accounting_service = accounting_service
        self._write_batcher = write_batcher
        self._image_retention_service = image_retention_service
        self._task_supervisor = task_supervisor
        self._database_paths, self._database_paths_invalid = (
            _canonical_database_paths(database_paths)
        )
        self._phase = HealthPhase.STARTING
        self._phase_lock = threading.Lock()
        self._probe_lock = asyncio.Lock()
        self._probe_task: asyncio.Task[_SqliteProbeResult] | None = None
        self._cached_probe: _SqliteProbeResult | None = None
        self._cache_expires_at = 0.0

    @property
    def phase(self) -> HealthPhase:
        with self._phase_lock:
            return self._phase

    @property
    def database_paths(self) -> tuple[Path, ...]:
        return self._database_paths

    def mark_ready(self) -> None:
        with self._phase_lock:
            if self._phase is HealthPhase.READY:
                return
            if self._phase is not HealthPhase.STARTING:
                raise RuntimeError("health service cannot become ready in its current phase")
            self._phase = HealthPhase.READY

    def mark_stopping(self) -> None:
        with self._phase_lock:
            if self._phase is not HealthPhase.STOPPING:
                self._phase = HealthPhase.STOPPING

    def mark_failed(self) -> None:
        with self._phase_lock:
            self._phase = HealthPhase.FAILED

    async def report(self) -> HealthReport:
        initial_phase = self.phase
        if initial_phase is HealthPhase.READY:
            sqlite_result = await self._sqlite_probe()
        else:
            sqlite_result = _SqliteProbeResult(
                ok=False,
                reason="not_ready",
                checked=0,
                failed=0,
            )

        phase = self.phase
        checks = (
            self._lifecycle_check(phase),
            *self._component_checks(),
            HealthCheck(
                "sqlite",
                sqlite_result.ok,
                sqlite_result.reason,
                (
                    ("checked", sqlite_result.checked),
                    ("failed", sqlite_result.failed),
                ),
            ),
        )
        return HealthReport(phase=phase, checks=checks)

    @staticmethod
    def _lifecycle_check(phase: HealthPhase) -> HealthCheck:
        return HealthCheck(
            "lifecycle",
            phase is HealthPhase.READY,
            phase.value,
        )

    def _component_checks(self) -> tuple[HealthCheck, ...]:
        checks = [
            self._runtime_check(),
            self._config_check(),
            self._accounting_check(),
            self._writer_check(),
            self._image_retention_check(),
        ]
        task_supervisor_check = self._task_supervisor_check()
        if task_supervisor_check is not None:
            checks.append(task_supervisor_check)
        return tuple(checks)

    def _runtime_check(self) -> HealthCheck:
        try:
            status = self._runtime_manager.status
            generation = self._runtime_manager.current_generation
            ok = getattr(status, "value", None) == "running" and isinstance(
                generation, int
            ) and not isinstance(generation, bool) and generation > 0
            return HealthCheck(
                "runtime",
                ok,
                "active_generation" if ok else "runtime_unavailable",
                (("generation", generation if isinstance(generation, int) else 0),),
            )
        except Exception:
            return HealthCheck("runtime", False, "snapshot_unavailable")

    def _config_check(self) -> HealthCheck:
        try:
            snapshot = self._config_update_coordinator.status_snapshot
            ok = (
                snapshot.state is ConfigUpdateState.RUNNING
                and snapshot.accepting
                and snapshot.pending_cleanup == 0
            )
            return HealthCheck(
                "config",
                ok,
                "accepting" if ok else "config_unavailable",
                (
                    ("active_updates", snapshot.active_updates),
                    ("pending_cleanup", snapshot.pending_cleanup),
                ),
            )
        except Exception:
            return HealthCheck("config", False, "snapshot_unavailable")

    def _accounting_check(self) -> HealthCheck:
        try:
            snapshot = self._accounting_service.health_snapshot()
            ok = (
                snapshot.state is AccountingHealthState.RUNNING
                and snapshot.initialized
                and snapshot.accepting
            )
            return HealthCheck(
                "accounting",
                ok,
                "accepting" if ok else "accounting_unavailable",
                (
                    ("active_sessions", snapshot.active_sessions),
                    ("pending_source_events", snapshot.pending_source_events),
                ),
            )
        except Exception:
            return HealthCheck("accounting", False, "snapshot_unavailable")

    def _writer_check(self) -> HealthCheck:
        try:
            snapshot = self._write_batcher.health_snapshot()
            ok = (
                snapshot.state is WriteBatcherState.RUNNING
                and snapshot.accepting
                and snapshot.task_running
                and snapshot.overflow == 0
            )
            reason = "running" if ok else (
                "writer_overflow" if snapshot.overflow else "writer_unavailable"
            )
            return HealthCheck(
                "writer",
                ok,
                reason,
                (
                    ("capacity", snapshot.capacity),
                    ("reserved", snapshot.reserved),
                    ("overflow", snapshot.overflow),
                ),
            )
        except Exception:
            return HealthCheck("writer", False, "snapshot_unavailable")

    def _task_supervisor_check(self) -> HealthCheck | None:
        if self._task_supervisor is None:
            return None
        try:
            failures = self._task_supervisor.failures
            task_count = self._task_supervisor.task_count
            ok = len(failures) == 0
            return HealthCheck(
                "task_supervisor",
                ok,
                "no_failures" if ok else "task_failed",
                (
                    ("task_count", task_count),
                    ("failure_count", len(failures)),
                ),
            )
        except Exception:
            return HealthCheck("task_supervisor", False, "snapshot_unavailable")

    def _image_retention_check(self) -> HealthCheck:
        try:
            snapshot = self._image_retention_service.snapshot()
            counters = (
                ("deleted_final", snapshot.deleted_final),
                ("deleted_temp", snapshot.deleted_temp),
                ("deleted_dirs", snapshot.deleted_dirs),
                ("failures", snapshot.failures),
            )
            if any(
                isinstance(value, bool) or not isinstance(value, int) or value < 0
                for _name, value in counters
            ):
                return HealthCheck(
                    "image_retention",
                    False,
                    "snapshot_unavailable",
                )
            if snapshot.last_run is None:
                return HealthCheck(
                    "image_retention",
                    False,
                    "retention_never_run",
                    counters,
                )
            ok = snapshot.failures == 0
            return HealthCheck(
                "image_retention",
                ok,
                "retention_ok" if ok else "retention_failed",
                counters,
            )
        except Exception:
            return HealthCheck(
                "image_retention",
                False,
                "snapshot_unavailable",
            )

    async def _sqlite_probe(self) -> _SqliteProbeResult:
        if self._database_paths_invalid:
            return _SqliteProbeResult(
                ok=False,
                reason="sqlite_configuration_invalid",
                checked=0,
                failed=1,
            )
        loop = asyncio.get_running_loop()
        async with self._probe_lock:
            if self._cached_probe is not None and loop.time() < self._cache_expires_at:
                return self._cached_probe
            task = self._probe_task
            if task is None:
                task = loop.create_task(
                    self._run_and_cache_sqlite_probe(),
                    name="health-sqlite-probe",
                )
                self._probe_task = task
        return await asyncio.shield(task)

    async def _run_and_cache_sqlite_probe(self) -> _SqliteProbeResult:
        try:
            result = await asyncio.wait_for(
                self._run_sqlite_probes(),
                timeout=SQLITE_PROBE_TIMEOUT_SECONDS,
            )
        except TimeoutError:
            result = _SqliteProbeResult(
                ok=False,
                reason="sqlite_timeout",
                checked=len(self._database_paths),
                failed=len(self._database_paths),
            )
        except Exception:
            result = _SqliteProbeResult(
                ok=False,
                reason="sqlite_probe_failed",
                checked=len(self._database_paths),
                failed=len(self._database_paths),
            )

        loop = asyncio.get_running_loop()
        async with self._probe_lock:
            self._cached_probe = result
            self._cache_expires_at = loop.time() + SQLITE_PROBE_TTL_SECONDS
            if self._probe_task is asyncio.current_task():
                self._probe_task = None
        return result

    async def _run_sqlite_probes(self) -> _SqliteProbeResult:
        outcomes = await asyncio.gather(
            *(
                asyncio.to_thread(_probe_sqlite_path_sync, path)
                for path in self._database_paths
            )
        )
        failed = sum(not outcome for outcome in outcomes)
        return _SqliteProbeResult(
            ok=failed == 0,
            reason="readable" if failed == 0 else "sqlite_unavailable",
            checked=len(outcomes),
            failed=failed,
        )
