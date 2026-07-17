from __future__ import annotations

import sqlite3
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from llm_gateway_core.db import accounting_schema
from llm_gateway_core.db.accounting_schema import (
    ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS,
    AccountingSchemaError,
    migrate_accounting_sink,
    migrate_accounting_source,
)
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB


SOURCE_TABLES = (
    "accounting_outbox",
    "accounting_event_components",
    "accounting_event_links",
)
SINK_TABLES = (
    "applied_accounting_events",
    "api_key_accounting_tombstones",
)
LEAF_COST_SOURCES = (
    "upstream",
    "token_registry",
    "operation_configured",
    "operation_default",
)
CHARGE_COST_SOURCES = (
    *LEAF_COST_SOURCES,
    "component_sum",
)
ALLOWED_COST_SOURCES = (
    *CHARGE_COST_SOURCES,
    "receipt_rollup",
)
MAX_FINITE_FLOAT = 1.7976931348623157e308
INVALID_EVENT_IDS = (
    "",
    " leading-space",
    "trailing-space ",
    "event\x00suffix",
    "event-не-ascii",
    "x" * 256,
    sqlite3.Binary(b"event-id"),
)
INVALID_FINGERPRINTS = (
    "a" * 63,
    "A" * 64,
    "a" * 63 + "g",
    "a" * 31 + "\x00" + "a" * 32,
    sqlite3.Binary(b"a" * 64),
    None,
)


@pytest.fixture
def db_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    return tmp_path


def _table_columns(conn: sqlite3.Connection, table: str) -> list[tuple]:
    return [tuple(row) for row in conn.execute(f'PRAGMA table_info("{table}")')]


def _column_names(conn: sqlite3.Connection, table: str) -> list[str]:
    return [str(row[1]) for row in _table_columns(conn, table)]


def _table_names(conn: sqlite3.Connection) -> set[str]:
    return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}


def _create_legacy_tokens_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE tokens_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                model TEXT,
                provider TEXT
            );
            CREATE TABLE usage_idempotency_keys (
                idempotency_key TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL
            );
            INSERT INTO tokens_usage (
                timestamp, prompt_tokens, completion_tokens, total_tokens,
                cost, model, provider
            ) VALUES (
                '2026-07-01T00:00:00+00:00', 2, 3, 5,
                0.25, 'legacy-model', 'legacy-provider'
            );
            INSERT INTO usage_idempotency_keys (idempotency_key, created_at)
            VALUES ('legacy-pdf-job', '2026-07-01T00:00:00+00:00');
            PRAGMA user_version = 73;
            """
        )


def _create_legacy_api_keys_db(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL UNIQUE,
                budget_usd REAL,
                spent_usd REAL NOT NULL DEFAULT 0.0,
                rpm INTEGER,
                tpm INTEGER,
                allowed_models TEXT,
                disabled INTEGER NOT NULL DEFAULT 0,
                metadata TEXT,
                created_at DATETIME NOT NULL,
                last_used_at DATETIME
            );
            CREATE INDEX idx_api_keys_key ON api_keys (api_key);
            INSERT INTO api_keys (
                name, api_key, budget_usd, spent_usd, rpm, tpm,
                allowed_models, disabled, metadata, created_at, last_used_at
            ) VALUES (
                'legacy-key', 'lgk_legacy_secret', 9.0, 4.25, 10, 100,
                '["gateway/model"]', 0, '{"owner":"legacy"}',
                '2026-07-01T00:00:00+00:00',
                '2026-07-02T00:00:00+00:00'
            );
            PRAGMA user_version = 91;
            """
        )


def _create_current_tokens_table(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE tokens_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL
            )
            """
        )
        conn.execute("PRAGMA journal_mode=WAL")


def _create_current_api_keys_table(path: Path) -> None:
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE api_keys (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT NOT NULL,
                api_key TEXT NOT NULL UNIQUE,
                spent_usd REAL NOT NULL DEFAULT 0.0,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.execute("PRAGMA journal_mode=WAL")


def _create_v1_source_schema(path: Path, *, with_component: bool = False) -> None:
    _create_current_tokens_table(path)
    with sqlite3.connect(path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(accounting_schema._MIGRATIONS_TABLE_SQL)
        for statement in accounting_schema._SOURCE_MIGRATION_V1_SQL:
            conn.execute(statement)
        conn.execute(
            """
            INSERT INTO gateway_schema_migrations (component, version, applied_at)
            VALUES ('accounting_source', 1, '2026-07-13T00:00:00+00:00')
            """
        )
        if with_component:
            _insert_outbox(conn, event_id="migrated-component-parent")
            conn.execute(
                """
                INSERT INTO accounting_event_components (
                    event_id, ordinal, provider, model, prompt_tokens,
                    completion_tokens, total_tokens, reasoning_tokens,
                    cached_tokens, cost_usd, cost_source,
                    component_fingerprint
                ) VALUES (?, 0, ?, ?, 2, 3, 5, 1, 1, 0.25, ?, ?)
                """,
                (
                    "migrated-component-parent",
                    "provider-a",
                    "model-a",
                    "upstream",
                    "b" * 64,
                ),
            )
        conn.commit()


def _marker_rows(conn: sqlite3.Connection) -> list[tuple[str, int]]:
    return [
        (str(row[0]), int(row[1]))
        for row in conn.execute("SELECT component, version FROM gateway_schema_migrations ORDER BY component, version")
    ]


def _insert_outbox(
    conn: sqlite3.Connection,
    *,
    event_id: object,
    schema_version: object = 1,
    event_kind: str = "charge",
    billing_fingerprint: object = "a" * 64,
    api_key_id: object = 7,
    usage_cost_usd: object = 0.0,
    total_tokens: object = 0,
    cost_source: str = "upstream",
    parent_event_id: object = None,
    http_method: str = "POST",
    route_template: str = "/v1/images/generations",
    projection_attempts: object = 0,
) -> None:
    conn.execute(
        """
        INSERT INTO accounting_outbox (
            event_id, schema_version, event_kind, billing_fingerprint,
            api_key_id, usage_cost_usd, total_tokens, cost_source,
            request_id, parent_event_id, http_method, route_template,
            occurred_at, created_at, projection_attempts
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            schema_version,
            event_kind,
            billing_fingerprint,
            api_key_id,
            usage_cost_usd,
            total_tokens,
            cost_source,
            parent_event_id,
            http_method,
            route_template,
            "2026-07-13T00:00:00+00:00",
            "2026-07-13T00:00:00+00:00",
            projection_attempts,
        ),
    )


