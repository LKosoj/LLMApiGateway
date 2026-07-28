from __future__ import annotations

import asyncio
import logging
import sqlite3
import threading
import time
from collections.abc import Mapping
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_gateway_core.db import accounting_schema
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingHealthState,
    AccountingOwnerAuditRow,
    AccountingOwnerState,
    AccountingParentLinkState,
    AccountingProjectionMark,
    AccountingReservation,
    AccountingSinkAuditRow,
    AccountingSourceAuditKind,
    AccountingSourceAuditRow,
    AccountingUsage,
    CostSource,
    ProjectionStatus,
    ReconciliationMode,
    SourceAcceptance,
    SourceStatus,
    StoredAccountingEvent,
)
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.deep_research_accounting import (
    DeepResearchAuthIdentity,
    build_deep_research_rollup_event,
)
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests._async_compat import run_async


NOW = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)


def _charge(event_id: str, *, api_key_id: int | None = 7) -> AccountingEvent:
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=event_id,
        kind=AccountingEventKind.CHARGE,
        api_key_id=api_key_id,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/model",
        provider="provider",
        model="model",
        usage=AccountingUsage(
            prompt_tokens=2,
            completion_tokens=1,
            total_tokens=3,
            cost=0.25,
        ),
        cost_source=CostSource.UPSTREAM,
        occurred_at=NOW,
    )


def _rollup(event_id: str) -> AccountingEvent:
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=event_id,
        kind=AccountingEventKind.ROLLUP,
        api_key_id=7,
        method="POST",
        route_template="/v1/web/deep-research",
        operation="web_deep_research",
        gateway_model="gateway/research",
        provider=None,
        model=None,
        usage=AccountingUsage(),
        cost_source=CostSource.RECEIPT_ROLLUP,
        occurred_at=NOW,
        child_event_ids=("usage:v1:http:child",),
        child_fingerprints=("a" * 64,),
    )


def _usage_without_outbox_audit_row(event_id: str) -> AccountingSourceAuditRow:
    return AccountingSourceAuditRow(
        event_id=event_id,
        row_kind=AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX,
        event_kind=AccountingEventKind.CHARGE,
        billing_fingerprint=None,
        api_key_id=7,
        spend_usd=0.25,
        projected_at=None,
        usage_present=True,
        parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
    )


def _unrolled_rollup_audit_row(event_id: str) -> AccountingSourceAuditRow:
    return AccountingSourceAuditRow(
        event_id=event_id,
        row_kind=AccountingSourceAuditKind.OUTBOX,
        event_kind=AccountingEventKind.ROLLUP,
        billing_fingerprint="a" * 64,
        api_key_id=7,
        spend_usd=0.0,
        projected_at=NOW,
        usage_present=True,
        parent_link_state=AccountingParentLinkState.UNROLLED,
    )


def _projection_mark(stored: StoredAccountingEvent) -> AccountingProjectionMark:
    return AccountingProjectionMark(
        event_id=stored.event.event_id,
        billing_fingerprint=stored.billing_fingerprint,
        projected_at=stored.projected_at,
        projection_attempts=stored.projection_attempts,
        last_attempt_at=stored.last_attempt_at,
        last_error_code=stored.last_error_code,
    )


class FakeSource:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._next_row_id = 1
        self.events: dict[str, StoredAccountingEvent] = {}
        self.accept_calls: list[str] = []
        self.projected_calls: list[str] = []
        self.failed_calls: list[tuple[str, AccountingErrorCode]] = []
        self.accept_error: BaseException | None = None
        self.ack_failures = 0
        self.marker_error: BaseException | None = None
        self.list_error: BaseException | None = None
        self.list_calls = 0
        self.list_entered = threading.Event()
        self.list_release: threading.Event | None = None
        self.audit_rows: tuple[AccountingSourceAuditRow, ...] = ()
        self.audit_error: BaseException | None = None
        self.audit_calls = 0
        self.audit_entered = threading.Event()
        self.audit_release: threading.Event | None = None
        self.accept_entered = threading.Event()
        self.accept_release: threading.Event | None = None
        self.ack_entered = threading.Event()
        self.ack_release: threading.Event | None = None

    def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
        self.accept_entered.set()
        if self.accept_release is not None:
            self.accept_release.wait(timeout=2)
        with self._lock:
            self.accept_calls.append(event.event_id)
            if self.accept_error is not None:
                raise self.accept_error
            existing = self.events.get(event.event_id)
            if existing is not None:
                return SourceAcceptance(SourceStatus.DUPLICATE, existing)
            stored = StoredAccountingEvent(
                event=event,
                usage_row_id=self._next_row_id,
                created_at=NOW,
            )
            self._next_row_id += 1
            self.events[event.event_id] = stored
            return SourceAcceptance(SourceStatus.ACCEPTED, stored)

    def list_pending_accounting_events(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[StoredAccountingEvent, ...]:
        self.list_entered.set()
        if self.list_release is not None:
            self.list_release.wait(timeout=2)
        with self._lock:
            self.list_calls += 1
            if self.list_error is not None:
                raise self.list_error
            rows = sorted(
                (row for row in self.events.values() if row.projected_at is None),
                key=lambda row: (row.created_at, row.event.event_id),
            )
            if after is not None:
                rows = [row for row in rows if (row.created_at, row.event.event_id) > after]
            return tuple(rows[:limit])

    def list_accounting_source_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSourceAuditRow, ...]:
        self.audit_entered.set()
        if self.audit_release is not None:
            self.audit_release.wait(timeout=2)
        with self._lock:
            self.audit_calls += 1
            if self.audit_error is not None:
                raise self.audit_error
            rows = sorted(self.audit_rows, key=lambda row: row.event_id)
            if after_event_id is not None:
                rows = [row for row in rows if row.event_id > after_event_id]
            return tuple(rows[:limit])

    def mark_accounting_event_projected(
        self,
        event_id: str,
        fingerprint: str,
        projected_at: datetime,
    ) -> AccountingProjectionMark:
        self.ack_entered.set()
        if self.ack_release is not None:
            self.ack_release.wait(timeout=2)
        with self._lock:
            if self.ack_failures:
                self.ack_failures -= 1
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            stored = self.events[event_id]
            if stored.billing_fingerprint != fingerprint:
                raise AccountingError(AccountingErrorCode.FINGERPRINT_CONFLICT)
            if stored.projected_at is not None:
                return _projection_mark(stored)
            updated = replace(
                stored,
                projected_at=projected_at,
                projection_attempts=stored.projection_attempts + 1,
                last_attempt_at=projected_at,
                last_error_code=None,
            )
            self.events[event_id] = updated
            self.projected_calls.append(event_id)
            return _projection_mark(updated)

    def mark_accounting_projection_failed(
        self,
        event_id: str,
        fingerprint: str,
        reason_code: AccountingErrorCode,
        attempted_at: datetime,
    ) -> AccountingProjectionMark:
        with self._lock:
            if self.marker_error is not None:
                raise self.marker_error
            stored = self.events[event_id]
            if stored.billing_fingerprint != fingerprint:
                raise AccountingError(AccountingErrorCode.FINGERPRINT_CONFLICT)
            if stored.projected_at is not None:
                return _projection_mark(stored)
            updated = replace(
                stored,
                projection_attempts=stored.projection_attempts + 1,
                last_attempt_at=attempted_at,
                last_error_code=reason_code,
            )
            self.events[event_id] = updated
            self.failed_calls.append((event_id, reason_code))
            return _projection_mark(updated)


class FakeSink:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.calls: list[str] = []
        self.applied: set[str] = set()
        self.errors: list[BaseException | None] = []
        self.result_override: object | None = None
        self.call_times: list[float] = []
        self.entered = threading.Event()
        self.release: threading.Event | None = None
        self.audit_rows: tuple[AccountingSinkAuditRow, ...] = ()
        self.audit_error: BaseException | None = None
        self.audit_calls = 0
        self.owner_states: dict[int, AccountingOwnerState] = {}
        self.owner_calls: list[tuple[int, ...]] = []
        self.records: dict[int, ApiKeyRecord] = {}
        self.update_calls: list[tuple[int, dict[str, object], bool]] = []
        self.reset_calls: list[int] = []
        self.due_key_ids: tuple[int, ...] = ()
        self.due_reset_calls: list[tuple[datetime, tuple[int, ...]]] = []
        self.due_reset_entered = threading.Event()
        self.due_reset_release: threading.Event | None = None
        self.delete_calls: list[tuple[int, datetime]] = []

    def apply_spend_event(
        self,
        stored_event: StoredAccountingEvent,
        applied_at: datetime,
    ) -> ProjectionStatus:
        del applied_at
        self.entered.set()
        if self.release is not None:
            self.release.wait(timeout=2)
        with self._lock:
            event_id = stored_event.event.event_id
            self.calls.append(event_id)
            self.call_times.append(time.monotonic())
            if self.errors:
                error = self.errors.pop(0)
                if error is not None:
                    raise error
            if self.result_override is not None:
                return self.result_override  # type: ignore[return-value]
            if event_id in self.applied:
                return ProjectionStatus.ALREADY_APPLIED
            self.applied.add(event_id)
            return ProjectionStatus.APPLIED

    def list_accounting_sink_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSinkAuditRow, ...]:
        with self._lock:
            self.audit_calls += 1
            if self.audit_error is not None:
                raise self.audit_error
            rows = sorted(self.audit_rows, key=lambda row: row.event_id)
            if after_event_id is not None:
                rows = [row for row in rows if row.event_id > after_event_id]
            return tuple(rows[:limit])

    def get_accounting_owner_states(
        self,
        *,
        api_key_ids: tuple[int, ...],
    ) -> tuple[AccountingOwnerAuditRow, ...]:
        with self._lock:
            self.owner_calls.append(api_key_ids)
            return tuple(
                AccountingOwnerAuditRow(
                    api_key_id=api_key_id,
                    owner_state=self.owner_states.get(
                        api_key_id,
                        AccountingOwnerState.ACTIVE,
                    ),
                )
                for api_key_id in api_key_ids
            )

    def update_accounting_key(
        self,
        key_id: int,
        *,
        changes: Mapping[str, object],
        reset_spent: bool = False,
    ) -> ApiKeyRecord | None:
        with self._lock:
            self.update_calls.append((key_id, dict(changes), reset_spent))
            record = self.records.get(key_id)
            if record is None:
                return None
            for name, value in changes.items():
                setattr(record, name, value)
            if reset_spent:
                record.spent_usd = 0.0
            return record

    def reset_accounting_spend(self, key_id: int) -> ApiKeyRecord | None:
        with self._lock:
            self.reset_calls.append(key_id)
            record = self.records.get(key_id)
            if record is None:
                return None
            record.spent_usd = 0.0
            return record

    def list_due_budget_key_ids(
        self,
        *,
        now: datetime,
        limit: int,
        after_key_id: int | None = None,
    ) -> tuple[int, ...]:
        del now
        rows = tuple(key_id for key_id in self.due_key_ids if after_key_id is None or key_id > after_key_id)
        return rows[:limit]

    def reset_due_accounting_budgets(
        self,
        *,
        now: datetime,
        eligible_key_ids: tuple[int, ...],
    ) -> tuple[ApiKeyRecord, ...]:
        self.due_reset_entered.set()
        if self.due_reset_release is not None:
            self.due_reset_release.wait(timeout=2)
        with self._lock:
            self.due_reset_calls.append((now, eligible_key_ids))
            rows = []
            for key_id in eligible_key_ids:
                record = self.records.get(key_id)
                if record is not None and key_id in self.due_key_ids:
                    record.spent_usd = 0.0
                    rows.append(record)
            return tuple(rows)

    def delete_to_accounting_tombstone(
        self,
        key_id: int,
        *,
        deleted_at: datetime,
    ) -> bool:
        with self._lock:
            self.delete_calls.append((key_id, deleted_at))
            return self.records.pop(key_id, None) is not None


