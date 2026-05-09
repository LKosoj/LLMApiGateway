import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import Request
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import chat as chat_api
from llm_gateway_core.middleware import chat_logging
from llm_gateway_core.utils.usage_tracking import initialize_tokens_usage
from tests._async_compat import run_async


def _build_fake_config_loader() -> Mock:
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
    fake_config_loader.load_operation_rules.return_value = {}
    fake_config_loader.operation_rules = {}
    return fake_config_loader


def _build_streaming_openai_usage_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-usage-1","choices":[{"delta":{"role":"assistant","content":"Hi"}}]}\n\n'
        yield (
            b'data: {"id":"chunk-usage-1","choices":[{"finish_reason":"stop"}],'
            b'"usage":{"prompt_tokens":13,"completion_tokens":7,"total_tokens":20}}\n\n'
        )
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


class ChatLoggingAnthropicWrapperUsageTests(unittest.TestCase):
    def setUp(self):
        self._original_tokens_usage_db = chat_logging.tokens_usage_db
        self._original_api_keys_db = chat_logging.api_keys_db
        self._original_rate_limiter = chat_logging.rate_limiter
        self._original_usd_budget_ledger = chat_logging.usd_budget_ledger

    def tearDown(self):
        chat_logging.set_tokens_usage_db(self._original_tokens_usage_db)
        chat_logging.set_api_keys_db(self._original_api_keys_db)
        chat_logging.set_rate_limiter(self._original_rate_limiter)
        chat_logging.set_usd_budget_ledger(self._original_usd_budget_ledger)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_fronted_stream_records_inner_openai_usage_from_request_state(
        self,
        config_loader_cls,
        async_client_ctor,
        tokens_usage_db_cls,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader()

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        fake_tokens_usage_db = Mock()
        tokens_usage_db_cls.return_value = fake_tokens_usage_db
        make_llm_request_mock.return_value = (_build_streaming_openai_usage_response(), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch.object(chat_logging.settings, "log_chat_messages", False):
                with patch.object(chat_logging, "get_token_usage", side_effect=lambda _: initialize_tokens_usage()):
                    with patch.object(chat_logging, "backfill_zero_token_counts", return_value=False):
                        with TestClient(main.app) as client:
                            with client.stream(
                                "POST",
                                "/v1/messages",
                                json={
                                    "model": "gateway-model",
                                    "messages": [{"role": "user", "content": "Hello"}],
                                    "max_tokens": 32,
                                    "stream": True,
                                },
                                headers={"Authorization": "Bearer test-gateway-key"},
                            ) as response:
                                response.read()
                                status_code = response.status_code

        self.assertEqual(status_code, 200)
        fake_tokens_usage_db.insert_usage.assert_called_once()
        usage = fake_tokens_usage_db.insert_usage.call_args.args[0]
        self.assertEqual(usage["prompt_tokens"], 13)
        self.assertEqual(usage["completion_tokens"], 7)
        self.assertEqual(usage["total_tokens"], 20)
        self.assertEqual(usage["gateway_model"], "gateway-model")
        self.assertEqual(usage["provider"], "test-provider")
        self.assertEqual(usage["model"], "provider-model")

    def test_responses_wrapper_copies_inner_openai_usage_to_request_state(self):
        async def body():
            yield (
                b'data: {"id":"chunk-responses-1","choices":[{"finish_reason":"stop"}],'
                b'"usage":{"prompt_tokens":9,"completion_tokens":4,"total_tokens":13}}\n\n'
            )
            yield b"data: [DONE]\n\n"

        request = Request(scope={"type": "http", "method": "POST", "path": "/v1/responses"})
        response = StreamingResponse(body(), media_type="text/event-stream")
        result = chat_api._openai_stream_to_responses(response, "gateway-model", request=request)

        async def consume():
            return [chunk async for chunk in result.body_iterator]

        run_async(consume())

        self.assertEqual(
            request.state.usage_tracker,
            {
                "prompt_tokens": 9,
                "completion_tokens": 4,
                "total_tokens": 13,
            },
        )


if __name__ == "__main__":
    unittest.main()
