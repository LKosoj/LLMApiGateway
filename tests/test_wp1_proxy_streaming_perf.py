"""Focused unit tests for WP1 (proxy core, streaming, HTTP pooling)."""

from __future__ import annotations

import asyncio
import json
import logging
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

from fastapi import HTTPException

from llm_gateway_core.api.v1 import chat_dispatch
from llm_gateway_core.api.v1.chat_accounting import ChatStreamDialect
from llm_gateway_core.api.v1.chat_dialects import _openai_tools_to_anthropic
from llm_gateway_core.api.v1.chat_model_behavior import (
    MixedScriptWordScanner,
    _alphabet_script,
    _classify_non_ascii_char,
)
from llm_gateway_core.api.v1.chat_streaming import (
    _DirectChatStreamObservationBuilder,
    _append_normalized_sse_chunk,
    _new_tool_call_rescue_state,
    _tool_call_rescue_flush_transparent,
)
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services import http_client_factory
from llm_gateway_core.services.error_classifier import _drop_empty_text_content_blocks
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from llm_gateway_core.services.payload_transform import apply_payload_transforms
from llm_gateway_core.services.provider_models import (
    PROVIDER_MODELS_NEGATIVE_CACHE_TTL_SECONDS,
    PROVIDER_MODELS_REQUEST_TIMEOUT_SECONDS,
    ProviderModelsCacheEntry,
    ProviderModelsService,
)
from llm_gateway_core.services.stream_observation import SSEEvent, parse_sse_json
from llm_gateway_core.services.tool_schema_normalizer import _copy_schema_tree
from llm_gateway_core.utils.text_sanitize import remove_surrogates, sanitize_payload
from llm_gateway_core.utils.usage_tracking import (
    ModelCostRates,
    _strip_base64_for_estimation,
    estimate_prompt_tokens,
)
from tests._async_compat import run_async


class LazyPromptEstimationTests(unittest.TestCase):
    def test_estimate_prompt_tokens_accepts_mapping_directly(self):
        payload = {
            "model": "gpt-4o",
            "messages": [{"role": "user", "content": "Hello world from WP1"}],
        }
        from_str = estimate_prompt_tokens(json.dumps(payload), "gpt-4o")
        from_dict = estimate_prompt_tokens(payload, "gpt-4o")
        self.assertGreater(from_dict, 0)
        self.assertEqual(from_dict, from_str)

    def test_strip_base64_for_estimation_is_copy_on_write_and_skips_blobs(self):
        blob = "A" * 50_000
        data_uri = f"data:image/png;base64,{blob}"
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "describe this"},
                        {"type": "image_url", "image_url": {"url": data_uri}},
                        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": blob}},
                    ],
                }
            ]
        }

        cleaned = _strip_base64_for_estimation(payload)
        self.assertIsNot(cleaned, payload)
        # Original payload must remain untouched.
        self.assertEqual(payload["messages"][0]["content"][1]["image_url"]["url"], data_uri)
        self.assertEqual(payload["messages"][0]["content"][2]["source"]["data"], blob)
        # Stripped payload replaces both formats with "".
        self.assertEqual(cleaned["messages"][0]["content"][1]["image_url"]["url"], "")
        self.assertEqual(cleaned["messages"][0]["content"][2]["source"]["data"], "")

        tokens = estimate_prompt_tokens(payload, "gpt-4o")
        self.assertLess(tokens, 200)

        plain = {"messages": [{"role": "user", "content": "no images here"}]}
        self.assertIs(_strip_base64_for_estimation(plain), plain)

    def test_stream_observation_builder_evaluates_prompt_estimator_lazily_and_once(self):
        calls = 0

        def estimator() -> int:
            nonlocal calls
            calls += 1
            return 123

        registry = {("openai", "gpt-4o"): ModelCostRates(input_rate=1.0, output_rate=2.0)}

        # Case 1: upstream provides usage -> estimator is never called.
        builder_with_usage = _DirectChatStreamObservationBuilder(
            dialect=ChatStreamDialect.OPENAI,
            provider="openai",
            model="gpt-4o",
            cost_rate_registry=registry,
            prompt_tokens_estimator=estimator,
        )
        builder_with_usage.observe(
            SSEEvent(data=json.dumps({"choices": [], "usage": {"prompt_tokens": 10, "completion_tokens": 5}}))
        )
        obs = builder_with_usage.observe(SSEEvent(data="[DONE]", done=True))
        self.assertIsNotNone(obs)
        self.assertEqual(calls, 0)

        # Case 2: upstream omits usage -> estimator runs once and clears closure.
        builder_no_usage = _DirectChatStreamObservationBuilder(
            dialect=ChatStreamDialect.OPENAI,
            provider="openai",
            model="gpt-4o",
            cost_rate_registry=registry,
            prompt_tokens_estimator=estimator,
        )
        builder_no_usage.observe(
            SSEEvent(data=json.dumps({"choices": [{"delta": {"content": "hi"}}]}))
        )
        obs_estimated = builder_no_usage.observe(SSEEvent(data="[DONE]", done=True))
        self.assertIsNotNone(obs_estimated)
        self.assertEqual(calls, 1)
        self.assertIsNone(builder_no_usage._prompt_tokens_estimator)
        # Subsequent call reuses memoized value.
        self.assertEqual(builder_no_usage._resolve_estimated_prompt_tokens(), 123)
        self.assertEqual(calls, 1)