def _service(
    source: FakeSource,
    sink: FakeSink,
    **kwargs,
) -> AccountingService:
    kwargs.setdefault("projector_backoff_seconds", 0.01)
    return AccountingService(
        source,
        sink,
        clock=lambda: NOW,
        **kwargs,
    )


async def _wait_until(predicate, *, timeout: float = 1.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("condition was not reached")
        await asyncio.sleep(0.01)


class FailOnceCreateTask:
    def __init__(self, target_name: str, error: BaseException) -> None:
        self._target_name = target_name
        self._error = error
        self._original = asyncio.create_task
        self.coroutine = None

    def __call__(self, coroutine, *, name=None, context=None):
        if name == self._target_name and self.coroutine is None:
            self.coroutine = coroutine
            raise self._error
        return self._original(coroutine, name=name, context=context)


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-start-task-factory-detail"),
        BaseException("terminal-start-task-factory-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_start_task_creation_failure_is_transactional_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        factory = FailOnceCreateTask("accounting-start", failure)
        monkeypatch.setattr(asyncio, "create_task", factory)

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await service.start()
            assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await service.start()
            assert error.value is failure

        assert factory.coroutine is not None
        assert factory.coroutine.cr_frame is None
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "new"
        assert snapshot.accepting is False

        await service.start()

        assert service.health_snapshot().state.value == "running"
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-projector-task-factory-detail"),
        BaseException("terminal-projector-task-factory-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_projector_task_creation_failure_is_a_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        factory = FailOnceCreateTask("accounting-projector", failure)
        monkeypatch.setattr(asyncio, "create_task", factory)

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await service.start()
            assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await service.start()
            assert error.value is failure

        assert factory.coroutine is not None
        assert factory.coroutine.cr_frame is None
        snapshot = service.health_snapshot()
        assert snapshot.accepting is False
        assert snapshot.initialized is False

        if isinstance(failure, Exception):
            assert snapshot.state.value == "blocked"
            await service.start()
            assert service.health_snapshot().state.value == "running"
            await service.stop()
        else:
            assert snapshot.state.value == "failed"
            with pytest.raises(BaseException) as stop_error:
                await service.stop()
            assert stop_error.value is failure
            assert service.health_snapshot().pending_source_events == 0

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-record-task-factory-detail"),
        BaseException("terminal-record-task-factory-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_record_task_creation_failure_releases_capacity_and_source_owner(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(
            source,
            FakeSink(),
            max_owner_tasks=1,
            max_retained_events=1,
        )
        await service.start()
        factory = FailOnceCreateTask("accounting-record", failure)
        monkeypatch.setattr(asyncio, "create_task", factory)
        event = _charge("event-record-task-factory")

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await service.record(event)
            assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await service.record(event)
            assert error.value is failure

        assert factory.coroutine is not None
        assert factory.coroutine.cr_frame is None
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.active_sessions == 0
        assert snapshot.pending_source_events == 0
        assert source.accept_calls == []

        receipt = await service.record(event)

        assert receipt.event_id == event.event_id
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-reconcile-task-factory-detail"),
        BaseException("terminal-reconcile-task-factory-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_reconcile_task_creation_failure_is_safe_and_retryable(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        await service.start()
        factory = FailOnceCreateTask("accounting-reconcile", failure)
        monkeypatch.setattr(asyncio, "create_task", factory)

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await service.reconcile()
            assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await service.reconcile()
            assert error.value is failure

        assert factory.coroutine is not None
        assert factory.coroutine.cr_frame is None
        assert service._reconcile_task is None
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.accepting is True

        report = await service.reconcile()

        assert report.clean is True
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-stop-task-factory-detail"),
        BaseException("terminal-stop-task-factory-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_stop_task_creation_failure_rolls_back_and_allows_retry(
    monkeypatch: pytest.MonkeyPatch,
    failure: BaseException,
) -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        await service.start()
        factory = FailOnceCreateTask("accounting-stop", failure)
        monkeypatch.setattr(asyncio, "create_task", factory)

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await service.stop()
            assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await service.stop()
            assert error.value is failure

        assert factory.coroutine is not None
        assert factory.coroutine.cr_frame is None
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.accepting is True

        await service.stop()

        assert service.health_snapshot().state.value == "stopped"

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-owner-task-detail"),
        BaseException("terminal-owner-task-detail"),
    ),
    ids=("ordinary", "terminal"),
)
def test_stop_drains_owner_result_before_returning_or_reraising_terminal(
    failure: BaseException,
) -> None:
    class OneShotFailureSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            if not self.failed:
                self.accept_entered.set()
                if self.accept_release is not None:
                    self.accept_release.wait(timeout=2)
                self.failed = True
                with self._lock:
                    self.accept_calls.append(event.event_id)
                raise failure
            return super().accept_accounting_event(event)

    async def scenario() -> None:
        source = OneShotFailureSource()
        source.accept_release = threading.Event()
        service = _service(source, FakeSink())
        await service.start()
        record_task = asyncio.create_task(service.record(_charge("event-stop-owner-result")))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)
        stop_task = asyncio.create_task(service.stop())
        await _wait_until(lambda: service.health_snapshot().state.value == "stopping")
        await asyncio.sleep(0)

        source.accept_release.set()
        record_result, stop_result = await asyncio.gather(
            record_task,
            stop_task,
            return_exceptions=True,
        )

        if isinstance(failure, Exception):
            assert isinstance(record_result, AccountingError)
            assert record_result.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert stop_result is None
            assert service.health_snapshot().state.value == "stopped"
        else:
            assert record_result is failure
            assert stop_result is failure
            assert service.health_snapshot().state.value == "failed"
        snapshot = service.health_snapshot()
        assert snapshot.active_sessions == 0
        assert snapshot.pending_source_events == 0

    run_async(scenario())


def test_stop_preserves_terminal_owner_removed_by_done_callback() -> None:
    terminal = BaseException("terminal-completed-owner-detail")

    class OneShotTerminalSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.failed = False

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            if not self.failed:
                self.failed = True
                with self._lock:
                    self.accept_calls.append(event.event_id)
                raise terminal
            return super().accept_accounting_event(event)

    async def scenario() -> None:
        source = OneShotTerminalSource()
        service = _service(source, FakeSink())
        await service.start()
        event = _charge("event-completed-terminal-owner")

        with pytest.raises(BaseException) as record_error:
            await service.record(event)

        assert record_error.value is terminal
        await _wait_until(lambda: service.health_snapshot().active_sessions == 0)
        assert service._terminal_owner_error is terminal

        with pytest.raises(BaseException) as stop_error:
            await service.stop()

        assert stop_error.value is terminal
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.active_sessions == 0
        assert snapshot.pending_source_events == 0
        assert source.projected_calls == [event.event_id]

    run_async(scenario())


def test_start_drains_pending_before_opening_admission() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        source.accept_accounting_event(_charge("event-startup"))
        service = _service(source, sink)

        await service.start()

        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.accepting is True
        assert source.projected_calls == ["event-startup"]
        assert sink.applied == {"event-startup"}
        await service.stop()

    run_async(scenario())


def test_clean_projector_waits_for_wakeup_without_idle_db_polling() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        assert source.list_calls == 1

        await asyncio.sleep(0.05)

        assert source.list_calls == 1
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "list_error",
    (
        AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED),
        RuntimeError("sensitive-startup-detail"),
    ),
)
def test_start_pending_read_failure_is_safe_and_never_opens_admission(
    list_error: BaseException,
) -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.list_error = list_error
        service = _service(source, FakeSink())

        with pytest.raises(AccountingError) as error:
            await service.start()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        assert "sensitive" not in repr(error.value)
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "blocked"
        assert snapshot.accepting is False
        source.list_error = None

        await service.start()

        assert service.health_snapshot().state.value == "running"
        await service.stop()

    run_async(scenario())


def test_start_backlog_over_capacity_is_bounded_and_retryable() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_accounting_event(_charge("event-start-cap-a"))
        source.accept_accounting_event(_charge("event-start-cap-b"))
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(20))
        service = _service(
            source,
            sink,
            max_retained_events=1,
            projector_batch_size=2,
        )

        with pytest.raises(AccountingError) as error:
            await service.start()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "blocked"
        assert snapshot.pending_source_events == 1
        sink.errors.clear()

        await service.start()

        assert source.projected_calls == ["event-start-cap-a", "event-start-cap-b"]
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_start_cancellation_does_not_cancel_pending_projection() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.release = threading.Event()
        source.accept_accounting_event(_charge("event-start-cancel"))
        service = _service(source, sink)
        start_task = asyncio.create_task(service.start())
        assert await asyncio.to_thread(sink.entered.wait, 1)

        start_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await start_task
        sink.release.set()

        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert source.projected_calls == ["event-start-cancel"]
        assert sink.applied == {"event-start-cancel"}
        await service.stop()

    run_async(scenario())


def test_repeated_and_concurrent_start_share_one_workflow() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())

        await asyncio.gather(service.start(), service.start())
        await service.start()

        assert source.list_calls == 1
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.accepting is True
        await service.stop()

    run_async(scenario())


def test_stop_racing_with_start_never_reopens_admission() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.release = threading.Event()
        source.accept_accounting_event(_charge("event-start-stop-race"))
        service = _service(source, sink)
        start_task = asyncio.create_task(service.start())
        assert await asyncio.to_thread(sink.entered.wait, 1)

        stop_task = asyncio.create_task(service.stop())
        await _wait_until(lambda: service.health_snapshot().state.value == "stopping")
        sink.release.set()

        with pytest.raises(AccountingError) as start_error:
            await start_task
        assert start_error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        await stop_task
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "stopped"
        assert snapshot.accepting is False
        assert source.projected_calls == ["event-start-stop-race"]

    run_async(scenario())


def test_record_keyed_charge_returns_exact_receipt_and_acknowledges_source() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-charge"))

        assert receipt.source_status is SourceStatus.ACCEPTED
        assert receipt.projection_status is ProjectionStatus.APPLIED
        assert receipt.event_id == "event-charge"
        assert receipt.usage_row_id == 1
        assert sink.calls == ["event-charge"]
        assert source.projected_calls == ["event-charge"]
        await service.stop()

    run_async(scenario())


