import logging
import os
import sqlite3
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config.paths import resolve_db_dir
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
 status_code, category, reason, auth_source)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


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
                auth_source TEXT
            )
            """)

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
        category: str | None = None,
        since: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict], int]:
        if limit < 0 or limit > 200:
            raise ValueError("limit must be between 0 and 200")
        if offset < 0:
            raise ValueError("offset must be a non-negative integer")
        if since is not None:
            # Reject unparseable values explicitly: ``timestamp >= ?`` is a
            # lexicographic string comparison in SQLite, so a malformed ``since``
            # would silently return a wrong slice instead of an error.
            try:
                datetime.fromisoformat(since)
            except (TypeError, ValueError) as exc:
                raise ValueError(
                    "since must be a valid ISO 8601 datetime string"
                ) from exc

        where_parts: list[str] = []
        params: list = []

        if api_key_id is not None:
            where_parts.append("api_key_id = ?")
            params.append(api_key_id)
        if category is not None:
            where_parts.append("category = ?")
            params.append(category)
        if since is not None:
            where_parts.append("timestamp >= ?")
            params.append(since)

        where_clause = ("WHERE " + " AND ".join(where_parts)) if where_parts else ""

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
                           client_ip, status_code, category, reason, auth_source
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
        )
    except Exception:
        logger.exception(
            "Unexpected error in record_rejection (path=%s status=%s category=%s)",
            getattr(request.url, "path", "?"),
            status_code,
            category,
        )
