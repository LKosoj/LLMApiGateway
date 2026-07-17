"""Unit tests for ``enforce_virtual_key_access``."""

from __future__ import annotations

import math
import threading
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from fastapi import FastAPI, HTTPException, Request

from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.services.accounting import AccountingValidationError
from llm_gateway_core.services.access_control import UsdBudgetLedger, enforce_virtual_key_access
from llm_gateway_core.services.rate_limiter import RateLimiter
from tests.runtime_test_support import bind_app_services


def _request_for_app(app: FastAPI, record: ApiKeyRecord) -> Request:
    request = Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.3"},
            "http_version": "1.1",
            "server": ("testserver", 80),
            "client": ("127.0.0.1", 1234),
            "scheme": "http",
            "method": "POST",
            "root_path": "",
            "path": "/v1/chat/completions",
            "raw_path": b"/v1/chat/completions",
            "query_string": b"",
            "headers": [],
            "app": app,
            "state": {},
        }
    )
    request.state.api_key_record = record
    request.state.llmgateway_request_id = None
    request.state.llmgateway_active_request_id = None
    request.state.api_key_id = record.id
    request.state.gateway_auth_source = None
    return request


def _make_request(
    record: ApiKeyRecord,
    *,
    rate_limiter: RateLimiter | None = None,
    rejections_db: RejectionsDB | None = None,
) -> Request:
    app = FastAPI()
    overrides: dict[str, object] = {}
    if rate_limiter is not None:
        overrides["rate_limiter"] = rate_limiter
    if rejections_db is not None:
        overrides["rejections_db"] = rejections_db
    bind_app_services(app, **overrides)
    return _request_for_app(app, record)


def _make_record(**kwargs) -> ApiKeyRecord:
    defaults = dict(
        id=1,
        name="k",
        api_key="lgk_test",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        allowed_models=[],
        disabled=False,
        metadata={},
        created_at="",
        last_used_at=None,
    )
    defaults.update(kwargs)
    return ApiKeyRecord(**defaults)


class EnforceVirtualKeyAccessTests(unittest.TestCase):
    def test_caller_without_api_key_record_passes_through_without_app(self):
        request = SimpleNamespace(state=SimpleNamespace())
        enforce_virtual_key_access(request, "any-model")  # should not raise

    def test_master_caller_passes_through_without_app(self):
        request = SimpleNamespace(state=SimpleNamespace(api_key_record=None))
        enforce_virtual_key_access(request, "any-model")  # should not raise

    def test_disabled_key_is_rejected(self):
        record = _make_record(disabled=True)
        request = _make_request(record)
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request, "gpt-4o")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_disallowed_model_is_rejected(self):
        record = _make_record(allowed_models=["gpt-4o"])
        request = _make_request(record)
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request, "claude-3")
        self.assertEqual(ctx.exception.status_code, 403)

    def test_allowed_model_passes(self):
        record = _make_record(allowed_models=["gpt-4o"])
        request = _make_request(record)
        enforce_virtual_key_access(request, "gpt-4o")

    def test_budget_is_owned_by_accounting_admission(self):
        record = _make_record(budget_usd=1.0, spent_usd=1.0)
        request = _make_request(record)
        enforce_virtual_key_access(request, "gpt-4o")

    def test_negative_budget_is_unlimited(self):
        record = _make_record(budget_usd=-1.0, spent_usd=9999.0)
        request = _make_request(record)
        enforce_virtual_key_access(request, "gpt-4o")

    def test_rate_limits_are_owned_by_accounting_admission(self):
        record = _make_record(rpm=1)
        rate_limiter = MagicMock(spec=RateLimiter)
        request = _make_request(record, rate_limiter=rate_limiter)
        enforce_virtual_key_access(request, "gpt-4o")
        enforce_virtual_key_access(request, "gpt-4o")
        rate_limiter.try_acquire.assert_not_called()

    def test_allowed_request_does_not_read_runtime_services(self):
        record = _make_record()
        request = SimpleNamespace(state=SimpleNamespace(api_key_record=record))

        enforce_virtual_key_access(request, "gpt-4o")