def test_projected_duplicate_skips_sink_and_returns_already_applied() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        event = _charge("event-projected-duplicate")
        await service.start()
        first = await service.record(event)
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))

        duplicate = await service.record(event)

        assert first.projection_status is ProjectionStatus.APPLIED
        assert duplicate.source_status is SourceStatus.DUPLICATE
        assert duplicate.projection_status is ProjectionStatus.ALREADY_APPLIED
        assert sink.calls == [event.event_id]
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_projected_duplicate_clears_pending_after_ambiguous_ack_error() -> None:
    class AckCommitThenErrorSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.ack_committed = threading.Event()
            self.ack_return_release = threading.Event()
            self.fail_first_ack_after_commit = True

        def mark_accounting_event_projected(
            self,
            event_id: str,
            fingerprint: str,
            projected_at: datetime,
        ) -> AccountingProjectionMark:
            updated = super().mark_accounting_event_projected(
                event_id,
                fingerprint,
                projected_at,
            )
            if self.fail_first_ack_after_commit:
                self.fail_first_ack_after_commit = False
                self.ack_committed.set()
                self.ack_return_release.wait(timeout=2)
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            return updated

    async def scenario() -> None:
        source = AckCommitThenErrorSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        event = _charge("event-projected-owner-race")
        owner_task = asyncio.create_task(service.record(event))
        assert await asyncio.to_thread(source.ack_committed.wait, 1)

        duplicate = await service.record(event)
        assert duplicate.projection_status is ProjectionStatus.ALREADY_APPLIED
        assert sink.calls == [event.event_id]

        source.ack_return_release.set()
        owner = await owner_task
        assert owner.projection_status is ProjectionStatus.PENDING
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_concurrent_duplicate_transient_after_peer_ack_clears_pending() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.release = threading.Event()
        service = _service(source, sink)
        event = _charge("event-concurrent-duplicate-ack")
        await service.start()
        first_task = asyncio.create_task(service.record(event))
        assert await asyncio.to_thread(sink.entered.wait, 1)
        duplicate_task = asyncio.create_task(service.record(event))
        await _wait_until(lambda: len(source.accept_calls) == 2)
        sink.errors.extend([None, AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)])
        sink.release.set()

        first, duplicate = await asyncio.gather(first_task, duplicate_task)

        assert first.projection_status is ProjectionStatus.APPLIED
        assert duplicate.projection_status is ProjectionStatus.ALREADY_APPLIED
        assert source.projected_calls == [event.event_id]
        assert sink.calls == [event.event_id, event.event_id]
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "event",
    (_charge("event-unattributed", api_key_id=None), _rollup("event-rollup")),
)
def test_record_no_sink_events_acknowledges_without_sink(event: AccountingEvent) -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(event)

        assert receipt.projection_status is ProjectionStatus.NO_SINK
        assert sink.calls == []
        assert source.projected_calls == [event.event_id]
        assert receipt.child_event_ids == event.child_event_ids
        assert receipt.child_fingerprints == event.child_fingerprints
        await service.stop()

    run_async(scenario())


def test_transient_sink_failure_returns_pending_and_projector_replays() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-transient"))

        assert receipt.projection_status is ProjectionStatus.PENDING
        assert service.health_snapshot().accepting is True
        assert service.health_snapshot().pending_source_events == 1
        assert source.failed_calls == [("event-transient", AccountingErrorCode.PROJECTION_WRITE_FAILED)]
        await _wait_until(lambda: source.projected_calls == ["event-transient"])
        assert sink.calls == ["event-transient", "event-transient"]
        assert len(sink.applied) == 1
        await service.stop()

    run_async(scenario())


def test_repeated_transient_projection_obeys_configured_backoff() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(20))
        service = _service(
            source,
            sink,
            projector_backoff_seconds=0.05,
        )
        await service.start()

        receipt = await service.record(_charge("event-backoff"))
        assert receipt.projection_status is ProjectionStatus.PENDING
        await _wait_until(lambda: len(sink.call_times) >= 3)

        assert sink.call_times[2] - sink.call_times[1] >= 0.04
        sink.errors.clear()
        await _wait_until(lambda: source.projected_calls == ["event-backoff"])
        await service.stop()

    run_async(scenario())


def test_zero_projector_backoff_is_rejected() -> None:
    with pytest.raises(AccountingError) as error:
        AccountingService(
            FakeSource(),
            FakeSink(),
            projector_backoff_seconds=0,
        )

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_failed_attempt_marker_failure_blocks_admission_with_source_error() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.marker_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(20))
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-marker-failure"))

        assert receipt.projection_status is ProjectionStatus.PENDING
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "blocked"
        assert snapshot.accepting is False
        assert snapshot.last_error_code is AccountingErrorCode.SOURCE_WRITE_FAILED
        source.marker_error = None
        sink.errors.clear()
        await _wait_until(lambda: source.projected_calls == ["event-marker-failure"])
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        await service.stop()

    run_async(scenario())


def test_permanent_failed_attempt_marker_error_is_not_hidden_as_pending() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.marker_error = AccountingError(AccountingErrorCode.FINGERPRINT_CONFLICT)
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-permanent-marker"))

        assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.fingerprint_conflicts == 1
        assert source.projected_calls == []
        source.marker_error = None
        with pytest.raises(AccountingError):
            await service.stop()

    run_async(scenario())


@pytest.mark.parametrize("mode", ["wrong-type", "different-event"])
def test_malformed_failed_marker_result_fails_closed(mode: str) -> None:
    class MalformedMarkerSource(FakeSource):
        def mark_accounting_projection_failed(
            self,
            event_id: str,
            fingerprint: str,
            reason_code: AccountingErrorCode,
            attempted_at: datetime,
        ) -> AccountingProjectionMark:
            del attempted_at
            if mode == "wrong-type":
                return object()  # type: ignore[return-value]
            other = _charge("event-marker-result-other")
            return AccountingProjectionMark(
                event_id=other.event_id,
                billing_fingerprint=other.billing_fingerprint,
                projected_at=None,
                projection_attempts=1,
                last_attempt_at=NOW,
                last_error_code=reason_code,
            )

    async def scenario() -> None:
        source = MalformedMarkerSource()
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge(f"event-marker-result-{mode}"))

        assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.pending_source_events == 1
        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED

    run_async(scenario())


@pytest.mark.parametrize(
    "invalid_status",
    (ProjectionStatus.PENDING, ProjectionStatus.NO_SINK, object()),
)
def test_keyed_sink_invalid_status_fails_closed_without_source_ack(
    invalid_status: object,
) -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.result_override = invalid_status
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-invalid-sink-status"))

        assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
        assert source.projected_calls == []
        assert service.health_snapshot().state.value == "failed"
        assert service.health_snapshot().pending_source_events == 1
        with pytest.raises(AccountingError):
            await service.stop()

    run_async(scenario())


@pytest.mark.parametrize("mode", ["wrong-type", "different-event", "unprojected"])
def test_malformed_source_ack_result_never_reports_clean_projection(mode: str) -> None:
    class MalformedAckSource(FakeSource):
        def mark_accounting_event_projected(
            self,
            event_id: str,
            fingerprint: str,
            projected_at: datetime,
        ) -> AccountingProjectionMark:
            if mode == "wrong-type":
                return object()  # type: ignore[return-value]
            if mode == "unprojected":
                return _projection_mark(self.events[event_id])
            other = _charge("event-ack-result-other")
            return AccountingProjectionMark(
                event_id=other.event_id,
                billing_fingerprint=other.billing_fingerprint,
                projected_at=NOW,
                projection_attempts=1,
                last_attempt_at=NOW,
                last_error_code=None,
            )

    async def scenario() -> None:
        source = MalformedAckSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        event = _charge(f"event-ack-result-{mode}")

        with pytest.raises(AccountingError) as error:
            await service.record(event)

        assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.pending_source_events == 1
        assert source.list_pending_accounting_events(limit=10)[0].event.event_id == event.event_id
        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_start_tolerates_permanent_failure_and_continues_to_later_event() -> None:
    """A permanent sink error (ORPHAN_SINK/FINGERPRINT_CONFLICT) hit while
    draining pending events at startup must not take the whole service down:
    the offending event is logged and left unsettled (``projected_at`` stays
    ``NULL``) for operator follow-up, but the drain continues on to later
    events instead of halting, and ``start()`` succeeds with the service
    RUNNING/accepting."""

    async def scenario() -> None:
        source = FakeSource()
        source.accept_accounting_event(_charge("event-a-permanent"))
        source.accept_accounting_event(_charge("event-b-should-still-run"))
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.ORPHAN_SINK))
        service = _service(source, sink)

        await service.start()

        assert sink.calls == ["event-a-permanent", "event-b-should-still-run"]
        assert source.projected_calls == ["event-b-should-still-run"]
        snapshot = service.health_snapshot()
        assert snapshot.state is AccountingHealthState.RUNNING
        assert snapshot.accepting is True
        assert snapshot.sink_orphans == 1
        assert snapshot.projection_attempts == 2
        assert snapshot.pending_source_events == 1

    run_async(scenario())


class _PersistentSinkFailureSink(FakeSink):
    """Always fails apply_spend_event for one api_key_id, forever.

    Unlike FakeSink.errors (a one-shot queue), this reproduces a ledger row
    that is durably broken, so repeated projection attempts (background
    drain, stop()) keep re-discovering the same permanent failure instead of
    succeeding on the next attempt.
    """

    def __init__(self, *, failing_key_id: int, code: AccountingErrorCode) -> None:
        super().__init__()
        self._failing_key_id = failing_key_id
        self._code = code

    def apply_spend_event(
        self,
        stored_event: StoredAccountingEvent,
        applied_at: datetime,
    ) -> ProjectionStatus:
        if stored_event.event.api_key_id == self._failing_key_id:
            raise AccountingError(self._code)
        return super().apply_spend_event(stored_event, applied_at)


def test_permanent_projection_failure_does_not_fence_reserve_for_the_same_key() -> None:
    """A key whose event permanently fails projection is not frozen: since
    quarantine no longer exists, reserve() for that same api_key_id keeps
    being admitted normally -- only the specific event stays unsettled."""

    async def scenario() -> None:
        source = FakeSource()
        sink = _PersistentSinkFailureSink(
            failing_key_id=7,
            code=AccountingErrorCode.ORPHAN_SINK,
        )
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-permanent-key-7", api_key_id=7))
        assert receipt.projection_status is ProjectionStatus.PENDING

        reservation = await service.reserve(
            request_id="request-after-permanent-failure",
            api_key_id=7,
            budget_usd=None,
            spent_usd=0.0,
            rpm_limit=None,
            tpm_limit=None,
            estimate_usd=0.0,
        )
        assert reservation.api_key_id == 7
        assert await service.release(reservation) is True

    run_async(scenario())


def test_stop_still_refuses_clean_drain_when_permanent_failure_persists() -> None:
    """stop() must still refuse a clean drain while a permanently-failing
    event remains unsettled, even though the service itself no longer
    crashes because of it. Money-safety: staying up buys availability, it
    does not forgive the unresolved charge."""

    async def scenario() -> None:
        source = FakeSource()
        sink = _PersistentSinkFailureSink(
            failing_key_id=7,
            code=AccountingErrorCode.FINGERPRINT_CONFLICT,
        )
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-permanent-stop", api_key_id=7))
        assert receipt.projection_status is ProjectionStatus.PENDING

        with pytest.raises(AccountingError) as drained:
            await service.stop()
        assert drained.value.code is AccountingErrorCode.DRAIN_FAILED
        assert service.health_snapshot().state is AccountingHealthState.FAILED

    run_async(scenario())


