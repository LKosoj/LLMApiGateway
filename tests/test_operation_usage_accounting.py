"""Regression tests for operation usage side effects."""

from __future__ import annotations

import unittest
from types import SimpleNamespace

from llm_gateway_core.api.v1.operation_proxy import record_operation_usage
from tests._async_compat import run_async


class _TokensUsageDB:
    def __init__(self) -> None:
        self.rows: list[dict] = []

    def insert_usage(self, row: dict) -> None:
        self.rows.append(dict(row))


class _FailingTokensUsageDB:
    def insert_usage(self, _row: dict) -> None:
        raise RuntimeError("usage db down")


class _ApiKeysDB:
    def __init__(self) -> None:
        self.spent: list[tuple[int, float]] = []

    def record_spent(self, key_id: int, cost_usd: float) -> None:
        self.spent.append((key_id, cost_usd))


class _RateLimiter:
    def __init__(self) -> None:
        self.tokens: list[tuple[int, int]] = []

    def add_tokens(self, key_id: int, tokens: int, *, tpm_limit=None) -> None:
        self.tokens.append((key_id, tokens))


class _Ledger:
    def __init__(self) -> None:
        self.commits: list[tuple[int, float, float | None]] = []

    def commit_reserved(self, key_id: int, actual: float, *, reserved: float | None) -> None:
        self.commits.append((key_id, actual, reserved))


class OperationUsageAccountingTests(unittest.TestCase):
    def test_record_operation_usage_updates_budget_and_tpm_side_effects(self):
        tokens_usage_db = _TokensUsageDB()
        api_keys_db = _ApiKeysDB()
        rate_limiter = _RateLimiter()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    tokens_usage_db=tokens_usage_db,
                    api_keys_db=api_keys_db,
                    rate_limiter=rate_limiter,
                )
            ),
            state=SimpleNamespace(
                api_key_id=7,
                llmgateway_provider="provider-a",
                llmgateway_provider_model="provider-model",
                llmgateway_gateway_model="gateway-model",
                llmgateway_operation="embeddings",
            ),
        )

        run_async(
            record_operation_usage(
                request,
                {
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                        "cost": 0.25,
                    }
                },
                gateway_model="gateway-model",
                operation="embeddings",
            )
        )

        self.assertEqual(len(tokens_usage_db.rows), 1)
        self.assertEqual(tokens_usage_db.rows[0]["api_key_id"], 7)
        self.assertEqual(api_keys_db.spent, [(7, 0.25)])
        self.assertEqual(rate_limiter.tokens, [(7, 5)])

    def test_record_operation_usage_preserves_accounting_when_usage_insert_fails(self):
        api_keys_db = _ApiKeysDB()
        rate_limiter = _RateLimiter()
        ledger = _Ledger()
        request = SimpleNamespace(
            app=SimpleNamespace(
                state=SimpleNamespace(
                    tokens_usage_db=_FailingTokensUsageDB(),
                    api_keys_db=api_keys_db,
                    rate_limiter=rate_limiter,
                    usd_budget_ledger=ledger,
                )
            ),
            state=SimpleNamespace(
                api_key_id=7,
                llmgateway_provider="provider-a",
                llmgateway_provider_model="provider-model",
                llmgateway_gateway_model="gateway-model",
                llmgateway_operation="embeddings",
                usd_budget_reserved=True,
                usd_budget_finalized=False,
                usd_budget_reserved_key_id=7,
                usd_budget_reserved_estimate=1.5,
            ),
        )

        run_async(
            record_operation_usage(
                request,
                {
                    "usage": {
                        "prompt_tokens": 2,
                        "completion_tokens": 3,
                        "total_tokens": 5,
                        "cost": 0.25,
                    }
                },
                gateway_model="gateway-model",
                operation="embeddings",
            )
        )

        self.assertEqual(api_keys_db.spent, [(7, 0.25)])
        self.assertEqual(ledger.commits, [(7, 0.25, 1.5)])
        self.assertTrue(request.state.usd_budget_finalized)
        self.assertEqual(rate_limiter.tokens, [(7, 5)])


if __name__ == "__main__":
    unittest.main()
