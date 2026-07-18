"""Integration tests: upstream ``x-ratelimit-*`` response headers feed the
quota ledger via ``attempt_model_fallback_rule`` -> ``UpstreamRoutingState``.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.services.request_handler import RequestErrorDetail
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _build_groq_config_loader() -> Mock:
    loader = Mock()
    loader.configured_paths = {}
    loader.providers_config = {
        "groq": SimpleNamespace(
            baseUrl="https://api.groq.com/openai/v1",
            apikey="GROQ_SECRET",
        ),
    }
    loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "groq",
                    "model": "groq-model",
                    "use_provider_order_as_fallback": False,
                },
            ],
            "rotate_models": False,
        }
    }
    loader.operation_rules = {}
    loader.model_rules = {}
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    loader.load_model_rules.return_value = loader.model_rules
    loader.load_operation_rules.return_value = loader.operation_rules
    loader.validate_fallback_operation_consistency.return_value = None
    loader.load_complete.return_value = loader
    return loader


class _ChatRateLimitHeaderScenario:
    """Boots ``main.app`` with a single Groq fallback rule and a mocked
    ``make_llm_request`` so tests can drive one or more chat completions
    against a shared, real-clock ``UpstreamRoutingState``.
    """

    def __init__(self, make_llm_request_side_effect) -> None:
        self._stack = ExitStack()
        self._make_llm_request_side_effect = make_llm_request_side_effect
        self.upstream_state = UpstreamRoutingState()

    def __enter__(self) -> "_ChatRateLimitHeaderScenario":
        stack = self._stack
        install_main_chat_accounting_double(stack)
        config_loader_cls = stack.enter_context(patch("main.ConfigLoader"))
        async_client_ctor = stack.enter_context(patch("main.create_shared_http_client"))
        stack.enter_context(patch("main.TokensUsageDB"))
        openrouter_service_cls = stack.enter_context(patch("main.OpenRouterFreeModelsService"))
        fallback_eval_service_cls = stack.enter_context(patch("main.FallbackModelEvalService"))
        self.make_llm_request = stack.enter_context(
            patch("llm_gateway_core.api.v1.chat.make_llm_request")
        )

        config_loader_cls.return_value = _build_groq_config_loader()

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        stack.enter_context(
            patch("main.ConfigUpdateCoordinator", return_value=config_update_coordinator)
        )

        openrouter_service = Mock()
        openrouter_service.start_runtime = AsyncMock()
        openrouter_service.stop = AsyncMock()
        openrouter_service_cls.return_value = openrouter_service

        fallback_eval_service = Mock()
        fallback_eval_service.stop = AsyncMock()
        fallback_eval_service_cls.return_value = fallback_eval_service

        self.make_llm_request.side_effect = self._make_llm_request_side_effect

        stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
        stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
        stack.enter_context(patch("main.UpstreamRoutingState", return_value=self.upstream_state))

        self.client = stack.enter_context(TestClient(main.app))
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stack.close()

    def post_chat(self):
        return self.client.post(
            "/v1/chat/completions",
            json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
            headers={"Authorization": "Bearer test-gateway-key"},
        )


class ChatDispatchRateLimitHeaderTests(unittest.TestCase):
    def test_successful_groq_response_records_observed_limit_via_headers(self):
        async def fake_make_llm_request(*_args, **kwargs):
            sink = kwargs.get("response_headers_sink")
            if sink is not None:
                sink.update(
                    {
                        "x-ratelimit-limit-requests": "100",
                        "x-ratelimit-remaining-requests": "42",
                        "x-ratelimit-reset-requests": "1m0s",
                    }
                )
            return {
                "id": "groq-success",
                "choices": [
                    {"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}
                ],
            }, None

        with _ChatRateLimitHeaderScenario(fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        rows = scenario.upstream_state.get_status_rows()
        self.assertEqual(len(rows), 1)
        observed_rpm = rows[0]["observed_limits"]["rpm"]
        self.assertEqual(observed_rpm["limit"], 100)
        self.assertEqual(observed_rpm["remaining"], 42)
        self.assertEqual(observed_rpm["source"], "header")

    def test_429_response_with_header_reset_blocks_subsequent_selection(self):
        async def fake_make_llm_request(*_args, **kwargs):
            sink = kwargs.get("response_headers_sink")
            if sink is not None:
                sink.update(
                    {
                        "x-ratelimit-limit-requests": "100",
                        "x-ratelimit-remaining-requests": "0",
                        # Comfortably longer than this test's run time.
                        "x-ratelimit-reset-requests": "1h0m0s",
                    }
                )
            return None, RequestErrorDetail(
                "Upstream request failed with HTTP status 429.",
                status_code=429,
            )

        with _ChatRateLimitHeaderScenario(fake_make_llm_request) as scenario:
            first_response = scenario.post_chat()
            self.assertEqual(first_response.status_code, 503)
            self.assertEqual(scenario.make_llm_request.await_count, 1)

            second_response = scenario.post_chat()

            self.assertEqual(second_response.status_code, 503)
            self.assertEqual(
                scenario.make_llm_request.await_count,
                1,
                "a key blocked by an observed zero-remaining header must not "
                "be retried upstream",
            )
            self.assertIn(
                "No upstream key is currently available",
                second_response.json()["detail"],
            )


if __name__ == "__main__":
    unittest.main()