def test_start_full_reconcile_is_skipped_when_permanent_failure_is_tolerated() -> None:
    """Demonstrates that the full startup audit is genuinely skipped (not
    merely already-clean by default) once a permanent projection failure is
    tolerated during the pending drain: an unrelated structural finding
    seeded in ``source.audit_rows`` -- the same kind of finding that
    ``test_start_structural_audit_finding_is_sticky_and_never_opens_admission``
    proves is normally a sticky, unconditional start() failure -- goes
    uncaught here, because _start_workflow skips ``_full_reconcile_locked()``
    entirely whenever the drain tolerated a permanent failure.

    This is a deliberate, known trade-off of not crashing start() on a
    permanent per-event projection failure: while any such event is being
    tolerated, a *separate* real structural problem elsewhere in the ledger
    will not be caught by that same start() call either.
    """

    async def scenario() -> None:
        source = FakeSource()
        source.audit_rows = (_usage_without_outbox_audit_row("audit-unrelated-finding"),)
        source.accept_accounting_event(_charge("event-tolerated", api_key_id=9))
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.ORPHAN_SINK))
        service = _service(source, sink)

        await service.start()

        snapshot = service.health_snapshot()
        assert snapshot.state is AccountingHealthState.RUNNING
        assert snapshot.accepting is True
        assert snapshot.last_full_reconciliation is None

    run_async(scenario())


def test_start_rejects_nonadvancing_full_pending_page() -> None:
    class RepeatingPageSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.cached_page: tuple[StoredAccountingEvent, ...] | None = None
            self.page_calls = 0

        def list_pending_accounting_events(
            self,
            *,
            limit: int,
            after: tuple[datetime, str] | None = None,
        ) -> tuple[StoredAccountingEvent, ...]:
            del after
            self.page_calls += 1
            if self.page_calls > 2:
                raise RuntimeError("nonadvancing-page-was-not-rejected")
            if self.cached_page is None:
                self.cached_page = super().list_pending_accounting_events(limit=limit)
            return self.cached_page

    async def scenario() -> None:
        source = RepeatingPageSource()
        source.accept_accounting_event(_charge("event-repeated-page"))
        service = _service(source, FakeSink(), projector_batch_size=1)

        with pytest.raises(AccountingError) as error:
            await service.start()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert source.page_calls == 2

    run_async(scenario())


def test_malformed_runtime_pending_page_has_typed_terminal_stop() -> None:
    class MalformedPageSource(FakeSource):
        malformed = False

        def list_pending_accounting_events(
            self,
            *,
            limit: int,
            after: tuple[datetime, str] | None = None,
        ) -> tuple[StoredAccountingEvent, ...]:
            if self.malformed:
                return (object(),)  # type: ignore[return-value]
            return super().list_pending_accounting_events(limit=limit, after=after)

    async def scenario() -> None:
        source = MalformedPageSource()
        sink = FakeSink()
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))
        service = _service(source, sink)
        await service.start()
        source.malformed = True

        receipt = await service.record(_charge("event-malformed-runtime-page"))

        assert receipt.projection_status is ProjectionStatus.PENDING
        await _wait_until(lambda: service.health_snapshot().state.value == "failed")
        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert "object" not in repr(stop_error.value)
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_source_failure_is_retained_and_recovers_without_new_admission() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-retained"))

        assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
        assert service.health_snapshot().state.value == "blocked"
        assert service.health_snapshot().pending_source_events == 1
        source.accept_error = None
        await _wait_until(lambda: source.projected_calls == ["event-retained"])
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize("mode", ["wrong-type", "different-event"])
def test_malformed_source_acceptance_fails_closed_without_leaking_reservation(
    mode: str,
) -> None:
    class MalformedSource(FakeSource):
        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            if mode == "wrong-type":
                return object()  # type: ignore[return-value]
            stored = StoredAccountingEvent(
                event=_charge("event-source-result-other"),
                usage_row_id=99,
                created_at=NOW,
            )
            return SourceAcceptance(SourceStatus.ACCEPTED, stored)

    async def scenario() -> None:
        source = MalformedSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge(f"event-source-result-{mode}"))

        assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.pending_source_events == 0
        assert sink.calls == []
        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_retained_retry_skips_sink_for_projected_duplicate() -> None:
    class RetryProjectedSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_accept = False

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            if self.fail_next_accept:
                with self._lock:
                    self.accept_calls.append(event.event_id)
                    self.fail_next_accept = False
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            return super().accept_accounting_event(event)

    async def scenario() -> None:
        source = RetryProjectedSource()
        event = _charge("event-retained-projected")
        accepted = source.accept_accounting_event(event)
        source.mark_accounting_event_projected(
            event.event_id,
            accepted.stored_event.billing_fingerprint,
            NOW,
        )
        source.accept_calls.clear()
        source.projected_calls.clear()
        source.fail_next_accept = True
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(event)

        assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
        await _wait_until(
            lambda: (
                source.accept_calls == [event.event_id, event.event_id]
                and service.health_snapshot().pending_source_events == 0
            )
        )
        assert sink.calls == []
        assert source.projected_calls == []
        await service.stop()

    run_async(scenario())


def test_retained_retry_waits_for_inline_projection_owner_before_recovery() -> None:
    class AckReturnBarrierSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.fail_next_accept = False
            self.ack_committed = threading.Event()
            self.ack_return_release = threading.Event()

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            if self.fail_next_accept:
                with self._lock:
                    self.accept_calls.append(event.event_id)
                    self.fail_next_accept = False
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            return super().accept_accounting_event(event)

        def mark_accounting_event_projected(
            self,
            event_id: str,
            fingerprint: str,
            projected_at: datetime,
        ) -> AccountingProjectionMark:
            updated = super().mark_accounting_event_projected(
                event_id,
                fingerprint,
                projected_at,
            )
            self.ack_committed.set()
            self.ack_return_release.wait(timeout=2)
            return updated

    async def scenario() -> None:
        source = AckReturnBarrierSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        event = _charge("event-retained-peer-ack-race")
        owner_task = asyncio.create_task(service.record(event))
        assert await asyncio.to_thread(source.ack_committed.wait, 1)

        source.fail_next_accept = True
        with pytest.raises(AccountingError) as error:
            await service.record(event)
        assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED

        await asyncio.sleep(0.05)
        assert len(source.accept_calls) == 2
        assert service.health_snapshot().pending_source_events == 1
        assert sink.calls == [event.event_id]

        source.ack_return_release.set()
        owner = await owner_task
        assert owner.projection_status is ProjectionStatus.APPLIED
        await _wait_until(lambda: len(source.accept_calls) >= 3)
        await _wait_until(lambda: source.list_calls >= 2)
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_unexpected_source_exception_is_credential_safe_and_retained() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = RuntimeError("sensitive-source-path SQL")
        service = _service(source, FakeSink())
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-safe-source-error"))

        assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        assert "sensitive" not in repr(error.value)
        assert service.health_snapshot().pending_source_events == 1
        source.accept_error = None
        await _wait_until(lambda: source.projected_calls == ["event-safe-source-error"])
        await service.stop()

    run_async(scenario())


def test_invalid_clock_after_start_fails_closed_without_masking_state() -> None:
    current = NOW

    def clock() -> datetime:
        return current

    async def scenario() -> None:
        nonlocal current
        service = AccountingService(FakeSource(), FakeSink(), clock=clock)
        await service.start()
        current = datetime(2026, 7, 14)

        with pytest.raises(AccountingError) as record_error:
            await service.record(_charge("event-invalid-clock"))

        assert record_error.value.code is AccountingErrorCode.INVALID_CONTRACT
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.last_error_code is AccountingErrorCode.INVALID_CONTRACT
        assert snapshot.last_error_at is None

        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_ordinary_clock_error_is_sanitized_and_stop_fails_closed() -> None:
    broken = False

    def clock() -> datetime:
        if broken:
            raise RuntimeError("clock-sensitive-detail")
        return NOW

    async def scenario() -> None:
        nonlocal broken
        service = AccountingService(FakeSource(), FakeSink(), clock=clock)
        await service.start()
        broken = True

        with pytest.raises(AccountingError) as record_error:
            await service.record(_charge("event-ordinary-clock"))

        assert record_error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        assert "sensitive" not in repr(record_error.value)
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.last_error_at is None

        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert "sensitive" not in repr(stop_error.value)
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_terminal_clock_error_preserves_identity_and_fails_closed() -> None:
    terminal: BaseException | None = None

    def clock() -> datetime:
        if terminal is not None:
            raise terminal
        return NOW

    async def scenario() -> None:
        nonlocal terminal
        service = AccountingService(FakeSource(), FakeSink(), clock=clock)
        await service.start()
        terminal = BaseException("terminal-clock-sensitive-detail")

        with pytest.raises(BaseException) as record_error:
            await service.record(_charge("event-terminal-clock"))

        assert record_error.value is terminal
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.last_error_code is AccountingErrorCode.ACCOUNTING_FAILED
        assert snapshot.last_error_at is None

        with pytest.raises(BaseException) as stop_error:
            await service.stop()
        assert stop_error.value is terminal
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_projector_terminal_base_exception_is_observed_and_fails_closed() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        loop_errors: list[dict] = []
        asyncio.get_running_loop().set_exception_handler(lambda _loop, context: loop_errors.append(context))
        await service.start()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        terminal = BaseException("terminal-sensitive-detail")
        source.list_error = terminal

        with pytest.raises(AccountingError):
            await service.record(_charge("event-projector-terminal"))

        await _wait_until(lambda: service.health_snapshot().state.value == "failed")
        assert service.health_snapshot().accepting is False
        assert loop_errors == []
        source.accept_error = None
        source.list_error = None
        with pytest.raises(BaseException) as stop_error:
            await service.stop()
        assert stop_error.value is terminal
        assert source.projected_calls == ["event-projector-terminal"]
        assert service.health_snapshot().state.value == "failed"
        assert loop_errors == []

    run_async(scenario())


def test_permanent_source_conflict_is_not_retained_for_retry() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = AccountingError(AccountingErrorCode.FINGERPRINT_CONFLICT)
        service = _service(source, FakeSink())
        await service.start()

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-source-conflict"))

        assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
        assert service.health_snapshot().state.value == "failed"
        assert service.health_snapshot().pending_source_events == 0
        with pytest.raises(AccountingError) as stop_error:
            await service.stop()
        assert stop_error.value.code is AccountingErrorCode.DRAIN_FAILED

    run_async(scenario())


