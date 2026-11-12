from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from tests.test_accounting_sink_repository import _stored_charge
from tests.test_accounting_source_repository import (
    _charge,
    _insert_usage_only,
)


@pytest.fixture
def source_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return TokensUsageDB(db_filename="partition-source.db")


@pytest.fixture
def sink_db(tmp_path, monkeypatch):
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return ApiKeysDB(db_filename="partition-sink.db")


def test_source_ids_use_created_at_cutoff_and_selected_rows(source_db) -> None:
    boundary = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    old = _charge("old-source", occurred_at=boundary - timedelta(days=2))
    new = _charge("new-source", occurred_at=boundary)
    source_db.accept_accounting_event(old)
    source_db.accept_accounting_event(new)
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "UPDATE accounting_outbox SET created_at = ? WHERE event_id = ?",
            ((boundary - timedelta(days=1)).isoformat(), old.event_id),
        )

    assert source_db.list_accounting_source_audit_event_ids(
        limit=10,
        since=boundary,
    ) == (new.event_id,)
    assert tuple(
        row.event_id
        for row in source_db.list_accounting_source_audit_rows(
            limit=1,
            event_ids=(new.event_id, "missing-source"),
        )
    ) == (new.event_id,)


def test_source_usage_only_id_is_filtered_by_usage_timestamp(source_db) -> None:
    boundary = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    _insert_usage_only(source_db, "usage-only")
    with sqlite3.connect(source_db.db_path) as conn:
        conn.execute(
            "UPDATE tokens_usage SET timestamp = ? WHERE accounting_event_id = ?",
            ((boundary + timedelta(seconds=1)).isoformat(), "usage-only"),
        )

    assert source_db.list_accounting_source_audit_event_ids(
        limit=10,
        since=boundary,
    ) == ("usage-only",)


def test_sink_ids_use_applied_at_cutoff_and_selected_rows(sink_db) -> None:
    key = sink_db.create(name="partition")
    boundary = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    old = _stored_charge(event_id="old-receipt", api_key_id=key.id, cost=0.1)
    new = _stored_charge(event_id="new-receipt", api_key_id=key.id, cost=0.2)
    sink_db.apply_spend_event(old, boundary - timedelta(days=1))
    sink_db.apply_spend_event(new, boundary)

    assert sink_db.list_accounting_sink_audit_event_ids(
        limit=10,
        since=boundary,
    ) == (new.event.event_id,)
    assert tuple(
        row.event_id
        for row in sink_db.list_accounting_sink_audit_rows(
            limit=1,
            event_ids=(new.event.event_id, "missing-receipt"),
        )
    ) == (new.event.event_id,)


def test_empty_selected_batch_returns_empty(source_db, sink_db) -> None:
    assert source_db.list_accounting_source_audit_rows(limit=1, event_ids=()) == ()
    assert source_db.list_accounting_source_audit_event_ids(limit=1) == ()
    assert sink_db.list_accounting_sink_audit_rows(limit=1, event_ids=()) == ()
    assert sink_db.list_accounting_sink_audit_event_ids(limit=1) == ()


def test_selected_child_keeps_parent_validation_outside_batch(source_db) -> None:
    from llm_gateway_core.services.accounting import AccountingParentLinkState
    from tests.test_accounting_source_repository import _rollup

    child = _charge('old-child', parent_event_id='new-parent')
    source_db.accept_accounting_event(child)
    source_db.accept_accounting_event(_rollup('new-parent', (child,)))
    rows = source_db.list_accounting_source_audit_rows(limit=1, event_ids=('old-child',))
    assert len(rows) == 1
    assert rows[0].parent_link_state is AccountingParentLinkState.LINKED


def test_history_enumeration_excludes_new_source_with_backdated_usage(source_db) -> None:
    boundary = datetime(2026, 7, 13, 12, 0, tzinfo=timezone.utc)
    event = _charge('new-backdated', occurred_at=boundary - timedelta(days=2))
    source_db.accept_accounting_event(event)
    assert source_db.list_accounting_source_audit_event_ids(limit=10, since=boundary) == (event.event_id,)
    assert source_db.list_accounting_source_audit_event_ids(limit=10, before=boundary) == ()
