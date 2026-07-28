"""Cancellation-safe orchestration for durable accounting projection."""

from __future__ import annotations

import asyncio
import logging
import secrets
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Protocol

from .accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingHealthSnapshot,
    AccountingHealthState,
    AccountingOwnerAuditRow,
    AccountingProjectionMark,
    AccountingReceipt,
    AccountingReservation,
    AccountingSinkAuditRow,
    AccountingSourceAuditRow,
    AccountingValidationError,
    ProjectionStatus,
    ReconciliationMode,
    ReconciliationReport,
    SourceAcceptance,
    SourceStatus,
    StoredAccountingEvent,
)
from .accounting_reconciliation import reconcile_accounting_repositories
from .access_control import UsdBudgetLedger
from .deep_research_accounting import (
    DeepResearchAccountingRegistry,
    DeepResearchAuthIdentity,
    DeepResearchChildAdmission,
    DeepResearchChildSeal,
    DeepResearchParentHandle,
    DeepResearchResolvedContext,
)
from .rate_limiter import REQUEST_ALREADY_TERMINAL, RateLimiter

if TYPE_CHECKING:
    from ..db.api_keys_db import ApiKeyRecord

logger = logging.getLogger(__name__)


class AccountingSourceRepository(Protocol):
    def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance: ...

    def list_pending_accounting_events(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[StoredAccountingEvent, ...]: ...

    def mark_accounting_event_projected(
        self,
        event_id: str,
        fingerprint: str,
        projected_at: datetime,
    ) -> AccountingProjectionMark: ...

    def mark_accounting_projection_failed(
        self,
        event_id: str,
        fingerprint: str,
        reason_code: AccountingErrorCode,
        attempted_at: datetime,
    ) -> AccountingProjectionMark: ...

    def list_accounting_source_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSourceAuditRow, ...]: ...


class AccountingSinkRepository(Protocol):
    def apply_spend_event(
        self,
        stored_event: StoredAccountingEvent,
        applied_at: datetime,
    ) -> ProjectionStatus: ...

    def list_accounting_sink_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSinkAuditRow, ...]: ...

    def get_accounting_owner_states(
        self,
        *,
        api_key_ids: tuple[int, ...],
    ) -> tuple[AccountingOwnerAuditRow, ...]: ...

    def update_accounting_key(
        self,
        key_id: int,
        *,
        changes: Mapping[str, object],
        reset_spent: bool = False,
    ) -> ApiKeyRecord | None: ...

    def reset_accounting_spend(self, key_id: int) -> ApiKeyRecord | None: ...

    def list_due_budget_key_ids(
        self,
        *,
        now: datetime,
        limit: int,
        after_key_id: int | None = None,
    ) -> tuple[int, ...]: ...

    def reset_due_accounting_budgets(
        self,
        *,
        now: datetime,
        eligible_key_ids: tuple[int, ...],
    ) -> tuple[ApiKeyRecord, ...]: ...

    def delete_to_accounting_tombstone(
        self,
        key_id: int,
        *,
        deleted_at: datetime,
    ) -> bool: ...


_RetainedKey = tuple[str, str]


@dataclass(slots=True)
class _AccountingSession:
    reservation: AccountingReservation
    state: str = "reserved"
    event_key: _RetainedKey | None = None
    owner_task: asyncio.Task[AccountingReceipt | None] | None = None
    retry_result: asyncio.Future[AccountingReceipt] | None = None
    charge_liability: bool | None = None
    ledger_terminalized: bool = False
    limiter_terminalized: bool = False

    @property
    def liability_applied(self) -> bool:
        return self.ledger_terminalized and self.limiter_terminalized
_PERMANENT_CODES = frozenset(
    {
        AccountingErrorCode.INVALID_CONTRACT,
        AccountingErrorCode.FINGERPRINT_CONFLICT,
        AccountingErrorCode.ORPHAN_SOURCE,
        AccountingErrorCode.ORPHAN_SINK,
        AccountingErrorCode.SCHEMA_MISMATCH,
    }
)
_TOLERATED_SINK_CODES = frozenset(
    {
        AccountingErrorCode.ORPHAN_SINK,
        AccountingErrorCode.FINGERPRINT_CONFLICT,
    }
)


class _ToleratedProjectionFailure(Exception):
    """Internal-only signal: a permanent sink failure was tolerated.

    Raised only when the projection sink's ``apply_spend_event`` call itself
    fails with ORPHAN_SINK/FINGERPRINT_CONFLICT -- a data-integrity problem
    scoped to one key's ledger row. It never crosses a public API boundary:
    every call site that can raise it also catches it, so it is distinct
    from an ``AccountingError`` with the same code raised elsewhere (e.g. a
    source-side marker-write conflict), which must still fail closed.
    """

    def __init__(self, code: AccountingErrorCode) -> None:
        super().__init__(code)
        self.code = code


def _positive_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise AccountingValidationError
    return value


def _positive_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccountingValidationError
    normalized = float(value)
    if normalized <= 0 or normalized == float("inf") or normalized != normalized:
        raise AccountingValidationError
    return normalized


