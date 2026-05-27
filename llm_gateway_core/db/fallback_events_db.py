import sqlite3
import os
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config.paths import resolve_db_dir
from .write_batcher import RUNTIME_PRAGMAS, WriteBatcher

MAX_FALLBACK_RECORDS_LIMIT = 100
logger = logging.getLogger(__name__)

INSERT_EVENT_SQL = """
INSERT INTO fallback_events
(timestamp, request_id, gateway_model, attempt_number, provider, model,
 success, error_type, error_message, duration_ms, operation, api_key_id, upstream_key_fingerprint)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def validate_fallback_records_pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 0 or limit > MAX_FALLBACK_RECORDS_LIMIT:
        raise ValueError(f"Invalid limit. Must be between 0 and {MAX_FALLBACK_RECORDS_LIMIT}.")
    if offset < 0:
        raise ValueError("Invalid offset. Must be greater than or equal to 0.")
    return limit, offset


class FallbackEventsDB:
    def __init__(self, db_filename: str = "tokens_usage.db", write_batcher: WriteBatcher | None = None):
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

    def _init_db(self):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()

            cursor.execute('''
            CREATE TABLE IF NOT EXISTS fallback_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                request_id TEXT NOT NULL,
                gateway_model TEXT NOT NULL,
                attempt_number INTEGER NOT NULL,
                provider TEXT NOT NULL,
                model TEXT NOT NULL,
                success INTEGER NOT NULL DEFAULT 0,
                error_type TEXT,
                error_message TEXT,
                duration_ms INTEGER NOT NULL DEFAULT 0,
                operation TEXT,
                api_key_id INTEGER,
                upstream_key_fingerprint TEXT
            )
            ''')

            cursor.execute("PRAGMA table_info(fallback_events)")
            existing_columns = {row[1] for row in cursor.fetchall()}
            if "operation" not in existing_columns:
                cursor.execute("ALTER TABLE fallback_events ADD COLUMN operation TEXT")
            if "api_key_id" not in existing_columns:
                cursor.execute("ALTER TABLE fallback_events ADD COLUMN api_key_id INTEGER")
            if "upstream_key_fingerprint" not in existing_columns:
                cursor.execute("ALTER TABLE fallback_events ADD COLUMN upstream_key_fingerprint TEXT")

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fallback_events_timestamp
            ON fallback_events (timestamp)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fallback_events_request_id
            ON fallback_events (request_id)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fallback_events_provider_model_key
            ON fallback_events (provider, model, upstream_key_fingerprint)
            ''')

            cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_fallback_events_timestamp_key
            ON fallback_events (timestamp, api_key_id)
            ''')

            conn.commit()
            logger.info("Fallback events table initialized at %s", self.db_path)
        except Exception as e:
            logger.error("Error initializing fallback events table: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Writes — fire-and-forget via WriteBatcher (thread-safe, sync)
    # ------------------------------------------------------------------

    def insert_event(
        self,
        request_id: str,
        gateway_model: str,
        attempt_number: int,
        provider: str,
        model: str,
        success: bool,
        error_type: str | None,
        error_message: str | None,
        duration_ms: int,
        operation: str | None = None,
        api_key_id: int | None = None,
        upstream_key_fingerprint: str | None = None,
    ) -> None:
        timestamp = datetime.now(timezone.utc).isoformat()
        params = (
            timestamp,
            request_id,
            gateway_model,
            attempt_number,
            provider,
            model,
            1 if success else 0,
            error_type,
            error_message[:500] if error_message else None,
            duration_ms,
            operation,
            api_key_id,
            upstream_key_fingerprint,
        )
        if self._batcher is not None:
            self._batcher.enqueue(INSERT_EVENT_SQL, params)
        else:
            self._insert_sync(params)

    def _insert_sync(self, params: tuple) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(INSERT_EVENT_SQL, params)
            conn.commit()
        except Exception as e:
            logger.error("Error inserting fallback event: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Reads — async via aiosqlite
    # ------------------------------------------------------------------

    async def get_aggregated_stats(self, period: str, start_date: datetime = None, end_date: datetime = None):
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                if period == 'hour':
                    date_format = '%Y-%m-%d %H:00:00'
                elif period == 'day':
                    date_format = '%Y-%m-%d'
                elif period == 'week':
                    date_format = '%Y-W%W'
                elif period == 'month':
                    date_format = '%Y-%m'
                else:
                    raise ValueError(f"Invalid period: {period}. Must be 'hour', 'day', 'week', or 'month'.")

                where_clause = "WHERE success = 0"
                params: list = []
                if start_date:
                    where_clause += " AND timestamp >= ?"
                    params.append(start_date.isoformat())
                if end_date:
                    where_clause += " AND timestamp <= ?"
                    params.append(end_date.isoformat())

                query = f"""
                SELECT
                    strftime('{date_format}', timestamp) as time_period,
                    gateway_model, provider, model, error_type,
                    COUNT(*) as count,
                    CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms
                FROM fallback_events
                {where_clause}
                GROUP BY time_period, gateway_model, provider, model, error_type
                ORDER BY time_period DESC, count DESC
                """
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error retrieving aggregated fallback stats: %s", e)
            return []

    async def get_fallback_records(self, limit: int = 25, offset: int = 0):
        limit, offset = validate_fallback_records_pagination(limit, offset)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row

                # Count distinct request_ids with more than 1 attempt
                cursor = await db.execute("""
                SELECT COUNT(*) FROM (
                    SELECT request_id FROM fallback_events
                    GROUP BY request_id HAVING COUNT(*) > 1
                )
                """)
                row = await cursor.fetchone()
                total_count = row[0]

                # Get paginated request_ids
                cursor = await db.execute("""
                SELECT request_id, MAX(timestamp) as latest_ts
                FROM fallback_events
                GROUP BY request_id
                HAVING COUNT(*) > 1
                ORDER BY latest_ts DESC
                LIMIT ? OFFSET ?
                """, (limit, offset))
                request_ids = [row[0] async for row in cursor]

                if not request_ids:
                    return [], total_count

                # Get all attempts for these request_ids
                placeholders = ",".join("?" * len(request_ids))
                cursor = await db.execute(f"""
                SELECT
                    request_id, timestamp, gateway_model, attempt_number,
                    provider, model, success, error_type, error_message, duration_ms,
                    operation, api_key_id, upstream_key_fingerprint
                FROM fallback_events
                WHERE request_id IN ({placeholders})
                ORDER BY request_id, attempt_number
                """, request_ids)

                rows = await cursor.fetchall()

                # Group by request_id
                chains: dict[str, list[dict]] = {}
                for row in rows:
                    record = dict(row)
                    rid = record["request_id"]
                    if rid not in chains:
                        chains[rid] = []
                    chains[rid].append(record)

                # Build response records in order of request_ids
                records = []
                for rid in request_ids:
                    attempts = chains.get(rid, [])
                    if not attempts:
                        continue

                    first = attempts[0]
                    last = attempts[-1]
                    total_duration_ms = sum(a["duration_ms"] for a in attempts)
                    any_success = any(a["success"] for a in attempts)

                    records.append({
                        "request_id": rid,
                        "timestamp": first["timestamp"],
                        "gateway_model": first["gateway_model"],
                        "total_attempts": len(attempts),
                        "total_duration_ms": total_duration_ms,
                        "success": any_success,
                        "final_provider": last["provider"],
                        "final_model": last["model"],
                        "operation": first["operation"],
                        "upstream_key_fingerprint": last["upstream_key_fingerprint"],
                        "attempts": [
                            {
                                "attempt_number": a["attempt_number"],
                                "provider": a["provider"],
                                "model": a["model"],
                                "success": bool(a["success"]),
                                "error_type": a["error_type"],
                                "error_message": a["error_message"],
                                "duration_ms": a["duration_ms"],
                                "operation": a["operation"],
                                "upstream_key_fingerprint": a["upstream_key_fingerprint"],
                            }
                            for a in attempts
                        ],
                    })

                return records, total_count
        except Exception as e:
            logger.error("Error retrieving fallback records: %s", e)
            return [], 0

    async def get_upstream_stats(
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

                if period == 'hour':
                    date_format = '%Y-%m-%d %H:00:00'
                elif period == 'day':
                    date_format = '%Y-%m-%d'
                elif period == 'week':
                    date_format = '%Y-W%W'
                elif period == 'month':
                    date_format = '%Y-%m'
                else:
                    raise ValueError(f"Invalid period: {period}. Must be 'hour', 'day', 'week', or 'month'.")

                where_parts = ["provider IS NOT NULL", "model IS NOT NULL"]
                params: list = []
                if start_date:
                    where_parts.append("timestamp >= ?")
                    params.append(start_date.isoformat())
                if end_date:
                    where_parts.append("timestamp <= ?")
                    params.append(end_date.isoformat())
                if api_key_id is not None:
                    where_parts.append("api_key_id = ?")
                    params.append(api_key_id)
                where_clause = "WHERE " + " AND ".join(where_parts)

                query = f"""
                SELECT
                    strftime('{date_format}', timestamp) as time_period,
                    COALESCE(operation, 'chat') as operation,
                    gateway_model,
                    provider,
                    model,
                    COALESCE(upstream_key_fingerprint, 'unknown') as upstream_key_fingerprint,
                    COUNT(*) as attempts,
                    SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) as successes,
                    SUM(CASE WHEN success = 0 THEN 1 ELSE 0 END) as errors,
                    ROUND(100.0 * SUM(CASE WHEN success = 1 THEN 1 ELSE 0 END) / COUNT(*), 2) as success_rate,
                    CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms,
                    MAX(duration_ms) as max_duration_ms
                FROM fallback_events
                {where_clause}
                GROUP BY time_period, operation, gateway_model, provider, model, upstream_key_fingerprint
                ORDER BY time_period DESC, attempts DESC
                """
                cursor = await db.execute(query, params)
                rows = await cursor.fetchall()
                return [dict(row) for row in rows]
        except ValueError:
            raise
        except Exception as e:
            logger.error("Error retrieving upstream stats: %s", e)
            return []

    async def get_events_count_last_24h(self, api_key_id: int | None = None) -> dict[int, int]:
        """Return a ``{api_key_id: count}`` map for fallback events in the last 24 h.

        When *api_key_id* is given, only that key's count is returned.
        Events with NULL ``api_key_id`` are ignored (no virtual key attribution).
        """
        try:
            cutoff = (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat()
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                db.row_factory = aiosqlite.Row
                if api_key_id is not None:
                    cursor = await db.execute(
                        """
                        SELECT api_key_id, COUNT(*) as cnt
                        FROM fallback_events
                        WHERE timestamp >= ? AND api_key_id = ?
                        GROUP BY api_key_id
                        """,
                        (cutoff, api_key_id),
                    )
                else:
                    cursor = await db.execute(
                        """
                        SELECT api_key_id, COUNT(*) as cnt
                        FROM fallback_events
                        WHERE timestamp >= ? AND api_key_id IS NOT NULL
                        GROUP BY api_key_id
                        """,
                        (cutoff,),
                    )
                rows = await cursor.fetchall()
                return {int(row["api_key_id"]): int(row["cnt"]) for row in rows}
        except Exception as e:
            logger.error("Error retrieving 24h fallback event counts: %s", e)
            return {}

    def cleanup_old_records(self, retention_days: int = 180):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()
            cutoff_date = (datetime.now() - timedelta(days=retention_days)).isoformat()
            cursor.execute("DELETE FROM fallback_events WHERE timestamp < ?", (cutoff_date,))
            deleted_count = cursor.rowcount
            conn.commit()
            if deleted_count > 0:
                logger.info("Cleaned up %d old fallback event records (older than %d days)", deleted_count, retention_days)
            else:
                logger.debug("No old fallback event records to clean up")
        except Exception as e:
            logger.error("Error cleaning up old fallback event records: %s", e)
            if conn:
                conn.rollback()
        finally:
            if conn:
                conn.close()