def _insert_component(
    conn: sqlite3.Connection,
    *,
    event_id: object = "component-parent",
    ordinal: object = 0,
    component_kind: object = "model",
    provider: object = "provider",
    model: object = "model",
    operation: object = None,
    gateway_model: object = None,
    prompt_tokens: object = 0,
    completion_tokens: object = 0,
    total_tokens: object = 0,
    reasoning_tokens: object = 0,
    cached_tokens: object = 0,
    cost_usd: object = 0.0,
    cost_source: str = "upstream",
    component_fingerprint: object = "b" * 64,
) -> None:
    conn.execute(
        """
        INSERT INTO accounting_event_components (
            event_id, ordinal, component_kind, provider, model, operation,
            gateway_model, prompt_tokens,
            completion_tokens, total_tokens, reasoning_tokens,
            cached_tokens, cost_usd, cost_source, component_fingerprint
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_id,
            ordinal,
            component_kind,
            provider,
            model,
            operation,
            gateway_model,
            prompt_tokens,
            completion_tokens,
            total_tokens,
            reasoning_tokens,
            cached_tokens,
            cost_usd,
            cost_source,
            component_fingerprint,
        ),
    )


def _insert_link(
    conn: sqlite3.Connection,
    *,
    parent_event_id: object = "link-parent",
    ordinal: object = 0,
    child_event_id: object = "link-child",
    child_billing_fingerprint: object = "c" * 64,
) -> None:
    conn.execute(
        """
        INSERT INTO accounting_event_links (
            parent_event_id, ordinal, child_event_id,
            child_billing_fingerprint
        ) VALUES (?, ?, ?, ?)
        """,
        (
            parent_event_id,
            ordinal,
            child_event_id,
            child_billing_fingerprint,
        ),
    )


def _insert_applied_event(
    conn: sqlite3.Connection,
    *,
    event_id: object = "applied-event",
    billing_fingerprint: object = "d" * 64,
    api_key_id: object = 1,
    spend_usd: object = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO applied_accounting_events (
            event_id, billing_fingerprint, api_key_id,
            spend_usd, applied_at, sink_kind
        ) VALUES (?, ?, ?, ?, '2026-07-13T00:00:00+00:00', 'active')
        """,
        (event_id, billing_fingerprint, api_key_id, spend_usd),
    )


def _insert_tombstone(
    conn: sqlite3.Connection,
    *,
    api_key_id: object = 1,
    spent_usd: object = 0.0,
) -> None:
    conn.execute(
        """
        INSERT INTO api_key_accounting_tombstones (
            api_key_id, deleted_at, spent_usd, last_used_at
        ) VALUES (?, '2026-07-13T00:00:00+00:00', ?, NULL)
        """,
        (api_key_id, spent_usd),
    )


def test_fresh_initialization_creates_exact_dormant_accounting_schema(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="source.db")
    sink = ApiKeysDB(db_filename="sink.db")

    with sqlite3.connect(source.db_path) as conn:
        token_columns = {row[1]: (row[2], row[3], row[4], row[5]) for row in _table_columns(conn, "tokens_usage")}
        assert token_columns["accounting_event_id"] == ("TEXT", 0, None, 0)
        assert token_columns["accounting_kind"] == ("TEXT", 1, "'charge'", 0)
        assert token_columns["parent_accounting_event_id"] == (
            "TEXT",
            0,
            None,
            0,
        )
        assert _column_names(conn, "accounting_outbox") == [
            "event_id",
            "schema_version",
            "event_kind",
            "billing_fingerprint",
            "api_key_id",
            "usage_cost_usd",
            "total_tokens",
            "cost_source",
            "request_id",
            "parent_event_id",
            "http_method",
            "route_template",
            "occurred_at",
            "created_at",
            "projected_at",
            "projection_attempts",
            "last_attempt_at",
            "last_error_code",
        ]
        assert _column_names(conn, "accounting_event_components") == [
            "event_id",
            "ordinal",
            "component_kind",
            "provider",
            "model",
            "operation",
            "gateway_model",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cost_usd",
            "cost_source",
            "component_fingerprint",
        ]
        assert _column_names(conn, "accounting_event_links") == [
            "parent_event_id",
            "ordinal",
            "child_event_id",
            "child_billing_fingerprint",
        ]
        event_index = next(
            row
            for row in conn.execute("PRAGMA index_list(tokens_usage)")
            if row[1] == "ux_tokens_usage_accounting_event"
        )
        assert event_index[2] == 1
        assert event_index[4] == 1
        assert [row[2] for row in conn.execute("PRAGMA index_info(ux_tokens_usage_accounting_event)")] == [
            "accounting_event_id"
        ]
        component_fks = {
            (row[3], row[2], row[4], row[6])
            for row in conn.execute("PRAGMA foreign_key_list(accounting_event_components)")
        }
        assert component_fks == {("event_id", "accounting_outbox", "event_id", "RESTRICT")}
        link_fks = {
            (row[3], row[2], row[4], row[6]) for row in conn.execute("PRAGMA foreign_key_list(accounting_event_links)")
        }
        assert link_fks == {
            ("parent_event_id", "accounting_outbox", "event_id", "RESTRICT"),
            ("child_event_id", "accounting_outbox", "event_id", "RESTRICT"),
        }
        link_unique_indexes = {
            (
                row[3],
                tuple(
                    index_row[0]
                    for index_row in conn.execute(
                        "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                        (row[1],),
                    )
                ),
            )
            for row in conn.execute("PRAGMA index_list(accounting_event_links)")
            if row[2] == 1
        }
        assert link_unique_indexes == {
            ("pk", ("parent_event_id", "ordinal")),
            ("u", ("parent_event_id", "child_event_id")),
        }
        assert conn.execute("PRAGMA foreign_key_check").fetchall() == []
        assert _marker_rows(conn) == [
            ("accounting_source", 1),
            ("accounting_source", 2),
        ]
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SOURCE_TABLES)

    with sqlite3.connect(sink.db_path) as conn:
        assert _column_names(conn, "applied_accounting_events") == [
            "event_id",
            "billing_fingerprint",
            "api_key_id",
            "spend_usd",
            "applied_at",
            "sink_kind",
        ]
        assert _column_names(conn, "api_key_accounting_tombstones") == [
            "api_key_id",
            "deleted_at",
            "spent_usd",
            "last_used_at",
        ]
        assert "api_key" not in _column_names(conn, "api_key_accounting_tombstones")
        assert _marker_rows(conn) == [("accounting_sink", 1)]
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SINK_TABLES)


