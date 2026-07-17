"""Bounded full reconciliation for the durable accounting repositories."""

from __future__ import annotations

from collections.abc import Iterator
from typing import Protocol, TypeVar, cast

from .accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    AccountingError,
    AccountingErrorCode,
    AccountingEventKind,
    AccountingOwnerAuditRow,
    AccountingOwnerState,
    AccountingParentLinkState,
    AccountingSinkAuditRow,
    AccountingSourceAuditKind,
    AccountingSourceAuditRow,
    AccountingValidationError,
    ReconciliationMode,
    ReconciliationReport,
)


class AccountingSourceAuditRepository(Protocol):
    def list_accounting_source_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSourceAuditRow, ...]: ...


class AccountingSinkAuditRepository(Protocol):
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


_AuditRow = TypeVar("_AuditRow", AccountingSourceAuditRow, AccountingSinkAuditRow)
_ORPHAN_OWNER_STATES = frozenset(
    {AccountingOwnerState.MISSING, AccountingOwnerState.AMBIGUOUS}
)


def _validate_page_size(value: object) -> int:
    if (
        isinstance(value, bool)
        or not isinstance(value, int)
        or value <= 0
        or value > ACCOUNTING_AUDIT_MAX_PAGE_SIZE
    ):
        raise AccountingValidationError
    return value


def _validate_page(
    rows: object,
    *,
    row_type: type[_AuditRow],
    limit: int,
    after_event_id: str | None,
) -> tuple[_AuditRow, ...]:
    if type(rows) is not tuple or len(rows) > limit:
        raise AccountingValidationError
    previous = after_event_id
    for row in rows:
        if not isinstance(row, row_type):
            raise AccountingValidationError
        if previous is not None and row.event_id <= previous:
            raise AccountingValidationError
        previous = row.event_id
    return cast(tuple[_AuditRow, ...], rows)


def _owner_states_for_source_page(
    sink: AccountingSinkAuditRepository,
    rows: tuple[AccountingSourceAuditRow, ...],
) -> dict[int, AccountingOwnerState]:
    api_key_ids = tuple(
        dict.fromkeys(
            row.api_key_id
            for row in rows
            if row.event_kind is AccountingEventKind.CHARGE
            and row.api_key_id is not None
        )
    )
    if not api_key_ids:
        return {}
    raw_owner_rows = sink.get_accounting_owner_states(api_key_ids=api_key_ids)
    if type(raw_owner_rows) is not tuple or len(raw_owner_rows) != len(api_key_ids):
        raise AccountingValidationError
    owner_states: dict[int, AccountingOwnerState] = {}
    for expected_key_id, owner_row in zip(api_key_ids, raw_owner_rows, strict=True):
        if (
            not isinstance(owner_row, AccountingOwnerAuditRow)
            or owner_row.api_key_id != expected_key_id
        ):
            raise AccountingValidationError
        owner_states[expected_key_id] = owner_row.owner_state
    return owner_states


def _source_stream(
    source: AccountingSourceAuditRepository,
    sink: AccountingSinkAuditRepository,
    *,
    page_size: int,
    scanned: list[int],
) -> Iterator[tuple[AccountingSourceAuditRow, AccountingOwnerState | None]]:
    after_event_id: str | None = None
    while True:
        rows = _validate_page(
            source.list_accounting_source_audit_rows(
                limit=page_size,
                after_event_id=after_event_id,
            ),
            row_type=AccountingSourceAuditRow,
            limit=page_size,
            after_event_id=after_event_id,
        )
        if not rows:
            return
        owner_states = _owner_states_for_source_page(sink, rows)
        scanned[0] += len(rows)
        for row in rows:
            owner_state = None
            if row.event_kind is AccountingEventKind.CHARGE and row.api_key_id is not None:
                owner_state = owner_states[row.api_key_id]
            yield row, owner_state
        after_event_id = rows[-1].event_id
        if len(rows) < page_size:
            return


def _sink_stream(
    sink: AccountingSinkAuditRepository,
    *,
    page_size: int,
    scanned: list[int],
) -> Iterator[AccountingSinkAuditRow]:
    after_event_id: str | None = None
    while True:
        rows = _validate_page(
            sink.list_accounting_sink_audit_rows(
                limit=page_size,
                after_event_id=after_event_id,
            ),
            row_type=AccountingSinkAuditRow,
            limit=page_size,
            after_event_id=after_event_id,
        )
        if not rows:
            return
        scanned[0] += len(rows)
        yield from rows
        after_event_id = rows[-1].event_id
        if len(rows) < page_size:
            return


def _is_owner_orphan(owner_state: AccountingOwnerState | None) -> bool:
    return owner_state in _ORPHAN_OWNER_STATES


