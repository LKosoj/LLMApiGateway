"""Focused tests for the Topology tab scope microcopy (P0-5).

/v1/topology is intentionally readable by both roles: master sees the full
routing graph, and "active request counts" are scoped to the caller's own
key. static/usage-stats.html renders one static page regardless of caller
role, so both master and virtual (user) keys must see the same two scope
labels under the Topology block header.

Test plan:
1. test_topology_scope_note_present_for_master
2. test_topology_scope_note_present_for_user
3. test_en_and_ru_catalogs_define_scope_strings
"""
from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.stats import stats_router
from llm_gateway_core.config.paths import STATIC_DIR
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from tests.runtime_test_support import bind_app_services


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    bind_app_services(app, api_keys_db=db)
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(stats_router, prefix="/v1")
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")
    return app


class TopologyScopeNoteHtmlTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        os.makedirs(Path(self._tmp.name) / "db", exist_ok=True)

        path_patch = patch.object(
            api_keys_db_module,
            "__file__",
            str(Path(self._tmp.name) / "llm_gateway_core" / "db" / "api_keys_db.py"),
        )
        path_patch.start()
        self.addCleanup(path_patch.stop)

        self.db = ApiKeysDB(db_filename="test_topology_scope_note.db")
        self.master_key = "master-topology-scope-test"

        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", self.master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        self.app = _build_app(self.db)
        self.client = self.enterContext(TestClient(self.app))

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def _user_headers(self) -> dict:
        record = self.db.create(name="topology-scope-user")
        return {"Authorization": f"Bearer {record.api_key}"}

    def _assert_scope_note(self, html: str) -> None:
        self.assertIn('class="topology-scope-note"', html)
        self.assertIn('data-i18n="topology:scope.config"', html)
        self.assertIn('data-i18n="topology:scope.activity"', html)
        # Must render inside the Topology tab panel, not as a standalone card.
        topology_start = html.index('id="topologyTabContent"')
        note_start = html.index('class="topology-scope-note"')
        toolbar_start = html.index('class="topology-toolbar"')
        self.assertTrue(topology_start < note_start < toolbar_start)

    def test_topology_scope_note_present_for_master(self):
        """Master sees the shared-config / scoped-activity note."""
        resp = self.client.get("/v1/ui/usage-stats", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        self._assert_scope_note(resp.text)

    def test_topology_scope_note_present_for_user(self):
        """A virtual (user-role) key sees the identical note."""
        resp = self.client.get("/v1/ui/usage-stats", headers=self._user_headers())
        self.assertEqual(resp.status_code, 200)
        self._assert_scope_note(resp.text)


class TopologyScopeNoteCatalogTests(unittest.TestCase):
    """Locale catalogs carry the strings referenced by the new bindings."""

    def test_en_and_ru_catalogs_define_scope_strings(self):
        expected_by_locale = {
            "en": {
                "config": "Gateway configuration — shared",
                "activity": "Activity — scoped to current key",
            },
            "ru": {
                "config": "Конфигурация шлюза — общая",
                "activity": "Активность — по текущему ключу",
            },
        }
        for locale, expected in expected_by_locale.items():
            catalog = json.loads(
                (STATIC_DIR / "locales" / locale / "topology.json").read_text(encoding="utf-8")
            )
            self.assertEqual(catalog["scope"], expected)


if __name__ == "__main__":
    unittest.main()