def test_source_checks_pin_cost_sources_rollup_and_component_integrity(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="source-checks.db")

    with sqlite3.connect(source.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        for ordinal, cost_source in enumerate(CHARGE_COST_SOURCES):
            _insert_outbox(
                conn,
                event_id=f"event-{ordinal}",
                cost_source=cost_source,
            )
        _insert_outbox(
            conn,
            event_id="valid-rollup",
            event_kind="rollup",
            cost_source="receipt_rollup",
        )

        for invalid_source in ("registry", "operation", "unknown"):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-{invalid_source}",
                    cost_source=invalid_source,
                )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_outbox(
                conn,
                event_id="charge-with-rollup-source",
                cost_source="receipt_rollup",
            )
        for ordinal, cost_source in enumerate(CHARGE_COST_SOURCES):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"rollup-with-charge-source-{ordinal}",
                    event_kind="rollup",
                    cost_source=cost_source,
                )

        with pytest.raises(sqlite3.IntegrityError):
            _insert_outbox(
                conn,
                event_id="nonzero-rollup-cost",
                event_kind="rollup",
                usage_cost_usd=0.1,
                cost_source="receipt_rollup",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_outbox(
                conn,
                event_id="nonzero-rollup-tokens",
                event_kind="rollup",
                total_tokens=1,
                cost_source="receipt_rollup",
            )

        _insert_outbox(conn, event_id="component-parent")
        for ordinal, cost_source in enumerate(LEAF_COST_SOURCES):
            _insert_component(
                conn,
                ordinal=ordinal,
                prompt_tokens=2,
                completion_tokens=3,
                total_tokens=5,
                cost_usd=0.1,
                cost_source=cost_source,
                component_fingerprint=f"{ordinal:x}" * 64,
            )
        for ordinal, cost_source in enumerate(
            ("upstream", "operation_configured", "operation_default"),
            start=len(LEAF_COST_SOURCES),
        ):
            _insert_component(
                conn,
                ordinal=ordinal,
                component_kind="operation",
                provider=None,
                model=None,
                operation="web_search",
                gateway_model=f"gateway/search-{ordinal}",
                cost_usd=0.1,
                cost_source=cost_source,
                component_fingerprint=f"{ordinal:x}" * 64,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_component(
                conn,
                ordinal=20,
                component_kind="operation",
                provider=None,
                model=None,
                operation="web_search",
                gateway_model="gateway/search",
                cost_source="token_registry",
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_component(
                conn,
                ordinal=21,
                component_kind="operation",
                provider=None,
                model=None,
                operation="web_search",
                gateway_model="gateway/search",
                prompt_tokens=1,
                total_tokens=1,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_component(
                conn,
                ordinal=22,
                component_kind="model",
                operation="web_search",
                gateway_model="gateway/search",
            )
        for ordinal, invalid_source in enumerate(
            ("component_sum", "receipt_rollup", "operation", "unknown"),
            start=30,
        ):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(
                    conn,
                    ordinal=ordinal,
                    cost_source=invalid_source,
                )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_component(
                conn,
                ordinal=40,
                prompt_tokens=1,
                completion_tokens=1,
                total_tokens=3,
            )
        with pytest.raises(sqlite3.IntegrityError):
            _insert_component(conn, event_id="missing")
        conn.rollback()


def test_outbox_route_identity_checks_reject_noncanonical_values(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="route-identity-checks.db")

    with sqlite3.connect(source.db_path) as conn:
        _insert_outbox(
            conn,
            event_id="valid-get-route",
            http_method="GET",
            route_template="/v1/pdf/jobs/{job_id}",
        )
        assert conn.execute("SELECT http_method, route_template FROM accounting_outbox").fetchone() == (
            "GET",
            "/v1/pdf/jobs/{job_id}",
        )

        invalid_identities = (
            ("post", "/v1/images/generations"),
            ("POST1", "/v1/images/generations"),
            ("POST\x00BAD", "/v1/images/generations"),
            ("", "/v1/images/generations"),
            ("POST", "v1/images/generations"),
            ("POST", " /v1/images/generations"),
            ("POST", "/v1/images/generations "),
            ("POST", "/v1/images/generations?size=1"),
            ("POST", "/v1/images/generations#fragment"),
            ("POST", "/v1/images\x00hidden"),
            ("POST", "/v1/изображения"),
            ("POST", "/" + "x" * 512),
        )
        for index, (http_method, route_template) in enumerate(invalid_identities):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-route-{index}",
                    http_method=http_method,
                    route_template=route_template,
                )
        conn.rollback()


def test_event_ids_require_canonical_bounded_ascii_text_in_every_table(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-event-ids.db")
    sink = ApiKeysDB(db_filename="strict-sink-event-ids.db")
    mandatory_invalid_ids = (*INVALID_EVENT_IDS, None)

    with sqlite3.connect(source.db_path) as conn:
        conn.execute(
            """
            INSERT INTO tokens_usage (
                timestamp, accounting_event_id, parent_accounting_event_id
            ) VALUES ('2026-07-13T00:00:00+00:00', NULL, NULL)
            """
        )
        for index, invalid_id in enumerate(INVALID_EVENT_IDS):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, ?)",
                    ("2026-07-13T00:00:00+00:00", invalid_id),
                )
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO tokens_usage (
                        timestamp, parent_accounting_event_id
                    ) VALUES (?, ?)
                    """,
                    ("2026-07-13T00:00:00+00:00", invalid_id),
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-parent-{index}",
                    parent_event_id=invalid_id,
                )

        for invalid_id in mandatory_invalid_ids:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(conn, event_id=invalid_id)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(conn, event_id=invalid_id)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_link(conn, parent_event_id=invalid_id)
            with pytest.raises(sqlite3.IntegrityError):
                _insert_link(conn, child_event_id=invalid_id)

        _insert_outbox(conn, event_id="x" * 255)
        conn.rollback()

    with sqlite3.connect(sink.db_path) as conn:
        for invalid_id in mandatory_invalid_ids:
            with pytest.raises(sqlite3.IntegrityError):
                _insert_applied_event(conn, event_id=invalid_id)
        _insert_applied_event(conn, event_id="x" * 255)
        conn.rollback()


def test_fingerprints_require_exact_lowercase_sha256_text_in_every_table(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-fingerprints.db")
    sink = ApiKeysDB(db_filename="strict-sink-fingerprints.db")

    with sqlite3.connect(source.db_path) as conn:
        _insert_outbox(conn, event_id="valid-outbox")
        _insert_component(conn, event_id="valid-outbox")
        _insert_link(conn)
        for index, invalid_fingerprint in enumerate(INVALID_FINGERPRINTS):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-outbox-fingerprint-{index}",
                    billing_fingerprint=invalid_fingerprint,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(
                    conn,
                    ordinal=index + 1,
                    component_fingerprint=invalid_fingerprint,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_link(
                    conn,
                    ordinal=index + 1,
                    child_event_id=f"link-child-{index}",
                    child_billing_fingerprint=invalid_fingerprint,
                )
        conn.rollback()

    with sqlite3.connect(sink.db_path) as conn:
        _insert_applied_event(conn)
        for index, invalid_fingerprint in enumerate(INVALID_FINGERPRINTS):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_applied_event(
                    conn,
                    event_id=f"invalid-applied-fingerprint-{index}",
                    billing_fingerprint=invalid_fingerprint,
                )
        conn.rollback()


def test_component_provider_and_model_require_normalized_bounded_text(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-component-text.db")
    invalid_values = (
        None,
        "",
        " ",
        " leading-space",
        "trailing-space ",
        "\tleading-tab",
        "trailing-lf\n",
        "\u00a0leading-nbsp",
        "trailing-nbsp\u00a0",
        "value\x00suffix",
        "a" * 513,
        "я" * 257,
        sqlite3.Binary(b"provider"),
    )

    with sqlite3.connect(source.db_path) as conn:
        _insert_component(
            conn,
            provider="провайдер",
            model="модель",
        )
        _insert_component(
            conn,
            ordinal=1,
            provider="я" * 256,
            model="я" * 256,
        )
        for index, invalid_value in enumerate(invalid_values, start=2):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(
                    conn,
                    ordinal=index,
                    provider=invalid_value,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(
                    conn,
                    ordinal=index,
                    model=invalid_value,
                )
        conn.rollback()


def test_integer_ids_versions_and_counters_require_sqlite_integer_storage(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-integers.db")
    sink = ApiKeysDB(db_filename="strict-sink-integers.db")
    invalid_integers = ("not-an-integer", 0.5, sqlite3.Binary(b"1"), -1)

    with sqlite3.connect(source.db_path) as conn:
        for index, invalid_version in enumerate((*invalid_integers, 0, None)):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO gateway_schema_migrations (
                        component, version, applied_at
                    ) VALUES (?, ?, '2026-07-13T00:00:00+00:00')
                    """,
                    (f"invalid-version-{index}", invalid_version),
                )

        for index, invalid_value in enumerate((*invalid_integers, 2)):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-schema-version-{index}",
                    schema_version=invalid_value,
                )
        for index, invalid_value in enumerate((*invalid_integers, 0)):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-api-key-{index}",
                    api_key_id=invalid_value,
                )
        _insert_outbox(conn, event_id="nullable-api-key", api_key_id=None)

        for field in ("total_tokens", "projection_attempts"):
            for index, invalid_value in enumerate(invalid_integers):
                kwargs = {
                    "event_id": f"invalid-{field}-{index}",
                    field: invalid_value,
                }
                with pytest.raises(sqlite3.IntegrityError):
                    _insert_outbox(conn, **kwargs)

        for field in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            for invalid_value in invalid_integers:
                kwargs = {field: invalid_value}
                with pytest.raises(sqlite3.IntegrityError):
                    _insert_component(conn, **kwargs)
        conn.rollback()

    with sqlite3.connect(sink.db_path) as conn:
        invalid_api_key_ids = (*invalid_integers, 0, None)
        for index, invalid_api_key_id in enumerate(invalid_api_key_ids):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_applied_event(
                    conn,
                    event_id=f"invalid-sink-api-key-{index}",
                    api_key_id=invalid_api_key_id,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_tombstone(
                    conn,
                    api_key_id=invalid_api_key_id,
                )
        conn.rollback()


def test_money_requires_finite_nonnegative_numeric_binary64_in_every_table(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-money.db")
    sink = ApiKeysDB(db_filename="strict-sink-money.db")
    invalid_money = (
        float("nan"),
        float("inf"),
        float("-inf"),
        -0.01,
        "not-numeric",
        sqlite3.Binary(b"0.1"),
    )

    with sqlite3.connect(source.db_path) as conn:
        _insert_outbox(
            conn,
            event_id="max-finite-outbox",
            usage_cost_usd=MAX_FINITE_FLOAT,
        )
        _insert_component(
            conn,
            event_id="max-finite-component",
            cost_usd=MAX_FINITE_FLOAT,
        )
        for index, invalid_value in enumerate(invalid_money):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_outbox(
                    conn,
                    event_id=f"invalid-outbox-money-{index}",
                    usage_cost_usd=invalid_value,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_component(
                    conn,
                    event_id=f"invalid-component-money-{index}",
                    ordinal=index + 1,
                    cost_usd=invalid_value,
                )
        conn.rollback()

    with sqlite3.connect(sink.db_path) as conn:
        _insert_applied_event(
            conn,
            event_id="max-finite-applied",
            spend_usd=MAX_FINITE_FLOAT,
        )
        _insert_tombstone(api_key_id=1, conn=conn, spent_usd=MAX_FINITE_FLOAT)
        for index, invalid_value in enumerate(invalid_money):
            with pytest.raises(sqlite3.IntegrityError):
                _insert_applied_event(
                    conn,
                    event_id=f"invalid-applied-money-{index}",
                    spend_usd=invalid_value,
                )
            with pytest.raises(sqlite3.IntegrityError):
                _insert_tombstone(
                    conn,
                    api_key_id=index + 2,
                    spent_usd=invalid_value,
                )
        conn.rollback()


def test_component_and_link_ordinals_require_nonnegative_sqlite_integer(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="strict-ordinals.db")

    with sqlite3.connect(source.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_outbox(conn, event_id="component-parent")
        _insert_outbox(
            conn,
            event_id="link-parent",
            event_kind="rollup",
            cost_source="receipt_rollup",
            route_template="/v1/web/deep-research",
        )

        invalid_ordinals = ("abc", 0.5, sqlite3.Binary(b"2"), -1)
        for index, ordinal in enumerate(invalid_ordinals):
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO accounting_event_components (
                        event_id, ordinal, provider, model, prompt_tokens,
                        completion_tokens, total_tokens, reasoning_tokens,
                        cached_tokens, cost_usd, cost_source,
                        component_fingerprint
                    ) VALUES (
                        'component-parent', ?, 'provider', 'model',
                        0, 0, 0, 0, 0, 0, 'upstream', ?
                    )
                    """,
                    (ordinal, "c" * 64),
                )

            child_id = f"child-{index}"
            _insert_outbox(conn, event_id=child_id)
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    """
                    INSERT INTO accounting_event_links (
                        parent_event_id, ordinal, child_event_id,
                        child_billing_fingerprint
                    ) VALUES ('link-parent', ?, ?, ?)
                    """,
                    (ordinal, child_id, "d" * 64),
                )

        conn.rollback()


def test_event_links_persist_child_order_and_reject_identity_collisions(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="ordered-event-links.db")

    with sqlite3.connect(source.db_path) as conn:
        conn.execute("PRAGMA foreign_keys=ON")
        _insert_outbox(
            conn,
            event_id="parent",
            event_kind="rollup",
            cost_source="receipt_rollup",
            route_template="/v1/web/deep-research",
        )
        for child_id in ("child-a", "child-b", "child-c"):
            _insert_outbox(conn, event_id=child_id)
        conn.executemany(
            """
            INSERT INTO accounting_event_links (
                parent_event_id, ordinal, child_event_id,
                child_billing_fingerprint
            ) VALUES ('parent', ?, ?, ?)
            """,
            (
                (0, "child-b", "b" * 64),
                (1, "child-a", "a" * 64),
            ),
        )

        assert conn.execute(
            """
            SELECT child_event_id, child_billing_fingerprint
            FROM accounting_event_links
            WHERE parent_event_id = 'parent'
            ORDER BY ordinal
            """
        ).fetchall() == [("child-b", "b" * 64), ("child-a", "a" * 64)]
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO accounting_event_links (
                    parent_event_id, ordinal, child_event_id,
                    child_billing_fingerprint
                ) VALUES ('parent', -1, 'child-c', ?)
                """,
                ("c" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO accounting_event_links (
                    parent_event_id, ordinal, child_event_id,
                    child_billing_fingerprint
                ) VALUES ('parent', 0, 'child-c', ?)
                """,
                ("c" * 64,),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO accounting_event_links (
                    parent_event_id, ordinal, child_event_id,
                    child_billing_fingerprint
                ) VALUES ('parent', 2, 'child-b', ?)
                """,
                ("b" * 64,),
            )
        conn.rollback()


def test_sink_checks_pin_non_secret_receipts_and_tombstones(db_dir: Path) -> None:
    sink = ApiKeysDB(db_filename="sink-checks.db")

    with sqlite3.connect(sink.db_path) as conn:
        conn.execute(
            """
            INSERT INTO applied_accounting_events (
                event_id, billing_fingerprint, api_key_id,
                spend_usd, applied_at, sink_kind
            ) VALUES ('event', ?, 1, 0.1, ?, 'active')
            """,
            ("a" * 64, "2026-07-13T00:00:00+00:00"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO applied_accounting_events (
                    event_id, billing_fingerprint, api_key_id,
                    spend_usd, applied_at, sink_kind
                ) VALUES ('negative', ?, 1, -0.1, ?, 'active')
                """,
                ("b" * 64, "2026-07-13T00:00:00+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO applied_accounting_events (
                    event_id, billing_fingerprint, api_key_id,
                    spend_usd, applied_at, sink_kind
                ) VALUES ('bad-kind', ?, 1, 0.1, ?, 'secret-copy')
                """,
                ("c" * 64, "2026-07-13T00:00:00+00:00"),
            )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """
                INSERT INTO api_key_accounting_tombstones (
                    api_key_id, deleted_at, spent_usd, last_used_at
                ) VALUES (1, ?, -0.1, NULL)
                """,
                ("2026-07-13T00:00:00+00:00",),
            )
        conn.rollback()


def test_partial_unique_event_index_allows_historical_nulls_only(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="partial-index.db")

    with sqlite3.connect(source.db_path) as conn:
        conn.execute(
            "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, NULL)",
            ("2026-07-13T00:00:00+00:00",),
        )
        conn.execute(
            "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, NULL)",
            ("2026-07-13T00:00:01+00:00",),
        )
        conn.execute(
            "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, ?)",
            ("2026-07-13T00:00:02+00:00", "event-id"),
        )
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                "INSERT INTO tokens_usage (timestamp, accounting_event_id) VALUES (?, ?)",
                ("2026-07-13T00:00:03+00:00", "event-id"),
            )
        assert conn.execute("SELECT COUNT(*) FROM tokens_usage WHERE accounting_event_id IS NULL").fetchone()[0] == 2
        conn.rollback()


def test_legacy_source_migration_preserves_history_idempotency_and_user_version(
    db_dir: Path,
) -> None:
    path = db_dir / "legacy-source.db"
    _create_legacy_tokens_db(path)

    source = TokensUsageDB(db_filename=path.name)

    with sqlite3.connect(source.db_path) as conn:
        row = conn.execute(
            """
            SELECT prompt_tokens, completion_tokens, total_tokens, cost,
                   model, provider, accounting_event_id, accounting_kind,
                   parent_accounting_event_id
            FROM tokens_usage
            """
        ).fetchone()
        assert row == (
            2,
            3,
            5,
            0.25,
            "legacy-model",
            "legacy-provider",
            None,
            "charge",
            None,
        )
        assert conn.execute("SELECT idempotency_key FROM usage_idempotency_keys").fetchone()[0] == "legacy-pdf-job"
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 73
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SOURCE_TABLES)


def test_source_v1_to_v2_migration_preserves_model_manifest_identity(
    db_dir: Path,
) -> None:
    path = db_dir / "source-v1-with-component.db"
    _create_v1_source_schema(path, with_component=True)

    migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert _marker_rows(conn) == [
            ("accounting_source", 1),
            ("accounting_source", 2),
        ]
        assert conn.execute(
            """
            SELECT component_kind, provider, model, operation, gateway_model,
                   component_fingerprint
            FROM accounting_event_components
            """
        ).fetchone() == (
            "model",
            "provider-a",
            "model-a",
            None,
            None,
            "b" * 64,
        )
        assert "accounting_event_components_v1" not in _table_names(conn)


def test_source_v2_literal_case_tamper_fails_before_writes(db_dir: Path) -> None:
    source = TokensUsageDB(db_filename="source-v2-literal-case-tamper.db")
    tampered_sql = accounting_schema._EVENT_COMPONENTS_V2_TABLE_SQL.replace(
        "component_kind IN ('model', 'operation')",
        "component_kind IN ('MODEL', 'operation')",
        1,
    )

    with sqlite3.connect(source.db_path) as conn:
        assert _marker_rows(conn) == [
            ("accounting_source", 1),
            ("accounting_source", 2),
        ]
        conn.execute(
            "ALTER TABLE accounting_event_components "
            "RENAME TO accounting_event_components_original"
        )
        conn.execute(tampered_sql)
        conn.execute("DROP TABLE accounting_event_components_original")

    with pytest.raises(
        AccountingSchemaError,
        match="accounting_event_components",
    ):
        migrate_accounting_source(source.db_path)

    with sqlite3.connect(source.db_path) as conn:
        assert _marker_rows(conn) == [
            ("accounting_source", 1),
            ("accounting_source", 2),
        ]
        assert all(
            conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0
            for table in SOURCE_TABLES
        )


def test_source_v2_marker_failure_rolls_back_table_rebuild(
    db_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = db_dir / "source-v2-marker-failure.db"
    _create_v1_source_schema(path, with_component=True)
    insert_marker = accounting_schema._insert_migration_marker

    def fail_v2_marker(
        conn: sqlite3.Connection,
        component: str,
        version: int,
    ) -> None:
        if component == "accounting_source" and version == 2:
            raise sqlite3.OperationalError("injected v2 marker failure")
        insert_marker(conn, component, version)

    monkeypatch.setattr(accounting_schema, "_insert_migration_marker", fail_v2_marker)

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert _marker_rows(conn) == [("accounting_source", 1)]
        assert _column_names(conn, "accounting_event_components") == [
            "event_id",
            "ordinal",
            "provider",
            "model",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
            "cost_usd",
            "cost_source",
            "component_fingerprint",
        ]
        assert conn.execute(
            "SELECT provider, model, component_fingerprint FROM accounting_event_components"
        ).fetchone() == ("provider-a", "model-a", "b" * 64)


def test_source_v1_schema_is_verified_exactly_before_v2_rebuild(
    db_dir: Path,
) -> None:
    path = db_dir / "source-v1-tampered-before-v2.db"
    _create_v1_source_schema(path)
    with sqlite3.connect(path) as conn:
        conn.execute("ALTER TABLE accounting_event_components RENAME TO original_components")
        conn.execute(
            """
            CREATE TABLE accounting_event_components (
                event_id TEXT NOT NULL,
                ordinal INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                PRIMARY KEY (event_id, ordinal)
            )
            """
        )
        conn.execute("DROP TABLE original_components")

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert _marker_rows(conn) == [("accounting_source", 1)]
        assert _column_names(conn, "accounting_event_components") == [
            "event_id",
            "ordinal",
            "provider",
            "model",
        ]
        assert "accounting_event_components_v1" not in _table_names(conn)


def test_source_v2_migration_verifies_migrated_rows_after_commit(
    db_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = db_dir / "source-v2-post-commit-row-verification.db"
    _create_v1_source_schema(path, with_component=True)
    verify_schema = accounting_schema._verify_source_schema

    def mutate_after_schema_verification(conn: sqlite3.Connection) -> None:
        verify_schema(conn)
        conn.execute(
            """
            UPDATE accounting_event_components
            SET component_fingerprint = ?
            WHERE event_id = 'migrated-component-parent'
            """,
            ("c" * 64,),
        )
        conn.commit()

    monkeypatch.setattr(
        accounting_schema,
        "_verify_source_schema",
        mutate_after_schema_verification,
    )

    with pytest.raises(
        AccountingSchemaError,
        match="accounting_event_components.migrated_rows",
    ):
        migrate_accounting_source(path)


@pytest.mark.parametrize("versions", ((2,), (1, 3), (1, 2, 3)))
def test_source_migration_rejects_marker_gaps_and_future_versions_without_ddl(
    db_dir: Path,
    versions: tuple[int, ...],
) -> None:
    path = db_dir / f"invalid-source-markers-{'-'.join(map(str, versions))}.db"
    _create_current_tokens_table(path)
    with sqlite3.connect(path) as conn:
        conn.execute(accounting_schema._MIGRATIONS_TABLE_SQL)
        conn.executemany(
            """
            INSERT INTO gateway_schema_migrations (component, version, applied_at)
            VALUES ('accounting_source', ?, '2026-07-13T00:00:00+00:00')
            """,
            ((version,) for version in versions),
        )

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert "accounting_event_id" not in _column_names(conn, "tokens_usage")
        assert not (_table_names(conn) & set(SOURCE_TABLES))
        assert [row[0] for row in conn.execute(
            """
            SELECT version FROM gateway_schema_migrations
            WHERE component = 'accounting_source' ORDER BY version
            """
        )] == sorted(versions)


def test_legacy_sink_migration_preserves_credentials_spend_and_user_version(
    db_dir: Path,
) -> None:
    path = db_dir / "legacy-sink.db"
    _create_legacy_api_keys_db(path)

    sink = ApiKeysDB(db_filename=path.name)

    with sqlite3.connect(sink.db_path) as conn:
        row = conn.execute(
            """
            SELECT name, api_key, budget_usd, spent_usd, rpm, tpm,
                   allowed_models, metadata, last_used_at
            FROM api_keys
            """
        ).fetchone()
        assert row == (
            "legacy-key",
            "lgk_legacy_secret",
            9.0,
            4.25,
            10,
            100,
            '["gateway/model"]',
            '{"owner":"legacy"}',
            "2026-07-02T00:00:00+00:00",
        )
        assert conn.execute("PRAGMA user_version").fetchone()[0] == 91
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SINK_TABLES)


def test_reopen_is_idempotent_and_keeps_one_component_marker(db_dir: Path) -> None:
    TokensUsageDB(db_filename="reopen-source.db")
    TokensUsageDB(db_filename="reopen-source.db")
    ApiKeysDB(db_filename="reopen-sink.db")
    ApiKeysDB(db_filename="reopen-sink.db")

    with sqlite3.connect(db_dir / "reopen-source.db") as conn:
        assert _marker_rows(conn) == [
            ("accounting_source", 1),
            ("accounting_source", 2),
        ]
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SOURCE_TABLES)
    with sqlite3.connect(db_dir / "reopen-sink.db") as conn:
        assert _marker_rows(conn) == [("accounting_sink", 1)]
        assert all(conn.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0] == 0 for table in SINK_TABLES)


@pytest.mark.parametrize(
    ("filename", "prepare", "migrate", "marker"),
    [
        (
            "concurrent-source.db",
            _create_current_tokens_table,
            migrate_accounting_source,
            (("accounting_source", 1), ("accounting_source", 2)),
        ),
        (
            "concurrent-sink.db",
            _create_current_api_keys_table,
            migrate_accounting_sink,
            ("accounting_sink", 1),
        ),
    ],
)
def test_concurrent_migration_calls_serialize_and_remain_idempotent(
    db_dir: Path,
    filename: str,
    prepare,
    migrate,
    marker: tuple[str, int] | tuple[tuple[str, int], ...],
) -> None:
    path = db_dir / filename
    prepare(path)
    barrier = threading.Barrier(4)

    def run_migration(_index: int) -> None:
        barrier.wait()
        migrate(path)

    with ThreadPoolExecutor(max_workers=4) as executor:
        list(executor.map(run_migration, range(4)))

    with sqlite3.connect(path) as conn:
        expected_markers = list(marker) if marker and isinstance(marker[0], tuple) else [marker]
        assert _marker_rows(conn) == expected_markers


def test_marker_failure_rolls_back_source_ddl_and_marker(
    db_dir: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    path = db_dir / "rollback-source.db"
    _create_current_tokens_table(path)

    def fail_marker(
        conn: sqlite3.Connection,
        component: str,
        version: int,
    ) -> None:
        assert component == "accounting_source"
        assert version == 1
        assert "accounting_outbox" in _table_names(conn)
        raise sqlite3.OperationalError("injected marker failure")

    monkeypatch.setattr(accounting_schema, "_insert_migration_marker", fail_marker)

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert "accounting_event_id" not in _column_names(conn, "tokens_usage")
        assert not (_table_names(conn) & set(SOURCE_TABLES))
        assert "gateway_schema_migrations" not in _table_names(conn)


def test_ddl_failure_rolls_back_all_preceding_source_changes(db_dir: Path) -> None:
    path = db_dir / "rollback-source-ddl.db"
    _create_current_tokens_table(path)
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE accounting_outbox (event_id TEXT PRIMARY KEY)")

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        migrate_accounting_source(path)

    with sqlite3.connect(path) as conn:
        assert "accounting_event_id" not in _column_names(conn, "tokens_usage")
        assert "ux_tokens_usage_accounting_event" not in {
            str(row[1]) for row in conn.execute("PRAGMA index_list(tokens_usage)")
        }
        assert _column_names(conn, "accounting_outbox") == ["event_id"]
        assert "accounting_event_components" not in _table_names(conn)
        assert "accounting_event_links" not in _table_names(conn)
        assert "gateway_schema_migrations" not in _table_names(conn)


@pytest.mark.parametrize(
    ("filename", "prepare", "migrate", "component"),
    [
        (
            "incomplete-source.db",
            _create_current_tokens_table,
            migrate_accounting_source,
            "accounting_source",
        ),
        (
            "incomplete-sink.db",
            _create_current_api_keys_table,
            migrate_accounting_sink,
            "accounting_sink",
        ),
    ],
)
def test_existing_marker_with_incomplete_manual_schema_fails_closed(
    db_dir: Path,
    filename: str,
    prepare,
    migrate,
    component: str,
) -> None:
    path = db_dir / filename
    prepare(path)
    with sqlite3.connect(path) as conn:
        conn.execute(
            """
            CREATE TABLE gateway_schema_migrations (
                component TEXT NOT NULL,
                version INTEGER NOT NULL,
                applied_at TEXT NOT NULL,
                PRIMARY KEY (component, version)
            )
            """
        )
        conn.execute(
            """
            INSERT INTO gateway_schema_migrations (component, version, applied_at)
            VALUES (?, 1, '2026-07-13T00:00:00+00:00')
            """,
            (component,),
        )

    with pytest.raises(AccountingSchemaError, match=component):
        migrate(path)


def test_existing_source_marker_with_tampered_index_fails_closed(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="tampered-source.db")
    with sqlite3.connect(source.db_path) as conn:
        conn.execute("DROP INDEX ux_tokens_usage_accounting_event")

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        TokensUsageDB(db_filename="tampered-source.db")


def test_existing_source_marker_with_unordered_links_fails_closed(
    db_dir: Path,
) -> None:
    source = TokensUsageDB(db_filename="tampered-links.db")
    with sqlite3.connect(source.db_path) as conn:
        conn.execute("DROP TABLE accounting_event_links")
        conn.execute(
            """
            CREATE TABLE accounting_event_links (
                parent_event_id TEXT NOT NULL,
                child_event_id TEXT NOT NULL,
                child_billing_fingerprint TEXT NOT NULL,
                PRIMARY KEY (parent_event_id, child_event_id)
            )
            """
        )

    with pytest.raises(AccountingSchemaError, match="accounting_source"):
        TokensUsageDB(db_filename="tampered-links.db")


def test_existing_sink_marker_with_tampered_index_fails_closed(
    db_dir: Path,
) -> None:
    sink = ApiKeysDB(db_filename="tampered-sink.db")
    with sqlite3.connect(sink.db_path) as conn:
        conn.execute("DROP INDEX ix_applied_accounting_events_key")

    with pytest.raises(AccountingSchemaError, match="accounting_sink"):
        ApiKeysDB(db_filename="tampered-sink.db")


def test_dedicated_migration_connections_use_strict_bounded_pragmas(
    db_dir: Path,
) -> None:
    assert 0 < ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS < 5_000

    source_conn = accounting_schema._open_migration_connection(
        db_dir / "pragma-source.db",
        enable_foreign_keys=True,
    )
    try:
        assert source_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert source_conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert source_conn.execute("PRAGMA busy_timeout").fetchone()[0] == (ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS)
        assert source_conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        source_conn.close()

    sink_conn = accounting_schema._open_migration_connection(
        db_dir / "pragma-sink.db",
        enable_foreign_keys=False,
    )
    try:
        assert sink_conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert sink_conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert sink_conn.execute("PRAGMA busy_timeout").fetchone()[0] == (ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS)
    finally:
        sink_conn.close()


def test_runtime_accounting_connection_uses_strict_bounded_pragmas(
    db_dir: Path,
) -> None:
    conn = accounting_schema.open_accounting_runtime_connection(
        db_dir / "runtime-accounting.db",
        enable_foreign_keys=True,
    )
    try:
        assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 2
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == (ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS)
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    finally:
        conn.close()


def test_runtime_accounting_connection_closes_partial_setup_and_is_safe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_detail = "secret " + "path and SQL"

    class FailingConnection:
        closed = False

        def execute(self, _statement: str):
            raise sqlite3.OperationalError(sensitive_detail)

        def close(self) -> None:
            self.closed = True

    connection = FailingConnection()
    monkeypatch.setattr(
        accounting_schema.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(AccountingSchemaError) as error:
        accounting_schema.open_accounting_runtime_connection(
            "secret.db",
            enable_foreign_keys=True,
        )

    assert connection.closed is True
    assert sensitive_detail not in str(error.value)
    assert sensitive_detail not in "".join(traceback.format_exception(error.value))
    assert error.value.__suppress_context__ is True


def test_migration_wrapper_suppresses_sensitive_ordinary_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_detail = "migration " + "secret SQL"

    class ProbeConnection:
        in_transaction = False

        def execute(self, _statement: str, _params: tuple = ()):
            raise sqlite3.OperationalError(sensitive_detail)

        def close(self) -> None:
            pass

    monkeypatch.setattr(
        accounting_schema,
        "_open_migration_connection",
        lambda *_args, **_kwargs: ProbeConnection(),
    )

    with pytest.raises(AccountingSchemaError) as error:
        accounting_schema.migrate_accounting_source(tmp_path / "migration-safe.db")

    rendered = "".join(traceback.format_exception(error.value))
    assert sensitive_detail not in rendered
    assert error.value.__suppress_context__ is True


def test_migration_wrapper_suppresses_sensitive_close_cause(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    sensitive_detail = "migration " + "secret close"

    class CloseFailingConnection(sqlite3.Connection):
        def close(self) -> None:
            super().close()
            raise RuntimeError(sensitive_detail)

    connection = sqlite3.connect(
        tmp_path / "migration-close-safe.db",
        factory=CloseFailingConnection,
    )
    monkeypatch.setattr(
        accounting_schema,
        "_open_migration_connection",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(AccountingSchemaError) as error:
        accounting_schema._run_migration(
            tmp_path / "migration-close-safe.db",
            component="accounting_test",
            enable_foreign_keys=False,
            statements=(),
            verify=lambda _conn: None,
        )

    rendered = "".join(traceback.format_exception(error.value))
    assert sensitive_detail not in rendered
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("close_kind", ["none", "ordinary", "terminal"])
def test_runtime_connection_setup_terminal_preserves_primary_during_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    close_kind: str,
) -> None:
    primary = BaseException("setup-primary-sensitive")

    class ProbeConnection:
        close_calls = 0

        def execute(self, _statement: str):
            raise primary

        def close(self) -> None:
            self.close_calls += 1
            if close_kind == "ordinary":
                raise RuntimeError("close-ordinary-sensitive")
            if close_kind == "terminal":
                raise BaseException("close-terminal-sensitive")

    connection = ProbeConnection()
    monkeypatch.setattr(
        accounting_schema.sqlite3,
        "connect",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(BaseException) as error:
        accounting_schema.open_accounting_runtime_connection(
            "ignored.db",
            enable_foreign_keys=True,
        )

    assert error.value is primary
    assert connection.close_calls == 1


def test_migration_terminal_primary_wins_over_close_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    primary = BaseException("migration-primary-sensitive")
    secondary = BaseException("migration-close-sensitive")

    class ProbeConnection:
        in_transaction = False
        close_calls = 0

        def execute(self, _statement: str, _params: tuple = ()):
            raise primary

        def close(self) -> None:
            self.close_calls += 1
            raise secondary

    connection = ProbeConnection()
    monkeypatch.setattr(
        accounting_schema,
        "_open_migration_connection",
        lambda *_args, **_kwargs: connection,
    )

    with pytest.raises(BaseException) as error:
        accounting_schema.migrate_accounting_source(tmp_path / "migration-primary.db")

    assert error.value is primary
    assert connection.close_calls == 1


def test_runtime_accounting_connection_rejects_non_boolean_fk_option(
    db_dir: Path,
) -> None:
    with pytest.raises(AccountingSchemaError):
        accounting_schema.open_accounting_runtime_connection(
            db_dir / "invalid-option.db",
            enable_foreign_keys=1,  # type: ignore[arg-type]
        )


def test_commit_accounting_transaction_is_a_patchable_single_commit_seam():
    class CommitProbe:
        commits = 0

        def commit(self) -> None:
            self.commits += 1

    probe = CommitProbe()
    accounting_schema.commit_accounting_transaction(probe)  # type: ignore[arg-type]

    assert probe.commits == 1
