from __future__ import annotations

import sqlite3
import traceback
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import pytest

from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingOwnerAuditRow,
    AccountingOwnerState,
    AccountingParentLinkState,
    AccountingSinkAuditRow,
    AccountingSourceAuditKind,
    AccountingSourceAuditRow,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
)
from llm_gateway_core.services.accounting_reconciliation import (
    reconcile_accounting_repositories,
)


NOW = datetime(2026, 7, 14, 8, 0, tzinfo=timezone.utc)


def _source(
    event_id: str,
    *,
    fingerprint: str | None = "a" * 64,
    api_key_id: int | None = 7,
    spend_usd: float = 0.25,
    projected: bool = True,
    usage_present: bool = True,
    row_kind: AccountingSourceAuditKind = AccountingSourceAuditKind.OUTBOX,
    event_kind: AccountingEventKind = AccountingEventKind.CHARGE,
    parent_link_state: AccountingParentLinkState = (
        AccountingParentLinkState.NOT_APPLICABLE
    ),
) -> AccountingSourceAuditRow:
    return AccountingSourceAuditRow(
        event_id=event_id,
        row_kind=row_kind,
        event_kind=event_kind,
        billing_fingerprint=fingerprint,
        api_key_id=api_key_id,
        spend_usd=spend_usd,
        projected_at=NOW if projected else None,
        usage_present=usage_present,
        parent_link_state=parent_link_state,
    )


def _sink(
    event_id: str,
    *,
    fingerprint: str = "a" * 64,
    api_key_id: int = 7,
    spend_usd: float = 0.25,
    owner_state: AccountingOwnerState = AccountingOwnerState.ACTIVE,
    sink_kind: Literal["active", "tombstone"] = "active",
) -> AccountingSinkAuditRow:
    return AccountingSinkAuditRow(
        event_id=event_id,
        billing_fingerprint=fingerprint,
        api_key_id=api_key_id,
        spend_usd=spend_usd,
        applied_at=NOW,
        sink_kind=sink_kind,
        owner_state=owner_state,
    )


class FakeSourceAuditRepository:
    def __init__(self, rows: tuple[AccountingSourceAuditRow, ...]) -> None:
        self.rows = rows
        self.calls: list[tuple[int, str | None]] = []

    def list_accounting_source_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSourceAuditRow, ...]:
        self.calls.append((limit, after_event_id))
        return tuple(
            row
            for row in self.rows
            if after_event_id is None or row.event_id > after_event_id
        )[:limit]


class FakeSinkAuditRepository:
    def __init__(
        self,
        rows: tuple[AccountingSinkAuditRow, ...],
        *,
        owners: dict[int, AccountingOwnerState] | None = None,
    ) -> None:
        self.rows = rows
        self.owners = owners or {}
        self.page_calls: list[tuple[int, str | None]] = []
        self.owner_calls: list[tuple[int, ...]] = []

    def list_accounting_sink_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSinkAuditRow, ...]:
        self.page_calls.append((limit, after_event_id))
        return tuple(
            row
            for row in self.rows
            if after_event_id is None or row.event_id > after_event_id
        )[:limit]

    def get_accounting_owner_states(
        self,
        *,
        api_key_ids: tuple[int, ...],
    ) -> tuple[AccountingOwnerAuditRow, ...]:
        self.owner_calls.append(api_key_ids)
        return tuple(
            AccountingOwnerAuditRow(
                api_key_id=api_key_id,
                owner_state=self.owners.get(
                    api_key_id,
                    AccountingOwnerState.MISSING,
                ),
            )
            for api_key_id in api_key_ids
        )


def test_empty_repositories_are_clean() -> None:
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository(()),
        FakeSinkAuditRepository(()),
        page_size=1,
    )

    assert report.full is True
    assert report.clean is True
    assert report.source_rows_scanned == 0
    assert report.sink_rows_scanned == 0


