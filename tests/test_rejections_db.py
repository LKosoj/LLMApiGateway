"""Unit tests for RejectionsDB and record_rejection helper."""

from __future__ import annotations

import os
import sqlite3
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, Request

import llm_gateway_core.db.rejections_db as rejections_db_module
from llm_gateway_core.config.paths import resolve_db_dir
from llm_gateway_core.db.rejections_db import RejectionsDB, record_rejection
from tests._async_compat import run_async
from tests.runtime_test_support import bind_app_services


class _RejectionsDBTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)

        self._root = Path(self._tmp.name)
        os.makedirs(self._root / "db", exist_ok=True)

        path_patch = patch.object(
            rejections_db_module,
            "__file__",
            str(self._root / "llm_gateway_core" / "db" / "rejections_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.db = RejectionsDB(db_filename="test_rejections.db")


class RejectionsDBSchemaTests(_RejectionsDBTestBase):
    def test_table_has_all_expected_columns(self):
        conn = sqlite3.connect(self.db.db_path)
        cursor = conn.execute("PRAGMA table_info(rejection_events)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        expected = {
            "id",
            "timestamp",
            "request_id",
            "api_key_id",
            "path",
            "method",
            "client_ip",
            "status_code",
            "category",
            "reason",
            "auth_source",
            "x_title",
        }
        self.assertEqual(columns, expected)

    def test_init_db_adds_x_title_column_for_existing_table(self):
        db_path = resolve_db_dir() / "legacy_rejections.db"
        conn = sqlite3.connect(db_path)
        conn.execute(
            """
            CREATE TABLE rejection_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp DATETIME NOT NULL,
                request_id TEXT,
                api_key_id INTEGER,
                path TEXT NOT NULL,
                method TEXT NOT NULL,
                client_ip TEXT,
                status_code INTEGER NOT NULL,
                category TEXT NOT NULL,
                reason TEXT,
                auth_source TEXT
            )
            """
        )
        conn.commit()
        conn.close()

        RejectionsDB(db_filename="legacy_rejections.db")

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(rejection_events)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()

        self.assertIn("x_title", columns)


class RejectionsDBInsertGetTests(_RejectionsDBTestBase):
    def _insert_one(self, **kwargs):
        defaults = dict(
            request_id="req-1",
            api_key_id=5,
            path="/v1/chat/completions",
            method="POST",
            client_ip="127.0.0.1",
            status_code=403,
            category="key_disabled",
            reason="API key is disabled",
            auth_source="bearer-virtual",
            x_title="tgBot",
        )
        defaults.update(kwargs)
        self.db.insert_rejection(**defaults)

    def test_insert_and_get_without_batcher(self):
        self._insert_one()
        items, total = run_async(self.db.get_rejections())
        self.assertEqual(total, 1)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["category"], "key_disabled")
        self.assertEqual(items[0]["status_code"], 403)
        self.assertEqual(items[0]["path"], "/v1/chat/completions")
        self.assertEqual(items[0]["x_title"], "tgBot")

    def test_filter_by_category(self):
        self._insert_one(category="key_disabled")
        self._insert_one(category="rate_limited", status_code=429)
        items, total = run_async(self.db.get_rejections(category="rate_limited"))
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["category"], "rate_limited")

    def test_filter_by_api_key_id(self):
        self._insert_one(api_key_id=10)
        self._insert_one(api_key_id=20)
        items, total = run_async(self.db.get_rejections(api_key_id=10))
        self.assertEqual(total, 1)
        self.assertEqual(items[0]["api_key_id"], 10)

    def test_filter_by_since(self):
        past = "2000-01-01T00:00:00+00:00"
        future = "2099-01-01T00:00:00+00:00"
        self._insert_one()
        items_all, total_all = run_async(self.db.get_rejections(since=past))
        self.assertEqual(total_all, 1)

        items_none, total_none = run_async(self.db.get_rejections(since=future))
        self.assertEqual(total_none, 0)
        self.assertEqual(len(items_none), 0)

    def test_dashboard_rejections_aggregates_scoped_rows(self):
        self._insert_one(api_key_id=5, category="rate_limited", status_code=429)
        self._insert_one(api_key_id=6, category="auth_invalid", status_code=401)

        data = run_async(
            self.db.get_dashboard_rejections(
                "day",
                datetime.fromisoformat("2000-01-01T00:00:00+00:00"),
                datetime.fromisoformat("2100-01-01T00:00:00+00:00"),
                api_key_id=5,
                category="rate_limited",
                status_code=429,
            )
        )

        self.assertEqual(data["summary"]["rejections"], 1)
        self.assertEqual(data["categories"], [{"label": "rate_limited", "rejections": 1}])
        self.assertEqual(data["status_codes"], [{"label": "429", "status_code": 429, "rejections": 1}])
        self.assertEqual(data["recent"][0]["api_key_id"], 5)


