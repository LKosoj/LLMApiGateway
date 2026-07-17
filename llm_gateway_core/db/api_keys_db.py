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
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, TypeVar

import aiosqlite

from ..config.paths import resolve_db_dir
from ..services.accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    AccountingError,
    AccountingErrorCode,
    AccountingEventKind,
    AccountingOwnerAuditRow,
    AccountingOwnerState,
    AccountingSinkAuditRow,
    AccountingValidationError,
    ProjectionStatus,
    StoredAccountingEvent,
)
from . import accounting_schema
from .accounting_schema import migrate_accounting_sink
from .write_batcher import WriteBatcher

logger = logging.getLogger(__name__)
_SQLITE_INT64_MAX = (1 << 63) - 1


API_KEY_PREFIX = "lgk_"
API_KEY_TOKEN_NBYTES = 32
MAX_METADATA_BYTES = 16 * 1024
_UNSET = object()
_ACCOUNTING_KEY_UPDATE_FIELDS = frozenset(
    {
        "name",
        "budget_usd",
        "rpm",
        "tpm",
        "allowed_models",
        "disabled",
        "metadata",
        "budget_period",
    }
)
_T = TypeVar("_T")

# Budget reset periods. ``none`` keeps the historical cumulative behaviour
# (``spent_usd`` only ever grows); ``daily``/``monthly`` schedule a periodic
# reset of ``spent_usd`` back to 0 at the next UTC boundary.
VALID_BUDGET_PERIODS = ("none", "daily", "monthly")


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
    budget_period: str = "none"
    budget_reset_at: str | None = None

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


def _validate_budget_period(period: str | None) -> str:
    """Normalize and validate a budget reset period.

    ``None`` / empty string map to the cumulative ``none`` period.
    """
    if period is None:
        return "none"
    normalized = str(period).strip().lower()
    if not normalized:
        return "none"
    if normalized not in VALID_BUDGET_PERIODS:
        raise ValueError(
            "budget_period must be one of: " + ", ".join(VALID_BUDGET_PERIODS)
        )
    return normalized


def compute_next_budget_reset(period: str, *, now: datetime | None = None) -> str | None:
    """Return the next UTC reset boundary (ISO, second precision) for *period*.

    ``daily`` → next midnight UTC; ``monthly`` → first day of next month at
    00:00 UTC. Returns ``None`` for the cumulative ``none`` period. The stored
    value is always second-precision UTC so lexicographic comparison against a
    second-precision UTC ``now`` matches chronological order.
    """
    if period not in ("daily", "monthly"):
        return None
    moment = _normalize_utc_datetime(now)
    if period == "daily":
        nxt = (moment + timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
    else:  # monthly
        year = moment.year + (1 if moment.month == 12 else 0)
        month = 1 if moment.month == 12 else moment.month + 1
        nxt = moment.replace(
            year=year,
            month=month,
            day=1,
            hour=0,
            minute=0,
            second=0,
            microsecond=0,
        )
    return nxt.isoformat()


def _normalize_utc_datetime(moment: datetime | None = None) -> datetime:
    value = moment or datetime.now(timezone.utc)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _utc_now_iso() -> str:
    return _normalize_utc_datetime().replace(microsecond=0).isoformat()


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
        budget_period=(
            row["budget_period"] if "budget_period" in row.keys() else None
        )
        or "none",
        budget_reset_at=(
            row["budget_reset_at"] if "budget_reset_at" in row.keys() else None
        ),
    )


def _accounting_timestamp(value: object) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        raise AccountingValidationError
    return value.astimezone(timezone.utc).isoformat()


def _validate_persisted_accounting_timestamp(value: object) -> None:
    if value is None:
        return
    if not isinstance(value, str):
        raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AccountingError(
            AccountingErrorCode.PROJECTION_WRITE_FAILED
        ) from None
    if (
        parsed.tzinfo is None
        or parsed.utcoffset() is None
        or parsed.astimezone(timezone.utc).isoformat() != value
    ):
        raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)


def _checked_spend_total(current: object, increment: object) -> float:
    if (
        isinstance(current, bool)
        or not isinstance(current, (int, float))
        or not math.isfinite(current)
        or current < 0
        or isinstance(increment, bool)
        or not isinstance(increment, (int, float))
        or not math.isfinite(increment)
        or increment < 0
    ):
        raise AccountingValidationError
    try:
        total = math.fsum((float(current), float(increment)))
    except (OverflowError, ValueError):
        raise AccountingValidationError from None
    if not math.isfinite(total) or total < 0:
        raise AccountingValidationError
    return 0.0 if total == 0.0 else total


def _receipt_matches(
    row: tuple | None,
    *,
    billing_fingerprint: str,
    api_key_id: int,
    spend_usd: float,
) -> bool:
    return row == (billing_fingerprint, api_key_id, spend_usd)


