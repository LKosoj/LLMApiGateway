"""Unit tests for ``enforce_virtual_key_access``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock

from fastapi import HTTPException

from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.db.rejections_db import RejectionsDB
from llm_gateway_core.services.access_control import UsdBudgetLedger, enforce_virtual_key_access
from llm_gateway_core.services.rate_limiter import RateLimiter


def _make_request(record: ApiKeyRecord | None, *, rate_limiter=None, rejections_db=None):
    request = SimpleNamespace()
    request.state = SimpleNamespace(
        api_key_record=record,
        llmgateway_request_id=None,
        llmgateway_active_request_id=None,
        api_key_id=getattr(record, "id", None) if record else None,
        gateway_auth_source=None,
    )
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace(rate_limiter=rate_limiter, rejections_db=rejections_db)
    request.client = SimpleNamespace(host="127.0.0.1")
    request.url = SimpleNamespace(path="/v1/chat/completions")
    request.method = "POST"
    return request


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
    def test_master_caller_passes_through(self):
        request = _make_request(None)
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

    def test_budget_exhausted_returns_429(self):
        record = _make_record(budget_usd=1.0, spent_usd=1.0)
        request = _make_request(record)
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request, "gpt-4o")
        self.assertEqual(ctx.exception.status_code, 429)

    def test_negative_budget_is_unlimited(self):
        record = _make_record(budget_usd=-1.0, spent_usd=9999.0)
        request = _make_request(record)
        enforce_virtual_key_access(request, "gpt-4o")

    def test_rpm_limit_raises_429(self):
        record = _make_record(rpm=1)
        rate_limiter = RateLimiter()
        request = _make_request(record, rate_limiter=rate_limiter)
        enforce_virtual_key_access(request, "gpt-4o")
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request, "gpt-4o")
        self.assertEqual(ctx.exception.status_code, 429)


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

    def test_budget_exhausted_records_budget_exhausted_category(self):
        mock_db = self._mock_db()
        record = _make_record(budget_usd=1.0, spent_usd=1.0)
        request = _make_request(record, rejections_db=mock_db)
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request, "gpt-4o")
        self.assertEqual(ctx.exception.status_code, 429)
        mock_db.insert_rejection.assert_called_once()
        self.assertEqual(mock_db.insert_rejection.call_args.kwargs["category"], "budget_exhausted")

    def test_rate_limited_records_rate_limited_category(self):
        mock_db = self._mock_db()
        record = _make_record(rpm=1)
        rate_limiter = RateLimiter()
        request1 = _make_request(record, rate_limiter=rate_limiter, rejections_db=mock_db)
        enforce_virtual_key_access(request1, "gpt-4o")  # first call passes

        request2 = _make_request(record, rate_limiter=rate_limiter, rejections_db=mock_db)
        with self.assertRaises(HTTPException) as ctx:
            enforce_virtual_key_access(request2, "gpt-4o")
        self.assertEqual(ctx.exception.status_code, 429)
        mock_db.insert_rejection.assert_called_once()
        self.assertEqual(mock_db.insert_rejection.call_args.kwargs["category"], "rate_limited")


class UsdBudgetLedgerTests(unittest.TestCase):
    def test_reset_record_allows_budget_after_spent_reset(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=10.0)
        self.assertFalse(ledger.reserve(1, 5.0))

        ledger.reset_record(1, budget_usd=10.0, spent_usd=0.0)

        self.assertTrue(ledger.reserve(1, 5.0))


if __name__ == "__main__":
    unittest.main()
