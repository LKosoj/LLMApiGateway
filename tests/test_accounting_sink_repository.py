from __future__ import annotations

import sqlite3
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path
from sys import float_info

import pytest

from llm_gateway_core.db import accounting_schema
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.services.accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingOwnerState,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
    StoredAccountingEvent,
)


OCCURRED_AT = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
APPLIED_AT = OCCURRED_AT + timedelta(seconds=1)


@pytest.fixture
def sink_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> ApiKeysDB:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return ApiKeysDB(db_filename="accounting-sink.db")


def _stored_charge(
    *,
    event_id: str,
    api_key_id: int | None,
    cost: float,
    occurred_at: datetime = OCCURRED_AT,
) -> StoredAccountingEvent:
    return StoredAccountingEvent(
        event=AccountingEvent(
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
            usage=AccountingUsage(cost=cost),
            cost_source=CostSource.UPSTREAM,
            occurred_at=occurred_at,
        ),
        usage_row_id=1,
        created_at=occurred_at,
    )


def _stored_rollup(*, event_id: str, api_key_id: int) -> StoredAccountingEvent:
    return StoredAccountingEvent(
        event=AccountingEvent(
            version=ACCOUNTING_EVENT_VERSION,
            event_id=event_id,
            kind=AccountingEventKind.ROLLUP,
            api_key_id=api_key_id,
            method="POST",
            route_template="/v1/web/deep-research",
            operation="web_deep_research",
            gateway_model="gateway/research",
            provider=None,
            model=None,
            usage=AccountingUsage(),
            cost_source=CostSource.RECEIPT_ROLLUP,
            occurred_at=OCCURRED_AT,
            child_event_ids=("usage:v1:http:child",),
            child_fingerprints=("a" * 64,),
        ),
        usage_row_id=1,
        created_at=OCCURRED_AT,
    )


def _receipt_rows(db: ApiKeysDB) -> list[tuple]:
    with sqlite3.connect(db.db_path) as conn:
        return conn.execute(
            """
            SELECT event_id, billing_fingerprint, api_key_id, spend_usd,
                   applied_at, sink_kind
            FROM applied_accounting_events
            ORDER BY event_id
            """
        ).fetchall()


def _insert_tombstone(
    db: ApiKeysDB,
    *,
    api_key_id: int,
    spent_usd: float = 0.0,
    last_used_at: str | None = None,
) -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            """
            INSERT INTO api_key_accounting_tombstones (
                api_key_id, deleted_at, spent_usd, last_used_at
            ) VALUES (?, ?, ?, ?)
            """,
            (
                api_key_id,
                "2026-07-13T00:00:00+00:00",
                spent_usd,
                last_used_at,
            ),
        )


