from __future__ import annotations

import sqlite3
import time
import traceback
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from llm_gateway_core.db import accounting_schema, tokens_usage_db
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.services.accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingParentLinkState,
    AccountingSourceAuditKind,
    AccountingUsage,
    BillingComponent,
    BillingComponentKind,
    CostSource,
    SourceStatus,
    build_component_sum_usage,
)
from tests._async_compat import run_async


@pytest.fixture
def source_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TokensUsageDB:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return TokensUsageDB(db_filename="accounting-source.db")


def _usage(
    *,
    prompt_tokens: int = 3,
    completion_tokens: int = 2,
    cost: float = 0.25,
    duration_ms: int | None = 125,
    ttft_ms: int | None = None,
    cost_saved: float = 0.05,
    is_estimated: bool = False,
) -> AccountingUsage:
    return AccountingUsage(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        reasoning_tokens=1,
        cached_tokens=1,
        cost=cost,
        cost_saved=cost_saved,
        duration_ms=duration_ms,
        ttft_ms=ttft_ms,
        is_estimated=is_estimated,
    )


def _charge(
    event_id: str,
    *,
    usage: AccountingUsage | None = None,
    components: tuple[BillingComponent, ...] = (),
    cost_source: CostSource = CostSource.UPSTREAM,
    parent_event_id: str | None = None,
    occurred_at: datetime | None = None,
    request_id: str | None = None,
    provider: str = "provider-a",
    model: str = "model-a",
) -> AccountingEvent:
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=event_id,
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/chat",
        provider=provider,
        model=model,
        usage=usage or _usage(),
        cost_source=cost_source,
        occurred_at=occurred_at or datetime(2026, 7, 13, 10, 0, tzinfo=timezone.utc),
        request_id=request_id,
        parent_event_id=parent_event_id,
        components=components,
    )


def _rollup(event_id: str, children: tuple[AccountingEvent, ...]) -> AccountingEvent:
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
        occurred_at=datetime(2026, 7, 13, 10, 5, tzinfo=timezone.utc),
        child_event_ids=tuple(child.event_id for child in children),
        child_fingerprints=tuple(child.billing_fingerprint for child in children),
    )


def _table_counts(db: TokensUsageDB) -> tuple[int, int, int, int]:
    with sqlite3.connect(db.db_path) as conn:
        return tuple(
            conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
            for table in (
                "tokens_usage",
                "accounting_outbox",
                "accounting_event_components",
                "accounting_event_links",
            )
        )


def _insert_usage_only(
    db: TokensUsageDB,
    event_id: str,
    *,
    cost: float = 0.5,
    api_key_id: int | None = 7,
    parent_event_id: str | None = None,
) -> None:
    with sqlite3.connect(db.db_path) as conn:
        conn.execute(
            """
            INSERT INTO tokens_usage (
                timestamp, total_tokens, cost, api_key_id,
                accounting_event_id, accounting_kind,
                parent_accounting_event_id
            ) VALUES (?, 0, ?, ?, ?, 'charge', ?)
            """,
            (
                "2026-07-13T10:00:00+00:00",
                cost,
                api_key_id,
                event_id,
                parent_event_id,
            ),
        )


class _CloseFailingConnection:
    def __init__(self, inner: sqlite3.Connection, error: BaseException) -> None:
        self._inner = inner
        self._error = error

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def close(self) -> None:
        self._inner.close()
        raise self._error


class _RollbackFailingConnection:
    def __init__(self, inner: sqlite3.Connection, error: BaseException) -> None:
        self._inner = inner
        self._error = error

    def __getattr__(self, name: str) -> object:
        return getattr(self._inner, name)

    def rollback(self) -> None:
        self._inner.rollback()
        raise self._error


def _inject_source_close_failure(
    monkeypatch: pytest.MonkeyPatch,
    error: BaseException,
) -> None:
    real_open = tokens_usage_db._open_accounting_source_connection

    def open_with_failing_close(db_path: Path) -> _CloseFailingConnection:
        return _CloseFailingConnection(real_open(db_path), error)

    monkeypatch.setattr(
        tokens_usage_db,
        "_open_accounting_source_connection",
        open_with_failing_close,
    )


def test_accept_charge_persists_one_exact_reconstructable_source_transaction(
    source_db: TokensUsageDB,
) -> None:
    occurred_at = datetime(2026, 7, 13, 11, 12, 13, 456789, tzinfo=timezone.utc)
    event = _charge("event-charge", occurred_at=occurred_at, request_id="request-first")

    result = source_db.accept_accounting_event(event)
    stored = source_db.get_accounting_event(event.event_id)

    assert result.status is SourceStatus.ACCEPTED
    assert result.stored_event == stored
    assert stored is not None
    assert stored.event == event
    assert stored.event.occurred_at == occurred_at
    assert stored.billing_fingerprint == event.billing_fingerprint
    assert stored.projected_at is None
    assert stored.projection_attempts == 0
    assert _table_counts(source_db) == (1, 1, 0, 0)
    with sqlite3.connect(source_db.db_path) as conn:
        usage = conn.execute(
            """
            SELECT timestamp, accounting_event_id, accounting_kind,
                   parent_accounting_event_id, request_id
            FROM tokens_usage
            """
        ).fetchone()
        outbox = conn.execute("SELECT occurred_at, billing_fingerprint FROM accounting_outbox").fetchone()
    assert usage == (occurred_at.isoformat(), event.event_id, "charge", None, "request-first")
    assert outbox == (occurred_at.isoformat(), event.billing_fingerprint)


