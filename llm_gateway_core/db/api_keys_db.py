"""Virtual API keys store.

Keys are stored as plaintext in the ``api_keys`` table per product requirement.
Each key carries an optional non-negative USD budget, optional
per-minute request/token rate limits, and an optional allow-list of gateway
model names.

Writes (create/update/delete) run through direct ``sqlite3`` connections so the
admin API sees a fresh row immediately. Spent-budget increments happen from
worker threads (observability runs under ``anyio.to_thread``), so they also
go through direct ``sqlite3`` connections — a ``WriteBatcher`` would have to
be dedicated to this file since the shared batcher writes to
``tokens_usage.db`` and would silently drop ``UPDATE api_keys`` statements
against the wrong database. ``set_batcher`` remains for callers that want to
route writes through their own batcher bound to this file.
"""

from __future__ import annotations

import json
import logging
import math
import os
import secrets
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import aiosqlite

from .write_batcher import RUNTIME_PRAGMAS, WriteBatcher

logger = logging.getLogger(__name__)


API_KEY_PREFIX = "lgk_"
API_KEY_TOKEN_NBYTES = 32
MAX_METADATA_BYTES = 16 * 1024
_UNSET = object()


def generate_api_key() -> str:
    """Return a freshly-minted virtual API key."""
    return API_KEY_PREFIX + secrets.token_urlsafe(API_KEY_TOKEN_NBYTES)


@dataclass
class ApiKeyRecord:
    id: int
    name: str
    api_key: str
    budget_usd: float | None
    spent_usd: float
    rpm: int | None
    tpm: int | None
    allowed_models: list[str] = field(default_factory=list)
    disabled: bool = False
    metadata: dict[str, Any] = field(default_factory=dict)
    created_at: str = ""
    last_used_at: str | None = None

    def budget_enforced(self) -> bool:
        """True when ``budget_usd`` caps spending (non-negative, not NULL)."""
        return self.budget_usd is not None and self.budget_usd >= 0

    def budget_exhausted(self) -> bool:
        return self.budget_enforced() and self.spent_usd >= float(self.budget_usd)

    def model_allowed(self, gateway_model: str | None) -> bool:
        if not self.allowed_models:
            return True
        return bool(gateway_model) and gateway_model in self.allowed_models


def _serialize_allowed_models(models: list[str] | None) -> str | None:
    if not models:
        return None
    cleaned = [m.strip() for m in models if isinstance(m, str) and m.strip()]
    return json.dumps(cleaned, ensure_ascii=False) if cleaned else None