class _FaultingConnection:
    def __init__(
        self,
        inner: sqlite3.Connection,
        *,
        execute_error: BaseException | None = None,
        execute_error_call: int = 1,
        rollback_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        self._inner = inner
        self._execute_error = execute_error
        self._execute_error_call = execute_error_call
        self._execute_calls = 0
        self._rollback_error = rollback_error
        self._close_error = close_error

    def __getattr__(self, name: str):
        return getattr(self._inner, name)

    def execute(self, sql: str, parameters: tuple = ()) -> sqlite3.Cursor:
        self._execute_calls += 1
        if (
            self._execute_error is not None
            and self._execute_calls == self._execute_error_call
        ):
            raise self._execute_error
        return self._inner.execute(sql, parameters)

    def rollback(self) -> None:
        if self._rollback_error is not None:
            raise self._rollback_error
        self._inner.rollback()

    def close(self) -> None:
        self._inner.close()
        if self._close_error is not None:
            raise self._close_error


def test_apply_spend_event_updates_active_key_and_writes_exact_receipt(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="active")
    stored = _stored_charge(event_id="usage:v1:http:active", api_key_id=key.id, cost=1.25)

    status = sink_db.apply_spend_event(stored, APPLIED_AT)

    assert status is ProjectionStatus.APPLIED
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(1.25)
    assert fresh.last_used_at == OCCURRED_AT.isoformat()
    assert _receipt_rows(sink_db) == [
        (
            stored.event.event_id,
            stored.billing_fingerprint,
            key.id,
            1.25,
            APPLIED_AT.isoformat(),
            "active",
        )
    ]


def test_zero_cost_still_updates_last_used_and_writes_receipt(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="zero")
    stored = _stored_charge(event_id="usage:v1:http:zero", api_key_id=key.id, cost=0.0)

    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED

    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == 0.0
    assert fresh.last_used_at == OCCURRED_AT.isoformat()
    assert len(_receipt_rows(sink_db)) == 1
    assert _receipt_rows(sink_db)[0][3] == 0.0


def test_apply_spend_event_updates_tombstone_without_active_key(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="deleted")
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute("DELETE FROM api_keys WHERE id = ?", (key.id,))
    _insert_tombstone(
        sink_db,
        api_key_id=key.id,
        spent_usd=2.0,
        last_used_at="2026-07-12T00:00:00+00:00",
    )
    stored = _stored_charge(event_id="usage:v1:http:tombstone", api_key_id=key.id, cost=0.5)

    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED

    with sqlite3.connect(sink_db.db_path) as conn:
        row = conn.execute(
            """
            SELECT spent_usd, last_used_at
            FROM api_key_accounting_tombstones
            WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()
    assert row == (2.5, OCCURRED_AT.isoformat())
    assert _receipt_rows(sink_db)[0][5] == "tombstone"


def test_last_used_at_never_moves_backwards(sink_db: ApiKeysDB) -> None:
    key = sink_db.create(name="monotonic")
    newer = OCCURRED_AT + timedelta(days=1)
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
            (newer.isoformat(), key.id),
        )
    stored = _stored_charge(event_id="usage:v1:http:late", api_key_id=key.id, cost=0.25)

    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED

    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.last_used_at == newer.isoformat()


@pytest.mark.parametrize("sink_kind", ["active", "tombstone"])
def test_malformed_last_used_at_fails_closed_without_spend_or_receipt(
    sink_db: ApiKeysDB,
    sink_kind: str,
) -> None:
    key = sink_db.create(name=f"malformed-{sink_kind}")
    if sink_kind == "active":
        with sqlite3.connect(sink_db.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET last_used_at = ? WHERE id = ?",
                ("not-a-timestamp", key.id),
            )
    else:
        with sqlite3.connect(sink_db.db_path) as conn:
            conn.execute("DELETE FROM api_keys WHERE id = ?", (key.id,))
        _insert_tombstone(
            sink_db,
            api_key_id=key.id,
            last_used_at="not-a-timestamp",
        )
    stored = _stored_charge(
        event_id=f"usage:v1:http:malformed-{sink_kind}",
        api_key_id=key.id,
        cost=1.0,
    )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert _receipt_rows(sink_db) == []
    with sqlite3.connect(sink_db.db_path) as conn:
        table = "api_keys" if sink_kind == "active" else "api_key_accounting_tombstones"
        id_column = "id" if sink_kind == "active" else "api_key_id"
        assert conn.execute(
            f"SELECT spent_usd, last_used_at FROM {table} WHERE {id_column} = ?",
            (key.id,),
        ).fetchone() == (0.0, "not-a-timestamp")


def test_exact_duplicate_returns_already_applied_without_second_spend(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="duplicate")
    stored = _stored_charge(event_id="usage:v1:http:duplicate", api_key_id=key.id, cost=0.75)

    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED
    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.ALREADY_APPLIED

    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(0.75)
    assert len(_receipt_rows(sink_db)) == 1


def test_existing_receipt_mismatch_is_typed_conflict_without_mutation(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="conflict")
    first = _stored_charge(event_id="usage:v1:http:conflict", api_key_id=key.id, cost=0.5)
    conflicting = _stored_charge(event_id=first.event.event_id, api_key_id=key.id, cost=9.0)
    assert sink_db.apply_spend_event(first, APPLIED_AT) is ProjectionStatus.APPLIED

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(conflicting, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(0.5)
    assert len(_receipt_rows(sink_db)) == 1


@pytest.mark.parametrize("owner_count", [0, 2])
def test_missing_or_ambiguous_sink_owner_is_typed_orphan_without_receipt(
    sink_db: ApiKeysDB,
    owner_count: int,
) -> None:
    if owner_count == 0:
        key_id = 77
    else:
        key = sink_db.create(name="both")
        key_id = key.id
        _insert_tombstone(sink_db, api_key_id=key_id, spent_usd=4.0)
    stored = _stored_charge(event_id=f"usage:v1:http:owner-{owner_count}", api_key_id=key_id, cost=1.0)

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.ORPHAN_SINK
    assert _receipt_rows(sink_db) == []
    if owner_count == 2:
        fresh = sink_db.get_by_id(key_id)
        assert fresh is not None
        assert fresh.spent_usd == 0.0
        with sqlite3.connect(sink_db.db_path) as conn:
            assert (
                conn.execute(
                    "SELECT spent_usd FROM api_key_accounting_tombstones WHERE api_key_id = ?",
                    (key_id,),
                ).fetchone()[0]
                == 4.0
            )


def test_receipt_insert_failure_rolls_back_spend_and_last_used(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="rollback")
    stored = _stored_charge(event_id="usage:v1:http:rollback", api_key_id=key.id, cost=2.0)
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_accounting_receipt
            BEFORE INSERT ON applied_accounting_events
            BEGIN
                SELECT RAISE(ABORT, 'injected receipt failure');
            END
            """
        )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert "injected receipt failure" not in str(error.value)
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == 0.0
    assert fresh.last_used_at is None
    assert _receipt_rows(sink_db) == []


def test_spend_update_failure_is_typed_and_writes_no_receipt(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="update-failure")
    stored = _stored_charge(
        event_id="usage:v1:http:update-failure",
        api_key_id=key.id,
        cost=2.0,
    )
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_accounting_spend_update
            BEFORE UPDATE OF spent_usd, last_used_at ON api_keys
            BEGIN
                SELECT RAISE(ABORT, 'injected update failure');
            END
            """
        )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert "injected update failure" not in str(error.value)
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == 0.0
    assert fresh.last_used_at is None
    assert _receipt_rows(sink_db) == []


def test_commit_failure_rolls_back_and_is_credential_safe(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="commit-failure")
    stored = _stored_charge(
        event_id="usage:v1:http:commit-failure",
        api_key_id=key.id,
        cost=2.0,
    )

    def fail_commit(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("injected path SQL secret")

    monkeypatch.setattr(accounting_schema, "commit_accounting_transaction", fail_commit)

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    rendered = f"{error.value!s} {error.value!r}"
    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert "injected" not in rendered
    assert "SQL" not in rendered
    assert "secret" not in rendered
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == 0.0
    assert fresh.last_used_at is None
    assert _receipt_rows(sink_db) == []


def test_committed_spend_close_failure_is_typed_and_retry_uses_exact_receipt(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="committed-close-failure")
    stored = _stored_charge(
        event_id="usage:v1:http:committed-close-failure",
        api_key_id=key.id,
        cost=2.5,
    )
    real_open = accounting_schema.open_accounting_runtime_connection
    open_count = 0

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        if open_count == 1:
            return _FaultingConnection(
                inner,
                close_error=RuntimeError("close-sensitive-detail"),
            )
        return inner

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert "close-sensitive-detail" not in "".join(
        traceback.format_exception(error.value)
    )
    assert error.value.__suppress_context__ is True
    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.ALREADY_APPLIED
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(2.5)
    assert len(_receipt_rows(sink_db)) == 1


def test_ordinary_primary_is_not_masked_by_close_failure(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="ordinary-primary-close-failure")
    stored = _stored_charge(
        event_id="usage:v1:http:ordinary-primary-close-failure",
        api_key_id=key.id,
        cost=1.0,
    )
    real_open = accounting_schema.open_accounting_runtime_connection

    def open_probe(path, *, enable_foreign_keys: bool):
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return _FaultingConnection(
            inner,
            execute_error=RuntimeError("primary-sensitive-detail"),
            close_error=RuntimeError("close-sensitive-detail"),
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert "primary-sensitive-detail" not in rendered
    assert "close-sensitive-detail" not in rendered
    assert error.value.__suppress_context__ is True


def test_terminal_primary_identity_is_not_masked_by_close_failure(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="terminal-primary-close-failure")
    stored = _stored_charge(
        event_id="usage:v1:http:terminal-primary-close-failure",
        api_key_id=key.id,
        cost=1.0,
    )
    terminal = BaseException("terminal-sensitive-detail")
    real_open = accounting_schema.open_accounting_runtime_connection

    def open_probe(path, *, enable_foreign_keys: bool):
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return _FaultingConnection(
            inner,
            execute_error=terminal,
            close_error=RuntimeError("close-sensitive-detail"),
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(BaseException) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value is terminal
    assert "close-sensitive-detail" not in "".join(
        traceback.format_exception(error.value)
    )


@pytest.mark.parametrize("cleanup_stage", ["rollback", "close"])
@pytest.mark.parametrize("primary_terminal", [False, True])
@pytest.mark.parametrize("cleanup_terminal", [False, True])
def test_primary_and_cleanup_failures_follow_terminal_priority(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_stage: str,
    primary_terminal: bool,
    cleanup_terminal: bool,
) -> None:
    key = sink_db.create(
        name=(
            f"cleanup-{cleanup_stage}-{primary_terminal}-{cleanup_terminal}"
        )
    )
    stored = _stored_charge(
        event_id=(
            f"usage:v1:http:cleanup-{cleanup_stage}-"
            f"{primary_terminal}-{cleanup_terminal}"
        ),
        api_key_id=key.id,
        cost=1.0,
    )
    primary_marker = "ordinary-primary-sensitive-detail"
    cleanup_marker = f"{cleanup_stage}-sensitive-detail"
    primary_error: BaseException = (
        BaseException("terminal-primary-sensitive-detail")
        if primary_terminal
        else RuntimeError(primary_marker)
    )
    cleanup_error: BaseException = (
        BaseException(f"terminal-{cleanup_marker}")
        if cleanup_terminal
        else RuntimeError(cleanup_marker)
    )
    real_open = accounting_schema.open_accounting_runtime_connection

    def open_probe(path, *, enable_foreign_keys: bool):
        return _FaultingConnection(
            real_open(path, enable_foreign_keys=enable_foreign_keys),
            execute_error=primary_error,
            execute_error_call=2 if cleanup_stage == "rollback" else 1,
            rollback_error=(cleanup_error if cleanup_stage == "rollback" else None),
            close_error=(cleanup_error if cleanup_stage == "close" else None),
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(BaseException) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    rendered = "".join(traceback.format_exception(error.value))
    if primary_terminal:
        assert error.value is primary_error
        assert cleanup_marker not in rendered
    elif cleanup_terminal:
        assert error.value is cleanup_error
        assert primary_marker not in rendered
    else:
        assert isinstance(error.value, AccountingError)
        assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
        assert primary_marker not in rendered
        assert cleanup_marker not in rendered
        assert error.value.__suppress_context__ is True
    assert _receipt_rows(sink_db) == []


def test_non_finite_spend_total_is_rejected_without_mutation(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="overflow")
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            "UPDATE api_keys SET spent_usd = ? WHERE id = ?",
            (float_info.max, key.id),
        )
    stored = _stored_charge(
        event_id="usage:v1:http:overflow",
        api_key_id=key.id,
        cost=float_info.max,
    )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == float_info.max
    assert fresh.last_used_at is None
    assert _receipt_rows(sink_db) == []


def test_concurrent_duplicate_applies_spend_exactly_once(sink_db: ApiKeysDB) -> None:
    key = sink_db.create(name="concurrent")
    stored = _stored_charge(event_id="usage:v1:http:concurrent", api_key_id=key.id, cost=1.5)
    barrier = threading.Barrier(8)

    def apply(_index: int) -> ProjectionStatus:
        barrier.wait()
        return sink_db.apply_spend_event(stored, APPLIED_AT)

    with ThreadPoolExecutor(max_workers=8) as executor:
        statuses = list(executor.map(apply, range(8)))

    assert statuses.count(ProjectionStatus.APPLIED) == 1
    assert statuses.count(ProjectionStatus.ALREADY_APPLIED) == 7
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(1.5)
    assert len(_receipt_rows(sink_db)) == 1


def test_held_write_lock_obeys_bounded_accounting_timeout(sink_db: ApiKeysDB) -> None:
    key = sink_db.create(name="locked")
    stored = _stored_charge(
        event_id="usage:v1:http:locked",
        api_key_id=key.id,
        cost=1.0,
    )
    blocker = accounting_schema.open_accounting_runtime_connection(
        sink_db.db_path,
        enable_foreign_keys=False,
    )
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(AccountingError) as error:
            sink_db.apply_spend_event(stored, APPLIED_AT)
    finally:
        blocker.rollback()
        blocker.close()

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert 1.5 <= time.monotonic() - started < 4.0
    assert _receipt_rows(sink_db) == []


def test_commit_then_raise_recovers_exact_receipt_without_second_spend(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="ambiguous")
    stored = _stored_charge(event_id="usage:v1:http:ambiguous", api_key_id=key.id, cost=3.0)

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        conn.commit()
        raise RuntimeError("ambiguous commit")

    monkeypatch.setattr(accounting_schema, "commit_accounting_transaction", commit_then_raise)

    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.ALREADY_APPLIED

    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == pytest.approx(3.0)
    assert len(_receipt_rows(sink_db)) == 1


@pytest.mark.parametrize("cleanup_stage", ["rollback", "close"])
def test_ordinary_ambiguous_commit_terminal_cleanup_preserves_identity_without_context(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    cleanup_stage: str,
) -> None:
    key = sink_db.create(name=f"ambiguous-terminal-{cleanup_stage}")
    stored = _stored_charge(
        event_id=f"usage:v1:http:ambiguous-terminal-{cleanup_stage}",
        api_key_id=key.id,
        cost=1.0,
    )
    commit_marker = "ambiguous-commit-credential-sensitive"
    terminal = BaseException(f"terminal-{cleanup_stage}-cleanup")
    real_open = accounting_schema.open_accounting_runtime_connection
    open_count = 0

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        if open_count != 1:
            return inner
        return _FaultingConnection(
            inner,
            rollback_error=(terminal if cleanup_stage == "rollback" else None),
            close_error=(terminal if cleanup_stage == "close" else None),
        )

    def ambiguous_commit(conn: sqlite3.Connection) -> None:
        if cleanup_stage == "close":
            conn.commit()
        raise RuntimeError(commit_marker)

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )
    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        ambiguous_commit,
    )

    with pytest.raises(BaseException) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value is terminal
    assert error.value.__suppress_context__ is True
    assert commit_marker not in "".join(traceback.format_exception(error.value))
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == (1.0 if cleanup_stage == "close" else 0.0)
    assert len(_receipt_rows(sink_db)) == (1 if cleanup_stage == "close" else 0)


def test_ambiguous_commit_recovered_receipt_mismatch_is_context_free_conflict(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="ambiguous-mismatch")
    stored = _stored_charge(
        event_id="usage:v1:http:ambiguous-mismatch",
        api_key_id=key.id,
        cost=1.0,
    )
    commit_marker = "ambiguous-commit-credential-sensitive"
    mismatched_fingerprint = (
        "e" * 64 if stored.billing_fingerprint == "f" * 64 else "f" * 64
    )

    def commit_corrupt_receipt_then_raise(conn: sqlite3.Connection) -> None:
        conn.commit()
        conn.execute(
            """
            UPDATE applied_accounting_events
            SET billing_fingerprint = ?
            WHERE event_id = ?
            """,
            (mismatched_fingerprint, stored.event.event_id),
        )
        conn.commit()
        raise RuntimeError(commit_marker)

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_corrupt_receipt_then_raise,
    )

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    assert error.value.__suppress_context__ is True
    assert commit_marker not in "".join(traceback.format_exception(error.value))
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == 1.0
    assert _receipt_rows(sink_db)[0][1] == mismatched_fingerprint


@pytest.mark.parametrize("commit_first", [False, True])
def test_commit_base_exception_preserves_identity_without_double_spend(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    commit_first: bool,
) -> None:
    key = sink_db.create(name=f"terminal-{commit_first}")
    stored = _stored_charge(
        event_id=f"usage:v1:http:terminal-{commit_first}",
        api_key_id=key.id,
        cost=2.0,
    )
    terminal = BaseException("terminal-sensitive-detail")

    def terminal_commit(conn: sqlite3.Connection) -> None:
        if commit_first:
            conn.commit()
        raise terminal

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        terminal_commit,
    )

    with pytest.raises(BaseException) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value is terminal
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == (2.0 if commit_first else 0.0)
    assert len(_receipt_rows(sink_db)) == (1 if commit_first else 0)
    if commit_first:
        assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.ALREADY_APPLIED
        assert sink_db.get_by_id(key.id).spent_usd == 2.0


@pytest.mark.parametrize("stage", ["open", "execute", "close"])
@pytest.mark.parametrize("commit_first", [False, True])
def test_terminal_commit_wins_over_recovery_failure(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    stage: str,
    commit_first: bool,
) -> None:
    key = sink_db.create(name=f"terminal-recovery-{stage}-{commit_first}")
    stored = _stored_charge(
        event_id=f"usage:v1:http:terminal-recovery-{stage}-{commit_first}",
        api_key_id=key.id,
        cost=1.0,
    )
    terminal = BaseException("terminal-primary")
    real_open = accounting_schema.open_accounting_runtime_connection
    open_count = 0

    class RecoveryConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def execute(self, sql: str, params: tuple = ()):
            if stage == "execute":
                raise RuntimeError("recovery-execute-sensitive")
            return self.inner.execute(sql, params)

        def close(self) -> None:
            self.inner.close()
            if stage == "close":
                raise RuntimeError("recovery-close-sensitive")

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        if open_count == 2 and stage == "open":
            raise RuntimeError("recovery-open-sensitive")
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return inner if open_count == 1 else RecoveryConnection(inner)

    def terminal_commit(conn: sqlite3.Connection) -> None:
        if commit_first:
            conn.commit()
        raise terminal

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )
    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        terminal_commit,
    )

    with pytest.raises(BaseException) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value is terminal
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.spent_usd == (1.0 if commit_first else 0.0)


@pytest.mark.parametrize("invalid_kind", ["unattributed", "rollup", "wrong-type"])
def test_apply_spend_event_rejects_non_sink_contracts_before_writing(
    sink_db: ApiKeysDB,
    invalid_kind: str,
) -> None:
    key = sink_db.create(name=f"invalid-{invalid_kind}")
    if invalid_kind == "unattributed":
        stored: object = _stored_charge(
            event_id="usage:v1:http:unattributed",
            api_key_id=None,
            cost=1.0,
        )
    elif invalid_kind == "rollup":
        stored = _stored_rollup(event_id="usage:v1:http:rollup", api_key_id=key.id)
    else:
        stored = object()

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
    assert _receipt_rows(sink_db) == []


def test_permanent_errors_do_not_expose_event_identity_path_or_sql(
    sink_db: ApiKeysDB,
) -> None:
    marker = "TOP-SECRET-EVENT"
    stored = _stored_charge(event_id=f"usage:v1:http:{marker}", api_key_id=999, cost=1.0)

    with pytest.raises(AccountingError) as error:
        sink_db.apply_spend_event(stored, APPLIED_AT)

    rendered = f"{error.value!s} {error.value!r}"
    assert error.value.code is AccountingErrorCode.ORPHAN_SINK
    assert marker not in rendered
    assert str(sink_db.db_path) not in rendered
    assert "SELECT" not in rendered
    assert "UPDATE" not in rendered


def test_sink_audit_pages_are_ordered_bounded_and_capture_current_owner_state(
    sink_db: ApiKeysDB,
) -> None:
    active = sink_db.create(name="audit-active")
    tombstone = sink_db.create(name="audit-tombstone")
    missing = sink_db.create(name="audit-missing")
    ambiguous = sink_db.create(name="audit-ambiguous")
    events = (
        ("audit-a-active", active.id),
        ("audit-b-tombstone", tombstone.id),
        ("audit-c-missing", missing.id),
        ("audit-d-ambiguous", ambiguous.id),
    )
    for event_id, api_key_id in reversed(events):
        stored = _stored_charge(
            event_id=event_id,
            api_key_id=api_key_id,
            cost=0.25,
        )
        assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED

    assert sink_db.delete(tombstone.id) is True
    _insert_tombstone(sink_db, api_key_id=tombstone.id, spent_usd=0.25)
    assert sink_db.delete(missing.id) is True
    _insert_tombstone(sink_db, api_key_id=ambiguous.id, spent_usd=0.25)

    rows = []
    cursor = None
    while True:
        page = sink_db.list_accounting_sink_audit_rows(
            limit=1,
            after_event_id=cursor,
        )
        if not page:
            break
        assert len(page) == 1
        rows.extend(page)
        cursor = page[-1].event_id

    assert [row.event_id for row in rows] == [event_id for event_id, _ in events]
    assert [row.owner_state for row in rows] == [
        AccountingOwnerState.ACTIVE,
        AccountingOwnerState.TOMBSTONE,
        AccountingOwnerState.MISSING,
        AccountingOwnerState.AMBIGUOUS,
    ]
    assert [row.sink_kind for row in rows] == ["active"] * 4


def test_sink_audit_preserves_historical_tombstone_sink_kind(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="audit-historical-tombstone")
    assert sink_db.delete(key.id) is True
    _insert_tombstone(sink_db, api_key_id=key.id)
    stored = _stored_charge(
        event_id="audit-tombstone-receipt",
        api_key_id=key.id,
        cost=0.5,
    )
    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED

    rows = sink_db.list_accounting_sink_audit_rows(limit=1)

    assert len(rows) == 1
    assert rows[0].sink_kind == "tombstone"
    assert rows[0].owner_state is AccountingOwnerState.TOMBSTONE


def test_owner_audit_batch_returns_all_states_in_input_order(
    sink_db: ApiKeysDB,
) -> None:
    active = sink_db.create(name="owner-active")
    tombstone = sink_db.create(name="owner-tombstone")
    ambiguous = sink_db.create(name="owner-ambiguous")
    assert sink_db.delete(tombstone.id) is True
    _insert_tombstone(sink_db, api_key_id=tombstone.id)
    _insert_tombstone(sink_db, api_key_id=ambiguous.id)
    missing_id = 999_999
    requested = (ambiguous.id, missing_id, active.id, tombstone.id)

    rows = sink_db.get_accounting_owner_states(api_key_ids=requested)

    assert tuple(row.api_key_id for row in rows) == requested
    assert tuple(row.owner_state for row in rows) == (
        AccountingOwnerState.AMBIGUOUS,
        AccountingOwnerState.MISSING,
        AccountingOwnerState.ACTIVE,
        AccountingOwnerState.TOMBSTONE,
    )
    assert sink_db.get_accounting_owner_states(api_key_ids=()) == ()


@pytest.mark.parametrize("limit", [0, -1, True, 1.0, ACCOUNTING_AUDIT_MAX_PAGE_SIZE + 1])
def test_sink_audit_rejects_invalid_page_limit(
    sink_db: ApiKeysDB,
    limit: object,
) -> None:
    with pytest.raises(AccountingError) as error:
        sink_db.list_accounting_sink_audit_rows(limit=limit)  # type: ignore[arg-type]

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


@pytest.mark.parametrize(
    "cursor",
    ["", " trailing ", "contains\nnewline", "кириллица", "x" * 256, 7],
)
def test_sink_audit_rejects_invalid_cursor(
    sink_db: ApiKeysDB,
    cursor: object,
) -> None:
    with pytest.raises(AccountingError) as error:
        sink_db.list_accounting_sink_audit_rows(
            limit=1,
            after_event_id=cursor,  # type: ignore[arg-type]
        )

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_owner_audit_enforces_exact_bounded_unique_ids(
    sink_db: ApiKeysDB,
) -> None:
    maximum = tuple(range(10_000, 10_000 + ACCOUNTING_AUDIT_MAX_PAGE_SIZE))

    rows = sink_db.get_accounting_owner_states(api_key_ids=maximum)

    assert len(rows) == ACCOUNTING_AUDIT_MAX_PAGE_SIZE
    assert all(row.owner_state is AccountingOwnerState.MISSING for row in rows)
    assert sink_db.get_accounting_owner_states(
        api_key_ids=((1 << 63) - 1,)
    )[0].owner_state is AccountingOwnerState.MISSING
    invalid_values: tuple[object, ...] = (
        [],
        (0,),
        (True,),
        (1 << 63,),
        (1, 1),
        maximum + (20_000,),
    )
    for invalid in invalid_values:
        with pytest.raises(AccountingError) as error:
            sink_db.get_accounting_owner_states(
                api_key_ids=invalid,  # type: ignore[arg-type]
            )
        assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_sink_audit_rejects_corrupt_receipt_with_credential_safe_error(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="audit-corrupt")
    stored = _stored_charge(
        event_id="audit-corrupt-receipt",
        api_key_id=key.id,
        cost=0.5,
    )
    assert sink_db.apply_spend_event(stored, APPLIED_AT) is ProjectionStatus.APPLIED
    marker = "TOP-SECRET-NONCANONICAL-TIMESTAMP"
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            "UPDATE applied_accounting_events SET applied_at = ? WHERE event_id = ?",
            (marker, stored.event.event_id),
        )

    with pytest.raises(AccountingError) as error:
        sink_db.list_accounting_sink_audit_rows(limit=1)

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.code is AccountingErrorCode.SCHEMA_MISMATCH
    assert marker not in rendered
    assert str(sink_db.db_path) not in rendered
    assert "SELECT" not in rendered


def test_sink_audit_maps_ordinary_open_failure_and_preserves_terminal_identity(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_open(*_args, **_kwargs):
        raise RuntimeError("TOP-SECRET-OPEN-FAILURE")

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        fail_open,
    )
    with pytest.raises(AccountingError) as error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    assert error.value.code is AccountingErrorCode.RECONCILE_FAILED
    assert "TOP-SECRET" not in "".join(traceback.format_exception(error.value))

    terminal = BaseException("terminal-sensitive-detail")

    def raise_terminal(*_args, **_kwargs):
        raise terminal

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        raise_terminal,
    )
    with pytest.raises(BaseException) as terminal_error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    assert terminal_error.value is terminal


def test_sink_audit_close_arbitration_preserves_terminal_priority(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = accounting_schema.open_accounting_runtime_connection

    def install_faults(
        *,
        execute_error: BaseException | None = None,
        close_error: BaseException | None = None,
    ) -> None:
        def open_probe(path, *, enable_foreign_keys: bool):
            return _FaultingConnection(
                real_open(path, enable_foreign_keys=enable_foreign_keys),
                execute_error=execute_error,
                close_error=close_error,
            )

        monkeypatch.setattr(
            accounting_schema,
            "open_accounting_runtime_connection",
            open_probe,
        )

    terminal_primary = BaseException("terminal-primary-sensitive-detail")
    install_faults(
        execute_error=terminal_primary,
        close_error=RuntimeError("ordinary-close-sensitive-detail"),
    )
    with pytest.raises(BaseException) as primary_error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    assert primary_error.value is terminal_primary

    terminal_close = BaseException("terminal-close-sensitive-detail")
    install_faults(
        execute_error=RuntimeError("ordinary-primary-sensitive-detail"),
        close_error=terminal_close,
    )
    with pytest.raises(BaseException) as close_error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    assert close_error.value is terminal_close

    install_faults(close_error=RuntimeError("ordinary-close-sensitive-detail"))
    with pytest.raises(AccountingError) as ordinary_close_error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    assert ordinary_close_error.value.code is AccountingErrorCode.RECONCILE_FAILED
    assert "sensitive" not in "".join(
        traceback.format_exception(ordinary_close_error.value)
    )

    install_faults(
        execute_error=RuntimeError("ordinary-primary-sensitive-detail"),
        close_error=RuntimeError("ordinary-close-sensitive-detail"),
    )
    with pytest.raises(AccountingError) as ordinary_primary_error:
        sink_db.list_accounting_sink_audit_rows(limit=1)
    rendered = "".join(traceback.format_exception(ordinary_primary_error.value))
    assert ordinary_primary_error.value.code is AccountingErrorCode.RECONCILE_FAILED
    assert "ordinary-primary-sensitive-detail" not in rendered
    assert "ordinary-close-sensitive-detail" not in rendered


def _set_key_accounting_state(
    db: ApiKeysDB,
    *,
    key_id: int,
    spent_usd: float,
    budget_reset_at: str | None,
    last_used_at: str | None = None,
) -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            """
            UPDATE api_keys
            SET spent_usd = ?, budget_reset_at = ?, last_used_at = ?
            WHERE id = ?
            """,
            (spent_usd, budget_reset_at, last_used_at, key_id),
        )


def test_accounting_update_and_manual_reset_are_one_atomic_patch(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(
        name="before",
        budget_usd=10.0,
        rpm=10,
        metadata={"tier": "old"},
        budget_period="daily",
    )
    boundary = "2026-07-15T00:00:00+00:00"
    _set_key_accounting_state(
        sink_db,
        key_id=key.id,
        spent_usd=7.5,
        budget_reset_at=boundary,
    )

    updated = sink_db.update_accounting_key(
        key.id,
        changes={
            "name": "after",
            "budget_usd": 12.0,
            "rpm": 0,
            "metadata": {"tier": "new"},
        },
        reset_spent=True,
    )

    assert updated is not None
    assert updated.name == "after"
    assert updated.api_key == key.api_key
    assert updated.budget_usd == pytest.approx(12.0)
    assert updated.rpm is None
    assert updated.metadata == {"tier": "new"}
    assert updated.spent_usd == 0.0
    assert updated.budget_reset_at == boundary


def test_accounting_update_failure_rolls_back_fields_and_spend(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="before", budget_usd=10.0)
    _set_key_accounting_state(
        sink_db,
        key_id=key.id,
        spent_usd=7.5,
        budget_reset_at=None,
    )
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_accounting_key_update
            BEFORE UPDATE OF name ON api_keys
            BEGIN
                SELECT RAISE(ABORT, 'injected accounting update failure');
            END
            """
        )

    with pytest.raises(AccountingError) as error:
        sink_db.update_accounting_key(
            key.id,
            changes={"name": "after", "budget_usd": 12.0},
            reset_spent=True,
        )

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    fresh = sink_db.get_by_id(key.id)
    assert fresh is not None
    assert fresh.name == "before"
    assert fresh.budget_usd == pytest.approx(10.0)
    assert fresh.spent_usd == pytest.approx(7.5)