def test_accept_component_sum_persists_ordered_manifest(source_db: TokensUsageDB) -> None:
    components = (
        BillingComponent("provider-a", "model-a", _usage(cost=0.1), CostSource.UPSTREAM),
        BillingComponent(
            "provider-b",
            "model-b",
            _usage(prompt_tokens=5, completion_tokens=4, cost=0.2),
            CostSource.TOKEN_REGISTRY,
        ),
    )
    event = _charge(
        "event-components",
        components=components,
        usage=build_component_sum_usage(components, duration_ms=321),
        cost_source=CostSource.COMPONENT_SUM,
    )

    stored = source_db.accept_accounting_event(event).stored_event

    assert stored.billing_fingerprint == event.billing_fingerprint
    assert [component.billing_fingerprint for component in stored.event.components] == [
        component.billing_fingerprint for component in event.components
    ]
    with sqlite3.connect(source_db.db_path) as conn:
        manifest = conn.execute(
            """
            SELECT ordinal, provider, model, component_fingerprint
            FROM accounting_event_components
            ORDER BY ordinal
            """
        ).fetchall()
    assert manifest == [
        (0, "provider-a", "model-a", components[0].billing_fingerprint),
        (1, "provider-b", "model-b", components[1].billing_fingerprint),
    ]


def test_accept_mixed_component_manifest_round_trips_and_audits(
    source_db: TokensUsageDB,
) -> None:
    components = (
        BillingComponent(
            "provider-a",
            "model-a",
            _usage(cost=0.2),
            CostSource.TOKEN_REGISTRY,
        ),
        BillingComponent(
            None,
            None,
            AccountingUsage(cost=0.1),
            CostSource.OPERATION_DEFAULT,
            component_kind=BillingComponentKind.OPERATION,
            operation="web_search",
            gateway_model="gateway/search",
        ),
    )
    event = _charge(
        "event-mixed-components",
        components=components,
        usage=build_component_sum_usage(components, duration_ms=321),
        cost_source=CostSource.COMPONENT_SUM,
    )

    accepted = source_db.accept_accounting_event(event)
    duplicate = source_db.accept_accounting_event(event)
    stored = source_db.get_accounting_event(event.event_id)
    audit_rows = source_db.list_accounting_source_audit_rows(limit=10)

    assert accepted.status is SourceStatus.ACCEPTED
    assert duplicate.status is SourceStatus.DUPLICATE
    assert stored is not None
    assert tuple(
        component.billing_fingerprint for component in stored.event.components
    ) == tuple(component.billing_fingerprint for component in components)
    assert stored.event.components[1].component_kind is BillingComponentKind.OPERATION
    assert stored.event.components[1].operation == "web_search"
    assert stored.event.components[1].gateway_model == "gateway/search"
    assert [row.event_id for row in audit_rows] == [event.event_id]
    with sqlite3.connect(source_db.db_path) as conn:
        manifest = conn.execute(
            """
            SELECT ordinal, component_kind, provider, model, operation,
                   gateway_model, component_fingerprint
            FROM accounting_event_components
            ORDER BY ordinal
            """
        ).fetchall()
    assert manifest == [
        (
            0,
            "model",
            "provider-a",
            "model-a",
            None,
            None,
            components[0].billing_fingerprint,
        ),
        (
            1,
            "operation",
            None,
            None,
            "web_search",
            "gateway/search",
            components[1].billing_fingerprint,
        ),
    ]


def test_malformed_persisted_component_kind_is_fingerprint_conflict(
    source_db: TokensUsageDB,
) -> None:
    component = BillingComponent(
        "provider-a",
        "model-a",
        _usage(cost=0.25),
        CostSource.UPSTREAM,
    )
    event = _charge(
        "event-malformed-component-kind",
        components=(component,),
        usage=build_component_sum_usage((component,)),
        cost_source=CostSource.COMPONENT_SUM,
    )
    source_db.accept_accounting_event(event)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute("PRAGMA ignore_check_constraints=ON")
        conn.execute(
            """
            UPDATE accounting_event_components
            SET component_kind = 'unknown'
            WHERE event_id = ?
            """,
            (event.event_id,),
        )

    with pytest.raises(AccountingError) as exc_info:
        source_db.get_accounting_event(event.event_id)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_accept_rollup_validates_and_persists_ordered_child_links(
    source_db: TokensUsageDB,
) -> None:
    rollup_id = "event-rollup"
    children = (
        _charge("event-child-a", parent_event_id=rollup_id),
        _charge("event-child-b", parent_event_id=rollup_id, model="model-b"),
    )
    for child in children:
        source_db.accept_accounting_event(child)
    event = _rollup(rollup_id, children)

    stored = source_db.accept_accounting_event(event).stored_event

    assert stored.event == event
    with sqlite3.connect(source_db.db_path) as conn:
        links = conn.execute(
            """
            SELECT ordinal, child_event_id, child_billing_fingerprint
            FROM accounting_event_links
            WHERE parent_event_id = ?
            ORDER BY ordinal
            """,
            (rollup_id,),
        ).fetchall()
    assert links == [
        (0, children[0].event_id, children[0].billing_fingerprint),
        (1, children[1].event_id, children[1].billing_fingerprint),
    ]


@pytest.mark.parametrize(
    "helper_name",
    [
        "_insert_accounting_usage",
        "_insert_accounting_outbox",
        "_insert_accounting_manifests",
        "_read_stored_accounting_event",
    ],
)
def test_source_transaction_rolls_back_after_each_write_boundary(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
    helper_name: str,
) -> None:
    original = getattr(tokens_usage_db, helper_name)

    def fail_after_call(*args, **kwargs):
        original(*args, **kwargs)
        raise sqlite3.OperationalError("sensitive-boundary-detail")

    monkeypatch.setattr(tokens_usage_db, helper_name, fail_after_call)

    with pytest.raises(AccountingError) as exc_info:
        source_db.accept_accounting_event(_charge(f"event-fail-{helper_name}"))

    assert exc_info.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    assert str(exc_info.value) == "source_write_failed"
    assert _table_counts(source_db) == (0, 0, 0, 0)


