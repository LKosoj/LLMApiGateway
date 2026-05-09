import sqlite3
import os
import logging
from pathlib import Path
from datetime import datetime, timedelta, timezone

import aiosqlite

from .write_batcher import RUNTIME_PRAGMAS, WriteBatcher

MAX_USAGE_RECORDS_LIMIT = 100
logger = logging.getLogger(__name__)

INSERT_USAGE_SQL = """
INSERT INTO tokens_usage
(timestamp, duration_ms, prompt_tokens, completion_tokens, total_tokens,
 reasoning_tokens, cached_tokens, cost, gateway_model, operation, model, provider, request_id, is_estimated, usage_source, cost_saved, api_key_id)
VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""


def _utc_isoformat(value: datetime) -> str:
    if value.tzinfo is None or value.utcoffset() is None:
        value = value.replace(tzinfo=timezone.utc)
    else:
        value = value.astimezone(timezone.utc)
    return value.isoformat()


def validate_usage_records_pagination(limit: int, offset: int) -> tuple[int, int]:
    if limit < 0 or limit > MAX_USAGE_RECORDS_LIMIT:
        raise ValueError(f"Invalid limit. Must be between 0 and {MAX_USAGE_RECORDS_LIMIT}.")
    if offset < 0:
        raise ValueError("Invalid offset. Must be greater than or equal to 0.")
    return limit, offset


class TokensUsageDB:
    def __init__(self, db_filename: str = "tokens_usage.db", write_batcher: WriteBatcher | None = None):
        project_root = Path(__file__).parent.parent.parent
        db_dir = project_root / "db"
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
            CREATE TABLE IF NOT EXISTS tokens_usage (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                duration_ms INTEGER,
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
                api_key_id INTEGER
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
        )
        if self._batcher is not None:
            self._batcher.enqueue(INSERT_USAGE_SQL, params)
        else:
            # Fallback for tests / standalone usage without batcher.
            self._insert_sync(params)

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
                    is_estimated, usage_source, cost_saved, api_key_id
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

    def cleanup_old_records(self, retention_days: int = 180):
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            cursor = conn.cursor()
            cutoff_date = (datetime.now(timezone.utc) - timedelta(days=retention_days)).isoformat()
            cursor.execute("DELETE FROM tokens_usage WHERE timestamp < ?", (cutoff_date,))
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
