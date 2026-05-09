"""Tests for providers declared with ``type: "anthropic"`` in providers.json.

Covers:
- ``ProviderDetails.type`` default and validation.
- ``_openai_request_to_anthropic_payload`` / ``_anthropic_response_to_openai`` unit behaviour.
- Dispatcher routing to ``/v1/messages`` with Anthropic headers for each
  client/provider format pair (OpenAI→Anthropic and Anthropic→Anthropic).
- Streaming attempt against an Anthropic provider short-circuits (phase b).
- ``ProviderModelsService`` hits ``/v1/models`` with ``x-api-key`` for Anthropic.
"""

from __future__ import annotations

import asyncio
import json
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pydantic
from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import chat as chat_module
from llm_gateway_core.config.loader import ANTHROPIC_API_VERSION, ProviderDetails
from llm_gateway_core.services.provider_models import ProviderModelsService


def _build_anthropic_text_stream() -> StreamingResponse:
    async def body():
        yield b'event: message_start\n'
        yield (
            b'data: {"type":"message_start","message":'
            b'{"id":"msg_anth_1","type":"message","role":"assistant",'
            b'"model":"claude-sonnet-4-6","content":[],"stop_reason":null,'
            b'"stop_sequence":null,"usage":{"input_tokens":9,"output_tokens":0}}}\n\n'
        )
        yield b'event: content_block_start\n'
        yield (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
        )
        yield b'event: content_block_delta\n'
        yield (
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"Hel"}}\n\n'
        )
        yield b'event: content_block_delta\n'
        yield (
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"lo"}}\n\n'
        )
        yield b'event: content_block_stop\n'
        yield b'data: {"type":"content_block_stop","index":0}\n\n'
        yield b'event: message_delta\n'
        yield (
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":4}}\n\n'
        )
        yield b'event: message_stop\n'
        yield b'data: {"type":"message_stop"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_anthropic_cached_text_stream() -> StreamingResponse:
    async def body():
        yield b'event: message_start\n'
        yield (
            b'data: {"type":"message_start","message":'
            b'{"id":"msg_cached_1","type":"message","role":"assistant",'
            b'"model":"claude-sonnet-4-6","content":[],"stop_reason":null,'
            b'"stop_sequence":null,"usage":{'
            b'"input_tokens":13,"cache_creation_input_tokens":100,'
            b'"cache_read_input_tokens":13120,"output_tokens":0}}}\n\n'
        )
        yield b'event: content_block_start\n'
        yield (
            b'data: {"type":"content_block_start","index":0,'
            b'"content_block":{"type":"text","text":""}}\n\n'
        )
        yield b'event: content_block_delta\n'
        yield (
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"text_delta","text":"ok"}}\n\n'
        )
        yield b'event: content_block_stop\n'
        yield b'data: {"type":"content_block_stop","index":0}\n\n'
        yield b'event: message_delta\n'
        yield (
            b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
            b'"usage":{"output_tokens":5}}\n\n'
        )
        yield b'event: message_stop\n'
        yield b'data: {"type":"message_stop"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream")


def _build_anthropic_tool_stream(tool_name: str = "get_weather") -> StreamingResponse:
    async def body():
        yield b'event: message_start\n'
        yield (
            b'data: {"type":"message_start","message":'
            b'{"id":"msg_tool_1","type":"message","role":"assistant",'
            b'"model":"claude","content":[],"stop_reason":null,'
            b'"stop_sequence":null,"usage":{"input_tokens":10,"output_tokens":0}}}\n\n'
        )
        yield b'event: content_block_start\n'
        payload = {
            "type": "content_block_start",
            "index": 0,
            "content_block": {
                "type": "tool_use",
                "id": "toolu_42",
                "name": tool_name,
                "input": {},
            },
        }
        yield f"data: {json.dumps(payload, separators=(',', ':'))}\n\n".encode()
        yield b'event: content_block_delta\n'
        yield (
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"{\\"city\\":"}}\n\n'
        )
        yield b'event: content_block_delta\n'
        yield (
            b'data: {"type":"content_block_delta","index":0,'
            b'"delta":{"type":"input_json_delta","partial_json":"\\"Moscow\\"}"}}\n\n'
        )
        yield b'event: content_block_stop\n'
        yield b'data: {"type":"content_block_stop","index":0}\n\n'
        yield b'event: message_delta\n'
        yield (
            b'data: {"type":"message_delta","delta":{"stop_reason":"tool_use"},'
            b'"usage":{"output_tokens":5}}\n\n'
        )
        yield b'event: message_stop\n'
        yield b'data: {"type":"message_stop"}\n\n'

    return StreamingResponse(body(), media_type="text/event-stream")


