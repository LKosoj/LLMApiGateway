import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.services.request_handler import (
    DEFAULT_RETRY_COUNT,
    DEFAULT_RETRY_DELAY_SECONDS,
    normalize_retry_settings,
)
from tests.chat_accounting_test_support import install_main_chat_accounting_double


class RetrySettingsTests(unittest.TestCase):
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

    def test_normalize_retry_settings_uses_safe_defaults_for_nulls(self):
        retry_count, retry_delay = normalize_retry_settings(None, None)

        self.assertEqual(retry_count, DEFAULT_RETRY_COUNT)
        self.assertEqual(retry_delay, DEFAULT_RETRY_DELAY_SECONDS)

    def test_normalize_retry_settings_clamps_negative_values(self):
        retry_count, retry_delay = normalize_retry_settings(-3, -1)

        self.assertEqual(retry_count, DEFAULT_RETRY_COUNT)
        self.assertEqual(retry_delay, DEFAULT_RETRY_DELAY_SECONDS)

    @patch("llm_gateway_core.api.v1.chat.asyncio")
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_chat_completions_handles_null_retry_delay_without_crash(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        asyncio_mock,
    ):
        sleep_mock = AsyncMock()
        asyncio_mock.sleep = sleep_mock
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
            )
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "retry_count": 1,
                        "retry_delay": None,
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_operation_rules.return_value = {}
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "temporary failure"),
            ({"id": "retry-success"}, None),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "retry-success"})
        self.assertEqual(make_llm_request_mock.await_count, 2)
        sleep_mock.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
