"""Integration tests for Admin API keys endpoints."""

from __future__ import annotations

import asyncio
import inspect
import json
import os
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock, call, create_autospec, patch

from fastapi import FastAPI
from fastapi.testclient import TestClient
from starlette.requests import Request

from llm_gateway_core.api.auth_ui import auth_router
from llm_gateway_core.api.v1.admin_api_keys import (
    ApiKeyUpdatePayload,
    _apply_update,
    admin_api_keys_router,
    delete_api_key,
    update_api_key,
)
from llm_gateway_core.db import api_keys_db as api_keys_db_module
from llm_gateway_core.db.api_keys_db import ApiKeysDB
from llm_gateway_core.middleware.auth import api_key_auth
from llm_gateway_core.services.access_control import UsdBudgetLedger
from llm_gateway_core.services.accounting import AccountingError, AccountingErrorCode
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests._async_compat import run_async
from tests.runtime_test_support import bind_app_services


def _make_accounting_service(
    db: ApiKeysDB,
    rate_limiter: RateLimiter,
    ledger: UsdBudgetLedger,
) -> AccountingService:
    service = create_autospec(AccountingService, instance=True, spec_set=True)

    async def update_key(
        key_id: int,
        *,
        changes: dict[str, object],
        reset_spent: bool,
    ):
        record = await asyncio.to_thread(
            db.update_accounting_key,
            key_id,
            changes=changes,
            reset_spent=reset_spent,
        )
        if record is not None and {"rpm", "tpm"} & changes.keys():
            rate_limiter.reset(key_id)
        if record is not None and reset_spent:
            ledger.reset_record(
                key_id,
                budget_usd=record.budget_usd,
                spent_usd=record.spent_usd,
            )
        return record

    async def delete_key(key_id: int) -> bool:
        deleted = await asyncio.to_thread(
            db.delete_to_accounting_tombstone,
            key_id,
            deleted_at=datetime.now(timezone.utc),
        )
        if deleted:
            rate_limiter.reset(key_id)
            ledger.discard_record(key_id)
        return deleted

    service.update_key.side_effect = update_key
    service.delete_key.side_effect = delete_key
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