class ParseSSEJsonCacheTests(unittest.TestCase):
    def test_parse_sse_json_caches_parsed_object_on_event(self):
        event = SSEEvent(data='{"choices":[{"delta":{"content":"ok"}}]}')
        other_equal = SSEEvent(data='{"choices":[{"delta":{"content":"ok"}}]}')

        with patch("llm_gateway_core.services.stream_observation.json.loads", wraps=json.loads) as mock_loads:
            first = parse_sse_json(event)
            second = parse_sse_json(event)

        self.assertIs(first, second)
        self.assertEqual(mock_loads.call_count, 1)
        self.assertEqual(event, other_equal)
        self.assertEqual(hash(event), hash(other_equal))
        self.assertNotIn("_parsed_json", repr(event))

    def test_parse_sse_json_does_not_cache_invalid_json_or_nan(self):
        for bad_data in ("{not-valid-json", '{"x": NaN}'):
            with self.subTest(bad_data=bad_data):
                event = SSEEvent(data=bad_data)
                with patch(
                    "llm_gateway_core.services.stream_observation.json.loads",
                    wraps=json.loads,
                ) as mock_loads:
                    with self.assertRaises(ValueError):
                        parse_sse_json(event)
                    with self.assertRaises(ValueError):
                        parse_sse_json(event)
                self.assertEqual(mock_loads.call_count, 2)

    def test_parse_sse_json_caches_json_null_none(self):
        event = SSEEvent(data="null")
        with patch("llm_gateway_core.services.stream_observation.json.loads", wraps=json.loads) as mock_loads:
            first = parse_sse_json(event)
            second = parse_sse_json(event)
        self.assertIsNone(first)
        self.assertIsNone(second)
        self.assertEqual(mock_loads.call_count, 1)


class ShallowCopyAndSanitizeTests(unittest.TestCase):
    def test_drop_empty_text_content_blocks_does_not_mutate_original_messages(self):
        original_msg = {
            "role": "assistant",
            "content": [{"type": "text", "text": ""}, {"type": "text", "text": "hello"}],
        }
        original_messages = [original_msg]
        payload = {"messages": original_messages}

        _drop_empty_text_content_blocks(payload)

        self.assertEqual(len(original_msg["content"]), 2)
        self.assertIsNot(payload["messages"], original_messages)
        self.assertEqual(payload["messages"][0]["content"], [{"type": "text", "text": "hello"}])

    def test_sanitize_payload_copy_on_write_and_ascii_fast_path(self):
        clean_payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "plain ascii"}]}
        self.assertIs(sanitize_payload(clean_payload), clean_payload)
        self.assertIs(remove_surrogates("plain ascii"), "plain ascii")

        dirty_payload = {"model": "gpt-4o", "messages": [{"role": "user", "content": "bad \ud800 text"}]}
        sanitized = sanitize_payload(dirty_payload)
        self.assertIsNot(sanitized, dirty_payload)
        self.assertEqual(dirty_payload["messages"][0]["content"], "bad \ud800 text")
        self.assertEqual(sanitized["messages"][0]["content"], "bad \ufffd text")

    def test_apply_payload_transforms_shallow_copies_root_and_clones_mutable_overrides(self):
        shared_messages = [{"role": "user", "content": "hi"}]
        override_obj = {"nested": [1, 2]}
        payload = {"messages": shared_messages, "temperature": 0.5}
        transformed = apply_payload_transforms(
            payload,
            {"overrides": {"extra": override_obj}},
        )
        self.assertIsNot(transformed, payload)
        self.assertIs(transformed["messages"], shared_messages)
        self.assertIsNot(transformed["extra"], override_obj)
        self.assertEqual(transformed["extra"], override_obj)