class EnforceVirtualKeyAccessRejectionTests(unittest.TestCase):
    """Verify that enforce_virtual_key_access records rejections via insert_rejection."""

    def _mock_db(self):
        return MagicMock(spec=RejectionsDB)

    def test_disabled_key_records_key_disabled_category(self):
        mock_db = self._mock_db()
        record = _make_record(disabled=True)
        request = _make_request(record, rejections_db=mock_db)
        with self.assertRaises(HTTPException):
            enforce_virtual_key_access(request, "gpt-4o")
        mock_db.insert_rejection.assert_called_once()
        self.assertEqual(mock_db.insert_rejection.call_args.kwargs["category"], "key_disabled")

    def test_model_not_allowed_records_model_not_allowed_category(self):
        mock_db = self._mock_db()
        record = _make_record(allowed_models=["gpt-4o"])
        request = _make_request(record, rejections_db=mock_db)
        with self.assertRaises(HTTPException):
            enforce_virtual_key_access(request, "claude-3")
        mock_db.insert_rejection.assert_called_once()
        self.assertEqual(mock_db.insert_rejection.call_args.kwargs["category"], "model_not_allowed")

class UsdBudgetLedgerTests(unittest.TestCase):
    def test_reset_record_allows_budget_after_spent_reset(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=10.0)
        self.assertFalse(ledger.reserve(1, 5.0))

        ledger.reset_record(1, budget_usd=10.0, spent_usd=0.0)

        self.assertTrue(ledger.reserve(1, 5.0))

    def test_release_uses_matching_reservation_amount(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve(1, 7.0))
        self.assertTrue(ledger.reserve(1, 2.0))

        ledger.release(1, 2.0)

        self.assertEqual(ledger.reserved_for(1), 7.0)

    def test_discard_record_removes_reserved_budget(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve(1, 5.0))

        ledger.discard_record(1)

        self.assertEqual(ledger.reserved_for(1), 0.0)

    def test_request_reservations_with_equal_estimates_are_independent(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 4.0))
        self.assertTrue(ledger.reserve_request("request-b", 1, 4.0))

        self.assertTrue(ledger.commit_request("request-b", 1, 3.0))
        self.assertEqual(ledger.reserved_for(1), 4.0)
        self.assertTrue(ledger.release_request("request-a", 1))

        self.assertEqual(ledger.reserved_for(1), 0.0)

    def test_request_terminal_operations_are_exactly_once(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 4.0))

        self.assertTrue(ledger.commit_request("request-a", 1, 3.0))
        self.assertFalse(ledger.commit_request("request-a", 1, 3.0))
        self.assertFalse(ledger.release_request("request-a", 1))

        self.assertTrue(ledger.reserve_request("request-b", 1, 7.0))
        self.assertFalse(ledger.reserve_request("request-c", 1, 0.01))

    def test_rejected_request_reservation_does_not_mutate_ledger(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 8.0))

        self.assertFalse(ledger.reserve_request("request-b", 1, 3.0))
        self.assertEqual(ledger.reserved_for(1), 8.0)
        self.assertFalse(ledger.release_request("request-b", 1))
        self.assertEqual(ledger.reserved_for(1), 8.0)

    def test_duplicate_request_reservation_does_not_double_reserve(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)

        self.assertTrue(ledger.reserve_request("request-a", 1, 6.0))
        self.assertTrue(ledger.reserve_request("request-a", 1, 6.0))

        self.assertEqual(ledger.reserved_for(1), 6.0)

    def test_duplicate_request_reservation_rejects_conflicting_estimate(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 1))
        self.assertTrue(ledger.reserve_request("request-a", 1, 1.0))

        with self.assertRaises(AccountingValidationError):
            ledger.reserve_request("request-a", 1, 1.01)

        self.assertEqual(ledger.reserved_for(1), 1.0)

    def test_request_usd_values_are_strict_finite_non_negative_numbers(self):
        invalid_values = (True, -1, math.nan, math.inf, "1")
        for index, invalid in enumerate(invalid_values):
            with self.subTest(invalid=invalid):
                ledger = UsdBudgetLedger(default_estimate_usd=5.0)
                ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
                with self.assertRaises(AccountingValidationError):
                    ledger.reserve_request(f"request-{index}", 1, invalid)
                self.assertEqual(ledger.reserved_for(1), 0.0)

                self.assertTrue(ledger.reserve_request("live", 1, 1.0))
                with self.assertRaises(AccountingValidationError):
                    ledger.commit_request("live", 1, invalid)
                self.assertEqual(ledger.reserved_for(1), 1.0)
                self.assertTrue(ledger.commit_request("live", 1, 0.0))

    def test_missing_or_terminal_commit_does_not_validate_irrelevant_actual(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)

        self.assertFalse(ledger.commit_request("missing", 1, math.nan))
        self.assertTrue(ledger.reserve_request("request-a", 1, 1.0))
        self.assertTrue(ledger.commit_request("request-a", 1, 1.0))
        self.assertFalse(ledger.commit_request("request-a", 1, math.nan))

    def test_request_arithmetic_overflow_does_not_mutate_ledger(self):
        maximum = float.fromhex("0x1.fffffffffffffp+1023")
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=None, spent_usd=maximum)
        self.assertTrue(ledger.reserve_request("request-a", 1, 1.0))

        with self.assertRaises(AccountingValidationError):
            ledger.commit_request("request-a", 1, maximum)

        self.assertEqual(ledger.reserved_for(1), 1.0)
        self.assertTrue(ledger.release_request("request-a", 1))

        ledger.reset_record(1, budget_usd=None, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-b", 1, maximum))
        with self.assertRaises(AccountingValidationError):
            ledger.reserve_request("request-c", 1, maximum)
        self.assertEqual(ledger.reserved_for(1), maximum)
        self.assertFalse(ledger.release_request("request-c", 1))

    def test_concurrent_request_reservations_do_not_oversubscribe_budget(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        barrier = threading.Barrier(3)
        results: list[bool | None] = [None, None]

        def reserve(index: int) -> None:
            barrier.wait()
            results[index] = ledger.reserve_request(f"request-{index}", 1, 6.0)

        threads = [threading.Thread(target=reserve, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        barrier.wait()
        for thread in threads:
            thread.join()

        self.assertCountEqual(results, [True, False])
        self.assertEqual(ledger.reserved_for(1), 6.0)

    def test_reverse_order_request_releases_leave_zero_reserved_total(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 0.1))
        self.assertTrue(ledger.reserve_request("request-b", 1, 0.7))

        self.assertTrue(ledger.release_request("request-b", 1))
        self.assertEqual(ledger.reserved_for(1), 0.1)
        self.assertTrue(ledger.release_request("request-a", 1))
        self.assertEqual(ledger.reserved_for(1), 0.0)

    def test_reverse_order_request_commits_leave_zero_reserved_total(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 0.1))
        self.assertTrue(ledger.reserve_request("request-b", 1, 0.7))

        self.assertTrue(ledger.commit_request("request-b", 1, 0.0))
        self.assertEqual(ledger.reserved_for(1), 0.1)
        self.assertTrue(ledger.commit_request("request-a", 1, 0.0))
        self.assertEqual(ledger.reserved_for(1), 0.0)

    def test_release_sum_failure_keeps_request_reservation_live(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=0.0)
        self.assertTrue(ledger.reserve_request("request-a", 1, 0.1))
        self.assertTrue(ledger.reserve_request("request-b", 1, 0.7))

        with (
            patch(
                "llm_gateway_core.services.access_control.math.fsum",
                side_effect=OverflowError,
            ),
            self.assertRaises(AccountingValidationError),
        ):
            ledger.release_request("request-b", 1)

        self.assertTrue(ledger.release_request("request-b", 1))
        self.assertTrue(ledger.release_request("request-a", 1))


if __name__ == "__main__":
    unittest.main()
