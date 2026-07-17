import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import chat as chat_api
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _build_fake_config_loader() -> Mock:
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
    return fake_config_loader


def _build_streaming_openai_tool_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-tool-1","created":1735689600,"choices":[{"delta":{"role":"assistant","tool_calls":[{"index":0,"id":"call_weather_1","type":"function","function":{"name":"get_weather","arguments":"{\\"city\\":\\"Mos"}}]}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-tool-1","created":1735689600,"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"cow\\"}"}}]}}],"usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-tool-1","created":1735689600,"choices":[{"finish_reason":"tool_calls"}]}\n'
        yield b"\n"
        yield b"data: [DONE]\n"
        yield b"\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_utf8_response() -> StreamingResponse:
    content_chunk = (
        'data: {"id":"chunk-utf8-1","created":1735689600,"choices":[{"delta":{"role":"assistant","content":"пр"}}]}\n\n'
    ).encode("utf-8")
    split_index = content_chunk.index("п".encode("utf-8")) + 1

    async def body():
        yield content_chunk[:split_index]
        yield content_chunk[split_index:]
        yield b'data: {"id":"chunk-utf8-1","created":1735689600,"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_response_with_unterminated_final_event() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-tail-1","created":1735689600,"choices":[{"delta":{"role":"assistant","content":"Hel"}}]}\n\n'
        yield b'data: {"id":"chunk-tail-1","created":1735689600,"choices":[{"delta":{"content":"lo"}}]}'

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_error_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-error-1","created":1735689600,"error":{"message":"SECRET_UPSTREAM_STREAM_ERROR","type":"server_error"}}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_reasoning_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-reasoning-1","created":1735689600,"choices":[{"delta":{"role":"assistant","reasoning_content":"internal note"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-reasoning-1","created":1735689600,"choices":[{"delta":{"content":"Visible"}}]}\n'
        yield b"\n"
        yield b'data: {"id":"chunk-reasoning-1","created":1735689600,"choices":[{"finish_reason":"stop"}],"usage":{"prompt_tokens":12,"completion_tokens":4,"total_tokens":16}}\n'
        yield b"\n"
        yield b"data: [DONE]\n"
        yield b"\n"

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_streaming_openai_wrapped_json_response() -> StreamingResponse:
    async def body():
        yield b'data: {"id":"chunk-json-1","created":1735689600,"choices":[{"delta":{"role":"assistant","content":"```"}}]}\n\n'
        yield b'data: {"id":"chunk-json-1","created":1735689600,"choices":[{"delta":{"content":"json\\n{"}}]}\n\n'
        yield b'data: {"id":"chunk-json-1","created":1735689600,"choices":[{"delta":{"content":"\\"ok\\":true}\\n"}}]}\n\n'
        yield b'data: {"id":"chunk-json-1","created":1735689600,"choices":[{"delta":{"content":"```"}}],"usage":{"prompt_tokens":9,"completion_tokens":2,"total_tokens":11}}\n\n'
        yield b'data: {"id":"chunk-json-1","created":1735689600,"choices":[{"finish_reason":"stop"}]}\n\n'
        yield b"data: [DONE]\n\n"

    return StreamingResponse(body(), media_type="text/event-stream")


class OpenAIResponsesTests(unittest.TestCase):
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

    def test_chat_module_exports_compatibility_routers(self):
        self.assertIsInstance(chat_api.responses_router, APIRouter)
        self.assertIsInstance(chat_api.anthropic_router, APIRouter)

    def test_build_openai_usage_to_responses_usage_handles_null_details(self):
        usage = chat_api._build_openai_usage_to_responses_usage(
            {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "prompt_tokens_details": None,
                "completion_tokens_details": None,
            }
        )

        self.assertEqual(
            usage,
            {
                "input_tokens": 10,
                "input_tokens_details": {"cached_tokens": 0},
                "output_tokens": 5,
                "output_tokens_details": {"reasoning_tokens": 0},
                "total_tokens": 15,
            },
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_translates_request_and_text_response(
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
                "created": 1735689600,
                "model": "provider-model",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "<think>\ninternal reasoning\n</think>\n```json\n{\"ok\":true}\n```",
                        },
                    }
                ],
                "usage": {
                    "prompt_tokens": 10,
                    "completion_tokens": 5,
                    "total_tokens": 15,
                    "cost": 0.123,
                    "prompt_tokens_details": {"cached_tokens": 2},
                    "completion_tokens_details": {"reasoning_tokens": 1},
                },
            },
            None,
        )

        responses_payload = {
            "model": "gateway-model",
            "instructions": "Be concise.",
            "input": [
                {
                    "type": "message",
                    "role": "developer",
                    "content": [{"type": "input_text", "text": "Classify carefully."}],
                },
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hi"}],
                },
                {
                    "type": "function_call",
                    "call_id": "call_lookup_1",
                    "name": "lookup",
                    "arguments": {"city": "Paris"},
                },
                {
                    "type": "function_call_output",
                    "call_id": "call_lookup_1",
                    "output": {"temp": 21},
                },
            ],
            "text": {"format": {"type": "json_object"}},
            "tool_choice": "auto",
            "tools": [
                {
                    "type": "function",
                    "name": "lookup",
                    "description": "Lookup weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "temperature": 0.2,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/responses",
                    json=responses_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "resp-1")
        self.assertEqual(body["object"], "response")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["model"], "gateway-model")
        self.assertEqual(body["output"][0]["type"], "message")
        self.assertEqual(body["output"][0]["content"][0]["type"], "output_text")
        self.assertEqual(body["output"][0]["content"][0]["text"], '{"ok":true}')
        self.assertEqual(body["usage"]["input_tokens"], 10)
        self.assertEqual(body["usage"]["output_tokens"], 5)
        self.assertEqual(body["usage"]["total_tokens"], 15)
        self.assertEqual(body["usage"]["input_tokens_details"]["cached_tokens"], 2)
        self.assertEqual(body["usage"]["output_tokens_details"]["reasoning_tokens"], 1)

        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(provider_payload["model"], "provider-model")
        self.assertEqual(
            provider_payload["messages"],
            [
                {"role": "system", "content": "Be concise."},
                {"role": "developer", "content": "Classify carefully."},
                {"role": "user", "content": "Hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_lookup_1",
                            "type": "function",
                            "function": {
                                "name": "lookup",
                                "arguments": '{"city":"Paris"}',
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_lookup_1",
                    "content": '{"temp":21}',
                },
            ],
        )
        self.assertEqual(provider_payload["response_format"], {"type": "json_object"})
        self.assertEqual(provider_payload["tool_choice"], "auto")
        self.assertEqual(
            provider_payload["tools"],
            [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Lookup weather",
                        "parameters": {
                            "type": "object",
                            "properties": {"city": {"type": "string"}},
                        },
                    },
                }
            ],
        )

        _tokens_usage_db.return_value.insert_usage.assert_not_called()
        self.accounting_service.release.assert_awaited_once()

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_translates_tool_response(
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
                "id": "resp-tool-1",
                "created": 1735689601,
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
                "usage": {"prompt_tokens": 14, "completion_tokens": 6, "total_tokens": 20},
            },
            None,
        )

        responses_payload = {
            "model": "gateway-model",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Check weather"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/responses",
                    json=responses_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["id"], "resp-tool-1")
        self.assertEqual(body["status"], "completed")
        self.assertEqual(len(body["output"]), 1)
        self.assertEqual(body["output"][0]["type"], "function_call")
        self.assertEqual(body["output"][0]["call_id"], "call_weather_2")
        self.assertEqual(body["output"][0]["name"], "get_weather")
        self.assertEqual(body["output"][0]["arguments"], '{"city":"Paris"}')

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_translates_tool_events(
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

        make_llm_request_mock.return_value = (_build_streaming_openai_tool_response(), None)

        responses_payload = {
            "model": "gateway-model",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Call a tool"}],
                }
            ],
            "tools": [
                {
                    "type": "function",
                    "name": "get_weather",
                    "parameters": {
                        "type": "object",
                        "properties": {"city": {"type": "string"}},
                    },
                }
            ],
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/responses",
                    json=responses_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")
                    status_code = response.status_code
                    content_type = response.headers.get("content-type")

        self.assertEqual(status_code, 200)
        self.assertEqual(content_type, "text/event-stream; charset=utf-8")
        self.assertIn('"type":"response.output_item.added"', response_text)
        self.assertIn('"type":"function_call"', response_text)
        self.assertIn('"name":"get_weather"', response_text)
        self.assertIn('"type":"response.function_call_arguments.delta"', response_text)
        self.assertIn('\\"city\\":\\"Mos', response_text)
        self.assertIn('cow\\"}', response_text)
        self.assertIn('"type":"response.completed"', response_text)
        self.assertIn('"input_tokens":11', response_text)
        self.assertIn('"output_tokens":4', response_text)
        self.assertIn("data: [DONE]", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_error_chunk_emits_failed_event(
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

        make_llm_request_mock.return_value = (_build_streaming_openai_error_response(), None)

        responses_payload = {
            "model": "gateway-model",
            "input": [
                {
                    "type": "message",
                    "role": "user",
                    "content": [{"type": "input_text", "text": "Hello"}],
                }
            ],
            "stream": True,
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/responses",
                    json=responses_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertIn('"type":"response.failed"', response_text)
        self.assertIn('"message":"Upstream stream failed."', response_text)
        self.assertNotIn('"type":"response.completed"', response_text)
        self.assertNotIn("SECRET_UPSTREAM_STREAM_ERROR", response_text)
        self.assertIn("data: [DONE]", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_preserves_split_utf8_chunks(
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
                    "/v1/responses",
                    json={
                        "model": "gateway-model",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Hello"}],
                            }
                        ],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('"delta":"пр"', response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_preserves_unterminated_final_event(
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
                    "/v1/responses",
                    json={
                        "model": "gateway-model",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Hello"}],
                            }
                        ],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertIn('"delta":"Hel"', response_text)
        self.assertIn('"delta":"lo"', response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_removes_wrapped_json_prefix_and_suffix(
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

        make_llm_request_mock.return_value = (_build_streaming_openai_wrapped_json_response(), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/responses",
                    json={
                        "model": "gateway-model",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Hello"}],
                            }
                        ],
                        "text": {"format": {"type": "json_object"}},
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        response_events = [
            json.loads(line[len("data: "):])
            for line in response_text.splitlines()
            if line.startswith("data: {")
        ]
        streamed_text = "".join(
            event["delta"]
            for event in response_events
            if event.get("type") == "response.output_text.delta"
        )
        self.assertNotIn("```json", response_text)
        self.assertNotIn('"delta":"```"', response_text)
        self.assertEqual(streamed_text, '{"ok":true}')
        self.assertIn('"type":"response.completed"', response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_responses_endpoint_stream_keeps_reasoning_and_visible_text_on_distinct_output_indices(
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

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/responses",
                    json={
                        "model": "gateway-model",
                        "input": [
                            {
                                "type": "message",
                                "role": "user",
                                "content": [{"type": "input_text", "text": "Hello"}],
                            }
                        ],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_events = [
                        json.loads(line[len("data: "):])
                        for line in response.read().decode("utf-8").splitlines()
                        if line.startswith("data: {")
                    ]

        self.assertEqual(response.status_code, 200)
        text_delta_events = [
            event
            for event in response_events
            if event.get("type") == "response.output_text.delta"
        ]
        self.assertEqual(text_delta_events[0]["delta"], "internal note")
        self.assertEqual(text_delta_events[0]["output_index"], 0)
        self.assertEqual(text_delta_events[0]["annotations"], ["thought"])
        self.assertEqual(text_delta_events[1]["delta"], "Visible")
        self.assertEqual(text_delta_events[1]["output_index"], 1)
