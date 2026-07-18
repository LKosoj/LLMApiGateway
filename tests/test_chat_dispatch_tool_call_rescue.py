import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from tests.chat_accounting_test_support import install_main_chat_accounting_double


WEATHER_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "parameters": {
                "type": "object",
                "properties": {"location": {"type": "string"}},
            },
        },
    }
]


def _tool_request_payload(*, model: str = "gateway-model") -> dict:
    return {
        "model": model,
        "messages": [{"role": "user", "content": "What's the weather in Paris?"}],
        "tools": WEATHER_TOOLS,
    }


def _valid_completion_response(response_id: str, content: str = "ok") -> dict:
    # Choices-shaped: a bare {"id": ...} would itself be flagged as an
    # empty_completion by Package D's degenerate-response detector.
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


def _inline_dialect_response(response_id: str) -> dict:
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": '<function=get_weather>{"location": "Paris"}</function>',
                },
            }
        ],
    }


def _unparseable_dialect_response(response_id: str) -> dict:
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    # Marker present (function tag) but no closing tag: this
                    # is a detected-but-unparsable dialect.
                    "content": '<function=get_weather>{"location": "Paris"}',
                    "role": "assistant",
                },
            }
        ],
    }


def _anthropic_shaped_dialect_response(response_id: str) -> dict:
    # Native Anthropic /v1/messages shape (content blocks), not the OpenAI
    # choices shape: the rescue helper only ever runs on OpenAI-shaped
    # responses, and this dialect text must survive the Anthropic->OpenAI
    # conversion completely untouched.
    return {
        "id": response_id,
        "model": "provider-model",
        "stop_reason": "end_turn",
        "content": [
            {
                "type": "text",
                "text": '<function=get_weather>{"location": "Paris"}</function>',
            }
        ],
        "usage": {"input_tokens": 10, "output_tokens": 5},
    }


def _structural_tool_call_response(response_id: str, arguments: str) -> dict:
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "tool_calls",
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather", "arguments": arguments},
                        }
                    ],
                },
            }
        ],
    }


