from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from cryptography.fernet import Fernet, InvalidToken

from ..config.paths import resolve_db_dir
from .write_batcher import RUNTIME_PRAGMAS

logger = logging.getLogger(__name__)


STATUS_ACTIVE = "active"
STATUS_REAUTH_REQUIRED = "reauth_required"


@dataclass
class OAuthCredentialRecord:
    credential_id: str
    provider_name: str
    auth_type: str
    status: str
    access_token: str
    refresh_token: str | None = None
    id_token: str | None = None
    expires_at: int | None = None
    scopes: list[str] = field(default_factory=list)
    account_label: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    reauth_reason: str | None = None
    created_at: str = ""
    updated_at: str = ""

    def is_expired_or_near_expiry(self, now_seconds: int, skew_seconds: int) -> bool:
        if self.expires_at is None:
            return False
        return self.expires_at <= now_seconds + skew_seconds


@dataclass
class OAuthCredentialStatus:
    credential_id: str
    provider_name: str
    auth_type: str
    status: str
    expires_at: int | None
    scopes: list[str]
    account_label: str | None
    metadata: dict[str, Any]
    reauth_reason: str | None
    created_at: str
    updated_at: str


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _serialize_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _deserialize_list(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return []
    if not isinstance(parsed, list):
        return []
    return [item for item in parsed if isinstance(item, str)]


def _deserialize_dict(raw: str | None) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


class OAuthTokensDB:
    def __init__(
        self,
        db_filename: str = "oauth_tokens.db",
        *,
        encryption_key: str | None = None,
    ) -> None:
        db_dir = resolve_db_dir(__file__)
        db_path = db_dir / db_filename
        os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self._fernet = _build_fernet(encryption_key)
        self._init_db()

    def _init_db(self) -> None:
        conn = None
        try:
            conn = sqlite3.connect(self.db_path, timeout=30.0)
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA synchronous=NORMAL")
            conn.execute("PRAGMA busy_timeout = 30000")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS oauth_credentials (
                    credential_id TEXT PRIMARY KEY,
                    provider_name TEXT NOT NULL,
                    auth_type TEXT NOT NULL,
                    status TEXT NOT NULL,
                    access_token_enc TEXT NOT NULL,
                    refresh_token_enc TEXT,
                    id_token_enc TEXT,
                    expires_at INTEGER,
                    scopes TEXT,
                    account_label TEXT,
                    metadata TEXT,
                    reauth_reason TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
            conn.commit()
            logger.info("OAuth tokens database initialized at %s", self.db_path)
        except Exception:
            if conn:
                conn.rollback()
            logger.exception("Error initializing OAuth tokens database")
            raise
        finally:
            if conn:
                conn.close()

    def has_encryption_key(self) -> bool:
        return self._fernet is not None

    def upsert_tokens(
        self,
        *,
        credential_id: str,
        provider_name: str,
        auth_type: str,
        access_token: str,
        refresh_token: str | None,
        id_token: str | None = None,
        expires_at: int | None = None,
        scopes: list[str] | None = None,
        account_label: str | None = None,
        metadata: dict[str, Any] | None = None,
        preserve_existing_refresh_token: bool = True,
    ) -> OAuthCredentialStatus:
        fernet = self._require_fernet()
        now = _utc_now_iso()
        access_token_enc = _encrypt(fernet, access_token)
        id_token_enc = _encrypt(fernet, id_token) if id_token else None
        scopes_json = _serialize_json(scopes or [])
        metadata_json = _serialize_json(metadata or {})

        with self._connect() as conn:
            existing_refresh_token_enc = None
            if preserve_existing_refresh_token and refresh_token is None:
                row = conn.execute(
                    "SELECT refresh_token_enc FROM oauth_credentials WHERE credential_id = ?",
                    (credential_id,),
                ).fetchone()
                if row is not None:
                    existing_refresh_token_enc = row["refresh_token_enc"]
            refresh_token_enc = (
                _encrypt(fernet, refresh_token)
                if refresh_token
                else existing_refresh_token_enc
            )
            conn.execute(
                """
                INSERT INTO oauth_credentials (
                    credential_id, provider_name, auth_type, status,
                    access_token_enc, refresh_token_enc, id_token_enc,
                    expires_at, scopes, account_label, metadata,
                    reauth_reason, created_at, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?)
                ON CONFLICT(credential_id) DO UPDATE SET
                    provider_name = excluded.provider_name,
                    auth_type = excluded.auth_type,
                    status = excluded.status,
                    access_token_enc = excluded.access_token_enc,
                    refresh_token_enc = excluded.refresh_token_enc,
                    id_token_enc = excluded.id_token_enc,
                    expires_at = excluded.expires_at,
                    scopes = excluded.scopes,
                    account_label = excluded.account_label,
                    metadata = excluded.metadata,
                    reauth_reason = NULL,
                    updated_at = excluded.updated_at
                """,
                (
                    credential_id,
                    provider_name,
                    auth_type,
                    STATUS_ACTIVE,
                    access_token_enc,
                    refresh_token_enc,
                    id_token_enc,
                    expires_at,
                    scopes_json,
                    account_label,
                    metadata_json,
                    now,
                    now,
                ),
            )
            conn.commit()
        status = self.get_status(credential_id)
        if status is None:
            raise RuntimeError("OAuth credential upsert did not persist a status row.")
        return status

    def get(self, credential_id: str) -> OAuthCredentialRecord | None:
        fernet = self._require_fernet()
        with self._connect() as conn:
            row = conn.execute(
                "SELECT * FROM oauth_credentials WHERE credential_id = ?",
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        try:
            return OAuthCredentialRecord(
                credential_id=row["credential_id"],
                provider_name=row["provider_name"],
                auth_type=row["auth_type"],
                status=row["status"],
                access_token=_decrypt(fernet, row["access_token_enc"]),
                refresh_token=_decrypt(fernet, row["refresh_token_enc"]) if row["refresh_token_enc"] else None,
                id_token=_decrypt(fernet, row["id_token_enc"]) if row["id_token_enc"] else None,
                expires_at=row["expires_at"],
                scopes=_deserialize_list(row["scopes"]),
                account_label=row["account_label"],
                metadata=_deserialize_dict(row["metadata"]),
                reauth_reason=row["reauth_reason"],
                created_at=row["created_at"],
                updated_at=row["updated_at"],
            )
        except InvalidToken as exc:
            raise ValueError("OAuth credential could not be decrypted with the configured key.") from exc

    def get_status(self, credential_id: str) -> OAuthCredentialStatus | None:
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT credential_id, provider_name, auth_type, status, expires_at, scopes,
                       account_label, metadata, reauth_reason, created_at, updated_at
                FROM oauth_credentials
                WHERE credential_id = ?
                """,
                (credential_id,),
            ).fetchone()
        if row is None:
            return None
        return _row_to_status(row)

    def list_statuses(self) -> list[OAuthCredentialStatus]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT credential_id, provider_name, auth_type, status, expires_at, scopes,
                       account_label, metadata, reauth_reason, created_at, updated_at
                FROM oauth_credentials
                ORDER BY provider_name, credential_id
                """
            ).fetchall()
        return [_row_to_status(row) for row in rows]

    def mark_reauth_required(self, credential_id: str, reason: str) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE oauth_credentials
                SET status = ?, reauth_reason = ?, updated_at = ?
                WHERE credential_id = ?
                """,
                (STATUS_REAUTH_REQUIRED, reason, _utc_now_iso(), credential_id),
            )
            conn.commit()

    def delete(self, credential_id: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute(
                "DELETE FROM oauth_credentials WHERE credential_id = ?",
                (credential_id,),
            )
            conn.commit()
            return cursor.rowcount > 0

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30.0)
        conn.row_factory = sqlite3.Row
        for pragma in RUNTIME_PRAGMAS:
            conn.execute(pragma)
        return conn

    def _require_fernet(self) -> Fernet:
        if self._fernet is None:
            raise ValueError("OAUTH_CREDENTIAL_ENCRYPTION_KEY is required for managed OAuth credentials.")
        return self._fernet


def _build_fernet(encryption_key: str | None) -> Fernet | None:
    if not encryption_key:
        return None
    try:
        return Fernet(encryption_key.encode("utf-8"))
    except (TypeError, ValueError) as exc:
        raise ValueError("OAUTH_CREDENTIAL_ENCRYPTION_KEY must be a valid Fernet key.") from exc


def _encrypt(fernet: Fernet, value: str) -> str:
    return fernet.encrypt(value.encode("utf-8")).decode("ascii")


def _decrypt(fernet: Fernet, value: str) -> str:
    return fernet.decrypt(value.encode("ascii")).decode("utf-8")


def _row_to_status(row: sqlite3.Row) -> OAuthCredentialStatus:
    return OAuthCredentialStatus(
        credential_id=row["credential_id"],
        provider_name=row["provider_name"],
        auth_type=row["auth_type"],
        status=row["status"],
        expires_at=row["expires_at"],
        scopes=_deserialize_list(row["scopes"]),
        account_label=row["account_label"],
        metadata=_deserialize_dict(row["metadata"]),
        reauth_reason=row["reauth_reason"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
    )