class AdminApiKeysHttpTests(unittest.TestCase):
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

        self.db = ApiKeysDB(db_filename="test_admin.db")

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

    def test_master_can_create_list_update_delete(self):
        # Create
        create_resp = self.client.post(
            "/v1/admin/api-keys",
            json={
                "name": "team-b",
                "budget_usd": 10.0,
                "rpm": 30,
                "allowed_models": ["gpt-4o"],
            },
            headers=self._master_headers(),
        )
        self.assertEqual(create_resp.status_code, 201)
        body = create_resp.json()
        self.assertEqual(body["name"], "team-b")
        self.assertTrue(body["api_key"].startswith("lgk_"))
        key_id = body["id"]

        # List
        list_resp = self.client.get("/v1/admin/api-keys", headers=self._master_headers())
        self.assertEqual(list_resp.status_code, 200)
        self.assertEqual(len(list_resp.json()["keys"]), 1)

        # Update
        update_resp = self.client.patch(
            f"/v1/admin/api-keys/{key_id}",
            json={"disabled": True},
            headers=self._master_headers(),
        )
        self.assertEqual(update_resp.status_code, 200)
        self.assertTrue(update_resp.json()["disabled"])

        # Delete
        delete_resp = self.client.delete(
            f"/v1/admin/api-keys/{key_id}",
            headers=self._master_headers(),
        )
        self.assertEqual(delete_resp.status_code, 200)
        self.assertTrue(delete_resp.json()["deleted"])

    def test_create_rejects_empty_name(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "   "},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_virtual_key_cannot_access_admin_api(self):
        # Seed a virtual key; use it for auth against admin endpoints.
        record = self.db.create(name="team-c")
        resp = self.client.get(
            "/v1/admin/api-keys",
            headers={"Authorization": f"Bearer {record.api_key}"},
        )
        # Middleware blocks the path before routing → 403.
        self.assertEqual(resp.status_code, 403)

    def test_missing_auth_returns_401(self):
        resp = self.client.get("/v1/admin/api-keys")
        self.assertEqual(resp.status_code, 401)

    def test_get_404_for_missing_id(self):
        resp = self.client.get("/v1/admin/api-keys/999", headers=self._master_headers())
        self.assertEqual(resp.status_code, 404)

    def test_update_can_clear_nullable_limits_and_metadata(self):
        record = self.db.create(
            name="team-d",
            budget_usd=10.0,
            rpm=30,
            tpm=300,
            metadata={"owner": "alice"},
        )

        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={
                "budget_usd": None,
                "rpm": None,
                "tpm": None,
                "metadata": None,
            },
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertIsNone(body["budget_usd"])
        self.assertIsNone(body["rpm"])
        self.assertIsNone(body["tpm"])
        self.assertEqual(body["metadata"], {})

    def test_create_with_budget_period_is_serialized(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "team-period", "budget_usd": 10.0, "budget_period": "daily"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 201)
        body = resp.json()
        self.assertEqual(body["budget_period"], "daily")
        self.assertIsNotNone(body["budget_reset_at"])

    def test_create_rejects_invalid_budget_period(self):
        resp = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "team-bad-period", "budget_period": "weekly"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 400)

    def test_update_budget_period_recomputes_reset(self):
        record = self.db.create(name="team-up", budget_usd=10.0)
        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"budget_period": "monthly"},
            headers=self._master_headers(),
        )
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        self.assertEqual(body["budget_period"], "monthly")
        self.assertIsNotNone(body["budget_reset_at"])

    def test_update_rejects_explicit_blank_name_before_accounting_service(self):
        record = self.db.create(name="team-name")

        for invalid_name in ("", "   \t\n"):
            with self.subTest(name=repr(invalid_name)):
                self.accounting_service.update_key.reset_mock()
                response = self.client.patch(
                    f"/v1/admin/api-keys/{record.id}",
                    json={"name": invalid_name},
                    headers=self._master_headers(),
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["detail"],
                    "API key name must not be empty",
                )
                self.accounting_service.update_key.assert_not_awaited()

    def test_update_rejects_invalid_budget_period_before_accounting_service(self):
        record = self.db.create(name="team-period-validation")

        for invalid_period in ("weekly", "  WEEKLY  "):
            with self.subTest(period=invalid_period):
                self.accounting_service.update_key.reset_mock()
                response = self.client.patch(
                    f"/v1/admin/api-keys/{record.id}",
                    json={"budget_period": invalid_period},
                    headers=self._master_headers(),
                )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["detail"],
                    "budget_period must be one of: none, daily, monthly",
                )
                self.accounting_service.update_key.assert_not_awaited()

    def test_update_preserves_name_and_budget_period_normalization(self):
        record = self.db.create(name="team-normalization", budget_period="daily")

        unchanged_name = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": None},
            headers=self._master_headers(),
        )
        normalized_name = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"name": "  team-renamed  "},
            headers=self._master_headers(),
        )

        self.assertEqual(unchanged_name.status_code, 200)
        self.assertEqual(unchanged_name.json()["name"], "team-normalization")
        self.assertEqual(normalized_name.status_code, 200)
        self.assertEqual(normalized_name.json()["name"], "team-renamed")

        for raw_period, expected_period in (
            (None, "none"),
            ("", "none"),
            ("   ", "none"),
            ("  DAILY  ", "daily"),
            ("Monthly", "monthly"),
        ):
            with self.subTest(period=raw_period):
                response = self.client.patch(
                    f"/v1/admin/api-keys/{record.id}",
                    json={"budget_period": raw_period},
                    headers=self._master_headers(),
                )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.json()["budget_period"], expected_period)

    def test_reset_spent_refreshes_usd_budget_ledger(self):
        record = self.db.create(name="team-e", budget_usd=10.0)
        ledger = self.app.state.services.usd_budget_ledger
        ledger.sync_record(record.id, budget_usd=10.0, spent_usd=10.0)
        self.assertFalse(ledger.reserve(record.id, 5.0))

        resp = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"reset_spent": True},
            headers=self._master_headers(),
        )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["spent_usd"], 0.0)
        self.assertTrue(ledger.reserve(record.id, 5.0))

    def test_delete_api_key_discards_usd_budget_ledger_entry(self):
        record = self.db.create(name="team-delete", budget_usd=10.0)
        services = self.app.state.services
        ledger = services.usd_budget_ledger
        ledger.sync_record(record.id, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve(record.id, 5.0))

        with patch.object(
            services.rate_limiter,
            "reset",
            wraps=services.rate_limiter.reset,
        ) as reset_rate_limit:
            resp = self.client.delete(
                f"/v1/admin/api-keys/{record.id}",
                headers=self._master_headers(),
            )

        self.assertEqual(resp.status_code, 200)
        reset_rate_limit.assert_called_once_with(record.id)
        self.assertEqual(ledger.reserved_for(record.id), 0.0)

    def test_update_resets_only_explicit_limits_or_spent(self):
        record = self.db.create(name="team-reset", budget_usd=10.0, rpm=10, tpm=100)
        services = self.app.state.services

        with (
            patch.object(
                services.rate_limiter,
                "reset",
                wraps=services.rate_limiter.reset,
            ) as reset_rate_limit,
            patch.object(
                services.usd_budget_ledger,
                "reset_record",
                wraps=services.usd_budget_ledger.reset_record,
            ) as reset_budget,
        ):
            unchanged = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"disabled": True},
                headers=self._master_headers(),
            )
            rpm_cleared = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"rpm": None},
                headers=self._master_headers(),
            )
            tpm_cleared = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"tpm": None},
                headers=self._master_headers(),
            )
            spent_reset = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"reset_spent": True},
                headers=self._master_headers(),
            )

        self.assertEqual(unchanged.status_code, 200)
        self.assertEqual(rpm_cleared.status_code, 200)
        self.assertEqual(tpm_cleared.status_code, 200)
        self.assertEqual(spent_reset.status_code, 200)
        self.assertEqual(
            reset_rate_limit.call_args_list,
            [call(record.id), call(record.id)],
        )
        reset_budget.assert_called_once_with(
            record.id,
            budget_usd=10.0,
            spent_usd=0.0,
        )

    def test_update_404_or_db_exception_does_not_reset_runtime_state(self):
        record = self.db.create(name="team-failure", budget_usd=10.0)
        services = self.app.state.services

        with (
            patch.object(
                services.accounting_service,
                "update_key",
                AsyncMock(return_value=None),
            ),
            patch.object(services.rate_limiter, "reset") as reset_rate_limit,
            patch.object(services.usd_budget_ledger, "reset_record") as reset_budget,
        ):
            response = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"rpm": None, "reset_spent": True},
                headers=self._master_headers(),
            )

        self.assertEqual(response.status_code, 404)
        reset_rate_limit.assert_not_called()
        reset_budget.assert_not_called()

        with (
            patch.object(
                services.accounting_service,
                "update_key",
                AsyncMock(side_effect=RuntimeError("database failed")),
            ),
            patch.object(services.rate_limiter, "reset") as reset_rate_limit,
            patch.object(services.usd_budget_ledger, "reset_record") as reset_budget,
            self.assertRaisesRegex(RuntimeError, "database failed"),
        ):
            self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={"rpm": None, "reset_spent": True},
                headers=self._master_headers(),
            )

        reset_rate_limit.assert_not_called()
        reset_budget.assert_not_called()

    def test_delete_404_or_db_exception_does_not_reset_runtime_state(self):
        record = self.db.create(name="team-delete-failure", budget_usd=10.0)
        services = self.app.state.services

        with (
            patch.object(
                services.accounting_service,
                "delete_key",
                AsyncMock(return_value=False),
            ),
            patch.object(services.rate_limiter, "reset") as reset_rate_limit,
            patch.object(services.usd_budget_ledger, "discard_record") as discard_budget,
        ):
            response = self.client.delete(
                f"/v1/admin/api-keys/{record.id}",
                headers=self._master_headers(),
            )

        self.assertEqual(response.status_code, 404)
        reset_rate_limit.assert_not_called()
        discard_budget.assert_not_called()

        with (
            patch.object(
                services.accounting_service,
                "delete_key",
                AsyncMock(side_effect=RuntimeError("database failed")),
            ),
            patch.object(services.rate_limiter, "reset") as reset_rate_limit,
            patch.object(services.usd_budget_ledger, "discard_record") as discard_budget,
            self.assertRaisesRegex(RuntimeError, "database failed"),
        ):
            self.client.delete(
                f"/v1/admin/api-keys/{record.id}",
                headers=self._master_headers(),
            )

        reset_rate_limit.assert_not_called()
        discard_budget.assert_not_called()

    def test_update_passes_all_explicit_fields_and_reset_in_one_service_call(self):
        record = self.db.create(name="team-combined", budget_usd=10.0)
        expected_changes = {
            "name": "team-updated",
            "budget_usd": None,
            "rpm": None,
            "tpm": None,
            "allowed_models": None,
            "disabled": True,
            "metadata": None,
            "budget_period": None,
        }

        with patch.object(
            self.accounting_service,
            "update_key",
            AsyncMock(return_value=record),
        ) as update_key:
            response = self.client.patch(
                f"/v1/admin/api-keys/{record.id}",
                json={**expected_changes, "reset_spent": True},
                headers=self._master_headers(),
            )

        self.assertEqual(response.status_code, 200)
        update_key.assert_awaited_once_with(
            record.id,
            changes=expected_changes,
            reset_spent=True,
        )

    def test_accounting_failures_use_safe_admin_http_mapping(self):
        record = self.db.create(name="team-errors")
        vectors = (
            (AccountingErrorCode.ACCOUNTING_IN_FLIGHT, 409, "accounting_in_flight"),
            (
                AccountingErrorCode.BUDGET_RESET_IN_PROGRESS,
                409,
                "budget_reset_in_progress",
            ),
            (AccountingErrorCode.SOURCE_WRITE_FAILED, 503, "accounting_unavailable"),
        )

        for code, status_code, public_code in vectors:
            with self.subTest(operation="update", code=code):
                self.accounting_service.update_key.reset_mock()
                self.accounting_service.update_key.side_effect = AccountingError(code)
                response = self.client.patch(
                    f"/v1/admin/api-keys/{record.id}",
                    json={"disabled": True},
                    headers=self._master_headers(),
                )
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["error"]["code"], public_code)
                self.assertNotEqual(response.json()["detail"], code.value)

            with self.subTest(operation="delete", code=code):
                self.accounting_service.delete_key.reset_mock()
                self.accounting_service.delete_key.side_effect = AccountingError(code)
                response = self.client.delete(
                    f"/v1/admin/api-keys/{record.id}",
                    headers=self._master_headers(),
                )
                self.assertEqual(response.status_code, status_code)
                self.assertEqual(response.json()["error"]["code"], public_code)
                self.assertNotEqual(response.json()["detail"], code.value)

        self.accounting_service.update_key.side_effect = None
        self.accounting_service.delete_key.side_effect = None

    def test_invalid_update_payload_does_not_call_accounting_service(self):
        record = self.db.create(name="team-invalid")
        self.accounting_service.update_key.reset_mock()

        response = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            content=json.dumps({"budget_usd": float("nan")}),
            headers={**self._master_headers(), "Content-Type": "application/json"},
        )

        self.assertEqual(response.status_code, 400)
        self.accounting_service.update_key.assert_not_awaited()

    def test_create_list_and_get_do_not_call_accounting_mutation_service(self):
        self.accounting_service.update_key.reset_mock()
        self.accounting_service.delete_key.reset_mock()

        created = self.client.post(
            "/v1/admin/api-keys",
            json={"name": "team-read-paths"},
            headers=self._master_headers(),
        )
        key_id = created.json()["id"]
        listed = self.client.get(
            "/v1/admin/api-keys",
            headers=self._master_headers(),
        )
        fetched = self.client.get(
            f"/v1/admin/api-keys/{key_id}",
            headers=self._master_headers(),
        )

        self.assertEqual(created.status_code, 201)
        self.assertEqual(listed.status_code, 200)
        self.assertEqual(fetched.status_code, 200)
        self.accounting_service.update_key.assert_not_awaited()
        self.accounting_service.delete_key.assert_not_awaited()

    def test_terminal_base_exception_from_service_is_not_mapped(self):
        record = self.db.create(name="team-terminal")
        primary = SystemExit("terminal accounting failure")
        scope = {
            "type": "http",
            "method": "PATCH",
            "path": f"/v1/admin/api-keys/{record.id}",
            "headers": [],
            "query_string": b"",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 12345),
            "scheme": "http",
            "root_path": "",
            "app": self.app,
            "state": {"api_key_role": "master"},
        }
        request = Request(scope)

        with patch.object(
            self.accounting_service,
            "update_key",
            AsyncMock(side_effect=primary),
        ):
            with self.assertRaises(SystemExit) as raised:
                run_async(
                    update_api_key(
                        request,
                        record.id,
                        ApiKeyUpdatePayload(disabled=True),
                    )
                )

        self.assertIs(raised.exception, primary)

    def test_admin_mutation_handlers_have_no_direct_storage_or_cache_owner(self):
        # PATCH only does the If-Match precondition itself and hands the
        # mutation to _apply_update, so the contract covers that helper too.
        source = (
            inspect.getsource(update_api_key)
            + inspect.getsource(_apply_update)
            + inspect.getsource(delete_api_key)
        )

        self.assertNotIn("api_keys_db", source)
        self.assertNotIn("rate_limiter", source)
        self.assertNotIn("usd_budget_ledger", source)
        self.assertIn("accounting_service.update_key", source)
        self.assertIn("accounting_service.delete_key", source)

    def test_container_dependencies_win_over_conflicting_legacy_aliases(self):
        record = self.db.create(name="team-container", rpm=10)
        self.app.state.api_keys_db = object()
        self.app.state.rate_limiter = object()
        self.app.state.usd_budget_ledger = object()
        self.app.state.accounting_service = object()

        response = self.client.patch(
            f"/v1/admin/api-keys/{record.id}",
            json={"rpm": None, "reset_spent": True},
            headers=self._master_headers(),
        )

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["rpm"])


if __name__ == "__main__":
    unittest.main()
