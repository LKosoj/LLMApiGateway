import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import anthropic
import httpx
import tiktoken
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from tests._async_compat import run_async
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _build_fake_config_loader() -> Mock:
    fake_config_loader = Mock()
    fake_config_loader.configured_paths = {}
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
    fake_config_loader.load_complete.return_value = fake_config_loader
    return fake_config_loader


def _estimate_expected_input_tokens(payload: dict, model_name: str) -> int:
    try:
        encoding = tiktoken.encoding_for_model(model_name)
    except KeyError:
        try:
            encoding = tiktoken.get_encoding("o200k_base")
        except ValueError:
            encoding = tiktoken.get_encoding("cl100k_base")

    serialized_payload = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(encoding.encode(serialized_payload))


def _build_streaming_openai_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-1","choices":[{"delta":{"role":"assistant","content":"Hel"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-1","choices":[{"delta":{"content":"lo"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":10,"completion_tokens":5}}\n'
        yield b"\n"
        yield b"data: [DONE]\n"
        yield b"\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_tool_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-tool-1","choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_weather_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"Mos"}}]}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-tool-1","choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"cow\\"}"}}]}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-tool-1","choices":[{"finish_reason":"tool_calls"}],"usage":{"prompt_tokens":11,"completion_tokens":4}}\n'
        yield b"\n"
        yield b"data: [DONE]\n"
        yield b"\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_reasoning_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-reasoning-1","choices":[{"delta":{"role":"assistant","reasoning_content":"internal note"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-reasoning-1","choices":[{"delta":{"content":"Visible"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-reasoning-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":4}}\n'
        yield b"\n"
        yield b"data: [DONE]\n"
        yield b"\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_utf8_response() -> StreamingResponse:
    content_chunk = (
        'data: {"id":"chunk-utf8-1","choices":[{"delta":{"role":"assistant","content":"пр"}}]}\n\n'
    ).encode("utf-8")
    split_index = content_chunk.index("п".encode("utf-8")) + 1

    async def body():
        yield content_chunk[:split_index]
        yield content_chunk[split_index:]
        yield b'data: {"id":"chunk-utf8-1","choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":7,"completion_tokens":2}}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_response_with_unterminated_final_event() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-tail-1","choices":[{"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
        yield b'data: {"id":"chunk-tail-1","choices":[{"delta":{"content":"lo"}}]}'

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_error_response() -> StreamingResponse:
    async def body():
        yield b'data: {"error":{"message":"SECRET_UPSTREAM_STREAM_ERROR","type":"server_error"}}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


class AnthropicMessagesTests(unittest.TestCase):
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
    def test_messages_endpoint_translates_anthropic_request_and_response(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.load_operation_rules.return_value = {}
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

        make_llm_request_mock.return_value = (
            {
                "id": "resp-1",
                "model": "provider-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {"role": "assistant", "content": "Hello!"},
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.045,
                    "prompt_tokens_details": {"cached_tokens": 4},
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            },
            None,
        )

        anthropic_payload = {
            "model": "gateway-model",
            "system": [{"type": "text", "text": "Be concise."}],
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Hi"}]},
            ],
            "max_tokens": 32,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": "resp-1",
                "type": "message",
                "role": "assistant",
                "model": "gateway-model",
                "content": [{"type": "text", "text": "Hello!"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        )

        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(provider_payload["model"], "provider-model")
        self.assertEqual(
            provider_payload["messages"],
            [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
        )
        self.assertEqual(provider_payload["max_tokens"], 32)
        self.assertFalse(provider_payload["stream"])

        _tokens_usage_db.return_value.insert_usage.assert_not_called()
        self.accounting_service.release.assert_awaited_once()

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_translates_anthropic_tools_roundtrip(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.load_operation_rules.return_value = {}
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

        make_llm_request_mock.return_value = (
            {
                "id": "resp-tools-1",
                "model": "provider-model",
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "call_weather_2",
                                    "type": "function",
                                    "function": {
                                        "name": "get_weather",
                                        "arguments": '{"city":"Paris"}',
                                    },
                                }
                            ],
                        },
                    }
                ],
                "usage": {"prompt_tokens": 14, "completion_tokens": 6},
            },
            None,
        )

        anthropic_payload = {
            "model": "gateway-model",
            "system": "Use tools when needed.",
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "Check weather"}]},
                {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": "toolu_weather_1",
                            "name": "get_weather",
                            "input": {"city": "Moscow"},
                        }
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": "toolu_weather_1",
                            "content": [{"type": "text", "text": '{"temp_c":21}'}],
                        },
                        {"type": "text", "text": "Now summarize it."},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Read weather by city",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                        "required": ["city"],
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "max_tokens": 32,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": "resp-tools-1",
                "type": "message",
                "role": "assistant",
                "model": "gateway-model",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_weather_2",
                        "name": "get_weather",
                        "input": {"city": "Paris"},
                    }
                ],
                "stop_reason": "tool_use",
                "stop_sequence": None,
                "usage": {"input_tokens": 14, "output_tokens": 6},
            },
        )

        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(provider_payload["model"], "provider-model")
        self.assertEqual(
            provider_payload["messages"],
            [
                {"role": "system", "content": "Use tools when needed."},
                {"role": "user", "content": "Check weather"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "toolu_weather_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": '{"city":"Moscow"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "toolu_weather_1",
                    "content": '{"temp_c":21}',
                },
                {"role": "user", "content": "Now summarize it."},
            ],
        )
        self.assertEqual(
            provider_payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "description": "Read weather by city",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                            "required": ["city"],
                        },
                    },
                }
            ],
        )
        self.assertEqual(
            provider_payload["tool_choice"],
            {"type": "function", "function": {"name": "get_weather"}},
        )
        self.assertFalse(provider_payload["stream"])

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_translates_openai_sse_to_anthropic_sse(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.load_operation_rules.return_value = {}
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

        make_llm_request_mock.return_value = (_build_streaming_openai_response(), None)

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 32,
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")
                    status_code = response.status_code
                    content_type = response.headers.get("content-type")

        self.assertEqual(status_code, 200)
        self.assertEqual(content_type, "text/event-stream; charset=utf-8")
        self.assertIn("event: message_start", response_text)
        self.assertIn("event: content_block_start", response_text)
        self.assertIn("event: content_block_delta", response_text)
        self.assertIn('"text":"Hel"', response_text)
        self.assertIn('"text":"lo"', response_text)
        self.assertIn("event: message_delta", response_text)
        self.assertIn('"stop_reason":"end_turn"', response_text)
        # Check for usage fields (values may vary based on estimation)
        self.assertIn('"input_tokens"', response_text)
        self.assertIn('"output_tokens"', response_text)
        self.assertIn("event: message_stop", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_error_chunk_emits_anthropic_error_event(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_build_streaming_openai_error_response(), None)

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 32,
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertIn("event: error", response_text)
        self.assertIn('"message":"Upstream stream failed."', response_text)
        self.assertNotIn("event: message_stop", response_text)
        self.assertNotIn("SECRET_UPSTREAM_STREAM_ERROR", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_translates_tool_calls_to_anthropic_tool_events(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
        fake_config_loader.configured_paths = {}
        fake_config_loader.operation_rules = {}
        fake_config_loader.load_operation_rules.return_value = {}
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

        make_llm_request_mock.return_value = (_build_streaming_openai_tool_response(), None)

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Call a tool"}],
            "tools": [
                {
                    "name": "get_weather",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "max_tokens": 32,
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")
                    status_code = response.status_code
                    content_type = response.headers.get("content-type")

        self.assertEqual(status_code, 200)
        self.assertEqual(content_type, "text/event-stream; charset=utf-8")
        self.assertIn("event: message_start", response_text)
        self.assertEqual(response_text.count("event: content_block_start"), 1)
        self.assertIn('"type":"tool_use"', response_text)
        self.assertIn('"name":"get_weather"', response_text)
        self.assertIn('"type":"input_json_delta"', response_text)
        self.assertIn('\\"city\\":\\"Mos', response_text)
        self.assertIn('cow\\"}', response_text)
        self.assertIn("event: content_block_stop", response_text)
        self.assertIn('"stop_reason":"tool_use"', response_text)
        # Check for usage fields (values may vary based on estimation)
        self.assertIn('"input_tokens"', response_text)
        self.assertIn('"output_tokens"', response_text)
        self.assertIn("event: message_stop", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_preserves_split_utf8_chunks(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_streaming_openai_utf8_response(), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
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
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('"text":"пр"', response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_preserves_unterminated_final_event(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            _build_streaming_openai_response_with_unterminated_final_event(),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
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
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('"text":"Hel"', response_text)
        self.assertIn('"text":"lo"', response_text)

    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_translates_anthropic_image_blocks_to_openai_content(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=AsyncMock(return_value=({"id": "resp-1", "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Seen"}}], "usage": {"prompt_tokens": 9, "completion_tokens": 3}}, None))) as make_llm_request_mock:
            anthropic_payload = {
                "model": "gateway-model",
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": "Describe this image"},
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/png",
                                    "data": "ZmFrZS1pbWFnZS1ieXRlcw==",
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 32,
            }

            with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/messages",
                        json=anthropic_payload,
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

        self.assertEqual(response.status_code, 200)
        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(
            provider_payload["messages"],
            [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {
                            "type": "image_url",
                            "image_url": {"url": "data:image/png;base64,ZmFrZS1pbWFnZS1ieXRlcw=="},
                        },
                    ],
                }
            ],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_passes_through_openai_supported_fields(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        """Test that only OpenAI-supported поля передаются корректно.

        Anthropic-specific поля (cache_control, container, inference_geo, thinking, metadata)
        не поддерживаются OpenAI API и не должны передаваться.
        """
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        # output_config converts to response_format={"type": "json_object"}, so the
        # mocked completion content must itself be valid JSON: otherwise the new
        # degenerate-response detector (Package D) would flag "ok" as format_ignored.
        make_llm_request_mock.return_value = (
            {
                "id": "resp-1",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": '{"ok": true}'}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
            None,
        )

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 32,
            "cache_control": {"type": "ephemeral"},  # Anthropic-specific, не передаётся
            "container": "container-1",  # Anthropic-specific, не передаётся
            "inference_geo": "us",  # Anthropic-specific, не передаётся
            "output_config": {"format": {"type": "json_object"}},  # Конвертируется в response_format
            "service_tier": "standard_only",  # Поддерживается обоими
            "thinking": {"type": "disabled"},  # Anthropic-specific, не передаётся
            "top_k": 5,  # OpenAI не поддерживает top_k (только top_p)
            "metadata": {"trace_id": "abc"},  # Anthropic-specific, не передаётся
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        provider_payload = make_llm_request_mock.await_args.args[3]

        # Проверяем только поля, поддерживаемые OpenAI
        self.assertEqual(provider_payload["service_tier"], "standard_only")
        # output_config конвертируется в response_format
        self.assertEqual(provider_payload["response_format"], {"type": "json_object"})
        self.assertNotIn("cache_control", provider_payload)
        self.assertNotIn("container", provider_payload)
        self.assertNotIn("inference_geo", provider_payload)
        self.assertNotIn("thinking", provider_payload)
        self.assertNotIn("metadata", provider_payload)
        # top_k не поддерживается OpenAI (в отличие от top_p)
        self.assertNotIn("top_k", provider_payload)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_translates_document_and_assistant_thinking_blocks(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            {
                "id": "resp-1",
                "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 2},
            },
            None,
        )

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "internal note", "signature": "sig"},
                        {"type": "redacted_thinking", "data": "opaque-state"},
                        {"type": "text", "text": "Visible answer prefix"},
                    ],
                },
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "document",
                            "title": "Rules",
                            "context": "Read carefully",
                            "source": {"type": "text", "media_type": "text/plain", "data": "Line one"},
                        }
                    ],
                },
            ],
            "max_tokens": 32,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(
            provider_payload["messages"],
            [
                {"role": "assistant", "content": "internal noteopaque-stateVisible answer prefix"},
                {
                    "role": "user",
                    "content": "Document title: Rules\nDocument context: Read carefully\nLine one",
                },
            ],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_omits_unsigned_reasoning_blocks_from_json_response(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            {
                "id": "resp-reasoning-1",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "reasoning_content": "internal note",
                            "content": "Visible answer",
                        },
                    }
                ],
                "usage": {"prompt_tokens": 8, "completion_tokens": 3},
            },
            None,
        )

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "max_tokens": 32,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "id": "resp-reasoning-1",
                "type": "message",
                "role": "assistant",
                "model": "gateway-model",
                "content": [{"type": "text", "text": "Visible answer"}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {"input_tokens": 8, "output_tokens": 3},
            },
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_endpoint_stream_preserves_legacy_reasoning_events(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_streaming_openai_reasoning_response(), None)

        anthropic_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hello"}],
            "max_tokens": 32,
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/messages",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertIn('"type":"thinking"', response_text)
        self.assertIn('"type":"thinking_delta"', response_text)
        self.assertEqual(response_text.count("event: content_block_start"), 2)
        self.assertIn('"type":"text_delta"', response_text)
        self.assertIn('"text":"Visible"', response_text)
        self.assertIn('"stop_reason":"end_turn"', response_text)

    def test_official_anthropic_sdk_messages_create_works_with_x_api_key(self):
        async def scenario():
            fake_config_loader = _build_fake_config_loader()
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as sdk_http_client:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as app_http_client:
                    sdk = anthropic.AsyncAnthropic(
                        api_key="test-gateway-key",
                        base_url="http://testserver",
                        http_client=sdk_http_client,
                    )

                    with patch.object(main.settings, "gateway_api_key", "test-gateway-key"), patch(
                        "main.ConfigLoader", return_value=fake_config_loader
                    ), patch("main.TokensUsageDB"), patch(
                        "main.create_shared_http_client", return_value=app_http_client
                    ), patch(
                        "llm_gateway_core.api.v1.chat.make_llm_request",
                        new=AsyncMock(
                            return_value=(
                                {
                                    "id": "resp-1",
                                    "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "Hello from SDK"}}],
                                    "usage": {"prompt_tokens": 10, "completion_tokens": 4},
                                },
                                None,
                            )
                        ),
                    ):
                        async with main.app.router.lifespan_context(main.app):
                            response = await sdk.messages.create(
                                model="gateway-model",
                                max_tokens=32,
                                messages=[{"role": "user", "content": "Hi"}],
                            )

            self.assertEqual(response.role, "assistant")
            self.assertEqual(response.content[0].type, "text")
            self.assertEqual(response.content[0].text, "Hello from SDK")
            self.assertEqual(response.usage.input_tokens, 10)
            self.assertEqual(response.usage.output_tokens, 4)

        run_async(scenario())

    def test_official_anthropic_sdk_messages_stream_works_with_x_api_key(self):
        async def scenario():
            fake_config_loader = _build_fake_config_loader()
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as sdk_http_client:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as app_http_client:
                    sdk = anthropic.AsyncAnthropic(
                        api_key="test-gateway-key",
                        base_url="http://testserver",
                        http_client=sdk_http_client,
                    )

                    with patch.object(main.settings, "gateway_api_key", "test-gateway-key"), patch(
                        "main.ConfigLoader", return_value=fake_config_loader
                    ), patch("main.TokensUsageDB"), patch(
                        "main.create_shared_http_client", return_value=app_http_client
                    ), patch(
                        "llm_gateway_core.api.v1.chat.make_llm_request",
                        new=AsyncMock(return_value=(_build_streaming_openai_response(), None)),
                    ):
                        async with main.app.router.lifespan_context(main.app):
                            async with sdk.messages.stream(
                                model="gateway-model",
                                max_tokens=32,
                                messages=[{"role": "user", "content": "Hello"}],
                            ) as stream:
                                final_text = await stream.get_final_text()
                                final_message = await stream.get_final_message()

            self.assertEqual(final_text, "Hello")
            self.assertEqual(final_message.role, "assistant")
            self.assertEqual(final_message.content[0].type, "text")
            self.assertEqual(final_message.usage.input_tokens, 10)
            self.assertEqual(final_message.usage.output_tokens, 5)

        run_async(scenario())

    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_count_tokens_endpoint_returns_deterministic_input_tokens(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = _build_fake_config_loader()
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        anthropic_payload = {
            "model": "gateway-model",
            "system": "Be concise.",
            "messages": [
                {"role": "user", "content": "Hello"},
                {
                    "role": "assistant",
                    "content": [
                        {"type": "thinking", "thinking": "internal", "signature": "sig"},
                        {"type": "text", "text": "Hi"},
                    ],
                },
            ],
            "tools": [
                {
                    "name": "get_weather",
                    "description": "Read weather by city",
                    "input_schema": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "tool_choice": {"type": "tool", "name": "get_weather"},
            "thinking": {"type": "disabled"},
            "output_config": {"format": {"type": "json_object"}},
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages/count_tokens",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

                self.assertEqual(response.status_code, 200)
                response_data = response.json()
                self.assertIn("input_tokens", response_data)
                self.assertIsInstance(response_data["input_tokens"], int)
                self.assertGreater(response_data["input_tokens"], 0)

                # Verify determinism: same input should produce same output
                response2 = client.post(
                    "/v1/messages/count_tokens",
                    json=anthropic_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                self.assertEqual(response2.json()["input_tokens"], response_data["input_tokens"])

    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_count_tokens_rejects_excluded_model(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = _build_fake_config_loader()
        fake_config_loader.model_rules = {"excluded_models": ["blocked/*"]}
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages/count_tokens",
                    json={
                        "model": "blocked/model",
                        "messages": [{"role": "user", "content": "Count these tokens"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 404)
        self.assertIn("excluded", response.json()["detail"])

    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_messages_count_tokens_rejects_alias_without_chat_route(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = _build_fake_config_loader()
        fake_config_loader.model_rules = {"aliases": {"public-model": "missing-route"}}
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages/count_tokens",
                    json={
                        "model": "public-model",
                        "messages": [{"role": "user", "content": "Count these tokens"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 404)
        self.assertIn("no chat route", response.json()["detail"])

    def test_official_anthropic_sdk_messages_count_tokens_works_with_x_api_key(self):
        async def scenario():
            fake_config_loader = _build_fake_config_loader()
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as sdk_http_client:
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as app_http_client:
                    sdk = anthropic.AsyncAnthropic(
                        api_key="test-gateway-key",
                        base_url="http://testserver",
                        http_client=sdk_http_client,
                    )

                    with patch.object(main.settings, "gateway_api_key", "test-gateway-key"), patch(
                        "main.ConfigLoader", return_value=fake_config_loader
                    ), patch("main.TokensUsageDB"), patch(
                        "main.create_shared_http_client", return_value=app_http_client
                    ):
                        async with main.app.router.lifespan_context(main.app):
                            response = await sdk.messages.count_tokens(
                                model="gateway-model",
                                messages=[{"role": "user", "content": "Count these tokens"}],
                            )

            self.assertGreater(response.input_tokens, 0)

        run_async(scenario())


if __name__ == "__main__":
    unittest.main()