def _parse_openai_sse_chunks(stream_text: str) -> list[dict]:
    chunks: list[dict] = []
    for line in stream_text.split("\n"):
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[len("data:"):].strip()
        if not data or data == "[DONE]":
            continue
        chunks.append(json.loads(data))
    return chunks


def _parse_anthropic_sse_events(stream_text: str) -> list[tuple[str | None, dict]]:
    events: list[tuple[str | None, dict]] = []
    for raw_event in stream_text.split("\n\n"):
        event_name: str | None = None
        data_lines: list[str] = []
        for line in raw_event.split("\n"):
            if line.startswith("event:"):
                event_name = line[len("event:"):].strip()
            elif line.startswith("data:"):
                data_lines.append(line[len("data:"):].lstrip())
        if not data_lines:
            continue
        payload = json.loads("\n".join(data_lines))
        events.append((event_name, payload))
    return events


class ProviderDetailsTypeTests(unittest.TestCase):
    def test_type_defaults_to_openai(self):
        provider = ProviderDetails(baseUrl="https://x.example", apikey="k")
        self.assertEqual(provider.type, "openai")

    def test_type_accepts_anthropic(self):
        provider = ProviderDetails(baseUrl="https://x.example", apikey="k", type="anthropic")
        self.assertEqual(provider.type, "anthropic")

    def test_unknown_type_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            ProviderDetails(baseUrl="https://x.example", apikey="k", type="bedrock")


class OpenAIRequestToAnthropicPayloadTests(unittest.TestCase):
    def test_system_role_moves_to_top_level_and_max_tokens_defaults(self):
        payload = {
            "model": "claude-sonnet-4-6",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
        }
        result = chat_module._openai_request_to_anthropic_payload(payload)
        self.assertEqual(result["system"], "Be concise.")
        self.assertEqual(
            result["messages"],
            [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        )
        self.assertEqual(result["max_tokens"], chat_module.ANTHROPIC_DEFAULT_MAX_TOKENS)
        self.assertEqual(result["max_tokens"], 32768)

    def test_assistant_tool_calls_become_tool_use_blocks(self):
        payload = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "Weather?"},
                {
                    "role": "assistant",
                    "content": "Checking.",
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "get_weather",
                                "arguments": json.dumps({"city": "Moscow"}),
                            },
                        }
                    ],
                },
                {
                    "role": "tool",
                    "tool_call_id": "call_1",
                    "content": "sunny",
                },
            ],
            "max_tokens": 100,
        }
        result = chat_module._openai_request_to_anthropic_payload(payload)
        assistant = result["messages"][1]
        self.assertEqual(assistant["role"], "assistant")
        self.assertEqual(assistant["content"][0], {"type": "text", "text": "Checking."})
        self.assertEqual(assistant["content"][1]["type"], "tool_use")
        self.assertEqual(assistant["content"][1]["id"], "call_1")
        self.assertEqual(assistant["content"][1]["name"], "get_weather")
        self.assertEqual(assistant["content"][1]["input"], {"city": "Moscow"})
        tool_result_msg = result["messages"][2]
        self.assertEqual(tool_result_msg["role"], "user")
        self.assertEqual(
            tool_result_msg["content"][0],
            {"type": "tool_result", "tool_use_id": "call_1", "content": "sunny"},
        )
        self.assertEqual(result["max_tokens"], 100)

    def test_tools_and_tool_choice_translate(self):
        payload = {
            "model": "claude",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "description": "Lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "lookup"}},
            "stop": ["STOP"],
        }
        result = chat_module._openai_request_to_anthropic_payload(payload)
        self.assertEqual(
            result["tools"],
            [
                {
                    "name": "lookup",
                    "description": "Lookup",
                    "input_schema": {"type": "object", "properties": {}},
                }
            ],
        )
        self.assertEqual(result["tool_choice"], {"type": "tool", "name": "lookup"})
        self.assertEqual(result["stop_sequences"], ["STOP"])

    def test_openai_tool_names_are_mapped_to_anthropic_contract(self):
        tool_name_map: dict[str, str] = {}
        payload = {
            "model": "claude",
            "messages": [
                {"role": "user", "content": "hi"},
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {
                                "name": "1bad_tool",
                                "arguments": json.dumps({"city": "Moscow"}),
                            },
                        }
                    ],
                },
            ],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "1bad_tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "1bad_tool"}},
        }

        result = chat_module._openai_request_to_anthropic_payload(
            payload,
            tool_name_map=tool_name_map,
        )

        self.assertEqual(tool_name_map, {"1bad_tool": "tool_1bad_tool"})
        self.assertEqual(result["messages"][1]["content"][0]["name"], "tool_1bad_tool")
        self.assertEqual(result["tools"][0]["name"], "tool_1bad_tool")
        self.assertEqual(result["tool_choice"], {"type": "tool", "name": "tool_1bad_tool"})

    def test_tool_choice_none_is_preserved_for_anthropic_provider(self):
        payload = {
            "model": "claude",
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "lookup",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": "none",
        }

        result = chat_module._openai_request_to_anthropic_payload(payload)

        self.assertEqual(result["tool_choice"], {"type": "none"})
        self.assertIn("tools", result)


