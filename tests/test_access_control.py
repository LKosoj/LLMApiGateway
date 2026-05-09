"""Unit tests for ``enforce_virtual_key_access``."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from fastapi import HTTPException

from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.services.access_control import UsdBudgetLedger, enforce_virtual_key_access
from llm_gateway_core.services.rate_limiter import RateLimiter


def _make_request(record: ApiKeyRecord | None, *, rate_limiter=None):
    request = SimpleNamespace()
    request.state = SimpleNamespace(api_key_record=record)
    request.app = SimpleNamespace()
    request.app.state = SimpleNamespace(rate_limiter=rate_limiter)
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


class UsdBudgetLedgerTests(unittest.TestCase):
    def test_reset_record_allows_budget_after_spent_reset(self):
        ledger = UsdBudgetLedger(default_estimate_usd=5.0)
        ledger.sync_record(1, budget_usd=10.0, spent_usd=10.0)
        self.assertFalse(ledger.reserve(1, 5.0))

        ledger.reset_record(1, budget_usd=10.0, spent_usd=0.0)

        self.assertTrue(ledger.reserve(1, 5.0))


if __name__ == "__main__":
    unittest.main()
