import json
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from llm_gateway_core.api.v1.chat import _dispatch_chat_request
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.services.accounting import (
    AccountingCostError,
    AccountingErrorCode,
    CostSource,
)
from llm_gateway_core.services.router_model import (
    _SELECTOR_SYSTEM_PROMPT,
    RouterModelService,
)
from llm_gateway_core.utils.usage_tracking import (
    build_model_cost_rate_registry,
)
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


def _openai_response(
    content: str,
    *,
    prompt_tokens: int = 1,
    completion_tokens: int = 1,
    cost: float | None = 0.0,
):
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if cost is not None:
        usage["cost"] = cost
    return (
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage,
        },
        None,
    )


class RouterConfigValidationTests(unittest.TestCase):
    def _fallback_rules(self):
        return {
            "gateway/selector": {
                "fallback_models": [{"provider": "p1", "model": "selector"}],
                "rotate_models": False,
            },
            "gateway/high": {
                "fallback_models": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p1", "model": "m2"},
                ],
                "rotate_models": False,
            },
        }

    def test_router_rules_accept_gateway_and_fallback_entry_targets(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "targets": [
                        {"type": "gateway_model", "model": "gateway/high"},
                        {"type": "fallback_entry", "gateway_model": "gateway/high", "index": 1},
                    ],
                }
            ]
        )

        rules = loader.parse_and_validate_router_rules_payload(
            payload,
            fallback_rules=self._fallback_rules(),
            fusion_rules={},
        )

        self.assertIn("gateway/router", rules)
        self.assertEqual(rules["gateway/router"]["selector_model"], "gateway/selector")
        self.assertEqual(len(rules["gateway/router"]["targets"]), 2)

    def test_router_rules_reject_unknown_selector_model(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/missing",
                    "targets": [{"type": "gateway_model", "model": "gateway/high"}],
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "unknown selector_model"):
            loader.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=self._fallback_rules(),
                fusion_rules={},
            )

    def test_router_rules_reject_out_of_range_fallback_entry_index(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "targets": [{"type": "fallback_entry", "gateway_model": "gateway/high", "index": 9}],
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "index 9"):
            loader.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=self._fallback_rules(),
                fusion_rules={},
            )

    def test_router_rules_reject_negative_fallback_entry_index(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "targets": [{"type": "fallback_entry", "gateway_model": "gateway/high", "index": -1}],
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "greater than or equal to 0"):
            loader.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=self._fallback_rules(),
                fusion_rules={},
            )


    def test_router_rules_accept_routing_policy_and_target_hints(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "routing_policy": "Prefer the cheapest candidate that can answer well.",
                    "targets": [
                        {
                            "type": "gateway_model",
                            "model": "gateway/high",
                            "description": "Code, long context and tool calls",
                            "cost_hint": "premium",
                        }
                    ],
                }
            ]
        )

        rules = loader.parse_and_validate_router_rules_payload(
            payload,
            fallback_rules=self._fallback_rules(),
            fusion_rules={},
        )

        rule = rules["gateway/router"]
        self.assertEqual(
            rule["routing_policy"],
            "Prefer the cheapest candidate that can answer well.",
        )
        target = rule["targets"][0]
        self.assertEqual(target["description"], "Code, long context and tool calls")
        self.assertEqual(target["cost_hint"], "premium")

    def test_router_rules_reject_unknown_cost_hint(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "targets": [
                        {"type": "gateway_model", "model": "gateway/high", "cost_hint": "cheapest"}
                    ],
                }
            ]
        )

        with self.assertRaises(ValueError):
            loader.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=self._fallback_rules(),
                fusion_rules={},
            )

    def test_router_rules_reject_blank_routing_policy(self):
        loader = ConfigLoader()
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/selector",
                    "routing_policy": "   ",
                    "targets": [{"type": "gateway_model", "model": "gateway/high"}],
                }
            ]
        )

        with self.assertRaisesRegex(ValueError, "routing_policy"):
            loader.parse_and_validate_router_rules_payload(
                payload,
                fallback_rules=self._fallback_rules(),
                fusion_rules={},
            )