class StreamBufferOptimizationTests(unittest.TestCase):
    def test_append_normalized_sse_chunk_handles_split_crlf_and_double_cr(self):
        buf = ""
        buf = _append_normalized_sse_chunk(buf, "data: 1\r")
        buf = _append_normalized_sse_chunk(buf, "\n\r\ndata: 2\r\n")
        self.assertEqual(buf, "data: 1\n\ndata: 2\n")

        # Edge case: buffer ends with \r and next chunk starts with \r\n -> matches (a+b).replace("\r\n", "\n")
        expected = ("foo\r" + "\r\nbar").replace("\r\n", "\n")
        actual = _append_normalized_sse_chunk("foo\r", "\r\nbar")
        self.assertEqual(actual, expected)

    def test_tool_call_rescue_state_uses_list_buffers_and_clears_on_flush(self):
        state = _new_tool_call_rescue_state()
        self.assertIsInstance(state["raw_buffer"], list)
        self.assertIsInstance(state["detect_buffer"], list)
        self.assertEqual(state["detect_len"], 0)

        state["raw_buffer"].append("hello ")
        state["raw_buffer"].append("world")
        state["detect_buffer"].append("hello ")
        state["detect_len"] = 6

        chunks = _tool_call_rescue_flush_transparent(state, {"id": "c1"}, 0)
        self.assertEqual(len(chunks), 1)
        self.assertEqual(chunks[0]["choices"][0]["delta"]["content"], "hello world")
        self.assertEqual(state["raw_buffer"], [])
        self.assertEqual(state["detect_buffer"], [])
        self.assertEqual(state["detect_len"], 0)
        self.assertEqual(state["mode"], "transparent")


class HttpPoolAndProviderModelsTests(unittest.TestCase):
    def test_http_pool_limits_constants(self):
        self.assertEqual(http_client_factory.HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS, 100)
        self.assertEqual(http_client_factory.HTTP_CLIENT_KEEPALIVE_EXPIRY_SECONDS, 30.0)
        limits = http_client_factory._default_pool_limits()
        self.assertEqual(limits.max_keepalive_connections, 100)
        self.assertEqual(limits.keepalive_expiry, 30.0)

    def test_provider_models_negative_cache_for_errors_and_timeout(self):
        now = [100.0]
        http_client = Mock()
        http_client.get = AsyncMock(
            side_effect=[
                httpx.ConnectError("upstream down"),
                httpx.Response(
                    200,
                    json={"data": [{"id": "recovered-model"}]},
                    request=httpx.Request("GET", "https://provider.example/models"),
                ),
            ]
        )
        service = ProviderModelsService(
            ttl_seconds=900,
            negative_ttl_seconds=PROVIDER_MODELS_NEGATIVE_CACHE_TTL_SECONDS,
            request_timeout_seconds=PROVIDER_MODELS_REQUEST_TIMEOUT_SECONDS,
            time_func=lambda: now[0],
        )
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="KEY")

        with self.assertRaises(ValueError):
            run_async(service.get_models("prov", provider_config, http_client))
        self.assertEqual(http_client.get.await_count, 1)
        self.assertEqual(
            http_client.get.await_args.kwargs["timeout"],
            PROVIDER_MODELS_REQUEST_TIMEOUT_SECONDS,
        )
        self.assertIsNone(service.snapshot_cache_entry("prov"))

        # Within negative TTL: cached error re-raised without HTTP call.
        now[0] = 110.0
        with self.assertRaises(ValueError):
            run_async(service.get_models("prov", provider_config, http_client))
        self.assertEqual(http_client.get.await_count, 1)

        # After negative TTL: retries and succeeds.
        now[0] = 125.0
        models = run_async(service.get_models("prov", provider_config, http_client))
        self.assertEqual(models, ["recovered-model"])
        self.assertEqual(http_client.get.await_count, 2)

    def test_provider_models_fetched_at_recorded_after_fetch_and_cleared_on_install(self):
        now = [100.0]

        async def slow_get(*_args, **_kwargs):
            now[0] = 107.5
            return httpx.Response(
                200,
                json={"data": [{"id": "m1"}]},
                request=httpx.Request("GET", "https://provider.example/models"),
            )

        http_client = Mock()
        http_client.get = AsyncMock(side_effect=slow_get)
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: now[0])
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="KEY")

        run_async(service.get_models("prov", provider_config, http_client))
        self.assertEqual(service._cache["prov"].fetched_at, 107.5)

        service.clear()
        self.assertEqual(service._cache, {})
        self.assertEqual(service._negative_cache, {})

        service._negative_cache["prov"] = SimpleNamespace(fetched_at=100.0, error_message="err")  # type: ignore[assignment]
        service.install_cache_entry(
            "prov",
            ProviderModelsCacheEntry(models=["m2"], entries={"m2": {"id": "m2"}}, fetched_at=108.0),
        )
        self.assertNotIn("prov", service._negative_cache)


