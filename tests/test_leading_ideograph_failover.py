import json
import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.responses import StreamingResponse
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.chat_model_behavior import (
    LEADING_IDEOGRAPH_FAILURE_MESSAGE,
    MIXED_SCRIPT_STREAM_FAILURE_MESSAGE,
)
from llm_gateway_core.api.v1.chat_streaming import detect_leading_ideograph_stream
from llm_gateway_core.api.v1.rules_editor import StructuredProviderItem, _serialize_structured_providers
from tests._async_compat import run_async
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _completion(response_id: str, content: str) -> dict:
    return {
        "id": response_id,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ],
    }


def _sse(payload: dict) -> bytes:
    return b"data: " + json.dumps(payload, ensure_ascii=False).encode() + b"\n\n"


def _openai_delta(content: str | None = None, *, tool_call: bool = False) -> bytes:
    delta: dict = {}
    if content is not None:
        delta["content"] = content
    if tool_call:
        delta["tool_calls"] = [{"index": 0, "id": "call_1", "function": {"name": "lookup", "arguments": ""}}]
    return _sse({"choices": [{"index": 0, "delta": delta}]})


class LeadingIdeographStreamTests(unittest.TestCase):
    def _response(self, *chunks: bytes) -> StreamingResponse:
        async def body():
            for chunk in chunks:
                yield chunk

        return StreamingResponse(body(), media_type="text/event-stream")

    def _detect_and_read(self, *chunks: bytes):
        async def run():
            response, failure = await detect_leading_ideograph_stream(
                self._response(*chunks),
                is_anthropic_provider=False,
            )
            if failure is not None:
                return failure, b""
            parts = []
            async for chunk in response.body_iterator:
                parts.append(chunk)
            return failure, b"".join(parts)

        return run_async(run())

    def test_first_ideograph_fails_before_the_client_sees_it(self):
        failure, forwarded = self._detect_and_read(_openai_delta("金"), _openai_delta("続き"))

        self.assertEqual(failure.behavior_class, "leading_ideograph")
        self.assertEqual(str(failure), LEADING_IDEOGRAPH_FAILURE_MESSAGE)
        self.assertEqual(forwarded, b"")

    def test_ideograph_anywhere_in_the_first_chunk_fails(self):
        failure, forwarded = self._detect_and_read(
            _openai_delta("This is one of those topics 金"),
            _openai_delta("続き"),
        )

        self.assertEqual(str(failure), LEADING_IDEOGRAPH_FAILURE_MESSAGE)
        self.assertEqual(forwarded, b"")

    def test_empty_bytes_do_not_count_as_the_first_chunk(self):
        failure, forwarded = self._detect_and_read(b"", _openai_delta("金"))

        self.assertEqual(failure.behavior_class, "leading_ideograph")
        self.assertEqual(forwarded, b"")

    def test_latin_preamble_is_forwarded_with_the_later_ideographs(self):
        failure, forwarded = self._detect_and_read(_openai_delta("This "), _openai_delta("金"))

        self.assertIsNone(failure)
        self.assertIn("This ".encode(), forwarded)
        self.assertIn("金".encode(), forwarded)

    def test_hiragana_first_chunk_is_forwarded(self):
        failure, forwarded = self._detect_and_read(_openai_delta("あ"), _openai_delta("い"))

        self.assertIsNone(failure)
        self.assertIn("あ".encode(), forwarded)
        self.assertIn("い".encode(), forwarded)

    def test_mixed_word_in_the_first_chunk_fails_before_the_client_sees_it(self):
        failure, forwarded = self._detect_and_read(_openai_delta("добромcompanyстве"))

        self.assertEqual(failure.behavior_class, "mixed_script_word")
        self.assertEqual(str(failure), MIXED_SCRIPT_STREAM_FAILURE_MESSAGE)
        self.assertEqual(forwarded, b"")

    def test_word_split_across_chunks_ends_the_stream_with_an_error(self):
        failure, forwarded = self._detect_and_read(
            _openai_delta("добром"),
            _openai_delta("companyстве"),
        )

        self.assertIsNone(failure)
        self.assertIn("добром".encode(), forwarded)
        self.assertNotIn("companyстве".encode(), forwarded)
        self.assertIn(b"mixed_script_word", forwarded)

    def test_two_alphabets_in_separate_words_are_forwarded(self):
        failure, forwarded = self._detect_and_read(
            _openai_delta("добром "),
            _openai_delta("company"),
        )

        self.assertIsNone(failure)
        self.assertIn("добром ".encode(), forwarded)
        self.assertIn("company".encode(), forwarded)
        self.assertNotIn(b"mixed_script_word", forwarded)

    def test_tool_call_before_text_is_forwarded(self):
        failure, forwarded = self._detect_and_read(_openai_delta(tool_call=True), _openai_delta("金"))

        self.assertIsNone(failure)
        self.assertIn(b"tool_calls", forwarded)
        self.assertIn("金".encode(), forwarded)


