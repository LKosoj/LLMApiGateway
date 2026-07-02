import asyncio
import sqlite3
import unittest
import uuid

from tests._async_compat import run_async
from llm_gateway_core.db.model_rotation_db import ModelRotationDB, normalize_rotation_scope
from llm_gateway_core.services.upstream_routing_state import fingerprint_api_key


class ModelRotationDBTests(unittest.TestCase):
    def setUp(self):
        self.db = ModelRotationDB(db_filename=f"test_rotation_{uuid.uuid4().hex}.db")
        self.db_paths = [self.db.db_path]

    def tearDown(self):
        for db_path in self.db_paths:
            if db_path.exists():
                db_path.unlink()
            db_path.with_suffix(db_path.suffix + "-wal").unlink(missing_ok=True)
            db_path.with_suffix(db_path.suffix + "-shm").unlink(missing_ok=True)

    def _db_path_for_filename(self, db_filename: str):
        db_path = self.db.db_path.parent / db_filename
        self.db_paths.append(db_path)
        return db_path

    def _rotation_rows(self, db: ModelRotationDB):
        with sqlite3.connect(db.db_path) as conn:
            return conn.execute(
                """
                SELECT api_key, gateway_model, last_model_index
                FROM model_rotation
                ORDER BY api_key, gateway_model
                """
            ).fetchall()

    def test_new_model_rotation_row_does_not_store_raw_key(self):
        secret = "raw-master-secret"

        result = run_async(
            self.db.get_next_model_index(
                f"Bearer {secret}",
                "gateway-model",
                2,
            )
        )

        self.assertEqual(result, 0)
        rows = self._rotation_rows(self.db)
        self.assertEqual(
            rows,
            [(f"master:{fingerprint_api_key(secret)}", "gateway-model", 0)],
        )
        self.assertNotIn(secret, rows[0][0])

    def test_scope_like_raw_tokens_are_hashed_unless_valid_runtime_scope(self):
        self.assertEqual(normalize_rotation_scope("user:42"), "user:42")
        self.assertEqual(normalize_rotation_scope("role:user"), "role:user")
        self.assertEqual(
            normalize_rotation_scope("master:not-a-fingerprint"),
            f"master:{fingerprint_api_key('master:not-a-fingerprint')}",
        )
        self.assertEqual(
            normalize_rotation_scope("Bearer user:42"),
            f"master:{fingerprint_api_key('user:42')}",
        )

    def test_get_next_model_index_is_atomic_under_concurrency(self):
        total_calls = 12
        total_models = 3
        scope_variants = [
            "api-key",
            "Bearer api-key",
            "bearer     api-key",
        ]

        async def run_concurrent():
            barrier = asyncio.Barrier(total_calls)

            async def get_index(position):
                await barrier.wait()
                return await self.db.get_next_model_index(
                    scope_variants[position % len(scope_variants)],
                    "gateway-model",
                    total_models,
                )

            tasks = [asyncio.create_task(get_index(position)) for position in range(total_calls)]
            return await asyncio.gather(*tasks)

        with self.assertNoLogs(level="ERROR"):
            results = run_async(run_concurrent())

        self.assertEqual(
            sorted(results),
            sorted([index % total_models for index in range(total_calls)]),
        )
        self.assertEqual(
            run_async(self.db.get_next_model_index("api-key", "gateway-model", total_models)),
            0,
        )
        self.assertEqual(
            self._rotation_rows(self.db),
            [(normalize_rotation_scope("api-key"), "gateway-model", 0)],
        )

    def test_init_migrates_plaintext_scopes_and_merges_conflicts(self):
        db_filename = f"test_rotation_legacy_{uuid.uuid4().hex}.db"
        db_path = self._db_path_for_filename(db_filename)
        secret = "legacy-master-secret"
        expected_scope = f"master:{fingerprint_api_key(secret)}"
        tricky_secret = "master:not-a-fingerprint"
        tricky_expected_scope = f"master:{fingerprint_api_key(tricky_secret)}"
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE model_rotation (
                    api_key TEXT,
                    gateway_model TEXT,
                    last_model_index INTEGER,
                    PRIMARY KEY (api_key, gateway_model)
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
                VALUES (?, ?, ?)
                """,
                [
                    (secret, "gateway-model", 1),
                    (f"Bearer {secret}", "gateway-model", 2),
                    (expected_scope, "gateway-model", 0),
                    ("user:42", "gateway-model", 1),
                    (tricky_secret, "gateway-model", 3),
                ],
            )
            conn.commit()

        migrated_db = ModelRotationDB(db_filename=db_filename)

        self.assertCountEqual(
            self._rotation_rows(migrated_db),
            [
                (expected_scope, "gateway-model", 2),
                (tricky_expected_scope, "gateway-model", 3),
                ("user:42", "gateway-model", 1),
            ],
        )
        wal_path = db_path.with_suffix(db_path.suffix + "-wal")
        if wal_path.exists():
            self.assertEqual(wal_path.stat().st_size, 0)
        self.assertEqual(
            run_async(migrated_db.get_next_model_index(secret, "gateway-model", 3)),
            0,
        )

    def test_init_migrates_legacy_virtual_key_rows_with_resolver(self):
        db_filename = f"test_rotation_virtual_legacy_{uuid.uuid4().hex}.db"
        db_path = self._db_path_for_filename(db_filename)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE model_rotation (
                    api_key TEXT,
                    gateway_model TEXT,
                    last_model_index INTEGER,
                    PRIMARY KEY (api_key, gateway_model)
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
                VALUES (?, ?, ?)
                """,
                [
                    ("lgk_virtual", "gateway-model", 1),
                    ("Bearer lgk_virtual", "gateway-model", 2),
                    ("user:42", "gateway-model", 3),
                ],
            )
            conn.commit()

        def legacy_scope_resolver(token: str) -> str | None:
            if token == "lgk_virtual":
                return "user:77"
            if token == "user:42":
                return "user:99"
            return None

        migrated_db = ModelRotationDB(
            db_filename=db_filename,
            legacy_scope_resolver=legacy_scope_resolver,
        )

        self.assertCountEqual(
            self._rotation_rows(migrated_db),
            [
                ("user:77", "gateway-model", 2),
                ("user:42", "gateway-model", 3),
            ],
        )
        rows_text = repr(self._rotation_rows(migrated_db))
        self.assertNotIn("lgk_virtual", rows_text)
        self.assertNotIn("Bearer", rows_text)

    def test_init_does_not_resolve_already_normalized_scope_rows(self):
        db_filename = f"test_rotation_normalized_scope_{uuid.uuid4().hex}.db"
        db_path = self._db_path_for_filename(db_filename)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        valid_master_scope = "master:4f36f2b2d8e9d2e6"
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE model_rotation (
                    api_key TEXT,
                    gateway_model TEXT,
                    last_model_index INTEGER,
                    PRIMARY KEY (api_key, gateway_model)
                )
                """
            )
            conn.executemany(
                """
                INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
                VALUES (?, ?, ?)
                """,
                [
                    (valid_master_scope, "gateway-model", 1),
                    ("user:42", "gateway-model", 2),
                    ("role:tester", "gateway-model", 3),
                ],
            )
            conn.commit()

        resolved_tokens = []

        def legacy_scope_resolver(token: str) -> str | None:
            resolved_tokens.append(token)
            raise RuntimeError("normalized scopes must not be resolved")

        migrated_db = ModelRotationDB(
            db_filename=db_filename,
            legacy_scope_resolver=legacy_scope_resolver,
        )

        self.assertEqual(resolved_tokens, [])
        self.assertCountEqual(
            self._rotation_rows(migrated_db),
            [
                (valid_master_scope, "gateway-model", 1),
                ("user:42", "gateway-model", 2),
                ("role:tester", "gateway-model", 3),
            ],
        )

    def test_init_aborts_migration_when_legacy_resolver_fails(self):
        db_filename = f"test_rotation_resolver_fail_{uuid.uuid4().hex}.db"
        db_path = self._db_path_for_filename(db_filename)
        db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(db_path) as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE model_rotation (
                    api_key TEXT,
                    gateway_model TEXT,
                    last_model_index INTEGER,
                    PRIMARY KEY (api_key, gateway_model)
                )
                """
            )
            conn.execute(
                """
                INSERT INTO model_rotation (api_key, gateway_model, last_model_index)
                VALUES (?, ?, ?)
                """,
                ("lgk_virtual", "gateway-model", 1),
            )
            conn.commit()

        def failing_resolver(_token: str) -> str | None:
            raise RuntimeError("api_keys lookup unavailable")

        with self.assertRaises(RuntimeError):
            ModelRotationDB(
                db_filename=db_filename,
                legacy_scope_resolver=failing_resolver,
            )

        with sqlite3.connect(db_path) as conn:
            rows = conn.execute(
                """
                SELECT api_key, gateway_model, last_model_index
                FROM model_rotation
                """
            ).fetchall()
        self.assertEqual(rows, [("lgk_virtual", "gateway-model", 1)])

    def test_model_rotation_db_uses_wal_journal_mode(self):
        with sqlite3.connect(self.db.db_path) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode").fetchone()[0]

        self.assertEqual(str(journal_mode).lower(), "wal")


if __name__ == "__main__":
    unittest.main()