def test_real_repositories_reconcile_projected_usage_retention(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    source = TokensUsageDB(db_filename="reconcile-source.db")
    sink = ApiKeysDB(db_filename="reconcile-sink.db")
    key = sink.create(name="reconcile-owner")
    event = AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id="event-real-reconciliation",
        kind=AccountingEventKind.CHARGE,
        api_key_id=key.id,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/chat",
        provider="provider",
        model="model",
        usage=AccountingUsage(cost=0.25),
        cost_source=CostSource.UPSTREAM,
        occurred_at=NOW,
    )
    stored = source.accept_accounting_event(event).stored_event
    assert sink.apply_spend_event(stored, NOW) is ProjectionStatus.APPLIED
    source.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        NOW,
    )

    first = reconcile_accounting_repositories(source, sink, page_size=1)
    with sqlite3.connect(source.db_path) as conn:
        conn.execute(
            "DELETE FROM tokens_usage WHERE accounting_event_id = ?",
            (event.event_id,),
        )
    retained = reconcile_accounting_repositories(source, sink, page_size=1)

    assert first.clean is True
    assert retained.clean is True
    assert retained.source_rows_scanned == 1
    assert retained.sink_rows_scanned == 1


def test_page_size_one_merges_interleaved_source_and_sink_ids() -> None:
    source = FakeSourceAuditRepository(
        (_source("event-a"), _source("event-c"))
    )
    sink = FakeSinkAuditRepository(
        (_sink("event-b"), _sink("event-c")),
        owners={7: AccountingOwnerState.ACTIVE},
    )

    report = reconcile_accounting_repositories(source, sink, page_size=1)

    assert report.source_rows_scanned == 2
    assert report.sink_rows_scanned == 2
    assert report.projected_without_receipt == 1
    assert report.receipt_without_source == 1
    assert report.clean is False
    assert all(limit == 1 for limit, _after in source.calls)
    assert all(limit == 1 for limit, _after in sink.page_calls)


def test_usage_only_with_same_id_receipt_counts_two_independent_issues() -> None:
    usage_only = _source(
        "event-a",
        fingerprint=None,
        row_kind=AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX,
        projected=False,
        parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
    )
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository((usage_only,)),
        FakeSinkAuditRepository(
            (_sink("event-a"),),
            owners={7: AccountingOwnerState.ACTIVE},
        ),
    )

    assert report.usage_without_outbox == 1
    assert report.receipt_without_source == 1
    assert report.projected_without_receipt == 0


def test_matched_rows_count_all_mismatches_and_one_owner_orphan() -> None:
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository((_source("event-a"),)),
        FakeSinkAuditRepository(
            (
                _sink(
                    "event-a",
                    fingerprint="b" * 64,
                    api_key_id=8,
                    spend_usd=0.5,
                    owner_state=AccountingOwnerState.AMBIGUOUS,
                ),
            ),
            owners={7: AccountingOwnerState.MISSING},
        ),
    )

    assert report.fingerprint_mismatches == 1
    assert report.api_key_mismatches == 1
    assert report.cost_mismatches == 1
    assert report.owner_orphans == 1


def test_projected_retention_and_unrolled_child_are_observation_only() -> None:
    retained = _source(
        "event-a",
        usage_present=False,
        parent_link_state=AccountingParentLinkState.UNROLLED,
    )
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository((retained,)),
        FakeSinkAuditRepository(
            (_sink("event-a", owner_state=AccountingOwnerState.TOMBSTONE),),
            owners={7: AccountingOwnerState.TOMBSTONE},
        ),
    )

    assert report.unrolled_children == 1
    assert report.pending_without_usage == 0
    assert report.clean is True


@pytest.mark.parametrize("matched_source", (False, True))
def test_historical_tombstone_receipt_with_current_active_owner_is_structural(
    matched_source: bool,
) -> None:
    source_rows = (_source("event-a"),) if matched_source else ()
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository(source_rows),
        FakeSinkAuditRepository(
            (
                _sink(
                    "event-a",
                    sink_kind="tombstone",
                    owner_state=AccountingOwnerState.ACTIVE,
                ),
            ),
            owners={7: AccountingOwnerState.ACTIVE},
        ),
    )

    assert report.owner_orphans == 1
    assert report.receipt_without_source == int(not matched_source)
    assert report.clean is False