def _deserialize_allowed_models(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [m for m in parsed if isinstance(m, str)]


def _serialize_metadata(metadata: dict[str, Any] | None) -> str | None:
    if not metadata:
        return None
    validate_metadata_size(metadata)
    return json.dumps(metadata, ensure_ascii=False)


def validate_metadata_size(metadata: dict[str, Any] | None) -> None:
    if not metadata:
        return
    try:
        raw = json.dumps(metadata, ensure_ascii=False)
    except (TypeError, ValueError) as exc:
        raise ValueError("metadata must be JSON serializable") from exc
    if len(raw.encode("utf-8")) > MAX_METADATA_BYTES:
        raise ValueError(f"metadata must be at most {MAX_METADATA_BYTES} bytes")


def _validate_budget_usd(budget_usd: float | None) -> float | None:
    if budget_usd is None:
        return None
    value = float(budget_usd)
    if not math.isfinite(value) or value < 0:
        raise ValueError("budget_usd must be a finite number greater than or equal to 0")
    return value


def _deserialize_metadata(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _row_to_record(row: sqlite3.Row | aiosqlite.Row | dict) -> ApiKeyRecord:
    return ApiKeyRecord(
        id=int(row["id"]),
        name=row["name"],
        api_key=row["api_key"],
        budget_usd=row["budget_usd"] if row["budget_usd"] is not None else None,
        spent_usd=float(row["spent_usd"] or 0.0),
        rpm=row["rpm"] if row["rpm"] is not None else None,
        tpm=row["tpm"] if row["tpm"] is not None else None,
        allowed_models=_deserialize_allowed_models(row["allowed_models"]),
        disabled=bool(row["disabled"]),
        metadata=_deserialize_metadata(row["metadata"]),
        created_at=row["created_at"] or "",
        last_used_at=row["last_used_at"],
    )


class ApiKeysDB:
    """CRUD + budget-tracking storage for virtual API keys."""

    def __init__(
        self,
        db_filename: str = "api_keys.db",
        write_batcher: WriteBatcher | None = None,
    ) -> None:
        project_root = Path(__file__).parent.parent.parent
        db_dir = project_root / "db"
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = db_dir / db_filename
        self._batcher = write_batcher
        self._init_db()

    def set_batcher(self, batcher: WriteBatcher) -> None:
        self._batcher = batcher

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def _init_db(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute("""
                CREATE TABLE IF NOT EXISTS api_keys (
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
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys (api_key)")
            conn.commit()
            logger.info("API keys database initialized at %s", self.db_path)
        except Exception:
            logger.exception("Error initializing API keys database")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # CRUD (sync — used from admin endpoints via asyncio.to_thread)
    # ------------------------------------------------------------------
    def create(
        self,
        *,
        name: str,
        budget_usd: float | None = None,
        rpm: int | None = None,
        tpm: int | None = None,
        allowed_models: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
        api_key: str | None = None,
    ) -> ApiKeyRecord:
        name = (name or "").strip()
        if not name:
            raise ValueError("API key name must not be empty")
        budget_value = _validate_budget_usd(budget_usd)
        validate_metadata_size(metadata)

        key_value = api_key or generate_api_key()
        created_at = datetime.now().isoformat()
        # Coerce non-positive RPM/TPM to NULL so ``create`` and ``update`` agree
        # on how "no limit" is represented; downstream code treats NULL as
        # unlimited, while a stored ``0`` would be picked up as a hard zero
        # limit by any future check that doesn't explicitly guard ``> 0``.
        rpm_value = rpm if rpm is not None and rpm > 0 else None
        tpm_value = tpm if tpm is not None and tpm > 0 else None
        params = (
            name,
            key_value,
            budget_value,
            0.0,
            rpm_value,
            tpm_value,
            _serialize_allowed_models(allowed_models),
            0,
            _serialize_metadata(metadata),
            created_at,
        )
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO api_keys
                    (name, api_key, budget_usd, spent_usd, rpm, tpm, allowed_models,
                     disabled, metadata, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    params,
                )
            except sqlite3.IntegrityError as exc:
                raise ValueError(f"API key collision: {exc}") from exc
            conn.commit()
            new_id = cursor.lastrowid
        return self.get_by_id(new_id)  # type: ignore[return-value]

    def get_by_id(self, key_id: int) -> ApiKeyRecord | None:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute("SELECT * FROM api_keys WHERE id = ?", (key_id,)).fetchone()
        return _row_to_record(row) if row else None

    def get_by_key(self, api_key: str) -> ApiKeyRecord | None:
        if not api_key:
            return None
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            row = conn.execute(
                "SELECT * FROM api_keys WHERE api_key = ?", (api_key,)
            ).fetchone()
        return _row_to_record(row) if row else None

    def list_all(self) -> list[ApiKeyRecord]:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                "SELECT * FROM api_keys ORDER BY id ASC"
            ).fetchall()
        return [_row_to_record(r) for r in rows]

    def update(
        self,
        key_id: int,
        *,
        name: str | None | object = _UNSET,
        budget_usd: float | None | object = _UNSET,
        rpm: int | None | object = _UNSET,
        tpm: int | None | object = _UNSET,
        allowed_models: list[str] | None | object = _UNSET,
        disabled: bool | None | object = _UNSET,
        metadata: dict[str, Any] | None | object = _UNSET,
        reset_spent: bool = False,
    ) -> ApiKeyRecord | None:
        fields: list[str] = []
        params: list[Any] = []

        if name is not _UNSET and name is not None:
            name_clean = name.strip()
            if not name_clean:
                raise ValueError("API key name must not be empty")
            fields.append("name = ?")
            params.append(name_clean)
        if budget_usd is not _UNSET:
            fields.append("budget_usd = ?")
            params.append(None if budget_usd is None else _validate_budget_usd(budget_usd))
        if rpm is not _UNSET:
            fields.append("rpm = ?")
            params.append(rpm if rpm is not None and rpm > 0 else None)
        if tpm is not _UNSET:
            fields.append("tpm = ?")
            params.append(tpm if tpm is not None and tpm > 0 else None)
        if allowed_models is not _UNSET and allowed_models is not None:
            fields.append("allowed_models = ?")
            params.append(_serialize_allowed_models(allowed_models))
        if disabled is not _UNSET and disabled is not None:
            fields.append("disabled = ?")
            params.append(1 if disabled else 0)
        if metadata is not _UNSET:
            fields.append("metadata = ?")
            if metadata is None:
                params.append(None)
            else:
                validate_metadata_size(metadata)
                params.append(_serialize_metadata(metadata))
        if reset_spent:
            fields.append("spent_usd = 0.0")

        if not fields:
            return self.get_by_id(key_id)

        params.append(key_id)
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            conn.execute(
                f"UPDATE api_keys SET {', '.join(fields)} WHERE id = ?",
                params,
            )
            conn.commit()
        return self.get_by_id(key_id)

    def delete(self, key_id: int) -> bool:
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            conn.commit()
            return cursor.rowcount > 0

    # ------------------------------------------------------------------
    # Budget tracking
    # ------------------------------------------------------------------
    def record_spent(self, key_id: int, cost_usd: float) -> None:
        """Asynchronously increment ``spent_usd`` and refresh ``last_used_at``.

        Goes through the shared :class:`WriteBatcher` so that the chat-logging
        background thread never blocks on the SQLite writer.
        """
        if key_id is None:
            return
        amount = float(cost_usd or 0.0)
        timestamp = datetime.now().isoformat()
        sql = (
            "UPDATE api_keys "
            "SET spent_usd = spent_usd + ?, last_used_at = ? "
            "WHERE id = ?"
        )
        params = (amount, timestamp, key_id)
        if self._batcher is not None:
            self._batcher.enqueue(sql, params)
        else:
            try:
                with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                    conn.execute("PRAGMA busy_timeout=30000")
                    conn.execute(sql, params)
                    conn.commit()
            except Exception:
                logger.exception("Failed to record spent_usd for key %s", key_id)

    def touch_last_used(self, key_id: int) -> None:
        """Update only ``last_used_at`` without modifying budget counters."""
        if key_id is None:
            return
        sql = "UPDATE api_keys SET last_used_at = ? WHERE id = ?"
        params = (datetime.now().isoformat(), key_id)
        if self._batcher is not None:
            self._batcher.enqueue(sql, params)
        else:
            try:
                with sqlite3.connect(self.db_path, timeout=30.0) as conn:
                    conn.execute(sql, params)
                    conn.commit()
            except Exception:
                logger.exception("Failed to refresh last_used_at for key %s", key_id)

    # ------------------------------------------------------------------
    # Async read helpers (for endpoints that already run in an event loop)
    # ------------------------------------------------------------------
    async def aget_by_key(self, api_key: str) -> ApiKeyRecord | None:
        if not api_key:
            return None
        async with aiosqlite.connect(self.db_path) as db:
            for pragma in RUNTIME_PRAGMAS:
                await db.execute(pragma)
            db.row_factory = aiosqlite.Row
            cursor = await db.execute(
                "SELECT * FROM api_keys WHERE api_key = ?", (api_key,)
            )
            row = await cursor.fetchone()
        return _row_to_record(row) if row else None