class ToolCallRescueDispatchTests(unittest.TestCase):
    """Package E: tool-call rescue is strictly opt-in in the non-streaming path."""

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

    def _one_model_config_loader(self, *, tool_call_rescue: bool, provider_type: str = "openai") -> Mock:
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
                type=provider_type,
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
                "tool_call_rescue": tool_call_rescue,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        return fake_config_loader

    def _two_model_config_loader(self, *, tool_call_rescue: bool) -> Mock:
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "first-provider": SimpleNamespace(
                baseUrl="https://first.example",
                apikey="DIRECT-KEY",
            ),
            "second-provider": SimpleNamespace(
                baseUrl="https://second.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake_config_loader.fallback_rules = {
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
                "tool_call_rescue": tool_call_rescue,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        return fake_config_loader

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_disabled_leaves_inline_dialect_text_untouched(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._one_model_config_loader(tool_call_rescue=False)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_inline_dialect_response("no-rescue"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        message = response.json()["choices"][0]["message"]
        self.assertEqual(
            message["content"],
            '<function=get_weather>{"location": "Paris"}</function>',
        )
        self.assertNotIn("tool_calls", message)
        self.assertEqual(make_llm_request_mock.await_count, 1)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_enabled_rescues_inline_dialect_into_structured_tool_calls(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._one_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_inline_dialect_response("rescued"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        message = body["choices"][0]["message"]
        self.assertEqual(body["choices"][0]["finish_reason"], "tool_calls")
        self.assertEqual(len(message["tool_calls"]), 1)
        tool_call = message["tool_calls"][0]
        self.assertEqual(tool_call["function"]["name"], "get_weather")
        self.assertEqual(tool_call["function"]["arguments"], '{"location": "Paris"}')
        self.assertEqual(make_llm_request_mock.await_count, 1)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_enabled_repairs_existing_structural_tool_calls(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._one_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        # Double-encoded arguments: a JSON string containing a JSON object.
        double_encoded_arguments = json.dumps(json.dumps({"location": "Paris"}))
        make_llm_request_mock.return_value = (
            _structural_tool_call_response("repaired", double_encoded_arguments),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        tool_call = response.json()["choices"][0]["message"]["tool_calls"][0]
        self.assertEqual(json.loads(tool_call["function"]["arguments"]), {"location": "Paris"})

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_unparsed_dialect_fails_over_without_cooldown_next_request_retries_same_model_first(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._two_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (_unparseable_dialect_response("bad-dialect-1"), None),
            (_valid_completion_response("second-provider-ok"), None),
            (_valid_completion_response("first-provider-ok-second-request"), None),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                first_response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                second_response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json()["id"], "second-provider-ok")
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json()["id"], "first-provider-ok-second-request")
        self.assertEqual(make_llm_request_mock.await_count, 3)
        attempted_models = [
            call.args[3]["model"] for call in make_llm_request_mock.await_args_list
        ]
        # "first-model" is retried first again on the second request: the
        # unparsed-dialect failure never scheduled a cooldown (skipBench).
        self.assertEqual(
            attempted_models,
            ["first-model", "second-model", "first-model"],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_all_candidates_unparsed_dialect_returns_503_with_behavior_class_in_attempts(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._two_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (_unparseable_dialect_response("bad-1"), None),
            (_unparseable_dialect_response("bad-2"), None),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(len(body["attempts"]), 2)
        for entry in body["attempts"]:
            self.assertEqual(entry["error_class"], "unparsed_tool_call_dialect")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_enabled_without_tools_in_request_is_a_no_op(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._one_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (_inline_dialect_response("no-tools-in-request"), None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        message = response.json()["choices"][0]["message"]
        self.assertEqual(
            message["content"],
            '<function=get_weather>{"location": "Paris"}</function>',
        )
        self.assertNotIn("tool_calls", message)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_enabled_for_anthropic_provider_is_a_no_op(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._one_model_config_loader(
            tool_call_rescue=True, provider_type="anthropic"
        )
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            _anthropic_shaped_dialect_response("anthropic-no-rescue"),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=_tool_request_payload(),
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        message = response.json()["choices"][0]["message"]
        self.assertEqual(
            message["content"],
            '<function=get_weather>{"location": "Paris"}</function>',
        )
        self.assertNotIn("tool_calls", message)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_flag_enabled_for_streaming_anthropic_provider_is_a_no_op(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        # Review finding #1: the non-stream gate excludes is_anthropic_provider
        # (see test_flag_enabled_for_anthropic_provider_is_a_no_op above); the
        # streaming gate in _finalize_chat_success_response must match it, or
        # "Anthropic provider + OpenAI client + stream=true" would be the one
        # combination where rescue silently stays on. The client here talks
        # OpenAI (/v1/chat/completions, not /v1/messages), so
        # client_expects_anthropic is False and the native Anthropic SSE
        # stream below is converted to OpenAI chunk shape by
        # _anthropic_stream_to_openai before _finalize_chat_success_response
        # ever sees it -- exactly the scenario where a shape-based rescue gate
        # would incorrectly still fire without the explicit provider check.
        config_loader_cls.return_value = self._one_model_config_loader(
            tool_call_rescue=True, provider_type="anthropic"
        )
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        dialect_text = '<function=get_weather>{"location": "Paris"}</function>'

        async def stream_body():
            yield (
                b'event: message_start\n'
                b'data: {"type":"message_start","message":{"id":"msg_s","type":"message",'
                b'"role":"assistant","model":"provider-model","content":[],'
                b'"stop_reason":null,"usage":{"input_tokens":5,"output_tokens":0}}}\n\n'
            )
            yield (
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,'
                b'"content_block":{"type":"text","text":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":' + json.dumps(dialect_text).encode("utf-8") + b'}}\n\n'
            )
            yield (
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":0}\n\n'
            )
            yield (
                b'event: message_delta\n'
                b'data: {"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                b'"usage":{"output_tokens":7}}\n\n'
            )
            yield b'event: message_stop\ndata: {"type":"message_stop"}\n\n'

        make_llm_request_mock.return_value = (
            StreamingResponse(stream_body(), media_type="text/event-stream"),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={**_tool_request_payload(), "stream": True},
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        streamed_content = []
        saw_tool_calls = False
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                if isinstance(content, str):
                    streamed_content.append(content)
                if delta.get("tool_calls"):
                    saw_tool_calls = True

        # Byte-for-byte transparent: the raw dialect text reaches the client
        # untouched, no tool_calls were synthesized.
        self.assertEqual("".join(streamed_content), dialect_text)
        self.assertFalse(saw_tool_calls)

    def _one_model_sub_provider_config_loader(self, *, tool_call_rescue: bool) -> Mock:
        # Mirrors _one_model_config_loader, but routes through the
        # sub-provider ordering loop (the "else" branch of
        # attempt_model_fallback_rule's use_provider_order_as_fallback
        # check) instead of the plain path: non-empty "providers_order" +
        # "use_provider_order_as_fallback": True. This branch is
        # structurally unreachable for a native Anthropic provider (see the
        # "or is_anthropic_provider" guard in chat_dispatch.py), so
        # provider_type stays "openai".
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "test-provider": SimpleNamespace(
                baseUrl="https://provider.example",
                apikey="DIRECT-KEY",
                type="openai",
            )
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "test-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": True,
                        "providers_order": ["sub-provider-a"],
                    }
                ],
                "rotate_models": False,
                "tool_call_rescue": tool_call_rescue,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        return fake_config_loader

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_sub_provider_path_streaming_dialect_is_rescued_into_delta_tool_calls(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        # Review finding #2 (second follow-up round): the sub-provider
        # ordering success branch of attempt_model_fallback_rule now sets
        # request.state.llmgateway_response_is_anthropic_provider explicitly
        # (see the sibling assignment right after
        # request.state.llmgateway_provider_model in the "else" branch).
        # That branch is only ever reachable when is_anthropic_provider is
        # False, so rescue must fire exactly like the plain-path e2e test
        # (test_e2e_streaming_dialect_is_rescued_into_delta_tool_calls) --
        # this guards against a regression that accidentally left the flag
        # at its getattr(..., False) default for the wrong reason, or a
        # future change that starts gating this branch on it incorrectly.
        config_loader_cls.return_value = self._one_model_sub_provider_config_loader(
            tool_call_rescue=True
        )
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            yield b'data: {"id":"s1","choices":[{"index":0,"delta":{"role":"assistant","content":"<function=get_weather>{\\"location\\": \\"Paris\\"}</function>"}}]}\n\n'
            yield b'data: {"id":"s1","choices":[{"index":0,"finish_reason":"stop","delta":{}}]}\n\n'
            yield b"data: [DONE]\n\n"

        make_llm_request_mock.return_value = (
            StreamingResponse(stream_body(), media_type="text/event-stream"),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={**_tool_request_payload(), "stream": True},
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        tool_call_deltas = []
        finish_reasons = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                delta = choice.get("delta", {})
                for tool_call_delta in delta.get("tool_calls", []) or []:
                    tool_call_deltas.append(tool_call_delta)
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None:
                    finish_reasons.append(finish_reason)

        self.assertTrue(tool_call_deltas, "expected at least one delta.tool_calls chunk")
        function_names = {d["function"]["name"] for d in tool_call_deltas if d.get("function", {}).get("name")}
        self.assertEqual(function_names, {"get_weather"})
        arguments = "".join(
            d.get("function", {}).get("arguments", "") for d in tool_call_deltas
        )
        self.assertEqual(json.loads(arguments), {"location": "Paris"})
        self.assertIn("tool_calls", finish_reasons)
        self.assertIn("data: [DONE]", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_e2e_streaming_dialect_is_rescued_into_delta_tool_calls(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        # Review finding #3: unlike test_chat_streaming_tool_call_rescue.py
        # (which calls _sanitize_openai_stream_tool_call_rescue directly),
        # this drives the real /v1/chat/completions dispatch path end to end
        # -- covering the wiring the unit tests can't: the "tool_call_rescue"
        # flag reaching dispatch_chat_request from fallback_rules,
        # _request_has_tools() gating on the actual request body, and
        # build_tool_schema_map() being built from the real "tools" payload
        # -- so a regression at any of those glue points (flag name typo,
        # wrong tools lookup, wrong condition) fails this test even if the
        # sanitizer itself is untouched.
        config_loader_cls.return_value = self._one_model_config_loader(tool_call_rescue=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            # The dialect marker itself is split across chunks, same as a
            # real token-by-token provider stream.
            yield b'data: {"id":"s1","choices":[{"index":0,"delta":{"role":"assistant","content":"<funct"}}]}\n\n'
            yield b'data: {"id":"s1","choices":[{"index":0,"delta":{"content":"ion=get_weather>{\\"location\\": \\"Paris\\"}</function>"}}]}\n\n'
            yield b'data: {"id":"s1","choices":[{"index":0,"finish_reason":"stop","delta":{}}]}\n\n'
            yield b"data: [DONE]\n\n"

        make_llm_request_mock.return_value = (
            StreamingResponse(stream_body(), media_type="text/event-stream"),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={**_tool_request_payload(), "stream": True},
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        tool_call_deltas = []
        finish_reasons = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                delta = choice.get("delta", {})
                for tool_call_delta in delta.get("tool_calls", []) or []:
                    tool_call_deltas.append(tool_call_delta)
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None:
                    finish_reasons.append(finish_reason)

        self.assertTrue(tool_call_deltas, "expected at least one delta.tool_calls chunk")
        function_names = {d["function"]["name"] for d in tool_call_deltas if d.get("function", {}).get("name")}
        self.assertEqual(function_names, {"get_weather"})
        arguments = "".join(
            d.get("function", {}).get("arguments", "") for d in tool_call_deltas
        )
        self.assertEqual(json.loads(arguments), {"location": "Paris"})
        self.assertIn("tool_calls", finish_reasons)
        self.assertIn("data: [DONE]", response_text)


if __name__ == "__main__":
    unittest.main()
