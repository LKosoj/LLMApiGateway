import logging
import os
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config.paths import resolve_db_dir
from ..services.accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingParentLinkState,
    AccountingProjectionMark,
    AccountingSourceAuditKind,
    AccountingSourceAuditRow,
    AccountingUsage,
    BillingComponent,
    BillingComponentKind,
    CostSource,
    SourceAcceptance,
    SourceStatus,
    StoredAccountingEvent,
)
from ..utils.usage_tracking import FALLBACK_USAGE_SOURCE
from . import accounting_schema
from .write_batcher import RUNTIME_PRAGMAS, WriteBatcher

MAX_USAGE_RECORDS_LIMIT = 100
logger = logging.getLogger(__name__)

INSERT_USAGE_SQL = """
INSERT INTO tokens_usage
(timestamp, duration_ms, prompt_tokens, completion_tokens, total_tokens,
 reasoning_tokens, cached_tokens, cost, gateway_model, operation, model, provider, request_id, is_estimated, usage_source, cost_saved, api_key_id, upstream_key_fingerprint, x_title)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

INSERT_USAGE_IDEMPOTENCY_SQL = """
INSERT OR IGNORE INTO usage_idempotency_keys
(idempotency_key, created_at)
VALUES (?, ?)
"""

_ACCOUNTING_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")

_INSERT_ACCOUNTING_USAGE_SQL = """
INSERT INTO tokens_usage (
    timestamp, duration_ms, ttft_ms, prompt_tokens, completion_tokens, total_tokens,
    reasoning_tokens, cached_tokens, cost, gateway_model, operation, model,
    provider, request_id, is_estimated, cost_unavailable, cost_saved, api_key_id,
    accounting_event_id, accounting_kind, parent_accounting_event_id,
    usage_source, upstream_key_fingerprint, x_title,
    client_ip, client_user_agent, fallback_depth
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ACCOUNTING_OUTBOX_SQL = """
INSERT INTO accounting_outbox (
    event_id, schema_version, event_kind, billing_fingerprint, api_key_id,
    usage_cost_usd, total_tokens, cost_source, request_id, parent_event_id,
    http_method, route_template, occurred_at, created_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ACCOUNTING_COMPONENT_SQL = """
INSERT INTO accounting_event_components (
    event_id, ordinal, component_kind, provider, model, operation,
    gateway_model, prompt_tokens, completion_tokens, total_tokens,
    reasoning_tokens, cached_tokens, cost_usd, cost_source,
    component_fingerprint
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_ACCOUNTING_LINK_SQL = """
INSERT INTO accounting_event_links (
    parent_event_id, ordinal, child_event_id, child_billing_fingerprint
) VALUES (?, ?, ?, ?)
"""

_SELECT_ACCOUNTING_USAGE_SQL = """
SELECT id, timestamp, duration_ms, ttft_ms, prompt_tokens, completion_tokens,
       total_tokens, reasoning_tokens, cached_tokens, cost, gateway_model,
       operation, model, provider, request_id, is_estimated, cost_unavailable,
       cost_saved, api_key_id, accounting_event_id, accounting_kind,
       parent_accounting_event_id
FROM tokens_usage
WHERE accounting_event_id = ?
"""

_SELECT_ACCOUNTING_OUTBOX_SQL = """
SELECT event_id, schema_version, event_kind, billing_fingerprint, api_key_id,
       usage_cost_usd, total_tokens, cost_source, request_id, parent_event_id,
       http_method, route_template, occurred_at, created_at, projected_at,
       projection_attempts, last_attempt_at, last_error_code
FROM accounting_outbox
WHERE event_id = ?
"""

_SELECT_ACCOUNTING_SOURCE_AUDIT_PAGE_SQL = """
WITH outbox_ids(event_id) AS (
    SELECT event_id
    FROM accounting_outbox
    WHERE event_id COLLATE BINARY > :after_event_id
    ORDER BY event_id COLLATE BINARY
    LIMIT :limit
),
usage_ids(event_id) AS (
    SELECT accounting_event_id
    FROM tokens_usage
    WHERE accounting_event_id IS NOT NULL
      AND accounting_event_id COLLATE BINARY > :after_event_id
    ORDER BY accounting_event_id COLLATE BINARY
    LIMIT :limit
),
candidate_ids(event_id) AS (
    SELECT event_id
    FROM outbox_ids

    UNION

    SELECT event_id
    FROM usage_ids
),
page_ids(event_id) AS (
    SELECT event_id
    FROM candidate_ids
    ORDER BY event_id COLLATE BINARY
    LIMIT :limit
),
usage_summary AS (
    SELECT usage.accounting_event_id AS event_id,
           COUNT(*) AS usage_count,
           MIN(usage.id) AS usage_id,
           MIN(usage.duration_ms) AS usage_duration_ms,
           MIN(usage.ttft_ms) AS usage_ttft_ms,
           MIN(usage.prompt_tokens) AS usage_prompt_tokens,
           MIN(usage.completion_tokens) AS usage_completion_tokens,
           MIN(usage.accounting_kind) AS usage_kind,
           MIN(usage.api_key_id) AS usage_api_key_id,
           MIN(usage.cost) AS usage_cost,
           MIN(usage.total_tokens) AS usage_total_tokens,
           MIN(usage.reasoning_tokens) AS usage_reasoning_tokens,
           MIN(usage.cached_tokens) AS usage_cached_tokens,
           MIN(usage.gateway_model) AS usage_gateway_model,
           MIN(usage.operation) AS usage_operation,
           MIN(usage.model) AS usage_model,
           MIN(usage.provider) AS usage_provider,
           MIN(usage.is_estimated) AS usage_is_estimated,
           MIN(usage.cost_unavailable) AS usage_cost_unavailable,
           MIN(usage.cost_saved) AS usage_cost_saved,
           MIN(usage.parent_accounting_event_id) AS usage_parent_event_id,
           MIN(usage.timestamp) AS usage_timestamp,
           MIN(usage.request_id) AS usage_request_id
    FROM page_ids AS page
    CROSS JOIN tokens_usage AS usage
    WHERE usage.accounting_event_id = page.event_id
    GROUP BY usage.accounting_event_id
),
outgoing_link_summary AS (
    SELECT links.parent_event_id AS event_id,
           COUNT(*) AS outgoing_link_count,
           MIN(links.ordinal) AS minimum_ordinal,
           MAX(links.ordinal) AS maximum_ordinal,
           SUM(
               CASE
                   WHEN parent.event_id IS NULL
                     OR child.event_id IS NULL
                     OR child.parent_event_id IS NOT links.parent_event_id
                     OR child.billing_fingerprint IS NOT links.child_billing_fingerprint
                     OR child.api_key_id IS NOT parent.api_key_id
                       THEN 1
                   ELSE 0
               END
           ) AS invalid_outgoing_links
    FROM page_ids AS page
    CROSS JOIN accounting_event_links AS links
    LEFT JOIN accounting_outbox AS parent ON parent.event_id = links.parent_event_id
    LEFT JOIN accounting_outbox AS child ON child.event_id = links.child_event_id
    WHERE links.parent_event_id = page.event_id
    GROUP BY links.parent_event_id
)
SELECT page.event_id,
       outbox.event_id AS outbox_event_id,
       outbox.schema_version,
       outbox.event_kind,
       outbox.billing_fingerprint,
       outbox.api_key_id,
       outbox.usage_cost_usd,
       outbox.total_tokens AS outbox_total_tokens,
       outbox.cost_source,
       outbox.request_id AS outbox_request_id,
       outbox.parent_event_id,
       outbox.http_method,
       outbox.route_template,
       outbox.occurred_at,
       outbox.projected_at,
       outbox.projection_attempts,
       outbox.last_attempt_at,
       outbox.last_error_code,
       usage.usage_count,
       usage.usage_id,
       usage.usage_duration_ms,
       usage.usage_ttft_ms,
       usage.usage_prompt_tokens,
       usage.usage_completion_tokens,
       usage.usage_kind,
       usage.usage_api_key_id,
       usage.usage_cost,
       usage.usage_total_tokens,
       usage.usage_reasoning_tokens,
       usage.usage_cached_tokens,
       usage.usage_gateway_model,
       usage.usage_operation,
       usage.usage_model,
       usage.usage_provider,
       usage.usage_is_estimated,
       usage.usage_cost_unavailable,
       usage.usage_cost_saved,
       usage.usage_parent_event_id,
       usage.usage_timestamp,
       usage.usage_request_id,
       parent.event_id AS parent_outbox_event_id,
       parent.event_kind AS parent_event_kind,
       parent.api_key_id AS parent_api_key_id,
       CASE WHEN expected_link.child_event_id IS NULL THEN NULL ELSE 1 END
           AS link_count,
       expected_link.parent_event_id AS link_parent_event_id,
       expected_link.child_billing_fingerprint AS link_fingerprint,
       outgoing.outgoing_link_count,
       outgoing.minimum_ordinal,
       outgoing.maximum_ordinal,
       outgoing.invalid_outgoing_links
FROM page_ids AS page
LEFT JOIN accounting_outbox AS outbox ON outbox.event_id = page.event_id
LEFT JOIN usage_summary AS usage ON usage.event_id = page.event_id
LEFT JOIN accounting_outbox AS parent ON parent.event_id = outbox.parent_event_id
LEFT JOIN accounting_event_links AS expected_link
       ON expected_link.parent_event_id = outbox.parent_event_id
      AND expected_link.child_event_id = page.event_id
LEFT JOIN outgoing_link_summary AS outgoing ON outgoing.event_id = page.event_id
ORDER BY page.event_id COLLATE BINARY
"""


def _accounting_error(code: AccountingErrorCode) -> AccountingError:
    return AccountingError(code)


def _raise_accounting_conflict() -> None:
    raise _accounting_error(AccountingErrorCode.FINGERPRINT_CONFLICT) from None


def _validate_accounting_event_id(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT) from None
    if not 1 <= len(encoded) <= 255 or any(not 0x20 <= byte <= 0x7E for byte in encoded):
        raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
    return value


def _validate_accounting_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _ACCOUNTING_FINGERPRINT_RE.fullmatch(value) is None:
        raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
    return value


def _normalize_accounting_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
    return value.astimezone(timezone.utc)


def _parse_accounting_datetime(value: object) -> datetime:
    if not isinstance(value, str):
        _raise_accounting_conflict()
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        _raise_accounting_conflict()
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        _raise_accounting_conflict()
    normalized = parsed.astimezone(timezone.utc)
    if normalized.isoformat() != value:
        _raise_accounting_conflict()
    return normalized


def _parse_optional_accounting_datetime(value: object) -> datetime | None:
    if value is None:
        return None
    return _parse_accounting_datetime(value)


def _open_accounting_source_connection(db_path) -> sqlite3.Connection:
    conn = accounting_schema.open_accounting_runtime_connection(
        db_path,
        enable_foreign_keys=True,
    )
    conn.row_factory = sqlite3.Row
    return conn


def _close_accounting_source_connection(
    conn: sqlite3.Connection,
    *,
    primary_error: BaseException | None = None,
) -> None:
    try:
        conn.close()
    except BaseException as close_error:
        if primary_error is not None:
            if isinstance(primary_error, Exception) and not isinstance(
                close_error,
                Exception,
            ):
                raise
            return
        if isinstance(close_error, Exception):
            raise _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED) from None
        raise


def _rollback_accounting_source_connection(
    conn: sqlite3.Connection,
    *,
    primary_error: BaseException,
) -> None:
    try:
        if conn.in_transaction:
            conn.rollback()
    except BaseException as rollback_error:
        if isinstance(primary_error, Exception) and not isinstance(
            rollback_error,
            Exception,
        ):
            raise


def _get_accounting_outbox_fingerprint(
    conn: sqlite3.Connection,
    event_id: str,
) -> str | None:
    row = conn.execute(
        "SELECT billing_fingerprint FROM accounting_outbox WHERE event_id = ?",
        (event_id,),
    ).fetchone()
    if row is None:
        return None
    fingerprint = row["billing_fingerprint"]
    if not isinstance(fingerprint, str) or _ACCOUNTING_FINGERPRINT_RE.fullmatch(fingerprint) is None:
        _raise_accounting_conflict()
    return fingerprint


def _insert_accounting_usage(
    conn: sqlite3.Connection,
    event: AccountingEvent,
) -> int:
    usage = event.usage
    usage_source = FALLBACK_USAGE_SOURCE if usage.is_estimated else None
    cursor = conn.execute(
        _INSERT_ACCOUNTING_USAGE_SQL,
        (
            event.occurred_at.isoformat(),
            usage.duration_ms,
            usage.ttft_ms,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.reasoning_tokens,
            usage.cached_tokens,
            usage.cost,
            event.gateway_model,
            event.operation,
            event.model,
            event.provider,
            event.request_id,
            int(usage.is_estimated),
            int(usage.cost_unavailable),
            usage.cost_saved,
            event.api_key_id,
            event.event_id,
            event.kind.value,
            event.parent_event_id,
            usage_source,
            event.upstream_key_fingerprint,
            event.x_title,
            event.client_ip,
            event.client_user_agent,
            event.fallback_depth,
        ),
    )
    usage_row_id = cursor.lastrowid
    if not isinstance(usage_row_id, int) or usage_row_id <= 0:
        _raise_accounting_conflict()
    return usage_row_id


_UPSERT_USAGE_HOURLY_SQL = """
INSERT INTO usage_hourly (
    hour, requests, prompt_tokens, completion_tokens, total_tokens,
    cost, cost_saved, duration_ms_sum, duration_count
) VALUES (?, 1, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(hour) DO UPDATE SET
    requests = requests + 1,
    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
    completion_tokens = completion_tokens + excluded.completion_tokens,
    total_tokens = total_tokens + excluded.total_tokens,
    cost = cost + excluded.cost,
    cost_saved = cost_saved + excluded.cost_saved,
    duration_ms_sum = duration_ms_sum + excluded.duration_ms_sum,
    duration_count = duration_count + excluded.duration_count
"""

_UPSERT_USAGE_LIFETIME_SQL = """
INSERT INTO usage_lifetime (
    id, requests, prompt_tokens, completion_tokens, total_tokens,
    cost, cost_saved, first_event_at
) VALUES (1, 1, ?, ?, ?, ?, ?, ?)
ON CONFLICT(id) DO UPDATE SET
    requests = requests + 1,
    prompt_tokens = prompt_tokens + excluded.prompt_tokens,
    completion_tokens = completion_tokens + excluded.completion_tokens,
    total_tokens = total_tokens + excluded.total_tokens,
    cost = cost + excluded.cost,
    cost_saved = cost_saved + excluded.cost_saved,
    first_event_at = CASE
        WHEN first_event_at IS NULL OR excluded.first_event_at < first_event_at
        THEN excluded.first_event_at
        ELSE first_event_at
    END
"""


def _upsert_usage_aggregates(
    conn: sqlite3.Connection,
    event: AccountingEvent,
) -> None:
    """Fold one accepted event into the durable hourly/lifetime aggregates.

    Runs inside the accept_accounting_event transaction, strictly on the
    ACCEPTED path: duplicate re-deliveries roll back before the raw insert and
    therefore never reach this fold, so aggregates count each event once.
    """
    usage = event.usage
    occurred_at = event.occurred_at.isoformat()
    hour = occurred_at[:13] + ":00:00"
    duration_ms = usage.duration_ms if usage.duration_ms is not None else 0
    duration_count = 1 if usage.duration_ms is not None else 0
    conn.execute(
        _UPSERT_USAGE_HOURLY_SQL,
        (
            hour,
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cost,
            usage.cost_saved,
            duration_ms,
            duration_count,
        ),
    )
    conn.execute(
        _UPSERT_USAGE_LIFETIME_SQL,
        (
            usage.prompt_tokens,
            usage.completion_tokens,
            usage.total_tokens,
            usage.cost,
            usage.cost_saved,
            occurred_at,
        ),
    )


def _insert_accounting_outbox(
    conn: sqlite3.Connection,
    event: AccountingEvent,
    created_at: datetime,
) -> None:
    conn.execute(
        _INSERT_ACCOUNTING_OUTBOX_SQL,
        (
            event.event_id,
            event.version,
            event.kind.value,
            event.billing_fingerprint,
            event.api_key_id,
            event.usage.cost,
            event.usage.total_tokens,
            event.cost_source.value,
            event.request_id,
            event.parent_event_id,
            event.method,
            event.route_template,
            event.occurred_at.isoformat(),
            created_at.isoformat(),
        ),
    )


def _validate_rollup_children(
    conn: sqlite3.Connection,
    event: AccountingEvent,
) -> None:
    for child_event_id, child_fingerprint in zip(
        event.child_event_ids,
        event.child_fingerprints,
        strict=True,
    ):
        row = conn.execute(
            """
            SELECT billing_fingerprint, api_key_id, parent_event_id
            FROM accounting_outbox
            WHERE event_id = ?
            """,
            (child_event_id,),
        ).fetchone()
        if (
            row is None
            or row["billing_fingerprint"] != child_fingerprint
            or row["api_key_id"] != event.api_key_id
            or row["parent_event_id"] != event.event_id
        ):
            _raise_accounting_conflict()


def _insert_accounting_manifests(
    conn: sqlite3.Connection,
    event: AccountingEvent,
) -> None:
    for ordinal, component in enumerate(event.components):
        usage = component.usage
        conn.execute(
            _INSERT_ACCOUNTING_COMPONENT_SQL,
            (
                event.event_id,
                ordinal,
                component.component_kind.value,
                component.provider,
                component.model,
                component.operation,
                component.gateway_model,
                usage.prompt_tokens,
                usage.completion_tokens,
                usage.total_tokens,
                usage.reasoning_tokens,
                usage.cached_tokens,
                usage.cost,
                component.cost_source.value,
                component.billing_fingerprint,
            ),
        )
    if event.kind is not AccountingEventKind.ROLLUP:
        return
    _validate_rollup_children(conn, event)
    for ordinal, (child_event_id, child_fingerprint) in enumerate(
        zip(event.child_event_ids, event.child_fingerprints, strict=True)
    ):
        conn.execute(
            _INSERT_ACCOUNTING_LINK_SQL,
            (event.event_id, ordinal, child_event_id, child_fingerprint),
        )


def _accounting_components_from_rows(
    rows: list[sqlite3.Row],
) -> tuple[BillingComponent, ...]:
    components: list[BillingComponent] = []
    try:
        for ordinal, row in enumerate(rows):
            if row["ordinal"] != ordinal:
                _raise_accounting_conflict()
            component = BillingComponent(
                provider=row["provider"],
                model=row["model"],
                usage=AccountingUsage(
                    prompt_tokens=row["prompt_tokens"],
                    completion_tokens=row["completion_tokens"],
                    total_tokens=row["total_tokens"],
                    reasoning_tokens=row["reasoning_tokens"],
                    cached_tokens=row["cached_tokens"],
                    cost=row["cost_usd"],
                ),
                cost_source=CostSource(row["cost_source"]),
                component_kind=BillingComponentKind(row["component_kind"]),
                operation=row["operation"],
                gateway_model=row["gateway_model"],
            )
            if component.billing_fingerprint != row["component_fingerprint"]:
                _raise_accounting_conflict()
            components.append(component)
        return tuple(components)
    except AccountingError as exc:
        if exc.code is AccountingErrorCode.FINGERPRINT_CONFLICT:
            raise
        _raise_accounting_conflict()
    except (IndexError, KeyError, TypeError, ValueError):
        _raise_accounting_conflict()


def _read_accounting_components(
    conn: sqlite3.Connection,
    event_id: str,
) -> tuple[BillingComponent, ...]:
    rows = conn.execute(
        """
        SELECT ordinal, component_kind, provider, model, operation,
               gateway_model, prompt_tokens, completion_tokens, total_tokens,
               reasoning_tokens, cached_tokens, cost_usd, cost_source,
               component_fingerprint
        FROM accounting_event_components
        WHERE event_id = ?
        ORDER BY ordinal
        """,
        (event_id,),
    ).fetchall()
    return _accounting_components_from_rows(rows)


def _accounting_links_from_rows(
    rows: list[sqlite3.Row],
    *,
    event_id: str,
    api_key_id: int | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    child_event_ids: list[str] = []
    child_fingerprints: list[str] = []
    for ordinal, row in enumerate(rows):
        if (
            row["ordinal"] != ordinal
            or row["stored_child_fingerprint"] is None
            or row["child_billing_fingerprint"] != row["stored_child_fingerprint"]
            or row["child_api_key_id"] != api_key_id
            or row["child_parent_event_id"] != event_id
        ):
            _raise_accounting_conflict()
        child_event_ids.append(row["child_event_id"])
        child_fingerprints.append(row["child_billing_fingerprint"])
    return tuple(child_event_ids), tuple(child_fingerprints)


def _read_accounting_links(
    conn: sqlite3.Connection,
    event_id: str,
    api_key_id: int | None,
) -> tuple[tuple[str, ...], tuple[str, ...]]:
    rows = conn.execute(
        """
        SELECT links.ordinal, links.child_event_id,
               links.child_billing_fingerprint,
               child.billing_fingerprint AS stored_child_fingerprint,
               child.api_key_id AS child_api_key_id,
               child.parent_event_id AS child_parent_event_id
        FROM accounting_event_links AS links
        LEFT JOIN accounting_outbox AS child
          ON child.event_id = links.child_event_id
        WHERE links.parent_event_id = ?
        ORDER BY links.ordinal
        """,
        (event_id,),
    ).fetchall()
    return _accounting_links_from_rows(
        rows,
        event_id=event_id,
        api_key_id=api_key_id,
    )


def _read_accounting_audit_manifests(
    conn: sqlite3.Connection,
    rows: list[sqlite3.Row],
) -> dict[
    str,
    tuple[
        tuple[BillingComponent, ...],
        tuple[str, ...],
        tuple[str, ...],
    ],
]:
    api_key_ids: dict[str, int | None] = {}
    try:
        for row in rows:
            if row["outbox_event_id"] is None:
                continue
            event_id = _validate_accounting_event_id(row["event_id"])
            if row["outbox_event_id"] != event_id or event_id in api_key_ids:
                _raise_accounting_conflict()
            api_key_ids[event_id] = row["api_key_id"]
    except AccountingError:
        _raise_accounting_conflict()
    except (IndexError, KeyError, TypeError):
        _raise_accounting_conflict()
    if not api_key_ids:
        return {}

    placeholders = ", ".join("?" for _ in api_key_ids)
    component_groups: dict[str, list[sqlite3.Row]] = {
        event_id: [] for event_id in api_key_ids
    }
    component_rows = conn.execute(
        f"""
        SELECT event_id, ordinal, component_kind, provider, model, operation,
               gateway_model, prompt_tokens, completion_tokens, total_tokens,
               reasoning_tokens, cached_tokens, cost_usd, cost_source,
               component_fingerprint
        FROM accounting_event_components
        WHERE event_id IN ({placeholders})
        ORDER BY event_id COLLATE BINARY, ordinal
        """,
        tuple(api_key_ids),
    ).fetchall()
    for component_row in component_rows:
        event_id = component_row["event_id"]
        if event_id not in component_groups:
            _raise_accounting_conflict()
        component_groups[event_id].append(component_row)

    link_groups: dict[str, list[sqlite3.Row]] = {
        event_id: [] for event_id in api_key_ids
    }
    link_rows = conn.execute(
        f"""
        SELECT links.parent_event_id AS event_id, links.ordinal,
               links.child_event_id, links.child_billing_fingerprint,
               child.billing_fingerprint AS stored_child_fingerprint,
               child.api_key_id AS child_api_key_id,
               child.parent_event_id AS child_parent_event_id
        FROM accounting_event_links AS links
        LEFT JOIN accounting_outbox AS child
          ON child.event_id = links.child_event_id
        WHERE links.parent_event_id IN ({placeholders})
        ORDER BY links.parent_event_id COLLATE BINARY, links.ordinal
        """,
        tuple(api_key_ids),
    ).fetchall()
    for link_row in link_rows:
        event_id = link_row["event_id"]
        if event_id not in link_groups:
            _raise_accounting_conflict()
        link_groups[event_id].append(link_row)

    manifests = {}
    try:
        for event_id, api_key_id in api_key_ids.items():
            child_event_ids, child_fingerprints = _accounting_links_from_rows(
                link_groups[event_id],
                event_id=event_id,
                api_key_id=api_key_id,
            )
            manifests[event_id] = (
                _accounting_components_from_rows(component_groups[event_id]),
                child_event_ids,
                child_fingerprints,
            )
        return manifests
    except AccountingError:
        _raise_accounting_conflict()
    except (IndexError, KeyError, TypeError, ValueError):
        _raise_accounting_conflict()


def _projection_mark_from_outbox(outbox: sqlite3.Row) -> AccountingProjectionMark:
    try:
        return AccountingProjectionMark(
            event_id=outbox["event_id"],
            billing_fingerprint=outbox["billing_fingerprint"],
            projected_at=_parse_optional_accounting_datetime(outbox["projected_at"]),
            projection_attempts=outbox["projection_attempts"],
            last_attempt_at=_parse_optional_accounting_datetime(outbox["last_attempt_at"]),
            last_error_code=(
                None
                if outbox["last_error_code"] is None
                else AccountingErrorCode(outbox["last_error_code"])
            ),
        )
    except AccountingError:
        _raise_accounting_conflict()
    except (TypeError, ValueError):
        _raise_accounting_conflict()


def _source_audit_count(value: object) -> int:
    if value is None:
        return 0
    if type(value) is not int or value <= 0:
        _raise_accounting_conflict()
    return value


def _source_audit_parent_link_state(
    row: sqlite3.Row,
    *,
    billing_fingerprint: str,
    api_key_id: int | None,
    parent_event_id: str | None,
) -> AccountingParentLinkState:
    link_count = _source_audit_count(row["link_count"])
    if parent_event_id is None:
        if link_count != 0:
            _raise_accounting_conflict()
        return AccountingParentLinkState.NOT_APPLICABLE

    if row["parent_outbox_event_id"] is None:
        if link_count != 0:
            _raise_accounting_conflict()
        return AccountingParentLinkState.UNROLLED

    if (
        row["parent_outbox_event_id"] != parent_event_id
        or row["parent_event_kind"] != AccountingEventKind.ROLLUP.value
        or row["parent_api_key_id"] != api_key_id
        or link_count != 1
        or row["link_parent_event_id"] != parent_event_id
        or row["link_fingerprint"] != billing_fingerprint
    ):
        _raise_accounting_conflict()
    return AccountingParentLinkState.LINKED


def _validate_source_audit_outgoing_manifest(
    row: sqlite3.Row,
    *,
    event_kind: AccountingEventKind,
) -> None:
    outgoing_count = _source_audit_count(row["outgoing_link_count"])
    if event_kind is AccountingEventKind.CHARGE:
        if outgoing_count != 0:
            _raise_accounting_conflict()
        return

    invalid_count = row["invalid_outgoing_links"]
    minimum_ordinal = row["minimum_ordinal"]
    maximum_ordinal = row["maximum_ordinal"]
    if (
        outgoing_count == 0
        or type(invalid_count) is not int
        or invalid_count != 0
        or type(minimum_ordinal) is not int
        or minimum_ordinal != 0
        or type(maximum_ordinal) is not int
        or maximum_ordinal != outgoing_count - 1
    ):
        _raise_accounting_conflict()


def _source_audit_row_from_db(
    row: sqlite3.Row,
    *,
    components: tuple[BillingComponent, ...],
    child_event_ids: tuple[str, ...],
    child_fingerprints: tuple[str, ...],
) -> AccountingSourceAuditRow:
    try:
        event_id = _validate_accounting_event_id(row["event_id"])
        usage_count = _source_audit_count(row["usage_count"])
        link_count = _source_audit_count(row["link_count"])

        if row["outbox_event_id"] is None:
            if (
                usage_count != 1
                or link_count != 0
                or _source_audit_count(row["outgoing_link_count"]) != 0
            ):
                _raise_accounting_conflict()
            usage_parent_event_id = row["usage_parent_event_id"]
            if usage_parent_event_id is not None:
                _validate_accounting_event_id(usage_parent_event_id)
            usage_total_tokens = row["usage_total_tokens"]
            if (
                isinstance(usage_total_tokens, bool)
                or not isinstance(usage_total_tokens, int)
                or usage_total_tokens < 0
            ):
                _raise_accounting_conflict()
            _parse_accounting_datetime(row["usage_timestamp"])
            return AccountingSourceAuditRow(
                event_id=event_id,
                row_kind=AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX,
                event_kind=AccountingEventKind(row["usage_kind"]),
                billing_fingerprint=None,
                api_key_id=row["usage_api_key_id"],
                spend_usd=row["usage_cost"],
                projected_at=None,
                usage_present=True,
                parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
            )

        if row["outbox_event_id"] != event_id or usage_count > 1:
            _raise_accounting_conflict()
        projection_mark = _projection_mark_from_outbox(row)
        event_kind = AccountingEventKind(row["event_kind"])
        cost_source = CostSource(row["cost_source"])
        total_tokens = row["outbox_total_tokens"]
        if (
            isinstance(total_tokens, bool)
            or not isinstance(total_tokens, int)
            or total_tokens < 0
        ):
            _raise_accounting_conflict()
        if event_kind is AccountingEventKind.ROLLUP:
            if (
                cost_source is not CostSource.RECEIPT_ROLLUP
                or row["usage_cost_usd"] != 0
                or total_tokens != 0
                or components
                or not child_event_ids
            ):
                _raise_accounting_conflict()
        elif (
            cost_source is CostSource.RECEIPT_ROLLUP
            or child_event_ids
            or bool(components) != (cost_source is CostSource.COMPONENT_SUM)
        ):
            _raise_accounting_conflict()
        _validate_source_audit_outgoing_manifest(row, event_kind=event_kind)

        _parse_accounting_datetime(row["occurred_at"])
        parent_event_id = row["parent_event_id"]
        if parent_event_id is not None:
            parent_event_id = _validate_accounting_event_id(parent_event_id)

        if usage_count == 1:
            event = _accounting_event_from_rows(
                {
                    "event_id": event_id,
                    "schema_version": row["schema_version"],
                    "event_kind": row["event_kind"],
                    "billing_fingerprint": row["billing_fingerprint"],
                    "api_key_id": row["api_key_id"],
                    "usage_cost_usd": row["usage_cost_usd"],
                    "total_tokens": total_tokens,
                    "cost_source": row["cost_source"],
                    "request_id": row["outbox_request_id"],
                    "parent_event_id": parent_event_id,
                    "http_method": row["http_method"],
                    "route_template": row["route_template"],
                    "occurred_at": row["occurred_at"],
                },
                {
                    "id": row["usage_id"],
                    "timestamp": row["usage_timestamp"],
                    "duration_ms": row["usage_duration_ms"],
                    "ttft_ms": row["usage_ttft_ms"],
                    "prompt_tokens": row["usage_prompt_tokens"],
                    "completion_tokens": row["usage_completion_tokens"],
                    "total_tokens": row["usage_total_tokens"],
                    "reasoning_tokens": row["usage_reasoning_tokens"],
                    "cached_tokens": row["usage_cached_tokens"],
                    "cost": row["usage_cost"],
                    "gateway_model": row["usage_gateway_model"],
                    "operation": row["usage_operation"],
                    "model": row["usage_model"],
                    "provider": row["usage_provider"],
                    "request_id": row["usage_request_id"],
                    "is_estimated": row["usage_is_estimated"],
                    "cost_unavailable": row["usage_cost_unavailable"],
                    "cost_saved": row["usage_cost_saved"],
                    "api_key_id": row["usage_api_key_id"],
                    "accounting_event_id": event_id,
                    "accounting_kind": row["usage_kind"],
                    "parent_accounting_event_id": row["usage_parent_event_id"],
                },
                components=components,
                child_event_ids=child_event_ids,
                child_fingerprints=child_fingerprints,
            )
            event_kind = event.kind
            parent_event_id = event.parent_event_id

        parent_link_state = _source_audit_parent_link_state(
            row,
            billing_fingerprint=projection_mark.billing_fingerprint,
            api_key_id=row["api_key_id"],
            parent_event_id=parent_event_id,
        )
        return AccountingSourceAuditRow(
            event_id=event_id,
            row_kind=AccountingSourceAuditKind.OUTBOX,
            event_kind=event_kind,
            billing_fingerprint=projection_mark.billing_fingerprint,
            api_key_id=row["api_key_id"],
            spend_usd=row["usage_cost_usd"],
            projected_at=projection_mark.projected_at,
            usage_present=usage_count == 1,
            parent_link_state=parent_link_state,
        )
    except AccountingError:
        _raise_accounting_conflict()
    except (KeyError, TypeError, ValueError):
        _raise_accounting_conflict()


def _read_projection_metadata(
    outbox: sqlite3.Row,
) -> tuple[datetime, datetime | None, int, datetime | None, AccountingErrorCode | None]:
    mark = _projection_mark_from_outbox(outbox)
    return (
        _parse_accounting_datetime(outbox["created_at"]),
        mark.projected_at,
        mark.projection_attempts,
        mark.last_attempt_at,
        mark.last_error_code,
    )


def _read_accounting_projection_mark(
    conn: sqlite3.Connection,
    event_id: str,
) -> AccountingProjectionMark | None:
    outbox = conn.execute(_SELECT_ACCOUNTING_OUTBOX_SQL, (event_id,)).fetchone()
    if outbox is None:
        return None
    return _projection_mark_from_outbox(outbox)


def _read_retained_accounting_event(
    conn: sqlite3.Connection,
    outbox: sqlite3.Row,
    fallback_event: AccountingEvent,
) -> StoredAccountingEvent:
    created_at, projected_at, attempts, last_attempt_at, last_error_code = _read_projection_metadata(outbox)
    if projected_at is None:
        _raise_accounting_conflict()
    persisted_envelope = (
        outbox["event_id"],
        outbox["schema_version"],
        outbox["event_kind"],
        outbox["billing_fingerprint"],
        outbox["api_key_id"],
        outbox["usage_cost_usd"],
        outbox["total_tokens"],
        outbox["cost_source"],
        outbox["parent_event_id"],
        outbox["http_method"],
        outbox["route_template"],
    )
    incoming_envelope = (
        fallback_event.event_id,
        fallback_event.version,
        fallback_event.kind.value,
        fallback_event.billing_fingerprint,
        fallback_event.api_key_id,
        fallback_event.usage.cost,
        fallback_event.usage.total_tokens,
        fallback_event.cost_source.value,
        fallback_event.parent_event_id,
        fallback_event.method,
        fallback_event.route_template,
    )
    if persisted_envelope != incoming_envelope:
        _raise_accounting_conflict()
    persisted_components = _read_accounting_components(conn, fallback_event.event_id)
    if tuple(component.billing_fingerprint for component in persisted_components) != tuple(
        component.billing_fingerprint for component in fallback_event.components
    ):
        _raise_accounting_conflict()
    child_event_ids, child_fingerprints = _read_accounting_links(
        conn,
        fallback_event.event_id,
        fallback_event.api_key_id,
    )
    if child_event_ids != fallback_event.child_event_ids or child_fingerprints != fallback_event.child_fingerprints:
        _raise_accounting_conflict()
    retained_event = replace(
        fallback_event,
        occurred_at=_parse_accounting_datetime(outbox["occurred_at"]),
        request_id=outbox["request_id"],
    )
    if retained_event.billing_fingerprint != outbox["billing_fingerprint"]:
        _raise_accounting_conflict()
    return StoredAccountingEvent(
        event=retained_event,
        usage_row_id=None,
        created_at=created_at,
        projected_at=projected_at,
        projection_attempts=attempts,
        last_attempt_at=last_attempt_at,
        last_error_code=last_error_code,
    )


def _accounting_event_from_rows(
    outbox,
    usage_row,
    *,
    components: tuple[BillingComponent, ...],
    child_event_ids: tuple[str, ...],
    child_fingerprints: tuple[str, ...],
) -> AccountingEvent:
    try:
        if type(usage_row["is_estimated"]) is not int or usage_row["is_estimated"] not in (0, 1):
            _raise_accounting_conflict()
        if (
            type(usage_row["cost_unavailable"]) is not int
            or usage_row["cost_unavailable"] not in (0, 1)
        ):
            _raise_accounting_conflict()
        if (
            usage_row["accounting_event_id"] != outbox["event_id"]
            or usage_row["accounting_kind"] != outbox["event_kind"]
            or usage_row["parent_accounting_event_id"] != outbox["parent_event_id"]
            or usage_row["request_id"] != outbox["request_id"]
            or usage_row["api_key_id"] != outbox["api_key_id"]
            or usage_row["timestamp"] != outbox["occurred_at"]
        ):
            _raise_accounting_conflict()

        event = AccountingEvent(
            version=outbox["schema_version"],
            event_id=outbox["event_id"],
            kind=AccountingEventKind(outbox["event_kind"]),
            api_key_id=outbox["api_key_id"],
            method=outbox["http_method"],
            route_template=outbox["route_template"],
            operation=usage_row["operation"],
            gateway_model=usage_row["gateway_model"],
            provider=usage_row["provider"],
            model=usage_row["model"],
            usage=AccountingUsage(
                prompt_tokens=usage_row["prompt_tokens"],
                completion_tokens=usage_row["completion_tokens"],
                total_tokens=usage_row["total_tokens"],
                reasoning_tokens=usage_row["reasoning_tokens"],
                cached_tokens=usage_row["cached_tokens"],
                cost=usage_row["cost"],
                cost_saved=usage_row["cost_saved"],
                duration_ms=usage_row["duration_ms"],
                ttft_ms=usage_row["ttft_ms"],
                is_estimated=bool(usage_row["is_estimated"]),
                cost_unavailable=bool(usage_row["cost_unavailable"]),
            ),
            cost_source=CostSource(outbox["cost_source"]),
            occurred_at=_parse_accounting_datetime(outbox["occurred_at"]),
            request_id=outbox["request_id"],
            parent_event_id=outbox["parent_event_id"],
            components=components,
            child_event_ids=child_event_ids,
            child_fingerprints=child_fingerprints,
        )
        if (
            event.billing_fingerprint != outbox["billing_fingerprint"]
            or event.usage.cost != outbox["usage_cost_usd"]
            or event.usage.total_tokens != outbox["total_tokens"]
        ):
            _raise_accounting_conflict()
        return event
    except AccountingError as exc:
        if exc.code is AccountingErrorCode.FINGERPRINT_CONFLICT:
            raise
        _raise_accounting_conflict()
    except (KeyError, TypeError, ValueError):
        _raise_accounting_conflict()


def _read_stored_accounting_event(
    conn: sqlite3.Connection,
    event_id: str,
    fallback_event: AccountingEvent | None = None,
) -> StoredAccountingEvent | None:
    outbox = conn.execute(_SELECT_ACCOUNTING_OUTBOX_SQL, (event_id,)).fetchone()
    if outbox is None:
        return None
    usage_rows = conn.execute(_SELECT_ACCOUNTING_USAGE_SQL, (event_id,)).fetchall()
    if not usage_rows and fallback_event is not None:
        try:
            return _read_retained_accounting_event(conn, outbox, fallback_event)
        except AccountingError as exc:
            if exc.code is AccountingErrorCode.FINGERPRINT_CONFLICT:
                raise
            _raise_accounting_conflict()
        except (TypeError, ValueError):
            _raise_accounting_conflict()
    if not usage_rows:
        mark = _projection_mark_from_outbox(outbox)
        if mark.projected_at is not None:
            return None
    if len(usage_rows) != 1:
        _raise_accounting_conflict()
    usage_row = usage_rows[0]
    components = _read_accounting_components(conn, event_id)
    child_event_ids, child_fingerprints = _read_accounting_links(
        conn,
        event_id,
        outbox["api_key_id"],
    )
    event = _accounting_event_from_rows(
        outbox,
        usage_row,
        components=components,
        child_event_ids=child_event_ids,
        child_fingerprints=child_fingerprints,
    )
    try:
        created_at, projected_at, attempts, last_attempt_at, last_error_code = _read_projection_metadata(outbox)
        return StoredAccountingEvent(
            event=event,
            usage_row_id=usage_row["id"],
            created_at=created_at,
            projected_at=projected_at,
            projection_attempts=attempts,
            last_attempt_at=last_attempt_at,
            last_error_code=last_error_code,
        )
    except AccountingError:
        _raise_accounting_conflict()
    except (TypeError, ValueError):
        _raise_accounting_conflict()


def _accepted_event_matches(
    stored_event: StoredAccountingEvent,
    incoming_event: AccountingEvent,
) -> bool:
    return (
        stored_event.billing_fingerprint == incoming_event.billing_fingerprint
        and tuple(
            component.billing_fingerprint
            for component in stored_event.event.components
        )
        == tuple(
            component.billing_fingerprint
            for component in incoming_event.components
        )
        and stored_event.event.child_event_ids == incoming_event.child_event_ids
        and stored_event.event.child_fingerprints
        == incoming_event.child_fingerprints
    )


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def _period_date_format(period: str) -> str:
    if period == "hour":
        return "%Y-%m-%d %H:00:00"
    if period == "day":
        return "%Y-%m-%d"
    if period == "week":
        return "%Y-W%W"
    if period == "month":
        return "%Y-%m"
    raise ValueError(f"Invalid period: {period}. Must be 'hour', 'day', 'week', or 'month'.")


# Throughput over rows where both completion tokens and a positive duration are
# known; pairing numerator and denominator on the same rows keeps the ratio honest.
_TOKENS_PER_SECOND_SQL = (
    "SUM(CASE WHEN duration_ms > 0 AND completion_tokens IS NOT NULL"
    " THEN completion_tokens ELSE 0 END) * 1000.0"
    " / NULLIF(SUM(CASE WHEN duration_ms > 0 AND completion_tokens IS NOT NULL"
    " THEN duration_ms ELSE 0 END), 0)"
)


def _append_optional_filter(where_parts: list[str], params: list, column: str, value: str | None) -> None:
    if value:
        where_parts.append(f"{column} = ?")
        params.append(value)


def _build_usage_where(
    *,
    start_date: datetime | None = None,
    end_date: datetime | None = None,
    api_key_id: int | None = None,
    api_key_unattributed: bool = False,
    operation: str | None = None,
    gateway_model: str | None = None,
    provider: str | None = None,
    model: str | None = None,
    upstream_key_fingerprint: str | None = None,
    usage_source: str | None = None,
    is_estimated: bool | None = None,
    x_title: str | None = None,
) -> tuple[str, list]:
    where_parts: list[str] = []
    params: list = []
    if start_date:
        where_parts.append("timestamp >= ?")
        params.append(_utc_isoformat(start_date))
    if end_date:
        where_parts.append("timestamp <= ?")
        params.append(_utc_isoformat(end_date))
    if api_key_unattributed:
        where_parts.append("api_key_id IS NULL")
    elif api_key_id is not None:
        where_parts.append("api_key_id = ?")
        params.append(api_key_id)
    _append_optional_filter(where_parts, params, "operation", operation)
    _append_optional_filter(where_parts, params, "gateway_model", gateway_model)
    _append_optional_filter(where_parts, params, "provider", provider)
    _append_optional_filter(where_parts, params, "model", model)
    _append_optional_filter(where_parts, params, "upstream_key_fingerprint", upstream_key_fingerprint)
    _append_optional_filter(where_parts, params, "usage_source", usage_source)
    _append_optional_filter(where_parts, params, "x_title", x_title)
    if is_estimated is not None:
        where_parts.append("is_estimated = ?")
        params.append(1 if is_estimated else 0)
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return where_clause, params


def _coerce_usage_summary(row) -> dict:
    if row is None:
        return {
            "requests": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cost": 0.0,
            "cost_saved": 0.0,
            "estimated_count": 0,
            "avg_duration_ms": None,
            "max_duration_ms": None,
            "duration_p50_ms": None,
            "duration_p95_ms": None,
            "ttft_avg_ms": None,
            "ttft_max_ms": None,
            "ttft_p50_ms": None,
            "ttft_p95_ms": None,
            "tokens_per_second": None,
            "first_attempt_requests": 0,
            "fallback_tracked_requests": 0,
        }
    return {
        "requests": int(row["requests"] or 0),
        "prompt_tokens": int(row["prompt_tokens"] or 0),
        "completion_tokens": int(row["completion_tokens"] or 0),
        "total_tokens": int(row["total_tokens"] or 0),
        "reasoning_tokens": int(row["reasoning_tokens"] or 0),
        "cached_tokens": int(row["cached_tokens"] or 0),
        "cost": float(row["cost"] or 0.0),
        "cost_saved": float(row["cost_saved"] or 0.0),
        "estimated_count": int(row["estimated_count"] or 0),
        "avg_duration_ms": row["avg_duration_ms"],
        "max_duration_ms": row["max_duration_ms"],
        "duration_p50_ms": row["duration_p50_ms"],
        "duration_p95_ms": row["duration_p95_ms"],
        "ttft_avg_ms": row["ttft_avg_ms"],
        "ttft_max_ms": row["ttft_max_ms"],
        "ttft_p50_ms": row["ttft_p50_ms"],
        "ttft_p95_ms": row["ttft_p95_ms"],
        "tokens_per_second": row["tokens_per_second"],
        "first_attempt_requests": int(row["first_attempt_requests"] or 0),
        "fallback_tracked_requests": int(row["fallback_tracked_requests"] or 0),
    }


def validate_usage_records_pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 0 or limit > MAX_USAGE_RECORDS_LIMIT:
        raise ValueError(f"Invalid limit. Must be between 0 and {MAX_USAGE_RECORDS_LIMIT}.")
    if offset < 0:
        raise ValueError("Invalid offset. Must be greater than or equal to 0.")
    return limit, offset


class TokensUsageDB:
    def __init__(self, db_filename: str = "tokens_usage.db", write_batcher: WriteBatcher | None = None):
        db_dir = resolve_db_dir(__file__)
        db_path = db_dir / db_filename

        os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self._batcher = write_batcher
        self._init_db()
        accounting_schema.migrate_accounting_source(self.db_path)

    def set_batcher(self, batcher: WriteBatcher) -> None:
        self._batcher = batcher

    # ------------------------------------------------------------------
    # Durable accounting source (sync, correctness-critical)
    # ------------------------------------------------------------------

    def accept_accounting_event(self, event: AccountingEvent) -> SourceAcceptance:
        if not isinstance(event, AccountingEvent):
            raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN IMMEDIATE")
            existing_fingerprint = _get_accounting_outbox_fingerprint(conn, event.event_id)
            if existing_fingerprint is not None:
                if existing_fingerprint != event.billing_fingerprint:
                    _raise_accounting_conflict()
                stored_event = _read_stored_accounting_event(conn, event.event_id, event)
                if stored_event is None or not _accepted_event_matches(
                    stored_event,
                    event,
                ):
                    _raise_accounting_conflict()
                conn.rollback()
                return SourceAcceptance(
                    status=SourceStatus.DUPLICATE,
                    stored_event=stored_event,
                )

            created_at = datetime.now(timezone.utc)
            _insert_accounting_usage(conn, event)
            _insert_accounting_outbox(conn, event, created_at)
            _insert_accounting_manifests(conn, event)
            _upsert_usage_aggregates(conn, event)
            stored_event = _read_stored_accounting_event(conn, event.event_id, event)
            if stored_event is None or stored_event.billing_fingerprint != event.billing_fingerprint:
                _raise_accounting_conflict()

            try:
                accounting_schema.commit_accounting_transaction(conn)
            except Exception as commit_error:
                if conn.in_transaction:
                    conn.rollback()
                active_conn = conn
                conn = None
                _close_accounting_source_connection(
                    active_conn,
                    primary_error=commit_error,
                )
                return self._recover_accounting_acceptance(event)
            return SourceAcceptance(
                status=SourceStatus.ACCEPTED,
                stored_event=stored_event,
            )
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def _recover_accounting_acceptance(self, event: AccountingEvent) -> SourceAcceptance:
        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN")
            stored_event = _read_stored_accounting_event(conn, event.event_id, event)
            if stored_event is None:
                raise _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if not _accepted_event_matches(stored_event, event):
                _raise_accounting_conflict()
            conn.rollback()
            return SourceAcceptance(
                status=SourceStatus.RECOVERED,
                stored_event=stored_event,
            )
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def get_accounting_event(self, event_id: str) -> StoredAccountingEvent | None:
        event_id = _validate_accounting_event_id(event_id)
        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN")
            stored_event = _read_stored_accounting_event(conn, event_id)
            conn.rollback()
            return stored_event
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def list_accounting_source_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSourceAuditRow, ...]:
        if (
            isinstance(limit, bool)
            or not isinstance(limit, int)
            or not 1 <= limit <= ACCOUNTING_AUDIT_MAX_PAGE_SIZE
        ):
            raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
        if after_event_id is not None:
            after_event_id = _validate_accounting_event_id(after_event_id)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN")
            rows = conn.execute(
                _SELECT_ACCOUNTING_SOURCE_AUDIT_PAGE_SQL,
                {
                    "after_event_id": after_event_id or "",
                    "limit": limit,
                },
            ).fetchall()
            if len(rows) > limit:
                _raise_accounting_conflict()
            manifests = _read_accounting_audit_manifests(conn, rows)

            audit_rows: list[AccountingSourceAuditRow] = []
            previous_event_id = after_event_id
            for row in rows:
                manifest = manifests.get(row["event_id"], ((), (), ()))
                audit_row = _source_audit_row_from_db(
                    row,
                    components=manifest[0],
                    child_event_ids=manifest[1],
                    child_fingerprints=manifest[2],
                )
                if previous_event_id is not None and audit_row.event_id <= previous_event_id:
                    _raise_accounting_conflict()
                audit_rows.append(audit_row)
                previous_event_id = audit_row.event_id
            conn.rollback()
            return tuple(audit_rows)
        except AccountingError as exc:
            primary_error = exc
            if conn is not None:
                _rollback_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None:
                _rollback_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None:
                _rollback_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def list_pending_accounting_events(
        self,
        *,
        limit: int,
        after: tuple[datetime, str] | None = None,
    ) -> tuple[StoredAccountingEvent, ...]:
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
        after_created_at: str | None = None
        after_event_id: str | None = None
        if after is not None:
            if not isinstance(after, tuple) or len(after) != 2:
                raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
            after_created_at = _normalize_accounting_datetime(after[0]).isoformat()
            after_event_id = _validate_accounting_event_id(after[1])

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN")
            if after_created_at is None:
                rows = conn.execute(
                    """
                    SELECT event_id
                    FROM accounting_outbox
                    WHERE projected_at IS NULL
                    ORDER BY created_at, event_id
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT event_id
                    FROM accounting_outbox
                    WHERE projected_at IS NULL
                      AND (created_at, event_id) > (?, ?)
                    ORDER BY created_at, event_id
                    LIMIT ?
                    """,
                    (after_created_at, after_event_id, limit),
                ).fetchall()
            pending: list[StoredAccountingEvent] = []
            for row in rows:
                stored_event = _read_stored_accounting_event(conn, row["event_id"])
                if stored_event is None or stored_event.projected_at is not None:
                    _raise_accounting_conflict()
                pending.append(stored_event)
            conn.rollback()
            return tuple(pending)
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def mark_accounting_event_projected(
        self,
        event_id: str,
        fingerprint: str,
        projected_at: datetime,
    ) -> AccountingProjectionMark:
        return self._update_accounting_projection(
            event_id=event_id,
            fingerprint=fingerprint,
            attempted_at=projected_at,
            projected_at=projected_at,
            error_code=None,
        )

    def mark_accounting_projection_failed(
        self,
        event_id: str,
        fingerprint: str,
        reason_code: AccountingErrorCode,
        attempted_at: datetime,
    ) -> AccountingProjectionMark:
        if not isinstance(reason_code, AccountingErrorCode):
            raise _accounting_error(AccountingErrorCode.INVALID_CONTRACT)
        return self._update_accounting_projection(
            event_id=event_id,
            fingerprint=fingerprint,
            attempted_at=attempted_at,
            projected_at=None,
            error_code=reason_code,
        )

    def _update_accounting_projection(
        self,
        *,
        event_id: str,
        fingerprint: str,
        attempted_at: datetime,
        projected_at: datetime | None,
        error_code: AccountingErrorCode | None,
    ) -> AccountingProjectionMark:
        event_id = _validate_accounting_event_id(event_id)
        fingerprint = _validate_accounting_fingerprint(fingerprint)
        attempted_at = _normalize_accounting_datetime(attempted_at)
        if projected_at is not None:
            projected_at = _normalize_accounting_datetime(projected_at)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN IMMEDIATE")
            current_mark = _read_accounting_projection_mark(conn, event_id)
            if current_mark is None:
                raise _accounting_error(AccountingErrorCode.ORPHAN_SOURCE)
            if current_mark.billing_fingerprint != fingerprint:
                _raise_accounting_conflict()
            if current_mark.projected_at is not None:
                conn.rollback()
                return current_mark

            current = _read_stored_accounting_event(conn, event_id)
            if current is None:
                _raise_accounting_conflict()

            if projected_at is None:
                cursor = conn.execute(
                    """
                    UPDATE accounting_outbox
                    SET projection_attempts = projection_attempts + 1,
                        last_attempt_at = ?, last_error_code = ?
                    WHERE event_id = ? AND billing_fingerprint = ?
                      AND projected_at IS NULL
                    """,
                    (attempted_at.isoformat(), error_code.value, event_id, fingerprint),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE accounting_outbox
                    SET projected_at = ?,
                        projection_attempts = projection_attempts + 1,
                        last_attempt_at = ?, last_error_code = NULL
                    WHERE event_id = ? AND billing_fingerprint = ?
                      AND projected_at IS NULL
                    """,
                    (
                        projected_at.isoformat(),
                        attempted_at.isoformat(),
                        event_id,
                        fingerprint,
                    ),
                )
            if cursor.rowcount != 1:
                _raise_accounting_conflict()
            updated = _read_accounting_projection_mark(conn, event_id)
            if updated is None:
                _raise_accounting_conflict()
            try:
                accounting_schema.commit_accounting_transaction(conn)
            except Exception as commit_error:
                if conn.in_transaction:
                    conn.rollback()
                active_conn = conn
                conn = None
                _close_accounting_source_connection(
                    active_conn,
                    primary_error=commit_error,
                )
                return self._recover_accounting_projection_update(
                    event_id=event_id,
                    fingerprint=fingerprint,
                    attempted_at=attempted_at,
                    projected_at=projected_at,
                    error_code=error_code,
                    expected_projection_attempts=updated.projection_attempts,
                )
            return updated
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    def _recover_accounting_projection_update(
        self,
        *,
        event_id: str,
        fingerprint: str,
        attempted_at: datetime,
        projected_at: datetime | None,
        error_code: AccountingErrorCode | None,
        expected_projection_attempts: int,
    ) -> AccountingProjectionMark:
        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = _open_accounting_source_connection(self.db_path)
            conn.execute("BEGIN")
            current = _read_accounting_projection_mark(conn, event_id)
            if current is None:
                raise _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if current.billing_fingerprint != fingerprint:
                _raise_accounting_conflict()
            if projected_at is None:
                recovered = (
                    error_code is not None
                    and current.projection_attempts == expected_projection_attempts
                    and current.projected_at is None
                    and current.last_attempt_at == attempted_at
                    and current.last_error_code is error_code
                )
            else:
                recovered = (
                    current.projection_attempts == expected_projection_attempts
                    and current.projected_at == projected_at
                    and current.last_attempt_at == attempted_at
                    and current.last_error_code is None
                )
            if not recovered:
                raise _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            conn.rollback()
            return current
        except AccountingError as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        except Exception:
            primary_error = _accounting_error(AccountingErrorCode.SOURCE_WRITE_FAILED)
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            if conn is not None and conn.in_transaction:
                conn.rollback()
            raise
        finally:
            if conn is not None:
                _close_accounting_source_connection(
                    conn,
                    primary_error=primary_error,
                )

    # ------------------------------------------------------------------
    # Schema init (sync — runs before event loop)
    # ------------------------------------------------------------------

    def _init_db(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS tokens_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                duration_ms INTEGER,
                ttft_ms INTEGER,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                reasoning_tokens INTEGER DEFAULT 0,
                cached_tokens INTEGER DEFAULT 0,
                cost REAL DEFAULT 0.0,
                gateway_model TEXT,
                operation TEXT,
                model TEXT,
                provider TEXT,
                request_id TEXT,
                is_estimated INTEGER NOT NULL DEFAULT 0,
                usage_source TEXT,
                cost_saved REAL NOT NULL DEFAULT 0.0,
                api_key_id INTEGER,
                upstream_key_fingerprint TEXT,
                x_title TEXT,
                cost_unavailable INTEGER NOT NULL DEFAULT 0,
                client_ip TEXT,
                client_user_agent TEXT,
                fallback_depth INTEGER
            )
            ''')

            cursor.execute("PRAGMA table_info(tokens_usage)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "request_id" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN request_id TEXT")
            if "gateway_model" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN gateway_model TEXT")
            if "operation" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN operation TEXT")
            if "duration_ms" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN duration_ms INTEGER")
            if "ttft_ms" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN ttft_ms INTEGER")
            if "reasoning_tokens" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN reasoning_tokens INTEGER DEFAULT 0")
            if "is_estimated" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN is_estimated INTEGER NOT NULL DEFAULT 0")
            if "usage_source" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN usage_source TEXT")
            if "cost_saved" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN cost_saved REAL NOT NULL DEFAULT 0.0")
            if "api_key_id" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN api_key_id INTEGER")
            if "upstream_key_fingerprint" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN upstream_key_fingerprint TEXT")
            if "x_title" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN x_title TEXT")
            if "cost_unavailable" not in existing_columns:
                cursor.execute(
                    "ALTER TABLE tokens_usage ADD COLUMN cost_unavailable INTEGER NOT NULL DEFAULT 0"
                )
            if "client_ip" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN client_ip TEXT")
            if "client_user_agent" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN client_user_agent TEXT")
            if "fallback_depth" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN fallback_depth INTEGER")

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_timestamp
            ON tokens_usage (timestamp)
            ''')

            # Composite index — ``get_aggregated_usage`` groups by time bucket
            # *and* gateway_model; a composite index lets SQLite satisfy both
            # the timestamp range filter and the grouping prefix in one seek.
            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_timestamp_gateway_model
            ON tokens_usage (timestamp, gateway_model)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_api_key_timestamp
            ON tokens_usage (api_key_id, timestamp DESC)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_api_key_operation_timestamp
            ON tokens_usage (api_key_id, operation, timestamp DESC)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_provider_model_timestamp
            ON tokens_usage (provider, model, timestamp DESC)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_upstream_key_timestamp
            ON tokens_usage (upstream_key_fingerprint, timestamp DESC)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_tokens_usage_x_title_timestamp
            ON tokens_usage (x_title, timestamp DESC)
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_idempotency_keys (
                idempotency_key TEXT PRIMARY KEY,
                created_at DATETIME NOT NULL
            )
            ''')

            # Durable analytics aggregates: raw tokens_usage rows are pruned by
            # cleanup_old_records(), so long-horizon totals/series must live in
            # tables the retention loop never touches. Both are updated in the
            # same transaction as the raw usage insert (accept_accounting_event).
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_hourly (
                hour TEXT PRIMARY KEY,
                requests INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0.0,
                cost_saved REAL NOT NULL DEFAULT 0.0,
                duration_ms_sum INTEGER NOT NULL DEFAULT 0,
                duration_count INTEGER NOT NULL DEFAULT 0
            )
            ''')

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS usage_lifetime (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                requests INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER NOT NULL DEFAULT 0,
                completion_tokens INTEGER NOT NULL DEFAULT 0,
                total_tokens INTEGER NOT NULL DEFAULT 0,
                cost REAL NOT NULL DEFAULT 0.0,
                cost_saved REAL NOT NULL DEFAULT 0.0,
                first_event_at TEXT
            )
            ''')

            # One-time backfill: seed the durable aggregates from raw rows that
            # existed before this schema was introduced, so unfiltered series
            # can read exclusively from usage_hourly (hourly ⊇ raw afterwards).
            hourly_rows = cursor.execute("SELECT COUNT(*) FROM usage_hourly").fetchone()[0]
            if hourly_rows == 0:
                cursor.execute('''
                INSERT INTO usage_hourly (
                    hour, requests, prompt_tokens, completion_tokens, total_tokens,
                    cost, cost_saved, duration_ms_sum, duration_count
                )
                SELECT
                    strftime('%Y-%m-%dT%H:00:00', timestamp),
                    COUNT(*),
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0),
                    COALESCE(SUM(total_tokens), 0),
                    COALESCE(SUM(cost), 0.0),
                    COALESCE(SUM(cost_saved), 0.0),
                    COALESCE(SUM(COALESCE(duration_ms, 0)), 0),
                    COUNT(duration_ms)
                FROM tokens_usage
                WHERE strftime('%Y-%m-%dT%H:00:00', timestamp) IS NOT NULL
                GROUP BY 1
                ''')
            lifetime_rows = cursor.execute("SELECT COUNT(*) FROM usage_lifetime").fetchone()[0]
            if lifetime_rows == 0:
                cursor.execute('''
                INSERT INTO usage_lifetime (
                    id, requests, prompt_tokens, completion_tokens, total_tokens,
                    cost, cost_saved, first_event_at
                )
                SELECT
                    1,
                    COUNT(*),
                    COALESCE(SUM(prompt_tokens), 0),
                    COALESCE(SUM(completion_tokens), 0),
                    COALESCE(SUM(total_tokens), 0),
                    COALESCE(SUM(cost), 0.0),
                    COALESCE(SUM(cost_saved), 0.0),
                    MIN(timestamp)
                FROM tokens_usage
                HAVING COUNT(*) > 0
                ''')

            conn.commit()
            logger.info("Tokens usage database initialized at %s", self.db_path)
        except Exception as e:
            logger.error("Error initializing tokens usage database: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Writes — fire-and-forget via WriteBatcher (thread-safe, sync)
    # ------------------------------------------------------------------

    def insert_usage(self, tokens_usage: dict) -> None:
        """Enqueue a usage record for batched writing.

        This method is **synchronous and thread-safe** so it can be called
        from the chat-logging background thread as well as from coroutines.
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        params = (
            timestamp,
            tokens_usage.get("duration_ms"),
            tokens_usage.get("prompt_tokens", 0),
            tokens_usage.get("completion_tokens", 0),
            tokens_usage.get("total_tokens", 0),
            tokens_usage.get("reasoning_tokens", 0),
            tokens_usage.get("cached_tokens", 0),
            tokens_usage.get("cost", 0.0),
            tokens_usage.get("gateway_model"),
            tokens_usage.get("operation"),
            tokens_usage.get("model"),
            tokens_usage.get("provider"),
            tokens_usage.get("request_id"),
            1 if tokens_usage.get("is_estimated") else 0,
            tokens_usage.get("usage_source"),
            float(tokens_usage.get("cost_saved") or 0.0),
            tokens_usage.get("api_key_id"),
            tokens_usage.get("upstream_key_fingerprint"),
            tokens_usage.get("x_title"),
        )
        if self._batcher is not None:
            self._batcher.enqueue(INSERT_USAGE_SQL, params)
        else:
            # Fallback for tests / standalone usage without batcher.
            self._insert_sync(params)

    def insert_usage_once(self, idempotency_key: str, tokens_usage: dict) -> bool:
        """Persist a usage row once for a durable idempotency key.

        This bypasses WriteBatcher because the idempotency key and the usage
        row must commit atomically.
        """
        if not idempotency_key:
            raise ValueError("idempotency_key must not be empty")

        timestamp = datetime.now(timezone.utc).isoformat()
        params = (
            timestamp,
            tokens_usage.get("duration_ms"),
            tokens_usage.get("prompt_tokens", 0),
            tokens_usage.get("completion_tokens", 0),
            tokens_usage.get("total_tokens", 0),
            tokens_usage.get("reasoning_tokens", 0),
            tokens_usage.get("cached_tokens", 0),
            tokens_usage.get("cost", 0.0),
            tokens_usage.get("gateway_model"),
            tokens_usage.get("operation"),
            tokens_usage.get("model"),
            tokens_usage.get("provider"),
            tokens_usage.get("request_id"),
            1 if tokens_usage.get("is_estimated") else 0,
            tokens_usage.get("usage_source"),
            float(tokens_usage.get("cost_saved") or 0.0),
            tokens_usage.get("api_key_id"),
            tokens_usage.get("upstream_key_fingerprint"),
            tokens_usage.get("x_title"),
        )
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute("BEGIN IMMEDIATE")
            cursor = conn.execute(INSERT_USAGE_IDEMPOTENCY_SQL, (idempotency_key, timestamp))
            if cursor.rowcount == 0:
                conn.rollback()
                return False
            conn.execute(INSERT_USAGE_SQL, params)
            conn.commit()
            return True
        except Exception as e:
            logger.error("Error inserting idempotent token usage data: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _insert_sync(self, params: tuple) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            # journal_mode=WAL was set persistently in _init_db; only apply the
            # per-connection pragmas here.
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(INSERT_USAGE_SQL, params)
            conn.commit()
        except Exception as e:
            logger.error("Error inserting token usage data: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Reads — async via aiosqlite
    # ------------------------------------------------------------------

    async def get_latest_usage_records(
        self, limit: int = 25, offset: int = 0, *, api_key_id: int | None = None
    ):
        limit, offset = validate_usage_records_pagination(limit, offset)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row
                where = ""
                params: list = []
                if api_key_id is not None:
                    where = " WHERE api_key_id = ?"
                    params.append(api_key_id)
                params.extend([limit, offset])
                query = f"""
                SELECT
                    id, timestamp, duration_ms, prompt_tokens, completion_tokens,
                    total_tokens, reasoning_tokens, cached_tokens, cost,
                    gateway_model, operation, model, provider, request_id,
                    is_estimated, usage_source, cost_saved, api_key_id,
                    upstream_key_fingerprint, x_title
                FROM tokens_usage
                {where}
                ORDER BY timestamp DESC
                LIMIT ? OFFSET ?
                """
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                results = [dict(row) for row in rows]
                logger.debug("Retrieved %d latest token usage records (limit=%d, offset=%d).", len(results), limit, offset)
                return results
        except Exception as e:
            logger.error("Error retrieving latest token usage records: %s", e)
            return []

    async def get_total_records_count(self, *, api_key_id: int | None = None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                if api_key_id is not None:
                    cursor = await db.execute(
                        "SELECT COUNT(*) FROM tokens_usage WHERE api_key_id = ?",
                        (api_key_id,),
                    )
                else:
                    cursor = await db.execute("SELECT COUNT(*) FROM tokens_usage")
                row = await cursor.fetchone()
                count = row[0]
                logger.debug("Total number of token usage records: %d", count)
                return count
        except Exception as e:
            logger.error("Error retrieving total token usage records count: %s", e)
            return 0

    async def get_aggregated_usage(
        self,
        period: str,
        start_date: datetime = None,
        end_date: datetime = None,
        *,
        api_key_id: int | None = None,
    ):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                date_format = _period_date_format(period)

                where_parts: list[str] = [
                    "gateway_model IS NOT NULL",
                    "provider IS NOT NULL",
                    "model IS NOT NULL",
                ]
                params: list = []
                if start_date:
                    where_parts.append("timestamp >= ?")
                    params.append(_utc_isoformat(start_date))
                if end_date:
                    where_parts.append("timestamp <= ?")
                    params.append(_utc_isoformat(end_date))
                if api_key_id is not None:
                    where_parts.append("api_key_id = ?")
                    params.append(api_key_id)
                where_clause = (" WHERE " + " AND ".join(where_parts)) if where_parts else ""

                query = f"""
                SELECT
                    strftime('{date_format}', timestamp) as time_period,
                    gateway_model, operation, provider, model,
                    SUM(prompt_tokens) as prompt_tokens,
                    SUM(completion_tokens) as completion_tokens,
                    SUM(total_tokens) as total_tokens,
                    SUM(reasoning_tokens) as reasoning_tokens,
                    SUM(cached_tokens) as cached_tokens,
                    SUM(cost) as cost,
                    SUM(cost_saved) as cost_saved,
                    SUM(is_estimated) as estimated_count,
                    COUNT(*) as count
                FROM tokens_usage
                {where_clause}
                GROUP BY time_period, gateway_model, operation, provider, model
                ORDER BY time_period DESC, gateway_model ASC, operation ASC, provider ASC, model ASC
                """
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                results = [dict(row) for row in rows]
                logger.debug("Retrieved aggregated token usage for period '%s'. Records found: %d", period, len(results))
                return results
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error retrieving aggregated token usage for period '%s': %s", period, e)
            return []

    async def get_dashboard_usage(
        self,
        period: str,
        start_date: datetime,
        end_date: datetime,
        *,
        api_key_id: int | None = None,
        api_key_unattributed: bool = False,
        operation: str | None = None,
        gateway_model: str | None = None,
        provider: str | None = None,
        model: str | None = None,
        upstream_key_fingerprint: str | None = None,
        usage_source: str | None = None,
        is_estimated: bool | None = None,
        x_title: str | None = None,
        recent_limit: int = 10,
    ) -> dict:
        """Return usage aggregates for the analytics dashboard.

        Unlike the legacy aggregate endpoint, this intentionally keeps rows
        with incomplete routing fields and groups missing values as ``unknown``.
        """
        recent_limit, _ = validate_usage_records_pagination(recent_limit, 0)
        date_format = _period_date_format(period)
        where_clause, params = _build_usage_where(
            start_date=start_date,
            end_date=end_date,
            api_key_id=api_key_id,
            api_key_unattributed=api_key_unattributed,
            operation=operation,
            gateway_model=gateway_model,
            provider=provider,
            model=model,
            upstream_key_fingerprint=upstream_key_fingerprint,
            usage_source=usage_source,
            is_estimated=is_estimated,
            x_title=x_title,
        )

        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                cursor = await db.execute(
                    f"""
                    WITH filtered AS (
                        SELECT * FROM tokens_usage
                        {where_clause}
                    ),
                    duration_ranked AS (
                        SELECT
                            duration_ms,
                            ROW_NUMBER() OVER (ORDER BY duration_ms) AS rn,
                            COUNT(*) OVER () AS cnt
                        FROM filtered
                        WHERE duration_ms IS NOT NULL
                    ),
                    ttft_ranked AS (
                        SELECT
                            ttft_ms,
                            ROW_NUMBER() OVER (ORDER BY ttft_ms) AS rn,
                            COUNT(*) OVER () AS cnt
                        FROM filtered
                        WHERE ttft_ms IS NOT NULL
                    )
                    SELECT
                        COUNT(*) as requests,
                        COALESCE(SUM(prompt_tokens), 0) as prompt_tokens,
                        COALESCE(SUM(completion_tokens), 0) as completion_tokens,
                        COALESCE(SUM(total_tokens), 0) as total_tokens,
                        COALESCE(SUM(reasoning_tokens), 0) as reasoning_tokens,
                        COALESCE(SUM(cached_tokens), 0) as cached_tokens,
                        COALESCE(SUM(cost), 0.0) as cost,
                        COALESCE(SUM(cost_saved), 0.0) as cost_saved,
                        COALESCE(SUM(is_estimated), 0) as estimated_count,
                        CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms,
                        MAX(duration_ms) as max_duration_ms,
                        (
                            SELECT duration_ms FROM duration_ranked
                            WHERE rn = CAST((cnt * 50 + 99) / 100 AS INTEGER)
                        ) as duration_p50_ms,
                        (
                            SELECT duration_ms FROM duration_ranked
                            WHERE rn = CAST((cnt * 95 + 99) / 100 AS INTEGER)
                        ) as duration_p95_ms,
                        (
                            SELECT CAST(AVG(ttft_ms) AS INTEGER) FROM filtered
                            WHERE ttft_ms IS NOT NULL
                        ) as ttft_avg_ms,
                        (SELECT MAX(ttft_ms) FROM filtered) as ttft_max_ms,
                        (
                            SELECT ttft_ms FROM ttft_ranked
                            WHERE rn = CAST((cnt * 50 + 99) / 100 AS INTEGER)
                        ) as ttft_p50_ms,
                        (
                            SELECT ttft_ms FROM ttft_ranked
                            WHERE rn = CAST((cnt * 95 + 99) / 100 AS INTEGER)
                        ) as ttft_p95_ms,
                        {_TOKENS_PER_SECOND_SQL} as tokens_per_second,
                        COALESCE(SUM(CASE WHEN fallback_depth = 0 THEN 1 ELSE 0 END), 0)
                            as first_attempt_requests,
                        COUNT(fallback_depth) as fallback_tracked_requests
                    FROM filtered
                    """,
                    params,
                )
                summary = _coerce_usage_summary(await cursor.fetchone())

                unfiltered = not any(
                    [
                        api_key_id is not None,
                        api_key_unattributed,
                        operation,
                        gateway_model,
                        provider,
                        model,
                        upstream_key_fingerprint,
                        usage_source,
                        is_estimated is not None,
                        x_title,
                    ]
                )
                if unfiltered:
                    # usage_hourly ⊇ raw rows (transactional upsert + startup
                    # backfill), so the unscoped timeline survives the raw-row
                    # retention pruning and covers the long ranges.
                    cursor = await db.execute(
                        f"""
                        SELECT
                            strftime('{date_format}', hour) as time_period,
                            COALESCE(SUM(requests), 0) as requests,
                            COALESCE(SUM(total_tokens), 0) as total_tokens,
                            COALESCE(SUM(cost), 0.0) as cost,
                            COALESCE(SUM(cost_saved), 0.0) as cost_saved,
                            CAST(
                                SUM(duration_ms_sum) * 1.0 / NULLIF(SUM(duration_count), 0)
                                AS INTEGER
                            ) as avg_duration_ms
                        FROM usage_hourly
                        WHERE hour >= ? AND hour <= ?
                        GROUP BY time_period
                        ORDER BY time_period ASC
                        """,
                        (
                            start_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00"),
                            end_date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:00:00"),
                        ),
                    )
                else:
                    cursor = await db.execute(
                        f"""
                        SELECT
                            strftime('{date_format}', timestamp) as time_period,
                            COUNT(*) as requests,
                            COALESCE(SUM(total_tokens), 0) as total_tokens,
                            COALESCE(SUM(cost), 0.0) as cost,
                            COALESCE(SUM(cost_saved), 0.0) as cost_saved,
                            CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms
                        FROM tokens_usage
                        {where_clause}
                        GROUP BY time_period
                        ORDER BY time_period ASC
                        """,
                        params,
                    )
                series = [dict(row) for row in await cursor.fetchall()]

                async def grouped(label_sql: str, select_sql: str = "") -> list[dict]:
                    cursor = await db.execute(
                        f"""
                        SELECT
                            {label_sql} as label
                            {select_sql},
                            COUNT(*) as requests,
                            COALESCE(SUM(total_tokens), 0) as total_tokens,
                            COALESCE(SUM(cost), 0.0) as cost,
                            COALESCE(SUM(cost_saved), 0.0) as cost_saved,
                            COALESCE(SUM(is_estimated), 0) as estimated_count,
                            CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms,
                            {_TOKENS_PER_SECOND_SQL} as tokens_per_second
                        FROM tokens_usage
                        {where_clause}
                        GROUP BY label
                        ORDER BY requests DESC, label ASC
                        LIMIT 20
                        """,
                        params,
                    )
                    return [dict(row) for row in await cursor.fetchall()]

                breakdowns = {
                    "operations": await grouped("COALESCE(operation, 'unknown')"),
                    "gateway_models": await grouped("COALESCE(gateway_model, 'unknown')"),
                    "providers": await grouped("COALESCE(provider, 'unknown')"),
                    "resolved_targets": await grouped(
                        "COALESCE(provider, 'unknown') || ' / ' || COALESCE(model, 'unknown')",
                        ", COALESCE(provider, 'unknown') as provider, COALESCE(model, 'unknown') as model",
                    ),
                    "api_keys": await grouped("COALESCE(CAST(api_key_id AS TEXT), 'unattributed')"),
                    "upstream_keys": await grouped("COALESCE(upstream_key_fingerprint, 'unknown')"),
                    "x_titles": await grouped("COALESCE(x_title, 'unknown')"),
                }

                # Per-provider latency: TTFT average plus nearest-rank p95 of
                # the full duration, merged into the providers breakdown rows.
                cursor = await db.execute(
                    f"""
                    SELECT
                        COALESCE(provider, 'unknown') as label,
                        CAST(AVG(ttft_ms) AS INTEGER) as ttft_avg_ms
                    FROM (SELECT * FROM tokens_usage {where_clause})
                    WHERE ttft_ms IS NOT NULL
                    GROUP BY label
                    """,
                    params,
                )
                provider_ttft = {row["label"]: row["ttft_avg_ms"] for row in await cursor.fetchall()}

                cursor = await db.execute(
                    f"""
                    WITH provider_ranked AS (
                        SELECT
                            COALESCE(provider, 'unknown') as label,
                            duration_ms,
                            ROW_NUMBER() OVER (
                                PARTITION BY COALESCE(provider, 'unknown')
                                ORDER BY duration_ms
                            ) as rn,
                            COUNT(*) OVER (
                                PARTITION BY COALESCE(provider, 'unknown')
                            ) as cnt
                        FROM (SELECT * FROM tokens_usage {where_clause})
                        WHERE duration_ms IS NOT NULL
                    )
                    SELECT label, duration_ms as duration_p95_ms
                    FROM provider_ranked
                    WHERE rn = CAST((cnt * 95 + 99) / 100 AS INTEGER)
                    """,
                    params,
                )
                provider_p95 = {row["label"]: row["duration_p95_ms"] for row in await cursor.fetchall()}

                for row in breakdowns["providers"]:
                    row["ttft_avg_ms"] = provider_ttft.get(row["label"])
                    row["duration_p95_ms"] = provider_p95.get(row["label"])

                cursor = await db.execute(
                    f"""
                    SELECT
                        id, timestamp, duration_ms, prompt_tokens, completion_tokens,
                        total_tokens, reasoning_tokens, cached_tokens, cost,
                        gateway_model, operation, model, provider, request_id,
                        is_estimated, usage_source, cost_saved, api_key_id,
                        upstream_key_fingerprint, x_title, client_ip,
                        client_user_agent, fallback_depth
                    FROM tokens_usage
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT ?
                    """,
                    params + [recent_limit],
                )
                recent_records = [dict(row) for row in await cursor.fetchall()]

                return {
                    "summary": summary,
                    "series": series,
                    "breakdowns": breakdowns,
                    "recent_records": recent_records,
                }
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error retrieving analytics usage dashboard data: %s", e)
            return {
                "summary": _coerce_usage_summary(None),
                "series": [],
                "breakdowns": {
                    "operations": [],
                    "gateway_models": [],
                    "providers": [],
                    "resolved_targets": [],
                    "api_keys": [],
                    "upstream_keys": [],
                    "x_titles": [],
                },
                "recent_records": [],
            }

    async def get_lifetime_totals(self) -> dict | None:
        """Return the all-time counters that survive raw-row retention pruning."""
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row
                cursor = await db.execute(
                    """
                    SELECT requests, prompt_tokens, completion_tokens, total_tokens,
                           cost, cost_saved, first_event_at
                    FROM usage_lifetime
                    WHERE id = 1
                    """
                )
                row = await cursor.fetchone()
                return dict(row) if row is not None else None
        except Exception as e:
            logger.error("Error retrieving lifetime usage totals: %s", e)
            return None

    async def get_dashboard_filter_options(
        self,
        start_date: datetime,
        end_date: datetime,
        *,
        api_key_id: int | None = None,
        api_key_unattributed: bool = False,
    ) -> dict[str, list[str]]:
        where_clause, params = _build_usage_where(
            start_date=start_date,
            end_date=end_date,
            api_key_id=api_key_id,
            api_key_unattributed=api_key_unattributed,
        )
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)

                async def distinct_values(column: str) -> list[str]:
                    if where_clause:
                        query = f"""
                        SELECT DISTINCT {column}
                        FROM tokens_usage
                        {where_clause}
                        AND {column} IS NOT NULL
                        ORDER BY {column} ASC
                        LIMIT 200
                        """
                    else:
                        query = f"""
                        SELECT DISTINCT {column}
                        FROM tokens_usage
                        WHERE {column} IS NOT NULL
                        ORDER BY {column} ASC
                        LIMIT 200
                        """
                    cursor = await db.execute(query, params)
                    return [str(row[0]) for row in await cursor.fetchall() if row[0]]

                return {
                    "operations": await distinct_values("operation"),
                    "gateway_models": await distinct_values("gateway_model"),
                    "providers": await distinct_values("provider"),
                    "models": await distinct_values("model"),
                    "upstream_keys": await distinct_values("upstream_key_fingerprint"),
                    "usage_sources": await distinct_values("usage_source"),
                    "x_titles": await distinct_values("x_title"),
                }
        except Exception as e:
            logger.error("Error retrieving analytics filter options: %s", e)
            return {
                "operations": [],
                "gateway_models": [],
                "providers": [],
                "models": [],
                "upstream_keys": [],
                "usage_sources": [],
                "x_titles": [],
            }

    def cleanup_old_records(self, retention_days: int = 180):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
            cursor.execute(
                """
                DELETE FROM tokens_usage
                WHERE timestamp < ?
                  AND (
                    accounting_event_id IS NULL
                    OR EXISTS (
                        SELECT 1
                        FROM accounting_outbox
                        WHERE event_id = tokens_usage.accounting_event_id
                          AND projected_at IS NOT NULL
                    )
                  )
                """,
                (cutoff_date,),
            )
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info("Cleaned up %d old token usage records (older than %d days)", deleted_count, retention_days)
            else:
                logger.debug("No old token usage records to clean up")
        except Exception as e:
            logger.error("Error cleaning up old token usage records: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()
