"""Tests for the Quota Dashboard API endpoint.

Tests cover:
- Master sees all keys
- Virtual key sees only its own card
- Countdown calculation from oldest_event_age
- No activity => reset_in_seconds = 60
- Budget fields
- Empty key list
- Fallback events count
- TTL cache
"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.quota import quota_router
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import ROLE_USER, api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests._async_compat import run_async
from tests.runtime_test_support import bind_app_services

import llm_gateway_core.api.v1.quota as quota_module


class _FakeFallbackEventsDB:
    def __init__(self, counts: dict[int, int] | None = None):
        self._counts = counts or {}
        self.calls: list[int | None] = []

    async def get_events_count_last_24h(self, api_key_id: int | None = None) -> dict[int, int]:
        self.calls.append(api_key_id)
        if api_key_id is not None:
            return {k: v for k, v in self._counts.items() if k == api_key_id}
        return dict(self._counts)


def _build_app(db: ApiKeysDB, rate_limiter: RateLimiter | None = None,
               fallback_db=None) -> FastAPI:
    app = FastAPI()
    bind_app_services(
        app,
        api_keys_db=db,
        fallback_events_db=(
            fallback_db if fallback_db is not None else _FakeFallbackEventsDB()
        ),
        rate_limiter=rate_limiter if rate_limiter is not None else RateLimiter(),
        usd_budget_ledger=UsdBudgetLedger(),
    )
    app.middleware("http")(api_key_auth)
    app.include_router(auth_router)
    app.include_router(quota_router, prefix="/v1")
    return app


class QuotaEndpointTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_quota.db")
        self.master_key = "master-quota-test"

        key_patchers = [
            patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", self.master_key),
            patch("llm_gateway_core.api.auth_ui.settings.gateway_api_key", self.master_key),
        ]
        for p in key_patchers:
            p.start()
            self.addCleanup(p.stop)

        # Invalidate cache before each test
        quota_module._quota_cache.invalidate()

    def _master_headers(self) -> dict:
        return {"Authorization": f"Bearer {self.master_key}"}

    def test_quota_keys_master_sees_all(self):
        """Master caller receives cards for every virtual key."""
        self.db.create(name="key-alpha", rpm=10, tpm=1000)
        self.db.create(name="key-beta", rpm=20)

        app = _build_app(self.db)
        client = TestClient(app)

        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        names = [d["name"] for d in data]
        self.assertIn("key-alpha", names)
        self.assertIn("key-beta", names)
        self.assertEqual(len(data), 2)

    def test_quota_keys_virtual_key_sees_only_own(self):
        """Virtual-key caller can only see its own card."""
        rec_a = self.db.create(name="owner", rpm=5)
        rec_b = self.db.create(name="other", rpm=5)

        app = _build_app(self.db)
        client = TestClient(app)

        # Authenticate as a virtual key (use the raw key value)
        virt_headers = {"Authorization": f"Bearer {rec_a.api_key}"}
        resp = client.get("/v1/api/quota/keys", headers=virt_headers)
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        self.assertEqual(len(data), 1)
        self.assertEqual(data[0]["key_id"], rec_a.id)
        ids = [d["key_id"] for d in data]
        self.assertNotIn(rec_b.id, ids)

    def test_countdown_rpm_calculation(self):
        """oldest_event 20 s ago → reset_in_seconds = 40."""
        rec = self.db.create(name="timed-key", rpm=60, tpm=None)

        # Build a RateLimiter and record events at different timestamps
        fake_now = 1000.0
        rl2 = RateLimiter(time_func=lambda: fake_now - 20.0)
        rl2.record(rec.id, tokens=0)
        rl2._time = lambda: fake_now - 10.0
        rl2.record(rec.id, tokens=0)
        rl2._time = lambda: fake_now
        rl2.record(rec.id, tokens=0)
        # Now oldest event is at fake_now - 20, so reset_in_seconds should be 40
        rpm_count, tpm_tokens, oldest_age = rl2.get_window_snapshot(rec.id)
        expected_reset = max(0, int(60.0 - oldest_age))
        self.assertAlmostEqual(oldest_age, 20.0, delta=1.0)
        self.assertEqual(expected_reset, 40)

        app = _build_app(self.db, rate_limiter=rl2)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        card = next(d for d in data if d["key_id"] == rec.id)
        # reset_in_seconds should be ~40 (allow 2-second drift for test execution)
        self.assertAlmostEqual(card["rpm"]["reset_in_seconds"], 40, delta=2)

    def test_countdown_expired_window(self):
        """No events in window → reset_in_seconds = 60 (full window)."""
        rec = self.db.create(name="idle-key", rpm=60)
        # RateLimiter with no recorded events
        rl = RateLimiter()

        app = _build_app(self.db, rate_limiter=rl)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        card = next(d for d in data if d["key_id"] == rec.id)
        self.assertEqual(card["rpm"]["reset_in_seconds"], 60)

    def test_budget_fields(self):
        """Budget fields are populated correctly."""
        rec = self.db.create(name="budgeted", budget_usd=5.0)
        # Manually set spent_usd via direct record_spent (sync path)
        self.db.record_spent(rec.id, 1.23)
        # Re-fetch
        updated = self.db.get_by_id(rec.id)

        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        card = next(d for d in data if d["key_id"] == rec.id)
        self.assertEqual(card["budget"]["limit_usd"], 5.0)
        self.assertAlmostEqual(card["budget"]["spent_usd"], updated.spent_usd, places=4)

    def test_quota_empty_no_keys(self):
        """Empty database returns an empty list."""
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json(), [])

    def test_fallback_events_count(self):
        """fallback_events_24h is populated from the FallbackEventsDB."""
        rec_a = self.db.create(name="with-fb")
        rec_b = self.db.create(name="no-fb")
        fake_fb_db = _FakeFallbackEventsDB({rec_a.id: 7})

        app = _build_app(self.db, fallback_db=fake_fb_db)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        data = resp.json()
        card_a = next(d for d in data if d["key_id"] == rec_a.id)
        card_b = next(d for d in data if d["key_id"] == rec_b.id)
        self.assertEqual(card_a["fallback_events_24h"], 7)
        self.assertEqual(card_b["fallback_events_24h"], 0)

    def test_quota_cache_ttl(self):
        """Repeated calls within TTL return cached data without re-querying."""
        self.db.create(name="cached-key")
        call_count = 0
        original_list_all = self.db.list_all

        def counting_list_all():
            nonlocal call_count
            call_count += 1
            return original_list_all()

        app = _build_app(self.db)
        client = TestClient(app)

        with patch.object(
            app.state.services.api_keys_db,
            "list_all",
            side_effect=counting_list_all,
        ):
            # First call — populates cache
            r1 = client.get("/v1/api/quota/keys", headers=self._master_headers())
            # Second call — should hit cache, list_all not called again
            r2 = client.get("/v1/api/quota/keys", headers=self._master_headers())

        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        # Cache is shared by key_id_filter=None; exactly 1 real list_all call for 2 requests
        self.assertEqual(call_count, 1)

    def test_response_structure(self):
        """Each key card contains the required fields."""
        self.db.create(name="struct-key", rpm=30, tpm=5000, budget_usd=10.0)
        app = _build_app(self.db)
        client = TestClient(app)
        resp = client.get("/v1/api/quota/keys", headers=self._master_headers())
        self.assertEqual(resp.status_code, 200)
        card = resp.json()[0]
        for field in ("key_id", "name", "disabled", "rpm", "tpm", "budget", "fallback_events_24h"):
            self.assertIn(field, card, f"Missing field: {field}")
        for sub in ("current", "limit", "reset_in_seconds"):
            self.assertIn(sub, card["rpm"])
            self.assertIn(sub, card["tpm"])
        for sub in ("spent_usd", "limit_usd"):
            self.assertIn(sub, card["budget"])


    def test_quota_keys_requires_auth(self):
        """Unauthenticated request returns 401."""
        app = _build_app(self.db)
        client = TestClient(app)
        response = client.get("/v1/api/quota/keys")  # no Authorization header
        self.assertEqual(response.status_code, 401)

    def test_malformed_user_key_ids_fail_before_database_query(self):
        app = _build_app(self.db)

        with patch.object(self.db, "list_all", wraps=self.db.list_all) as list_all:
            for key_id in (None, True, False, 0, -1, 1.0):
                with self.subTest(key_id=key_id):
                    request = Request(
                        {
                            "type": "http",
                            "app": app,
                            "state": {
                                "api_key_role": ROLE_USER,
                                "api_key_id": key_id,
                            },
                        }
                    )

                    with self.assertRaises(HTTPException) as raised:
                        run_async(quota_module.get_quota_keys(request))

                    self.assertEqual(raised.exception.status_code, 401)
                    self.assertEqual(raised.exception.detail, "Authentication required.")

        list_all.assert_not_called()

    def test_mandatory_empty_dependencies_produce_zero_snapshot(self):
        record = self.db.create(name="empty-dependencies")
        fallback_db = _FakeFallbackEventsDB()
        rate_limiter = RateLimiter()
        app = _build_app(
            self.db,
            rate_limiter=rate_limiter,
            fallback_db=fallback_db,
        )

        with patch.object(
            rate_limiter,
            "get_window_snapshot",
            return_value=(0, 0, None),
        ) as get_window_snapshot:
            response = TestClient(app).get(
                "/v1/api/quota/keys",
                headers=self._master_headers(),
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(fallback_db.calls, [None])
        get_window_snapshot.assert_called_once_with(record.id)
        card = response.json()[0]
        self.assertEqual(card["rpm"]["current"], 0)
        self.assertEqual(card["tpm"]["current"], 0)
        self.assertEqual(card["rpm"]["reset_in_seconds"], 60)
        self.assertEqual(card["fallback_events_24h"], 0)

    def test_database_exception_is_not_exposed_in_response(self):
        app = _build_app(self.db)
        client = TestClient(app)

        with patch.object(
            self.db,
            "list_all",
            side_effect=RuntimeError("postgres://admin:secret-token@db/quota"),
        ):
            response = client.get(
                "/v1/api/quota/keys",
                headers=self._master_headers(),
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.json()["detail"], "Could not retrieve quota data.")
        self.assertNotIn("secret-token", response.text)

    def test_container_dependencies_win_over_conflicting_legacy_aliases(self):
        self.db.create(name="container-quota")
        app = _build_app(self.db)
        app.state.api_keys_db = object()
        app.state.fallback_events_db = object()
        app.state.rate_limiter = object()

        response = TestClient(app).get(
            "/v1/api/quota/keys",
            headers=self._master_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()[0]["name"], "container-quota")


if __name__ == "__main__":
    unittest.main()