def _non_negative_float(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccountingValidationError
    normalized = float(value)
    if normalized < 0 or normalized == float("inf") or normalized != normalized:
        raise AccountingValidationError
    return normalized


def _optional_non_negative_float(value: object) -> float | None:
    if value is None:
        return None
    return _non_negative_float(value)


def _optional_positive_int(value: object) -> int | None:
    if value is None:
        return None
    return _positive_int(value)


def _api_key_id(value: object) -> int:
    return _positive_int(value)


class AccountingService:
    """Own source acceptance, idempotent projection and bounded replay tasks."""

    def __init__(
        self,
        source: AccountingSourceRepository,
        sink: AccountingSinkRepository,
        *,
        max_retained_events: int = 128,
        max_owner_tasks: int = 128,
        max_active_sessions: int = 128,
        max_deep_research_children: int = 1024,
        projector_batch_size: int = 64,
        projector_backoff_seconds: float = 0.25,
        clock: Callable[[], datetime] | None = None,
        budget_ledger: UsdBudgetLedger | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self._source = source
        self._sink = sink
        self._max_retained_events = _positive_int(max_retained_events)
        self._max_owner_tasks = _positive_int(max_owner_tasks)
        self._max_active_sessions = _positive_int(max_active_sessions)
        self._max_deep_research_children = _positive_int(max_deep_research_children)
        self._projector_batch_size = _positive_int(projector_batch_size)
        self._projector_backoff_seconds = _positive_float(projector_backoff_seconds)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._budget_ledger = budget_ledger or UsdBudgetLedger()
        self._rate_limiter = rate_limiter or RateLimiter()
        # max_children_per_context bounds every child session a research run
        # ever opens, not the concurrent ones: the collector keeps terminal
        # slots until seal() rolls their receipts up. Sharing the concurrency
        # limit with it capped one run at 128 operations.
        self._deep_research_accounting = DeepResearchAccountingRegistry(
            max_contexts=self._max_active_sessions,
            max_children_per_context=self._max_deep_research_children,
            clock=self._now,
        )

        self._state_guard = threading.Lock()
        self._state = AccountingHealthState.NEW
        self._initialized = False
        self._accepting = False
        self._retained: dict[_RetainedKey, AccountingEvent] = {}
        self._source_inflight: dict[_RetainedKey, int] = {}
        self._owner_tasks: set[asyncio.Task[AccountingReceipt]] = set()
        self._terminal_owner_error: BaseException | None = None
        self._pending_events: set[_RetainedKey] = set()
        self._projection_attempts = 0
        self._fingerprint_conflicts = 0
        self._source_orphans = 0
        self._sink_orphans = 0
        self._last_error_code: AccountingErrorCode | None = None
        self._last_error_at: datetime | None = None
        self._fatal_error_code: AccountingErrorCode | None = None
        self._last_full_reconciliation: ReconciliationReport | None = None
        self._sessions: dict[str, _AccountingSession] = {}
        self._session_by_request: dict[str, str] = {}
        self._session_tasks: set[asyncio.Task[AccountingReceipt | None]] = set()
        self._key_mutation_tasks: set[asyncio.Task[object]] = set()
        self._retry_sessions: dict[_RetainedKey, str] = {}
        self._event_session_owner: dict[_RetainedKey, str] = {}
        self._active_by_key: dict[int, int] = {}
        self._key_fences: dict[int, AccountingErrorCode] = {}
        self._reset_pending_at: dict[int, datetime] = {}
        self._deleted_keys: set[int] = set()
        self._known_key_ids: set[int] = set()
        self._key_limits: dict[int, tuple[int | None, int | None]] = {}

        self._loop: asyncio.AbstractEventLoop | None = None
        self._lifecycle_lock: asyncio.Lock | None = None
        self._drain_lock: asyncio.Lock | None = None
        self._wake: asyncio.Event | None = None
        self._stop_requested: asyncio.Event | None = None
        self._sessions_drained: asyncio.Event | None = None
        self._projector_task: asyncio.Task[None] | None = None
        self._start_task: asyncio.Task[None] | None = None
        self._reconcile_task: asyncio.Task[ReconciliationReport] | None = None
        self._stop_task: asyncio.Task[None] | None = None

    def __repr__(self) -> str:
        return "<AccountingService>"

    def _now(self) -> datetime:
        value = self._clock()
        if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
            raise AccountingValidationError
        return value.astimezone(timezone.utc)

    def _bind_loop(self) -> None:
        loop = asyncio.get_running_loop()
        if self._loop is None:
            self._loop = loop
            self._lifecycle_lock = asyncio.Lock()
            self._drain_lock = asyncio.Lock()
            self._wake = asyncio.Event()
            self._stop_requested = asyncio.Event()
            self._sessions_drained = asyncio.Event()
            self._sessions_drained.set()
        elif self._loop is not loop:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)

    @property
    def _lifecycle(self) -> asyncio.Lock:
        if self._lifecycle_lock is None:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        return self._lifecycle_lock

    @property
    def _drain(self) -> asyncio.Lock:
        if self._drain_lock is None:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        return self._drain_lock

    @property
    def _projector_wake(self) -> asyncio.Event:
        if self._wake is None:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        return self._wake

    @property
    def _projector_stop(self) -> asyncio.Event:
        if self._stop_requested is None:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        return self._stop_requested

    @property
    def _session_drain(self) -> asyncio.Event:
        if self._sessions_drained is None:
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        return self._sessions_drained

    def _set_state(
        self,
        state: AccountingHealthState,
        *,
        accepting: bool,
        initialized: bool | None = None,
    ) -> None:
        with self._state_guard:
            self._state = state
            self._accepting = accepting
            if initialized is not None:
                self._initialized = initialized

    def _record_error(
        self,
        code: AccountingErrorCode,
        *,
        permanent: bool,
        block: bool = True,
    ) -> None:
        # Error-state publication must not depend on a diagnostic timestamp.
        try:
            now = self._now()
        except BaseException:
            now = None
        with self._state_guard:
            previous_state = self._state
            self._last_error_code = code
            self._last_error_at = now
            if code is AccountingErrorCode.FINGERPRINT_CONFLICT:
                self._fingerprint_conflicts += 1
            elif code is AccountingErrorCode.ORPHAN_SOURCE:
                self._source_orphans += 1
            elif code is AccountingErrorCode.ORPHAN_SINK:
                self._sink_orphans += 1
            if permanent:
                self._fatal_error_code = code
                self._state = AccountingHealthState.FAILED
                self._accepting = False
            elif block and self._state not in {
                AccountingHealthState.STOPPING,
                AccountingHealthState.STOPPED,
                AccountingHealthState.FAILED,
            }:
                self._state = AccountingHealthState.BLOCKED
                self._accepting = False
            new_state = self._state
            closed = new_state is not previous_state and not self._accepting
        # Logged outside the guard: closing admission rejects every in-flight
        # request with `admission_closed`, and the code that caused it is
        # otherwise visible only in the health snapshot until it is overwritten.
        if closed:
            logger.error(
                "Accounting admission closed: %s -> %s (error %s)",
                previous_state.value,
                new_state.value,
                code.value,
            )

    def _clear_transient_block(self) -> None:
        with self._state_guard:
            cleared = self._clear_transient_block_locked()
        if cleared:
            self._log_admission_reopened()

    def _clear_transient_block_locked(self) -> bool:
        if (
            self._state is AccountingHealthState.BLOCKED
            and self._fatal_error_code is None
            and self._unsettled_count_locked() == 0
        ):
            self._state = AccountingHealthState.RUNNING
            self._accepting = True
            return True
        return False

    @staticmethod
    def _log_admission_reopened() -> None:
        logger.warning("Accounting admission reopened: blocked -> running")

    def _has_fatal_error(self) -> bool:
        with self._state_guard:
            return self._fatal_error_code is not None

    @staticmethod
    def _pending_key(stored: StoredAccountingEvent) -> _RetainedKey:
        return (stored.event.event_id, stored.billing_fingerprint)

    @classmethod
    def _validate_source_result(
        cls,
        acceptance: object,
        expected_key: _RetainedKey,
    ) -> StoredAccountingEvent:
        if not isinstance(acceptance, SourceAcceptance):
            raise AccountingValidationError
        stored = acceptance.stored_event
        if cls._pending_key(stored) != expected_key:
            raise AccountingValidationError
        return stored

    @staticmethod
    def _validate_projection_mark(
        mark: object,
        expected_key: _RetainedKey,
        *,
        require_projected: bool,
    ) -> AccountingProjectionMark:
        if not isinstance(mark, AccountingProjectionMark):
            raise AccountingValidationError
        if (
            (mark.event_id, mark.billing_fingerprint) != expected_key
            or (require_projected and mark.projected_at is None)
        ):
            raise AccountingValidationError
        return mark

    def _validate_pending_page(
        self,
        rows: object,
        *,
        after: tuple[datetime, str] | None,
    ) -> tuple[StoredAccountingEvent, ...]:
        if type(rows) is not tuple or len(rows) > self._projector_batch_size:
            raise AccountingValidationError
        previous = after
        for stored in rows:
            if not isinstance(stored, StoredAccountingEvent) or stored.projected_at is not None:
                raise AccountingValidationError
            cursor = (stored.created_at, stored.event.event_id)
            if previous is not None and cursor <= previous:
                raise AccountingValidationError
            previous = cursor
        return rows

    def _unsettled_count_locked(self) -> int:
        return len(self._retained.keys() | self._source_inflight.keys() | self._pending_events)

    def _mark_pending(self, stored: StoredAccountingEvent) -> None:
        with self._state_guard:
            key = self._pending_key(stored)
            unsettled_count = self._unsettled_count_locked()
            if (
                key not in self._pending_events
                and key not in self._retained
                and key not in self._source_inflight
                and unsettled_count >= self._max_retained_events
            ):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._pending_events.add(key)

    def _clear_pending(self, stored: StoredAccountingEvent) -> None:
        with self._state_guard:
            self._pending_events.discard(self._pending_key(stored))
            cleared = self._clear_transient_block_locked()
        if cleared:
            self._log_admission_reopened()

    def _accept_source_result(
        self,
        retained_key: _RetainedKey,
        stored: StoredAccountingEvent,
        *,
        source_owner: bool,
    ) -> None:
        with self._state_guard:
            if source_owner:
                self._release_source_owner_locked(retained_key)
            self._retained.pop(retained_key, None)
            pending_key = self._pending_key(stored)
            if stored.projected_at is None:
                self._pending_events.add(pending_key)
            else:
                self._pending_events.discard(pending_key)
            cleared = self._clear_transient_block_locked()
        if cleared:
            self._log_admission_reopened()

    def _release_source_owner_locked(self, key: _RetainedKey) -> None:
        owner_count = self._source_inflight[key]
        if owner_count == 1:
            self._source_inflight.pop(key)
        else:
            self._source_inflight[key] = owner_count - 1

    def _finish_source_failure(
        self,
        key: _RetainedKey,
        event: AccountingEvent,
        *,
        retain_for_retry: bool,
    ) -> None:
        with self._state_guard:
            self._release_source_owner_locked(key)
            if retain_for_retry:
                self._retained.setdefault(key, event)
            else:
                self._retained.pop(key, None)

    def health_snapshot(self) -> AccountingHealthSnapshot:
        with self._state_guard:
            last_full_reconciliation = self._last_full_reconciliation
            return AccountingHealthSnapshot(
                state=self._state,
                initialized=self._initialized,
                accepting=self._accepting,
                active_sessions=len(self._sessions) + len(self._owner_tasks),
                active_session_limit=self._max_active_sessions,
                reset_pending_keys=len(self._reset_pending_at),
                pending_source_events=self._unsettled_count_locked(),
                projection_attempts=self._projection_attempts,
                fingerprint_conflicts=self._fingerprint_conflicts,
                source_orphans=self._source_orphans,
                sink_orphans=self._sink_orphans,
                unrolled_children=(
                    last_full_reconciliation.unrolled_children
                    if last_full_reconciliation is not None
                    else 0
                ),
                last_full_reconciliation=last_full_reconciliation,
                last_error_code=self._last_error_code,
                last_error_at=self._last_error_at,
            )

    def _publish_full_reconciliation(self, report: object) -> ReconciliationReport:
        if not isinstance(report, ReconciliationReport) or not report.full:
            raise AccountingValidationError
        with self._state_guard:
            self._last_full_reconciliation = report
        return report

    async def _full_reconcile_locked(self) -> ReconciliationReport:
        report = await asyncio.to_thread(
            reconcile_accounting_repositories,
            self._source,
            self._sink,
            page_size=min(
                self._projector_batch_size,
                ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
            ),
        )
        return self._publish_full_reconciliation(report)

    async def start(self) -> None:
        self._bind_loop()
        task = self._start_task
        if task is not None and not task.done():
            await asyncio.shield(task)
            return
        with self._state_guard:
            if self._state is AccountingHealthState.RUNNING:
                return
            retryable_block = (
                self._state is AccountingHealthState.BLOCKED
                and not self._initialized
                and self._fatal_error_code is None
            )
            if self._state is not AccountingHealthState.NEW and not retryable_block:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            previous_state = self._state
            previous_accepting = self._accepting
            previous_initialized = self._initialized
            self._state = AccountingHealthState.STARTING
            self._accepting = False
            self._initialized = False
        workflow = self._start_workflow()
        try:
            task = asyncio.create_task(
                workflow,
                name="accounting-start",
            )
        except Exception:
            workflow.close()
            with self._state_guard:
                if self._state is AccountingHealthState.STARTING:
                    self._state = previous_state
                    self._accepting = previous_accepting
                    self._initialized = previous_initialized
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        except BaseException:
            workflow.close()
            with self._state_guard:
                if self._state is AccountingHealthState.STARTING:
                    self._state = previous_state
                    self._accepting = previous_accepting
                    self._initialized = previous_initialized
            raise
        self._start_task = task
        task.add_done_callback(self._start_task_done)
        await asyncio.shield(task)

    @staticmethod
    def _start_task_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        task.exception()

    async def _start_workflow(self) -> None:
        async with self._lifecycle:
            with self._state_guard:
                if self._state is not AccountingHealthState.STARTING:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            try:
                async with self._drain:
                    clean, tolerated = await self._drain_pending_locked()
                    report = None
                    if clean and not tolerated:
                        report = await self._full_reconcile_locked()
            except AccountingError as exc:
                self._record_error(
                    exc.code,
                    permanent=exc.code in _PERMANENT_CODES,
                )
                self._set_state(
                    (AccountingHealthState.FAILED if self._has_fatal_error() else AccountingHealthState.BLOCKED),
                    accepting=False,
                    initialized=False,
                )
                raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
            except Exception:
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=False,
                )
                self._set_state(
                    AccountingHealthState.BLOCKED,
                    accepting=False,
                    initialized=False,
                )
                raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
            except BaseException:
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
                raise
            if not clean:
                self._set_state(
                    (AccountingHealthState.FAILED if self._has_fatal_error() else AccountingHealthState.BLOCKED),
                    accepting=False,
                    initialized=False,
                )
                raise AccountingError(AccountingErrorCode.RECONCILE_FAILED)
            if not tolerated and (report is None or not report.clean):
                self._record_error(
                    AccountingErrorCode.RECONCILE_FAILED,
                    permanent=True,
                )
                self._set_state(
                    AccountingHealthState.FAILED,
                    accepting=False,
                    initialized=False,
                )
                raise AccountingError(AccountingErrorCode.RECONCILE_FAILED)
            with self._state_guard:
                if self._state is not AccountingHealthState.STARTING:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._projector_stop.clear()
            self._projector_wake.clear()
            workflow = self._projector_loop()
            try:
                self._projector_task = asyncio.create_task(
                    workflow,
                    name="accounting-projector",
                )
            except Exception:
                workflow.close()
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=False,
                )
                self._set_state(
                    AccountingHealthState.BLOCKED,
                    accepting=False,
                    initialized=False,
                )
                raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
            except BaseException:
                workflow.close()
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
                self._set_state(
                    AccountingHealthState.FAILED,
                    accepting=False,
                    initialized=False,
                )
                raise
            self._projector_task.add_done_callback(self._projector_task_done)
            self._set_state(
                AccountingHealthState.RUNNING,
                accepting=True,
                initialized=True,
            )

    async def reconcile(self) -> ReconciliationReport:
        """Drain runtime backlog without exposing a public full-audit mode."""

        self._bind_loop()
        with self._state_guard:
            if (
                not self._initialized
                or self._fatal_error_code is not None
                or self._state
                not in {
                    AccountingHealthState.RUNNING,
                    AccountingHealthState.BLOCKED,
                }
            ):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            task = self._reconcile_task
            create_task = task is None or task.done()
        if create_task:
            workflow = self._reconcile_workflow()
            try:
                task = asyncio.create_task(
                    workflow,
                    name="accounting-reconcile",
                )
            except Exception:
                workflow.close()
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
            except BaseException:
                workflow.close()
                raise
            with self._state_guard:
                self._reconcile_task = task
            task.add_done_callback(self._reconcile_task_done)
        return await asyncio.shield(task)

    @staticmethod
    def _reconcile_task_done(task: asyncio.Task[ReconciliationReport]) -> None:
        if task.cancelled():
            return
        task.exception()

    async def _reconcile_workflow(self) -> ReconciliationReport:
        try:
            async with self._drain:
                retained_clean = await self._retry_retained_locked()
                pending_clean, _ = await self._drain_pending_locked()
                with self._state_guard:
                    retained_events = len(self._retained)
                    pending_source_events = len(self._pending_events)
                    if not retained_clean:
                        retained_clean = retained_events == 0
        except AccountingError as exc:
            self._record_error(
                exc.code,
                permanent=exc.code in _PERMANENT_CODES,
            )
            raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
        except Exception:
            self._record_error(
                AccountingErrorCode.ACCOUNTING_FAILED,
                permanent=False,
            )
            raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
        except BaseException:
            self._record_error(
                AccountingErrorCode.ACCOUNTING_FAILED,
                permanent=True,
            )
            raise

        report = ReconciliationReport(
            mode=ReconciliationMode.RUNTIME,
            retained_events=retained_events,
            pending_source_events=pending_source_events,
        )
        if retained_clean and pending_clean and report.clean:
            self._clear_transient_block()
        else:
            self._record_error(
                AccountingErrorCode.RECONCILE_FAILED,
                permanent=False,
            )
        return report

    async def reserve(
        self,
        *,
        request_id: str,
        api_key_id: int | None,
        budget_usd: float | None,
        spent_usd: float,
        rpm_limit: int | None,
        tpm_limit: int | None,
        estimate_usd: float,
    ) -> AccountingReservation:
        """Acquire one bounded request session before upstream dispatch."""

        self._bind_loop()
        key_id = None if api_key_id is None else _api_key_id(api_key_id)
        budget = _optional_non_negative_float(budget_usd)
        spent = _non_negative_float(spent_usd)
        rpm = _optional_positive_int(rpm_limit)
        tpm = _optional_positive_int(tpm_limit)
        estimate = _non_negative_float(estimate_usd)
        reservation = AccountingReservation(
            reservation_id=secrets.token_hex(16),
            request_id=request_id,
            api_key_id=key_id,
            reserved_usd=estimate,
        )

        with self._state_guard:
            if self._state is not AccountingHealthState.RUNNING or not self._accepting:
                raise AccountingError(AccountingErrorCode.ADMISSION_CLOSED)
            existing_id = self._session_by_request.get(reservation.request_id)
            if existing_id is not None:
                existing = self._sessions[existing_id].reservation
                if (
                    existing.api_key_id != key_id
                    or existing.reserved_usd != estimate
                ):
                    raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                return existing
            if key_id is not None:
                fence = self._key_fences.get(key_id)
                if fence is not None:
                    raise AccountingError(fence)
            if len(self._sessions) >= self._max_active_sessions:
                raise AccountingError(AccountingErrorCode.ACTIVE_SESSION_LIMIT)

            if key_id is not None:
                if key_id not in self._known_key_ids:
                    self._budget_ledger.sync_record(
                        key_id,
                        budget_usd=budget,
                        spent_usd=spent,
                    )
                    self._known_key_ids.add(key_id)
                    self._key_limits[key_id] = (rpm, tpm)
                else:
                    rpm, tpm = self._key_limits[key_id]
                if not self._budget_ledger.reserve_request(
                    reservation.request_id,
                    key_id,
                    estimate,
                ):
                    raise AccountingError(AccountingErrorCode.BUDGET_EXHAUSTED)
                try:
                    rate_error = self._rate_limiter.admit_request(
                        reservation.request_id,
                        key_id,
                        rpm_limit=rpm,
                        tpm_limit=tpm,
                    )
                except BaseException as admission_error:
                    cleanup_error: BaseException | None = None
                    try:
                        self._budget_ledger.release_request(
                            reservation.request_id,
                            key_id,
                        )
                    except BaseException as exc:
                        cleanup_error = exc
                    if not isinstance(admission_error, Exception):
                        raise admission_error.with_traceback(
                            admission_error.__traceback__
                        )
                    if cleanup_error is not None and not isinstance(
                        cleanup_error,
                        Exception,
                    ):
                        raise cleanup_error.with_traceback(
                            cleanup_error.__traceback__
                        ) from None
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if rate_error is not None:
                    self._budget_ledger.release_request(reservation.request_id, key_id)
                    if rate_error == REQUEST_ALREADY_TERMINAL:
                        raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                    raise AccountingError(AccountingErrorCode.RATE_LIMITED)

            self._sessions[reservation.reservation_id] = _AccountingSession(
                reservation=reservation,
            )
            self._session_by_request[reservation.request_id] = reservation.reservation_id
            if key_id is not None:
                self._active_by_key[key_id] = self._active_by_key.get(key_id, 0) + 1
            self._session_drain.clear()
        return reservation

    def begin_deep_research_parent(
        self,
        reservation: AccountingReservation,
        *,
        gateway_model: str,
        auth_identity: DeepResearchAuthIdentity,
    ) -> tuple[DeepResearchParentHandle, str]:
        """Open one bounded child collector for an admitted parent request."""
        self._bind_loop()
        with self._state_guard:
            session = self._session_locked(reservation)
            if (
                session is None
                or session.state != "reserved"
                or session.owner_task is not None
                or not self._accepting
            ):
                raise AccountingError(AccountingErrorCode.ADMISSION_CLOSED)
        return self._deep_research_accounting.begin(
            reservation=reservation,
            gateway_model=gateway_model,
            auth_identity=auth_identity,
        )

    def resolve_deep_research_context(
        self,
        token: str,
    ) -> DeepResearchResolvedContext:
        self._bind_loop()
        return self._deep_research_accounting.resolve(token)

    async def reserve_deep_research_child(
        self,
        token: str,
        *,
        request_id: str,
        estimate_usd: float,
    ) -> DeepResearchChildAdmission:
        """Reserve one ordinary accounting session under a signed parent token."""
        self._bind_loop()
        prepared = self._deep_research_accounting.prepare_child(
            token,
            request_id=request_id,
        )
        try:
            reservation = await self.reserve(
                request_id=request_id,
                api_key_id=prepared.auth_identity.api_key_id,
                budget_usd=None,
                spent_usd=0.0,
                rpm_limit=None,
                tpm_limit=None,
                estimate_usd=estimate_usd,
            )
        except BaseException:
            self._deep_research_accounting.abort_child(prepared)
            raise
        try:
            return self._deep_research_accounting.bind_child(
                prepared,
                reservation,
            )
        except BaseException as exc:
            try:
                await self.release(reservation)
            except BaseException:
                pass
            self._deep_research_accounting.abort_child(prepared)
            if isinstance(exc, Exception):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
            raise

    async def seal_deep_research_children(
        self,
        handle: DeepResearchParentHandle,
    ) -> DeepResearchChildSeal:
        self._bind_loop()
        return await self._deep_research_accounting.seal(handle)

    async def cancel_deep_research_children(
        self,
        handle: DeepResearchParentHandle,
    ) -> None:
        self._bind_loop()
        await self._deep_research_accounting.cancel(handle)

    def _session_locked(
        self,
        reservation: AccountingReservation,
    ) -> _AccountingSession | None:
        if not isinstance(reservation, AccountingReservation):
            raise AccountingValidationError
        session = self._sessions.get(reservation.reservation_id)
        if session is None:
            return None
        if session.reservation != reservation:
            raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
        return session

    def _detach_session_locked(
        self,
        session: _AccountingSession,
    ) -> tuple[int | None, bool, bool]:
        reservation = session.reservation
        self._sessions.pop(reservation.reservation_id, None)
        self._session_by_request.pop(reservation.request_id, None)
        if (
            session.event_key is not None
            and self._event_session_owner.get(session.event_key) == reservation.reservation_id
        ):
            self._event_session_owner.pop(session.event_key, None)
        session.state = "terminal"
        key_id = reservation.api_key_id
        pending_reset = False
        deleted = False
        if key_id is not None:
            active = self._active_by_key[key_id] - 1
            if active:
                self._active_by_key[key_id] = active
            else:
                self._active_by_key.pop(key_id, None)
                pending_reset = key_id in self._reset_pending_at
                deleted = key_id in self._deleted_keys
        if not self._sessions:
            self._session_drain.set()
        return key_id, pending_reset, deleted

    async def _after_session_terminal(
        self,
        key_id: int | None,
        *,
        pending_reset: bool,
        deleted: bool,
    ) -> None:
        if key_id is None:
            return
        if deleted:
            self._budget_ledger.discard_record(key_id)
            self._rate_limiter.reset(key_id)
            with self._state_guard:
                self._known_key_ids.discard(key_id)
                self._key_limits.pop(key_id, None)
            return
        if pending_reset:
            await self._finish_pending_reset(key_id)

    async def release(self, reservation: AccountingReservation) -> bool:
        self._bind_loop()
        with self._state_guard:
            session = self._session_locked(reservation)
            if session is None:
                return False
            if session.owner_task is not None:
                if session.state != "releasing":
                    raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                task = session.owner_task
            else:
                if session.state != "reserved":
                    raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                session.state = "releasing"
                workflow = self._release_session_workflow(reservation.reservation_id)
                try:
                    task = asyncio.create_task(
                        workflow,
                        name="accounting-session-release",
                    )
                except Exception:
                    workflow.close()
                    session.state = "reserved"
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                except BaseException:
                    workflow.close()
                    session.state = "reserved"
                    raise
                session.owner_task = task
                self._session_tasks.add(task)
                task.add_done_callback(self._session_task_done)
        await asyncio.shield(task)
        return True

    async def _release_session_workflow(self, session_id: str) -> None:
        try:
            with self._state_guard:
                session = self._sessions.get(session_id)
                if session is None:
                    return
                reservation = session.reservation
                key_id = reservation.api_key_id
                if key_id is not None:
                    if not self._budget_ledger.release_request(
                        reservation.request_id,
                        key_id,
                    ):
                        raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                    if not self._rate_limiter.attribute_request_tokens(
                        reservation.request_id,
                        key_id,
                        0,
                    ):
                        raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                key_id, pending_reset, deleted = self._detach_session_locked(session)
            self._deep_research_accounting.finish_child_release(reservation)
            await self._after_session_terminal(
                key_id,
                pending_reset=pending_reset,
                deleted=deleted,
            )
        except BaseException:
            await self._finish_failed_session(session_id)
            raise

    async def commit(
        self,
        reservation: AccountingReservation,
        event: AccountingEvent,
    ) -> AccountingReceipt | None:
        self._bind_loop()
        if not isinstance(event, AccountingEvent):
            raise AccountingValidationError
        with self._state_guard:
            session = self._session_locked(reservation)
            if session is None:
                return None
            if (
                event.request_id != reservation.request_id
                or event.api_key_id != reservation.api_key_id
            ):
                raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
            event_key = (event.event_id, event.billing_fingerprint)
            if session.owner_task is not None:
                if session.event_key != event_key:
                    raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                task = session.owner_task
            else:
                if session.state != "reserved":
                    raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
                session.state = "committing"
                session.event_key = event_key
                owner_id = self._event_session_owner.get(event_key)
                if owner_id is None:
                    unsettled_count = self._unsettled_count_locked()
                    if (
                        event_key not in self._retained
                        and event_key not in self._source_inflight
                        and event_key not in self._pending_events
                        and unsettled_count >= self._max_retained_events
                    ):
                        session.state = "reserved"
                        session.event_key = None
                        raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                    self._source_inflight[event_key] = self._source_inflight.get(event_key, 0) + 1
                    session.retry_result = asyncio.get_running_loop().create_future()
                    self._retry_sessions[event_key] = reservation.reservation_id
                    self._event_session_owner[event_key] = reservation.reservation_id
                    workflow = self._commit_session_workflow(
                        reservation.reservation_id,
                        event,
                        event_key,
                    )
                    source_owner = True
                else:
                    owner = self._sessions.get(owner_id)
                    if owner is None or owner.owner_task is None:
                        session.state = "reserved"
                        session.event_key = None
                        raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                    workflow = self._follow_session_workflow(
                        reservation.reservation_id,
                        owner.owner_task,
                        event,
                    )
                    source_owner = False
                try:
                    task = asyncio.create_task(
                        workflow,
                        name="accounting-session-commit",
                    )
                except Exception:
                    workflow.close()
                    if source_owner:
                        self._release_source_owner_locked(event_key)
                        self._retry_sessions.pop(event_key, None)
                        self._event_session_owner.pop(event_key, None)
                    session.state = "reserved"
                    session.event_key = None
                    session.retry_result = None
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                except BaseException:
                    workflow.close()
                    if source_owner:
                        self._release_source_owner_locked(event_key)
                        self._retry_sessions.pop(event_key, None)
                        self._event_session_owner.pop(event_key, None)
                    session.state = "reserved"
                    session.event_key = None
                    session.retry_result = None
                    raise
                session.owner_task = task
                self._session_tasks.add(task)
                task.add_done_callback(self._session_task_done)
        return await asyncio.shield(task)

    def _session_task_done(
        self,
        task: asyncio.Task[AccountingReceipt | None],
    ) -> None:
        self._session_tasks.discard(task)
        if task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass
        else:
            task.exception()

    def _apply_session_liability(
        self,
        session_id: str,
        acceptance: SourceAcceptance,
    ) -> None:
        with self._state_guard:
            session = self._sessions.get(session_id)
            if session is None or session.liability_applied:
                return
            stored = acceptance.stored_event
            if session.event_key != self._pending_key(stored):
                raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
            reservation = session.reservation
            key_id = reservation.api_key_id
            charge_liability = acceptance.status in {
                SourceStatus.ACCEPTED,
                SourceStatus.RECOVERED,
            }
            billable_liability = (
                charge_liability
                and stored.event.kind is AccountingEventKind.CHARGE
            )
            if session.charge_liability is None:
                session.charge_liability = charge_liability
            elif session.charge_liability is not charge_liability:
                raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
            if key_id is None:
                session.ledger_terminalized = True
                session.limiter_terminalized = True
                return
            if not session.ledger_terminalized:
                try:
                    if billable_liability:
                        ledger_ok = self._budget_ledger.commit_request(
                            reservation.request_id,
                            key_id,
                            stored.event.usage.cost,
                        )
                    else:
                        ledger_ok = self._budget_ledger.release_request(
                            reservation.request_id,
                            key_id,
                        )
                except AccountingError:
                    raise
                except Exception:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if ledger_ok is not True:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                session.ledger_terminalized = True
            if not session.limiter_terminalized:
                try:
                    limiter_ok = self._rate_limiter.attribute_request_tokens(
                        reservation.request_id,
                        key_id,
                        stored.event.usage.total_tokens if billable_liability else 0,
                    )
                except AccountingError:
                    raise
                except Exception:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if limiter_ok is not True:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                session.limiter_terminalized = True

    def _apply_duplicate_session(self, session_id: str) -> None:
        with self._state_guard:
            session = self._sessions.get(session_id)
            if session is None or session.liability_applied:
                return
            reservation = session.reservation
            key_id = reservation.api_key_id
            if session.charge_liability is None:
                session.charge_liability = False
            elif session.charge_liability is not False:
                raise AccountingError(AccountingErrorCode.RESERVATION_CONFLICT)
            if key_id is None:
                session.ledger_terminalized = True
                session.limiter_terminalized = True
                return
            if not session.ledger_terminalized:
                try:
                    ledger_ok = self._budget_ledger.release_request(
                        reservation.request_id,
                        key_id,
                    )
                except AccountingError:
                    raise
                except Exception:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if ledger_ok is not True:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                session.ledger_terminalized = True
            if not session.limiter_terminalized:
                try:
                    limiter_ok = self._rate_limiter.attribute_request_tokens(
                        reservation.request_id,
                        key_id,
                        0,
                    )
                except AccountingError:
                    raise
                except Exception:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if limiter_ok is not True:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                session.limiter_terminalized = True

    async def _follow_session_workflow(
        self,
        session_id: str,
        owner_task: asyncio.Task[AccountingReceipt | None],
        event: AccountingEvent,
    ) -> AccountingReceipt:
        try:
            owner_receipt = await asyncio.shield(owner_task)
            if not isinstance(owner_receipt, AccountingReceipt):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            while True:
                try:
                    self._apply_duplicate_session(session_id)
                except AccountingError as exc:
                    if exc.code in _PERMANENT_CODES:
                        self._record_error(exc.code, permanent=True)
                        raise
                    self._record_error(exc.code, permanent=False)
                    await asyncio.sleep(self._projector_backoff_seconds)
                    continue
                break
            receipt = replace(
                owner_receipt,
                source_status=SourceStatus.DUPLICATE,
            )
        except BaseException:
            await self._finish_failed_session(session_id)
            raise
        try:
            await self._finish_committed_session(session_id, event, receipt)
        except BaseException:
            await self._finish_failed_session(session_id)
            raise
        self._clear_transient_block()
        return receipt

    async def _commit_session_workflow(
        self,
        session_id: str,
        event: AccountingEvent,
        event_key: _RetainedKey,
    ) -> AccountingReceipt:
        try:
            receipt = await self._record_workflow(
                event,
                event_key,
                on_acceptance=lambda acceptance: self._apply_session_liability(
                    session_id,
                    acceptance,
                ),
            )
        except AccountingError as exc:
            with self._state_guard:
                session = self._sessions.get(session_id)
                retry_result = None if session is None else session.retry_result
                liability_applied = bool(session and session.liability_applied)
                retryable = (
                    session is not None
                    and exc.code not in _PERMANENT_CODES
                    and (event_key in self._retained or liability_applied)
                )
                if retryable:
                    session.state = "retrying"
            if retry_result is not None and retryable:
                try:
                    receipt = await asyncio.shield(retry_result)
                except BaseException:
                    await self._finish_failed_session(session_id)
                    raise
            else:
                await self._finish_failed_session(session_id)
                raise
        except BaseException:
            await self._finish_failed_session(session_id)
            raise
        finally:
            with self._state_guard:
                if self._retry_sessions.get(event_key) == session_id:
                    self._retry_sessions.pop(event_key, None)

        try:
            await self._finish_committed_session(session_id, event, receipt)
        except BaseException:
            await self._finish_failed_session(session_id)
            raise
        return receipt

    async def _finish_failed_session(self, session_id: str) -> None:
        cleanup_error: BaseException | None = None
        with self._state_guard:
            session = self._sessions.get(session_id)
            if session is None:
                return
            reservation = session.reservation
            if session.charge_liability is None and reservation.api_key_id is not None:
                if not session.ledger_terminalized:
                    try:
                        if self._budget_ledger.release_request(
                            reservation.request_id,
                            reservation.api_key_id,
                        ):
                            session.ledger_terminalized = True
                    except BaseException as exc:
                        cleanup_error = exc
                if not session.limiter_terminalized:
                    try:
                        if self._rate_limiter.attribute_request_tokens(
                            reservation.request_id,
                            reservation.api_key_id,
                            0,
                        ):
                            session.limiter_terminalized = True
                    except BaseException as exc:
                        if cleanup_error is None or (
                            isinstance(cleanup_error, Exception)
                            and not isinstance(exc, Exception)
                        ):
                            cleanup_error = exc
            key_id, pending_reset, deleted = self._detach_session_locked(session)
        self._deep_research_accounting.finish_child_release(reservation)
        await self._after_session_terminal(
            key_id,
            pending_reset=pending_reset,
            deleted=deleted,
        )
        if cleanup_error is not None:
            if isinstance(cleanup_error, AccountingError) or not isinstance(
                cleanup_error,
                Exception,
            ):
                raise cleanup_error.with_traceback(cleanup_error.__traceback__)
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None

    async def _finish_committed_session(
        self,
        session_id: str,
        event: AccountingEvent,
        receipt: AccountingReceipt,
    ) -> None:
        with self._state_guard:
            session = self._sessions.get(session_id)
            if session is None:
                return
            if not session.liability_applied:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            reservation = session.reservation
        self._deep_research_accounting.finish_child_commit(
            reservation,
            event,
            receipt,
        )
        with self._state_guard:
            session = self._sessions.get(session_id)
            if session is None or session.reservation != reservation:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            key_id, pending_reset, deleted = self._detach_session_locked(session)
        await self._after_session_terminal(
            key_id,
            pending_reset=pending_reset,
            deleted=deleted,
        )

    @staticmethod
    def _validate_key_record(record: object, key_id: int) -> ApiKeyRecord:
        if (
            record is None
            or type(getattr(record, "id", None)) is not int
            or getattr(record, "id", None) != key_id
            or not hasattr(record, "budget_usd")
            or not hasattr(record, "spent_usd")
        ):
            raise AccountingValidationError
        _optional_non_negative_float(record.budget_usd)
        _non_negative_float(record.spent_usd)
        return record

    def _sync_key_record(self, record: ApiKeyRecord) -> None:
        rpm = _optional_positive_int(record.rpm)
        tpm = _optional_positive_int(record.tpm)
        self._budget_ledger.reset_record(
            record.id,
            budget_usd=record.budget_usd,
            spent_usd=record.spent_usd,
        )
        with self._state_guard:
            self._known_key_ids.add(record.id)
            self._key_limits[record.id] = (rpm, tpm)

    async def _drain_before_key_mutation_locked(self) -> None:
        retained_clean = await self._retry_retained_locked()
        pending_clean, _ = await self._drain_pending_locked()
        with self._state_guard:
            clean = (
                retained_clean
                and pending_clean
                and not self._retained
                and not self._source_inflight
                and not self._pending_events
            )
        if not clean:
            raise AccountingError(AccountingErrorCode.DRAIN_FAILED)

    def _key_mutation_task_done(self, task: asyncio.Task[object]) -> None:
        self._key_mutation_tasks.discard(task)
        if task.cancelled():
            try:
                task.exception()
            except BaseException:
                pass
        else:
            task.exception()

    async def update_key(
        self,
        key_id: int,
        *,
        changes: Mapping[str, object],
        reset_spent: bool,
    ) -> ApiKeyRecord | None:
        self._bind_loop()
        workflow = self._update_key_workflow(
            key_id,
            changes=changes,
            reset_spent=reset_spent,
        )
        try:
            task = asyncio.create_task(workflow, name="accounting-key-update")
        except Exception:
            workflow.close()
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        except BaseException:
            workflow.close()
            raise
        self._key_mutation_tasks.add(task)
        task.add_done_callback(self._key_mutation_task_done)
        return await asyncio.shield(task)

    async def _update_key_workflow(
        self,
        key_id: int,
        *,
        changes: Mapping[str, object],
        reset_spent: bool,
    ) -> ApiKeyRecord | None:
        key_id = _api_key_id(key_id)
        if not isinstance(changes, Mapping) or not isinstance(reset_spent, bool):
            raise AccountingValidationError
        frozen_changes = dict(changes)
        with self._state_guard:
            if self._state not in {
                AccountingHealthState.RUNNING,
                AccountingHealthState.BLOCKED,
            }:
                raise AccountingError(AccountingErrorCode.ADMISSION_CLOSED)
            if key_id in self._deleted_keys:
                return None
            if key_id in self._key_fences:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_IN_FLIGHT)
            resets_limiter = bool({"rpm", "tpm"} & frozen_changes.keys())
            if (reset_spent or resets_limiter) and self._active_by_key.get(key_id, 0):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_IN_FLIGHT)
            self._key_fences[key_id] = AccountingErrorCode.ACCOUNTING_IN_FLIGHT
        try:
            async with self._drain:
                await self._drain_before_key_mutation_locked()
                record = await asyncio.to_thread(
                    self._sink.update_accounting_key,
                    key_id,
                    changes=frozen_changes,
                    reset_spent=reset_spent,
                )
            if record is None:
                return None
            record = self._validate_key_record(record, key_id)
            self._sync_key_record(record)
            if resets_limiter:
                self._rate_limiter.reset(key_id)
            return record
        except AccountingError:
            raise
        except Exception:
            raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
        finally:
            with self._state_guard:
                if self._key_fences.get(key_id) is AccountingErrorCode.ACCOUNTING_IN_FLIGHT:
                    self._key_fences.pop(key_id, None)

    async def reset_due_budgets(self, *, now: datetime) -> None:
        self._bind_loop()
        workflow = self._reset_due_budgets_workflow(now=now)
        try:
            task = asyncio.create_task(workflow, name="accounting-due-reset")
        except Exception:
            workflow.close()
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        except BaseException:
            workflow.close()
            raise
        self._key_mutation_tasks.add(task)
        task.add_done_callback(self._key_mutation_task_done)
        await asyncio.shield(task)

    async def _reset_due_budgets_workflow(self, *, now: datetime) -> None:
        if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
            raise AccountingValidationError
        moment = now.astimezone(timezone.utc)
        with self._state_guard:
            if self._state not in {
                AccountingHealthState.RUNNING,
                AccountingHealthState.BLOCKED,
            }:
                raise AccountingError(AccountingErrorCode.ADMISSION_CLOSED)
        after_key_id: int | None = None
        while True:
            try:
                raw_key_ids = await asyncio.to_thread(
                    self._sink.list_due_budget_key_ids,
                    now=moment,
                    limit=self._projector_batch_size,
                    after_key_id=after_key_id,
                )
            except AccountingError:
                raise
            except Exception:
                raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
            key_ids = self._validate_due_key_ids(raw_key_ids, after_key_id=after_key_id)
            if not key_ids:
                return
            eligible: list[int] = []
            with self._state_guard:
                for key_id in key_ids:
                    if key_id in self._deleted_keys:
                        continue
                    fence = self._key_fences.get(key_id)
                    if fence is not None and fence is not AccountingErrorCode.BUDGET_RESET_IN_PROGRESS:
                        continue
                    if self._active_by_key.get(key_id, 0):
                        self._reset_pending_at[key_id] = moment
                        self._key_fences[key_id] = AccountingErrorCode.BUDGET_RESET_IN_PROGRESS
                    else:
                        self._reset_pending_at[key_id] = moment
                        self._key_fences[key_id] = AccountingErrorCode.BUDGET_RESET_IN_PROGRESS
                        eligible.append(key_id)
            if eligible:
                await self._reset_due_keys(moment, tuple(eligible))
            after_key_id = key_ids[-1]
            if len(key_ids) < self._projector_batch_size:
                return

    def _validate_due_key_ids(
        self,
        rows: object,
        *,
        after_key_id: int | None,
    ) -> tuple[int, ...]:
        if type(rows) is not tuple or len(rows) > self._projector_batch_size:
            raise AccountingValidationError
        previous = after_key_id or 0
        normalized = []
        for value in rows:
            key_id = _api_key_id(value)
            if key_id <= previous:
                raise AccountingValidationError
            normalized.append(key_id)
            previous = key_id
        return tuple(normalized)

    async def _finish_pending_reset(self, key_id: int) -> None:
        with self._state_guard:
            moment = self._reset_pending_at.get(key_id)
            if moment is None or self._active_by_key.get(key_id, 0):
                return
        await self._reset_due_keys(moment, (key_id,))

    async def _reset_due_keys(
        self,
        moment: datetime,
        key_ids: tuple[int, ...],
    ) -> None:
        try:
            async with self._drain:
                await self._drain_before_key_mutation_locked()
                records = await asyncio.to_thread(
                    self._sink.reset_due_accounting_budgets,
                    now=moment,
                    eligible_key_ids=key_ids,
                )
            if type(records) is not tuple:
                raise AccountingValidationError
            seen: set[int] = set()
            for raw_record in records:
                record_id = getattr(raw_record, "id", None)
                if record_id not in key_ids or record_id in seen:
                    raise AccountingValidationError
                record = self._validate_key_record(raw_record, record_id)
                seen.add(record.id)
                self._sync_key_record(record)
        except AccountingError:
            raise
        except Exception:
            raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
        else:
            with self._state_guard:
                for key_id in key_ids:
                    self._reset_pending_at.pop(key_id, None)
                    if self._key_fences.get(key_id) is AccountingErrorCode.BUDGET_RESET_IN_PROGRESS:
                        self._key_fences.pop(key_id, None)

    async def delete_key(self, key_id: int) -> bool:
        self._bind_loop()
        workflow = self._delete_key_workflow(key_id)
        try:
            task = asyncio.create_task(workflow, name="accounting-key-delete")
        except Exception:
            workflow.close()
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        except BaseException:
            workflow.close()
            raise
        self._key_mutation_tasks.add(task)
        task.add_done_callback(self._key_mutation_task_done)
        return await asyncio.shield(task)

    async def _delete_key_workflow(self, key_id: int) -> bool:
        key_id = _api_key_id(key_id)
        with self._state_guard:
            if self._state not in {
                AccountingHealthState.RUNNING,
                AccountingHealthState.BLOCKED,
            }:
                raise AccountingError(AccountingErrorCode.ADMISSION_CLOSED)
            if key_id in self._deleted_keys:
                return False
            if key_id in self._key_fences and key_id not in self._reset_pending_at:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_IN_FLIGHT)
            previous_pending = self._reset_pending_at.get(key_id)
            self._key_fences[key_id] = AccountingErrorCode.ADMISSION_CLOSED
        try:
            async with self._drain:
                deleted = await asyncio.to_thread(
                    self._sink.delete_to_accounting_tombstone,
                    key_id,
                    deleted_at=self._now(),
                )
            if not isinstance(deleted, bool):
                raise AccountingValidationError
        except BaseException as exc:
            with self._state_guard:
                if previous_pending is None:
                    self._key_fences.pop(key_id, None)
                else:
                    self._key_fences[key_id] = AccountingErrorCode.BUDGET_RESET_IN_PROGRESS
            if isinstance(exc, AccountingError) or not isinstance(exc, Exception):
                raise
            raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
        if not deleted:
            with self._state_guard:
                if previous_pending is None:
                    self._key_fences.pop(key_id, None)
                else:
                    self._key_fences[key_id] = AccountingErrorCode.BUDGET_RESET_IN_PROGRESS
            return False
        with self._state_guard:
            self._deleted_keys.add(key_id)
            self._reset_pending_at.pop(key_id, None)
            active = self._active_by_key.get(key_id, 0)
        if not active:
            self._budget_ledger.discard_record(key_id)
            self._rate_limiter.reset(key_id)
            with self._state_guard:
                self._known_key_ids.discard(key_id)
                self._key_limits.pop(key_id, None)
        return True

    async def record(self, event: AccountingEvent) -> AccountingReceipt:
        self._bind_loop()
        if not isinstance(event, AccountingEvent):
            raise AccountingValidationError
        key = (event.event_id, event.billing_fingerprint)
        with self._state_guard:
            if self._state is not AccountingHealthState.RUNNING or not self._accepting:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            if len(self._owner_tasks) >= self._max_owner_tasks:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            unsettled_count = self._unsettled_count_locked()
            if (
                key not in self._retained
                and key not in self._source_inflight
                and key not in self._pending_events
                and unsettled_count >= self._max_retained_events
            ):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._source_inflight[key] = self._source_inflight.get(key, 0) + 1
        workflow = self._record_workflow(event, key)
        try:
            task = asyncio.create_task(
                workflow,
                name="accounting-record",
            )
        except Exception:
            workflow.close()
            with self._state_guard:
                self._release_source_owner_locked(key)
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        except BaseException:
            workflow.close()
            with self._state_guard:
                self._release_source_owner_locked(key)
            raise
        with self._state_guard:
            self._owner_tasks.add(task)
        task.add_done_callback(self._owner_task_done)
        return await asyncio.shield(task)

    def _owner_task_done(self, task: asyncio.Task[AccountingReceipt]) -> None:
        terminal_error: BaseException | None = None
        if task.cancelled():
            try:
                task.exception()
            except BaseException as exc:
                terminal_error = exc
        else:
            error = task.exception()
            if isinstance(error, BaseException) and not isinstance(error, Exception):
                terminal_error = error
        with self._state_guard:
            self._owner_tasks.discard(task)
            if terminal_error is not None and self._terminal_owner_error is None:
                self._terminal_owner_error = terminal_error

    async def _record_workflow(
        self,
        event: AccountingEvent,
        retained_key: _RetainedKey,
        *,
        on_acceptance: Callable[[SourceAcceptance], None] | None = None,
    ) -> AccountingReceipt:
        try:
            acceptance = await asyncio.to_thread(
                self._source.accept_accounting_event,
                event,
            )
            stored = self._validate_source_result(acceptance, retained_key)
        except AccountingError as exc:
            permanent = exc.code in _PERMANENT_CODES
            self._finish_source_failure(
                retained_key,
                event,
                retain_for_retry=not permanent,
            )
            self._record_error(exc.code, permanent=permanent)
            self._projector_wake.set()
            raise
        except Exception:
            self._finish_source_failure(
                retained_key,
                event,
                retain_for_retry=True,
            )
            safe_error = AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._record_error(safe_error.code, permanent=False)
            self._projector_wake.set()
            raise safe_error from None
        except BaseException:
            self._finish_source_failure(
                retained_key,
                event,
                retain_for_retry=True,
            )
            self._record_error(
                AccountingErrorCode.ACCOUNTING_FAILED,
                permanent=True,
            )
            self._projector_wake.set()
            raise
        self._accept_source_result(retained_key, stored, source_owner=True)
        if on_acceptance is not None:
            try:
                on_acceptance(acceptance)
            except AccountingError as exc:
                with self._state_guard:
                    self._retained.setdefault(retained_key, stored.event)
                self._record_error(
                    exc.code,
                    permanent=exc.code in _PERMANENT_CODES,
                )
                self._projector_wake.set()
                raise
            except Exception:
                with self._state_guard:
                    self._retained.setdefault(retained_key, stored.event)
                safe_error = AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                self._record_error(safe_error.code, permanent=False)
                self._projector_wake.set()
                raise safe_error from None
            except BaseException:
                with self._state_guard:
                    self._retained.setdefault(retained_key, stored.event)
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
                self._projector_wake.set()
                raise
        if stored.projected_at is not None:
            if stored.event.kind is AccountingEventKind.CHARGE and stored.event.api_key_id is not None:
                projection_status = ProjectionStatus.ALREADY_APPLIED
            else:
                projection_status = ProjectionStatus.NO_SINK
            return self._receipt(acceptance, projection_status)
        try:
            projection_status = await self._project_stored(stored)
        except _ToleratedProjectionFailure as exc:
            # Ledger-integrity failure scoped to this one key's spend row.
            # The event stays unsettled (projected_at IS NULL) for operator
            # follow-up, but the key and the service both keep accepting
            # requests instead of tripping into FAILED.
            logger.error(
                "Permanent spend-projection failure for api_key_id=%s "
                "(%s); event remains unsettled for operator follow-up.",
                event.api_key_id,
                exc.code.value,
            )
            self._record_error(exc.code, permanent=False, block=False)
            self._projector_wake.set()
            return self._receipt(acceptance, ProjectionStatus.PENDING)
        except AccountingError as exc:
            self._record_error(
                exc.code,
                permanent=exc.code in _PERMANENT_CODES,
            )
            self._projector_wake.set()
            raise
        except Exception:
            safe_error = AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._record_error(safe_error.code, permanent=True)
            self._projector_wake.set()
            raise safe_error from None
        except BaseException:
            self._record_error(
                AccountingErrorCode.ACCOUNTING_FAILED,
                permanent=True,
            )
            self._projector_wake.set()
            raise
        return self._receipt(acceptance, projection_status)

    @staticmethod
    def _receipt(
        acceptance: SourceAcceptance,
        projection_status: ProjectionStatus,
    ) -> AccountingReceipt:
        stored = acceptance.stored_event
        return AccountingReceipt(
            source_status=acceptance.status,
            projection_status=projection_status,
            event_id=stored.event.event_id,
            billing_fingerprint=stored.billing_fingerprint,
            usage_row_id=stored.usage_row_id,
            child_event_ids=stored.event.child_event_ids,
            child_fingerprints=stored.event.child_fingerprints,
        )

    async def _project_stored(
        self,
        stored: StoredAccountingEvent,
    ) -> ProjectionStatus:
        async with self._drain:
            return await self._project_stored_locked(stored)

    async def _project_stored_locked(
        self,
        stored: StoredAccountingEvent,
    ) -> ProjectionStatus:
        with self._state_guard:
            self._projection_attempts += 1
        attempted_at = self._now()
        event = stored.event
        if event.kind is AccountingEventKind.CHARGE and event.api_key_id is not None:
            try:
                projection_status = await asyncio.to_thread(
                    self._sink.apply_spend_event,
                    stored,
                    attempted_at,
                )
            except AccountingError as exc:
                source_already_projected = await self._projection_failed(stored, exc)
                if source_already_projected and exc.code not in _PERMANENT_CODES:
                    return ProjectionStatus.ALREADY_APPLIED
                if exc.code in _PERMANENT_CODES:
                    if exc.code in _TOLERATED_SINK_CODES:
                        # Data-integrity failure scoped to this one key's
                        # ledger row (the sink call itself rejected it), as
                        # opposed to a source-side bookkeeping conflict.
                        # Callers tolerate this instead of failing closed.
                        raise _ToleratedProjectionFailure(exc.code) from exc
                    raise
                return ProjectionStatus.PENDING
            except Exception:
                safe_error = AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
                if await self._projection_failed(stored, safe_error):
                    return ProjectionStatus.ALREADY_APPLIED
                return ProjectionStatus.PENDING
            if (
                projection_status is not ProjectionStatus.APPLIED
                and projection_status is not ProjectionStatus.ALREADY_APPLIED
            ):
                contract_error = AccountingError(AccountingErrorCode.INVALID_CONTRACT)
                await self._projection_failed(stored, contract_error)
                raise contract_error
        else:
            projection_status = ProjectionStatus.NO_SINK

        try:
            marked = await asyncio.to_thread(
                self._source.mark_accounting_event_projected,
                event.event_id,
                stored.billing_fingerprint,
                attempted_at,
            )
            self._validate_projection_mark(
                marked,
                self._pending_key(stored),
                require_projected=True,
            )
        except AccountingError as exc:
            if exc.code in _PERMANENT_CODES:
                raise
            self._record_error(exc.code, permanent=False)
            self._projector_wake.set()
            return ProjectionStatus.PENDING
        except Exception:
            self._record_error(
                AccountingErrorCode.SOURCE_WRITE_FAILED,
                permanent=False,
            )
            self._projector_wake.set()
            return ProjectionStatus.PENDING
        self._clear_pending(stored)
        return projection_status

    async def _projection_failed(
        self,
        stored: StoredAccountingEvent,
        error: AccountingError,
    ) -> bool:
        permanent = error.code in _PERMANENT_CODES
        try:
            marked = await asyncio.to_thread(
                self._source.mark_accounting_projection_failed,
                stored.event.event_id,
                stored.billing_fingerprint,
                error.code,
                self._now(),
            )
            marked = self._validate_projection_mark(
                marked,
                self._pending_key(stored),
                require_projected=False,
            )
        except AccountingError as marker_error:
            if marker_error.code in _PERMANENT_CODES:
                raise
            self._record_error(
                marker_error.code,
                permanent=False,
            )
            return False
        except Exception:
            self._record_error(
                AccountingErrorCode.SOURCE_WRITE_FAILED,
                permanent=False,
            )
            return False
        else:
            if marked.projected_at is not None:
                self._clear_pending(stored)
                return True
            if permanent:
                return False
            self._record_error(
                error.code,
                permanent=False,
                block=False,
            )
            return False
        finally:
            self._projector_wake.set()

    async def _retry_retained_locked(self) -> bool:
        while True:
            with self._state_guard:
                retained = tuple(self._retained.items())[: self._projector_batch_size]
            if not retained:
                return True
            batch_succeeded = True
            for key, event in retained:
                try:
                    acceptance = await asyncio.to_thread(
                        self._source.accept_accounting_event,
                        event,
                    )
                    stored = self._validate_source_result(acceptance, key)
                except AccountingError as exc:
                    permanent = exc.code in _PERMANENT_CODES
                    if permanent:
                        with self._state_guard:
                            self._retained.pop(key, None)
                        self._publish_retry_error(key, exc)
                        raise
                    batch_succeeded = False
                    self._record_error(exc.code, permanent=False)
                    continue
                except Exception:
                    batch_succeeded = False
                    self._record_error(
                        AccountingErrorCode.ACCOUNTING_FAILED,
                        permanent=False,
                    )
                    continue
                except BaseException as exc:
                    self._publish_retry_error(key, exc)
                    raise
                self._accept_source_result(key, stored, source_owner=False)
                with self._state_guard:
                    session_id = self._retry_sessions.get(key)
                    session = None if session_id is None else self._sessions.get(session_id)
                if session_id is not None:
                    if (
                        acceptance.status is SourceStatus.DUPLICATE
                        and (session is None or session.charge_liability is not False)
                    ):
                        acceptance = SourceAcceptance(
                            status=SourceStatus.RECOVERED,
                            stored_event=stored,
                        )
                    try:
                        self._apply_session_liability(session_id, acceptance)
                    except AccountingError as exc:
                        if exc.code in _PERMANENT_CODES:
                            self._publish_retry_error(key, exc)
                            raise
                        with self._state_guard:
                            self._retained.setdefault(key, stored.event)
                        batch_succeeded = False
                        self._record_error(exc.code, permanent=False)
                        continue
                    except BaseException as exc:
                        self._publish_retry_error(key, exc)
                        raise
                if stored.projected_at is not None:
                    if stored.event.kind is AccountingEventKind.CHARGE and stored.event.api_key_id is not None:
                        status = ProjectionStatus.ALREADY_APPLIED
                    else:
                        status = ProjectionStatus.NO_SINK
                    self._publish_retry_receipt(
                        key,
                        self._receipt(acceptance, status),
                    )
                    continue
                tolerated = False
                try:
                    status = await self._project_stored_locked(stored)
                except _ToleratedProjectionFailure as exc:
                    # Ledger-integrity failure scoped to this key's spend
                    # row. The event stays unsettled (projected_at IS NULL)
                    # for operator follow-up; don't count it against batch
                    # cleanliness, since retrying will just fail again
                    # forever and would spin the projector in a permanent
                    # retry-backoff cycle.
                    logger.error(
                        "Permanent spend-projection failure for "
                        "api_key_id=%s (%s) during retry; event remains "
                        "unsettled for operator follow-up.",
                        stored.event.api_key_id,
                        exc.code.value,
                    )
                    self._record_error(exc.code, permanent=False, block=False)
                    status = ProjectionStatus.PENDING
                    tolerated = True
                except AccountingError as exc:
                    if exc.code in _PERMANENT_CODES:
                        self._publish_retry_error(key, exc)
                        raise
                    return False
                except Exception:
                    safe_error = AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
                    self._publish_retry_error(key, safe_error)
                    raise safe_error from None
                self._publish_retry_receipt(
                    key,
                    self._receipt(acceptance, status),
                )
                if status is ProjectionStatus.PENDING and not tolerated:
                    batch_succeeded = False
            if not batch_succeeded:
                return False

    def _publish_retry_receipt(
        self,
        key: _RetainedKey,
        receipt: AccountingReceipt,
    ) -> None:
        with self._state_guard:
            session_id = self._retry_sessions.get(key)
            session = None if session_id is None else self._sessions.get(session_id)
            future = None if session is None else session.retry_result
        if future is not None and not future.done():
            future.set_result(receipt)

    def _publish_retry_error(
        self,
        key: _RetainedKey,
        error: BaseException,
    ) -> None:
        with self._state_guard:
            session_id = self._retry_sessions.get(key)
            session = None if session_id is None else self._sessions.get(session_id)
            future = None if session is None else session.retry_result
        if future is not None and not future.done():
            future.set_exception(error)

    def _publish_all_retry_errors(self, error: BaseException) -> None:
        with self._state_guard:
            futures = tuple(
                session.retry_result
                for session_id in self._retry_sessions.values()
                if (session := self._sessions.get(session_id)) is not None
                and session.retry_result is not None
            )
        for future in futures:
            if not future.done():
                future.set_exception(error)

    async def _drain_pending_locked(self) -> tuple[bool, bool]:
        after: tuple[datetime, str] | None = None
        clean = True
        tolerated_permanent_failure = False
        while True:
            try:
                raw_rows = await asyncio.to_thread(
                    self._source.list_pending_accounting_events,
                    limit=self._projector_batch_size,
                    after=after,
                )
                rows = self._validate_pending_page(raw_rows, after=after)
            except AccountingError:
                raise
            except Exception:
                raise AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED) from None
            if not rows:
                break
            for stored in rows:
                self._mark_pending(stored)
                try:
                    status = await self._project_stored_locked(stored)
                except _ToleratedProjectionFailure as exc:
                    # Ledger-integrity failure scoped to this key's spend
                    # row. The event stays unsettled (projected_at IS NULL)
                    # for operator follow-up; don't let it trip the whole
                    # service into FAILED, and don't mark `clean = False`
                    # either, since retrying will just fail again forever
                    # and would spin the projector in a permanent
                    # retry-backoff cycle.
                    logger.error(
                        "Permanent spend-projection failure for "
                        "api_key_id=%s (%s) during drain; event remains "
                        "unsettled for operator follow-up.",
                        stored.event.api_key_id,
                        exc.code.value,
                    )
                    self._record_error(exc.code, permanent=False, block=False)
                    tolerated_permanent_failure = True
                    continue
                except AccountingError as exc:
                    if exc.code in _PERMANENT_CODES:
                        raise
                    clean = False
                    continue
                except Exception:
                    raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
                if status is ProjectionStatus.PENDING:
                    clean = False
            last = rows[-1]
            after = (last.created_at, last.event.event_id)
            if len(rows) < self._projector_batch_size:
                break
        return clean, tolerated_permanent_failure

    async def _projector_loop(self) -> None:
        retry_needed = False
        while not self._projector_stop.is_set():
            if retry_needed:
                self._projector_wake.clear()
                try:
                    await asyncio.wait_for(
                        self._projector_stop.wait(),
                        timeout=self._projector_backoff_seconds,
                    )
                except TimeoutError:
                    pass
            else:
                await self._projector_wake.wait()
                self._projector_wake.clear()
            if self._projector_stop.is_set() or self._has_fatal_error():
                break
            try:
                async with self._drain:
                    retained_clean = await self._retry_retained_locked()
                    pending_clean, _ = await self._drain_pending_locked()
                    if not retained_clean:
                        with self._state_guard:
                            retained_clean = not self._retained
            except AccountingError as exc:
                permanent = exc.code in _PERMANENT_CODES
                self._record_error(exc.code, permanent=permanent)
                if permanent:
                    self._publish_all_retry_errors(exc)
                    break
                retry_needed = True
                continue
            except asyncio.CancelledError as exc:
                self._publish_all_retry_errors(exc)
                raise
            except BaseException as exc:
                self._publish_all_retry_errors(exc)
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
                raise
            retry_needed = not (retained_clean and pending_clean)
            if retained_clean and pending_clean:
                self._clear_transient_block()

    def _projector_task_done(self, task: asyncio.Task[None]) -> None:
        if task.cancelled():
            try:
                task.exception()
            except BaseException as error:
                self._publish_all_retry_errors(error)
            if not self._projector_stop.is_set():
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
            return
        try:
            error = task.exception()
        except BaseException:
            error = BaseException()
        if error is not None and not self._has_fatal_error():
            self._record_error(
                AccountingErrorCode.ACCOUNTING_FAILED,
                permanent=True,
            )
        if error is not None:
            self._publish_all_retry_errors(error)

    async def stop(self) -> None:
        self._bind_loop()
        with self._state_guard:
            if self._state is AccountingHealthState.STOPPED:
                return
            if self._state is AccountingHealthState.NEW:
                self._state = AccountingHealthState.STOPPED
                self._accepting = False
                return
            previous_state = self._state
            previous_accepting = self._accepting
            self._state = AccountingHealthState.STOPPING
            self._accepting = False
        task = self._stop_task
        if task is None or task.done():
            previous_stop_task = task
            workflow = self._stop_workflow()
            try:
                task = asyncio.create_task(
                    workflow,
                    name="accounting-stop",
                )
            except Exception:
                workflow.close()
                with self._state_guard:
                    if self._state is AccountingHealthState.STOPPING and self._stop_task is previous_stop_task:
                        self._state = previous_state
                        self._accepting = previous_accepting
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
            except BaseException:
                workflow.close()
                with self._state_guard:
                    if self._state is AccountingHealthState.STOPPING and self._stop_task is previous_stop_task:
                        self._state = previous_state
                        self._accepting = previous_accepting
                raise
            self._stop_task = task
            task.add_done_callback(self._stop_task_done)
        await asyncio.shield(task)

    @staticmethod
    def _stop_task_done(task: asyncio.Task[None]) -> None:
        if task.cancelled():
            return
        task.exception()

    async def _stop_workflow(self) -> None:
        terminal_error: BaseException | None = None
        start_task = self._start_task
        if start_task is not None:
            start_results = await asyncio.gather(
                start_task,
                return_exceptions=True,
            )
            for result in start_results:
                if terminal_error is None and isinstance(result, BaseException) and not isinstance(result, Exception):
                    terminal_error = result
        async with self._lifecycle:
            with self._state_guard:
                if self._state is AccountingHealthState.STOPPED:
                    return
                self._state = AccountingHealthState.STOPPING
                self._accepting = False
            await self._session_drain.wait()
            with self._state_guard:
                session_tasks = tuple(self._session_tasks)
            if session_tasks:
                session_results = await asyncio.gather(
                    *session_tasks,
                    return_exceptions=True,
                )
                for result in session_results:
                    if (
                        terminal_error is None
                        and isinstance(result, BaseException)
                        and not isinstance(result, Exception)
                    ):
                        terminal_error = result
            key_mutation_tasks = tuple(self._key_mutation_tasks)
            if key_mutation_tasks:
                mutation_results = await asyncio.gather(
                    *key_mutation_tasks,
                    return_exceptions=True,
                )
                for result in mutation_results:
                    if (
                        terminal_error is None
                        and isinstance(result, BaseException)
                        and not isinstance(result, Exception)
                    ):
                        terminal_error = result
            with self._state_guard:
                owner_tasks = tuple(self._owner_tasks)
                completed_owner_error = self._terminal_owner_error
                self._terminal_owner_error = None
            if terminal_error is None and completed_owner_error is not None:
                terminal_error = completed_owner_error
            if owner_tasks:
                owner_results = await asyncio.gather(
                    *owner_tasks,
                    return_exceptions=True,
                )
                with self._state_guard:
                    completed_owner_error = self._terminal_owner_error
                    self._terminal_owner_error = None
                if terminal_error is None and completed_owner_error is not None:
                    terminal_error = completed_owner_error
                for result in owner_results:
                    if (
                        terminal_error is None
                        and isinstance(result, BaseException)
                        and not isinstance(result, Exception)
                    ):
                        terminal_error = result
            reconcile_task = self._reconcile_task
            if reconcile_task is not None:
                reconcile_results = await asyncio.gather(
                    reconcile_task,
                    return_exceptions=True,
                )
                for result in reconcile_results:
                    if isinstance(result, BaseException) and not isinstance(result, Exception):
                        if terminal_error is None:
                            terminal_error = result
                        self._record_error(
                            AccountingErrorCode.ACCOUNTING_FAILED,
                            permanent=True,
                        )

            self._projector_stop.set()
            self._projector_wake.set()
            projector = self._projector_task
            if projector is not None:
                try:
                    await projector
                except BaseException as exc:
                    if terminal_error is None:
                        terminal_error = exc
                    self._record_error(
                        AccountingErrorCode.ACCOUNTING_FAILED,
                        permanent=True,
                    )
                finally:
                    self._projector_task = None

            retained_clean = False
            pending_clean = False
            report: ReconciliationReport | None = None
            try:
                async with self._drain:
                    retained_clean = await self._retry_retained_locked()
                    pending_clean, _ = await self._drain_pending_locked()
                    if not retained_clean:
                        with self._state_guard:
                            retained_clean = not self._retained
                    if retained_clean and pending_clean:
                        report = await self._full_reconcile_locked()
            except AccountingError as exc:
                self._record_error(
                    exc.code,
                    permanent=exc.code in _PERMANENT_CODES,
                )
                pending_clean = False
            except Exception:
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=False,
                )
                pending_clean = False
            except BaseException as exc:
                self._record_error(
                    AccountingErrorCode.ACCOUNTING_FAILED,
                    permanent=True,
                )
                if terminal_error is None:
                    terminal_error = exc
                pending_clean = False
            if report is not None and not report.clean:
                self._record_error(
                    AccountingErrorCode.RECONCILE_FAILED,
                    permanent=True,
                )
            with self._state_guard:
                terminal_clean = (
                    retained_clean
                    and pending_clean
                    and report is not None
                    and report.clean
                    and self._unsettled_count_locked() == 0
                    and not self._sessions
                    and not self._active_by_key
                    and not self._reset_pending_at
                    and self._fatal_error_code is None
                )
                if terminal_clean:
                    self._state = AccountingHealthState.STOPPED
                    self._accepting = False
                else:
                    self._state = AccountingHealthState.FAILED
                    self._accepting = False
            if terminal_error is not None:
                raise terminal_error.with_traceback(terminal_error.__traceback__)
            if not terminal_clean:
                raise AccountingError(AccountingErrorCode.DRAIN_FAILED)