def _validate_accounting_audit_event_id(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise AccountingValidationError
    try:
        encoded = value.encode("ascii")
    except UnicodeEncodeError:
        raise AccountingValidationError from None
    if not 1 <= len(encoded) <= 255 or any(
        not 0x20 <= byte <= 0x7E for byte in encoded
    ):
        raise AccountingValidationError
    return value


def _validate_accounting_audit_limit(limit: object) -> int:
    if (
        isinstance(limit, bool)
        or not isinstance(limit, int)
        or not 1 <= limit <= ACCOUNTING_AUDIT_MAX_PAGE_SIZE
    ):
        raise AccountingValidationError
    return limit


def _validate_accounting_key_id(key_id: object) -> int:
    if (
        isinstance(key_id, bool)
        or not isinstance(key_id, int)
        or not 1 <= key_id <= _SQLITE_INT64_MAX
    ):
        raise AccountingValidationError
    return key_id


def _prepare_accounting_key_update(
    changes: Mapping[str, object],
    *,
    reset_spent: bool,
) -> tuple[list[str], list[object]]:
    if not isinstance(changes, Mapping) or not isinstance(reset_spent, bool):
        raise AccountingValidationError
    if any(
        not isinstance(field_name, str)
        or field_name not in _ACCOUNTING_KEY_UPDATE_FIELDS
        for field_name in changes
    ):
        raise AccountingValidationError

    fields: list[str] = []
    params: list[object] = []
    try:
        for field_name, value in changes.items():
            if field_name == "name":
                if value is None:
                    continue
                if not isinstance(value, str):
                    raise AccountingValidationError
                name_clean = value.strip()
                if not name_clean:
                    raise AccountingValidationError
                fields.append("name = ?")
                params.append(name_clean)
            elif field_name == "budget_usd":
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, (int, float))
                ):
                    raise AccountingValidationError
                fields.append("budget_usd = ?")
                params.append(None if value is None else _validate_budget_usd(value))
            elif field_name in {"rpm", "tpm"}:
                if value is not None and (
                    isinstance(value, bool) or not isinstance(value, int)
                ):
                    raise AccountingValidationError
                if value is not None and value > _SQLITE_INT64_MAX:
                    raise AccountingValidationError
                fields.append(f"{field_name} = ?")
                params.append(value if value is not None and value > 0 else None)
            elif field_name == "allowed_models":
                if value is None:
                    continue
                if not isinstance(value, list) or any(
                    not isinstance(model, str) for model in value
                ):
                    raise AccountingValidationError
                fields.append("allowed_models = ?")
                params.append(_serialize_allowed_models(value))
            elif field_name == "disabled":
                if value is None:
                    continue
                if not isinstance(value, bool):
                    raise AccountingValidationError
                fields.append("disabled = ?")
                params.append(1 if value else 0)
            elif field_name == "metadata":
                if value is not None and not isinstance(value, dict):
                    raise AccountingValidationError
                fields.append("metadata = ?")
                if value is None:
                    params.append(None)
                else:
                    validate_metadata_size(value)
                    params.append(_serialize_metadata(value))
            else:
                if value is not None and not isinstance(value, str):
                    raise AccountingValidationError
                period_value = _validate_budget_period(value)
                fields.extend(("budget_period = ?", "budget_reset_at = ?"))
                params.extend((period_value, compute_next_budget_reset(period_value)))
    except AccountingValidationError:
        raise
    except Exception:
        raise AccountingValidationError from None
    if reset_spent:
        fields.append("spent_usd = 0.0")
    return fields, params


def _parse_accounting_audit_timestamp(value: object) -> datetime:
    if not isinstance(value, str):
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH) from None
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
    normalized = parsed.astimezone(timezone.utc)
    if normalized.isoformat() != value:
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
    return normalized


def _accounting_sink_audit_row(row: sqlite3.Row) -> AccountingSinkAuditRow:
    try:
        return AccountingSinkAuditRow(
            event_id=row["event_id"],
            billing_fingerprint=row["billing_fingerprint"],
            api_key_id=row["api_key_id"],
            spend_usd=row["spend_usd"],
            applied_at=_parse_accounting_audit_timestamp(row["applied_at"]),
            sink_kind=row["sink_kind"],
            owner_state=row["owner_state"],
        )
    except AccountingValidationError:
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH) from None
    except (IndexError, KeyError, TypeError):
        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH) from None


def _close_accounting_audit_connection(
    conn: sqlite3.Connection,
    *,
    primary_error: BaseException | None,
) -> None:
    try:
        conn.close()
    except BaseException as close_error:
        if primary_error is not None:
            if not isinstance(close_error, Exception) and isinstance(
                primary_error,
                Exception,
            ):
                raise close_error.with_traceback(close_error.__traceback__)
            return
        if isinstance(close_error, Exception):
            raise AccountingError(AccountingErrorCode.RECONCILE_FAILED) from None
        raise


def _recover_accounting_key_result(
    db_path: str | os.PathLike[str],
    recovery: Callable[[sqlite3.Connection], _T],
) -> _T:
    conn: sqlite3.Connection | None = None
    primary_error: BaseException | None = None
    try:
        conn = accounting_schema.open_accounting_runtime_connection(
            db_path,
            enable_foreign_keys=False,
        )
        conn.row_factory = sqlite3.Row
        return recovery(conn)
    except BaseException as exc:
        if isinstance(exc, AccountingError) or not isinstance(exc, Exception):
            primary_error = exc
        else:
            primary_error = AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
        if primary_error is exc:
            raise
        raise primary_error from None
    finally:
        if conn is not None:
            try:
                conn.close()
            except BaseException as close_error:
                if primary_error is not None:
                    if isinstance(primary_error, Exception) and not isinstance(
                        close_error,
                        Exception,
                    ):
                        raise close_error.with_traceback(close_error.__traceback__) from None
                elif not isinstance(close_error, Exception):
                    raise


