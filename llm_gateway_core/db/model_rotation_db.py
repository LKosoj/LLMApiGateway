import sqlite3
import os
import logging
from pathlib import Path

import aiosqlite

from .write_batcher import RUNTIME_PRAGMAS


class ModelRotationDB:
    def __init__(self, db_filename: str = "llmgateway_rotation.db"):
        project_root = Path(__file__).parent.parent.parent
        db_dir = project_root / "db"
        db_path = db_dir / db_filename

        os.makedirs(db_dir, exist_ok=True)

        self.db_path = db_path
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

            conn.commit()
            logging.info("Model rotation database initialized at %s", self.db_path)
        except Exception as e:
            logging.error("Error initializing model rotation database: %s", e)
            if conn:
                conn.rollback()
            raise
        finally:
            if conn:
                conn.close()

    # ------------------------------------------------------------------
    # Atomic read+write — async via aiosqlite (no batcher)
    # ------------------------------------------------------------------

    async def get_next_model_index(self, api_key: str, gateway_model: str, total_models: int) -> int:
        if total_models <= 0:
            logging.warning("Cannot get next model index with zero or negative total models.")
            return 0

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
                    (api_key, gateway_model, total_models),
                )
                result = await cursor.fetchone()
                if result is None:
                    raise RuntimeError("Atomic model rotation upsert did not return a row.")
                await db.commit()
                return int(result[0])
        except Exception as e:
            masked_api_key = str(api_key)[:5] if api_key is not None else ""
            logging.error(
                "Error getting next model index for key='%s...', model='%s': %s",
                masked_api_key, gateway_model, e,
            )
            return 0
