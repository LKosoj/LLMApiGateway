"""Integration tests for the Admin rejections endpoint."""

from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.admin_rejections import admin_rejections_router
from llm_gateway_core.db import rejections_db as rejections_db_module
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.middleware.auth import (
    DEFAULT_UI_PATH,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    api_key_auth,
    create_authenticated_session,
)
from tests.runtime_test_support import bind_app_services


def _build_app(db: RejectionsDB) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, rejections_db=db)
    app.middleware("http")(api_key_auth)
    app.include_router(admin_rejections_router, prefix="/v1")
    return app


class _FakeApiKeysDB:
    """Minimal stand-in so session auth can resolve a virtual key by id."""

    def __init__(self, record: ApiKeyRecord):
        self.record = record

    def get_by_id(self, key_id: int) -> ApiKeyRecord | None:
        return self.record if key_id == self.record.id else None


class AdminRejectionsHttpTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(Path(self._tmp.name) / "db", exist_ok=True)

        path_patch = patch.object(
            rejections_db_module,
            "__file__",
            str(Path(self._tmp.name) / "llm_gateway_core" / "db" / "rejections_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.db = RejectionsDB(db_filename="test_admin_rejections.db")

        self.master_key = "master-test-token"
        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        self.app = _build_app(self.db)
        self.client = TestClient(self.app)
        self.addCleanup(self.client.close)

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def test_master_can_list_rejections(self):
        self.db.insert_rejection(
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

        resp = self.client.get("/v1/admin/rejections", headers=self._master_headers())

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["total"], 1)
        self.assertEqual(len(body["items"]), 1)
        self.assertEqual(body["items"][0]["category"], "key_disabled")
        self.assertEqual(body["items"][0]["x_title"], "tgBot")

    def test_container_db_wins_over_conflicting_legacy_alias(self):
        self.app.state.rejections_db = object()

        resp = self.client.get("/v1/admin/rejections", headers=self._master_headers())

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), {"items": [], "total": 0})

    def test_unknown_category_returns_400(self):
        resp = self.client.get(
            "/v1/admin/rejections",
            params={"category": "definitely-not-valid"},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("category", resp.json()["detail"].lower())

    def test_valid_category_filter_returns_200(self):
        resp = self.client.get(
            "/v1/admin/rejections",
            params={"category": "rate_limited"},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 0)

    def test_ip_blocked_category_is_accepted(self):
        self.db.insert_rejection(
            request_id="req-block",
            api_key_id=None,
            path="/v1/chat/completions",
            method="POST",
            client_ip="150.109.231.218",
            status_code=429,
            category="ip_blocked",
            reason="IP blocked for 1200s after 5 consecutive failed auth attempts",
            auth_source=None,
        )

        resp = self.client.get(
            "/v1/admin/rejections",
            params={"category": "ip_blocked"},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["total"], 1)
        self.assertEqual(resp.json()["items"][0]["client_ip"], "150.109.231.218")

    def test_invalid_since_returns_400(self):
        resp = self.client.get(
            "/v1/admin/rejections",
            params={"since": "not-a-timestamp"},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 400)
        self.assertIn("since", resp.json()["detail"].lower())

    def test_offset_paginates_results(self):
        for i in range(3):
            self.db.insert_rejection(
                request_id=f"r{i}",
                api_key_id=None,
                path="/v1/x",
                method="GET",
                client_ip=None,
                status_code=401,
                category="auth_invalid",
                reason=f"n{i}",
                auth_source=None,
            )

        first = self.client.get(
            "/v1/admin/rejections",
            params={"limit": 2, "offset": 0},
            headers=self._master_headers(),
        )
        self.assertEqual(first.status_code, 200)
        self.assertEqual(first.json()["total"], 3)
        self.assertEqual(len(first.json()["items"]), 2)

        second = self.client.get(
            "/v1/admin/rejections",
            params={"limit": 2, "offset": 2},
            headers=self._master_headers(),
        )
        self.assertEqual(second.status_code, 200)
        self.assertEqual(second.json()["total"], 3)
        self.assertEqual(len(second.json()["items"]), 1)

    def test_negative_offset_returns_400(self):
        resp = self.client.get(
            "/v1/admin/rejections",
            params={"offset": -1},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_since_filter_works(self):
        self.db.insert_rejection(
            request_id="req-since",
            api_key_id=None,
            path="/v1/models",
            method="GET",
            client_ip="127.0.0.1",
            status_code=401,
            category="auth_invalid",
            reason="missing key",
            auth_source=None,
        )

        past = self.client.get(
            "/v1/admin/rejections",
            params={"since": "2000-01-01T00:00:00+00:00"},
            headers=self._master_headers(),
        )
        self.assertEqual(past.status_code, 200)
        self.assertEqual(past.json()["total"], 1)

        future = self.client.get(
            "/v1/admin/rejections",
            params={"since": "2099-01-01T00:00:00+00:00"},
            headers=self._master_headers(),
        )
        self.assertEqual(future.status_code, 200)
        self.assertEqual(future.json()["total"], 0)


class RejectionsUiMasterOnlyTests(unittest.TestCase):
    """The /v1/ui/rejections HTML page must be reachable only by the master."""

    def setUp(self) -> None:
        self.gateway_key = "test-gateway-key"
        patcher = patch(
            "llm_gateway_core.middleware.auth.settings.gateway_api_key",
            self.gateway_key,
        )
        patcher.start()
        self.addCleanup(patcher.stop)

        self.user_record = ApiKeyRecord(
            id=11,
            name="user",
            api_key="virtual-key",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
            disabled=False,
        )

        app = FastAPI()
        bind_app_services(app, api_keys_db=_FakeApiKeysDB(self.user_record))
        app.middleware("http")(api_key_auth)
        app.include_router(admin_rejections_router, prefix="/v1")
        self.client = TestClient(app)
        self.addCleanup(self.client.close)

    def test_master_session_can_open_rejections_ui(self):
        self.client.cookies.set(SESSION_COOKIE_NAME, create_authenticated_session())
        resp = self.client.get("/v1/ui/rejections", headers={"Accept": "text/html"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/html", resp.headers["content-type"])
        self.assertIn("Rejections Audit", resp.text)
        self.assertIn("X-Title", resp.text)

    def test_non_master_session_is_redirected_from_rejections_ui(self):
        self.client.cookies.set(
            SESSION_COOKIE_NAME,
            create_authenticated_session(role=ROLE_USER, key_id=self.user_record.id),
        )
        resp = self.client.get(
            "/v1/ui/rejections",
            headers={"Accept": "text/html"},
            follow_redirects=False,
        )
        self.assertEqual(resp.status_code, 303)
        self.assertEqual(resp.headers["location"], DEFAULT_UI_PATH)


if __name__ == "__main__":
    unittest.main()