class LeadingIdeographProviderSaveTests(unittest.TestCase):
    def test_enabled_flag_is_written_and_false_is_omitted(self):
        enabled = StructuredProviderItem.model_validate(
            {
                "name": "a6",
                "baseUrl": "https://api.a6api.vip/v1",
                "apikey": "${APIKEY_A6}",
                "switch_on_leading_ideograph": True,
            }
        )
        disabled = StructuredProviderItem.model_validate(
            {
                "name": "other",
                "baseUrl": "https://other.example/v1",
                "apikey": "key",
                "switch_on_leading_ideograph": False,
            }
        )

        payload = json.loads(_serialize_structured_providers([enabled, disabled]))

        self.assertIs(payload[0]["a6"]["switch_on_leading_ideograph"], True)
        self.assertNotIn("switch_on_leading_ideograph", payload[1]["other"])


class LeadingIdeographFailoverTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        install_main_chat_accounting_double(self._accounting_stack)
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        patchers = (
            patch.object(main.AtomicConfigFileTransaction, "recover_pending"),
            patch(
                "llm_gateway_core.services.runtime_candidate.build_operation_cost_calculator_registry",
                return_value={},
            ),
            patch("main.ConfigUpdateCoordinator", return_value=config_update_coordinator),
        )
        for patcher in patchers:
            patcher.start()
            self.addCleanup(patcher.stop)

    def _loader(self, *, enabled: bool) -> Mock:
        a6 = SimpleNamespace(
            baseUrl="https://a6.example/v1",
            apikey="DIRECT-KEY",
            switch_on_leading_ideograph=True if enabled else None,
        )
        other = SimpleNamespace(
            baseUrl="https://other.example/v1",
            apikey="DIRECT-KEY",
            switch_on_leading_ideograph=None,
        )
        fake_config_loader = Mock()
        fake_config_loader.providers_config = {"a6": a6, "other": other}
        fake_config_loader.fallback_rules = {
            "gateway-model": {
                "fallback_models": [
                    {
                        "provider": "a6",
                        "model": "a6-first",
                        "retry_count": 2,
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "a6",
                        "model": "a6-second",
                        "use_provider_order_as_fallback": False,
                    },
                    {
                        "provider": "other",
                        "model": "other-model",
                        "use_provider_order_as_fallback": False,
                    },
                ],
                "rotate_models": False,
            }
        }
        fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
        fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
        fake_config_loader.load_complete.return_value = fake_config_loader
        return fake_config_loader

    def _post(self):
        request_payload = {
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "Объясни, как работает кэш"}],
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                return client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_enabled_provider_skips_its_later_models(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._loader(enabled=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = [
            (_completion("a6-garbage", "This is one of those topics. 金狻猊传奇"), None),
            (_completion("other-ok", "Кэш хранит готовый ответ."), None),
        ]

        with self.assertLogs(level="WARNING") as captured:
            response = self._post()

        self.assertTrue(
            any(
                "switch_on_leading_ideograph" in line and "first response chunk contains an ideograph" in line
                for line in captured.output
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Кэш хранит готовый ответ.")
        self.assertEqual(
            [call.args[3]["model"] for call in make_llm_request_mock.await_args_list],
            ["a6-first", "other-model"],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_disabled_provider_keeps_an_ideograph_reply(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._loader(enabled=False)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = [
            (_completion("a6-chinese", "金狻猊传奇"), None),
        ]

        response = self._post()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "金狻猊传奇")
        self.assertEqual(make_llm_request_mock.await_count, 1)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_mixed_word_skips_later_models_of_the_same_provider(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._loader(enabled=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = [
            (_completion("a6-mixed", "Слово добромcompanyстве сломано."), None),
            (_completion("other-ok", "Кэш хранит готовый ответ."), None),
        ]

        with self.assertLogs(level="WARNING") as captured:
            response = self._post()

        self.assertTrue(
            any(
                "switch_on_leading_ideograph" in line
                and "mixes letters from different alphabets" in line
                for line in captured.output
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["choices"][0]["message"]["content"], "Кэш хранит готовый ответ.")
        self.assertEqual(
            [call.args[3]["model"] for call in make_llm_request_mock.await_args_list],
            ["a6-first", "other-model"],
        )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_streaming_mixed_word_fails_without_a_fallback(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._loader(enabled=True)
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def body():
            yield _openai_delta("добромcompanyстве")

        make_llm_request_mock.side_effect = [
            (StreamingResponse(body(), media_type="text/event-stream"), None),
            (_completion("other-ok", "Кэш хранит готовый ответ."), None),
        ]

        request_payload = {
            "model": "gateway-model",
            "stream": True,
            "messages": [{"role": "user", "content": "Объясни, как работает кэш"}],
        }
        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json=request_payload,
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 502)
        self.assertIn("mixes letters from different alphabets", response.text)
        self.assertEqual(make_llm_request_mock.await_count, 1)