def test_caller_cancellation_does_not_cancel_full_projection_workflow() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.release = threading.Event()
        service = _service(source, sink)
        await service.start()
        task = asyncio.create_task(service.record(_charge("event-cancelled")))
        assert await asyncio.to_thread(sink.entered.wait, 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        sink.release.set()

        await _wait_until(lambda: source.projected_calls == ["event-cancelled"])
        await _wait_until(lambda: service.health_snapshot().active_sessions == 0)
        assert sink.applied == {"event-cancelled"}
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_caller_cancellation_during_source_accept_keeps_owned_workflow() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_release = threading.Event()
        service = _service(source, FakeSink())
        await service.start()
        task = asyncio.create_task(service.record(_charge("event-cancelled-source")))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        source.accept_release.set()

        await _wait_until(lambda: source.projected_calls == ["event-cancelled-source"])
        await _wait_until(lambda: service.health_snapshot().active_sessions == 0)
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_caller_cancellation_after_sink_commit_still_acknowledges_source() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.ack_release = threading.Event()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        task = asyncio.create_task(service.record(_charge("event-cancelled-ack")))
        assert await asyncio.to_thread(source.ack_entered.wait, 1)
        assert sink.applied == {"event-cancelled-ack"}

        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        source.ack_release.set()

        await _wait_until(lambda: source.projected_calls == ["event-cancelled-ack"])
        await _wait_until(lambda: service.health_snapshot().active_sessions == 0)
        assert sink.calls == ["event-cancelled-ack"]
        await service.stop()

    run_async(scenario())


def test_sink_commit_before_ack_failure_replays_without_double_spend() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        source.ack_failures = 1
        service = _service(source, sink)
        await service.start()

        receipt = await service.record(_charge("event-ack-retry"))

        assert receipt.projection_status is ProjectionStatus.PENDING
        assert service.health_snapshot().pending_source_events == 1
        await _wait_until(lambda: source.projected_calls == ["event-ack-retry"])
        assert service.health_snapshot().pending_source_events == 0
        assert sink.calls == ["event-ack-retry", "event-ack-retry"]
        assert sink.applied == {"event-ack-retry"}
        await service.stop()

    run_async(scenario())


def test_owner_task_capacity_rejects_before_second_source_write() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        source.accept_release = threading.Event()
        service = _service(source, sink, max_owner_tasks=1, max_retained_events=1)
        await service.start()
        first = asyncio.create_task(service.record(_charge("event-capacity-a")))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-capacity-b"))

        assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        assert source.accept_calls == []
        source.accept_release.set()
        await first
        assert source.accept_calls == ["event-capacity-a"]
        await service.stop()

    run_async(scenario())


def test_retained_capacity_rejects_before_second_source_write() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_release = threading.Event()
        service = _service(
            source,
            FakeSink(),
            max_owner_tasks=2,
            max_retained_events=1,
        )
        await service.start()
        first = asyncio.create_task(service.record(_charge("event-retained-cap-a")))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-retained-cap-b"))

        assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        source.accept_release.set()
        await first
        assert "event-retained-cap-b" not in source.accept_calls
        await service.stop()

    run_async(scenario())


def test_concurrent_duplicate_source_snapshots_keep_unique_capacity_reserved() -> None:
    class BarrierSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.first_a = threading.Event()
            self.second_a = threading.Event()
            self.release_first_a = threading.Event()
            self.release_second_a = threading.Event()

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            result = super().accept_accounting_event(event)
            if event.event_id == "event-shared-cap-a":
                if result.status is SourceStatus.ACCEPTED:
                    self.first_a.set()
                    self.release_first_a.wait(timeout=2)
                else:
                    self.second_a.set()
                    self.release_second_a.wait(timeout=2)
            return result

    async def scenario() -> None:
        source = BarrierSource()
        sink = FakeSink()
        service = _service(
            source,
            sink,
            max_retained_events=1,
            max_owner_tasks=3,
        )
        await service.start()
        event_a = _charge("event-shared-cap-a")
        first = asyncio.create_task(service.record(event_a))
        assert await asyncio.to_thread(source.first_a.wait, 1)
        duplicate = asyncio.create_task(service.record(event_a))
        assert await asyncio.to_thread(source.second_a.wait, 1)

        source.release_first_a.set()
        await first
        assert service.health_snapshot().pending_source_events == 1

        with pytest.raises(AccountingError) as rejected:
            await service.record(_charge("event-shared-cap-b"))
        assert rejected.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        assert "event-shared-cap-b" not in source.accept_calls

        source.release_second_a.set()
        await duplicate
        assert service.health_snapshot().pending_source_events == 0
        assert sink.calls == [event_a.event_id, event_a.event_id]
        await service.stop()

    run_async(scenario())


def test_last_source_owner_release_reopens_after_projector_recovery() -> None:
    class BarrierSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self._call_guard = threading.Lock()
            self._call_no = 0
            self.first = threading.Event()
            self.second = threading.Event()
            self.release_first = threading.Event()
            self.release_second = threading.Event()

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            with self._call_guard:
                self._call_no += 1
                call = self._call_no
            if call == 1:
                self.first.set()
                self.release_first.wait(timeout=2)
                with self._lock:
                    self.accept_calls.append(event.event_id)
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if call == 2:
                self.second.set()
                self.release_second.wait(timeout=2)
            return super().accept_accounting_event(event)

    async def scenario() -> None:
        source = BarrierSource()
        service = _service(source, FakeSink())
        await service.start()
        event = _charge("event-last-source-owner")
        first = asyncio.create_task(service.record(event))
        assert await asyncio.to_thread(source.first.wait, 1)
        second = asyncio.create_task(service.record(event))
        assert await asyncio.to_thread(source.second.wait, 1)
        source.release_first.set()
        with pytest.raises(AccountingError):
            await first
        await _wait_until(lambda: source.projected_calls == [event.event_id])
        assert service.health_snapshot().state.value == "blocked"

        source.release_second.set()
        receipt = await second

        assert receipt.projection_status is ProjectionStatus.ALREADY_APPLIED
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_retained_recovery_drains_more_than_one_projector_batch() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        source.accept_release = threading.Event()
        service = _service(
            source,
            FakeSink(),
            max_owner_tasks=3,
            max_retained_events=3,
            projector_batch_size=2,
        )
        await service.start()
        tasks = [asyncio.create_task(service.record(_charge(f"event-retained-wave-{index}"))) for index in range(3)]
        assert await asyncio.to_thread(source.accept_entered.wait, 1)
        source.accept_release.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert all(
            isinstance(result, AccountingError) and result.code is AccountingErrorCode.SOURCE_WRITE_FAILED
            for result in results
        )
        source.accept_error = None

        await _wait_until(lambda: len(source.projected_calls) == 3)
        await _wait_until(lambda: service.health_snapshot().state.value == "running")
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_pending_backlog_is_bounded_by_unsettled_event_capacity() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(100))
        service = _service(
            source,
            sink,
            max_owner_tasks=2,
            max_retained_events=1,
        )
        await service.start()
        receipt = await service.record(_charge("event-pending-cap-a"))
        assert receipt.projection_status is ProjectionStatus.PENDING

        with pytest.raises(AccountingError) as error:
            await service.record(_charge("event-pending-cap-b"))

        assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        assert "event-pending-cap-b" not in source.accept_calls
        assert service.health_snapshot().pending_source_events == 1
        sink.errors.clear()
        await _wait_until(lambda: source.projected_calls == ["event-pending-cap-a"])
        await service.stop()

    run_async(scenario())


def test_pending_duplicate_in_flight_uses_one_unsettled_capacity_slot() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(100))
        service = _service(
            source,
            sink,
            max_owner_tasks=3,
            max_retained_events=2,
        )
        await service.start()
        pending_event = _charge("event-pending-duplicate-cap")
        receipt = await service.record(pending_event)
        assert receipt.projection_status is ProjectionStatus.PENDING
        source.accept_entered.clear()
        source.accept_release = threading.Event()
        duplicate_task = asyncio.create_task(service.record(pending_event))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)

        distinct_task = asyncio.create_task(service.record(_charge("event-distinct-cap")))
        await asyncio.sleep(0)

        assert duplicate_task.done() is False
        assert distinct_task.done() is False
        assert service.health_snapshot().pending_source_events == 2
        source.accept_release.set()
        duplicate, distinct = await asyncio.gather(duplicate_task, distinct_task)
        assert duplicate.projection_status is ProjectionStatus.PENDING
        assert distinct.projection_status is ProjectionStatus.PENDING
        sink.errors.clear()
        await _wait_until(lambda: len(source.projected_calls) == 2)
        await service.stop()

    run_async(scenario())


def test_projector_pending_mark_reuses_capacity_of_same_retained_key() -> None:
    class BarrierSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.block_lists = False
            self.list_entered = threading.Event()
            self.list_release = threading.Event()
            self.accepted_a = threading.Event()
            self.accept_a_release = threading.Event()

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            acceptance = super().accept_accounting_event(event)
            if event.event_id == "event-overlap-a":
                self.accepted_a.set()
                self.accept_a_release.wait(timeout=2)
            return acceptance

        def list_pending_accounting_events(
            self,
            *,
            limit: int,
            after: tuple[datetime, str] | None = None,
        ) -> tuple[StoredAccountingEvent, ...]:
            if self.block_lists:
                self.list_entered.set()
                self.list_release.wait(timeout=2)
            return super().list_pending_accounting_events(limit=limit, after=after)

    async def scenario() -> None:
        source = BarrierSource()
        sink = FakeSink()
        sink.errors.extend(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) for _ in range(100))
        service = _service(
            source,
            sink,
            max_owner_tasks=2,
            max_retained_events=2,
        )
        await service.start()
        source.block_lists = True
        pending = await service.record(_charge("event-overlap-b"))
        assert pending.projection_status is ProjectionStatus.PENDING
        assert await asyncio.to_thread(source.list_entered.wait, 1)
        owner_task = asyncio.create_task(service.record(_charge("event-overlap-a")))
        assert await asyncio.to_thread(source.accepted_a.wait, 1)

        source.list_release.set()

        await _wait_until(lambda: "event-overlap-a" in sink.calls)
        assert service.health_snapshot().state.value == "running"
        assert service.health_snapshot().pending_source_events == 2
        source.accept_a_release.set()
        owner_receipt = await owner_task
        assert owner_receipt.projection_status is ProjectionStatus.PENDING
        sink.errors.clear()
        await _wait_until(lambda: len(source.projected_calls) == 2)
        await service.stop()

    run_async(scenario())


def test_stop_with_persistent_retained_event_fails_without_false_stopped() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        service = _service(source, sink)
        await service.start()
        with pytest.raises(AccountingError):
            await service.record(_charge("event-undrained"))

        with pytest.raises(AccountingError) as error:
            await service.stop()

        assert error.value.code is AccountingErrorCode.DRAIN_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False

    run_async(scenario())


def test_stop_succeeds_when_final_drain_clears_transient_retained_projection() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        sink = FakeSink()
        service = _service(
            source,
            sink,
            projector_backoff_seconds=1.0,
        )
        await service.start()
        with pytest.raises(AccountingError):
            await service.record(_charge("event-stop-final-drain"))
        await _wait_until(lambda: len(source.accept_calls) >= 2)
        source.accept_error = None
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))

        await service.stop()

        assert source.projected_calls == ["event-stop-final-drain"]
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "stopped"
        assert snapshot.pending_source_events == 0

    run_async(scenario())


