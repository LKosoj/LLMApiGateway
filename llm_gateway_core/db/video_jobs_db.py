"""Persist the provider selected for an asynchronous video task."""

import asyncio
import json
import sqlite3
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from ..config.paths import resolve_db_dir


class VideoJobsDB:
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or resolve_db_dir() / "llmgateway_video_jobs.db"
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.execute("""
                CREATE TABLE IF NOT EXISTS video_jobs (
                    id TEXT PRIMARY KEY,
                    owner TEXT NOT NULL,
                    gateway_model TEXT NOT NULL,
                    provider TEXT NOT NULL,
                    route TEXT NOT NULL,
                    upstream_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    billing_payload TEXT,
                    UNIQUE(owner, provider, upstream_id)
                )
            """)
            connection.commit()

    async def create(self, owner: str, gateway_model: str, route: dict, upstream_id: str) -> dict:
        job = {
            "id": f"video_{uuid4().hex}",
            "owner": owner,
            "gateway_model": gateway_model,
            "route": route,
            "upstream_id": upstream_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "billing_payload": None,
        }

        def insert():
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.execute(
                    "INSERT OR IGNORE INTO video_jobs VALUES (?, ?, ?, ?, ?, ?, ?, NULL)",
                    (job["id"], owner, gateway_model, route["provider"], json.dumps(route), upstream_id, job["created_at"]),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT id FROM video_jobs WHERE owner = ? AND provider = ? AND upstream_id = ?",
                    (owner, route["provider"], upstream_id),
                ).fetchone()
                return row[0]

        job_id = await asyncio.to_thread(insert)
        return await self.get(job_id, owner)

    async def get(self, job_id: str, owner: str) -> dict | None:
        def read():
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.row_factory = sqlite3.Row
                row = connection.execute(
                    "SELECT * FROM video_jobs WHERE id = ? AND owner = ?", (job_id, owner),
                ).fetchone()
                if row is None:
                    return None
                job = dict(row)
                job["route"] = json.loads(job["route"])
                job["billing_payload"] = json.loads(job["billing_payload"]) if job["billing_payload"] else None
                return job

        return await asyncio.to_thread(read)

    async def freeze_billing(self, job_id: str, payload: dict) -> dict:
        """Keep one immutable billing observation for deduplicated polling charges."""
        def freeze():
            with closing(sqlite3.connect(self.db_path)) as connection:
                connection.execute(
                    "UPDATE video_jobs SET billing_payload = ? WHERE id = ? AND billing_payload IS NULL",
                    (json.dumps(payload), job_id),
                )
                connection.commit()
                row = connection.execute(
                    "SELECT billing_payload FROM video_jobs WHERE id = ?", (job_id,),
                ).fetchone()
                return json.loads(row[0])

        return await asyncio.to_thread(freeze)
