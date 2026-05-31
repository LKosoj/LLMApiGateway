import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config.paths import resolve_db_dir
from ..utils.usage_tracking import extract_request_x_title
from .write_batcher import RUNTIME_PRAGMAS, WriteBatcher

logger = logging.getLogger(__name__)

VALID_CATEGORIES = {
    "auth_invalid",
    "key_disabled",
    "model_not_allowed",
    "budget_exhausted",
    "rate_limited",
    "master_only",
    "unauthorized",
}

INSERT_REJECTION_SQL = """
INSERT INTO rejection_events
(timestamp, request_id, api_key_id, path, method, client_ip,
 status_code, category, reason, auth_source, x_title)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


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


def _validate_iso_datetime(value: str, name: str) -> None:
    try:
        datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a valid ISO 8601 datetime string") from exc


def _build_rejections_where(
    *,
    api_key_id: int | None = None,
    api_key_unattributed: bool = False,
    category: str | None = None,
    since: str | None = None,
    until: str | None = None,
    status_code: int | None = None,
    path: str | None = None,
    method: str | None = None,
) -> tuple[str, list]:
    where_parts: list[str] = []
    params: list = []
    if api_key_unattributed:
        where_parts.append("api_key_id IS NULL")
    elif api_key_id is not None:
        where_parts.append("api_key_id = ?")
        params.append(api_key_id)
    if category is not None:
        where_parts.append("category = ?")
        params.append(category)
    if since is not None:
        _validate_iso_datetime(since, "since")
        where_parts.append("timestamp >= ?")
        params.append(since)
    if until is not None:
        _validate_iso_datetime(until, "until")
        where_parts.append("timestamp <= ?")
        params.append(until)
    if status_code is not None:
        where_parts.append("status_code = ?")
        params.append(status_code)
    if path:
        where_parts.append("path = ?")
        params.append(path)
    if method:
        where_parts.append("method = ?")
        params.append(method.upper())
    where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""
    return where_clause, params


class RejectionsDB:
    def __init__(
        self,
        db_filename: str = "tokens_usage.db",
        write_batcher: WriteBatcher | None = None,
    ):
        db_dir = resolve_db_dir(__file__)
        db_path = db_dir / db_filename

        os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self._batcher = write_batcher
        self._init_db()

    def set_batcher(self, batcher: WriteBatcher) -> None:
        self._batcher = batcher

    # ------------------------------------------------------------------
    # Schema init (sync — runs before event loop)
    # ------------------------------------------------------------------

    def _init_db(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()

            cursor.execute("""
            CREATE TABLE IF NOT EXISTS rejection_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                request_id TEXT,
                api_key_id INTEGER,
                path TEXT NOT NULL,
                method TEXT NOT NULL,
                client_ip TEXT,
                status_code INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason TEXT,
                auth_source TEXT,
                x_title TEXT
            )
            """)

            cursor.execute("PRAGMA table_info(rejection_events)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "x_title" not in existing_columns:
                cursor.execute("ALTER TABLE rejection_events ADD COLUMN x_title TEXT")

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_rejection_events_timestamp
            ON rejection_events (timestamp)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_rejection_events_api_key_id
            ON rejection_events (api_key_id)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_rejection_events_category
            ON rejection_events (category)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_rejection_events_api_key_timestamp
            ON rejection_events (api_key_id, timestamp DESC)
            """)

            cursor.execute("""
            CREATE INDEX IF NOT EXISTS idx_rejection_events_api_key_category_timestamp
            ON rejection_events (api_key_id, category, timestamp DESC)
            """)

            conn.commit()
            logger.info("Rejection events table initialized at %s", self.db_path)
        except Exception as e:
            logger.error("Error initializing rejection events table: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Writes — fire-and-forget via WriteBatcher (thread-safe, sync)
    # ------------------------------------------------------------------

    def insert_rejection(
        self,
        *,
        request_id: str | None,
        api_key_id: int | None,
        path: str,
        method: str,
        client_ip: str | None,
        status_code: int,
        category: str,
        reason: str | None,
        auth_source: str | None,
        x_title: str | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        params = (
            timestamp,
            request_id,
            api_key_id,
            path,
            method,
            client_ip,
            status_code,
            category,
            reason[:500] if reason else None,
            auth_source,
            x_title,
        )
        if self._batcher is not None:
            self._batcher.enqueue(INSERT_REJECTION_SQL, params)
        else:
            self._insert_sync(params)

    def _insert_sync(self, params: tuple) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(INSERT_REJECTION_SQL, params)
            conn.commit()
        except Exception as e:
            logger.error("Error inserting rejection event: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Reads — async via aiosqlite
    # ------------------------------------------------------------------

    async def get_rejections(
        self,
        *,
        api_key_id: int | None = None,
        api_key_unattributed: bool = False,
        category: str | None = None,
        since: str | None = None,
        until: str | None = None,
        status_code: int | None = None,
        path: str | None = None,
        method: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        if limit < 0 or limit > 200:
            raise ValueError("limit must be between 0 and 200")
        if offset < 0:
            raise ValueError("offset must be a non-negative integer")
        where_clause, params = _build_rejections_where(
            api_key_id=api_key_id,
            api_key_unattributed=api_key_unattributed,
            category=category,
            since=since,
            until=until,
            status_code=status_code,
            path=path,
            method=method,
        )

        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                cursor = await db.execute(
                    f"SELECT COUNT(*) FROM rejection_events {where_clause}",
                    params,
                )
                row = await cursor.fetchone()
                total = row[0]

                cursor = await db.execute(
                    f"""
                    SELECT id, timestamp, request_id, api_key_id, path, method,
                           client_ip, status_code, category, reason, auth_source,
                           x_title
                    FROM rejection_events
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT ? OFFSET ?
                    """,
                    params + [limit, offset],
                )
                rows = await cursor.fetchall()
                return [dict(row) for row in rows], total
        except Exception as e:
            logger.error("Error retrieving rejection events: %s", e)
            return [], 0

    async def get_dashboard_rejections(
        self,
        period: str,
        start_date: datetime,
        end_date: datetime,
        *,
        api_key_id: int | None = None,
        api_key_unattributed: bool = False,
        category: str | None = None,
        status_code: int | None = None,
        path: str | None = None,
        method: str | None = None,
    ) -> dict:
        date_format = _period_date_format(period)
        where_clause, params = _build_rejections_where(
            api_key_id=api_key_id,
            api_key_unattributed=api_key_unattributed,
            category=category,
            since=_utc_isoformat(start_date),
            until=_utc_isoformat(end_date),
            status_code=status_code,
            path=path,
            method=method,
        )
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                cursor = await db.execute(
                    f"""
                    SELECT COUNT(*) as rejections
                    FROM rejection_events
                    {where_clause}
                    """,
                    params,
                )
                row = await cursor.fetchone()
                summary = {"rejections": int(row["rejections"] or 0) if row else 0}

                cursor = await db.execute(
                    f"""
                    SELECT
                        strftime('{date_format}', timestamp) as time_period,
                        COUNT(*) as rejections
                    FROM rejection_events
                    {where_clause}
                    GROUP BY time_period
                    ORDER BY time_period ASC
                    """,
                    params,
                )
                series = [dict(row) for row in await cursor.fetchall()]

                cursor = await db.execute(
                    f"""
                    SELECT category as label, COUNT(*) as rejections
                    FROM rejection_events
                    {where_clause}
                    GROUP BY category
                    ORDER BY rejections DESC, label ASC
                    LIMIT 20
                    """,
                    params,
                )
                categories = [dict(row) for row in await cursor.fetchall()]

                cursor = await db.execute(
                    f"""
                    SELECT CAST(status_code AS TEXT) as label, status_code, COUNT(*) as rejections
                    FROM rejection_events
                    {where_clause}
                    GROUP BY status_code
                    ORDER BY rejections DESC, status_code ASC
                    LIMIT 20
                    """,
                    params,
                )
                status_codes = [dict(row) for row in await cursor.fetchall()]

                cursor = await db.execute(
                    f"""
                    SELECT id, timestamp, request_id, api_key_id, path, method,
                           status_code, category, reason, auth_source
                    FROM rejection_events
                    {where_clause}
                    ORDER BY timestamp DESC
                    LIMIT 10
                    """,
                    params,
                )
                recent = [dict(row) for row in await cursor.fetchall()]

                return {
                    "summary": summary,
                    "series": series,
                    "categories": categories,
                    "status_codes": status_codes,
                    "recent": recent,
                }
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error retrieving analytics rejection dashboard data: %s", e)
            return {
                "summary": {"rejections": 0},
                "series": [],
                "categories": [],
                "status_codes": [],
                "recent": [],
            }

    # ------------------------------------------------------------------
    # Cleanup
    # ------------------------------------------------------------------

    def cleanup_old_records(self, retention_days: int = 180) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()
            cutoff_date = (
                datetime.now(timezone.utc) - timedelta(days=retention_days)
            ).isoformat()
            cursor.execute(
                "DELETE FROM rejection_events WHERE timestamp < ?", (cutoff_date,)
            )
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info(
                    "Cleaned up %d old rejection event records (older than %d days)",
                    deleted_count,
                    retention_days,
                )
            else:
                logger.debug("No old rejection event records to clean up")
        except Exception as e:
            logger.error("Error cleaning up old rejection event records: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()


# ------------------------------------------------------------------
# Module-level helper — called from middleware and access control
# ------------------------------------------------------------------


def record_rejection(
    request,
    *,
    status_code: int,
    reason: str,
    category: str,
) -> None:
    """Record a governance rejection into RejectionsDB (resilient noop).

    If ``rejections_db`` is absent from ``request.app.state``, logs a warning
    and returns without raising — the caller's error response must not be
    suppressed.
    """
    db: RejectionsDB | None = getattr(request.app.state, "rejections_db", None)
    if db is None:
        logger.warning(
            "record_rejection called but rejections_db is not set on app.state "
            "(path=%s status=%s category=%s)",
            getattr(request.url, "path", "?"),
            status_code,
            category,
        )
        return

    try:
        request_id = getattr(request.state, "llmgateway_request_id", None) or getattr(
            request.state, "llmgateway_active_request_id", None
        )
        api_key_id = getattr(request.state, "api_key_id", None)
        auth_source = getattr(request.state, "gateway_auth_source", None)
        x_title = extract_request_x_title(request)
        client_ip = (
            request.client.host
            if getattr(request, "client", None) is not None
            else None
        )
        db.insert_rejection(
            request_id=request_id,
            api_key_id=api_key_id,
            path=request.url.path,
            method=request.method,
            client_ip=client_ip,
            status_code=status_code,
            category=category,
            reason=reason,
            auth_source=auth_source,
            x_title=x_title,
        )
    except Exception:
        logger.exception(
            "Unexpected error in record_rejection (path=%s status=%s category=%s)",
            getattr(request.url, "path", "?"),
            status_code,
            category,
        )