def test_stop_cancellation_does_not_cancel_owned_record_or_cleanup() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_release = threading.Event()
        service = _service(source, FakeSink())
        await service.start()
        record_task = asyncio.create_task(service.record(_charge("event-stop-cancel")))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)
        stop_task = asyncio.create_task(service.stop())
        await _wait_until(lambda: service.health_snapshot().state.value == "stopping")
        with pytest.raises(AccountingError) as rejected:
            await service.record(_charge("event-after-stop-started"))
        assert rejected.value.code is AccountingErrorCode.ACCOUNTING_FAILED

        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task
        source.accept_release.set()

        await record_task
        await _wait_until(lambda: service.health_snapshot().state.value == "stopped")
        assert source.projected_calls == ["event-stop-cancel"]
        assert service.health_snapshot().active_sessions == 0

    run_async(scenario())


def test_stop_pending_read_failure_becomes_typed_drain_failure() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        source.list_error = RuntimeError("sensitive-stop-detail")

        with pytest.raises(AccountingError) as error:
            await service.stop()

        assert error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert "sensitive" not in repr(error.value)
        assert service.health_snapshot().state.value == "failed"

    run_async(scenario())


def test_repeated_and_concurrent_stop_share_one_workflow() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()

        await asyncio.gather(service.stop(), service.stop())
        await service.stop()

        assert source.list_calls == 2
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "stopped"
        assert snapshot.accepting is False

    run_async(scenario())


def test_stop_terminal_pending_read_failure_preserves_identity_and_fails_closed() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        terminal = BaseException("terminal-sensitive-stop-detail")
        await service.start()
        source.list_error = terminal

        with pytest.raises(BaseException) as error:
            await service.stop()

        assert error.value is terminal
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.last_error_code is AccountingErrorCode.ACCOUNTING_FAILED

    run_async(scenario())


def test_ambiguous_source_ack_commit_recovers_without_stale_pending(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
        source = TokensUsageDB(db_filename="accounting-service-ack.db")
        service = _service(source, FakeSink())  # type: ignore[arg-type]
        await service.start()
        real_commit = accounting_schema.commit_accounting_transaction

        def commit_then_raise(conn: sqlite3.Connection) -> None:
            real_commit(conn)
            raise sqlite3.OperationalError("ambiguous-service-ack-sensitive-detail")

        monkeypatch.setattr(
            accounting_schema,
            "commit_accounting_transaction",
            commit_then_raise,
        )
        event = replace(_charge("event-service-ambiguous-ack"), api_key_id=None)

        receipt = await service.record(event)

        assert receipt.projection_status is ProjectionStatus.NO_SINK
        assert service.health_snapshot().pending_source_events == 0
        stored = source.get_accounting_event(event.event_id)
        assert stored is not None
        assert stored.projected_at is not None
        await service.stop()

    run_async(scenario())


def test_record_lifecycle_rejection_has_no_source_side_effects() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        with pytest.raises(AccountingError):
            await service.record(_charge("event-before-start"))
        await service.start()
        await service.stop()
        with pytest.raises(AccountingError):
            await service.record(_charge("event-after-stop"))
        assert source.accept_calls == []

    run_async(scenario())


def test_start_waits_for_full_audit_before_opening_admission() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.audit_release = threading.Event()
        service = _service(source, FakeSink())

        start_task = asyncio.create_task(service.start())
        assert await asyncio.to_thread(source.audit_entered.wait, 1)
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "starting"
        assert snapshot.accepting is False
        assert snapshot.last_full_reconciliation is None

        source.audit_release.set()
        await start_task

        snapshot = service.health_snapshot()
        assert snapshot.state.value == "running"
        assert snapshot.accepting is True
        assert snapshot.last_full_reconciliation is not None
        assert snapshot.last_full_reconciliation.mode is ReconciliationMode.FULL
        assert snapshot.last_full_reconciliation.clean is True
        await service.stop()

    run_async(scenario())


def test_start_structural_audit_finding_is_sticky_and_never_opens_admission() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.audit_rows = (_usage_without_outbox_audit_row("audit-usage-only"),)
        service = _service(source, FakeSink())

        with pytest.raises(AccountingError) as error:
            await service.start()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.accepting is False
        assert snapshot.last_full_reconciliation is not None
        assert snapshot.last_full_reconciliation.usage_without_outbox == 1
        assert snapshot.last_full_reconciliation.clean is False
        with pytest.raises(AccountingError) as retry_error:
            await service.start()
        assert retry_error.value.code is AccountingErrorCode.ACCOUNTING_FAILED

    run_async(scenario())


def test_start_ordinary_audit_failure_is_retryable_without_partial_publication() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.audit_error = RuntimeError("sensitive-audit-detail")
        service = _service(source, FakeSink())

        with pytest.raises(AccountingError) as error:
            await service.start()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "blocked"
        assert snapshot.last_full_reconciliation is None
        source.audit_error = None

        await service.start()

        assert service.health_snapshot().state.value == "running"
        await service.stop()

    run_async(scenario())


def test_runtime_reconcile_callers_share_shielded_drain_without_full_audit() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        initial_source_audits = source.audit_calls
        initial_sink_audits = sink.audit_calls
        source.list_entered.clear()
        source.list_release = threading.Event()

        first = asyncio.create_task(service.reconcile())
        assert await asyncio.to_thread(source.list_entered.wait, 1)
        second = asyncio.create_task(service.reconcile())
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert not second.done()

        source.list_release.set()
        report = await second

        assert report.mode is ReconciliationMode.RUNTIME
        assert report.clean is True
        assert source.audit_calls == initial_source_audits
        assert sink.audit_calls == initial_sink_audits
        await service.stop()

    run_async(scenario())


def test_runtime_reconcile_ordinary_failure_blocks_then_recovers() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        source.list_error = RuntimeError("sensitive-runtime-reconcile-detail")

        with pytest.raises(AccountingError) as error:
            await service.reconcile()

        assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "blocked"
        assert snapshot.accepting is False
        source.list_error = None

        report = await service.reconcile()

        assert report.clean is True
        assert service.health_snapshot().state.value == "running"
        assert service.health_snapshot().accepting is True
        await service.stop()

    run_async(scenario())


def test_runtime_reconcile_terminal_failure_preserves_identity() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        terminal = BaseException("terminal-sensitive-runtime-reconcile")
        source.list_error = terminal

        with pytest.raises(BaseException) as error:
            await service.reconcile()

        assert error.value is terminal
        assert service.health_snapshot().state.value == "failed"
        source.list_error = None
        with pytest.raises(BaseException) as stop_error:
            await service.stop()
        assert stop_error.value is terminal
        assert service.health_snapshot().pending_source_events == 0

    run_async(scenario())


def test_runtime_reconcile_serializes_inline_projection_without_self_deadlock() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        source.list_entered.clear()
        source.list_release = threading.Event()
        sink.entered.clear()

        reconcile_task = asyncio.create_task(service.reconcile())
        assert await asyncio.to_thread(source.list_entered.wait, 1)
        record_task = asyncio.create_task(service.record(_charge("event-drain-owner")))
        await _wait_until(lambda: "event-drain-owner" in source.accept_calls)
        await asyncio.sleep(0.05)
        assert sink.entered.is_set() is False

        source.list_release.set()
        report, receipt = await asyncio.gather(reconcile_task, record_task)

        assert report.clean is True
        assert receipt.event_id == "event-drain-owner"
        assert service.health_snapshot().pending_source_events == 0
        await service.stop()

    run_async(scenario())


def test_stop_waits_for_active_runtime_reconcile_before_final_full_audit() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        initial_audit_calls = source.audit_calls
        source.list_entered.clear()
        source.list_release = threading.Event()
        reconcile_task = asyncio.create_task(service.reconcile())
        assert await asyncio.to_thread(source.list_entered.wait, 1)

        stop_task = asyncio.create_task(service.stop())
        await _wait_until(lambda: service.health_snapshot().state.value == "stopping")
        with pytest.raises(AccountingError) as rejected:
            await service.reconcile()
        assert rejected.value.code is AccountingErrorCode.ACCOUNTING_FAILED
        await asyncio.sleep(0.05)
        assert stop_task.done() is False
        assert source.audit_calls == initial_audit_calls

        source.list_release.set()
        await reconcile_task
        await stop_task

        snapshot = service.health_snapshot()
        assert snapshot.state.value == "stopped"
        assert snapshot.accepting is False
        assert source.audit_calls == initial_audit_calls + 1

    run_async(scenario())


def test_shutdown_full_audit_replaces_observation_counters_instead_of_summing() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.audit_rows = (_unrolled_rollup_audit_row("audit-unrolled"),)
        service = _service(source, FakeSink())
        await service.start()

        started = service.health_snapshot()
        assert started.last_full_reconciliation is not None
        assert started.last_full_reconciliation.unrolled_children == 1
        assert started.unrolled_children == 1
        assert started.last_full_reconciliation.clean is True
        source.audit_rows = ()

        await service.stop()

        stopped = service.health_snapshot()
        assert stopped.last_full_reconciliation is not None
        assert stopped.last_full_reconciliation.unrolled_children == 0
        assert stopped.unrolled_children == 0

    run_async(scenario())


def test_shutdown_structural_audit_finding_fails_with_completed_report() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        source.audit_rows = (_usage_without_outbox_audit_row("audit-stop-usage-only"),)

        with pytest.raises(AccountingError) as error:
            await service.stop()

        assert error.value.code is AccountingErrorCode.DRAIN_FAILED
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.last_full_reconciliation is not None
        assert snapshot.last_full_reconciliation.usage_without_outbox == 1
        assert snapshot.last_full_reconciliation.clean is False

    run_async(scenario())


def test_shutdown_terminal_full_audit_preserves_identity_and_prior_report() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        prior_report = service.health_snapshot().last_full_reconciliation
        terminal = BaseException("terminal-sensitive-audit-detail")
        source.audit_error = terminal

        with pytest.raises(BaseException) as error:
            await service.stop()

        assert error.value is terminal
        snapshot = service.health_snapshot()
        assert snapshot.state.value == "failed"
        assert snapshot.last_full_reconciliation is prior_report

    run_async(scenario())


def _key_record(
    key_id: int = 7,
    *,
    budget_usd: float | None = 10.0,
    spent_usd: float = 1.0,
) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=key_id,
        name=f"key-{key_id}",
        api_key="secret-not-for-accounting-state",
        budget_usd=budget_usd,
        spent_usd=spent_usd,
        rpm=10,
        tpm=100,
    )


async def _reserve(
    service: AccountingService,
    request_id: str,
    *,
    key_id: int | None = 7,
    budget_usd: float | None = 10.0,
    spent_usd: float = 1.0,
    estimate_usd: float = 1.0,
) -> AccountingReservation:
    return await service.reserve(
        request_id=request_id,
        api_key_id=key_id,
        budget_usd=budget_usd,
        spent_usd=spent_usd,
        rpm_limit=10,
        tpm_limit=100,
        estimate_usd=estimate_usd,
    )