def test_manual_accounting_reset_preserves_existing_period_boundary(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="manual-reset", budget_period="daily")
    boundary = "2026-07-15T00:00:00+00:00"
    _set_key_accounting_state(
        sink_db,
        key_id=key.id,
        spent_usd=4.0,
        budget_reset_at=boundary,
    )

    reset = sink_db.reset_accounting_spend(key.id)

    assert reset is not None
    assert reset.spent_usd == 0.0
    assert reset.budget_reset_at == boundary
    assert sink_db.reset_accounting_spend(key.id + 10_000) is None


def test_due_key_pages_are_bounded_and_reset_rechecks_exact_eligibility(
    sink_db: ApiKeysDB,
) -> None:
    now = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
    due_one = sink_db.create(name="due-one", budget_period="daily")
    due_two = sink_db.create(name="due-two", budget_period="monthly")
    future = sink_db.create(name="future", budget_period="daily")
    cumulative = sink_db.create(name="cumulative")
    for key, spent, boundary in (
        (due_one, 3.0, "2026-07-14T00:00:00+00:00"),
        (due_two, 4.0, "2026-07-01T00:00:00+00:00"),
        (future, 5.0, "2026-07-15T00:00:00+00:00"),
        (cumulative, 6.0, "2026-07-01T00:00:00+00:00"),
    ):
        _set_key_accounting_state(
            sink_db,
            key_id=key.id,
            spent_usd=spent,
            budget_reset_at=boundary,
        )

    first_page = sink_db.list_due_budget_key_ids(now=now, limit=1)
    second_page = sink_db.list_due_budget_key_ids(
        now=now,
        limit=1,
        after_key_id=first_page[-1],
    )
    assert first_page == (due_one.id,)
    assert second_page == (due_two.id,)

    _set_key_accounting_state(
        sink_db,
        key_id=due_two.id,
        spent_usd=4.0,
        budget_reset_at="2026-08-01T00:00:00+00:00",
    )
    reset = sink_db.reset_due_accounting_budgets(
        now=now,
        eligible_key_ids=(due_one.id, due_two.id),
    )

    assert tuple(record.id for record in reset) == (due_one.id,)
    assert reset[0].spent_usd == 0.0
    assert reset[0].budget_reset_at == "2026-07-15T00:00:00+00:00"
    assert sink_db.get_by_id(due_two.id).spent_usd == pytest.approx(4.0)


