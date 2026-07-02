import json
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.db.api_keys_db import ApiKeyRecord
from llm_gateway_core.services.upstream_routing_state import fingerprint_api_key


class ChatFallbackTests(unittest.TestCase):
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_unknown_model_uses_configured_fallback_provider(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "openrouter": SimpleNamespace(
                baseUrl="https://openrouter.example",
                apikey="DIRECT-KEY",
            ),
            "backup-provider": SimpleNamespace(
                baseUrl="https://backup.example",
                apikey="DIRECT-KEY",
            ),
        }
        fake_config_loader.fallback_rules = {}
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = ({"id": "fallback-success"}, None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch.object(main.settings, "fallback_provider", "openrouter"):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={"model": "unknown-model", "messages": [{"role": "user", "content": "hello"}]},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "fallback-success"})
        self.assertEqual(
            make_llm_request_mock.await_args.args[1],
            "https://openrouter.example/chat/completions",
        )
        self.assertEqual(
            make_llm_request_mock.await_args.args[3]["model"],
            "unknown-model",
        )

    @patch("llm_gateway_core.api.v1.chat.logging.warning")
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_failed_attempt_log_can_include_messages_when_debug_flag_enabled(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        logging_warning_mock,
    ):
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
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "invalid params, invalid chat setting (2013)"),
            ({"id": "fallback-success"}, None),
        ]

        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "very secret prompt"}],
            "tool_choice": "auto",
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch.object(main.settings, "log_fallback_full_messages", True):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json=request_payload,
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

        self.assertEqual(response.status_code, 200)
        failed_attempt_calls = [
            call for call in logging_warning_mock.call_args_list if "Failed attempt with model" in call.args[0]
        ]
        self.assertEqual(len(failed_attempt_calls), 1)
        logged_payload = failed_attempt_calls[0].args[5]
        self.assertIn("messages", logged_payload)
        self.assertEqual(logged_payload["messages"][0]["content"], "very secret prompt")
        raw_message_calls = [
            call for call in logging_warning_mock.call_args_list if "Failed attempt raw messages for model" in call.args[0]
        ]
        self.assertEqual(len(raw_message_calls), 1)
        self.assertEqual(
            raw_message_calls[0].args[3],
            [{"role": "user", "content": "very secret prompt"}],
        )

    @patch("llm_gateway_core.api.v1.chat.logging.warning")
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_failed_attempt_log_reports_missing_messages_key_when_debug_flag_enabled(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        logging_warning_mock,
    ):
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
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "invalid params, invalid chat setting (2013)"),
            ({"id": "fallback-success"}, None),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with patch.object(main.settings, "log_fallback_full_messages", True):
                with TestClient(main.app) as client:
                    response = client.post(
                        "/v1/chat/completions",
                        json={"model": "gateway-model"},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

        self.assertEqual(response.status_code, 200)
        missing_messages_calls = [
            call
            for call in logging_warning_mock.call_args_list
            if "has no 'messages' key" in call.args[0]
        ]
        self.assertEqual(len(missing_messages_calls), 1)
        self.assertEqual(
            missing_messages_calls[0].args[3],
            ["model"],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_temporarily_unavailable_model_is_skipped_during_cooldown(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
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
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "The engine is currently overloaded, please try again later"),
            ({"id": "first-request-ok"}, None),
            ({"id": "second-request-ok"}, None),
        ]

        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "hello"}],
        }

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                first_response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                second_response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(first_response.json(), {"id": "first-request-ok"})
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(second_response.json(), {"id": "second-request-ok"})
        self.assertEqual(make_llm_request_mock.await_count, 3)
        attempted_models = [
            call.args[3]["model"]
            for call in make_llm_request_mock.await_args_list
        ]
        self.assertEqual(
            attempted_models,
            ["first-model", "second-model", "second-model"],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_json_object_response_removes_markdown_wrapper(
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
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            {
                "id": "json-success",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "<think>\ninternal reasoning\n</think>\n```json\n{\"ok\":true}\n```",
                        },
                    }
                ],
            },
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            '{"ok":true}',
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_streaming_json_object_response_removes_markdown_wrapper(
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
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"role":"assistant","content":"<thi"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"nk>hidden reasoning</think>```"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"json\\n{"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"\\"ok\\":true}\\n"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"```"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"finish_reason":"stop"}]}\n\n'
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
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        streamed_content = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                if isinstance(content, str):
                    streamed_content.append(content)

        self.assertNotIn("```json", response_text)
        self.assertNotIn('\\"content":"```"', response_text)
        self.assertNotIn("<think>", response_text)
        self.assertNotIn("hidden reasoning", response_text)
        self.assertEqual("".join(streamed_content), '{"ok":true}')
        self.assertIn("data: [DONE]", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_streaming_json_object_sanitizes_final_unterminated_event(
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
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"```json\\n{\\"ok\\":true}\\n"}}]}\n\n'
            yield b'data: {"id":"stream-json-1","created":1735689600,"choices":[{"delta":{"content":"```"}}]}'

        make_llm_request_mock.return_value = (
            StreamingResponse(stream_body(), media_type="text/event-stream"),
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                with client.stream(
                    "POST",
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn("```json", response_text)
        self.assertNotIn('\\"content":"```"', response_text)
        streamed_content = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                content = choice.get("delta", {}).get("content")
                if isinstance(content, str):
                    streamed_content.append(content)
        self.assertEqual("".join(streamed_content), '{"ok":true}')

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_non_json_response_preserves_think_tags_when_flag_disabled(
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
                "strip_think_tags": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            {
                "id": "chat-success",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "<think>hidden reasoning</think>Visible answer",
                        },
                    }
                ],
            },
            None,
        )

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
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            "<think>hidden reasoning</think>Visible answer",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_non_json_response_strips_think_tags_when_flag_enabled(
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
                "strip_think_tags": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            {
                "id": "chat-success",
                "choices": [
                    {
                        "finish_reason": "stop",
                        "message": {
                            "role": "assistant",
                            "content": "<think>hidden reasoning</think>Visible answer",
                        },
                    }
                ],
            },
            None,
        )

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
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Visible answer")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_streaming_non_json_response_strips_think_tags_when_flag_enabled(
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
                "strip_think_tags": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            yield b'data: {"id":"stream-1","created":1735689600,"choices":[{"delta":{"role":"assistant","content":"<thi"}}]}\n\n'
            yield b'data: {"id":"stream-1","created":1735689600,"choices":[{"delta":{"content":"nk>hidden reasoning</think>Visible"}}]}\n\n'
            yield b'data: {"id":"stream-1","created":1735689600,"choices":[{"delta":{"content":" answer"}}]}\n\n'
            yield b'data: {"id":"stream-1","created":1735689600,"choices":[{"finish_reason":"stop"}]}\n\n'
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
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        streamed_content = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            for choice in payload.get("choices", []):
                delta = choice.get("delta", {})
                content = delta.get("content")
                if isinstance(content, str):
                    streamed_content.append(content)

        self.assertNotIn("<think>", response_text)
        self.assertNotIn("hidden reasoning", response_text)
        self.assertEqual("".join(streamed_content), "Visible answer")
        self.assertIn("data: [DONE]", response_text)

    def _anthropic_provider_config_loader(self, *, strip_think_tags: bool) -> Mock:
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {
            "anthropic-provider": SimpleNamespace(
                baseUrl="https://anthropic.example",
                apikey="DIRECT-KEY",
                type="anthropic",
            )
        }
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "anthropic-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
                "strip_think_tags": strip_think_tags,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        return fake_config_loader

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_raw_response_preserves_think_tags_when_flag_disabled(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._anthropic_provider_config_loader(
            strip_think_tags=False
        )

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            {
                "id": "msg_raw",
                "type": "message",
                "role": "assistant",
                "model": "provider-model",
                "content": [
                    {"type": "text", "text": "<think>hidden reasoning</think>Visible answer"}
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json={
                        "model": "gateway-model",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["content"][0]["text"],
            "<think>hidden reasoning</think>Visible answer",
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_raw_response_strips_think_tags_when_flag_enabled(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._anthropic_provider_config_loader(
            strip_think_tags=True
        )

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (
            {
                "id": "msg_raw",
                "type": "message",
                "role": "assistant",
                "model": "provider-model",
                "content": [
                    {"type": "text", "text": "<think>hidden reasoning</think>Visible answer"}
                ],
                "stop_reason": "end_turn",
                "usage": {"input_tokens": 5, "output_tokens": 7},
            },
            None,
        )

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/messages",
                    json={
                        "model": "gateway-model",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["content"][0]["text"], "Visible answer")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_raw_streaming_strips_think_tags_when_flag_enabled(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._anthropic_provider_config_loader(
            strip_think_tags=True
        )

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

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
            # <think> tag deliberately split across two text deltas.
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"<thi"}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"nk>hidden reasoning</think>Visible"}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":" answer"}}\n\n'
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
                    "/v1/messages",
                    json={
                        "model": "gateway-model",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        streamed_text = []
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            if payload.get("type") != "content_block_delta":
                continue
            delta = payload.get("delta", {})
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                streamed_text.append(delta["text"])

        self.assertNotIn("<think>", response_text)
        self.assertNotIn("hidden reasoning", response_text)
        self.assertEqual("".join(streamed_text), "Visible answer")
        # The native Anthropic lifecycle must remain intact.
        self.assertIn("event: content_block_stop", response_text)
        self.assertIn("event: message_stop", response_text)

    @staticmethod
    def _collect_anthropic_text_deltas(response_text: str) -> dict[int, str]:
        per_index: dict[int, list[str]] = {}
        for line in response_text.splitlines():
            if not line.startswith("data: {"):
                continue
            payload = json.loads(line[len("data: "):])
            if payload.get("type") != "content_block_delta":
                continue
            delta = payload.get("delta", {})
            if delta.get("type") == "text_delta" and isinstance(delta.get("text"), str):
                per_index.setdefault(payload.get("index", 0), []).append(delta["text"])
        return {index: "".join(parts) for index, parts in per_index.items()}

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_raw_streaming_drops_unclosed_think_block(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._anthropic_provider_config_loader(
            strip_think_tags=True
        )

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            yield (
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,'
                b'"content_block":{"type":"text","text":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"visible "}}\n\n'
            )
            # A <think> that never receives its closing tag before the block ends.
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"<think>secret reasoning"}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":" still secret"}}\n\n'
            )
            yield (
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":0}\n\n'
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
                    "/v1/messages",
                    json={
                        "model": "gateway-model",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        per_index = self._collect_anthropic_text_deltas(response_text)
        self.assertEqual(per_index.get(0), "visible ")
        self.assertNotIn("secret", response_text)
        self.assertNotIn("<think>", response_text)
        self.assertIn("event: content_block_stop", response_text)
        self.assertIn("event: message_stop", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_anthropic_raw_streaming_strips_think_per_block_index(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._anthropic_provider_config_loader(
            strip_think_tags=True
        )

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def stream_body():
            # Two independent text blocks (index 0 and index 1), each with its own
            # think block — proves per-content-block-index state isolation.
            yield (
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":0,'
                b'"content_block":{"type":"text","text":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":0,'
                b'"delta":{"type":"text_delta","text":"<think>secret0</think>Alpha"}}\n\n'
            )
            yield (
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":0}\n\n'
            )
            yield (
                b'event: content_block_start\n'
                b'data: {"type":"content_block_start","index":1,'
                b'"content_block":{"type":"text","text":""}}\n\n'
            )
            yield (
                b'event: content_block_delta\n'
                b'data: {"type":"content_block_delta","index":1,'
                b'"delta":{"type":"text_delta","text":"Beta<think>secret1</think>"}}\n\n'
            )
            yield (
                b'event: content_block_stop\n'
                b'data: {"type":"content_block_stop","index":1}\n\n'
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
                    "/v1/messages",
                    json={
                        "model": "gateway-model",
                        "max_tokens": 100,
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                ) as response:
                    response_text = response.read().decode("utf-8")

        self.assertEqual(response.status_code, 200)
        per_index = self._collect_anthropic_text_deltas(response_text)
        self.assertEqual(per_index.get(0), "Alpha")
        self.assertEqual(per_index.get(1), "Beta")
        self.assertNotIn("secret0", response_text)
        self.assertNotIn("secret1", response_text)
        self.assertNotIn("<think>", response_text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_retry_preserves_payload_between_attempts(
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
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_payloads = []

        async def fake_make_llm_request(_client, _target_url, _headers, payload, _is_streaming):
            seen_payloads.append(copy.deepcopy(payload))
            if len(seen_payloads) == 1:
                return None, "temporary failure"
            return {"id": "retry-success"}, None

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
        self.assertEqual(response.json(), {"id": "retry-success"})
        self.assertEqual(request_payload, original_payload)
        self.assertEqual(len(seen_payloads), 2)
        self.assertEqual(seen_payloads[0], seen_payloads[1])
        self.assertEqual(seen_payloads[0]["messages"], original_payload["messages"])
        self.assertEqual(seen_payloads[0]["metadata"], original_payload["metadata"])

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.ModelRotationDB", return_value=Mock(get_next_model_index=AsyncMock(return_value=0)))
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_model_rotation_uses_normalized_api_key_for_valid_bearer_variants(
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
                        "model": "provider-model-1",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model-2",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = ({"id": "rotation-success"}, None)

        valid_headers = [
            {"Authorization": "bearer test-gateway-key"},
            {"Authorization": "Bearer     test-gateway-key"},
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                for headers in valid_headers:
                    with self.subTest(headers=headers):
                        get_next_model_index_mock.reset_mock()
                        response = client.post(
                            "/v1/chat/completions",
                            json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                            headers=headers,
                        )

                        self.assertEqual(response.status_code, 200)
                        self.assertEqual(response.json(), {"id": "rotation-success"})
                        self.assertEqual(
                            get_next_model_index_mock.call_args.kwargs["api_key"],
                            f"master:{fingerprint_api_key('test-gateway-key')}",
                        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.ModelRotationDB", return_value=Mock(get_next_model_index=AsyncMock(return_value=0)))
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_model_rotation_uses_virtual_key_id_scope(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        model_rotation_db_cls,
        make_llm_request_mock,
    ):
        class FakeApiKeysDB:
            def __init__(self, record):
                self.record = record

            def get_by_key(self, api_key):
                return self.record if api_key == self.record.api_key else None

            def get_by_id(self, key_id):
                return self.record if key_id == self.record.id else None

            def reset_due_budgets(self):
                return []

            def record_spent(self, key_id, cost):
                return None

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
                        "model": "provider-model-1",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model-2",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.return_value = ({"id": "rotation-success"}, None)
        record = ApiKeyRecord(
            id=123,
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
                        json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                        headers={"Authorization": "Bearer lgk_virtual"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "rotation-success"})
        self.assertEqual(get_next_model_index_mock.call_args.kwargs["api_key"], "user:123")

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.ModelRotationDB", return_value=Mock(get_next_model_index=AsyncMock(return_value=1)))
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_model_rotation_calls_async_db_method_directly(
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
                        "model": "provider-model-1",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "provider-model-2",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": True,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = ({"id": "rotation-success"}, None)

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        get_next_model_index_mock.assert_awaited_once()
        self.assertEqual(make_llm_request_mock.await_args.args[3]["model"], "provider-model-2")

    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_missing_provider_returns_controlled_5xx_error(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
    ):
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {}
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "missing-provider",
                        "model": "provider-model",
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertGreaterEqual(response.status_code, 500)
        self.assertLess(response.status_code, 600)
        self.assertIn("Configured provider is unavailable", response.json()["detail"])
        self.assertNotIn("Traceback", response.text)
        self.assertNotIn("AttributeError", response.text)

    @patch("llm_gateway_core.api.v1.chat.asyncio.sleep", new_callable=AsyncMock)
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_null_retry_delay_uses_safe_normalized_value(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        sleep_mock,
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
                        "retry_delay": None,
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        make_llm_request_mock.side_effect = [
            (None, "temporary failure"),
            ({"id": "retry-success"}, None),
        ]

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "retry-success"})
        self.assertEqual(make_llm_request_mock.await_count, 2)
        sleep_mock.assert_not_awaited()

    @patch("llm_gateway_core.api.v1.chat.asyncio.sleep", new_callable=AsyncMock)
    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_chain_attempt_budget_exhaustion_does_not_sleep_before_exit(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
        sleep_mock,
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
                        "retry_delay": 10,
                        "use_provider_order_as_fallback": False,
                    }
                ],
                "rotate_models": False,
                "max_total_attempts": 1,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.return_value = (None, "temporary failure")

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(make_llm_request_mock.await_count, 1)
        sleep_mock.assert_not_awaited()

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_context_overflow_switches_to_dedicated_fallback_before_next_regular_model(
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
                        "retry_count": 1,
                        "retry_delay": 0,
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "regular-backup-model",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "context_overflow_fallback": {
                    "provider": "test-provider",
                    "model": "large-context-model",
                    "use_provider_order_as_fallback": False,
                },
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_models: list[str] = []

        async def fake_make_llm_request(_client, _target_url, _headers, payload, _is_streaming):
            seen_models.append(payload["model"])
            if payload["model"] == "small-model":
                return None, "{\"error\":{\"code\":\"context_length_exceeded\",\"message\":\"This model's maximum context length is 8192 tokens.\"}}"
            if payload["model"] == "large-context-model":
                return {"id": "context-fallback-success"}, None
            return None, "unexpected fallback path"

        make_llm_request_mock.side_effect = fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "context-fallback-success"})
        self.assertEqual(seen_models, ["small-model", "large-context-model"])

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_context_overflow_fallback_applies_json_object_sanitization(
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
                "context_overflow_fallback": {
                    "provider": "test-provider",
                    "model": "large-context-model",
                    "use_provider_order_as_fallback": False,
                },
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def fake_make_llm_request(_client, _target_url, _headers, payload, _is_streaming):
            if payload["model"] == "small-model":
                return None, "{\"error\":{\"code\":\"context_length_exceeded\",\"message\":\"maximum context length exceeded\"}}"
            return (
                {
                    "id": "context-fallback-success",
                    "choices": [
                        {
                            "finish_reason": "stop",
                            "message": {
                                "role": "assistant",
                                "content": "<think>hidden</think>\n```json\n{\"ok\":true}\n```",
                            },
                        }
                    ],
                },
                None,
            )

        make_llm_request_mock.side_effect = fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "gateway-model",
                        "messages": [{"role": "user", "content": "hello"}],
                        "response_format": {"type": "json_object"},
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["choices"][0]["message"]["content"],
            '{"ok":true}',
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_generic_error_does_not_trigger_dedicated_context_fallback(
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
                        "model": "primary-model",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "test-provider",
                        "model": "regular-backup-model",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "context_overflow_fallback": {
                    "provider": "test-provider",
                    "model": "large-context-model",
                    "use_provider_order_as_fallback": False,
                },
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        config_loader_cls.return_value = fake_config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        seen_models: list[str] = []

        async def fake_make_llm_request(_client, _target_url, _headers, payload, _is_streaming):
            seen_models.append(payload["model"])
            if payload["model"] == "primary-model":
                return None, '{"error":{"code":"rate_limit_exceeded","message":"Too many requests"}}'
            if payload["model"] == "regular-backup-model":
                return {"id": "regular-fallback-success"}, None
            return None, "unexpected context fallback path"

        make_llm_request_mock.side_effect = fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"id": "regular-fallback-success"})
        self.assertEqual(seen_models, ["primary-model", "regular-backup-model"])


if __name__ == "__main__":
    unittest.main()