class FusionParallelToolsTests(unittest.TestCase):
    def test_fusion_web_tools_run_in_parallel_preserving_order_and_budget(self):
        service = FusionEnsembleService(config_loader=Mock(), cost_rate_registry={})
        active = 0
        max_active = 0

        async def fake_execute(_request, tool_call, _web_tools, *, observe=False):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            # Make first call slower than second to verify order preservation.
            delay = 0.03 if tool_call["id"] == "c1" else 0.005
            await asyncio.sleep(delay)
            active -= 1
            text = f"res-{tool_call['id']}"
            usage = {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2}
            if observe:
                return text, usage, (f"comp-{tool_call['id']}",)
            return text, usage

        captured_convos: list[list[dict]] = []

        async def fake_raw_chat_call(_member, convo, *_args, **_kwargs):
            captured_convos.append([dict(m) for m in convo])
            if len(captured_convos) == 1:
                return {
                    "choices": [
                        {
                            "message": {
                                "role": "assistant",
                                "content": "",
                                "tool_calls": [
                                    {"id": "c1", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
                                    {"id": "c2", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
                                    {"id": "c3", "type": "function", "function": {"name": "web_search", "arguments": "{}"}},
                                ],
                            }
                        }
                    ],
                    "usage": {"prompt_tokens": 5, "completion_tokens": 5},
                }
            return {
                "choices": [{"message": {"role": "assistant", "content": "final answer"}}],
                "usage": {"prompt_tokens": 10, "completion_tokens": 3},
            }

        request = Mock()
        member = {"provider": "p1", "model": "m1"}
        web_tools = {"max_tool_calls": 2, "search_operation": "web_search"}

        with (
            patch.object(service, "_raw_chat_call", side_effect=fake_raw_chat_call),
            patch.object(service, "_execute_web_tool", side_effect=fake_execute),
        ):
            text, _usage = run_async(
                service._call_member_with_tools(
                    member=member,
                    messages=[{"role": "user", "content": "q"}],
                    http_client=Mock(),
                    proxy_http_clients={},
                    request=request,
                    web_tools=web_tools,
                    cost_rate_registry={},
                    observe=False,
                )
            )

        self.assertEqual(text, "final answer")
        self.assertEqual(max_active, 2)
        tool_messages = [m for m in captured_convos[1] if m.get("role") == "tool"]
        self.assertEqual([m["tool_call_id"] for m in tool_messages], ["c1", "c2", "c3"])
        self.assertEqual(tool_messages[0]["content"], "res-c1")
        self.assertEqual(tool_messages[1]["content"], "res-c2")
        self.assertIn("budget exhausted", tool_messages[2]["content"])


class MicroOptimizationsTests(unittest.TestCase):
    def test_openai_tools_to_anthropic_resolves_collisions_with_used_names(self):
        tools = [
            {"type": "function", "function": {"name": "my.tool", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "my_tool", "parameters": {"type": "object"}}},
            {"type": "function", "function": {"name": "my tool", "parameters": {"type": "object"}}},
        ]
        name_map: dict[str, str] = {}
        translated = _openai_tools_to_anthropic(tools, name_map)
        names = [t["name"] for t in translated]
        self.assertEqual(len(set(names)), 3)
        self.assertEqual(names, ["my_tool", "my_tool_2", "my_tool_3"])

    def test_copy_schema_tree_skips_deepcopy_for_immutable_scalars(self):
        schema = {
            "type": "object",
            "properties": {"count": {"type": "integer", "default": 5, "nullable": False, "title": "cnt"}},
            "required": ["count"],
        }
        with patch("llm_gateway_core.services.tool_schema_normalizer.copy.deepcopy") as mock_deepcopy:
            copied = _copy_schema_tree(schema)
        mock_deepcopy.assert_not_called()
        self.assertEqual(copied, schema)
        self.assertIsNot(copied, schema)
        self.assertIsNot(copied["properties"], schema["properties"])

    def test_mixed_script_scanner_ascii_fast_path_and_non_ascii_cache(self):
        _classify_non_ascii_char.cache_clear()
        self.assertEqual(_alphabet_script("A"), "latin")
        self.assertEqual(_alphabet_script("z"), "latin")
        self.assertIsNone(_alphabet_script("1"))
        self.assertEqual(_classify_non_ascii_char.cache_info().currsize, 0)

        scanner = MixedScriptWordScanner()
        self.assertFalse(scanner.feed("hello world 123!"))
        self.assertEqual(_classify_non_ascii_char.cache_info().currsize, 0)

        self.assertTrue(MixedScriptWordScanner().feed("добромcompanyстве"))
        self.assertGreater(_classify_non_ascii_char.cache_info().currsize, 0)
        # Combining mark inside Cyrillic word stays same script.
        self.assertFalse(MixedScriptWordScanner().feed("при\u0301вет"))

    def test_debug_logging_guard_skips_redaction_when_not_debug(self):
        def _make_request() -> SimpleNamespace:
            config_loader = SimpleNamespace(
                providers_config={},
                fallback_rules={},
                model_rules=None,
                fusion_rules={"test-fusion": {"members": []}},
                router_rules=None,
            )
            services = SimpleNamespace(http_client=Mock(), fallback_events_db=Mock())
            runtime_snapshot = SimpleNamespace(config_loader=config_loader)
            state = SimpleNamespace(
                runtime_snapshot=runtime_snapshot,
                llmgateway_request_id="req-1",
            )
            return SimpleNamespace(
                app=SimpleNamespace(state=SimpleNamespace(services=services)),
                state=state,
                headers={},
            )

        payload = {"model": "test-fusion", "stream": True, "messages": [{"role": "user", "content": "hi"}]}
        root_logger = logging.getLogger()
        prev_level = root_logger.level
        try:
            root_logger.setLevel(logging.INFO)
            with (
                patch.object(chat_dispatch, "redact_payload_for_log", wraps=chat_dispatch.redact_payload_for_log) as mock_redact,
                patch.object(chat_dispatch, "update_active_request"),
            ):
                with self.assertRaises(HTTPException):
                    run_async(
                        chat_dispatch.dispatch_chat_request(
                            _make_request(),  # type: ignore[arg-type]
                            payload,
                            enforce_model_access=False,
                            attempt_model_fallback_rule_hook=AsyncMock(),
                            logging_hook=logging,
                        )
                    )
                mock_redact.assert_not_called()

            root_logger.setLevel(logging.DEBUG)
            with (
                patch.object(chat_dispatch, "redact_payload_for_log", wraps=chat_dispatch.redact_payload_for_log) as mock_redact,
                patch.object(chat_dispatch, "update_active_request"),
            ):
                with self.assertRaises(HTTPException):
                    run_async(
                        chat_dispatch.dispatch_chat_request(
                            _make_request(),  # type: ignore[arg-type]
                            payload,
                            enforce_model_access=False,
                            attempt_model_fallback_rule_hook=AsyncMock(),
                            logging_hook=logging,
                        )
                    )
                mock_redact.assert_called_once_with(payload)
        finally:
            root_logger.setLevel(prev_level)


if __name__ == "__main__":
    unittest.main()
