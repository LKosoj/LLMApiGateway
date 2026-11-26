"""Focused tests for WP3 SQLite, WriteBatcher, index, and analytics optimizations."""

from __future__ import annotations

import asyncio
import sqlite3
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from llm_gateway_core.api.v1 import quota as quota_module
from llm_gateway_core.db import (
    accounting_schema,
    api_keys_db as api_keys_db_module,
    fallback_events_db as fallback_events_db_module,
    rejections_db as rejections_db_module,
    tokens_usage_db as tokens_usage_db_module,
)
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.db.fallback_events_db import FallbackEventsDB
from llm_gateway_core.db.model_rotation_db import ModelRotationDB
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.db.video_jobs_db import VideoJobsDB
from llm_gateway_core.db.write_batcher import (
    RUNTIME_PRAGMAS,
    TUNING_PRAGMAS,
    WriteBatcher,
    WriteBatcherOverloaded,
    WriteBatcherState,
    apply_runtime_pragmas,
)
from llm_gateway_core.services.accounting import (
    AccountingEvent,
    AccountingEventKind,
    AccountingUsage,
    BillingComponent,
    CostSource,
)
from llm_gateway_core.services.ip_blocklist import IpBlockGuard
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests._async_compat import run_async


class WriteBatcherOptimizationTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_path = Path(self._tmp.name) / "batcher.db"
        self.dead_letter_path = Path(self._tmp.name) / "batcher.dl.jsonl"
        with sqlite3.connect(self.db_path) as conn:
            conn.execute("CREATE TABLE items (id INTEGER PRIMARY KEY, value TEXT)")
            conn.execute("CREATE TABLE other (id INTEGER PRIMARY KEY, value TEXT)")
            conn.commit()

    def test_flush_once_uses_executemany_for_consecutive_same_sql(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                batch_size=10,
                batch_window=0.05,
                flush_interval=10.0,
            )
            await batcher.start()
            conn = await batcher._ensure_connection()
            execute_calls: list[str] = []
            executemany_calls: list[tuple[str, int]] = []
            orig_execute = conn.execute
            orig_executemany = conn.executemany

            async def track_execute(sql, parameters=None):
                execute_calls.append(sql)
                return await orig_execute(sql, parameters)

            async def track_executemany(sql, parameters):
                params_list = list(parameters)
                executemany_calls.append((sql, len(params_list)))
                return await orig_executemany(sql, params_list)

            with patch.object(conn, "execute", side_effect=track_execute), patch.object(
                conn, "executemany", side_effect=track_executemany
            ):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("a",))
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("b",))
                batcher.enqueue("INSERT INTO other (value) VALUES (?)", ("c",))
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("d",))
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("e",))
                await batcher.stop()

            self.assertEqual(len(execute_calls), 1)
            self.assertIn("INTO other", execute_calls[0])
            self.assertEqual(len(executemany_calls), 2)
            self.assertEqual([cnt for _, cnt in executemany_calls], [2, 2])

        run_async(scenario())

        with sqlite3.connect(self.db_path) as conn:
            items = [r[0] for r in conn.execute("SELECT value FROM items ORDER BY id")]
            other = [r[0] for r in conn.execute("SELECT value FROM other ORDER BY id")]
        self.assertEqual(items, ["a", "b", "d", "e"])
        self.assertEqual(other, ["c"])

    def test_cancellation_during_batch_window_preserves_accounting_invariants(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                batch_size=10,
                batch_window=5.0,
                flush_interval=10.0,
                dead_letter_path=self.dead_letter_path,
            )
            await batcher.start()
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("first",))
            # Let consumer pull "first" and enter the batch_window wait_for()
            await asyncio.sleep(0.05)
            consumer = batcher._task
            self.assertIsNotNone(consumer)
            consumer.cancel()
            await consumer
            await batcher.stop()
            snap = batcher.health_snapshot()
            self.assertEqual(snap.state, WriteBatcherState.FAILED)
            self.assertEqual(snap.reserved, 0)
            self.assertEqual(snap.accepted, 1)
            self.assertEqual(snap.diagnostic_terminal, 1)

        run_async(scenario())
        self.assertTrue(self.dead_letter_path.exists())
        self.assertEqual(len(self.dead_letter_path.read_text(encoding="utf-8").splitlines()), 1)

    def test_dead_letter_executor_flushes_on_stop(self):
        async def scenario():
            batcher = WriteBatcher(
                self.db_path,
                queue_maxsize=1,
                dead_letter_path=self.dead_letter_path,
            )
            batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("ok",))
            with self.assertRaises(WriteBatcherOverloaded):
                batcher.enqueue("INSERT INTO items (value) VALUES (?)", ("overflow",))
            await batcher.stop()

        run_async(scenario())
        lines = self.dead_letter_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        self.assertIn('"overloaded"', lines[0])


class PragmaAndIndexTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_dir = Path(self._tmp.name) / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)

    def test_tuning_pragmas_in_runtime_pragmas_and_accounting_connection(self):
        self.assertEqual(
            TUNING_PRAGMAS,
            (
                "PRAGMA temp_store=MEMORY",
                "PRAGMA cache_size=-32000",
                "PRAGMA mmap_size=268435456",
            ),
        )
        for pragma in TUNING_PRAGMAS:
            self.assertIn(pragma, RUNTIME_PRAGMAS)

        db_path = self.db_dir / "pragmas.db"
        with sqlite3.connect(db_path) as conn:
            apply_runtime_pragmas(conn)
            self.assertEqual(conn.execute("PRAGMA temp_store").fetchone()[0], 2)
            self.assertEqual(conn.execute("PRAGMA cache_size").fetchone()[0], -32000)

        acc_conn = accounting_schema.open_accounting_runtime_connection(
            db_path, enable_foreign_keys=True
        )
        try:
            self.assertEqual(acc_conn.execute("PRAGMA synchronous").fetchone()[0], 2)
            self.assertEqual(acc_conn.execute("PRAGMA temp_store").fetchone()[0], 2)
            self.assertEqual(acc_conn.execute("PRAGMA cache_size").fetchone()[0], -32000)
        finally:
            acc_conn.close()

    def test_redundant_indexes_dropped_and_category_timestamp_index_created(self):
        with (
            patch.object(api_keys_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(tokens_usage_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(fallback_events_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(rejections_db_module, "resolve_db_dir", return_value=self.db_dir),
        ):
            api_db = ApiKeysDB(db_filename="api_keys.db")
            usage_db = TokensUsageDB(db_filename="usage.db")
            fb_db = FallbackEventsDB(db_filename="usage.db")
            rej_db = RejectionsDB(db_filename="usage.db")

        with sqlite3.connect(api_db.db_path) as conn:
            api_indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='api_keys'"
                )
            }
        self.assertNotIn("idx_api_keys_key", api_indexes)

        with sqlite3.connect(usage_db.db_path) as conn:
            usage_indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='tokens_usage'"
                )
            }
            fb_indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='fallback_events'"
                )
            }
            rej_indexes = {
                r[0]
                for r in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='rejection_events'"
                )
            }
        self.assertNotIn("idx_tokens_usage_timestamp", usage_indexes)
        self.assertNotIn("idx_fallback_events_timestamp", fb_indexes)
        self.assertNotIn("idx_rejection_events_api_key_id", rej_indexes)
        self.assertNotIn("idx_rejection_events_category", rej_indexes)
        self.assertIn("idx_rejection_events_category_timestamp", rej_indexes)
        self.assertEqual(fb_db.db_path, rej_db.db_path)


class AnalyticsSummaryAndCleanupTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.db_dir = Path(self._tmp.name) / "db"
        self.db_dir.mkdir(parents=True, exist_ok=True)
        patchers = [
            patch.object(tokens_usage_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(fallback_events_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(rejections_db_module, "resolve_db_dir", return_value=self.db_dir),
            patch.object(api_keys_db_module, "resolve_db_dir", return_value=self.db_dir),
        ]
        for p in patchers:
            p.start()
            self.addCleanup(p.stop)

        self.usage_db = TokensUsageDB(db_filename="analytics.db")
        self.fb_db = FallbackEventsDB(db_filename="analytics.db")
        self.rej_db = RejectionsDB(db_filename="analytics.db")
        self.api_db = ApiKeysDB(db_filename="keys.db")

    def test_real_sqlite_usage_and_fallback_summary_match_full_dashboard(self):
        now = datetime.now(timezone.utc)
        with sqlite3.connect(self.usage_db.db_path) as conn:
            conn.executemany(
                """
                INSERT INTO tokens_usage (
                    timestamp, duration_ms, ttft_ms, prompt_tokens, completion_tokens,
                    total_tokens, reasoning_tokens, cached_tokens, cost, gateway_model,
                    operation, model, provider, request_id, is_estimated, cost_saved,
                    api_key_id, fallback_depth
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        (now - timedelta(minutes=10)).isoformat(),
                        200,
                        50,
                        10,
                        20,
                        30,
                        2,
                        4,
                        0.05,
                        "gw",
                        "chat",
                        "m1",
                        "p1",
                        "r1",
                        0,
                        0.01,
                        1,
                        0,
                    ),
                    (
                        (now - timedelta(minutes=5)).isoformat(),
                        400,
                        150,
                        20,
                        40,
                        60,
                        3,
                        6,
                        0.10,
                        "gw",
                        "chat",
                        "m1",
                        "p1",
                        "r2",
                        1,
                        0.02,
                        1,
                        1,
                    ),
                ],
            )
            conn.commit()

        self.fb_db.insert_event("r1", "gw", 1, "p1", "m1", False, "http_500", "err", 100, "chat", 1)
        self.fb_db.insert_event("r1", "gw", 2, "p1", "m2", True, None, None, 200, "chat", 1)
        self.fb_db.insert_event("r2", "gw", 1, "p1", "m1", False, "timeout", "err", 300, "chat", 1)

        start = now - timedelta(hours=1)
        end = now + timedelta(minutes=5)

        full_usage = run_async(self.usage_db.get_dashboard_usage("hour", start, end, api_key_id=1))
        summary_usage = run_async(self.usage_db.get_dashboard_usage_summary(start, end, api_key_id=1))
        self.assertEqual(summary_usage, full_usage["summary"])
        self.assertEqual(summary_usage["requests"], 2)
        self.assertEqual(summary_usage["ttft_avg_ms"], 100)
        self.assertEqual(summary_usage["ttft_max_ms"], 150)

        full_fb = run_async(self.fb_db.get_dashboard_fallback("hour", start, end, api_key_id=1))
        summary_fb_full = run_async(
            self.fb_db.get_dashboard_fallback_summary(start, end, api_key_id=1, include_requests=True)
        )
        summary_fb_user = run_async(
            self.fb_db.get_dashboard_fallback_summary(start, end, api_key_id=1, include_requests=False)
        )
        self.assertEqual(summary_fb_full["summary"], full_fb["summary"])
        self.assertEqual(summary_fb_full["requests"], full_fb["requests"])
        self.assertEqual(summary_fb_full["requests"], {"total": 2, "rescued": 1, "failed": 1})
        self.assertEqual(summary_fb_user, {"summary": full_fb["summary"]})

    def test_chunked_cleanup_old_records_across_all_three_tables(self):
        old_ts = (datetime.now(timezone.utc) - timedelta(days=200)).isoformat()
        new_ts = datetime.now(timezone.utc).isoformat()
        with sqlite3.connect(self.usage_db.db_path) as conn:
            for i in range(5):
                conn.execute(
                    "INSERT INTO tokens_usage (timestamp, total_tokens) VALUES (?, ?)",
                    (old_ts, i),
                )
                conn.execute(
                    "INSERT INTO fallback_events (timestamp, request_id, gateway_model, attempt_number, provider, model) "
                    "VALUES (?, ?, 'gw', 1, 'p', 'm')",
                    (old_ts, f"old-{i}"),
                )
                conn.execute(
                    "INSERT INTO rejection_events (timestamp, path, method, status_code, category) "
                    "VALUES (?, '/v1/chat', 'POST', 401, 'auth_invalid')",
                    (old_ts,),
                )
            for i in range(2):
                conn.execute(
                    "INSERT INTO tokens_usage (timestamp, total_tokens) VALUES (?, ?)",
                    (new_ts, i),
                )
                conn.execute(
                    "INSERT INTO fallback_events (timestamp, request_id, gateway_model, attempt_number, provider, model) "
                    "VALUES (?, ?, 'gw', 1, 'p', 'm')",
                    (new_ts, f"new-{i}"),
                )
                conn.execute(
                    "INSERT INTO rejection_events (timestamp, path, method, status_code, category) "
                    "VALUES (?, '/v1/chat', 'POST', 401, 'auth_invalid')",
                    (new_ts,),
                )
            conn.commit()

        with (
            patch.object(tokens_usage_db_module, "_CLEANUP_BATCH_SIZE", 2),
            patch.object(fallback_events_db_module, "_CLEANUP_BATCH_SIZE", 2),
            patch.object(rejections_db_module, "_CLEANUP_BATCH_SIZE", 2),
        ):
            self.usage_db.cleanup_old_records(retention_days=30)
            self.fb_db.cleanup_old_records(retention_days=30)
            self.rej_db.cleanup_old_records(retention_days=30)

        with sqlite3.connect(self.usage_db.db_path) as conn:
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM tokens_usage").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM fallback_events").fetchone()[0], 2)
            self.assertEqual(conn.execute("SELECT COUNT(*) FROM rejection_events").fetchone()[0], 2)

    def test_batched_pending_accounting_events_and_rollup_validation_with_chunking(self):
        base_time = datetime(2026, 6, 1, 12, 0, 0, tzinfo=timezone.utc)
        rollup_id = "evt-rollup-1"
        child_events: list[AccountingEvent] = []
        for idx in range(5):
            child = AccountingEvent(
                version=1,
                event_id=f"evt-child-{idx}",
                kind=AccountingEventKind.CHARGE,
                api_key_id=1,
                method="POST",
                route_template="/v1/chat/completions",
                operation="chat",
                gateway_model="gw",
                provider="p1",
                model="m1",
                usage=AccountingUsage(
                    prompt_tokens=10,
                    completion_tokens=5,
                    total_tokens=15,
                    cost=0.01,
                ),
                cost_source=CostSource.COMPONENT_SUM,
                occurred_at=base_time + timedelta(seconds=idx),
                request_id=f"req-{idx}",
                parent_event_id=rollup_id,
                components=(
                    BillingComponent(
                        provider="p1",
                        model="m1",
                        usage=AccountingUsage(
                            prompt_tokens=10,
                            completion_tokens=5,
                            total_tokens=15,
                            cost=0.01,
                        ),
                        cost_source=CostSource.TOKEN_REGISTRY,
                    ),
                ),
            )
            child_events.append(child)

        rollup = AccountingEvent(
            version=1,
            event_id=rollup_id,
            kind=AccountingEventKind.ROLLUP,
            api_key_id=1,
            method="POST",
            route_template="/v1/chat/completions",
            operation="chat",
            gateway_model="gw",
            provider="p1",
            model="m1",
            usage=AccountingUsage(
                prompt_tokens=0,
                completion_tokens=0,
                total_tokens=0,
                cost=0.0,
            ),
            cost_source=CostSource.RECEIPT_ROLLUP,
            occurred_at=base_time + timedelta(seconds=10),
            request_id="req-rollup",
            child_event_ids=tuple(c.event_id for c in child_events),
            child_fingerprints=tuple(c.billing_fingerprint for c in child_events),
        )

        with patch.object(tokens_usage_db_module, "_SQLITE_IN_CHUNK_SIZE", 2):
            for child in child_events:
                self.usage_db.accept_accounting_event(child)
            self.usage_db.accept_accounting_event(rollup)
            pending = self.usage_db.list_pending_accounting_events(limit=20)

        self.assertEqual(len(pending), 6)
        self.assertEqual(
            [item.event.event_id for item in pending],
            [*(c.event_id for c in child_events), rollup_id],
        )
        self.assertEqual(pending[-1].event.child_event_ids, rollup.child_event_ids)
        self.assertEqual(len(pending[0].event.components), 1)

    def test_single_connection_create_and_promote(self):
        real_connect = sqlite3.connect
        calls = 0

        def counting_connect(*args, **kwargs):
            nonlocal calls
            calls += 1
            return real_connect(*args, **kwargs)

        with patch.object(sqlite3, "connect", side_effect=counting_connect):
            rec = self.api_db.create(name="single-conn")
        self.assertEqual(rec.name, "single-conn")
        self.assertEqual(calls, 1)

        video_db = VideoJobsDB(db_path=self.db_dir / "video.db")
        calls = 0
        with patch.object(sqlite3, "connect", side_effect=counting_connect):
            job = run_async(
                video_db.create("owner-1", "gw-video", {"provider": "p1", "model": "v1"}, "up-1")
            )
        self.assertEqual(job["owner"], "owner-1")
        self.assertEqual(job["route"], {"provider": "p1", "model": "v1"})
        self.assertEqual(calls, 1)

        calls = 0
        with patch.object(sqlite3, "connect", side_effect=counting_connect):
            blocks = self.rej_db.promote_repeat_ip_blocks()
        self.assertEqual(blocks, [])
        self.assertEqual(calls, 1)


class ConcurrencyAndApiOptimizationTests(unittest.TestCase):
    def test_ip_block_guard_does_not_hold_fast_lock_during_db_scan(self):
        entered_db = threading.Event()
        release_db = threading.Event()

        class _SlowRejectionsDB:
            def list_permanent_ip_blocks(self):
                return []

            def promote_repeat_ip_blocks(self):
                entered_db.set()
                release_db.wait(timeout=2.0)
                return [{"client_ip": "1.2.3.4", "blocked_at": "2026-06-01T00:00:00+00:00", "reason": "repeat"}]

            def release_permanent_ip_block(self, ip: str) -> bool:
                return True

        guard = IpBlockGuard(rejections_db=_SlowRejectionsDB())
        scan_thread = threading.Thread(target=guard.scan_permanent_blocks)
        scan_thread.start()
        self.assertTrue(entered_db.wait(timeout=2.0))

        # check_blocked must not block while promote_repeat_ip_blocks is in SQLite
        self.assertIsNone(guard.check_blocked("9.9.9.9"))

        release_db.set()
        scan_thread.join(timeout=2.0)
        self.assertEqual(guard.check_blocked("1.2.3.4"), -1)

    def test_quota_snapshot_uses_get_by_id_for_filtered_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "db"
            db_dir.mkdir(parents=True, exist_ok=True)
            with patch.object(api_keys_db_module, "resolve_db_dir", return_value=db_dir):
                api_db = ApiKeysDB(db_filename="quota_opt.db")
            rec = api_db.create(name="user-key", rpm=10)

            class _FakeFB:
                async def get_events_count_last_24h(self, api_key_id=None):
                    return {rec.id: 3}

            with patch.object(api_db, "list_all", side_effect=AssertionError("list_all must not be called")), patch.object(
                api_db, "get_by_id", wraps=api_db.get_by_id
            ) as get_by_id_spy:
                snapshot = run_async(
                    quota_module._build_quota_snapshot(
                        api_db, _FakeFB(), RateLimiter(), key_id_filter=rec.id
                    )
                )

            get_by_id_spy.assert_called_once_with(rec.id)
            self.assertEqual(len(snapshot), 1)
            self.assertEqual(snapshot[0]["key_id"], rec.id)
            self.assertEqual(snapshot[0]["fallback_events_24h"], 3)

    def test_model_rotation_db_recreates_connection_across_event_loops(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_dir = Path(tmp) / "db"
            db_dir.mkdir(parents=True, exist_ok=True)
            with patch("llm_gateway_core.db.model_rotation_db.resolve_db_dir", return_value=db_dir):
                db = ModelRotationDB(db_filename="rot_loops.db")
            try:
                idx1 = run_async(db.get_next_model_index("user:1", "gw", 3))
                idx2 = run_async(db.get_next_model_index("user:1", "gw", 3))
                self.assertEqual((idx1, idx2), (0, 1))
            finally:
                run_async(db.close())


if __name__ == "__main__":
    unittest.main()