class RejectionsDBPaginationTests(_RejectionsDBTestBase):
    def _seed(self, count: int) -> None:
        conn = sqlite3.connect(self.db.db_path)
        for i in range(count):
            conn.execute(
                """INSERT INTO rejection_events
                   (timestamp, request_id, api_key_id, path, method, client_ip,
                    status_code, category, reason, auth_source)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    f"2026-01-01T00:00:{i:02d}+00:00",
                    f"req-{i}",
                    None,
                    "/v1/test",
                    "GET",
                    None,
                    401,
                    "auth_invalid",
                    f"rec {i}",
                    None,
                ),
            )
        conn.commit()
        conn.close()

    def test_offset_paginates_within_total(self):
        self._seed(5)  # newest-first ordering → req-4, req-3, req-2, req-1, req-0
        page1, total1 = run_async(self.db.get_rejections(limit=2, offset=0))
        self.assertEqual(total1, 5)
        self.assertEqual([r["request_id"] for r in page1], ["req-4", "req-3"])

        page2, total2 = run_async(self.db.get_rejections(limit=2, offset=2))
        self.assertEqual(total2, 5)
        self.assertEqual([r["request_id"] for r in page2], ["req-2", "req-1"])

        page3, _ = run_async(self.db.get_rejections(limit=2, offset=4))
        self.assertEqual([r["request_id"] for r in page3], ["req-0"])

    def test_offset_beyond_end_returns_empty_but_total_intact(self):
        self._seed(3)
        items, total = run_async(self.db.get_rejections(limit=10, offset=10))
        self.assertEqual(items, [])
        self.assertEqual(total, 3)

    def test_negative_offset_raises(self):
        with self.assertRaises(ValueError):
            run_async(self.db.get_rejections(offset=-1))


class RejectionsDBCleanupTests(_RejectionsDBTestBase):
    def test_cleanup_removes_old_records(self):
        # Insert a record with a very old timestamp directly via SQL
        conn = sqlite3.connect(self.db.db_path)
        conn.execute(
            """INSERT INTO rejection_events
               (timestamp, request_id, api_key_id, path, method, client_ip,
                status_code, category, reason, auth_source)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "2000-01-01T00:00:00",
                "old-req",
                None,
                "/v1/test",
                "GET",
                None,
                401,
                "auth_invalid",
                "old record",
                None,
            ),
        )
        conn.commit()
        conn.close()

        items, total = run_async(self.db.get_rejections())
        self.assertEqual(total, 1)

        self.db.cleanup_old_records(retention_days=30)

        items, total = run_async(self.db.get_rejections())
        self.assertEqual(total, 0)


class RecordRejectionContainerTests(unittest.TestCase):
    @staticmethod
    def _request(app: FastAPI) -> Request:
        request = Request(
            {
                "type": "http",
                "asgi": {"version": "3.0", "spec_version": "2.3"},
                "http_version": "1.1",
                "server": ("testserver", 80),
                "client": ("10.0.0.1", 1234),
                "scheme": "http",
                "method": "POST",
                "root_path": "",
                "path": "/v1/chat/completions",
                "raw_path": b"/v1/chat/completions",
                "query_string": b"",
                "headers": [(b"x-title", b" tgBot ")],
                "app": app,
                "state": {},
            }
        )
        request.state.llmgateway_request_id = "req-abc"
        request.state.llmgateway_active_request_id = None
        request.state.api_key_id = 3
        request.state.gateway_auth_source = "bearer-virtual"
        return request

    def test_missing_services_raises_without_warning_fallback(self):
        app = FastAPI()
        legacy_db = MagicMock(spec=RejectionsDB)
        app.state.rejections_db = legacy_db
        request = self._request(app)

        with patch.object(rejections_db_module.logger, "warning") as warning:
            with self.assertRaises(AttributeError):
                record_rejection(
                    request,
                    status_code=401,
                    reason="test",
                    category="auth_invalid",
                )

        warning.assert_not_called()
        legacy_db.insert_rejection.assert_not_called()

    def test_record_rejection_uses_container_db_and_preserves_payload(self):
        container_db = MagicMock(spec=RejectionsDB)
        legacy_db = MagicMock(spec=RejectionsDB)
        app = FastAPI()
        app.state.rejections_db = legacy_db
        bind_app_services(app, rejections_db=container_db)
        request = self._request(app)

        record_rejection(request, status_code=429, reason="rate limit", category="rate_limited")

        container_db.insert_rejection.assert_called_once_with(
            request_id="req-abc",
            api_key_id=3,
            path="/v1/chat/completions",
            method="POST",
            client_ip="10.0.0.1",
            status_code=429,
            category="rate_limited",
            reason="rate limit",
            auth_source="bearer-virtual",
            x_title="tgBot",
        )
        legacy_db.insert_rejection.assert_not_called()

    def test_record_rejection_swallows_container_insert_exception(self):
        container_db = MagicMock(spec=RejectionsDB)
        container_db.insert_rejection.side_effect = RuntimeError("db is down")
        legacy_db = MagicMock(spec=RejectionsDB)
        app = FastAPI()
        app.state.rejections_db = legacy_db
        bind_app_services(app, rejections_db=container_db)
        request = self._request(app)

        # Must not raise even though insert_rejection blows up — the caller's
        # error response must not be suppressed by audit failures.
        with self.assertLogs("llm_gateway_core.db.rejections_db", level="ERROR") as log_ctx:
            record_rejection(
                request, status_code=403, reason="boom", category="unauthorized"
            )

        container_db.insert_rejection.assert_called_once()
        legacy_db.insert_rejection.assert_not_called()
        self.assertTrue(any("record_rejection" in msg for msg in log_ctx.output))


if __name__ == "__main__":
    unittest.main()