def test_duplicate_returns_first_persisted_event_and_conflict_fails_closed(
    source_db: TokensUsageDB,
) -> None:
    original = _charge("event-duplicate", request_id="first-request")
    source_db.accept_accounting_event(original)
    diagnostic_retry = replace(
        original,
        occurred_at=original.occurred_at + timedelta(minutes=1),
        request_id="second-request",
        usage=replace(original.usage, duration_ms=999, cost_saved=8.0, is_estimated=True),
    )

    duplicate = source_db.accept_accounting_event(diagnostic_retry)

    assert duplicate.status is SourceStatus.DUPLICATE
    assert duplicate.stored_event.event == original
    assert _table_counts(source_db) == (1, 1, 0, 0)

    conflict = replace(original, model="other-model")
    with pytest.raises(AccountingError) as exc_info:
        source_db.accept_accounting_event(conflict)
    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    assert _table_counts(source_db) == (1, 1, 0, 0)


def test_rollup_duplicate_rejects_different_child_ids_with_same_fingerprints(
    source_db: TokensUsageDB,
) -> None:
    rollup_id = "event-rollup-duplicate"
    original_child = _charge("event-child-original", parent_event_id=rollup_id)
    replacement_child = _charge("event-child-replacement", parent_event_id=rollup_id)
    assert original_child.billing_fingerprint == replacement_child.billing_fingerprint
    source_db.accept_accounting_event(original_child)
    source_db.accept_accounting_event(replacement_child)
    original = _rollup(rollup_id, (original_child,))
    replacement = _rollup(rollup_id, (replacement_child,))
    assert original.billing_fingerprint == replacement.billing_fingerprint
    source_db.accept_accounting_event(original)

    with pytest.raises(AccountingError) as exc_info:
        source_db.accept_accounting_event(replacement)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    stored = source_db.get_accounting_event(rollup_id)
    assert stored is not None
    assert stored.event == original


def test_corrupt_persisted_event_is_safe_fingerprint_conflict(
    source_db: TokensUsageDB,
) -> None:
    event = _charge("event-corrupt")
    source_db.accept_accounting_event(event)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "UPDATE tokens_usage SET model = 'tampered-model' WHERE accounting_event_id = ?",
            (event.event_id,),
        )

    for operation in (
        lambda: source_db.get_accounting_event(event.event_id),
        lambda: source_db.accept_accounting_event(event),
    ):
        with pytest.raises(AccountingError) as exc_info:
            operation()
        assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
        assert str(exc_info.value) == "fingerprint_conflict"
        assert "tampered-model" not in repr(exc_info.value)


def test_corrupt_persisted_datetime_is_absent_from_formatted_traceback(
    source_db: TokensUsageDB,
) -> None:
    event = _charge("event-corrupt-datetime")
    source_db.accept_accounting_event(event)
    sensitive_value = "TOP_" + "SECRET_DB_VALUE"
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "UPDATE tokens_usage SET timestamp = ? WHERE accounting_event_id = ?",
            (sensitive_value, event.event_id),
        )
        conn.execute(
            "UPDATE accounting_outbox SET occurred_at = ? WHERE event_id = ?",
            (sensitive_value, event.event_id),
        )

    with pytest.raises(AccountingError) as error:
        source_db.get_accounting_event(event.event_id)

    assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    assert sensitive_value not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True


def test_corrupt_projection_metadata_fails_before_duplicate_classification(
    source_db: TokensUsageDB,
) -> None:
    event = _charge("event-corrupt-projection-metadata")
    source_db.accept_accounting_event(event)
    projected_at = datetime(2026, 7, 13, 12, 6, tzinfo=timezone.utc)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            """
            UPDATE accounting_outbox
            SET projected_at = ?, projection_attempts = 0,
                last_attempt_at = NULL, last_error_code = NULL
            WHERE event_id = ?
            """,
            (projected_at.isoformat(), event.event_id),
        )

    for operation in (
        lambda: source_db.accept_accounting_event(event),
        lambda: source_db.get_accounting_event(event.event_id),
    ):
        with pytest.raises(AccountingError) as error:
            operation()
        assert error.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_concurrent_same_event_accepts_once_and_duplicates_once(
    source_db: TokensUsageDB,
) -> None:
    event = _charge("event-concurrent-same")
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: source_db.accept_accounting_event(event), range(2)))

    assert sorted(result.status.value for result in results) == ["accepted", "duplicate"]
    assert _table_counts(source_db) == (1, 1, 0, 0)


def test_concurrent_different_fingerprints_accepts_one_and_conflicts_one(
    source_db: TokensUsageDB,
) -> None:
    events = (_charge("event-concurrent-different"), _charge("event-concurrent-different", model="model-b"))

    def accept(event: AccountingEvent) -> SourceStatus | AccountingErrorCode:
        try:
            return source_db.accept_accounting_event(event).status
        except AccountingError as exc:
            return exc.code

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(accept, events))

    assert SourceStatus.ACCEPTED in results
    assert AccountingErrorCode.FINGERPRINT_CONFLICT in results
    assert _table_counts(source_db) == (1, 1, 0, 0)


def test_held_write_lock_obeys_bounded_accounting_timeout(source_db: TokensUsageDB) -> None:
    blocker = accounting_schema.open_accounting_runtime_connection(
        source_db.db_path,
        enable_foreign_keys=True,
    )
    blocker.execute("BEGIN IMMEDIATE")
    started = time.monotonic()
    try:
        with pytest.raises(AccountingError) as exc_info:
            source_db.accept_accounting_event(_charge("event-locked"))
    finally:
        blocker.rollback()
        blocker.close()

    elapsed = time.monotonic() - started
    assert exc_info.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    assert 1.5 <= elapsed < 4.0
    assert _table_counts(source_db) == (0, 0, 0, 0)