def _is_sink_owner_orphan(row: AccountingSinkAuditRow) -> bool:
    return _is_owner_orphan(row.owner_state) or (
        row.sink_kind == "tombstone"
        and row.owner_state is AccountingOwnerState.ACTIVE
    )


def _reconcile_accounting_repositories(
    source: AccountingSourceAuditRepository,
    sink: AccountingSinkAuditRepository,
    *,
    page_size: int = ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
) -> ReconciliationReport:
    """Merge bounded source/sink pages without cross-database attachment."""

    page_size = _validate_page_size(page_size)
    source_scanned = [0]
    sink_scanned = [0]
    source_rows = iter(
        _source_stream(
            source,
            sink,
            page_size=page_size,
            scanned=source_scanned,
        )
    )
    sink_rows = iter(_sink_stream(sink, page_size=page_size, scanned=sink_scanned))

    counters = {
        "pending_source_events": 0,
        "usage_without_outbox": 0,
        "pending_without_usage": 0,
        "projected_without_receipt": 0,
        "receipt_without_source": 0,
        "unexpected_receipts": 0,
        "fingerprint_mismatches": 0,
        "api_key_mismatches": 0,
        "cost_mismatches": 0,
        "owner_orphans": 0,
        "unrolled_children": 0,
    }

    source_item = next(source_rows, None)
    sink_row = next(sink_rows, None)
    while source_item is not None or sink_row is not None:
        if source_item is None:
            counters["receipt_without_source"] += 1
            if _is_sink_owner_orphan(sink_row):
                counters["owner_orphans"] += 1
            sink_row = next(sink_rows, None)
            continue

        source_row, source_owner_state = source_item
        if sink_row is None or source_row.event_id < sink_row.event_id:
            if source_row.row_kind is AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX:
                counters["usage_without_outbox"] += 1
            else:
                if source_row.projected_at is None:
                    counters["pending_source_events"] += 1
                    if not source_row.usage_present:
                        counters["pending_without_usage"] += 1
                elif (
                    source_row.event_kind is AccountingEventKind.CHARGE
                    and source_row.api_key_id is not None
                ):
                    counters["projected_without_receipt"] += 1
            if source_row.parent_link_state is AccountingParentLinkState.UNROLLED:
                counters["unrolled_children"] += 1
            if _is_owner_orphan(source_owner_state):
                counters["owner_orphans"] += 1
            source_item = next(source_rows, None)
            continue

        if sink_row.event_id < source_row.event_id:
            counters["receipt_without_source"] += 1
            if _is_sink_owner_orphan(sink_row):
                counters["owner_orphans"] += 1
            sink_row = next(sink_rows, None)
            continue

        if source_row.parent_link_state is AccountingParentLinkState.UNROLLED:
            counters["unrolled_children"] += 1
        if source_row.row_kind is AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX:
            counters["usage_without_outbox"] += 1
            counters["receipt_without_source"] += 1
        else:
            if source_row.projected_at is None:
                counters["pending_source_events"] += 1
                if not source_row.usage_present:
                    counters["pending_without_usage"] += 1
            expects_receipt = (
                source_row.event_kind is AccountingEventKind.CHARGE
                and source_row.api_key_id is not None
            )
            if not expects_receipt:
                counters["unexpected_receipts"] += 1
            else:
                if source_row.billing_fingerprint != sink_row.billing_fingerprint:
                    counters["fingerprint_mismatches"] += 1
                if source_row.api_key_id != sink_row.api_key_id:
                    counters["api_key_mismatches"] += 1
                if source_row.spend_usd != sink_row.spend_usd:
                    counters["cost_mismatches"] += 1
        if (
            source_row.api_key_id == sink_row.api_key_id
            and source_owner_state is not None
            and source_owner_state is not sink_row.owner_state
        ):
            raise AccountingValidationError
        if _is_owner_orphan(source_owner_state) or _is_sink_owner_orphan(sink_row):
            counters["owner_orphans"] += 1
        source_item = next(source_rows, None)
        sink_row = next(sink_rows, None)

    return ReconciliationReport(
        mode=ReconciliationMode.FULL,
        source_rows_scanned=source_scanned[0],
        sink_rows_scanned=sink_scanned[0],
        **counters,
    )


def reconcile_accounting_repositories(
    source: AccountingSourceAuditRepository,
    sink: AccountingSinkAuditRepository,
    *,
    page_size: int = ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
) -> ReconciliationReport:
    """Run one credential-safe full audit while preserving terminal control flow."""

    try:
        return _reconcile_accounting_repositories(
            source,
            sink,
            page_size=page_size,
        )
    except AccountingError:
        raise
    except Exception:
        raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
    except BaseException:
        raise