class AnthropicResponseToOpenAITests(unittest.TestCase):
    def test_text_and_usage_round_trip(self):
        anthropic_response = {
            "id": "msg_1",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "Hello!"}],
            "stop_reason": "end_turn",
            "usage": {"input_tokens": 11, "output_tokens": 3},
        }
        openai_response = chat_module._anthropic_response_to_openai(
            anthropic_response, "gateway-model"
        )
        self.assertEqual(openai_response["id"], "msg_1")
        self.assertEqual(openai_response["object"], "chat.completion")
        self.assertEqual(openai_response["choices"][0]["message"]["content"], "Hello!")
        self.assertEqual(openai_response["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            openai_response["usage"],
            {"prompt_tokens": 11, "completion_tokens": 3, "total_tokens": 14},
        )

    def test_usage_includes_anthropic_cache_tokens(self):
        anthropic_response = {
            "id": "msg_cached",
            "type": "message",
            "role": "assistant",
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": "ok"}],
            "stop_reason": "end_turn",
            "usage": {
                "input_tokens": 13,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 13120,
                "output_tokens": 5,
            },
        }

        openai_response = chat_module._anthropic_response_to_openai(
            anthropic_response, "gateway-model"
        )

        self.assertEqual(
            openai_response["usage"],
            {
                "prompt_tokens": 13233,
                "completion_tokens": 5,
                "total_tokens": 13238,
                "prompt_tokens_details": {"cached_tokens": 13120},
            },
        )

    def test_tool_use_blocks_become_tool_calls_with_arguments_json(self):
        anthropic_response = {
            "content": [
                {"type": "text", "text": "Let me check"},
                {
                    "type": "tool_use",
                    "id": "toolu_42",
                    "name": "get_weather",
                    "input": {"city": "Moscow"},
                },
            ],
            "stop_reason": "tool_use",
            "usage": {"input_tokens": 5, "output_tokens": 6},
        }
        openai_response = chat_module._anthropic_response_to_openai(
            anthropic_response, "gateway-model"
        )
        message = openai_response["choices"][0]["message"]
        self.assertEqual(message["content"], "Let me check")
        tool_call = message["tool_calls"][0]
        self.assertEqual(tool_call["id"], "toolu_42")
        self.assertEqual(tool_call["function"]["name"], "get_weather")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"city": "Moscow"})
        self.assertEqual(openai_response["choices"][0]["finish_reason"], "tool_calls")


def _build_fake_config_loader(provider_type: str) -> Mock:
    loader = Mock()
    loader.providers_config = {
        "anthropic-upstream": SimpleNamespace(
            baseUrl="https://api.anthropic.example",
            apikey="ANTHROPIC-KEY",
            type=provider_type,
        )
    }
    loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "anthropic-upstream",
                    "model": "claude-sonnet-4-6",
                    "use_provider_order_as_fallback": False,
                }
            ],
            "rotate_models": False,
        }
    }
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    return loader


