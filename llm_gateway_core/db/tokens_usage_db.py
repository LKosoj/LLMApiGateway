import sqlite3
import os
import logging
from datetime import datetime, timedelta, timezone

import aiosqlite

from ..config.paths import resolve_db_dir
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
                api_key_id INTEGER,
                upstream_key_fingerprint TEXT,
                x_title TEXT
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
            if "upstream_key_fingerprint" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN upstream_key_fingerprint TEXT")
            if "x_title" not in existing_columns:
                cursor.execute("ALTER TABLE tokens_usage ADD COLUMN x_title TEXT")

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
                        MAX(duration_ms) as max_duration_ms
                    FROM tokens_usage
                    {where_clause}
                    """,
                    params,
                )
                summary = _coerce_usage_summary(await cursor.fetchone())

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
                            CAST(AVG(duration_ms) AS INTEGER) as avg_duration_ms
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

                cursor = await db.execute(
                    f"""
                    SELECT
                        id, timestamp, duration_ms, prompt_tokens, completion_tokens,
                        total_tokens, reasoning_tokens, cached_tokens, cost,
                        gateway_model, operation, model, provider, request_id,
                        is_estimated, usage_source, cost_saved, api_key_id,
                        upstream_key_fingerprint, x_title
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