@pytest.mark.parametrize("limit", [False, 0, ACCOUNTING_AUDIT_MAX_PAGE_SIZE + 1])
def test_due_key_lookup_rejects_unbounded_limits(
    sink_db: ApiKeysDB,
    limit: object,
) -> None:
    with pytest.raises(AccountingValidationError):
        sink_db.list_due_budget_key_ids(
            now=OCCURRED_AT,
            limit=limit,  # type: ignore[arg-type]
        )


def test_due_reset_rejects_duplicate_or_unbounded_key_batches(
    sink_db: ApiKeysDB,
) -> None:
    with pytest.raises(AccountingValidationError):
        sink_db.reset_due_accounting_budgets(
            now=OCCURRED_AT,
            eligible_key_ids=(1, 1),
        )
    with pytest.raises(AccountingValidationError):
        sink_db.reset_due_accounting_budgets(
            now=OCCURRED_AT,
            eligible_key_ids=tuple(range(1, ACCOUNTING_AUDIT_MAX_PAGE_SIZE + 2)),
        )


def test_delete_moves_only_accounting_state_and_late_spend_to_tombstone(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="delete", metadata={"secret-adjacent": "value"})
    last_used_at = "2026-07-14T11:59:00+00:00"
    _set_key_accounting_state(
        sink_db,
        key_id=key.id,
        spent_usd=3.0,
        budget_reset_at=None,
        last_used_at=last_used_at,
    )
    deleted_at = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)

    assert sink_db.delete_to_accounting_tombstone(key.id, deleted_at=deleted_at) is True
    assert sink_db.get_by_id(key.id) is None
    assert sink_db.get_by_key(key.api_key) is None
    with sqlite3.connect(sink_db.db_path) as conn:
        tombstone = conn.execute(
            """
            SELECT api_key_id, deleted_at, spent_usd, last_used_at
            FROM api_key_accounting_tombstones
            WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()
    assert tombstone == (key.id, deleted_at.isoformat(), 3.0, last_used_at)

    late = _stored_charge(
        event_id="usage:v1:http:late-after-delete",
        api_key_id=key.id,
        cost=0.5,
        occurred_at=deleted_at + timedelta(seconds=1),
    )
    assert (
        sink_db.apply_spend_event(late, deleted_at + timedelta(seconds=2))
        is ProjectionStatus.APPLIED
    )
    with sqlite3.connect(sink_db.db_path) as conn:
        spend = conn.execute(
            """
            SELECT spent_usd FROM api_key_accounting_tombstones
            WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()[0]
    assert spend == pytest.approx(3.5)
    assert sink_db.delete_to_accounting_tombstone(key.id, deleted_at=deleted_at) is False


def test_delete_fails_closed_on_owner_collision_without_mutation(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="collision")
    _insert_tombstone(sink_db, api_key_id=key.id, spent_usd=8.0)

    with pytest.raises(AccountingError) as error:
        sink_db.delete_to_accounting_tombstone(key.id, deleted_at=OCCURRED_AT)

    assert error.value.code is AccountingErrorCode.ORPHAN_SINK
    assert sink_db.get_by_id(key.id) is not None
    with sqlite3.connect(sink_db.db_path) as conn:
        spend = conn.execute(
            """
            SELECT spent_usd FROM api_key_accounting_tombstones
            WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()[0]
    assert spend == pytest.approx(8.0)


def test_delete_failure_rolls_back_tombstone_and_keeps_credential(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(name="delete-failure")
    with sqlite3.connect(sink_db.db_path) as conn:
        conn.execute(
            """
            CREATE TRIGGER reject_accounting_key_delete
            BEFORE DELETE ON api_keys
            BEGIN
                SELECT RAISE(ABORT, 'injected accounting delete failure');
            END
            """
        )

    with pytest.raises(AccountingError) as error:
        sink_db.delete_to_accounting_tombstone(key.id, deleted_at=OCCURRED_AT)

    assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert sink_db.get_by_key(key.api_key) is not None
    with sqlite3.connect(sink_db.db_path) as conn:
        tombstone = conn.execute(
            """
            SELECT 1 FROM api_key_accounting_tombstones WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()
    assert tombstone is None


def test_accounting_key_transaction_preserves_terminal_cleanup_priority(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_open = accounting_schema.open_accounting_runtime_connection
    terminal = BaseException("terminal-accounting-key-write-sensitive-detail")

    def open_probe(path, *, enable_foreign_keys: bool):
        return _FaultingConnection(
            real_open(path, enable_foreign_keys=enable_foreign_keys),
            execute_error=terminal,
            close_error=RuntimeError("ordinary-close-sensitive-detail"),
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(BaseException) as error:
        sink_db.reset_accounting_spend(1)
    assert error.value is terminal


@pytest.mark.parametrize(
    ("changes", "reset_spent"),
    [
        ({"unknown": 1}, False),
        ({"name": 1}, False),
        ({"name": "   "}, False),
        ({"budget_usd": "1"}, False),
        ({"rpm": True}, False),
        ({"tpm": 1.5}, False),
        ({"allowed_models": ("model",)}, False),
        ({"allowed_models": [1]}, False),
        ({"disabled": 1}, False),
        ({"metadata": []}, False),
        ({"budget_period": 1}, False),
        ({}, 1),
    ],
)
def test_accounting_update_rejects_invalid_snapshot_without_mutation(
    sink_db: ApiKeysDB,
    changes: object,
    reset_spent: object,
) -> None:
    key = sink_db.create(name="unchanged", budget_usd=5.0)

    with pytest.raises(AccountingValidationError) as error:
        sink_db.update_accounting_key(
            key.id,
            changes=changes,  # type: ignore[arg-type]
            reset_spent=reset_spent,  # type: ignore[arg-type]
        )

    assert error.value.__context__ is None
    assert sink_db.get_by_id(key.id) == key


def test_accounting_update_preserves_legacy_none_and_nonpositive_semantics(
    sink_db: ApiKeysDB,
) -> None:
    key = sink_db.create(
        name="legacy-semantics",
        budget_usd=5.0,
        rpm=10,
        tpm=20,
        allowed_models=["gateway/model"],
        metadata={"old": True},
        budget_period="daily",
    )
    key = sink_db.update(key.id, disabled=True)
    assert key is not None

    updated = sink_db.update_accounting_key(
        key.id,
        changes={
            "name": None,
            "budget_usd": None,
            "rpm": 0,
            "tpm": -1,
            "allowed_models": None,
            "disabled": None,
            "metadata": None,
            "budget_period": None,
        },
    )

    assert updated is not None
    assert updated.name == key.name
    assert updated.budget_usd is None
    assert updated.rpm is None
    assert updated.tpm is None
    assert updated.allowed_models == key.allowed_models
    assert updated.disabled is True
    assert updated.metadata == {}
    assert updated.budget_period == "none"
    assert updated.budget_reset_at is None


def _install_commit_then_raise(
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[list[str], list[int]]:
    real_open = accounting_schema.open_accounting_runtime_connection
    lifecycle: list[str] = []
    commit_calls: list[int] = []
    open_count = 0

    class TrackingConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self.inner.row_factory = value

        def close(self) -> None:
            lifecycle.append("close")
            self.inner.close()

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return TrackingConnection(inner) if open_count == 1 else inner

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        commit_calls.append(1)
        conn.commit()
        raise RuntimeError("ambiguous-accounting-key-commit-sensitive")

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )
    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_then_raise,
    )
    return lifecycle, commit_calls


def test_accounting_update_recovers_exact_result_after_commit_then_raise(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="before")
    lifecycle, commit_calls = _install_commit_then_raise(monkeypatch)

    updated = sink_db.update_accounting_key(
        key.id,
        changes={"name": "after", "rpm": 0},
        reset_spent=True,
    )

    assert updated is not None
    assert updated.name == "after"
    assert updated.rpm is None
    assert updated.spent_usd == 0.0
    assert commit_calls == [1]
    assert lifecycle == ["close"]


def test_accounting_delete_recovers_exact_tombstone_after_commit_then_raise(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="delete-ambiguous")
    lifecycle, commit_calls = _install_commit_then_raise(monkeypatch)

    assert sink_db.delete_to_accounting_tombstone(key.id, deleted_at=OCCURRED_AT)

    assert sink_db.get_by_id(key.id) is None
    with sqlite3.connect(sink_db.db_path) as conn:
        count = conn.execute(
            """
            SELECT COUNT(*) FROM api_key_accounting_tombstones
            WHERE api_key_id = ?
            """,
            (key.id,),
        ).fetchone()[0]
    assert count == 1
    assert commit_calls == [1]
    assert lifecycle == ["close"]


def test_due_reset_recovers_exact_batch_after_commit_then_raise(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="due-ambiguous", budget_period="daily")
    _set_key_accounting_state(
        sink_db,
        key_id=key.id,
        spent_usd=2.0,
        budget_reset_at="2026-07-13T00:00:00+00:00",
    )
    lifecycle, commit_calls = _install_commit_then_raise(monkeypatch)

    reset = sink_db.reset_due_accounting_budgets(
        now=OCCURRED_AT,
        eligible_key_ids=(key.id,),
    )

    assert tuple(record.id for record in reset) == (key.id,)
    assert reset[0].spent_usd == 0.0
    assert reset[0].budget_reset_at == "2026-07-14T00:00:00+00:00"
    assert commit_calls == [1]
    assert lifecycle == ["close"]


def test_accounting_update_recovers_after_ordinary_close_ambiguity(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    key = sink_db.create(name="close-before")
    real_open = accounting_schema.open_accounting_runtime_connection
    open_count = 0

    class CloseThenRaiseConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self.inner.row_factory = value

        def close(self) -> None:
            self.inner.close()
            raise RuntimeError("ambiguous-close-sensitive")

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return CloseThenRaiseConnection(inner) if open_count == 1 else inner

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    updated = sink_db.update_accounting_key(
        key.id,
        changes={"name": "close-after"},
    )

    assert updated is not None
    assert updated.name == "close-after"


def test_due_collision_rolls_back_earlier_reset_even_when_collision_is_not_due(
    sink_db: ApiKeysDB,
) -> None:
    due = sink_db.create(name="rollback-due", budget_period="daily")
    collision = sink_db.create(name="collision-future", budget_period="daily")
    due_boundary = "2026-07-13T00:00:00+00:00"
    _set_key_accounting_state(
        sink_db,
        key_id=due.id,
        spent_usd=3.0,
        budget_reset_at=due_boundary,
    )
    _set_key_accounting_state(
        sink_db,
        key_id=collision.id,
        spent_usd=4.0,
        budget_reset_at="2026-07-15T00:00:00+00:00",
    )
    _insert_tombstone(sink_db, api_key_id=collision.id, spent_usd=4.0)

    with pytest.raises(AccountingError) as error:
        sink_db.reset_due_accounting_budgets(
            now=OCCURRED_AT,
            eligible_key_ids=(due.id, collision.id),
        )

    assert error.value.code is AccountingErrorCode.ORPHAN_SINK
    fresh_due = sink_db.get_by_id(due.id)
    assert fresh_due is not None
    assert fresh_due.spent_usd == pytest.approx(3.0)
    assert fresh_due.budget_reset_at == due_boundary


@pytest.mark.parametrize("operation", ["update", "delete", "due"])
@pytest.mark.parametrize("terminal_close", [False, True])
def test_exact_recovery_close_arbitration_preserves_result_or_terminal(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    operation: str,
    terminal_close: bool,
) -> None:
    key = sink_db.create(
        name=f"recovery-close-{operation}",
        budget_period="daily" if operation == "due" else "none",
    )
    if operation == "due":
        _set_key_accounting_state(
            sink_db,
            key_id=key.id,
            spent_usd=2.0,
            budget_reset_at="2026-07-13T00:00:00+00:00",
        )
    terminal = BaseException(f"terminal-recovery-close-{operation}")
    close_error: BaseException = (
        terminal if terminal_close else RuntimeError("ordinary-recovery-close-sensitive")
    )
    real_open = accounting_schema.open_accounting_runtime_connection
    open_count = 0

    class RecoveryCloseConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            self.inner.row_factory = value

        def close(self) -> None:
            self.inner.close()
            raise close_error

    def open_probe(path, *, enable_foreign_keys: bool):
        nonlocal open_count
        open_count += 1
        inner = real_open(path, enable_foreign_keys=enable_foreign_keys)
        return RecoveryCloseConnection(inner) if open_count == 2 else inner

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        conn.commit()
        raise RuntimeError("ambiguous-commit-sensitive")

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )
    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_then_raise,
    )

    def invoke():
        if operation == "update":
            return sink_db.update_accounting_key(
                key.id,
                changes={"name": "recovered-update"},
            )
        if operation == "delete":
            return sink_db.delete_to_accounting_tombstone(
                key.id,
                deleted_at=OCCURRED_AT,
            )
        return sink_db.reset_due_accounting_budgets(
            now=OCCURRED_AT,
            eligible_key_ids=(key.id,),
        )

    if terminal_close:
        with pytest.raises(BaseException) as error:
            invoke()
        assert error.value is terminal
    else:
        result = invoke()
        if operation == "update":
            assert result.name == "recovered-update"
        elif operation == "delete":
            assert result is True
        else:
            assert tuple(record.id for record in result) == (key.id,)

    if operation == "update":
        assert sink_db.get_by_id(key.id).name == "recovered-update"
    elif operation == "delete":
        assert sink_db.get_by_id(key.id) is None
    else:
        assert sink_db.get_by_id(key.id).spent_usd == 0.0


@pytest.mark.parametrize("terminal_setter", [False, True])
@pytest.mark.parametrize("terminal_close", [False, True])
def test_due_list_row_factory_failure_preserves_cleanup_priority(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    terminal_setter: bool,
    terminal_close: bool,
) -> None:
    setter_terminal = BaseException("terminal-row-factory-setter")
    close_terminal = BaseException("terminal-row-factory-close")
    setter_error: BaseException = (
        setter_terminal
        if terminal_setter
        else RuntimeError("ordinary-row-factory-setter-sensitive")
    )
    close_error: BaseException = (
        close_terminal
        if terminal_close
        else RuntimeError("ordinary-row-factory-close-sensitive")
    )
    real_open = accounting_schema.open_accounting_runtime_connection

    class SetterFailureConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            raise setter_error

        def close(self) -> None:
            self.inner.close()
            raise close_error

    def open_probe(path, *, enable_foreign_keys: bool):
        return SetterFailureConnection(
            real_open(path, enable_foreign_keys=enable_foreign_keys)
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(BaseException) as error:
        sink_db.list_due_budget_key_ids(now=OCCURRED_AT, limit=1)

    if terminal_setter:
        assert error.value is setter_terminal
    elif terminal_close:
        assert error.value is close_terminal
    else:
        assert isinstance(error.value, AccountingError)
        assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
        rendered = "".join(traceback.format_exception(error.value))
        assert "sensitive" not in rendered


@pytest.mark.parametrize("terminal_setter", [False, True])
@pytest.mark.parametrize("terminal_close", [False, True])
def test_accounting_transaction_row_factory_failure_uses_cleanup_arbitration(
    sink_db: ApiKeysDB,
    monkeypatch: pytest.MonkeyPatch,
    terminal_setter: bool,
    terminal_close: bool,
) -> None:
    key = sink_db.create(name="transaction-setter-unchanged")
    setter_terminal = BaseException("terminal-transaction-setter")
    close_terminal = BaseException("terminal-transaction-close")
    setter_error: BaseException = (
        setter_terminal
        if terminal_setter
        else RuntimeError("ordinary-transaction-setter-sensitive")
    )
    close_error: BaseException = (
        close_terminal
        if terminal_close
        else RuntimeError("ordinary-transaction-close-sensitive")
    )
    close_calls: list[int] = []
    real_open = accounting_schema.open_accounting_runtime_connection

    class SetterFailureConnection:
        def __init__(self, inner: sqlite3.Connection) -> None:
            self.inner = inner

        def __getattr__(self, name: str):
            return getattr(self.inner, name)

        @property
        def row_factory(self):
            return self.inner.row_factory

        @row_factory.setter
        def row_factory(self, value) -> None:
            raise setter_error

        def close(self) -> None:
            close_calls.append(1)
            self.inner.close()
            raise close_error

    def open_probe(path, *, enable_foreign_keys: bool):
        return SetterFailureConnection(
            real_open(path, enable_foreign_keys=enable_foreign_keys)
        )

    monkeypatch.setattr(
        accounting_schema,
        "open_accounting_runtime_connection",
        open_probe,
    )

    with pytest.raises(BaseException) as error:
        sink_db.update_accounting_key(
            key.id,
            changes={"name": "must-not-commit"},
        )

    assert close_calls == [1]
    if terminal_setter:
        assert error.value is setter_terminal
    elif terminal_close:
        assert error.value is close_terminal
    else:
        assert isinstance(error.value, AccountingError)
        assert error.value.code is AccountingErrorCode.PROJECTION_WRITE_FAILED
        rendered = "".join(traceback.format_exception(error.value))
        assert "sensitive" not in rendered
    assert sink_db.get_by_id(key.id) == key
