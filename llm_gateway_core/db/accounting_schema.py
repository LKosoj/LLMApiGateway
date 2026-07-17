"""Versioned SQLite schema for durable accounting.

R2.1 only installs and verifies dormant source/sink structures. Event writes,
projection, reconciliation, and ``spent_usd`` changes belong to later waves.
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from os import PathLike


ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS = 2_000

_SOURCE_COMPONENT = "accounting_source"
_SINK_COMPONENT = "accounting_sink"
_SOURCE_SCHEMA_VERSION = 2
_SINK_SCHEMA_VERSION = 1
_PYTHON_WHITESPACE_CODEPOINTS_SQL = (
    "9, 10, 11, 12, 13, 28, 29, 30, 31, 32, 133, 160, 5760, "
    "8192, 8193, 8194, 8195, 8196, 8197, 8198, 8199, 8200, "
    "8201, 8202, 8232, 8233, 8239, 8287, 12288"
)
_PYTHON_WHITESPACE_ABSENT_SQL = "\n        AND ".join(
    f"instr(gateway_model, char({codepoint})) = 0"
    for codepoint in (
        9,
        10,
        11,
        12,
        13,
        28,
        29,
        30,
        31,
        32,
        133,
        160,
        5760,
        8192,
        8193,
        8194,
        8195,
        8196,
        8197,
        8198,
        8199,
        8200,
        8201,
        8202,
        8232,
        8233,
        8239,
        8287,
        12288,
    )
)


class AccountingSchemaError(RuntimeError):
    """Raised when an accounting migration or exact schema check fails."""


_MIGRATIONS_TABLE_SQL = """
CREATE TABLE gateway_schema_migrations (
    component TEXT NOT NULL,
    version INTEGER NOT NULL CHECK (
        typeof(version) = 'integer' AND version > 0
    ),
    applied_at TEXT NOT NULL,
    PRIMARY KEY (component, version)
)
"""

_ADD_ACCOUNTING_EVENT_ID_SQL = """
ALTER TABLE tokens_usage ADD COLUMN accounting_event_id TEXT CHECK (
    accounting_event_id IS NULL OR (
        typeof(accounting_event_id) = 'text'
        AND length(accounting_event_id) BETWEEN 1 AND 255
        AND accounting_event_id = trim(accounting_event_id)
        AND instr(accounting_event_id, char(0)) = 0
        AND accounting_event_id NOT GLOB '*[^ -~]*'
    )
)
"""

_ADD_ACCOUNTING_KIND_SQL = """
ALTER TABLE tokens_usage ADD COLUMN accounting_kind TEXT NOT NULL DEFAULT 'charge'
CHECK (accounting_kind IN ('charge', 'rollup'))
"""

_ADD_PARENT_ACCOUNTING_EVENT_ID_SQL = """
ALTER TABLE tokens_usage ADD COLUMN parent_accounting_event_id TEXT CHECK (
    parent_accounting_event_id IS NULL OR (
        typeof(parent_accounting_event_id) = 'text'
        AND length(parent_accounting_event_id) BETWEEN 1 AND 255
        AND parent_accounting_event_id = trim(parent_accounting_event_id)
        AND instr(parent_accounting_event_id, char(0)) = 0
        AND parent_accounting_event_id NOT GLOB '*[^ -~]*'
    )
)
"""

_TOKENS_USAGE_EVENT_INDEX_SQL = """
CREATE UNIQUE INDEX ux_tokens_usage_accounting_event
ON tokens_usage(accounting_event_id) WHERE accounting_event_id IS NOT NULL
"""

_OUTBOX_TABLE_SQL = """
CREATE TABLE accounting_outbox (
    event_id TEXT NOT NULL PRIMARY KEY CHECK (
        typeof(event_id) = 'text'
        AND length(event_id) BETWEEN 1 AND 255
        AND event_id = trim(event_id)
        AND instr(event_id, char(0)) = 0
        AND event_id NOT GLOB '*[^ -~]*'
    ),
    schema_version INTEGER NOT NULL CHECK (
        typeof(schema_version) = 'integer' AND schema_version = 1
    ),
    event_kind TEXT NOT NULL CHECK (event_kind IN ('charge', 'rollup')),
    billing_fingerprint TEXT NOT NULL CHECK (
        typeof(billing_fingerprint) = 'text'
        AND length(billing_fingerprint) = 64
        AND instr(billing_fingerprint, char(0)) = 0
        AND billing_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    api_key_id INTEGER CHECK (
        api_key_id IS NULL OR (
            typeof(api_key_id) = 'integer' AND api_key_id > 0
        )
    ),
    usage_cost_usd REAL NOT NULL CHECK (
        typeof(usage_cost_usd) IN ('integer', 'real')
        AND usage_cost_usd >= 0
        AND usage_cost_usd <= 1.7976931348623157e308
    ),
    total_tokens INTEGER NOT NULL CHECK (
        typeof(total_tokens) = 'integer' AND total_tokens >= 0
    ),
    cost_source TEXT NOT NULL CHECK (
        cost_source IN (
            'upstream',
            'token_registry',
            'operation_configured',
            'operation_default',
            'component_sum',
            'receipt_rollup'
        )
    ),
    request_id TEXT,
    parent_event_id TEXT CHECK (
        parent_event_id IS NULL OR (
            typeof(parent_event_id) = 'text'
            AND length(parent_event_id) BETWEEN 1 AND 255
            AND parent_event_id = trim(parent_event_id)
            AND instr(parent_event_id, char(0)) = 0
            AND parent_event_id NOT GLOB '*[^ -~]*'
        )
    ),
    http_method TEXT NOT NULL CHECK (
        length(http_method) BETWEEN 1 AND 16
        AND instr(http_method, char(0)) = 0
        AND http_method = upper(http_method)
        AND http_method NOT GLOB '*[^A-Z]*'
    ),
    route_template TEXT NOT NULL CHECK (
        length(route_template) BETWEEN 1 AND 512
        AND route_template = trim(route_template)
        AND substr(route_template, 1, 1) = '/'
        AND instr(route_template, char(0)) = 0
        AND instr(route_template, '?') = 0
        AND instr(route_template, '#') = 0
        AND route_template NOT GLOB '*[^ -~]*'
    ),
    occurred_at TEXT NOT NULL,
    created_at TEXT NOT NULL,
    projected_at TEXT,
    projection_attempts INTEGER NOT NULL DEFAULT 0 CHECK (
        typeof(projection_attempts) = 'integer' AND projection_attempts >= 0
    ),
    last_attempt_at TEXT,
    last_error_code TEXT,
    CHECK (event_kind <> 'rollup' OR (usage_cost_usd = 0 AND total_tokens = 0)),
    CHECK (
        (event_kind = 'charge' AND cost_source <> 'receipt_rollup')
        OR (event_kind = 'rollup' AND cost_source = 'receipt_rollup')
    )
)
"""

_OUTBOX_PENDING_INDEX_SQL = """
CREATE INDEX ix_accounting_outbox_pending
ON accounting_outbox(created_at, event_id) WHERE projected_at IS NULL
"""

_OUTBOX_KEY_PENDING_INDEX_SQL = """
CREATE INDEX ix_accounting_outbox_key_pending
ON accounting_outbox(api_key_id, created_at) WHERE projected_at IS NULL
"""

_EVENT_COMPONENTS_V1_TABLE_SQL = f"""
CREATE TABLE accounting_event_components (
    event_id TEXT NOT NULL CHECK (
        typeof(event_id) = 'text'
        AND length(event_id) BETWEEN 1 AND 255
        AND event_id = trim(event_id)
        AND instr(event_id, char(0)) = 0
        AND event_id NOT GLOB '*[^ -~]*'
    ),
    ordinal INTEGER NOT NULL CHECK (
        typeof(ordinal) = 'integer' AND ordinal >= 0
    ),
    provider TEXT NOT NULL CHECK (
        typeof(provider) = 'text'
        AND length(CAST(provider AS BLOB)) BETWEEN 1 AND 512
        AND unicode(substr(provider, 1, 1)) NOT IN (
            {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
        )
        AND unicode(substr(provider, -1, 1)) NOT IN (
            {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
        )
        AND instr(provider, char(0)) = 0
    ),
    model TEXT NOT NULL CHECK (
        typeof(model) = 'text'
        AND length(CAST(model AS BLOB)) BETWEEN 1 AND 512
        AND unicode(substr(model, 1, 1)) NOT IN (
            {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
        )
        AND unicode(substr(model, -1, 1)) NOT IN (
            {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
        )
        AND instr(model, char(0)) = 0
    ),
    prompt_tokens INTEGER NOT NULL CHECK (
        typeof(prompt_tokens) = 'integer' AND prompt_tokens >= 0
    ),
    completion_tokens INTEGER NOT NULL CHECK (
        typeof(completion_tokens) = 'integer' AND completion_tokens >= 0
    ),
    total_tokens INTEGER NOT NULL CHECK (
        typeof(total_tokens) = 'integer' AND total_tokens >= 0
    ),
    reasoning_tokens INTEGER NOT NULL CHECK (
        typeof(reasoning_tokens) = 'integer' AND reasoning_tokens >= 0
    ),
    cached_tokens INTEGER NOT NULL CHECK (
        typeof(cached_tokens) = 'integer' AND cached_tokens >= 0
    ),
    cost_usd REAL NOT NULL CHECK (
        typeof(cost_usd) IN ('integer', 'real')
        AND cost_usd >= 0
        AND cost_usd <= 1.7976931348623157e308
    ),
    cost_source TEXT NOT NULL CHECK (
        cost_source IN (
            'upstream',
            'token_registry',
            'operation_configured',
            'operation_default'
        )
    ),
    component_fingerprint TEXT NOT NULL CHECK (
        typeof(component_fingerprint) = 'text'
        AND length(component_fingerprint) = 64
        AND instr(component_fingerprint, char(0)) = 0
        AND component_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (event_id, ordinal),
    CHECK (total_tokens = prompt_tokens + completion_tokens),
    FOREIGN KEY (event_id) REFERENCES accounting_outbox(event_id) ON DELETE RESTRICT
)
"""

_EVENT_COMPONENTS_V2_TABLE_SQL = f"""
CREATE TABLE accounting_event_components (
    event_id TEXT NOT NULL CHECK (
        typeof(event_id) = 'text'
        AND length(event_id) BETWEEN 1 AND 255
        AND event_id = trim(event_id)
        AND instr(event_id, char(0)) = 0
        AND event_id NOT GLOB '*[^ -~]*'
    ),
    ordinal INTEGER NOT NULL CHECK (
        typeof(ordinal) = 'integer' AND ordinal >= 0
    ),
    component_kind TEXT NOT NULL CHECK (
        component_kind IN ('model', 'operation')
    ),
    provider TEXT CHECK (
        provider IS NULL OR (
            typeof(provider) = 'text'
            AND length(CAST(provider AS BLOB)) BETWEEN 1 AND 512
            AND unicode(substr(provider, 1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND unicode(substr(provider, -1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND instr(provider, char(0)) = 0
        )
    ),
    model TEXT CHECK (
        model IS NULL OR (
            typeof(model) = 'text'
            AND length(CAST(model AS BLOB)) BETWEEN 1 AND 512
            AND unicode(substr(model, 1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND unicode(substr(model, -1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND instr(model, char(0)) = 0
        )
    ),
    operation TEXT CHECK (
        operation IS NULL OR (
            typeof(operation) = 'text'
            AND length(CAST(operation AS BLOB)) BETWEEN 1 AND 512
            AND unicode(substr(operation, 1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND unicode(substr(operation, -1, 1)) NOT IN (
                {_PYTHON_WHITESPACE_CODEPOINTS_SQL}
            )
            AND instr(operation, char(0)) = 0
        )
    ),
    gateway_model TEXT CHECK (
        gateway_model IS NULL OR (
            typeof(gateway_model) = 'text'
            AND length(CAST(gateway_model AS BLOB)) BETWEEN 1 AND 255
            AND {_PYTHON_WHITESPACE_ABSENT_SQL}
            AND instr(gateway_model, char(0)) = 0
        )
    ),
    prompt_tokens INTEGER NOT NULL CHECK (
        typeof(prompt_tokens) = 'integer' AND prompt_tokens >= 0
    ),
    completion_tokens INTEGER NOT NULL CHECK (
        typeof(completion_tokens) = 'integer' AND completion_tokens >= 0
    ),
    total_tokens INTEGER NOT NULL CHECK (
        typeof(total_tokens) = 'integer' AND total_tokens >= 0
    ),
    reasoning_tokens INTEGER NOT NULL CHECK (
        typeof(reasoning_tokens) = 'integer' AND reasoning_tokens >= 0
    ),
    cached_tokens INTEGER NOT NULL CHECK (
        typeof(cached_tokens) = 'integer' AND cached_tokens >= 0
    ),
    cost_usd REAL NOT NULL CHECK (
        typeof(cost_usd) IN ('integer', 'real')
        AND cost_usd >= 0
        AND cost_usd <= 1.7976931348623157e308
    ),
    cost_source TEXT NOT NULL CHECK (
        (
            component_kind = 'model'
            AND cost_source IN (
                'upstream',
                'token_registry',
                'operation_configured',
                'operation_default'
            )
        ) OR (
            component_kind = 'operation'
            AND cost_source IN (
                'upstream',
                'operation_configured',
                'operation_default'
            )
        )
    ),
    component_fingerprint TEXT NOT NULL CHECK (
        typeof(component_fingerprint) = 'text'
        AND length(component_fingerprint) = 64
        AND instr(component_fingerprint, char(0)) = 0
        AND component_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (event_id, ordinal),
    CHECK (total_tokens = prompt_tokens + completion_tokens),
    CHECK (
        (
            component_kind = 'model'
            AND provider IS NOT NULL
            AND model IS NOT NULL
            AND operation IS NULL
            AND gateway_model IS NULL
        ) OR (
            component_kind = 'operation'
            AND provider IS NULL
            AND model IS NULL
            AND operation IS NOT NULL
            AND gateway_model IS NOT NULL
        )
    ),
    CHECK (
        component_kind <> 'operation' OR (
            prompt_tokens = 0
            AND completion_tokens = 0
            AND total_tokens = 0
            AND reasoning_tokens = 0
            AND cached_tokens = 0
        )
    ),
    FOREIGN KEY (event_id) REFERENCES accounting_outbox(event_id) ON DELETE RESTRICT
)
"""

_EVENT_LINKS_TABLE_SQL = """
CREATE TABLE accounting_event_links (
    parent_event_id TEXT NOT NULL CHECK (
        typeof(parent_event_id) = 'text'
        AND length(parent_event_id) BETWEEN 1 AND 255
        AND parent_event_id = trim(parent_event_id)
        AND instr(parent_event_id, char(0)) = 0
        AND parent_event_id NOT GLOB '*[^ -~]*'
    ),
    ordinal INTEGER NOT NULL CHECK (
        typeof(ordinal) = 'integer' AND ordinal >= 0
    ),
    child_event_id TEXT NOT NULL CHECK (
        typeof(child_event_id) = 'text'
        AND length(child_event_id) BETWEEN 1 AND 255
        AND child_event_id = trim(child_event_id)
        AND instr(child_event_id, char(0)) = 0
        AND child_event_id NOT GLOB '*[^ -~]*'
    ),
    child_billing_fingerprint TEXT NOT NULL CHECK (
        typeof(child_billing_fingerprint) = 'text'
        AND length(child_billing_fingerprint) = 64
        AND instr(child_billing_fingerprint, char(0)) = 0
        AND child_billing_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    PRIMARY KEY (parent_event_id, ordinal),
    UNIQUE (parent_event_id, child_event_id),
    CHECK (parent_event_id <> child_event_id),
    FOREIGN KEY (parent_event_id) REFERENCES accounting_outbox(event_id) ON DELETE RESTRICT,
    FOREIGN KEY (child_event_id) REFERENCES accounting_outbox(event_id) ON DELETE RESTRICT
)
"""

_APPLIED_EVENTS_TABLE_SQL = """
CREATE TABLE applied_accounting_events (
    event_id TEXT NOT NULL PRIMARY KEY CHECK (
        typeof(event_id) = 'text'
        AND length(event_id) BETWEEN 1 AND 255
        AND event_id = trim(event_id)
        AND instr(event_id, char(0)) = 0
        AND event_id NOT GLOB '*[^ -~]*'
    ),
    billing_fingerprint TEXT NOT NULL CHECK (
        typeof(billing_fingerprint) = 'text'
        AND length(billing_fingerprint) = 64
        AND instr(billing_fingerprint, char(0)) = 0
        AND billing_fingerprint NOT GLOB '*[^0-9a-f]*'
    ),
    api_key_id INTEGER NOT NULL CHECK (
        typeof(api_key_id) = 'integer' AND api_key_id > 0
    ),
    spend_usd REAL NOT NULL CHECK (
        typeof(spend_usd) IN ('integer', 'real')
        AND spend_usd >= 0
        AND spend_usd <= 1.7976931348623157e308
    ),
    applied_at TEXT NOT NULL,
    sink_kind TEXT NOT NULL CHECK (sink_kind IN ('active', 'tombstone'))
)
"""

_APPLIED_EVENTS_KEY_INDEX_SQL = """
CREATE INDEX ix_applied_accounting_events_key
ON applied_accounting_events(api_key_id, applied_at)
"""

_TOMBSTONES_TABLE_SQL = """
CREATE TABLE api_key_accounting_tombstones (
    api_key_id INTEGER NOT NULL PRIMARY KEY CHECK (
        typeof(api_key_id) = 'integer' AND api_key_id > 0
    ),
    deleted_at TEXT NOT NULL,
    spent_usd REAL NOT NULL CHECK (
        typeof(spent_usd) IN ('integer', 'real')
        AND spent_usd >= 0
        AND spent_usd <= 1.7976931348623157e308
    ),
    last_used_at TEXT
) WITHOUT ROWID
"""

_SOURCE_MIGRATION_V1_SQL = (
    _ADD_ACCOUNTING_EVENT_ID_SQL,
    _ADD_ACCOUNTING_KIND_SQL,
    _ADD_PARENT_ACCOUNTING_EVENT_ID_SQL,
    _TOKENS_USAGE_EVENT_INDEX_SQL,
    _OUTBOX_TABLE_SQL,
    _OUTBOX_PENDING_INDEX_SQL,
    _OUTBOX_KEY_PENDING_INDEX_SQL,
    _EVENT_COMPONENTS_V1_TABLE_SQL,
    _EVENT_LINKS_TABLE_SQL,
)

_RENAME_EVENT_COMPONENTS_V1_SQL = """
ALTER TABLE accounting_event_components
RENAME TO accounting_event_components_v1
"""

_COPY_EVENT_COMPONENTS_V1_TO_V2_SQL = """
INSERT INTO accounting_event_components (
    event_id, ordinal, component_kind, provider, model, operation,
    gateway_model, prompt_tokens, completion_tokens, total_tokens,
    reasoning_tokens, cached_tokens, cost_usd, cost_source,
    component_fingerprint
)
SELECT event_id, ordinal, 'model', provider, model, NULL, NULL,
       prompt_tokens, completion_tokens, total_tokens, reasoning_tokens,
       cached_tokens, cost_usd, cost_source, component_fingerprint
FROM accounting_event_components_v1
ORDER BY event_id COLLATE BINARY, ordinal
"""

_SOURCE_MIGRATION_V2_SQL = (
    _RENAME_EVENT_COMPONENTS_V1_SQL,
    _EVENT_COMPONENTS_V2_TABLE_SQL,
    _COPY_EVENT_COMPONENTS_V1_TO_V2_SQL,
    "DROP TABLE accounting_event_components_v1",
)

_SINK_MIGRATION_SQL = (
    _APPLIED_EVENTS_TABLE_SQL,
    _APPLIED_EVENTS_KEY_INDEX_SQL,
    _TOMBSTONES_TABLE_SQL,
)


def _normalize_sql(value: str) -> str:
    segments = re.split(r"('(?:''|[^'])*')", value.strip().rstrip(";"))
    return "".join(
        segment if index % 2 else re.sub(r"\s+", " ", segment).casefold()
        for index, segment in enumerate(segments)
    )


def _fail_schema(component: str, object_name: str) -> None:
    raise AccountingSchemaError(f"{component} accounting schema mismatch: {object_name}")


def _object_sql(
    conn: sqlite3.Connection,
    *,
    component: str,
    object_type: str,
    object_name: str,
) -> str:
    row = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = ? AND name = ?",
        (object_type, object_name),
    ).fetchone()
    if row is None or not isinstance(row[0], str):
        _fail_schema(component, object_name)
    return row[0]


def _verify_sql_object(
    conn: sqlite3.Connection,
    *,
    component: str,
    object_type: str,
    object_name: str,
    expected_sql: str,
) -> None:
    actual_sql = _object_sql(
        conn,
        component=component,
        object_type=object_type,
        object_name=object_name,
    )
    if _normalize_sql(actual_sql) != _normalize_sql(expected_sql):
        _fail_schema(component, object_name)


def _verify_explicit_indexes(
    conn: sqlite3.Connection,
    *,
    component: str,
    table: str,
    expected: dict[str, str],
) -> None:
    actual = {
        str(row[0]): str(row[1])
        for row in conn.execute(
            """
            SELECT name, sql
            FROM sqlite_master
            WHERE type = 'index' AND tbl_name = ? AND sql IS NOT NULL
            """,
            (table,),
        )
    }
    if set(actual) != set(expected):
        _fail_schema(component, f"{table}.indexes")
    for name, expected_sql in expected.items():
        if _normalize_sql(actual[name]) != _normalize_sql(expected_sql):
            _fail_schema(component, name)


def _verify_unique_indexes(
    conn: sqlite3.Connection,
    *,
    component: str,
    table: str,
    expected: set[tuple[str, int, tuple[str, ...]]],
) -> None:
    actual: set[tuple[str, int, tuple[str, ...]]] = set()
    for name, is_unique, origin, is_partial in conn.execute(
        'SELECT name, "unique", origin, partial FROM pragma_index_list(?)',
        (table,),
    ):
        if not is_unique:
            continue
        columns = tuple(
            str(row[0])
            for row in conn.execute(
                "SELECT name FROM pragma_index_info(?) ORDER BY seqno",
                (name,),
            )
        )
        actual.add((str(origin), int(is_partial), columns))
    if actual != expected:
        _fail_schema(component, f"{table}.unique_indexes")


def _verify_columns(
    conn: sqlite3.Connection,
    *,
    component: str,
    table: str,
    expected: tuple[tuple[str, str, int, str | None, int], ...],
) -> None:
    actual = tuple(
        (str(row[1]), str(row[2]), int(row[3]), row[4], int(row[5]))
        for row in conn.execute(f'PRAGMA table_info("{table}")')
    )
    if actual != expected:
        _fail_schema(component, f"{table}.columns")


def _verify_foreign_keys(
    conn: sqlite3.Connection,
    *,
    component: str,
    table: str,
    expected: set[tuple[str, str, str, str, str, str]],
) -> None:
    actual = {
        (
            str(row[3]),
            str(row[2]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
        )
        for row in conn.execute(f'PRAGMA foreign_key_list("{table}")')
    }
    if actual != expected:
        _fail_schema(component, f"{table}.foreign_keys")


def _verify_migration_ledger(conn: sqlite3.Connection, component: str) -> None:
    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="gateway_schema_migrations",
        expected_sql=_MIGRATIONS_TABLE_SQL,
    )
    _verify_columns(
        conn,
        component=component,
        table="gateway_schema_migrations",
        expected=(
            ("component", "TEXT", 1, None, 1),
            ("version", "INTEGER", 1, None, 2),
            ("applied_at", "TEXT", 1, None, 0),
        ),
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="gateway_schema_migrations",
        expected={},
    )


def _verify_component_versions(
    conn: sqlite3.Connection,
    component: str,
    expected: tuple[int, ...],
) -> None:
    rows = tuple(
        row
        for row in conn.execute(
            """
            SELECT version, applied_at
            FROM gateway_schema_migrations
            WHERE component = ?
            ORDER BY version
            """,
            (component,),
        )
    )
    actual = tuple(row[0] for row in rows)
    if actual != expected or any(
        not isinstance(row[1], str) or not row[1] for row in rows
    ):
        _fail_schema(component, "migration_markers")


def _verify_marker(
    conn: sqlite3.Connection,
    component: str,
    version: int,
) -> None:
    rows = conn.execute(
        """
        SELECT applied_at
        FROM gateway_schema_migrations
        WHERE component = ? AND version = ?
        """,
        (component, version),
    ).fetchall()
    if len(rows) != 1 or not isinstance(rows[0][0], str) or not rows[0][0]:
        _fail_schema(component, "migration_marker")


def _verify_source_schema_version(
    conn: sqlite3.Connection,
    *,
    version: int,
    event_components_sql: str,
    event_component_columns: tuple[tuple[str, str, int, str | None, int], ...],
) -> None:
    component = _SOURCE_COMPONENT
    _verify_migration_ledger(conn, component)
    _verify_component_versions(conn, component, tuple(range(1, version + 1)))
    _verify_marker(conn, component, version)

    token_columns = {
        str(row[1]): (str(row[2]), int(row[3]), row[4], int(row[5]))
        for row in conn.execute('PRAGMA table_info("tokens_usage")')
    }
    expected_token_columns = {
        "accounting_event_id": ("TEXT", 0, None, 0),
        "accounting_kind": ("TEXT", 1, "'charge'", 0),
        "parent_accounting_event_id": ("TEXT", 0, None, 0),
    }
    if any(token_columns.get(name) != value for name, value in expected_token_columns.items()):
        _fail_schema(component, "tokens_usage.columns")

    tokens_usage_sql = _object_sql(
        conn,
        component=component,
        object_type="table",
        object_name="tokens_usage",
    )
    token_column_constraints = {
        "accounting_event_id": """
            accounting_event_id TEXT CHECK (
                accounting_event_id IS NULL OR (
                    typeof(accounting_event_id) = 'text'
                    AND length(accounting_event_id) BETWEEN 1 AND 255
                    AND accounting_event_id = trim(accounting_event_id)
                    AND instr(accounting_event_id, char(0)) = 0
                    AND accounting_event_id NOT GLOB '*[^ -~]*'
                )
            )
        """,
        "accounting_kind": """
            accounting_kind TEXT NOT NULL DEFAULT 'charge'
            CHECK (accounting_kind IN ('charge', 'rollup'))
        """,
        "parent_accounting_event_id": """
            parent_accounting_event_id TEXT CHECK (
                parent_accounting_event_id IS NULL OR (
                    typeof(parent_accounting_event_id) = 'text'
                    AND length(parent_accounting_event_id) BETWEEN 1 AND 255
                    AND parent_accounting_event_id = trim(parent_accounting_event_id)
                    AND instr(parent_accounting_event_id, char(0)) = 0
                    AND parent_accounting_event_id NOT GLOB '*[^ -~]*'
                )
            )
        """,
    }
    normalized_tokens_usage_sql = _normalize_sql(tokens_usage_sql)
    for name, constraint in token_column_constraints.items():
        if _normalize_sql(constraint) not in normalized_tokens_usage_sql:
            _fail_schema(component, f"tokens_usage.{name}")

    _verify_sql_object(
        conn,
        component=component,
        object_type="index",
        object_name="ux_tokens_usage_accounting_event",
        expected_sql=_TOKENS_USAGE_EVENT_INDEX_SQL,
    )
    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="accounting_outbox",
        expected_sql=_OUTBOX_TABLE_SQL,
    )
    _verify_columns(
        conn,
        component=component,
        table="accounting_outbox",
        expected=(
            ("event_id", "TEXT", 1, None, 1),
            ("schema_version", "INTEGER", 1, None, 0),
            ("event_kind", "TEXT", 1, None, 0),
            ("billing_fingerprint", "TEXT", 1, None, 0),
            ("api_key_id", "INTEGER", 0, None, 0),
            ("usage_cost_usd", "REAL", 1, None, 0),
            ("total_tokens", "INTEGER", 1, None, 0),
            ("cost_source", "TEXT", 1, None, 0),
            ("request_id", "TEXT", 0, None, 0),
            ("parent_event_id", "TEXT", 0, None, 0),
            ("http_method", "TEXT", 1, None, 0),
            ("route_template", "TEXT", 1, None, 0),
            ("occurred_at", "TEXT", 1, None, 0),
            ("created_at", "TEXT", 1, None, 0),
            ("projected_at", "TEXT", 0, None, 0),
            ("projection_attempts", "INTEGER", 1, "0", 0),
            ("last_attempt_at", "TEXT", 0, None, 0),
            ("last_error_code", "TEXT", 0, None, 0),
        ),
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="accounting_outbox",
        expected={
            "ix_accounting_outbox_pending": _OUTBOX_PENDING_INDEX_SQL,
            "ix_accounting_outbox_key_pending": _OUTBOX_KEY_PENDING_INDEX_SQL,
        },
    )

    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="accounting_event_components",
        expected_sql=event_components_sql,
    )
    _verify_columns(
        conn,
        component=component,
        table="accounting_event_components",
        expected=event_component_columns,
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="accounting_event_components",
        expected={},
    )
    _verify_foreign_keys(
        conn,
        component=component,
        table="accounting_event_components",
        expected={
            (
                "event_id",
                "accounting_outbox",
                "event_id",
                "NO ACTION",
                "RESTRICT",
                "NONE",
            )
        },
    )

    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="accounting_event_links",
        expected_sql=_EVENT_LINKS_TABLE_SQL,
    )
    _verify_columns(
        conn,
        component=component,
        table="accounting_event_links",
        expected=(
            ("parent_event_id", "TEXT", 1, None, 1),
            ("ordinal", "INTEGER", 1, None, 2),
            ("child_event_id", "TEXT", 1, None, 0),
            ("child_billing_fingerprint", "TEXT", 1, None, 0),
        ),
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="accounting_event_links",
        expected={},
    )
    _verify_unique_indexes(
        conn,
        component=component,
        table="accounting_event_links",
        expected={
            ("pk", 0, ("parent_event_id", "ordinal")),
            ("u", 0, ("parent_event_id", "child_event_id")),
        },
    )
    _verify_foreign_keys(
        conn,
        component=component,
        table="accounting_event_links",
        expected={
            (
                "parent_event_id",
                "accounting_outbox",
                "event_id",
                "NO ACTION",
                "RESTRICT",
                "NONE",
            ),
            (
                "child_event_id",
                "accounting_outbox",
                "event_id",
                "NO ACTION",
                "RESTRICT",
                "NONE",
            ),
        },
    )
    for table in ("accounting_event_components", "accounting_event_links"):
        if conn.execute(f'PRAGMA foreign_key_check("{table}")').fetchone() is not None:
            _fail_schema(component, f"{table}.foreign_key_check")


_EVENT_COMPONENTS_V1_COLUMNS = (
    ("event_id", "TEXT", 1, None, 1),
    ("ordinal", "INTEGER", 1, None, 2),
    ("provider", "TEXT", 1, None, 0),
    ("model", "TEXT", 1, None, 0),
    ("prompt_tokens", "INTEGER", 1, None, 0),
    ("completion_tokens", "INTEGER", 1, None, 0),
    ("total_tokens", "INTEGER", 1, None, 0),
    ("reasoning_tokens", "INTEGER", 1, None, 0),
    ("cached_tokens", "INTEGER", 1, None, 0),
    ("cost_usd", "REAL", 1, None, 0),
    ("cost_source", "TEXT", 1, None, 0),
    ("component_fingerprint", "TEXT", 1, None, 0),
)

_EVENT_COMPONENTS_V2_COLUMNS = (
    ("event_id", "TEXT", 1, None, 1),
    ("ordinal", "INTEGER", 1, None, 2),
    ("component_kind", "TEXT", 1, None, 0),
    ("provider", "TEXT", 0, None, 0),
    ("model", "TEXT", 0, None, 0),
    ("operation", "TEXT", 0, None, 0),
    ("gateway_model", "TEXT", 0, None, 0),
    ("prompt_tokens", "INTEGER", 1, None, 0),
    ("completion_tokens", "INTEGER", 1, None, 0),
    ("total_tokens", "INTEGER", 1, None, 0),
    ("reasoning_tokens", "INTEGER", 1, None, 0),
    ("cached_tokens", "INTEGER", 1, None, 0),
    ("cost_usd", "REAL", 1, None, 0),
    ("cost_source", "TEXT", 1, None, 0),
    ("component_fingerprint", "TEXT", 1, None, 0),
)


def _verify_source_schema_v1(conn: sqlite3.Connection) -> None:
    _verify_source_schema_version(
        conn,
        version=1,
        event_components_sql=_EVENT_COMPONENTS_V1_TABLE_SQL,
        event_component_columns=_EVENT_COMPONENTS_V1_COLUMNS,
    )


def _verify_source_schema(conn: sqlite3.Connection) -> None:
    _verify_source_schema_version(
        conn,
        version=_SOURCE_SCHEMA_VERSION,
        event_components_sql=_EVENT_COMPONENTS_V2_TABLE_SQL,
        event_component_columns=_EVENT_COMPONENTS_V2_COLUMNS,
    )
    if conn.execute(
        """
        SELECT 1 FROM sqlite_master
        WHERE type = 'table' AND name = 'accounting_event_components_v1'
        """
    ).fetchone() is not None:
        _fail_schema(_SOURCE_COMPONENT, "accounting_event_components_v1")


def _verify_sink_schema(conn: sqlite3.Connection) -> None:
    component = _SINK_COMPONENT
    _verify_migration_ledger(conn, component)
    _verify_component_versions(conn, component, (_SINK_SCHEMA_VERSION,))
    _verify_marker(conn, component, _SINK_SCHEMA_VERSION)
    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="applied_accounting_events",
        expected_sql=_APPLIED_EVENTS_TABLE_SQL,
    )
    _verify_columns(
        conn,
        component=component,
        table="applied_accounting_events",
        expected=(
            ("event_id", "TEXT", 1, None, 1),
            ("billing_fingerprint", "TEXT", 1, None, 0),
            ("api_key_id", "INTEGER", 1, None, 0),
            ("spend_usd", "REAL", 1, None, 0),
            ("applied_at", "TEXT", 1, None, 0),
            ("sink_kind", "TEXT", 1, None, 0),
        ),
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="applied_accounting_events",
        expected={"ix_applied_accounting_events_key": _APPLIED_EVENTS_KEY_INDEX_SQL},
    )
    _verify_sql_object(
        conn,
        component=component,
        object_type="table",
        object_name="api_key_accounting_tombstones",
        expected_sql=_TOMBSTONES_TABLE_SQL,
    )
    _verify_columns(
        conn,
        component=component,
        table="api_key_accounting_tombstones",
        expected=(
            ("api_key_id", "INTEGER", 1, None, 1),
            ("deleted_at", "TEXT", 1, None, 0),
            ("spent_usd", "REAL", 1, None, 0),
            ("last_used_at", "TEXT", 0, None, 0),
        ),
    )
    _verify_explicit_indexes(
        conn,
        component=component,
        table="api_key_accounting_tombstones",
        expected={},
    )


def open_accounting_runtime_connection(
    db_path: str | PathLike[str],
    *,
    enable_foreign_keys: bool,
) -> sqlite3.Connection:
    if not isinstance(enable_foreign_keys, bool):
        raise AccountingSchemaError("accounting connection option mismatch")
    timeout_seconds = ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS / 1_000
    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path, timeout=timeout_seconds)
        journal_mode = conn.execute("PRAGMA journal_mode=WAL").fetchone()[0]
        conn.execute("PRAGMA synchronous=FULL")
        conn.execute(f"PRAGMA busy_timeout={ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS}")
        if enable_foreign_keys:
            conn.execute("PRAGMA foreign_keys=ON")

        if str(journal_mode).casefold() != "wal":
            raise AccountingSchemaError("accounting migration requires WAL")
        if conn.execute("PRAGMA synchronous").fetchone()[0] != 2:
            raise AccountingSchemaError("accounting migration requires FULL sync")
        if conn.execute("PRAGMA busy_timeout").fetchone()[0] != ACCOUNTING_MIGRATION_BUSY_TIMEOUT_MS:
            raise AccountingSchemaError("accounting migration timeout mismatch")
        if enable_foreign_keys and conn.execute("PRAGMA foreign_keys").fetchone()[0] != 1:
            raise AccountingSchemaError("accounting source requires foreign keys")
        return conn
    except BaseException as exc:
        if conn is not None:
            try:
                conn.close()
            except BaseException:
                pass
        if not isinstance(exc, Exception):
            raise exc.with_traceback(exc.__traceback__)
        if isinstance(exc, AccountingSchemaError):
            raise
        raise AccountingSchemaError("accounting runtime connection failed") from None


def commit_accounting_transaction(conn: sqlite3.Connection) -> None:
    """Patchable seam for ambiguous-commit recovery tests."""

    conn.commit()


def _open_migration_connection(
    db_path: str | PathLike[str],
    *,
    enable_foreign_keys: bool,
) -> sqlite3.Connection:
    return open_accounting_runtime_connection(
        db_path,
        enable_foreign_keys=enable_foreign_keys,
    )


def _insert_migration_marker(
    conn: sqlite3.Connection,
    component: str,
    version: int,
) -> None:
    applied_at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    conn.execute(
        """
        INSERT INTO gateway_schema_migrations (component, version, applied_at)
        VALUES (?, ?, ?)
        """,
        (component, version, applied_at),
    )


def _execute_migration_statements(
    conn: sqlite3.Connection,
    statements: Iterable[str],
) -> None:
    for statement in statements:
        conn.execute(statement)


def _read_source_v2_component_rows(
    conn: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    return tuple(
        tuple(row)
        for row in conn.execute(
            """
            SELECT event_id, ordinal, component_kind, provider, model,
                   operation, gateway_model, prompt_tokens,
                   completion_tokens, total_tokens, reasoning_tokens,
                   cached_tokens, cost_usd, cost_source,
                   component_fingerprint
            FROM accounting_event_components
            ORDER BY event_id COLLATE BINARY, ordinal
            """
        )
    )


def _apply_source_v2_migration(
    conn: sqlite3.Connection,
) -> tuple[tuple[object, ...], ...]:
    before = tuple(
        tuple(row)
        for row in conn.execute(
            """
            SELECT event_id, ordinal, provider, model, prompt_tokens,
                   completion_tokens, total_tokens, reasoning_tokens,
                   cached_tokens, cost_usd, cost_source,
                   component_fingerprint
            FROM accounting_event_components
            ORDER BY event_id COLLATE BINARY, ordinal
            """
        )
    )
    _execute_migration_statements(conn, _SOURCE_MIGRATION_V2_SQL)
    after = _read_source_v2_component_rows(conn)
    expected = tuple(
        (
            row[0],
            row[1],
            "model",
            row[2],
            row[3],
            None,
            None,
            *row[4:],
        )
        for row in before
    )
    if after != expected:
        _fail_schema(_SOURCE_COMPONENT, "accounting_event_components.migration")
    return expected


def _run_versioned_migrations(
    db_path: str | PathLike[str],
    *,
    component: str,
    enable_foreign_keys: bool,
    steps: tuple[
        tuple[
            int,
            Callable[[sqlite3.Connection], None],
            Callable[[sqlite3.Connection], None] | None,
        ],
        ...,
    ],
    verify: Callable[[sqlite3.Connection], None],
) -> None:
    conn: sqlite3.Connection | None = None
    primary_error: BaseException | None = None
    try:
        conn = _open_migration_connection(
            db_path,
            enable_foreign_keys=enable_foreign_keys,
        )
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            _MIGRATIONS_TABLE_SQL.replace(
                "CREATE TABLE",
                "CREATE TABLE IF NOT EXISTS",
                1,
            )
        )
        _verify_migration_ledger(conn, component)
        versions = tuple(
            row[0]
            for row in conn.execute(
                """
                SELECT version FROM gateway_schema_migrations
                WHERE component = ? ORDER BY version
                """,
                (component,),
            )
        )
        expected_versions = tuple(step[0] for step in steps)
        if versions != expected_versions[: len(versions)]:
            _fail_schema(component, "migration_markers")
        for version, apply_migration, verify_previous in steps[len(versions) :]:
            if verify_previous is not None:
                verify_previous(conn)
            apply_migration(conn)
            _insert_migration_marker(conn, component, version)
        conn.commit()
        verify(conn)
    except BaseException as exc:
        primary_error = exc
        if conn is not None:
            try:
                if conn.in_transaction:
                    conn.rollback()
            except BaseException:
                pass
        if not isinstance(exc, Exception):
            raise exc.with_traceback(exc.__traceback__)
        if isinstance(exc, AccountingSchemaError):
            raise
        raise AccountingSchemaError(f"{component} accounting migration failed") from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException as close_error:
                if primary_error is None:
                    if not isinstance(close_error, Exception):
                        raise close_error.with_traceback(close_error.__traceback__)
                    raise AccountingSchemaError(f"{component} accounting migration failed") from None


def _run_migration(
    db_path: str | PathLike[str],
    *,
    component: str,
    enable_foreign_keys: bool,
    statements: Iterable[str],
    verify: Callable[[sqlite3.Connection], None],
) -> None:
    statements = tuple(statements)
    _run_versioned_migrations(
        db_path,
        component=component,
        enable_foreign_keys=enable_foreign_keys,
        steps=(
            (
                1,
                lambda conn: _execute_migration_statements(conn, statements),
                None,
            ),
        ),
        verify=verify,
    )


def migrate_accounting_source(db_path: str | PathLike[str]) -> None:
    """Install or verify source accounting schema in ``tokens_usage.db``."""

    migrated_rows: tuple[tuple[object, ...], ...] | None = None

    def apply_source_v2(conn: sqlite3.Connection) -> None:
        nonlocal migrated_rows
        migrated_rows = _apply_source_v2_migration(conn)

    def verify_source_v2(conn: sqlite3.Connection) -> None:
        _verify_source_schema(conn)
        if (
            migrated_rows is not None
            and _read_source_v2_component_rows(conn) != migrated_rows
        ):
            _fail_schema(
                _SOURCE_COMPONENT,
                "accounting_event_components.migrated_rows",
            )

    _run_versioned_migrations(
        db_path,
        component=_SOURCE_COMPONENT,
        enable_foreign_keys=True,
        steps=(
            (
                1,
                lambda conn: _execute_migration_statements(
                    conn,
                    _SOURCE_MIGRATION_V1_SQL,
                ),
                None,
            ),
            (2, apply_source_v2, _verify_source_schema_v1),
        ),
        verify=verify_source_v2,
    )


def migrate_accounting_sink(db_path: str | PathLike[str]) -> None:
    """Install or verify sink accounting schema in ``api_keys.db``."""

    _run_migration(
        db_path,
        component=_SINK_COMPONENT,
        enable_foreign_keys=False,
        statements=_SINK_MIGRATION_SQL,
        verify=_verify_sink_schema,
    )
