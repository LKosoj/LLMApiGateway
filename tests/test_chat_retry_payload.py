import copy
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _valid_completion_response(response_id: str, content: str = "ok") -> dict:
    # A bare {"id": ...} response has no "choices" and would itself be flagged
    # as an empty_completion by the degenerate-response detector (Package D),
    # so "success" mocks in this file must be choices-shaped.
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


class ChatRetryPayloadTests(unittest.TestCase):
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
    def test_retry_attempts_reuse_clean_payload_copies(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
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
                        "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(
            _client, _target_url, _headers, payload, _is_streaming, **_kwargs
        ):
            seen_payloads.append(copy.deepcopy(payload))
            if len(seen_payloads) == 1:
                return None, "temporary failure"
            return _valid_completion_response("retry-success"), None

        make_llm_request_mock.side_effect = fake_make_llm_request

        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hello"}],
            "metadata": {"trace_id": "abc-123"},
        }
        original_payload = copy.deepcopy(request_payload)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("retry-success"))
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 2)
        self.assertEqual(seen_payloads[0], seen_payloads[1])
        self.assertEqual(seen_payloads[0]["messages"], original_payload["messages"])
        self.assertEqual(seen_payloads[0]["metadata"], original_payload["metadata"])
        self.assertEqual(seen_payloads[0]["model"], "provider-model")
        self.assertNotIn("provider", seen_payloads[0])
        self.assertNotIn("allow_fallbacks", seen_payloads[0])

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_null_response_format_is_omitted_from_provider_payload(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
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
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(
            _client, _target_url, _headers, payload, _is_streaming, **_kwargs
        ):
            seen_payloads.append(copy.deepcopy(payload))
            return _valid_completion_response("success"), None

        make_llm_request_mock.side_effect = fake_make_llm_request

        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hello"}],
            "response_format": None,
            "temperature": 0.2,
        }
        original_payload = copy.deepcopy(request_payload)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("success"))
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 1)
        self.assertNotIn("response_format", seen_payloads[0])
        self.assertEqual(seen_payloads[0]["temperature"], 0.2)
        self.assertEqual(seen_payloads[0]["model"], "provider-model")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_parameterless_function_tools_receive_empty_schema(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
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
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(
            _client, _target_url, _headers, payload, _is_streaming, **_kwargs
        ):
            seen_payloads.append(copy.deepcopy(payload))
            return _valid_completion_response("success"), None

        make_llm_request_mock.side_effect = fake_make_llm_request

        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "list files"}],
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "list_directory",
                        "description": "List directory contents.",
                    },
                }
            ],
        }
        original_payload = copy.deepcopy(request_payload)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("success"))
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 1)
        tool_payload = seen_payloads[0]["tools"][0]["function"]
        self.assertEqual(tool_payload["name"], "list_directory")
        self.assertEqual(
            tool_payload["parameters"],
            {"type": "object", "properties": {}},
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_minimax_models_merge_multiple_system_messages(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
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
                        "model": "mm.MiniMax-M2.7",
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(
            _client, _target_url, _headers, payload, _is_streaming, **_kwargs
        ):
            seen_payloads.append(copy.deepcopy(payload))
            return _valid_completion_response("success"), None

        make_llm_request_mock.side_effect = fake_make_llm_request

        request_payload = {
            "model": "gateway-model",
            "messages": [
                {"role": "system", "content": "System A"},
                {"role": "system", "content": "System B"},
                {"role": "user", "content": "hello"},
            ],
        }
        original_payload = copy.deepcopy(request_payload)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("success"))
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(
            seen_payloads[0]["messages"],
            [
                {"role": "system", "content": "System A\n\nSystem B"},
                {"role": "user", "content": "hello"},
            ],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_non_minimax_models_keep_multiple_system_messages(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
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
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(
            _client, _target_url, _headers, payload, _is_streaming, **_kwargs
        ):
            seen_payloads.append(copy.deepcopy(payload))
            return _valid_completion_response("success"), None

        make_llm_request_mock.side_effect = fake_make_llm_request

        request_payload = {
            "model": "gateway-model",
            "messages": [
                {"role": "system", "content": "System A"},
                {"role": "system", "content": "System B"},
                {"role": "user", "content": "hello"},
            ],
        }
        original_payload = copy.deepcopy(request_payload)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("success"))
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 1)
        self.assertEqual(seen_payloads[0]["messages"], original_payload["messages"])


if __name__ == "__main__":
    unittest.main()
