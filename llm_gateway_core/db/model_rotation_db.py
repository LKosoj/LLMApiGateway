import sqlite3
import os
import logging
import re
from collections.abc import Callable

import aiosqlite

from ..config.paths import resolve_db_dir
from ..services.upstream_routing_state import fingerprint_api_key
from .write_batcher import RUNTIME_PRAGMAS

_VALID_USER_SCOPE_RE = re.compile(r"^user:\d+$")
_VALID_MASTER_SCOPE_RE = re.compile(r"^master:(?:[0-9a-f]{16}|keyless)$")
_VALID_ROLE_SCOPE_RE = re.compile(r"^role:[a-z][a-z0-9_-]*$")


def _extract_bearer_token(scope: str) -> str:
    parts = scope.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]
    return scope


def _is_valid_rotation_scope(scope: str) -> bool:
    return bool(
        _VALID_USER_SCOPE_RE.fullmatch(scope)
        or _VALID_MASTER_SCOPE_RE.fullmatch(scope)
        or _VALID_ROLE_SCOPE_RE.fullmatch(scope)
    )


def normalize_rotation_scope(scope: str | None) -> str:
    normalized = str(scope or "").strip()
    if _is_valid_rotation_scope(normalized):
        return normalized

    token = _extract_bearer_token(normalized)
    return f"master:{fingerprint_api_key(token)}"


class ModelRotationDB:
    def __init__(
        self,
        db_filename: str = "llmgateway_rotation.db",
        *,
        legacy_scope_resolver: Callable[[str], str | None] | None = None,
    ):
        db_dir = resolve_db_dir(__file__)
        db_path = db_dir / db_filename

        os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
        self._legacy_scope_resolver = legacy_scope_resolver
        self._init_db()

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
            CREATE TABLE IF NOT EXISTS model_rotation (
                api_key TEXT,
                gateway_model TEXT,
                last_model_index INTEGER,
                PRIMARY KEY (api_key, gateway_model)
            )
            ''')

            migrated_plaintext_scopes = self._migrate_plaintext_scopes(conn)
            conn.commit()
            if migrated_plaintext_scopes:
                self._checkpoint_wal_best_effort(conn)
            logging.info("Model rotation database initialized at %s", self.db_path)
        except Exception as e:
            logging.error("Error initializing model rotation database: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    def _migrate_plaintext_scopes(self, conn: sqlite3.Connection) -> bool:
        rows = conn.execute(
            """
            SELECT api_key, gateway_model, last_model_index
            FROM model_rotation
            """
        ).fetchall()
        if not rows:
            return False

        merged_rows: dict[tuple[str, str], int] = {}
        changed = False
        for api_key, gateway_model, last_model_index in rows:
            normalized_scope = self._normalize_legacy_rotation_scope(api_key)
            key = (normalized_scope, gateway_model)
            index_value = int(last_model_index or 0)
            previous_index = merged_rows.get(key)
            if previous_index is None or index_value > previous_index:
                merged_rows[key] = index_value
            if normalized_scope != api_key or previous_index is not None:
                changed = True

        if not changed:
            return False

        conn.execute("DELETE FROM model_rotation")
        conn.executemany(
            """
            INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
            VALUES (?, ?, ?)
            """,
            [
                (api_key, gateway_model, last_model_index)
                for (api_key, gateway_model), last_model_index in merged_rows.items()
            ],
        )
        logging.info("Migrated %s model rotation scope row(s).", len(rows))
        return True

    def _normalize_legacy_rotation_scope(self, scope: str | None) -> str:
        normalized = str(scope or "").strip()
        token = _extract_bearer_token(normalized)
        if normalized == token and _is_valid_rotation_scope(normalized):
            return normalized
        resolved_scope = self._resolve_legacy_raw_token(token)
        if resolved_scope is not None:
            return resolved_scope
        return f"master:{fingerprint_api_key(token)}"

    def _resolve_legacy_raw_token(self, token: str) -> str | None:
        if self._legacy_scope_resolver is None:
            return None
        resolved_scope = self._legacy_scope_resolver(token)
        if resolved_scope is None:
            return None
        if _is_valid_rotation_scope(resolved_scope):
            return resolved_scope
        logging.warning("Ignoring invalid resolved model rotation scope.")
        return None

    def _checkpoint_wal_best_effort(self, conn: sqlite3.Connection) -> None:
        try:
            row = conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone()
        except sqlite3.Error:
            logging.exception("Failed to checkpoint model rotation WAL after scope migration.")
            return
        if row is None:
            return
        busy, log_frames, checkpointed_frames = (int(value or 0) for value in row[:3])
        if busy:
            logging.warning(
                "Model rotation WAL checkpoint after scope migration was busy "
                "(busy=%s, log_frames=%s, checkpointed_frames=%s).",
                busy,
                log_frames,
                checkpointed_frames,
            )

    # ------------------------------------------------------------------
    # Atomic read+write — async via aiosqlite (no batcher)
    # ------------------------------------------------------------------

    async def get_next_model_index(self, api_key: str, gateway_model: str, total_models: int) -> int:
        if total_models <= 0:
            logging.warning("Cannot get next model index with zero or negative total models.")
            return 0

        rotation_scope = normalize_rotation_scope(api_key)
        try:
            async with aiosqlite.connect(self.db_path) as db:
                for pragma in RUNTIME_PRAGMAS:
                    await db.execute(pragma)
                cursor = await db.execute(
                    """
                    INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
                    VALUES (?, ?, 0)
                    ON CONFLICT(api_key, gateway_model)
                    DO UPDATE SET last_model_index = (model_rotation.last_model_index + 1) % ?
                    RETURNING last_model_index
                    """,
                    (rotation_scope, gateway_model, total_models),
                )
                result = await cursor.fetchone()
                if result is None:
                    raise RuntimeError("Atomic model rotation upsert did not return a row.")
                await db.commit()
                return int(result[0])
        except Exception as e:
            masked_api_key = str(rotation_scope)[:12] if rotation_scope is not None else ""
            logging.error(
                "Error getting next model index for key='%s...', model='%s': %s",
                masked_api_key, gateway_model, e,
            )
            return 0
