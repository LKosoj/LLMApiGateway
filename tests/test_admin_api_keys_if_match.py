"""Tests for P1-7: optimistic-concurrency protection on ``PATCH /admin/api-keys``.

``update_api_key`` used to apply whatever the client sent unconditionally, so
two tabs editing the same key could silently clobber each other's changes.
``GET /admin/api-keys/{id}`` now returns an ``ETag`` derived from the
admin-editable fields, and ``PATCH`` honors an optional ``If-Match`` header:
present and stale -> 412 (no mutation applied); present and fresh -> normal
200; absent -> unchanged legacy behavior (no precondition check at all).
"""

from __future__ import annotations

import asyncio
import os
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_api_keys import admin_api_keys_router
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests.runtime_test_support import bind_app_services


def _make_accounting_service(db: ApiKeysDB, rate_limiter: RateLimiter, ledger: UsdBudgetLedger):
    from unittest.mock import create_autospec

    from llm_gateway_core.services.accounting_service import AccountingService

    service = create_autospec(AccountingService, instance=True, spec_set=True)

    async def update_key(key_id: int, *, changes: dict[str, object], reset_spent: bool):
        record = await asyncio.to_thread(
            db.update_accounting_key, key_id, changes=changes, reset_spent=reset_spent
        )
        if record is not None and {"rpm", "tpm"} & changes.keys():
            rate_limiter.reset(key_id)
        if record is not None and reset_spent:
            ledger.reset_record(key_id, budget_usd=record.budget_usd, spent_usd=record.spent_usd)
        return record

    service.update_key.side_effect = update_key
    return service


def _build_app(db: ApiKeysDB) -> FastAPI:
    app = FastAPI()
    rate_limiter = RateLimiter()
    ledger = UsdBudgetLedger()
    bind_app_services(
        app,
        api_keys_db=db,
        rate_limiter=rate_limiter,
        usd_budget_ledger=ledger,
        accounting_service=_make_accounting_service(db, rate_limiter, ledger),
    )
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(admin_api_keys_router, prefix="/v1")
    return app


class AdminApiKeysIfMatchTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_admin_if_match.db")

        master_key = "master-test-token"
        self.master_key = master_key
        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        self.app = _build_app(self.db)
        self.accounting_service = self.app.state.services.accounting_service
        self.client = TestClient(self.app)

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def _get_etag(self, key_id: int) -> str:
        resp = self.client.get(f"/v1/admin/api-keys/{key_id}", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        etag = resp.headers.get("ETag")
        self.assertIsNotNone(etag)
        return etag

    def test_get_returns_etag_header(self):
        record = self.db.create(name="team-etag")
        etag = self._get_etag(record.id)
        self.assertTrue(etag.startswith('"') and etag.endswith('"'))

    def test_patch_without_if_match_is_unchanged_legacy_behavior(self):
        record = self.db.create(name="team-legacy")
        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "team-legacy-renamed"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "team-legacy-renamed")

    def test_patch_with_fresh_if_match_succeeds(self):
        record = self.db.create(name="team-fresh")
        etag = self._get_etag(record.id)
        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "team-fresh-renamed"},
            headers={**self._master_headers(), "If-Match": etag},
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["name"], "team-fresh-renamed")

    def test_patch_with_stale_if_match_returns_412_and_does_not_mutate(self):
        record = self.db.create(name="team-stale")
        stale_etag = self._get_etag(record.id)

        # Another tab/admin updates the record first, without If-Match.
        other_update = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "team-stale-from-other-tab"},
            headers=self._master_headers(),
        )
        self.assertEqual(other_update.status_code, 200)

        self.accounting_service.update_key.reset_mock()
        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "team-stale-from-stale-tab"},
            headers={**self._master_headers(), "If-Match": stale_etag},
        )

        self.assertEqual(resp.status_code, 412)
        self.assertEqual(resp.json()["error"]["code"], "api_key_conflict")
        self.accounting_service.update_key.assert_not_awaited()

        # The other tab's change must survive untouched.
        current = self.db.get_by_id(record.id)
        self.assertEqual(current.name, "team-stale-from-other-tab")

    def test_patch_if_match_for_missing_key_returns_404_not_412(self):
        resp = self.client.patch(
            "/v1/admin/api-keys/999999",
            json={"name": "does-not-exist"},
            headers={**self._master_headers(), "If-Match": '"whatever"'},
        )
        self.assertEqual(resp.status_code, 404)

    def test_etag_ignores_usage_driven_fields(self):
        # spent_usd/last_used_at change on every gateway request, independent
        # of admin edits; the ETag must not fluctuate because of them, or a
        # busy key would 412 on virtually every admin save.
        record = self.db.create(name="team-usage-driven", budget_usd=10.0)
        etag_before = self._get_etag(record.id)

        with sqlite3.connect(self.db.db_path) as conn:
            conn.execute(
                "UPDATE api_keys SET spent_usd = spent_usd + 1.0, last_used_at = ? WHERE id = ?",
                ("2024-01-01T00:00:00+00:00", record.id),
            )

        etag_after = self._get_etag(record.id)
        self.assertEqual(etag_before, etag_after)

    def test_concurrent_patches_serialized_by_per_key_lock(self):
        # Two admins hit PATCH at the same time with the same fresh etag.
        # Without the per-key asyncio.Lock, both would pass the etag check
        # inside the endpoint and both would write. With the lock, the second
        # coroutine re-reads the record inside the critical section, sees the
        # first coroutine's fresh etag, and correctly returns 412.
        record = self.db.create(name="team-concurrent")
        shared_etag = self._get_etag(record.id)

        first_inside_update = asyncio.Event()
        release_first_update = asyncio.Event()

        original_update = self.accounting_service.update_key.side_effect

        async def instrumented_update(key_id: int, *, changes, reset_spent):
            if not first_inside_update.is_set():
                first_inside_update.set()
                await release_first_update.wait()
            return await original_update(
                key_id, changes=changes, reset_spent=reset_spent
            )

        self.accounting_service.update_key.side_effect = instrumented_update

        async def run() -> tuple[int, int]:
            transport = httpx.ASGITransport(app=self.app)
            async with httpx.AsyncClient(
                transport=transport, base_url="http://testserver"
            ) as client:
                first_task = asyncio.create_task(
                    client.patch(
                        f"/v1/admin/api-keys/{record.id}",
                        json={"name": "team-concurrent-first"},
                        headers={**self._master_headers(), "If-Match": shared_etag},
                    )
                )
                # Wait until the first request is safely past the etag check
                # and inside the (mocked) update call, then fire the second
                # request holding the same now-fresh etag.
                await first_inside_update.wait()
                second_task = asyncio.create_task(
                    client.patch(
                        f"/v1/admin/api-keys/{record.id}",
                        json={"name": "team-concurrent-second"},
                        headers={**self._master_headers(), "If-Match": shared_etag},
                    )
                )
                # Give the second request enough scheduling to reach the lock
                # so it queues behind the first, then let the first finish.
                await asyncio.sleep(0)
                release_first_update.set()
                first_resp, second_resp = await asyncio.gather(first_task, second_task)
                return first_resp.status_code, second_resp.status_code

        first_status, second_status = asyncio.run(run())

        self.assertEqual(first_status, 200)
        self.assertEqual(second_status, 412)
        current = self.db.get_by_id(record.id)
        self.assertEqual(current.name, "team-concurrent-first")

    def test_stale_if_match_still_rejected_after_usage_only_change(self):
        # A usage-only change must NOT reset staleness detection for a real
        # admin-field edit that happened in between.
        record = self.db.create(name="team-mixed")
        etag0 = self._get_etag(record.id)

        rename = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "team-mixed-renamed"},
            headers=self._master_headers(),
        )
        self.assertEqual(rename.status_code, 200)

        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"budget_usd": 5.0},
            headers={**self._master_headers(), "If-Match": etag0},
        )
        self.assertEqual(resp.status_code, 412)


if __name__ == "__main__":
    unittest.main()
