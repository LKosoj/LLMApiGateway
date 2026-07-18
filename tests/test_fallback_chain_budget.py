"""Tests for chain-wide attempt budget (max_total_attempts)."""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _build_fake_config_loader(fallback_rule: dict, model: str = "gateway-model") -> Mock:
    fake_config_loader = Mock()
    fake_config_loader.configured_paths = {}
    fake_config_loader.operation_rules = {}
    fake_config_loader.providers_config = {
        "provider-a": SimpleNamespace(baseUrl="https://a.example", apikey="K"),
        "provider-b": SimpleNamespace(baseUrl="https://b.example", apikey="K"),
    }
    fake_config_loader.fallback_rules = {model: fallback_rule}
    fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
    fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
    fake_config_loader.load_operation_rules.return_value = {}
    fake_config_loader.load_complete.return_value = fake_config_loader
    return fake_config_loader


class FallbackChainBudgetTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        self.accounting_service = install_main_chat_accounting_double(
            self._accounting_stack
        )
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        coordinator_patcher = patch(
            "main.ConfigUpdateCoordinator",
            return_value=config_update_coordinator,
        )
        coordinator_patcher.start()
        self.addCleanup(coordinator_patcher.stop)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_without_budget_each_model_retries_independently(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader(
            {
                "fallback_models": [
                    {
                        "provider": "provider-a", "model": "m-a",
                        "retry_count": 2, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "provider-b", "model": "m-b",
                        "retry_count": 2, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": False,
            }
        )
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # Fail everything; expect 3 attempts on model-a (1 initial + 2 retries),
        # then 3 attempts on model-b = 6 total.
        make_llm_request_mock.return_value = (None, "boom")

        with patch.object(main.settings, "gateway_api_key", "k"):
            with TestClient(main.app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer k"},
                )

        self.assertEqual(resp.status_code, 503)
        self.assertEqual(make_llm_request_mock.await_count, 6)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_budget_caps_total_attempts_across_chain(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader(
            {
                "fallback_models": [
                    {
                        "provider": "provider-a", "model": "m-a",
                        "retry_count": 5, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "provider-b", "model": "m-b",
                        "retry_count": 5, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": False,
                "max_total_attempts": 3,
            }
        )
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (None, "boom")

        with patch.object(main.settings, "gateway_api_key", "k"):
            with TestClient(main.app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer k"},
                )

        self.assertEqual(resp.status_code, 503)
        # Budget of 3 must be respected across both models.
        self.assertEqual(make_llm_request_mock.await_count, 3)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_budget_allows_second_model_when_first_exhausts_retries_partially(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader(
            {
                "fallback_models": [
                    {
                        "provider": "provider-a", "model": "m-a",
                        "retry_count": 1, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "provider-b", "model": "m-b",
                        "retry_count": 1, "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": False,
                "max_total_attempts": 3,
            }
        )
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # First two attempts (model-a, initial + retry) fail; third attempt
        # (model-b, initial) should fit within the budget=3 and succeed.
        make_llm_request_mock.side_effect = [
            (None, "fail-1"),
            (None, "fail-2"),
            (
                {
                    "id": "ok",
                    "choices": [
                        {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                    ],
                },
                None,
            ),
        ]

        with patch.object(main.settings, "gateway_api_key", "k"):
            with TestClient(main.app) as client:
                resp = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hi"}]},
                    headers={"Authorization": "Bearer k"},
                )

        self.assertEqual(resp.status_code, 200)
        self.assertEqual(
            resp.json(),
            {
                "id": "ok",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
            },
        )
        self.assertEqual(make_llm_request_mock.await_count, 3)


if __name__ == "__main__":
    unittest.main()