class RouterDispatchTests(unittest.TestCase):
    def _config_loader(self, router_targets, routing_policy=None):
        loader = SimpleNamespace()
        loader.providers_config = {
            "p1": ProviderDetails(baseUrl="https://p1.example/v1", apikey="KEY"),
        }
        loader.fallback_rules = {
            "gateway/selector": {
                "fallback_models": [{"provider": "p1", "model": "selector-upstream"}],
                "rotate_models": False,
            },
            "gateway/high": {
                "fallback_models": [
                    {"provider": "p1", "model": "model-a"},
                    {"provider": "p1", "model": "model-b"},
                    {"provider": "p1", "model": "model-c"},
                ],
                "rotate_models": False,
            },
            "gateway/light": {
                "fallback_models": [{"provider": "p1", "model": "light-upstream"}],
                "rotate_models": False,
            },
            "gateway/free": {
                "fallback_models": [{"provider": "p1", "model": "free-upstream"}],
                "rotate_models": False,
            },
        }
        loader.fusion_rules = {}
        loader.operation_rules = {}
        router_config = {
            "selector_model": "gateway/selector",
            "targets": router_targets,
        }
        if routing_policy is not None:
            router_config["routing_policy"] = routing_policy
        loader.router_rules = {"gateway/router": router_config}
        loader.model_rules = {}
        return loader

    def _request(self, config_loader, *, cost_rate_registry=None):
        if cost_rate_registry is None:
            cost_rate_registry = MappingProxyType(
                build_model_cost_rate_registry(config_loader.providers_config)
            )
        runtime_http_client = object()
        services = make_app_services(http_client=runtime_http_client)
        router_model_service = RouterModelService(
            config_loader,
            cost_rate_registry=cost_rate_registry,
        )
        runtime_snapshot = make_runtime_snapshot(
            config_loader=config_loader,
            http_client=runtime_http_client,
            cost_rate_registry=cost_rate_registry,
            router_model_service=router_model_service,
        )
        app = SimpleNamespace(
            state=SimpleNamespace(
                services=services,
                config_loader=object(),
                http_client=object(),
                proxy_http_clients={"p1": object()},
                fallback_events_db=None,
                fusion_service=object(),
                router_model_service=object(),
                upstream_routing_state=object(),
            )
        )
        return SimpleNamespace(
            app=app,
            state=SimpleNamespace(runtime_snapshot=runtime_snapshot),
            headers={},
        )

    def test_fallback_entry_target_starts_at_selected_index_and_continues_chain(self):
        config_loader = self._config_loader(
            [{"type": "fallback_entry", "gateway_model": "gateway/high", "index": 1}]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            if payload["model"] == "selector-upstream":
                return _openai_response(
                    json.dumps({"candidate_id": "fallback_entry:gateway/high:1", "reason": "coding", "confidence": 0.8}),
                    prompt_tokens=2,
                    completion_tokens=1,
                )
            if payload["model"] == "model-b":
                return None, "model-b unavailable"
            return _openai_response("final from model-c", prompt_tokens=3, completion_tokens=4)

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "write code"}]},
                )
            )

        self.assertEqual(seen_models, ["selector-upstream", "model-b", "model-c"])
        self.assertEqual(response["choices"][0]["message"]["content"], "final from model-c")
        self.assertEqual(response["router"]["selected_candidate_id"], "fallback_entry:gateway/high:1")
        self.assertEqual(response["usage"]["total_tokens"], 10)
        self.assertEqual(request.state.llmgateway_provider_model, "model-c")

    def test_gateway_model_target_uses_full_fallback_chain(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            if payload["model"] == "selector-upstream":
                return _openai_response(json.dumps({"candidate_id": "gateway:gateway/high"}))
            if payload["model"] == "model-a":
                return None, "model-a unavailable"
            return _openai_response("final from model-b")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "hard task"}]},
                )
            )

        self.assertEqual(seen_models, ["selector-upstream", "model-a", "model-b"])
        self.assertEqual(response["choices"][0]["message"]["content"], "final from model-b")
        self.assertEqual(response["router"]["selected_candidate_id"], "gateway:gateway/high")

    def test_recent_tool_error_selects_premium_without_selector_call(self):
        config_loader = self._config_loader(
            [
                {"type": "gateway_model", "model": "gateway/light", "cost_hint": "cheap"},
                {"type": "gateway_model", "model": "gateway/free", "cost_hint": "free"},
                {"type": "gateway_model", "model": "gateway/high", "cost_hint": "premium"},
            ]
        )
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            return _openai_response("recovered")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="gateway/router",
                    router_config=config_loader.router_rules["gateway/router"],
                    request_body={
                        "model": "gateway/router",
                        "messages": [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "exec_command", "arguments": "{}"},
                                    }
                                ],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": "call_1",
                                "content": "Traceback (most recent call last): SyntaxError: invalid syntax",
                            },
                        ],
                    },
                )
            )
        response = observed.response

        self.assertEqual(seen_models, ["model-a"])
        self.assertEqual([component.model for component in observed.observation.components], ["model-a"])
        self.assertEqual(response["router"]["selected_candidate_id"], "gateway:gateway/high")
        self.assertEqual(response["router"]["reason"], "tool_error")
        self.assertEqual(response["router"]["decision_source"], "tool_history")
        self.assertIsNone(response["router"]["confidence"])
        self.assertEqual(response["usage"]["total_tokens"], 2)

    def test_passed_tests_after_edit_select_cheap_but_not_free(self):
        config_loader = self._config_loader(
            [
                {"type": "gateway_model", "model": "gateway/light", "cost_hint": "cheap"},
                {"type": "gateway_model", "model": "gateway/free", "cost_hint": "free"},
                {"type": "gateway_model", "model": "gateway/high", "cost_hint": "premium"},
            ]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            return _openai_response("next step")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {
                        "model": "gateway/router",
                        "messages": [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "apply_patch", "arguments": "{}"},
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": "call_1", "content": "Done!"},
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_2",
                                        "type": "function",
                                        "function": {"name": "exec_command", "arguments": "{}"},
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": "call_2", "content": "12 passed in 0.4s"},
                        ],
                    },
                )
            )

        self.assertEqual(seen_models, ["light-upstream"])
        self.assertEqual(response["router"]["selected_candidate_id"], "gateway:gateway/light")
        self.assertEqual(response["router"]["reason"], "tests_passed_after_change")
        self.assertEqual(response["router"]["decision_source"], "tool_history")

    def test_ambiguous_tool_history_still_uses_selector(self):
        config_loader = self._config_loader(
            [
                {"type": "gateway_model", "model": "gateway/light", "cost_hint": "cheap"},
                {"type": "gateway_model", "model": "gateway/high", "cost_hint": "premium"},
            ]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            if payload["model"] == "selector-upstream":
                return _openai_response(json.dumps({"candidate_id": "gateway:gateway/light"}))
            return _openai_response("answer")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {
                        "model": "gateway/router",
                        "messages": [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "read_file", "arguments": "{}"},
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": "call_1", "content": "file contents"},
                        ],
                    },
                )
            )

        self.assertEqual(seen_models, ["selector-upstream", "light-upstream"])
        self.assertEqual(response["router"]["decision_source"], "llm_selector")

    def test_partial_test_failure_still_uses_selector(self):
        config_loader = self._config_loader(
            [
                {"type": "gateway_model", "model": "gateway/light", "cost_hint": "cheap"},
                {"type": "gateway_model", "model": "gateway/high", "cost_hint": "premium"},
            ]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            if payload["model"] == "selector-upstream":
                return _openai_response(json.dumps({"candidate_id": "gateway:gateway/high"}))
            return _openai_response("fix the failure")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {
                        "model": "gateway/router",
                        "messages": [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "apply_patch", "arguments": "{}"},
                                    }
                                ],
                            },
                            {
                                "role": "tool",
                                "tool_call_id": "call_1",
                                "content": "1 failed, 12 passed in 0.4s",
                            },
                        ],
                    },
                )
            )

        self.assertEqual(seen_models, ["selector-upstream", "model-a"])
        self.assertEqual(response["router"]["decision_source"], "llm_selector")

    def test_rates_resolve_cheap_target_when_cost_hints_are_missing(self):
        config_loader = self._config_loader(
            [
                {"type": "gateway_model", "model": "gateway/light"},
                {"type": "gateway_model", "model": "gateway/high"},
            ]
        )
        config_loader.providers_config["p1"].models = {
            "light-upstream": {"input_rate": 1, "output_rate": 2},
            "model-a": {"input_rate": 10, "output_rate": 20},
        }
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            return _openai_response("next step")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {
                        "model": "gateway/router",
                        "messages": [
                            {
                                "role": "assistant",
                                "tool_calls": [
                                    {
                                        "id": "call_1",
                                        "type": "function",
                                        "function": {"name": "apply_patch", "arguments": "{}"},
                                    }
                                ],
                            },
                            {"role": "tool", "tool_call_id": "call_1", "content": "5 passed in 0.2s"},
                        ],
                    },
                )
            )

        self.assertEqual(seen_models, ["light-upstream"])
        self.assertEqual(response["router"]["selected_candidate_id"], "gateway:gateway/light")

    def test_routing_policy_and_target_hints_reach_the_selector(self):
        config_loader = self._config_loader(
            [
                {
                    "type": "gateway_model",
                    "model": "gateway/high",
                    "description": "Code, long context and tool calls",
                    "cost_hint": "premium",
                }
            ],
            routing_policy="Prefer the cheapest candidate that can answer well.",
        )
        request = self._request(config_loader)
        selector_payloads = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                selector_payloads.append(payload)
                return _openai_response(json.dumps({"candidate_id": "gateway:gateway/high"}))
            return _openai_response("final")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "hi"}]},
                )
            )

        self.assertEqual(len(selector_payloads), 1)
        system_prompt = selector_payloads[0]["messages"][0]["content"]
        self.assertIn("Prefer the cheapest candidate that can answer well.", system_prompt)
        candidate = json.loads(selector_payloads[0]["messages"][1]["content"])["candidates"][0]
        self.assertEqual(candidate["description"], "Code, long context and tool calls")
        self.assertEqual(candidate["cost_hint"], "premium")

    def test_selector_prompt_stays_unchanged_without_policy_and_hints(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        request = self._request(config_loader)
        selector_payloads = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                selector_payloads.append(payload)
                return _openai_response(json.dumps({"candidate_id": "gateway:gateway/high"}))
            return _openai_response("final")

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "hi"}]},
                )
            )

        self.assertEqual(selector_payloads[0]["messages"][0]["content"], _SELECTOR_SYSTEM_PROMPT)
        candidate = json.loads(selector_payloads[0]["messages"][1]["content"])["candidates"][0]
        self.assertNotIn("description", candidate)
        self.assertNotIn("cost_hint", candidate)

    def test_selector_upstream_cost_does_not_mask_delegate_local_cost(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.providers_config["p1"].models = {
            "model-a": {
                "input_rate": 1000,
                "output_rate": 2000,
            },
        }
        request = self._request(config_loader)

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                response, error = _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
                )
                response["usage"]["cost"] = 0.01
                return response, error
            return _openai_response(
                "final from model-a",
                prompt_tokens=3,
                completion_tokens=4,
                cost=None,
            )

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "hard task"}]},
                )
            )

        self.assertEqual(response["usage"]["prompt_tokens"], 5)
        self.assertEqual(response["usage"]["completion_tokens"], 5)
        self.assertEqual(response["usage"]["total_tokens"], 10)
        self.assertAlmostEqual(response["usage"]["cost"], 0.021, places=6)

    def test_captured_cost_registry_is_stable_and_preserves_upstream_zero(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.providers_config["p1"].models = {
            "selector-upstream": {
                "input_rate": 1000,
                "output_rate": 2000,
            },
            "model-a": {
                "input_rate": 1000,
                "output_rate": 2000,
            },
        }
        cost_rate_registry = MappingProxyType(
            build_model_cost_rate_registry(config_loader.providers_config)
        )
        config_loader.providers_config["p1"].models = {
            "selector-upstream": {
                "input_rate": 800_000,
                "output_rate": 800_000,
            },
            "model-a": {
                "input_rate": 800_000,
                "output_rate": 800_000,
            },
        }

        for selector_upstream_cost, expected_cost in ((None, 0.015), (0.0, 0.011)):
            with self.subTest(selector_upstream_cost=selector_upstream_cost):
                request = self._request(
                    config_loader,
                    cost_rate_registry=cost_rate_registry,
                )
                router_service = request.state.runtime_snapshot.router_model_service
                self.assertIs(router_service._cost_rate_registry, cost_rate_registry)

                async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
                    if payload["model"] == "selector-upstream":
                        response, error = _openai_response(
                            json.dumps({"candidate_id": "gateway:gateway/high"}),
                            prompt_tokens=2,
                            completion_tokens=1,
                            cost=None,
                        )
                        if selector_upstream_cost is not None:
                            response["usage"]["cost"] = selector_upstream_cost
                        return response, error
                    return _openai_response(
                        "final from model-a",
                        prompt_tokens=3,
                        completion_tokens=4,
                        cost=None,
                    )

                with patch(
                    "llm_gateway_core.api.v1.chat.make_llm_request",
                    side_effect=fake_make_llm_request,
                ):
                    response = run_async(
                        _dispatch_chat_request(
                            request,
                            {
                                "model": "gateway/router",
                                "messages": [{"role": "user", "content": "hard task"}],
                            },
                        )
                    )

                self.assertAlmostEqual(response["usage"]["cost"], expected_cost, places=6)

    def test_delegate_success_without_actual_usage_fails_closed(self):
        for delegate_usage in ({}, None):
            with self.subTest(delegate_usage=delegate_usage):
                config_loader = self._config_loader(
                    [{"type": "gateway_model", "model": "gateway/high"}]
                )
                request = self._request(config_loader)

                async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
                    if payload["model"] == "selector-upstream":
                        return _openai_response(
                            json.dumps({"candidate_id": "gateway:gateway/high"}),
                            prompt_tokens=2,
                            completion_tokens=1,
                        )
                    response = {
                        "choices": [{"message": {"role": "assistant", "content": "final without usage"}}],
                    }
                    if delegate_usage is not None:
                        response["usage"] = delegate_usage
                    return response, None

                with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
                    with self.assertRaises(AccountingCostError) as error:
                        run_async(
                            _dispatch_chat_request(
                                request,
                                {
                                    "model": "gateway/router",
                                    "messages": [{"role": "user", "content": "hard task"}],
                                },
                            )
                        )
                    self.assertIs(error.exception.code, AccountingErrorCode.COST_UNAVAILABLE)

    def test_selector_usage_is_added_to_raw_anthropic_delegate_response(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.providers_config["p1"].models = {
            "selector-upstream": {
                "input_rate": 1000,
                "output_rate": 2000,
            },
        }
        config_loader.providers_config["anthropic"] = ProviderDetails(
            baseUrl="https://anthropic.example",
            apikey="KEY",
            type="anthropic",
            models={
                "claude-sonnet": {
                    "input_rate": 3000,
                    "output_rate": 15000,
                },
            },
        )
        config_loader.fallback_rules["gateway/high"]["fallback_models"] = [
            {"provider": "anthropic", "model": "claude-sonnet"}
        ]
        request = self._request(config_loader)
        request.state.llmgateway_original_anthropic_payload = {
            "model": "gateway/router",
            "max_tokens": 100,
            "messages": [{"role": "user", "content": "hard task"}],
        }

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                return _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
                    cost=None,
                )
            return (
                {
                    "id": "msg_1",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "final from claude"}],
                    "model": payload["model"],
                    "usage": {"input_tokens": 3, "output_tokens": 4},
                },
                None,
            )

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            response = run_async(
                _dispatch_chat_request(
                    request,
                    {"model": "gateway/router", "messages": [{"role": "user", "content": "hard task"}]},
                )
            )

        self.assertEqual(response["usage"]["input_tokens"], 5)
        self.assertEqual(response["usage"]["output_tokens"], 5)
        self.assertAlmostEqual(response["usage"]["cost"], 0.073, places=6)
        self.assertEqual(response["router"]["selected_candidate_id"], "gateway:gateway/high")
        self.assertTrue(request.state.llmgateway_response_is_anthropic_raw)

    def test_unknown_selector_candidate_fails_explicitly(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        request = self._request(config_loader)

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            return _openai_response(json.dumps({"candidate_id": "gateway:missing"}))

        with patch("llm_gateway_core.api.v1.chat.make_llm_request", side_effect=fake_make_llm_request):
            with self.assertRaises(HTTPException) as ctx:
                run_async(
                    _dispatch_chat_request(
                        request,
                        {"model": "gateway/router", "messages": [{"role": "user", "content": "hi"}]},
                    )
                )

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertIn("unknown candidate_id", str(ctx.exception.detail))

    def test_run_observed_returns_selector_then_delegate_components_without_synthetic_reuse(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.providers_config["p1"].models = {
            "selector-upstream": {"input_rate": 1000, "output_rate": 2000},
            "model-a": {"input_rate": 1000, "output_rate": 2000},
        }
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                response, error = _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
                )
                response["usage"]["cost"] = 0.01
                return response, error
            response, error = _openai_response(
                "final from model-a",
                prompt_tokens=3,
                completion_tokens=4,
            )
            response["usage"]["cost"] = 0.02
            return response, error

        with patch(
            "llm_gateway_core.api.v1.chat.make_llm_request",
            side_effect=fake_make_llm_request,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="gateway/router",
                    router_config=config_loader.router_rules["gateway/router"],
                    request_body={
                        "model": "gateway/router",
                        "messages": [{"role": "user", "content": "hard task"}],
                    },
                )
            )

        self.assertEqual(
            [(component.provider, component.model) for component in observed.observation.components],
            [("p1", "selector-upstream"), ("p1", "model-a")],
        )
        self.assertEqual(
            [component.usage.cost for component in observed.observation.components],
            [0.01, 0.02],
        )
        self.assertEqual(
            [component.cost_source for component in observed.observation.components],
            [CostSource.UPSTREAM, CostSource.UPSTREAM],
        )
        self.assertAlmostEqual(observed.observation.usage.cost, 0.03)
        self.assertAlmostEqual(observed.response["usage"]["cost"], 0.03)
        self.assertEqual(observed.observation.top_provider, "p1")
        self.assertEqual(observed.observation.top_model, "model-a")

    def test_run_observed_records_only_successful_selector_and_delegate_after_retries(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.fallback_rules["gateway/selector"]["fallback_models"] = [
            {"provider": "p1", "model": "selector-broken"},
            {"provider": "p1", "model": "selector-upstream"},
        ]
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            model = payload["model"]
            seen_models.append(model)
            if model in {"selector-broken", "model-a"}:
                return None, f"{model} unavailable"
            if model == "selector-upstream":
                response, error = _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"})
                )
                response["usage"]["cost"] = 0.0
                return response, error
            response, error = _openai_response("final from model-b")
            response["usage"]["cost"] = 0.0
            return response, error

        with patch(
            "llm_gateway_core.api.v1.chat.make_llm_request",
            side_effect=fake_make_llm_request,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="gateway/router",
                    router_config=config_loader.router_rules["gateway/router"],
                    request_body={
                        "model": "gateway/router",
                        "messages": [{"role": "user", "content": "hard task"}],
                    },
                )
            )

        self.assertEqual(
            seen_models,
            ["selector-broken", "selector-upstream", "model-a", "model-b"],
        )
        self.assertEqual(
            [component.model for component in observed.observation.components],
            ["selector-upstream", "model-b"],
        )

    def test_run_observed_uses_registry_for_missing_component_costs(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        config_loader.providers_config["p1"].models = {
            "selector-upstream": {"input_rate": 1000, "output_rate": 2000},
            "model-a": {"input_rate": 1000, "output_rate": 2000},
        }
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service

        async def fake_make_llm_request(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                return _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
                    cost=None,
                )
            return _openai_response(
                "final from model-a",
                prompt_tokens=3,
                completion_tokens=4,
                cost=None,
            )

        with patch(
            "llm_gateway_core.api.v1.chat.make_llm_request",
            side_effect=fake_make_llm_request,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="gateway/router",
                    router_config=config_loader.router_rules["gateway/router"],
                    request_body={
                        "model": "gateway/router",
                        "messages": [{"role": "user", "content": "hard task"}],
                    },
                )
            )

        self.assertEqual(
            [component.cost_source for component in observed.observation.components],
            [CostSource.TOKEN_REGISTRY, CostSource.TOKEN_REGISTRY],
        )
        self.assertEqual(
            [component.usage.cost for component in observed.observation.components],
            [0.004, 0.011],
        )

    def test_invalid_selector_cost_fails_before_delegate_and_without_terminal_observation(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service
        seen_models = []

        async def invalid_selector_cost(client, url, headers, payload, is_streaming, **_kwargs):
            seen_models.append(payload["model"])
            response, error = _openai_response(
                json.dumps({"candidate_id": "gateway:gateway/high"})
            )
            response["usage"]["cost"] = "invalid"
            return response, error

        with (
            patch(
                "llm_gateway_core.api.v1.chat.make_llm_request",
                side_effect=invalid_selector_cost,
            ),
            patch(
                "llm_gateway_core.services.router_model.ChatTerminalObservation"
            ) as observation_type,
        ):
            with self.assertRaises(AccountingCostError):
                run_async(
                    service.run_observed(
                        request=request,
                        gateway_model_name="gateway/router",
                        router_config=config_loader.router_rules["gateway/router"],
                        request_body={
                            "model": "gateway/router",
                            "messages": [{"role": "user", "content": "hard task"}],
                        },
                    )
                )

        self.assertEqual(seen_models, ["selector-upstream"])
        observation_type.assert_not_called()

    def test_delegate_failure_does_not_construct_terminal_observation(self):
        config_loader = self._config_loader(
            [{"type": "gateway_model", "model": "gateway/high"}]
        )
        request = self._request(config_loader)
        service = request.state.runtime_snapshot.router_model_service

        async def fail_delegate(client, url, headers, payload, is_streaming, **_kwargs):
            if payload["model"] == "selector-upstream":
                response, error = _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"})
                )
                response["usage"]["cost"] = 0.0
                return response, error
            return None, "delegate unavailable"

        with (
            patch(
                "llm_gateway_core.api.v1.chat.make_llm_request",
                side_effect=fail_delegate,
            ),
            patch(
                "llm_gateway_core.services.router_model.ChatTerminalObservation"
            ) as observation_type,
        ):
            with self.assertRaises(HTTPException):
                run_async(
                    service.run_observed(
                        request=request,
                        gateway_model_name="gateway/router",
                        router_config=config_loader.router_rules["gateway/router"],
                        request_body={
                            "model": "gateway/router",
                            "messages": [{"role": "user", "content": "hard task"}],
                        },
                    )
                )

        observation_type.assert_not_called()


if __name__ == "__main__":
    unittest.main()