class DispatcherAnthropicProviderTests(unittest.TestCase):
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_client_routes_to_anthropic_provider_and_converts_response(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            {
                "id": "msg_abc",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Hello"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
            None,
        )

        openai_request = {
            "model": "gateway-model",
            "messages": [
                {"role": "system", "content": "Be concise."},
                {"role": "user", "content": "Hi"},
            ],
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
        self.assertEqual(response.status_code, 200)

        call_args = make_llm_request_mock.await_args
        target_url = call_args.args[1]
        headers = call_args.args[2]
        provider_payload = call_args.args[3]

        self.assertEqual(target_url, "https://api.anthropic.example/v1/messages")
        self.assertEqual(headers["x-api-key"], "ANTHROPIC-KEY")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("Authorization", headers)
        self.assertEqual(provider_payload["model"], "claude-sonnet-4-6")
        self.assertEqual(provider_payload["system"], "Be concise.")
        self.assertEqual(
            provider_payload["messages"],
            [{"role": "user", "content": [{"type": "text", "text": "Hi"}]}],
        )
        self.assertEqual(provider_payload["max_tokens"], chat_module.ANTHROPIC_DEFAULT_MAX_TOKENS)
        self.assertNotIn("stream", provider_payload)

        body = response.json()
        self.assertEqual(body["choices"][0]["message"]["content"], "Hello")
        self.assertEqual(body["choices"][0]["finish_reason"], "stop")
        self.assertEqual(
            body["usage"],
            {"prompt_tokens": 7, "completion_tokens": 2, "total_tokens": 9},
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_tool_name_is_mapped_for_anthropic_provider_and_restored_in_response(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            {
                "id": "msg_tool_abc",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_1",
                        "name": "tool_1bad_tool",
                        "input": {"city": "Moscow"},
                    }
                ],
                "stop_reason": "tool_use",
                "usage": {"input_tokens": 7, "output_tokens": 2},
            },
            None,
        )

        openai_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Weather?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "1bad_tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": {"type": "function", "function": {"name": "1bad_tool"}},
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(provider_payload["tools"][0]["name"], "tool_1bad_tool")
        self.assertEqual(provider_payload["tool_choice"], {"type": "tool", "name": "tool_1bad_tool"})

        body = response.json()
        tool_call = body["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "1bad_tool")
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"city": "Moscow"})

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_client_forwards_native_payload_without_lossy_roundtrip(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (
            {
                "id": "msg_abc",
                "type": "message",
                "role": "assistant",
                "model": "claude-sonnet-4-6",
                "content": [{"type": "text", "text": "Hi!"}],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 4, "output_tokens": 1},
            },
            None,
        )

        anthropic_request = {
            "model": "gateway-model",
            "system": [
                {
                    "type": "text",
                    "text": "You are helpful",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
        self.assertEqual(response.status_code, 200)

        call_args = make_llm_request_mock.await_args
        target_url = call_args.args[1]
        provider_payload = call_args.args[3]
        self.assertEqual(target_url, "https://api.anthropic.example/v1/messages")
        # Native fields (including cache_control) survive untouched.
        self.assertEqual(
            provider_payload["system"],
            [
                {
                    "type": "text",
                    "text": "You are helpful",
                    "cache_control": {"type": "ephemeral"},
                }
            ],
        )
        self.assertEqual(provider_payload["model"], "claude-sonnet-4-6")
        self.assertEqual(provider_payload["max_tokens"], 64)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_client_streams_from_anthropic_provider_as_openai_sse(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_anthropic_text_stream(), None)

        openai_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        chunks = _parse_openai_sse_chunks(response.text)
        # First chunk announces the assistant role.
        self.assertEqual(chunks[0]["choices"][0]["delta"].get("role"), "assistant")
        # Text deltas flow through as OpenAI ``content``.
        content_texts = [
            chunk["choices"][0]["delta"].get("content")
            for chunk in chunks
            if chunk["choices"][0]["delta"].get("content")
        ]
        self.assertEqual("".join(content_texts), "Hello")
        # Final chunk carries finish_reason + usage.
        final_chunk = chunks[-1]
        self.assertEqual(final_chunk["choices"][0]["finish_reason"], "stop")
        self.assertEqual(final_chunk["usage"]["prompt_tokens"], 9)
        self.assertEqual(final_chunk["usage"]["completion_tokens"], 4)
        self.assertTrue(response.text.rstrip().endswith("data: [DONE]"))

        # Upstream was reached — dispatcher did NOT short-circuit.
        make_llm_request_mock.assert_awaited_once()

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_client_stream_usage_includes_anthropic_cache_tokens(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_anthropic_cached_text_stream(), None)

        openai_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Hi"}],
            "stream": True,
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        final_chunk = _parse_openai_sse_chunks(response.text)[-1]
        self.assertEqual(
            final_chunk["usage"],
            {
                "prompt_tokens": 13233,
                "completion_tokens": 5,
                "total_tokens": 13238,
                "prompt_tokens_details": {"cached_tokens": 13120},
            },
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_client_receives_tool_call_deltas_from_anthropic_stream(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_anthropic_tool_stream(), None)

        openai_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Weather?"}],
            "stream": True,
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        chunks = _parse_openai_sse_chunks(response.text)

        tool_call_id = None
        tool_call_name = None
        arguments_parts: list[str] = []
        for chunk in chunks:
            tool_calls = chunk["choices"][0]["delta"].get("tool_calls")
            if not tool_calls:
                continue
            for delta_entry in tool_calls:
                if delta_entry.get("id"):
                    tool_call_id = delta_entry["id"]
                function = delta_entry.get("function") or {}
                if function.get("name"):
                    tool_call_name = function["name"]
                if function.get("arguments"):
                    arguments_parts.append(function["arguments"])

        self.assertEqual(tool_call_id, "toolu_42")
        self.assertEqual(tool_call_name, "get_weather")
        self.assertEqual(json.loads("".join(arguments_parts)), {"city": "Moscow"})
        self.assertEqual(chunks[-1]["choices"][0]["finish_reason"], "tool_calls")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_openai_client_stream_restores_mapped_tool_name_from_anthropic_provider(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_anthropic_tool_stream("tool_1bad_tool"), None)

        openai_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Weather?"}],
            "stream": True,
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "1bad_tool",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=openai_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        provider_payload = make_llm_request_mock.await_args.args[3]
        self.assertEqual(provider_payload["tools"][0]["name"], "tool_1bad_tool")

        chunks = _parse_openai_sse_chunks(response.text)
        tool_call_name = None
        for chunk in chunks:
            tool_calls = chunk["choices"][0]["delta"].get("tool_calls")
            if not tool_calls:
                continue
            function = tool_calls[0].get("function") or {}
            if function.get("name"):
                tool_call_name = function["name"]

        self.assertEqual(tool_call_name, "1bad_tool")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_client_receives_native_anthropic_stream_without_roundtrip(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = _build_fake_config_loader("anthropic")
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = (_build_anthropic_text_stream(), None)

        anthropic_request = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 32,
            "stream": True,
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json=anthropic_request,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        events = _parse_anthropic_sse_events(response.text)
        event_names = [name for name, _ in events]
        # Native Anthropic SSE events survive unchanged (no OpenAI→Anthropic
        # reconstruction from ``_openai_stream_to_anthropic``).
        self.assertIn("message_start", event_names)
        self.assertIn("content_block_delta", event_names)
        self.assertIn("message_stop", event_names)
        text_parts = [
            payload["delta"]["text"]
            for name, payload in events
            if name == "content_block_delta"
            and payload.get("delta", {}).get("type") == "text_delta"
        ]
        self.assertEqual("".join(text_parts), "Hello")


class ProviderModelsServiceAnthropicTests(unittest.TestCase):
    def test_fetch_models_uses_v1_models_and_anthropic_headers(self):
        service = ProviderModelsService()
        provider_config = ProviderDetails(
            baseUrl="https://api.anthropic.example",
            apikey="ANTHROPIC-KEY",
            type="anthropic",
        )

        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {
            "data": [
                {"id": "claude-sonnet-4-6", "type": "model"},
                {"id": "claude-haiku-4-5", "type": "model"},
            ]
        }
        fake_client = Mock()
        fake_client.get = AsyncMock(return_value=fake_response)

        models = asyncio.run(
            service._fetch_models("anthropic-upstream", provider_config, fake_client)
        )
        self.assertEqual(models, ["claude-sonnet-4-6", "claude-haiku-4-5"])

        call = fake_client.get.await_args
        self.assertEqual(call.args[0], "https://api.anthropic.example/v1/models")
        headers = call.kwargs["headers"]
        self.assertEqual(headers["x-api-key"], "ANTHROPIC-KEY")
        self.assertEqual(headers["anthropic-version"], ANTHROPIC_API_VERSION)
        self.assertNotIn("Authorization", headers)

    def test_fetch_models_openai_provider_unchanged(self):
        service = ProviderModelsService()
        provider_config = ProviderDetails(
            baseUrl="https://api.openai.example",
            apikey="OPENAI-KEY",
        )

        fake_response = Mock()
        fake_response.status_code = 200
        fake_response.json.return_value = {"data": [{"id": "gpt-4o"}]}
        fake_client = Mock()
        fake_client.get = AsyncMock(return_value=fake_response)

        models = asyncio.run(
            service._fetch_models("openai-upstream", provider_config, fake_client)
        )
        self.assertEqual(models, ["gpt-4o"])

        call = fake_client.get.await_args
        self.assertEqual(call.args[0], "https://api.openai.example/models")
        headers = call.kwargs["headers"]
        self.assertEqual(headers["Authorization"], "Bearer OPENAI-KEY")
        self.assertNotIn("x-api-key", headers)


if __name__ == "__main__":
    unittest.main()