def test_receipt_for_rollup_is_unexpected() -> None:
    rollup = _source(
        "event-a",
        event_kind=AccountingEventKind.ROLLUP,
        spend_usd=0.0,
    )
    report = reconcile_accounting_repositories(
        FakeSourceAuditRepository((rollup,)),
        FakeSinkAuditRepository((_sink("event-a", spend_usd=0.0),)),
    )

    assert report.unexpected_receipts == 1


def test_invalid_page_and_owner_response_fail_closed() -> None:
    source = FakeSourceAuditRepository((_source("event-a"),))
    source.list_accounting_source_audit_rows = lambda **_kwargs: [  # type: ignore[method-assign,assignment]
        _source("event-a")
    ]
    with pytest.raises(AccountingValidationError):
        reconcile_accounting_repositories(source, FakeSinkAuditRepository(()))

    sink = FakeSinkAuditRepository((), owners={7: AccountingOwnerState.ACTIVE})
    sink.get_accounting_owner_states = lambda **_kwargs: ()  # type: ignore[method-assign]
    with pytest.raises(AccountingValidationError):
        reconcile_accounting_repositories(
            FakeSourceAuditRepository((_source("event-a"),)),
            sink,
        )


def test_nonadvancing_page_and_terminal_exception_preserve_failure_semantics() -> None:
    repeated = FakeSourceAuditRepository((_source("event-a"),))
    repeated.list_accounting_source_audit_rows = (  # type: ignore[method-assign]
        lambda **_kwargs: (_source("event-a"),)
    )
    with pytest.raises(AccountingValidationError):
        reconcile_accounting_repositories(
            repeated,
            FakeSinkAuditRepository(()),
            page_size=1,
        )

    terminal = KeyboardInterrupt("terminal")
    failing = FakeSourceAuditRepository(())

    def raise_terminal(**_kwargs):
        raise terminal

    failing.list_accounting_source_audit_rows = raise_terminal  # type: ignore[method-assign]
    with pytest.raises(KeyboardInterrupt) as raised:
        reconcile_accounting_repositories(failing, FakeSinkAuditRepository(()))
    assert raised.value is terminal


def test_repository_failures_use_safe_merge_boundary_without_changing_typed_errors() -> None:
    typed = AccountingValidationError()
    typed_source = FakeSourceAuditRepository(())

    def raise_typed(**_kwargs):
        raise typed

    typed_source.list_accounting_source_audit_rows = raise_typed  # type: ignore[method-assign]
    with pytest.raises(AccountingValidationError) as typed_error:
        reconcile_accounting_repositories(
            typed_source,
            FakeSinkAuditRepository(()),
        )
    assert typed_error.value is typed

    marker = "TOP-SECRET-PATH-/srv/hidden.db"
    ordinary_source = FakeSourceAuditRepository(())

    def raise_ordinary(**_kwargs):
        raise RuntimeError(marker)

    ordinary_source.list_accounting_source_audit_rows = raise_ordinary  # type: ignore[method-assign]
    with pytest.raises(AccountingError) as ordinary_error:
        reconcile_accounting_repositories(
            ordinary_source,
            FakeSinkAuditRepository(()),
        )
    rendered = "".join(traceback.format_exception(ordinary_error.value))
    assert ordinary_error.value.code is AccountingErrorCode.RECONCILE_FAILED
    assert marker not in rendered
    assert ordinary_error.value.__suppress_context__ is True


@pytest.mark.parametrize("page_size", (0, 257, True))
def test_page_size_is_hard_bounded(page_size: object) -> None:
    with pytest.raises(AccountingValidationError):
        reconcile_accounting_repositories(
            FakeSourceAuditRepository(()),
            FakeSinkAuditRepository(()),
            page_size=page_size,  # type: ignore[arg-type]
        )