def _cleanup_accounting_key_transaction(
    conn: sqlite3.Connection,
    *,
    primary_error: BaseException,
) -> BaseException:
    selected_error = primary_error
    try:
        if conn.in_transaction:
            conn.rollback()
    except BaseException as rollback_error:
        if not isinstance(rollback_error, Exception) and isinstance(
            selected_error,
            Exception,
        ):
            selected_error = rollback_error
    try:
        conn.close()
    except BaseException as close_error:
        if not isinstance(close_error, Exception) and isinstance(
            selected_error,
            Exception,
        ):
            selected_error = close_error
    return selected_error


def _run_accounting_key_transaction(
    db_path: str | os.PathLike[str],
    operation: Callable[[sqlite3.Connection], _T],
    recovery: Callable[[sqlite3.Connection], _T],
) -> _T:
    try:
        conn = accounting_schema.open_accounting_runtime_connection(
            db_path,
            enable_foreign_keys=False,
        )
    except BaseException as exc:
        if not isinstance(exc, Exception):
            raise
        raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None

    try:
        conn.row_factory = sqlite3.Row
        conn.execute("BEGIN IMMEDIATE")
        result = operation(conn)
    except BaseException as operation_error:
        primary_error: BaseException
        if isinstance(operation_error, AccountingError) or not isinstance(
            operation_error,
            Exception,
        ):
            primary_error = operation_error
        else:
            primary_error = AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
        selected_error = _cleanup_accounting_key_transaction(
            conn,
            primary_error=primary_error,
        )
        if selected_error is operation_error:
            raise operation_error.with_traceback(operation_error.__traceback__) from None
        raise selected_error.with_traceback(selected_error.__traceback__) from None

    try:
        accounting_schema.commit_accounting_transaction(conn)
    except BaseException as commit_error:
        selected_error = _cleanup_accounting_key_transaction(
            conn,
            primary_error=commit_error,
        )
        if not isinstance(selected_error, Exception):
            raise selected_error.with_traceback(selected_error.__traceback__) from None
        return _recover_accounting_key_result(db_path, recovery)

    try:
        conn.close()
    except BaseException as close_error:
        if not isinstance(close_error, Exception):
            raise close_error.with_traceback(close_error.__traceback__) from None
        return _recover_accounting_key_result(db_path, recovery)
    return result


