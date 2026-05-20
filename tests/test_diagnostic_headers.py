import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main


def _successful_chat_response() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }


def _build_config_loader() -> Mock:
    loader = Mock()
    loader.providers_config = {
        "first-provider": SimpleNamespace(
            baseUrl="https://first.example",
            apikey="FIRST_PROVIDER_SECRET",
        ),
        "second-provider": SimpleNamespace(
            baseUrl="https://second.example",
            apikey="SECOND_PROVIDER_SECRET",
        ),
    }
    loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                },
                {
                    "provider": "second-provider",
                    "model": "second-model",
                    "use_provider_order_as_fallback": False,
                },
            ],
            "rotate_models": False,
        }
    }
    loader.operation_rules = {}
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    loader.load_operation_rules.return_value = loader.operation_rules
    loader.validate_fallback_operation_consistency.return_value = None
    return loader


class DiagnosticHeadersTests(unittest.TestCase):
    def _post_chat(self, *, routing_diagnostic_headers: bool):
        with ExitStack() as stack:
            config_loader_cls = stack.enter_context(patch("main.ConfigLoader"))
            async_client_ctor = stack.enter_context(patch("main.httpx.AsyncClient"))
            stack.enter_context(patch("main.TokensUsageDB"))
            openrouter_service_cls = stack.enter_context(patch("main.OpenRouterFreeModelsService"))
            fallback_eval_service_cls = stack.enter_context(patch("main.FallbackModelEvalService"))
            make_llm_request = stack.enter_context(patch("llm_gateway_core.api.v1.chat.make_llm_request"))

            config_loader_cls.return_value = _build_config_loader()

            fake_http_client = Mock()
            fake_http_client.aclose = AsyncMock()
            async_client_ctor.return_value = fake_http_client

            openrouter_service = Mock()
            openrouter_service.start = AsyncMock()
            openrouter_service.stop = AsyncMock()
            openrouter_service_cls.return_value = openrouter_service

            fallback_eval_service = Mock()
            fallback_eval_service.stop = AsyncMock()
            fallback_eval_service_cls.return_value = fallback_eval_service

            make_llm_request.side_effect = [
                (None, "upstream rate limit"),
                (_successful_chat_response(), None),
            ]

            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(
                patch.object(
                    main.settings,
                    "routing_diagnostic_headers",
                    routing_diagnostic_headers,
                )
            )
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [
                            {"role": "user", "content": "hello"},
                        ],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        return response

    def test_routing_diagnostic_headers_are_absent_when_disabled(self):
        response = self._post_chat(routing_diagnostic_headers=False)

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("X-Routed-Via", response.headers)
        self.assertNotIn("X-Fallback-Attempts", response.headers)

    def test_routing_diagnostic_headers_are_present_when_enabled(self):
        response = self._post_chat(routing_diagnostic_headers=True)

        self.assertEqual(response.status_code, 200)
        routed_via = response.headers["X-Routed-Via"]
        self.assertTrue(routed_via.startswith("second-provider/second-model"))
        self.assertNotIn("SECOND_PROVIDER_SECRET", routed_via)
        self.assertEqual(response.headers["X-Fallback-Attempts"], "2")


if __name__ == "__main__":
    unittest.main()