def test_successful_source_close_failure_is_not_suppressed_by_caller_exception(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "success-close-sensitive-detail"
    _inject_source_close_failure(monkeypatch, RuntimeError(secret))

    try:
        raise ValueError("caller-exception-sensitive-detail")
    except ValueError:
        with pytest.raises(AccountingError) as error:
            source_db.get_accounting_event("event-missing-close-failure")

    assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    rendered = "".join(traceback.format_exception(error.value))
    assert secret not in rendered
    assert "caller-exception-sensitive-detail" not in rendered
    assert error.value.__suppress_context__ is True


def test_ordinary_source_failure_is_not_masked_by_close_failure(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary_secret = "ordinary-primary-sensitive-detail"
    close_secret = "ordinary-close-sensitive-detail"
    _inject_source_close_failure(monkeypatch, RuntimeError(close_secret))

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError(primary_secret)

    monkeypatch.setattr(tokens_usage_db, "_read_stored_accounting_event", fail_read)

    with pytest.raises(AccountingError) as error:
        source_db.get_accounting_event("event-ordinary-primary")

    rendered = "".join(traceback.format_exception(error.value))
    assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    assert primary_secret not in rendered
    assert close_secret not in rendered
    assert error.value.__suppress_context__ is True


def test_terminal_source_failure_identity_survives_close_failure(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    terminal = BaseException("terminal-primary-sensitive-detail")
    close_secret = "terminal-close-sensitive-detail"
    _inject_source_close_failure(monkeypatch, RuntimeError(close_secret))

    def fail_read(*_args: object, **_kwargs: object) -> None:
        raise terminal

    monkeypatch.setattr(tokens_usage_db, "_read_stored_accounting_event", fail_read)

    with pytest.raises(BaseException) as error:
        source_db.get_accounting_event("event-terminal-primary")

    assert error.value is terminal
    assert close_secret not in "".join(traceback.format_exception(error.value))


def test_source_audit_terminal_primary_survives_rollback_failure(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_db.accept_accounting_event(_charge("event-audit-terminal-rollback"))
    terminal = BaseException("terminal-audit-primary-sensitive-detail")
    rollback_secret = "audit-rollback-sensitive-detail"
    real_open = tokens_usage_db._open_accounting_source_connection

    def open_with_failing_rollback(db_path: Path) -> _RollbackFailingConnection:
        return _RollbackFailingConnection(
            real_open(db_path),
            RuntimeError(rollback_secret),
        )

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise terminal

    monkeypatch.setattr(
        tokens_usage_db,
        "_open_accounting_source_connection",
        open_with_failing_rollback,
    )
    monkeypatch.setattr(tokens_usage_db, "_read_accounting_audit_manifests", fail_audit)

    with pytest.raises(BaseException) as error:
        source_db.list_accounting_source_audit_rows(limit=1)

    assert error.value is terminal
    assert rollback_secret not in "".join(traceback.format_exception(error.value))


def test_source_audit_terminal_close_wins_over_ordinary_primary(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source_db.accept_accounting_event(_charge("event-audit-terminal-close"))
    terminal_close = BaseException("terminal-audit-close-sensitive-detail")
    _inject_source_close_failure(monkeypatch, terminal_close)

    def fail_audit(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("ordinary-audit-primary-sensitive-detail")

    monkeypatch.setattr(tokens_usage_db, "_read_accounting_audit_manifests", fail_audit)

    with pytest.raises(BaseException) as error:
        source_db.list_accounting_source_audit_rows(limit=1)

    assert error.value is terminal_close


def test_ambiguous_commit_recovers_exact_persisted_event(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    real_commit = accounting_schema.commit_accounting_transaction

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        real_commit(conn)
        raise sqlite3.OperationalError("ambiguous-sensitive-detail")

    monkeypatch.setattr(accounting_schema, "commit_accounting_transaction", commit_then_raise)

    result = source_db.accept_accounting_event(_charge("event-ambiguous"))

    assert result.status is SourceStatus.RECOVERED
    assert result.stored_event.event.event_id == "event-ambiguous"
    assert _table_counts(source_db) == (1, 1, 0, 0)


@pytest.mark.parametrize("scenario", ["acceptance", "projection"])
def test_ambiguous_commit_recovery_survives_primary_connection_close_failure(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
    scenario: str,
) -> None:
    event = _charge(f"event-ambiguous-close-{scenario}")
    if scenario == "projection":
        source_db.accept_accounting_event(event)

    real_open = tokens_usage_db._open_accounting_source_connection
    open_count = 0

    def open_with_first_close_failure(db_path: Path):
        nonlocal open_count
        open_count += 1
        conn = real_open(db_path)
        if open_count == 1:
            return _CloseFailingConnection(
                conn,
                RuntimeError("ambiguous-close-sensitive-detail"),
            )
        return conn

    real_commit = accounting_schema.commit_accounting_transaction

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        real_commit(conn)
        raise sqlite3.OperationalError("ambiguous-commit-sensitive-detail")

    monkeypatch.setattr(
        tokens_usage_db,
        "_open_accounting_source_connection",
        open_with_first_close_failure,
    )
    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_then_raise,
    )

    if scenario == "acceptance":
        result = source_db.accept_accounting_event(event)
        assert result.status is SourceStatus.RECOVERED
        assert result.stored_event.event == event
        assert _table_counts(source_db) == (1, 1, 0, 0)
    else:
        projected_at = datetime(2026, 7, 13, 12, 7, tzinfo=timezone.utc)
        mark = source_db.mark_accounting_event_projected(
            event.event_id,
            event.billing_fingerprint,
            projected_at,
        )
        assert mark.projected_at == projected_at
        assert mark.projection_attempts == 1
        assert source_db.list_pending_accounting_events(limit=10) == ()


def test_commit_failure_before_commit_is_typed_and_rolls_back(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_before_commit(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("pre-commit-sensitive-detail")

    monkeypatch.setattr(accounting_schema, "commit_accounting_transaction", fail_before_commit)

    with pytest.raises(AccountingError) as exc_info:
        source_db.accept_accounting_event(_charge("event-pre-commit-failure"))

    assert exc_info.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    assert str(exc_info.value) == "source_write_failed"
    assert _table_counts(source_db) == (0, 0, 0, 0)


def test_ambiguous_projection_ack_commit_recovers_exact_mark(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _charge("event-ambiguous-ack")
    source_db.accept_accounting_event(event)
    projected_at = datetime(2026, 7, 13, 12, 3, tzinfo=timezone.utc)
    real_commit = accounting_schema.commit_accounting_transaction

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        real_commit(conn)
        raise sqlite3.OperationalError("ambiguous-ack-sensitive-detail")

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_then_raise,
    )

    projected = source_db.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        projected_at,
    )

    assert projected.projected_at == projected_at
    assert projected.last_attempt_at == projected_at
    assert projected.last_error_code is None
    assert source_db.list_pending_accounting_events(limit=10) == ()


def test_ambiguous_projection_failure_commit_recovers_exact_mark(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _charge("event-ambiguous-failure-mark")
    source_db.accept_accounting_event(event)
    attempted_at = datetime(2026, 7, 13, 12, 4, tzinfo=timezone.utc)
    real_commit = accounting_schema.commit_accounting_transaction

    def commit_then_raise(conn: sqlite3.Connection) -> None:
        real_commit(conn)
        raise sqlite3.OperationalError("ambiguous-marker-sensitive-detail")

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_then_raise,
    )

    failed = source_db.mark_accounting_projection_failed(
        event.event_id,
        event.billing_fingerprint,
        AccountingErrorCode.PROJECTION_WRITE_FAILED,
        attempted_at,
    )

    assert failed.projected_at is None
    assert failed.last_attempt_at == attempted_at
    assert failed.last_error_code is AccountingErrorCode.PROJECTION_WRITE_FAILED
    assert [row.event.event_id for row in source_db.list_pending_accounting_events(limit=10)] == [event.event_id]


def test_projection_failure_precommit_error_is_not_misclassified_as_recovery(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _charge("event-failure-mark-precommit")
    source_db.accept_accounting_event(event)
    attempted_at = datetime(2026, 7, 13, 12, 5, tzinfo=timezone.utc)
    first = source_db.mark_accounting_projection_failed(
        event.event_id,
        event.billing_fingerprint,
        AccountingErrorCode.PROJECTION_WRITE_FAILED,
        attempted_at,
    )

    def fail_before_commit(_conn: sqlite3.Connection) -> None:
        raise sqlite3.OperationalError("precommit-marker-sensitive-detail")

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        fail_before_commit,
    )

    with pytest.raises(AccountingError) as error:
        source_db.mark_accounting_projection_failed(
            event.event_id,
            event.billing_fingerprint,
            AccountingErrorCode.PROJECTION_WRITE_FAILED,
            attempted_at,
        )

    assert error.value.code is AccountingErrorCode.SOURCE_WRITE_FAILED
    stored = source_db.get_accounting_event(event.event_id)
    assert stored is not None
    assert stored.projection_attempts == first.projection_attempts == 1


def test_pending_pagination_and_conditional_projection_marks_are_deterministic(
    source_db: TokensUsageDB,
) -> None:
    events = (_charge("event-c"), _charge("event-a"), _charge("event-b"))
    for event in events:
        source_db.accept_accounting_event(event)
    created_at = "2026-07-13T12:00:00+00:00"
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute("UPDATE accounting_outbox SET created_at = ?", (created_at,))

    first_page = source_db.list_pending_accounting_events(limit=2)
    second_page = source_db.list_pending_accounting_events(
        limit=2,
        after=(first_page[-1].created_at, first_page[-1].event.event_id),
    )

    assert [row.event.event_id for row in first_page] == ["event-a", "event-b"]
    assert [row.event.event_id for row in second_page] == ["event-c"]

    attempted_at = datetime(2026, 7, 13, 12, 1, tzinfo=timezone.utc)
    failed = source_db.mark_accounting_projection_failed(
        "event-a",
        events[1].billing_fingerprint,
        AccountingErrorCode.PROJECTION_WRITE_FAILED,
        attempted_at,
    )
    assert failed.projection_attempts == 1
    assert failed.last_attempt_at == attempted_at
    assert failed.last_error_code is AccountingErrorCode.PROJECTION_WRITE_FAILED

    projected_at = datetime(2026, 7, 13, 12, 2, tzinfo=timezone.utc)
    projected = source_db.mark_accounting_event_projected(
        "event-a",
        events[1].billing_fingerprint,
        projected_at,
    )
    assert projected.projected_at == projected_at
    assert projected.projection_attempts == 2
    assert projected.last_error_code is None
    assert [row.event.event_id for row in source_db.list_pending_accounting_events(limit=10)] == [
        "event-b",
        "event-c",
    ]

    repeated = source_db.mark_accounting_event_projected(
        "event-a",
        events[1].billing_fingerprint,
        projected_at + timedelta(minutes=1),
    )
    assert repeated == projected

    with pytest.raises(AccountingError) as exc_info:
        source_db.mark_accounting_event_projected(
            "event-b",
            "0" * 64,
            projected_at,
        )
    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_cleanup_removes_old_legacy_rows_but_retains_pending_accounting_source(
    source_db: TokensUsageDB,
) -> None:
    event = _charge(
        "event-old-pending",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    source_db.accept_accounting_event(event)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute("INSERT INTO tokens_usage (timestamp) VALUES ('2020-01-01T00:00:00+00:00')")

    source_db.cleanup_old_records(retention_days=1)

    with sqlite3.connect(source_db.db_path) as conn:
        rows = conn.execute("SELECT accounting_event_id FROM tokens_usage ORDER BY id").fetchall()
    assert rows == [(event.event_id,)]
    assert source_db.get_accounting_event(event.event_id) is not None


def test_exact_duplicate_survives_projected_usage_retention(
    source_db: TokensUsageDB,
) -> None:
    event = _charge(
        "event-old-projected",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
        request_id="first-request",
    )
    accepted = source_db.accept_accounting_event(event)
    source_db.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        datetime(2026, 7, 13, tzinfo=timezone.utc),
    )

    source_db.cleanup_old_records(retention_days=1)
    duplicate = source_db.accept_accounting_event(event)

    assert accepted.stored_event.usage_row_id is not None
    assert duplicate.status is SourceStatus.DUPLICATE
    assert duplicate.stored_event.usage_row_id is None
    assert duplicate.stored_event.event == event
    assert duplicate.stored_event.projected_at == datetime(2026, 7, 13, tzinfo=timezone.utc)
    with sqlite3.connect(source_db.db_path) as conn:
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM tokens_usage WHERE accounting_event_id = ?",
                (event.event_id,),
            ).fetchone()[0]
            == 0
        )
        assert (
            conn.execute(
                "SELECT COUNT(*) FROM accounting_outbox WHERE event_id = ?",
                (event.event_id,),
            ).fetchone()[0]
            == 1
        )


def test_repeated_projection_mark_survives_projected_usage_retention(
    source_db: TokensUsageDB,
) -> None:
    event = _charge(
        "event-retained-projection-mark",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    source_db.accept_accounting_event(event)
    projected_at = datetime(2026, 7, 13, 12, 30, tzinfo=timezone.utc)
    source_db.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        projected_at,
    )
    source_db.cleanup_old_records(retention_days=1)

    repeated = source_db.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        projected_at + timedelta(minutes=1),
    )

    assert repeated.event_id == event.event_id
    assert repeated.billing_fingerprint == event.billing_fingerprint
    assert repeated.projected_at == projected_at
    assert source_db.get_accounting_event(event.event_id) is None


def test_ambiguous_projection_recovery_survives_concurrent_usage_retention(
    source_db: TokensUsageDB,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    event = _charge(
        "event-retained-ambiguous-mark",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    source_db.accept_accounting_event(event)
    projected_at = datetime(2026, 7, 13, 12, 31, tzinfo=timezone.utc)
    real_commit = accounting_schema.commit_accounting_transaction

    def commit_cleanup_then_raise(conn: sqlite3.Connection) -> None:
        real_commit(conn)
        source_db.cleanup_old_records(retention_days=1)
        raise sqlite3.OperationalError("ambiguous-retention-sensitive-detail")

    monkeypatch.setattr(
        accounting_schema,
        "commit_accounting_transaction",
        commit_cleanup_then_raise,
    )

    recovered = source_db.mark_accounting_event_projected(
        event.event_id,
        event.billing_fingerprint,
        projected_at,
    )

    assert recovered.event_id == event.event_id
    assert recovered.billing_fingerprint == event.billing_fingerprint
    assert recovered.projected_at == projected_at
    assert source_db.get_accounting_event(event.event_id) is None


def test_rollup_missing_or_mismatched_child_is_conflict_without_partial_rows(
    source_db: TokensUsageDB,
) -> None:
    child = _charge("event-rollup-child", parent_event_id="event-rollup-invalid")
    missing_child_rollup = _rollup("event-rollup-invalid", (child,))

    with pytest.raises(AccountingError) as missing_exc:
        source_db.accept_accounting_event(missing_child_rollup)
    assert missing_exc.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    assert _table_counts(source_db) == (0, 0, 0, 0)

    source_db.accept_accounting_event(child)
    wrong_fingerprint = replace(
        missing_child_rollup,
        child_fingerprints=("0" * 64,),
    )
    with pytest.raises(AccountingError) as mismatch_exc:
        source_db.accept_accounting_event(wrong_fingerprint)
    assert mismatch_exc.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT
    assert _table_counts(source_db) == (1, 1, 0, 0)


def test_source_audit_page_merges_interleaved_ids_with_size_one_cursor(
    source_db: TokensUsageDB,
) -> None:
    _insert_usage_only(source_db, "event-a", cost=0.5)
    event_b = _charge("event-b")
    source_db.accept_accounting_event(event_b)
    _insert_usage_only(source_db, "event-c", cost=0.75)

    first = source_db.list_accounting_source_audit_rows(limit=1)
    second = source_db.list_accounting_source_audit_rows(
        limit=1,
        after_event_id=first[-1].event_id,
    )
    third = source_db.list_accounting_source_audit_rows(
        limit=1,
        after_event_id=second[-1].event_id,
    )
    exhausted = source_db.list_accounting_source_audit_rows(
        limit=1,
        after_event_id=third[-1].event_id,
    )

    assert [first[0].event_id, second[0].event_id, third[0].event_id] == [
        "event-a",
        "event-b",
        "event-c",
    ]
    assert first[0].row_kind is AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX
    assert first[0].billing_fingerprint is None
    assert first[0].spend_usd == 0.5
    assert first[0].usage_present is True
    assert first[0].parent_link_state is AccountingParentLinkState.NOT_APPLICABLE
    assert second[0].row_kind is AccountingSourceAuditKind.OUTBOX
    assert second[0].billing_fingerprint == event_b.billing_fingerprint
    assert second[0].spend_usd == event_b.usage.cost
    assert second[0].usage_present is True
    assert second[0].parent_link_state is AccountingParentLinkState.NOT_APPLICABLE
    assert exhausted == ()


def test_source_audit_query_bounds_each_indexed_stream_before_union(
    source_db: TokensUsageDB,
) -> None:
    sql = tokens_usage_db._SELECT_ACCOUNTING_SOURCE_AUDIT_PAGE_SQL
    with sqlite3.connect(source_db.db_path) as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN " + sql,
            {"after_event_id": "event-a", "limit": 1},
        ).fetchall()
    details = tuple(str(row[3]) for row in plan)

    assert sql.count("LIMIT :limit") == 3
    assert any(
        "SEARCH accounting_outbox" in detail and "(event_id>?)" in detail
        for detail in details
    )
    assert any(
        "SEARCH tokens_usage USING COVERING INDEX ux_tokens_usage_accounting_event"
        in detail
        and "(accounting_event_id>?)" in detail
        for detail in details
    )
    assert any(
        "SEARCH usage USING INDEX ux_tokens_usage_accounting_event" in detail
        and "(accounting_event_id=?)" in detail
        for detail in details
    )
    assert any(
        "SEARCH links" in detail and "(parent_event_id=?)" in detail
        for detail in details
    )
    assert not any("SCAN accounting_outbox" in detail for detail in details)
    assert not any("SCAN tokens_usage" in detail for detail in details)


@pytest.mark.parametrize(
    "limit",
    (0, -1, ACCOUNTING_AUDIT_MAX_PAGE_SIZE + 1, True, 1.5),
)
def test_source_audit_page_rejects_invalid_limit(
    source_db: TokensUsageDB,
    limit: object,
) -> None:
    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=limit)  # type: ignore[arg-type]

    assert exc_info.value.code is AccountingErrorCode.INVALID_CONTRACT


@pytest.mark.parametrize("cursor", ("", " event", "event ", "событие"))
def test_source_audit_page_rejects_invalid_cursor(
    source_db: TokensUsageDB,
    cursor: str,
) -> None:
    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(
            limit=ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
            after_event_id=cursor,
        )

    assert exc_info.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_source_audit_page_keeps_pending_and_projected_rows_without_usage(
    source_db: TokensUsageDB,
) -> None:
    projected = _charge(
        "event-a-projected",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    pending = _charge(
        "event-b-pending",
        occurred_at=datetime(2020, 1, 1, tzinfo=timezone.utc),
    )
    source_db.accept_accounting_event(projected)
    source_db.accept_accounting_event(pending)
    projected_at = datetime(2026, 7, 13, 14, 0, tzinfo=timezone.utc)
    source_db.mark_accounting_event_projected(
        projected.event_id,
        projected.billing_fingerprint,
        projected_at,
    )
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "DELETE FROM tokens_usage WHERE accounting_event_id IN (?, ?)",
            (projected.event_id, pending.event_id),
        )

    rows = source_db.list_accounting_source_audit_rows(limit=10)

    assert [(row.event_id, row.usage_present, row.projected_at) for row in rows] == [
        (projected.event_id, False, projected_at),
        (pending.event_id, False, None),
    ]


def test_source_audit_page_classifies_unrolled_and_linked_child(
    source_db: TokensUsageDB,
) -> None:
    parent_id = "event-parent"
    child = _charge("event-child", parent_event_id=parent_id)
    source_db.accept_accounting_event(child)

    unrolled = source_db.list_accounting_source_audit_rows(limit=10)

    assert unrolled[0].event_id == child.event_id
    assert unrolled[0].parent_link_state is AccountingParentLinkState.UNROLLED

    source_db.accept_accounting_event(_rollup(parent_id, (child,)))
    linked = {
        row.event_id: row
        for row in source_db.list_accounting_source_audit_rows(limit=10)
    }

    assert linked[child.event_id].parent_link_state is AccountingParentLinkState.LINKED
    assert linked[parent_id].parent_link_state is AccountingParentLinkState.NOT_APPLICABLE


def test_source_audit_page_rejects_non_rollup_parent(
    source_db: TokensUsageDB,
) -> None:
    parent_id = "event-parent-charge"
    source_db.accept_accounting_event(
        _charge("event-child-charge-parent", parent_event_id=parent_id)
    )
    source_db.accept_accounting_event(_charge(parent_id))

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


@pytest.mark.parametrize(
    "corrupt",
    (
        "usage_cost",
        "gateway_model",
        "route_template",
        "projection_timestamp",
        "link_fingerprint",
    ),
)
def test_source_audit_page_rejects_corrupt_source_identity(
    source_db: TokensUsageDB,
    corrupt: str,
) -> None:
    parent_id = "event-corrupt-parent"
    child = _charge("event-corrupt-child", parent_event_id=parent_id)
    source_db.accept_accounting_event(child)
    source_db.accept_accounting_event(_rollup(parent_id, (child,)))
    with sqlite3.connect(source_db.db_path) as conn:
        if corrupt == "usage_cost":
            conn.execute(
                "UPDATE tokens_usage SET cost = cost + 1 WHERE accounting_event_id = ?",
                (child.event_id,),
            )
        elif corrupt == "gateway_model":
            conn.execute(
                "UPDATE tokens_usage SET gateway_model = ? WHERE accounting_event_id = ?",
                ("gateway/tampered", child.event_id),
            )
        elif corrupt == "route_template":
            conn.execute(
                "UPDATE accounting_outbox SET route_template = ? WHERE event_id = ?",
                ("/v1/tampered", child.event_id),
            )
        elif corrupt == "projection_timestamp":
            conn.execute(
                """
                UPDATE accounting_outbox
                SET projected_at = 'not-a-timestamp', projection_attempts = 1,
                    last_attempt_at = 'not-a-timestamp', last_error_code = NULL
                WHERE event_id = ?
                """,
                (child.event_id,),
            )
        else:
            conn.execute(
                """
                UPDATE accounting_event_links
                SET child_billing_fingerprint = ?
                WHERE parent_event_id = ? AND child_event_id = ?
                """,
                ("0" * 64, parent_id, child.event_id),
            )

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_source_audit_page_rejects_self_consistent_component_manifest_tampering(
    source_db: TokensUsageDB,
) -> None:
    components = (
        BillingComponent(
            "provider-a",
            "model-a",
            _usage(cost=0.1),
            CostSource.UPSTREAM,
        ),
        BillingComponent(
            "provider-b",
            "model-b",
            _usage(cost=0.2),
            CostSource.UPSTREAM,
        ),
    )
    event = _charge(
        "event-audit-components",
        components=components,
        usage=build_component_sum_usage(components, duration_ms=321),
        cost_source=CostSource.COMPONENT_SUM,
    )
    source_db.accept_accounting_event(event)
    tampered = replace(components[0], provider="provider-tampered")
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            """
            UPDATE accounting_event_components
            SET provider = ?, component_fingerprint = ?
            WHERE event_id = ? AND ordinal = 0
            """,
            (tampered.provider, tampered.billing_fingerprint, event.event_id),
        )

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_source_audit_page_rejects_rollup_without_outgoing_manifest(
    source_db: TokensUsageDB,
) -> None:
    parent_id = "event-empty-rollup"
    child = _charge("event-empty-rollup-child", parent_event_id=parent_id)
    source_db.accept_accounting_event(child)
    source_db.accept_accounting_event(_rollup(parent_id, (child,)))
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "DELETE FROM accounting_event_links WHERE parent_event_id = ?",
            (parent_id,),
        )

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


@pytest.mark.parametrize(
    "corruption",
    ("ordinal_gap", "fingerprint", "missing_child", "child_parent", "child_key"),
)
def test_source_audit_page_rejects_corrupt_outgoing_rollup_manifest(
    source_db: TokensUsageDB,
    corruption: str,
) -> None:
    parent_id = "event-manifest-parent"
    child = _charge("event-manifest-child", parent_event_id=parent_id)
    source_db.accept_accounting_event(child)
    source_db.accept_accounting_event(_rollup(parent_id, (child,)))
    with sqlite3.connect(source_db.db_path) as conn:
        if corruption == "ordinal_gap":
            conn.execute(
                "UPDATE accounting_event_links SET ordinal = 1 WHERE parent_event_id = ?",
                (parent_id,),
            )
        elif corruption == "fingerprint":
            conn.execute(
                """
                UPDATE accounting_event_links
                SET child_billing_fingerprint = ?
                WHERE parent_event_id = ?
                """,
                ("0" * 64, parent_id),
            )
        elif corruption == "missing_child":
            conn.execute("PRAGMA foreign_keys=OFF")
            conn.execute(
                "DELETE FROM accounting_outbox WHERE event_id = ?",
                (child.event_id,),
            )
        elif corruption == "child_parent":
            conn.execute(
                "UPDATE accounting_outbox SET parent_event_id = ? WHERE event_id = ?",
                ("event-other-parent", child.event_id),
            )
            conn.execute(
                """
                UPDATE tokens_usage
                SET parent_accounting_event_id = ?
                WHERE accounting_event_id = ?
                """,
                ("event-other-parent", child.event_id),
            )
        else:
            conn.execute(
                "UPDATE accounting_outbox SET api_key_id = 8 WHERE event_id = ?",
                (child.event_id,),
            )
            conn.execute(
                "UPDATE tokens_usage SET api_key_id = 8 WHERE accounting_event_id = ?",
                (child.event_id,),
            )

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_source_audit_page_rejects_outgoing_links_from_charge(
    source_db: TokensUsageDB,
) -> None:
    parent = _charge("event-a-charge-parent")
    child = _charge("event-z-standalone-child")
    source_db.accept_accounting_event(parent)
    source_db.accept_accounting_event(child)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            """
            INSERT INTO accounting_event_links (
                parent_event_id, ordinal, child_event_id,
                child_billing_fingerprint
            ) VALUES (?, 0, ?, ?)
            """,
            (parent.event_id, child.event_id, child.billing_fingerprint),
        )

    with pytest.raises(AccountingError) as exc_info:
        source_db.list_accounting_source_audit_rows(limit=10)

    assert exc_info.value.code is AccountingErrorCode.FINGERPRINT_CONFLICT


def test_get_dashboard_usage_computes_nearest_rank_percentiles(
    source_db: TokensUsageDB,
) -> None:
    base = datetime(2026, 7, 14, 9, 0, tzinfo=timezone.utc)
    for offset, value in enumerate((100, 200, 300, 400, 500)):
        source_db.accept_accounting_event(
            _charge(
                f"event-ttft-{value}",
                usage=_usage(duration_ms=value, ttft_ms=value),
                occurred_at=base + timedelta(minutes=offset),
            )
        )
    source_db.accept_accounting_event(
        _charge(
            "event-ttft-null",
            usage=_usage(duration_ms=600, ttft_ms=None),
            occurred_at=base + timedelta(minutes=5),
        )
    )

    result = run_async(
        source_db.get_dashboard_usage(
            "day",
            base - timedelta(days=1),
            base + timedelta(days=1),
        )
    )
    summary = result["summary"]

    assert summary["requests"] == 6
    assert summary["avg_duration_ms"] == 350
    assert summary["max_duration_ms"] == 600
    assert summary["duration_p50_ms"] == 300
    assert summary["duration_p95_ms"] == 600
    assert summary["ttft_avg_ms"] == 300
    assert summary["ttft_max_ms"] == 500
    assert summary["ttft_p50_ms"] == 300
    assert summary["ttft_p95_ms"] == 500