def test_reservation_cap_and_budget_admission_are_atomic() -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink(), max_active_sessions=1)
        await service.start()

        reservation = await _reserve(service, "request-cap-1")
        assert service.health_snapshot().active_sessions == 1
        assert service.health_snapshot().active_session_limit == 1
        with pytest.raises(AccountingError) as cap_error:
            await _reserve(service, "request-cap-2")
        assert cap_error.value.code is AccountingErrorCode.ACTIVE_SESSION_LIMIT

        assert await service.release(reservation) is True
        with pytest.raises(AccountingError) as budget_error:
            await _reserve(
                service,
                "request-budget",
                key_id=8,
                budget_usd=1.5,
                spent_usd=1.0,
                estimate_usd=1.0,
            )
        assert budget_error.value.code is AccountingErrorCode.BUDGET_EXHAUSTED
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_commit_accepts_once_and_terminal_duplicate_is_noop() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-accepted")
        event = replace(
            _charge("event-request-accepted"),
            request_id=reservation.request_id,
        )

        receipt = await service.commit(reservation, event)

        assert receipt is not None
        assert receipt.source_status is SourceStatus.ACCEPTED
        assert await service.commit(reservation, event) is None
        assert await service.release(reservation) is False
        assert service.health_snapshot().active_sessions == 0
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (1, 3)
        await service.stop()

    run_async(scenario())


def test_fresh_duplicate_releases_reservation_without_spend_or_tpm() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-fresh-duplicate")
        event = replace(
            _charge("event-fresh-duplicate"),
            request_id=reservation.request_id,
        )
        source.events[event.event_id] = StoredAccountingEvent(
            event=event,
            usage_row_id=1,
            created_at=NOW,
            projected_at=NOW,
        )

        receipt = await service.commit(reservation, event)

        assert receipt is not None
        assert receipt.source_status is SourceStatus.DUPLICATE
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (1, 0)
        await service.stop()

    run_async(scenario())