class ApiKeysDB:
    """CRUD + budget-tracking storage for virtual API keys."""

    def __init__(
        self,
        db_filename: str = "api_keys.db",
        write_batcher: WriteBatcher | None = None,
    ) -> None:
        db_dir = resolve_db_dir(__file__)
        os.makedirs(db_dir, exist_ok=True)
        self.db_path = db_dir / db_filename
        self._batcher: WriteBatcher | None = None
        if write_batcher is not None:
            self.set_batcher(write_batcher)
        self._init_db()
        migrate_accounting_sink(self.db_path)

    def set_batcher(self, batcher: WriteBatcher) -> None:
        if batcher.db_path.resolve() != self.db_path.resolve():
            raise ValueError(
                "ApiKeysDB requires a WriteBatcher bound to the same SQLite "
                f"database: got {batcher.db_path}, expected {self.db_path}."
            )
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
                    last_used_at DATETIME,
                    budget_period TEXT NOT NULL DEFAULT 'none',
                    budget_reset_at DATETIME
                )
            """)
            conn.execute("CREATE INDEX IF NOT EXISTS idx_api_keys_key ON api_keys (api_key)")
            self._ensure_budget_period_columns(conn)
            conn.commit()
            try:
                os.chmod(self.db_path, 0o600)
            except OSError:
                logger.warning(
                    "Could not tighten permissions on API keys database %s", self.db_path
                )
            logger.info("API keys database initialized at %s", self.db_path)
        except Exception:
            logger.exception("Error initializing API keys database")
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    @staticmethod
    def _ensure_budget_period_columns(conn: sqlite3.Connection) -> None:
        """Idempotently add the periodic-budget columns to legacy databases."""
        existing = {row[1] for row in conn.execute("PRAGMA table_info(api_keys)")}
        if "budget_period" not in existing:
            conn.execute(
                "ALTER TABLE api_keys ADD COLUMN budget_period TEXT NOT NULL DEFAULT 'none'"
            )
        if "budget_reset_at" not in existing:
            conn.execute("ALTER TABLE api_keys ADD COLUMN budget_reset_at DATETIME")

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
        budget_period: str | None = None,
    ) -> ApiKeyRecord:
        name = (name or "").strip()
        if not name:
            raise ValueError("API key name must not be empty")
        budget_value = _validate_budget_usd(budget_usd)
        period_value = _validate_budget_period(budget_period)
        reset_at_value = compute_next_budget_reset(period_value)
        validate_metadata_size(metadata)

        key_value = api_key or generate_api_key()
        created_at = _utc_now_iso()
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
            period_value,
            reset_at_value,
        )
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.execute("PRAGMA busy_timeout=30000")
            try:
                cursor = conn.execute(
                    """
                    INSERT INTO api_keys
                    (name, api_key, budget_usd, spent_usd, rpm, tpm, allowed_models,
                     disabled, metadata, created_at, budget_period, budget_reset_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
        budget_period: str | None | object = _UNSET,
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
        if budget_period is not _UNSET:
            # Recomputing the reset boundary on every explicit write is
            # idempotent within the current period window (daily → same next
            # midnight, monthly → same first-of-next-month), so an unrelated
            # edit that re-sends the period never drifts the schedule.
            period_value = _validate_budget_period(budget_period)
            fields.append("budget_period = ?")
            params.append(period_value)
            fields.append("budget_reset_at = ?")
            params.append(compute_next_budget_reset(period_value))
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

    def update_accounting_key(
        self,
        key_id: int,
        *,
        changes: Mapping[str, object],
        reset_spent: bool = False,
    ) -> ApiKeyRecord | None:
        """Apply one accounting-owned key patch in a durable transaction."""

        key_id = _validate_accounting_key_id(key_id)
        if not isinstance(changes, Mapping):
            raise AccountingValidationError
        try:
            changes_snapshot = dict(changes)
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise AccountingValidationError from None
        fields, params = _prepare_accounting_key_update(
            changes_snapshot,
            reset_spent=reset_spent,
        )
        expected_result: ApiKeyRecord | None = None

        def update_key(conn: sqlite3.Connection) -> ApiKeyRecord | None:
            nonlocal expected_result
            active_row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            tombstone_row = conn.execute(
                """
                SELECT 1 FROM api_key_accounting_tombstones
                WHERE api_key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if active_row is not None and tombstone_row is not None:
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
            if active_row is None:
                return None
            _checked_spend_total(active_row["spent_usd"], 0.0)
            _validate_persisted_accounting_timestamp(active_row["last_used_at"])
            _validate_persisted_accounting_timestamp(active_row["budget_reset_at"])
            if fields:
                cursor = conn.execute(
                    f"UPDATE api_keys SET {', '.join(fields)} WHERE id = ?",
                    (*params, key_id),
                )
                if cursor.rowcount != 1:
                    raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
                active_row = conn.execute(
                    "SELECT * FROM api_keys WHERE id = ?",
                    (key_id,),
                ).fetchone()
            if active_row is None:
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
            expected_result = _row_to_record(active_row)
            return expected_result

        def recover_update(conn: sqlite3.Connection) -> ApiKeyRecord | None:
            active_row = conn.execute(
                "SELECT * FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            tombstone_row = conn.execute(
                """
                SELECT 1 FROM api_key_accounting_tombstones
                WHERE api_key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if expected_result is None:
                if active_row is not None:
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
                return None
            if active_row is None or tombstone_row is not None:
                raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
            recovered = _row_to_record(active_row)
            if recovered != expected_result:
                raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
            return recovered

        return _run_accounting_key_transaction(
            self.db_path,
            update_key,
            recover_update,
        )

    def reset_accounting_spend(self, key_id: int) -> ApiKeyRecord | None:
        """Reset spend without moving an existing periodic boundary."""

        return self.update_accounting_key(key_id, changes={}, reset_spent=True)

    def list_due_budget_key_ids(
        self,
        *,
        now: datetime,
        limit: int,
        after_key_id: int | None = None,
    ) -> tuple[int, ...]:
        """Return one bounded ID-ordered page of currently due periodic keys."""

        now_iso = _accounting_timestamp(now)
        limit = _validate_accounting_audit_limit(limit)
        if after_key_id is not None:
            after_key_id = _validate_accounting_key_id(after_key_id)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = accounting_schema.open_accounting_runtime_connection(
                self.db_path,
                enable_foreign_keys=False,
            )
            conn.row_factory = sqlite3.Row
            if after_key_id is None:
                rows = conn.execute(
                    """
                    SELECT id FROM api_keys
                    WHERE budget_period IN ('daily', 'monthly')
                      AND budget_reset_at IS NOT NULL
                      AND budget_reset_at <= ?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (now_iso, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT id FROM api_keys
                    WHERE id > ?
                      AND budget_period IN ('daily', 'monthly')
                      AND budget_reset_at IS NOT NULL
                      AND budget_reset_at <= ?
                    ORDER BY id
                    LIMIT ?
                    """,
                    (after_key_id, now_iso, limit),
                ).fetchall()
            key_ids = tuple(_validate_accounting_key_id(row[0]) for row in rows)
            if len(key_ids) > limit or tuple(sorted(key_ids)) != key_ids:
                raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
            return key_ids
        except BaseException as exc:
            if isinstance(exc, AccountingError) or not isinstance(exc, Exception):
                primary_error = exc
            else:
                primary_error = AccountingError(
                    AccountingErrorCode.PROJECTION_WRITE_FAILED
                )
            if primary_error is exc:
                raise
            raise primary_error from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except BaseException as close_error:
                    if primary_error is not None:
                        if isinstance(primary_error, Exception) and not isinstance(
                            close_error,
                            Exception,
                        ):
                            raise close_error.with_traceback(
                                close_error.__traceback__
                            ) from None
                    elif isinstance(close_error, Exception):
                        raise AccountingError(
                            AccountingErrorCode.PROJECTION_WRITE_FAILED
                        ) from None
                    else:
                        raise

    def reset_due_accounting_budgets(
        self,
        *,
        now: datetime,
        eligible_key_ids: tuple[int, ...],
    ) -> tuple[ApiKeyRecord, ...]:
        """Reset only supplied keys that remain exactly due under the write lock."""

        moment = datetime.fromisoformat(_accounting_timestamp(now))
        if (
            type(eligible_key_ids) is not tuple
            or len(eligible_key_ids) > ACCOUNTING_AUDIT_MAX_PAGE_SIZE
            or len(set(eligible_key_ids)) != len(eligible_key_ids)
        ):
            raise AccountingValidationError
        key_ids = tuple(
            _validate_accounting_key_id(key_id) for key_id in eligible_key_ids
        )
        if not key_ids:
            return ()

        expected_results: tuple[ApiKeyRecord, ...] = ()

        def reset_due(conn: sqlite3.Connection) -> tuple[ApiKeyRecord, ...]:
            nonlocal expected_results
            reset_records: list[ApiKeyRecord] = []
            for key_id in key_ids:
                row = conn.execute(
                    "SELECT * FROM api_keys WHERE id = ?",
                    (key_id,),
                ).fetchone()
                tombstone_row = conn.execute(
                    """
                    SELECT 1 FROM api_key_accounting_tombstones
                    WHERE api_key_id = ?
                    """,
                    (key_id,),
                ).fetchone()
                if row is not None and tombstone_row is not None:
                    raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
                if row is None:
                    continue
                _checked_spend_total(row["spent_usd"], 0.0)
                _validate_persisted_accounting_timestamp(row["last_used_at"])
                budget_period = row["budget_period"]
                budget_reset_at = row["budget_reset_at"]
                if (
                    budget_period not in {"daily", "monthly"}
                    or budget_reset_at is None
                ):
                    continue
                _validate_persisted_accounting_timestamp(budget_reset_at)
                if datetime.fromisoformat(budget_reset_at) > moment:
                    continue
                next_reset = compute_next_budget_reset(
                    budget_period,
                    now=moment,
                )
                cursor = conn.execute(
                    """
                    UPDATE api_keys
                    SET spent_usd = 0.0, budget_reset_at = ?
                    WHERE id = ?
                      AND budget_period = ?
                      AND budget_reset_at = ?
                    """,
                    (
                        next_reset,
                        key_id,
                        budget_period,
                        budget_reset_at,
                    ),
                )
                if cursor.rowcount != 1:
                    raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
                updated = conn.execute(
                    "SELECT * FROM api_keys WHERE id = ?",
                    (key_id,),
                ).fetchone()
                if updated is None:
                    raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
                reset_records.append(_row_to_record(updated))
            expected_results = tuple(reset_records)
            return expected_results

        def recover_due_reset(
            conn: sqlite3.Connection,
        ) -> tuple[ApiKeyRecord, ...]:
            recovered_records: list[ApiKeyRecord] = []
            for expected in expected_results:
                active_row = conn.execute(
                    "SELECT * FROM api_keys WHERE id = ?",
                    (expected.id,),
                ).fetchone()
                tombstone_row = conn.execute(
                    """
                    SELECT 1 FROM api_key_accounting_tombstones
                    WHERE api_key_id = ?
                    """,
                    (expected.id,),
                ).fetchone()
                if active_row is None or tombstone_row is not None:
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
                recovered = _row_to_record(active_row)
                if recovered != expected:
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
                recovered_records.append(recovered)
            return tuple(recovered_records)

        return _run_accounting_key_transaction(
            self.db_path,
            reset_due,
            recover_due_reset,
        )

    def delete_to_accounting_tombstone(
        self,
        key_id: int,
        *,
        deleted_at: datetime,
    ) -> bool:
        """Atomically remove a credential and retain only non-secret spend state."""

        key_id = _validate_accounting_key_id(key_id)
        deleted_at_iso = _accounting_timestamp(deleted_at)
        expected_deleted = False
        expected_tombstone: tuple[object, ...] | None = None

        def tombstone_key(conn: sqlite3.Connection) -> bool:
            nonlocal expected_deleted, expected_tombstone
            active_row = conn.execute(
                """
                SELECT spent_usd, last_used_at FROM api_keys WHERE id = ?
                """,
                (key_id,),
            ).fetchone()
            tombstone_row = conn.execute(
                """
                SELECT 1 FROM api_key_accounting_tombstones
                WHERE api_key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if active_row is not None and tombstone_row is not None:
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
            if active_row is None:
                return False

            spent_usd = _checked_spend_total(active_row["spent_usd"], 0.0)
            last_used_at = active_row["last_used_at"]
            _validate_persisted_accounting_timestamp(last_used_at)
            conn.execute(
                """
                INSERT INTO api_key_accounting_tombstones (
                    api_key_id, deleted_at, spent_usd, last_used_at
                ) VALUES (?, ?, ?, ?)
                """,
                (key_id, deleted_at_iso, spent_usd, last_used_at),
            )
            cursor = conn.execute("DELETE FROM api_keys WHERE id = ?", (key_id,))
            if cursor.rowcount != 1:
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)
            expected_deleted = True
            expected_tombstone = (
                key_id,
                deleted_at_iso,
                spent_usd,
                last_used_at,
            )
            return True

        def recover_delete(conn: sqlite3.Connection) -> bool:
            active_row = conn.execute(
                "SELECT 1 FROM api_keys WHERE id = ?",
                (key_id,),
            ).fetchone()
            tombstone_row = conn.execute(
                """
                SELECT api_key_id, deleted_at, spent_usd, last_used_at
                FROM api_key_accounting_tombstones
                WHERE api_key_id = ?
                """,
                (key_id,),
            ).fetchone()
            if not expected_deleted:
                if active_row is not None:
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
                return False
            if (
                active_row is not None
                or tombstone_row is None
                or tuple(tombstone_row) != expected_tombstone
            ):
                raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED)
            return True

        return _run_accounting_key_transaction(
            self.db_path,
            tombstone_key,
            recover_delete,
        )

    # ------------------------------------------------------------------
    # Budget tracking
    # ------------------------------------------------------------------
    def apply_spend_event(
        self,
        stored_event: StoredAccountingEvent,
        applied_at: datetime,
    ) -> ProjectionStatus:
        """Atomically apply one keyed charge and persist its idempotency receipt."""

        if not isinstance(stored_event, StoredAccountingEvent):
            raise AccountingValidationError
        event = stored_event.event
        api_key_id = event.api_key_id
        if (
            event.kind is not AccountingEventKind.CHARGE
            or isinstance(api_key_id, bool)
            or not isinstance(api_key_id, int)
            or api_key_id <= 0
        ):
            raise AccountingValidationError

        event_id = event.event_id
        billing_fingerprint = stored_event.billing_fingerprint
        spend_usd = event.usage.cost
        occurred_at_iso = _accounting_timestamp(event.occurred_at)
        applied_at_iso = _accounting_timestamp(applied_at)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = accounting_schema.open_accounting_runtime_connection(
                self.db_path,
                enable_foreign_keys=False,
            )
            conn.execute("BEGIN IMMEDIATE")
            existing_receipt = conn.execute(
                """
                SELECT billing_fingerprint, api_key_id, spend_usd
                FROM applied_accounting_events
                WHERE event_id = ?
                """,
                (event_id,),
            ).fetchone()
            if existing_receipt is not None:
                if not _receipt_matches(
                    existing_receipt,
                    billing_fingerprint=billing_fingerprint,
                    api_key_id=api_key_id,
                    spend_usd=spend_usd,
                ):
                    raise AccountingError(AccountingErrorCode.FINGERPRINT_CONFLICT)
                conn.rollback()
                return ProjectionStatus.ALREADY_APPLIED

            active_row = conn.execute(
                "SELECT spent_usd, last_used_at FROM api_keys WHERE id = ?",
                (api_key_id,),
            ).fetchone()
            tombstone_row = conn.execute(
                """
                SELECT spent_usd, last_used_at
                FROM api_key_accounting_tombstones
                WHERE api_key_id = ?
                """,
                (api_key_id,),
            ).fetchone()
            if (active_row is None) == (tombstone_row is None):
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)

            if active_row is not None:
                sink_kind = "active"
                current_spend = active_row[0]
                last_used_at = active_row[1]
            else:
                sink_kind = "tombstone"
                current_spend = tombstone_row[0]
                last_used_at = tombstone_row[1]

            _validate_persisted_accounting_timestamp(last_used_at)
            next_spend = _checked_spend_total(current_spend, spend_usd)
            if active_row is not None:
                cursor = conn.execute(
                    """
                    UPDATE api_keys
                    SET spent_usd = ?,
                        last_used_at = CASE
                            WHEN last_used_at IS NULL OR last_used_at < ? THEN ?
                            ELSE last_used_at
                        END
                    WHERE id = ?
                    """,
                    (next_spend, occurred_at_iso, occurred_at_iso, api_key_id),
                )
            else:
                cursor = conn.execute(
                    """
                    UPDATE api_key_accounting_tombstones
                    SET spent_usd = ?,
                        last_used_at = CASE
                            WHEN last_used_at IS NULL OR last_used_at < ? THEN ?
                            ELSE last_used_at
                        END
                    WHERE api_key_id = ?
                    """,
                    (next_spend, occurred_at_iso, occurred_at_iso, api_key_id),
                )
            if cursor.rowcount != 1:
                raise AccountingError(AccountingErrorCode.ORPHAN_SINK)

            conn.execute(
                """
                INSERT INTO applied_accounting_events (
                    event_id, billing_fingerprint, api_key_id,
                    spend_usd, applied_at, sink_kind
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    event_id,
                    billing_fingerprint,
                    api_key_id,
                    spend_usd,
                    applied_at_iso,
                    sink_kind,
                ),
            )
            try:
                accounting_schema.commit_accounting_transaction(conn)
            except BaseException as commit_error:
                active_conn = conn
                conn = None
                secondary_error: BaseException | None = None
                try:
                    if active_conn.in_transaction:
                        active_conn.rollback()
                except BaseException as exc:
                    secondary_error = exc
                try:
                    active_conn.close()
                except BaseException as exc:
                    if secondary_error is None or (
                        isinstance(secondary_error, Exception)
                        and not isinstance(exc, Exception)
                    ):
                        secondary_error = exc

                recovered_receipt = None
                recovery_conn: sqlite3.Connection | None = None
                try:
                    recovery_conn = accounting_schema.open_accounting_runtime_connection(
                        self.db_path,
                        enable_foreign_keys=False,
                    )
                    recovered_receipt = recovery_conn.execute(
                        """
                        SELECT billing_fingerprint, api_key_id, spend_usd
                        FROM applied_accounting_events
                        WHERE event_id = ?
                        """,
                        (event_id,),
                    ).fetchone()
                except BaseException as exc:
                    if secondary_error is None or (
                        isinstance(secondary_error, Exception)
                        and not isinstance(exc, Exception)
                    ):
                        secondary_error = exc
                finally:
                    if recovery_conn is not None:
                        try:
                            recovery_conn.close()
                        except BaseException as exc:
                            if secondary_error is None or (
                                isinstance(secondary_error, Exception)
                                and not isinstance(exc, Exception)
                            ):
                                secondary_error = exc

                if not isinstance(commit_error, Exception):
                    raise commit_error.with_traceback(commit_error.__traceback__)
                if secondary_error is not None:
                    if not isinstance(secondary_error, Exception):
                        raise secondary_error.with_traceback(
                            secondary_error.__traceback__
                        ) from None
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
                if recovered_receipt is None:
                    raise AccountingError(AccountingErrorCode.PROJECTION_WRITE_FAILED) from None
                if not _receipt_matches(
                    recovered_receipt,
                    billing_fingerprint=billing_fingerprint,
                    api_key_id=api_key_id,
                    spend_usd=spend_usd,
                ):
                    raise AccountingError(
                        AccountingErrorCode.FINGERPRINT_CONFLICT
                    ) from None
                return ProjectionStatus.ALREADY_APPLIED
            return ProjectionStatus.APPLIED
        except BaseException as exc:
            if isinstance(exc, AccountingError) or not isinstance(exc, Exception):
                primary_error = exc
            else:
                primary_error = AccountingError(
                    AccountingErrorCode.PROJECTION_WRITE_FAILED
                )
            rollback_error: BaseException | None = None
            try:
                if conn is not None and conn.in_transaction:
                    conn.rollback()
            except BaseException as cleanup_error:
                rollback_error = cleanup_error
            if (
                rollback_error is not None
                and not isinstance(rollback_error, Exception)
                and isinstance(primary_error, Exception)
            ):
                primary_error = rollback_error
                raise rollback_error.with_traceback(rollback_error.__traceback__) from None
            if primary_error is exc:
                raise
            raise primary_error from None
        finally:
            if conn is not None:
                try:
                    conn.close()
                except BaseException as close_error:
                    if primary_error is not None:
                        if isinstance(primary_error, Exception) and not isinstance(
                            close_error,
                            Exception,
                        ):
                            raise close_error.with_traceback(
                                close_error.__traceback__
                            ) from None
                    elif isinstance(close_error, Exception):
                        raise AccountingError(
                            AccountingErrorCode.PROJECTION_WRITE_FAILED
                        ) from None
                    else:
                        raise

    def list_accounting_sink_audit_rows(
        self,
        *,
        limit: int,
        after_event_id: str | None = None,
    ) -> tuple[AccountingSinkAuditRow, ...]:
        """Return one bounded, strictly ordered page of persisted receipts."""

        limit = _validate_accounting_audit_limit(limit)
        if after_event_id is not None:
            after_event_id = _validate_accounting_audit_event_id(after_event_id)

        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = accounting_schema.open_accounting_runtime_connection(
                self.db_path,
                enable_foreign_keys=False,
            )
            conn.row_factory = sqlite3.Row
            if after_event_id is None:
                rows = conn.execute(
                    """
                    SELECT receipt.event_id, receipt.billing_fingerprint,
                           receipt.api_key_id, receipt.spend_usd,
                           receipt.applied_at, receipt.sink_kind,
                           CASE
                               WHEN active.id IS NOT NULL
                                AND tombstone.api_key_id IS NOT NULL
                                   THEN 'ambiguous'
                               WHEN active.id IS NOT NULL THEN 'active'
                               WHEN tombstone.api_key_id IS NOT NULL THEN 'tombstone'
                               ELSE 'missing'
                           END AS owner_state
                    FROM applied_accounting_events AS receipt
                    LEFT JOIN api_keys AS active
                           ON active.id = receipt.api_key_id
                    LEFT JOIN api_key_accounting_tombstones AS tombstone
                           ON tombstone.api_key_id = receipt.api_key_id
                    ORDER BY receipt.event_id COLLATE BINARY
                    LIMIT ?
                    """,
                    (limit,),
                ).fetchall()
            else:
                rows = conn.execute(
                    """
                    SELECT receipt.event_id, receipt.billing_fingerprint,
                           receipt.api_key_id, receipt.spend_usd,
                           receipt.applied_at, receipt.sink_kind,
                           CASE
                               WHEN active.id IS NOT NULL
                                AND tombstone.api_key_id IS NOT NULL
                                   THEN 'ambiguous'
                               WHEN active.id IS NOT NULL THEN 'active'
                               WHEN tombstone.api_key_id IS NOT NULL THEN 'tombstone'
                               ELSE 'missing'
                           END AS owner_state
                    FROM applied_accounting_events AS receipt
                    LEFT JOIN api_keys AS active
                           ON active.id = receipt.api_key_id
                    LEFT JOIN api_key_accounting_tombstones AS tombstone
                           ON tombstone.api_key_id = receipt.api_key_id
                    WHERE receipt.event_id COLLATE BINARY > ?
                    ORDER BY receipt.event_id COLLATE BINARY
                    LIMIT ?
                    """,
                    (after_event_id, limit),
                ).fetchall()

            if len(rows) > limit:
                raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
            audit_rows: list[AccountingSinkAuditRow] = []
            previous_event_id = after_event_id
            for raw_row in rows:
                audit_row = _accounting_sink_audit_row(raw_row)
                if (
                    previous_event_id is not None
                    and audit_row.event_id <= previous_event_id
                ):
                    raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
                audit_rows.append(audit_row)
                previous_event_id = audit_row.event_id
            return tuple(audit_rows)
        except AccountingError as exc:
            primary_error = exc
            raise
        except Exception:
            primary_error = AccountingError(AccountingErrorCode.RECONCILE_FAILED)
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if conn is not None:
                _close_accounting_audit_connection(
                    conn,
                    primary_error=primary_error,
                )

    def get_accounting_owner_states(
        self,
        *,
        api_key_ids: tuple[int, ...],
    ) -> tuple[AccountingOwnerAuditRow, ...]:
        """Resolve a bounded owner batch without exposing key material."""

        if type(api_key_ids) is not tuple or len(api_key_ids) > ACCOUNTING_AUDIT_MAX_PAGE_SIZE:
            raise AccountingValidationError
        if any(
            type(api_key_id) is not int
            or api_key_id <= 0
            or api_key_id > _SQLITE_INT64_MAX
            for api_key_id in api_key_ids
        ):
            raise AccountingValidationError
        if len(set(api_key_ids)) != len(api_key_ids):
            raise AccountingValidationError
        if not api_key_ids:
            return ()

        values_sql = ", ".join(
            f"({ordinal}, ?)" for ordinal in range(len(api_key_ids))
        )
        conn: sqlite3.Connection | None = None
        primary_error: BaseException | None = None
        try:
            conn = accounting_schema.open_accounting_runtime_connection(
                self.db_path,
                enable_foreign_keys=False,
            )
            conn.row_factory = sqlite3.Row
            rows = conn.execute(
                f"""
                WITH requested(ordinal, api_key_id) AS (
                    VALUES {values_sql}
                )
                SELECT requested.ordinal, requested.api_key_id,
                       CASE
                           WHEN active.id IS NOT NULL
                            AND tombstone.api_key_id IS NOT NULL
                               THEN 'ambiguous'
                           WHEN active.id IS NOT NULL THEN 'active'
                           WHEN tombstone.api_key_id IS NOT NULL THEN 'tombstone'
                           ELSE 'missing'
                       END AS owner_state
                FROM requested
                LEFT JOIN api_keys AS active
                       ON active.id = requested.api_key_id
                LEFT JOIN api_key_accounting_tombstones AS tombstone
                       ON tombstone.api_key_id = requested.api_key_id
                ORDER BY requested.ordinal
                """,
                api_key_ids,
            ).fetchall()

            if len(rows) != len(api_key_ids):
                raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
            owner_rows: list[AccountingOwnerAuditRow] = []
            for ordinal, raw_row in enumerate(rows):
                try:
                    if (
                        raw_row["ordinal"] != ordinal
                        or raw_row["api_key_id"] != api_key_ids[ordinal]
                    ):
                        raise AccountingError(AccountingErrorCode.SCHEMA_MISMATCH)
                    owner_rows.append(
                        AccountingOwnerAuditRow(
                            api_key_id=raw_row["api_key_id"],
                            owner_state=AccountingOwnerState(raw_row["owner_state"]),
                        )
                    )
                except AccountingValidationError:
                    raise AccountingError(
                        AccountingErrorCode.SCHEMA_MISMATCH
                    ) from None
                except AccountingError:
                    raise
                except (IndexError, KeyError, TypeError, ValueError):
                    raise AccountingError(
                        AccountingErrorCode.SCHEMA_MISMATCH
                    ) from None
            return tuple(owner_rows)
        except AccountingError as exc:
            primary_error = exc
            raise
        except Exception:
            primary_error = AccountingError(AccountingErrorCode.RECONCILE_FAILED)
            raise primary_error from None
        except BaseException as exc:
            primary_error = exc
            raise
        finally:
            if conn is not None:
                _close_accounting_audit_connection(
                    conn,
                    primary_error=primary_error,
                )

    def record_spent(self, key_id: int, cost_usd: float) -> None:
        """Asynchronously increment ``spent_usd`` and refresh ``last_used_at``.

        Goes through the shared :class:`WriteBatcher` so that the chat-logging
        background thread never blocks on the SQLite writer.
        """
        if key_id is None:
            return
        amount = float(cost_usd or 0.0)
        timestamp = _utc_now_iso()
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

    def reset_due_budgets(self, *, now: datetime | None = None) -> list[ApiKeyRecord]:
        """Reset ``spent_usd`` for periodic keys whose reset boundary has passed.

        Only ``daily``/``monthly`` keys with a non-NULL ``budget_reset_at`` in
        the past are touched, so cumulative (``none``) keys keep their historical
        behaviour. Returns the post-reset records so the caller can refresh any
        in-memory budget ledger.
        """
        moment = _normalize_utc_datetime(now)
        now_iso = moment.replace(microsecond=0).isoformat()

        reset_records: list[ApiKeyRecord] = []
        with sqlite3.connect(self.db_path, timeout=30.0) as conn:
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA busy_timeout=30000")
            # Serialize the read-modify-write against concurrent record_spent()
            # writers (which run on separate connections): an IMMEDIATE
            # transaction takes the write lock up front, so a spend committed
            # between the SELECT and the zeroing UPDATE can no longer be
            # silently wiped — a competing writer waits and then accumulates
            # from the freshly reset 0.0 (i.e. against the new period).
            conn.execute("BEGIN IMMEDIATE")
            rows = conn.execute(
                "SELECT * FROM api_keys "
                "WHERE budget_period IN ('daily', 'monthly') "
                "AND budget_reset_at IS NOT NULL "
                "AND budget_reset_at <= ?",
                (now_iso,),
            ).fetchall()
            for row in rows:
                next_reset = compute_next_budget_reset(row["budget_period"], now=moment)
                conn.execute(
                    "UPDATE api_keys SET spent_usd = 0.0, budget_reset_at = ? WHERE id = ?",
                    (next_reset, row["id"]),
                )
                record = _row_to_record(row)
                record.spent_usd = 0.0
                record.budget_reset_at = next_reset
                reset_records.append(record)
            conn.commit()
        return reset_records
