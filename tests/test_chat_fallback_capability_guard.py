import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _valid_completion_response(response_id: str, content: str = "ok") -> dict:
    # A bare {"id": ...} response has no "choices" and would itself be flagged
    # as an empty_completion by the degenerate-response detector, so "success"
    # mocks in this module must be choices-shaped.
    return {
        "id": response_id,
        "choices": [
            {"finish_reason": "stop", "message": {"role": "assistant", "content": content}}
        ],
    }


class ChatFallbackCapabilityGuardTests(unittest.TestCase):
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

    @staticmethod
    def _vision_request_payload(model: str) -> dict:
        return {
            "model": model,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "https://example.com/a.png"},
                        },
                    ],
                }
            ],
        }

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_vision_request_skips_non_vision_candidate_and_uses_next(
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
                        "model": "text-only-model",
                        "use_provider_order_as_fallback": False,
                        "supports_vision": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "vision-model",
                        "use_provider_order_as_fallback": False,
                    },
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

        make_llm_request_mock.return_value = (_valid_completion_response("vision-success"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=self._vision_request_payload("gateway-model"),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("vision-success"))
        self.assertEqual(make_llm_request_mock.await_count, 1)
        self.assertEqual(
            make_llm_request_mock.await_args.args[3]["model"],
            "vision-model",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_all_candidates_filtered_returns_422_no_capable_model(
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
                        "model": "text-only-model-1",
                        "use_provider_order_as_fallback": False,
                        "supports_vision": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "text-only-model-2",
                        "use_provider_order_as_fallback": False,
                        "supports_vision": False,
                    },
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

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=self._vision_request_payload("gateway-model"),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 422)
        body = response.json()
        self.assertEqual(body["error"]["code"], "no_capable_model")
        self.assertEqual(body["detail"]["code"], "no_capable_model")
        self.assertEqual(body["detail"]["gateway_model"], "gateway-model")
        self.assertEqual(
            {candidate["model"] for candidate in body["detail"]["candidates"]},
            {"text-only-model-1", "text-only-model-2"},
        )
        self.assertEqual(make_llm_request_mock.await_count, 0)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_context_overflow_fallback_target_not_filtered_by_capability_guard(
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
                        "model": "small-model",
                        "use_provider_order_as_fallback": False,
                    }
                ],
                # Deliberately marked as vision-unsupported to prove the capability
                # guard never prefilters the dedicated context-overflow target.
                "context_overflow_fallback": {
                    "provider": "test-provider",
                    "model": "large-context-model",
                    "use_provider_order_as_fallback": False,
                    "supports_vision": False,
                },
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

        async def fake_make_llm_request(_client, _target_url, _headers, payload, _is_streaming, **_kwargs):
            if payload["model"] == "small-model":
                return None, "{\"error\":{\"code\":\"context_length_exceeded\",\"message\":\"too long\"}}"
            if payload["model"] == "large-context-model":
                return _valid_completion_response("context-fallback-success"), None
            return None, "unexpected fallback path"

        make_llm_request_mock.side_effect = fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=self._vision_request_payload("gateway-model"),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("context-fallback-success"))

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_no_metadata_configs_are_unaffected_by_capability_guard(
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

        make_llm_request_mock.return_value = (_valid_completion_response("plain-success"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=self._vision_request_payload("gateway-model"),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("plain-success"))
        self.assertEqual(
            make_llm_request_mock.await_args.args[3]["model"],
            "provider-model",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.ModelRotationDB", return_value=Mock(get_next_model_index=AsyncMock(return_value=0)))
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_rotation_index_unaffected_by_per_request_filtering(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        model_rotation_db_cls,
        make_llm_request_mock,
    ):
        get_next_model_index_mock = model_rotation_db_cls.return_value.get_next_model_index
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
                        "model": "text-only-model",
                        "use_provider_order_as_fallback": False,
                        "supports_vision": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "vision-model-1",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "vision-model-2",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_valid_completion_response("rotation-success"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=self._vision_request_payload("gateway-model"),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), _valid_completion_response("rotation-success"))
        self.assertEqual(get_next_model_index_mock.call_args.kwargs["total_models"], 3)


if __name__ == "__main__":
    unittest.main()