def test_source_failure_retains_session_until_supervised_retry_accepts() -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        service = _service(source, FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-retry")
        event = replace(_charge("event-retry"), request_id=reservation.request_id)

        commit_task = asyncio.create_task(service.commit(reservation, event))
        await _wait_until(lambda: service.health_snapshot().state is AccountingHealthState.BLOCKED)
        assert service.health_snapshot().active_sessions == 1
        assert service._budget_ledger.reserved_for(7) == 1.0
        with pytest.raises(AccountingError) as blocked:
            await _reserve(service, "request-blocked-during-retry")
        assert blocked.value.code is AccountingErrorCode.ADMISSION_CLOSED

        source.accept_error = None
        receipt = await asyncio.wait_for(commit_task, timeout=1)

        assert receipt is not None
        assert receipt.source_status is SourceStatus.ACCEPTED
        assert service.health_snapshot().active_sessions == 0
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (1, 3)
        await service.stop()

    run_async(scenario())


def test_transient_admission_block_logs_the_code_that_closed_it(caplog) -> None:
    async def scenario() -> None:
        source = FakeSource()
        source.accept_error = AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
        service = _service(source, FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-blocked-log")
        event = replace(_charge("event-blocked-log"), request_id=reservation.request_id)

        with caplog.at_level(logging.WARNING, logger="llm_gateway_core.services.accounting_service"):
            commit_task = asyncio.create_task(service.commit(reservation, event))
            await _wait_until(
                lambda: service.health_snapshot().state is AccountingHealthState.BLOCKED
            )
            source.accept_error = None
            await asyncio.wait_for(commit_task, timeout=1)
            await _wait_until(
                lambda: service.health_snapshot().state is AccountingHealthState.RUNNING
            )
        await service.stop()

        assert (
            "Accounting admission closed: running -> blocked (error source_write_failed)"
            in caplog.text
        )
        assert "Accounting admission reopened: blocked -> running" in caplog.text

    run_async(scenario())


def test_stop_closes_admission_and_waits_for_caller_to_release_session() -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-shutdown")

        stop_task = asyncio.create_task(service.stop())
        await _wait_until(lambda: service.health_snapshot().state is AccountingHealthState.STOPPING)
        await asyncio.sleep(0)
        assert stop_task.done() is False
        with pytest.raises(AccountingError) as blocked:
            await _reserve(service, "request-after-stop")
        assert blocked.value.code is AccountingErrorCode.ADMISSION_CLOSED

        assert await service.release(reservation) is True
        await asyncio.wait_for(stop_task, timeout=1)
        assert service.health_snapshot().active_sessions == 0

    run_async(scenario())


def test_manual_reset_rejects_active_key_without_repository_mutation() -> None:
    async def scenario() -> None:
        sink = FakeSink()
        sink.records[7] = _key_record()
        service = _service(FakeSource(), sink)
        await service.start()
        reservation = await _reserve(service, "request-reset-active")

        with pytest.raises(AccountingError) as error:
            await service.update_key(7, changes={"budget_usd": 8.0}, reset_spent=True)

        assert error.value.code is AccountingErrorCode.ACCOUNTING_IN_FLIGHT
        assert sink.update_calls == []
        assert await service.release(reservation) is True
        await service.stop()

    run_async(scenario())


def test_due_reset_fences_busy_key_and_last_terminal_runs_reset() -> None:
    async def scenario() -> None:
        sink = FakeSink()
        sink.records[7] = _key_record()
        sink.due_key_ids = (7,)
        service = _service(FakeSource(), sink)
        await service.start()
        reservation = await _reserve(service, "request-due-reset")

        await service.reset_due_budgets(now=NOW)

        assert service.health_snapshot().reset_pending_keys == 1
        assert sink.due_reset_calls == []
        with pytest.raises(AccountingError) as error:
            await _reserve(service, "request-due-fenced")
        assert error.value.code is AccountingErrorCode.BUDGET_RESET_IN_PROGRESS

        assert await service.release(reservation) is True
        assert sink.due_reset_calls == [(NOW, (7,))]
        assert service.health_snapshot().reset_pending_keys == 0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (1, 0)
        await service.stop()

    run_async(scenario())


def test_uncertain_source_owner_recovers_once_and_follower_is_duplicate() -> None:
    class CommitThenRaiseSource(FakeSource):
        def __init__(self) -> None:
            super().__init__()
            self.raised = False

        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            acceptance = super().accept_accounting_event(event)
            if not self.raised:
                self.raised = True
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            return acceptance

    async def scenario() -> None:
        source = CommitThenRaiseSource()
        service = _service(source, FakeSink())
        await service.start()
        first = await _reserve(service, "request-owner")
        second = await _reserve(service, "request-follower")
        first_event = replace(_charge("event-shared"), request_id=first.request_id)
        second_event = replace(first_event, request_id=second.request_id)

        owner_task = asyncio.create_task(service.commit(first, first_event))
        follower_task = asyncio.create_task(service.commit(second, second_event))
        owner_receipt, follower_receipt = await asyncio.gather(owner_task, follower_task)

        assert owner_receipt is not None
        assert follower_receipt is not None
        assert owner_receipt.source_status is SourceStatus.RECOVERED
        assert follower_receipt.source_status is SourceStatus.DUPLICATE
        assert source.accept_calls == ["event-shared", "event-shared"]
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (2, 3)
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_follower_terminal_phase_retries_only_missing_limiter_step() -> None:
    class CountingLedger(UsdBudgetLedger):
        def __init__(self) -> None:
            super().__init__()
            self.release_calls = 0

        def release_request(self, request_id: str, key_id: int) -> bool:
            self.release_calls += 1
            return super().release_request(request_id, key_id)

    class FailSecondAttributeLimiter(RateLimiter):
        def __init__(self) -> None:
            super().__init__()
            self.attribute_calls = 0

        def attribute_request_tokens(self, request_id: str, key_id: int, tokens: int) -> bool:
            self.attribute_calls += 1
            if self.attribute_calls == 2:
                raise RuntimeError("sensitive-follower-terminal-failure")
            return super().attribute_request_tokens(request_id, key_id, tokens)

    async def scenario() -> None:
        source = FakeSource()
        source.accept_release = threading.Event()
        ledger = CountingLedger()
        limiter = FailSecondAttributeLimiter()
        service = _service(
            source,
            FakeSink(),
            budget_ledger=ledger,
            rate_limiter=limiter,
        )
        await service.start()
        owner = await _reserve(service, "request-follower-owner")
        follower = await _reserve(service, "request-follower-retry")
        owner_event = replace(_charge("event-follower-retry"), request_id=owner.request_id)
        follower_event = replace(owner_event, request_id=follower.request_id)

        owner_task = asyncio.create_task(service.commit(owner, owner_event))
        assert await asyncio.to_thread(source.accept_entered.wait, 1)
        follower_task = asyncio.create_task(service.commit(follower, follower_event))
        await asyncio.sleep(0)
        source.accept_release.set()
        owner_receipt, follower_receipt = await asyncio.gather(owner_task, follower_task)

        assert owner_receipt is not None
        assert follower_receipt is not None
        assert owner_receipt.source_status is SourceStatus.ACCEPTED
        assert follower_receipt.source_status is SourceStatus.DUPLICATE
        assert ledger.release_calls == 1
        assert limiter.attribute_calls == 3
        assert limiter.get_window_snapshot(7)[:2] == (2, 3)
        assert source.accept_calls == ["event-follower-retry"]
        with pytest.raises(AccountingError) as reused:
            await _reserve(service, follower.request_id)
        assert reused.value.code is AccountingErrorCode.RESERVATION_CONFLICT
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_pending_projection_blocks_reset_before_repository_mutation() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        sink.records[7] = _key_record()
        service = _service(source, sink)
        await service.start()
        event = _charge("event-pending-before-reset")
        source.events[event.event_id] = StoredAccountingEvent(
            event=event,
            usage_row_id=1,
            created_at=NOW,
        )
        sink.errors.append(AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED))

        with pytest.raises(AccountingError) as error:
            await service.update_key(7, changes={}, reset_spent=True)

        assert error.value.code is AccountingErrorCode.DRAIN_FAILED
        assert sink.update_calls == []
        await service.reconcile()
        await service.stop()

    run_async(scenario())


def test_cancelled_release_keeps_due_reset_owned_until_cleanup_finishes() -> None:
    async def scenario() -> None:
        sink = FakeSink()
        sink.records[7] = _key_record()
        sink.due_key_ids = (7,)
        service = _service(FakeSource(), sink)
        await service.start()
        reservation = await _reserve(service, "request-release-cancel")
        await service.reset_due_budgets(now=NOW)
        sink.due_reset_release = threading.Event()

        caller = asyncio.create_task(service.release(reservation))
        assert await asyncio.to_thread(sink.due_reset_entered.wait, 1)
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller
        assert service.health_snapshot().reset_pending_keys == 1

        sink.due_reset_release.set()
        await _wait_until(lambda: service.health_snapshot().active_sessions == 0)
        await _wait_until(lambda: service.health_snapshot().reset_pending_keys == 0)
        assert await service.release(reservation) is False
        await service.stop()

    run_async(scenario())


def test_stale_reserve_context_cannot_overwrite_service_owned_budget_update() -> None:
    async def scenario() -> None:
        sink = FakeSink()
        sink.records[7] = _key_record(budget_usd=2.0, spent_usd=0.0)
        sink.records[8] = _key_record(8, budget_usd=2.0, spent_usd=0.0)
        service = _service(FakeSource(), sink)
        await service.start()
        initial = await _reserve(
            service,
            "request-initial-sync",
            budget_usd=2.0,
            spent_usd=0.0,
            estimate_usd=0.1,
        )
        await service.release(initial)
        await service.update_key(
            7,
            changes={"budget_usd": 0.5},
            reset_spent=False,
        )

        with pytest.raises(AccountingError) as error:
            await _reserve(
                service,
                "request-stale-context",
                budget_usd=10.0,
                spent_usd=0.0,
                estimate_usd=1.0,
            )

        assert error.value.code is AccountingErrorCode.BUDGET_EXHAUSTED
        limit_initial = await _reserve(
            service,
            "request-limit-initial",
            key_id=8,
            budget_usd=2.0,
            spent_usd=0.0,
            estimate_usd=0.1,
        )
        await service.release(limit_initial)
        await service.update_key(
            8,
            changes={"rpm": 1},
            reset_spent=False,
        )
        limit_first = await _reserve(
            service,
            "request-limit-stale-first",
            key_id=8,
            budget_usd=2.0,
            spent_usd=0.0,
            estimate_usd=0.1,
        )
        await service.release(limit_first)
        with pytest.raises(AccountingError) as rate_error:
            await _reserve(
                service,
                "request-limit-stale-second",
                key_id=8,
                budget_usd=2.0,
                spent_usd=0.0,
                estimate_usd=0.1,
            )
        assert rate_error.value.code is AccountingErrorCode.RATE_LIMITED
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


@pytest.mark.parametrize(
    "failure",
    (
        RuntimeError("sensitive-admission-failure"),
        BaseException("terminal-admission-failure"),
    ),
    ids=("ordinary", "terminal"),
)
def test_admission_exception_releases_ledger_without_creating_session(
    failure: BaseException,
) -> None:
    class FailingAdmissionLimiter(RateLimiter):
        def admit_request(
            self,
            request_id: str,
            key_id: int,
            *,
            rpm_limit: int | None,
            tpm_limit: int | None,
        ) -> str | None:
            del request_id, key_id, rpm_limit, tpm_limit
            raise failure

    async def scenario() -> None:
        ledger = UsdBudgetLedger()
        service = _service(
            FakeSource(),
            FakeSink(),
            budget_ledger=ledger,
            rate_limiter=FailingAdmissionLimiter(),
        )
        await service.start()

        if isinstance(failure, Exception):
            with pytest.raises(AccountingError) as error:
                await _reserve(service, "request-admission-error")
            assert error.value.code is AccountingErrorCode.ACCOUNTING_FAILED
            assert "sensitive" not in repr(error.value)
        else:
            with pytest.raises(BaseException) as error:
                await _reserve(service, "request-admission-error")
            assert error.value is failure

        assert ledger.reserved_for(7) == 0.0
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_terminal_liability_retry_does_not_double_commit_ledger_or_tpm() -> None:
    class CountingLedger(UsdBudgetLedger):
        def __init__(self) -> None:
            super().__init__()
            self.commit_calls = 0

        def commit_request(self, request_id: str, key_id: int, actual: float) -> bool:
            self.commit_calls += 1
            return super().commit_request(request_id, key_id, actual)

    class FailOnceTerminalLimiter(RateLimiter):
        def __init__(self) -> None:
            super().__init__()
            self.attribute_calls = 0

        def attribute_request_tokens(self, request_id: str, key_id: int, tokens: int) -> bool:
            self.attribute_calls += 1
            if self.attribute_calls == 1:
                raise RuntimeError("sensitive-terminal-attribution-failure")
            return super().attribute_request_tokens(request_id, key_id, tokens)

    async def scenario() -> None:
        source = FakeSource()
        ledger = CountingLedger()
        limiter = FailOnceTerminalLimiter()
        service = _service(
            source,
            FakeSink(),
            budget_ledger=ledger,
            rate_limiter=limiter,
        )
        await service.start()
        reservation = await _reserve(service, "request-terminal-retry")
        event = replace(
            _charge("event-terminal-retry"),
            request_id=reservation.request_id,
        )

        receipt = await asyncio.wait_for(
            service.commit(reservation, event),
            timeout=1,
        )

        assert receipt is not None
        assert receipt.source_status is SourceStatus.RECOVERED
        assert ledger.commit_calls == 1
        assert limiter.attribute_calls == 2
        assert limiter.get_window_snapshot(7)[:2] == (1, 3)
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_terminal_retained_retry_unblocks_commit_and_stop() -> None:
    terminal = BaseException("terminal-retained-retry")

    class RetryTerminalSource(FakeSource):
        def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
            self.accept_calls.append(event.event_id)
            if len(self.accept_calls) == 1:
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED)
            raise terminal

    async def scenario() -> None:
        service = _service(RetryTerminalSource(), FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-terminal-source-retry")
        event = replace(
            _charge("event-terminal-source-retry"),
            request_id=reservation.request_id,
        )

        with pytest.raises(BaseException) as commit_error:
            await asyncio.wait_for(
                service.commit(reservation, event),
                timeout=1,
            )
        assert commit_error.value is terminal
        assert service.health_snapshot().active_sessions == 0

        with pytest.raises(BaseException) as stop_error:
            await asyncio.wait_for(service.stop(), timeout=1)
        assert stop_error.value is terminal

    run_async(scenario())


def test_terminal_request_id_cannot_be_reserved_again_in_live_window() -> None:
    async def scenario() -> None:
        source = FakeSource()
        service = _service(source, FakeSink())
        await service.start()
        reservation = await _reserve(service, "request-terminal-id")
        event = replace(
            _charge("event-terminal-id"),
            request_id=reservation.request_id,
        )
        receipt = await service.commit(reservation, event)
        assert receipt is not None

        with pytest.raises(AccountingError) as error:
            await _reserve(service, "request-terminal-id")

        assert error.value.code is AccountingErrorCode.RESERVATION_CONFLICT
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (1, 3)
        assert source.accept_calls == ["event-terminal-id"]
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_delete_tombstones_active_key_and_discards_state_only_at_zero() -> None:
    async def scenario() -> None:
        sink = FakeSink()
        sink.records[7] = _key_record()
        service = _service(FakeSource(), sink)
        await service.start()
        reservation = await _reserve(service, "request-delete-active")

        assert await service.delete_key(7) is True
        assert sink.delete_calls == [(7, NOW)]
        assert service._budget_ledger.reserved_for(7) == 1.0
        event = replace(_charge("event-delete-late"), request_id=reservation.request_id)
        receipt = await service.commit(reservation, event)

        assert receipt is not None
        assert sink.calls == [event.event_id]
        assert service._budget_ledger.reserved_for(7) == 0.0
        with pytest.raises(AccountingError) as error:
            await _reserve(service, "request-delete-fenced")
        assert error.value.code is AccountingErrorCode.ADMISSION_CLOSED
        await service.stop()

    run_async(scenario())


def test_deep_research_child_charge_and_zero_cost_parent_rollup() -> None:
    async def scenario() -> None:
        source = FakeSource()
        sink = FakeSink()
        service = _service(source, sink)
        await service.start()
        parent_reservation = await _reserve(
            service,
            "deep-parent-request",
            spent_usd=0.0,
        )
        parent, token = service.begin_deep_research_parent(
            parent_reservation,
            gateway_model="gateway/deep-research",
            auth_identity=DeepResearchAuthIdentity(
                api_key_id=7,
                allowed_models=("gateway/model",),
            ),
        )
        child = await service.reserve_deep_research_child(
            token,
            request_id="deep-child-request",
            estimate_usd=1.0,
        )
        child_event = replace(
            _charge("deep-child-event"),
            request_id=child.reservation.request_id,
            parent_event_id=child.parent_event_id,
        )

        child_receipt = await service.commit(child.reservation, child_event)
        assert child_receipt is not None
        sealed = await service.seal_deep_research_children(parent)
        rollup = build_deep_research_rollup_event(
            parent,
            sealed,
            occurred_at=NOW,
        )
        parent_receipt = await service.commit(parent_reservation, rollup)

        assert parent_receipt is not None
        assert sealed.receipts == (child_receipt,)
        assert sealed.diagnostic_child_cost_usd == child_event.usage.cost
        assert source.accept_calls == [child_event.event_id, rollup.event_id]
        assert sink.calls == [child_event.event_id]
        assert service._budget_ledger.reserved_for(7) == 0.0
        assert service._rate_limiter.get_window_snapshot(7)[:2] == (2, 3)
        assert service.health_snapshot().active_sessions == 0
        await service.stop()

    run_async(scenario())


def test_deep_research_children_share_parent_budget_admission() -> None:
    async def scenario() -> None:
        service = _service(FakeSource(), FakeSink())
        await service.start()
        parent_reservation = await _reserve(
            service,
            "deep-budget-parent",
            budget_usd=2.0,
            spent_usd=0.0,
            estimate_usd=1.0,
        )
        parent, token = service.begin_deep_research_parent(
            parent_reservation,
            gateway_model="gateway/deep-research",
            auth_identity=DeepResearchAuthIdentity(api_key_id=7),
        )
        first = await service.reserve_deep_research_child(
            token,
            request_id="deep-budget-child-a",
            estimate_usd=1.0,
        )

        with pytest.raises(AccountingError) as error:
            await service.reserve_deep_research_child(
                token,
                request_id="deep-budget-child-b",
                estimate_usd=1.0,
            )

        assert error.value.code is AccountingErrorCode.BUDGET_EXHAUSTED
        first_event = replace(
            _charge("deep-budget-event-a"),
            request_id=first.reservation.request_id,
            parent_event_id=first.parent_event_id,
        )
        assert await service.commit(first.reservation, first_event) is not None
        sealed = await service.seal_deep_research_children(parent)
        rollup = build_deep_research_rollup_event(parent, sealed, occurred_at=NOW)
        assert await service.commit(parent_reservation, rollup) is not None
        assert service._budget_ledger.reserved_for(7) == 0.0
        await service.stop()

    run_async(scenario())


def test_deep_research_child_budget_outlives_the_concurrency_limit() -> None:
    async def scenario() -> None:
        service = _service(
            FakeSource(),
            FakeSink(),
            max_active_sessions=2,
            max_deep_research_children=4,
        )
        await service.start()
        parent_reservation = await _reserve(
            service,
            "deep-cap-parent",
            spent_usd=0.0,
        )
        parent, token = service.begin_deep_research_parent(
            parent_reservation,
            gateway_model="gateway/deep-research",
            auth_identity=DeepResearchAuthIdentity(api_key_id=7),
        )

        for ordinal in range(4):
            child = await service.reserve_deep_research_child(
                token,
                request_id=f"deep-cap-child-{ordinal}",
                estimate_usd=0.1,
            )
            assert await service.release(child.reservation) is True

        with pytest.raises(AccountingError) as error:
            await service.reserve_deep_research_child(
                token,
                request_id="deep-cap-child-4",
                estimate_usd=0.1,
            )

        assert error.value.code is AccountingErrorCode.ACTIVE_SESSION_LIMIT
        assert service.health_snapshot().active_sessions == 1
        await service.cancel_deep_research_children(parent)
        assert await service.release(parent_reservation) is True
        await service.stop()

    run_async(scenario())
