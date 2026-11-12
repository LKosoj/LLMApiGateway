from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from pathlib import Path

import pytest

from llm_gateway_core.db.tokens_usage_db import TokensUsageDB


@pytest.fixture
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return tmp_path


def test_source_v2_to_v3_migration_preserves_rows_and_is_idempotent(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="source-v2-to-v3.db")
    with sqlite3.connect(source.db_path) as conn:
        conn.execute(
            "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, ?)",
            ("2026-11-12T10:00:00+00:00", "preserved-event"),
        )
        conn.execute("DROP INDEX ix_accounting_outbox_created")
        conn.execute("DROP INDEX ix_tokens_usage_accounting_time")
        conn.execute(
            "DELETE FROM gateway_schema_migrations "
            "WHERE component = 'accounting_source' AND version = 3"
        )

    TokensUsageDB(db_filename="source-v2-to-v3.db")
    TokensUsageDB(db_filename="source-v2-to-v3.db")

    with sqlite3.connect(source.db_path) as conn:
        assert conn.execute(
            "SELECT accounting_event_id FROM tokens_usage"
        ).fetchone() == ("preserved-event",)
        assert [row[0] for row in conn.execute(
            "SELECT version FROM gateway_schema_migrations "
            "WHERE component = 'accounting_source' ORDER BY version"
        )] == [1, 2, 3]
        assert {
            row[1]
            for row in conn.execute("PRAGMA index_list(accounting_outbox)")
        } >= {
            "ix_accounting_outbox_created",
        }
        assert {
            row[1]
            for row in conn.execute("PRAGMA index_list(tokens_usage)")
        } >= {
            "ix_tokens_usage_accounting_time",
        }


def test_recent_source_lookup_explain_uses_time_indexes(db_dir: Path) -> None:
    source = TokensUsageDB(db_filename="source-time-indexes.db")
    boundary = datetime(2026, 11, 12, tzinfo=timezone.utc)
    source.list_accounting_source_audit_event_ids(limit=10, since=boundary)

    with sqlite3.connect(source.db_path) as conn:
        outbox_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT event_id FROM accounting_outbox "
            "INDEXED BY ix_accounting_outbox_created "
            "WHERE created_at >= ? ORDER BY event_id LIMIT 10",
            (boundary.isoformat(),),
        ).fetchall()
        usage_plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT accounting_event_id FROM tokens_usage "
            "INDEXED BY ix_tokens_usage_accounting_time "
            "WHERE accounting_event_id IS NOT NULL AND timestamp >= ? "
            "ORDER BY accounting_event_id LIMIT 10",
            (boundary.isoformat(),),
        ).fetchall()

    assert any("ix_accounting_outbox_created" in row[3] for row in outbox_plan)
    assert any("ix_tokens_usage_accounting_time" in row[3] for row in usage_plan)
