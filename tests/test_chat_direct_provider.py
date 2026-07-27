"""Coverage for the master-only ``X-LLMGateway-Provider`` header.

The Playground Chat tab uses it to talk to a provider model that no gateway
rule references: the model id in the body is sent straight to the named
provider, skipping model policy, Fusion/Router rules and the fallback chain.
"""
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _valid_completion_response(response_id: str, content: str = "ok") -> dict:
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


class FakeApiKeysDB:
    def __init__(self, record):
        self.record = record

    @property
    def db_path(self):
        return main.resolve_db_dir() / "tokens_usage.db"

    def get_by_key(self, api_key):
        return self.record if api_key == self.record.api_key else None

    def get_by_id(self, key_id):
        return self.record if key_id == self.record.id else None

    def reset_due_budgets(self):
        return []

    def record_spent(self, key_id, cost):
        return None


def _fake_config_loader() -> Mock:
    config_loader = Mock()
    config_loader.providers_config = {
        "routed-provider": SimpleNamespace(
            baseUrl="https://routed.example",
            apikey="DIRECT-KEY",
        ),
        "direct-provider": SimpleNamespace(
            baseUrl="https://direct.example",
            apikey="DIRECT-KEY",
        ),
    }
    config_loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "routed-provider",
                    "model": "routed-model",
                    "use_provider_order_as_fallback": False,
                }
            ],
            "rotate_models": False,
        }
    }
    config_loader.model_rules = {}
    config_loader.fusion_rules = {}
    config_loader.router_rules = {}
    config_loader.load_providers.return_value = config_loader.providers_config
    config_loader.load_fallback_rules.return_value = config_loader.fallback_rules
    config_loader.load_complete.return_value = config_loader
    return config_loader


class ChatDirectProviderTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        self.accounting_service = install_main_chat_accounting_double(
            self._accounting_stack,
        )
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        patchers = (
            patch.object(main.AtomicConfigFileTransaction, "recover_pending"),
            patch(
                "llm_gateway_core.services.runtime_candidate."
                "build_operation_cost_calculator_registry",
                return_value={},
            ),
            patch(
                "main.ConfigUpdateCoordinator",
                return_value=config_update_coordinator,
            ),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_header_sends_model_to_named_provider_without_fallback_rule(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_valid_completion_response("direct-success"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch.object(main.settings, "fallback_provider", "routed-provider"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "provider-only-model",
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                        headers={
                            "Authorization": "Bearer test-gateway-key",
                            "X-LLMGateway-Provider": "direct-provider",
                        },
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("direct-success"))
        self.assertEqual(
            make_llm_request_mock.await_args.args[1],
            "https://direct.example/chat/completions",
        )
        self.assertEqual(
            make_llm_request_mock.await_args.args[3]["model"],
            "provider-only-model",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_header_overrides_the_configured_fallback_chain(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_valid_completion_response("pinned"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={
                        "Authorization": "Bearer test-gateway-key",
                        "X-LLMGateway-Provider": "direct-provider",
                    },
                )

        self.assertEqual(response.status_code, 200)
        # The rule for "gateway-model" points at routed-provider/routed-model;
        # the pinned provider wins and the model name is used verbatim.
        self.assertEqual(
            make_llm_request_mock.await_args.args[1],
            "https://direct.example/chat/completions",
        )
        self.assertEqual(
            make_llm_request_mock.await_args.args[3]["model"],
            "gateway-model",
        )
        self.assertEqual(make_llm_request_mock.await_count, 1)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_unknown_provider_is_rejected_with_404(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "provider-only-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={
                        "Authorization": "Bearer test-gateway-key",
                        "X-LLMGateway-Provider": "missing-provider",
                    },
                )

        self.assertEqual(response.status_code, 404)
        make_llm_request_mock.assert_not_awaited()

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_virtual_key_may_not_pin_a_provider(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        record = ApiKeyRecord(
            id=321,
            name="virtual-key",
            api_key="lgk_virtual",
            budget_usd=None,
            spent_usd=0.0,
            rpm=None,
            tpm=None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch("main.ApiKeysDB", return_value=FakeApiKeysDB(record)):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={
                            "model": "gateway-model",
                            "messages": [{"role": "user", "content": "hello"}],
                        },
                        headers={
                            "Authorization": "Bearer lgk_virtual",
                            "X-LLMGateway-Provider": "direct-provider",
                        },
                    )

        self.assertEqual(response.status_code, 403)
        make_llm_request_mock.assert_not_awaited()


class PlaygroundProviderCatalogTests(unittest.TestCase):
    """The Chat tab needs provider names to offer the provider-model source."""

    def test_playground_models_expose_sorted_provider_names(self):
        from llm_gateway_core.api.v1.rules_editor import _build_playground_models

        config_loader = SimpleNamespace(
            operation_rules={},
            fallback_rules={"llmgateway/light": {}},
            fusion_rules={},
            router_rules={},
            providers_config={"zeta": object(), "alpha": object()},
        )

        models = _build_playground_models(config_loader)

        self.assertEqual(models["providers"], ["alpha", "zeta"])
        self.assertEqual(models["chat"], ["llmgateway/light"])

    def test_playground_models_without_providers_yield_empty_list(self):
        from llm_gateway_core.api.v1.rules_editor import _build_playground_models

        config_loader = SimpleNamespace(
            operation_rules={},
            fallback_rules={},
            fusion_rules={},
            router_rules={},
            providers_config={},
        )

        self.assertEqual(_build_playground_models(config_loader)["providers"], [])


if __name__ == "__main__":
    unittest.main()
