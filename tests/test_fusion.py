import asyncio
import json
import time
import unittest
from types import MappingProxyType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, create_autospec, patch

import pydantic
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import web as web_api
from llm_gateway_core.config.loader import ConfigLoader, FusionModelConfig, ProviderDetails
from llm_gateway_core.services import fusion_ensemble
from llm_gateway_core.services.accounting import (
    AccountingReceipt,
    AccountingReservation,
    AccountingCostError,
    AccountingUsage,
    BillingComponent,
    BillingComponentKind,
    CostSource,
    ProjectionStatus,
    SourceStatus,
)
from llm_gateway_core.services.accounting_service import AccountingService
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from llm_gateway_core.utils.usage_tracking import build_model_cost_rate_registry
from tests._async_compat import run_async


def _panel_response(content, usage=None):
    return (
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": usage
            or {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": 0.0,
            },
        },
        None,
    )


async def _fake_make_llm_request(client, url, headers, payload, is_streaming):
    system_prompt = payload["messages"][0].get("content", "")
    if "judge of a panel" in system_prompt:
        content = json.dumps(
            {
                "agreements": ["both agree"],
                "disputes": [],
                "per_model_insights": [{"model": "m1", "insight": "deep"}],
                "blind_spots": ["edge case"],
            }
        )
    elif "lead model of a Fusion" in system_prompt:
        content = "FINAL ANSWER"
    else:
        content = f"answer for {payload['model']}"
    return _panel_response(content)


def _frozen_cost_rate_registry(config_loader):
    return MappingProxyType(build_model_cost_rate_registry(config_loader.providers_config))


class FusionConfigValidationTests(unittest.TestCase):
    def test_panel_must_be_non_empty(self):
        with self.assertRaises(pydantic.ValidationError):
            FusionModelConfig(
                gateway_model_name="llmgateway/fusion",
                panel=[],
                main_model={"provider": "p", "model": "m"},
            )

    def test_panel_at_most_eight(self):
        members = [{"provider": "p", "model": f"m{i}"} for i in range(9)]
        with self.assertRaises(pydantic.ValidationError):
            FusionModelConfig(
                gateway_model_name="llmgateway/fusion",
                panel=members,
                main_model={"provider": "p", "model": "m"},
            )

    def test_temperature_out_of_range_rejected(self):
        with self.assertRaises(pydantic.ValidationError):
            FusionModelConfig(
                gateway_model_name="llmgateway/fusion",
                panel=[{"provider": "p", "model": "m", "temperature": 3}],
                main_model={"provider": "p", "model": "m"},
            )

    def test_mapping_rejects_unknown_provider(self):
        loader = ConfigLoader()
        providers = {"known": ProviderDetails(baseUrl="https://x", apikey="k")}
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "llmgateway/fusion",
                    "panel": [{"provider": "ghost", "model": "m"}],
                    "main_model": {"provider": "known", "model": "m"},
                }
            ]
        )
        with self.assertRaises(ValueError):
            loader.parse_and_validate_fusion_rules_payload(payload, providers_config=providers)

    def test_mapping_accepts_known_providers(self):
        loader = ConfigLoader()
        providers = {"known": ProviderDetails(baseUrl="https://x", apikey="k")}
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "llmgateway/fusion",
                    "panel": [{"provider": "known", "model": "m1"}],
                    "main_model": {"provider": "known", "model": "main"},
                }
            ]
        )
        rules = loader.parse_and_validate_fusion_rules_payload(payload, providers_config=providers)
        self.assertIn("llmgateway/fusion", rules)
        self.assertNotIn("gateway_model_name", rules["llmgateway/fusion"])


class FusionServiceTests(unittest.TestCase):
    def _service(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(baseUrl="https://p1.example", apikey="K", type="openai"),
            }
        )
        return FusionEnsembleService(
            config_loader,
            cost_rate_registry=_frozen_cost_rate_registry(config_loader),
        )

    def _fusion_config(self, **overrides):
        config = {
            "panel": [
                {"provider": "p1", "model": "m1"},
                {"provider": "p1", "model": "m2"},
            ],
            "main_model": {"provider": "p1", "model": "main"},
            "include_details_default": True,
        }
        config.update(overrides)
        return config

    def test_pipeline_runs_panel_judge_and_main(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=_fake_make_llm_request,
        ):
            result = run_async(
                service.run(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._fusion_config(),
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        self.assertEqual(result["model"], "llmgateway/fusion-test")
        self.assertEqual(result["object"], "chat.completion")
        content = result["choices"][0]["message"]["content"]
        self.assertTrue(content.startswith("FINAL ANSWER"))
        self.assertIn("Fusion panel analysis", content)
        self.assertEqual(result["fusion"]["analysis"]["agreements"], ["both agree"])
        self.assertEqual(len(result["fusion"]["panel"]), 2)
        self.assertTrue(all("content" in entry for entry in result["fusion"]["panel"]))
        # 2 panel + 1 judge + 1 main = 4 calls, each total_tokens 2
        self.assertEqual(result["usage"]["total_tokens"], 8)
        self.assertEqual(request.state.llmgateway_provider, "p1")
        self.assertEqual(request.state.llmgateway_provider_model, "main")

    def test_pipeline_uses_injected_cost_registry_after_loader_pricing_mutation(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(
                    baseUrl="https://p1.example",
                    apikey="K",
                    type="openai",
                    models={
                        "m1": {"input_rate": 1.0, "output_rate": 2.0},
                        "judge": {"input_rate": 3.0, "output_rate": 4.0},
                        "main": {"input_rate": 100.0, "output_rate": 200.0},
                    },
                ),
            }
        )
        cost_rate_registry = _frozen_cost_rate_registry(config_loader)
        service = FusionEnsembleService(
            config_loader,
            cost_rate_registry=cost_rate_registry,
        )
        self.assertIs(service._cost_rate_registry, cost_rate_registry)
        config_loader.providers_config["p1"].models = {
            "m1": {"input_rate": 10_000.0, "output_rate": 20_000.0},
            "judge": {"input_rate": 30_000.0, "output_rate": 40_000.0},
            "main": {"input_rate": 50_000.0, "output_rate": 60_000.0},
        }
        request = SimpleNamespace(state=SimpleNamespace())

        async def priced_members(client, url, headers, payload, is_streaming):
            model = payload["model"]
            if model == "m1":
                return _panel_response(
                    "m1 answer",
                    MappingProxyType(
                        {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
                    ),
                )
            if model == "m2":
                return _panel_response(
                    "m2 answer",
                    {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2, "cost": 0.02},
                )
            if model == "judge":
                return _panel_response(
                    json.dumps({"agreements": [], "disputes": []}),
                    {"prompt_tokens": 2, "completion_tokens": 3, "total_tokens": 5},
                )
            return _panel_response(
                "FINAL ANSWER",
                {"prompt_tokens": 4, "completion_tokens": 6, "total_tokens": 10, "cost": 0.0},
            )

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=priced_members,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._fusion_config(judge_model={"provider": "p1", "model": "judge"}),
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )
        result = observed.response

        expected_cost = 0.02 + ((10 * 1.0 + 5 * 2.0) / 1_000_000) + ((2 * 3.0 + 3 * 4.0) / 1_000_000)
        self.assertEqual(result["usage"]["total_tokens"], 32)
        self.assertAlmostEqual(result["usage"]["cost"], expected_cost)
        self.assertNotIn("_cost_incomplete", result["usage"])
        self.assertAlmostEqual(request.state.usage_tracker["cost"], expected_cost)
        self.assertTrue(request.state.usage_tracker[fusion_ensemble.RATE_BASED_COST_SKIP_KEY])
        self.assertNotIn(fusion_ensemble.UPSTREAM_COST_PRESENT_KEY, request.state.usage_tracker)
        self.assertEqual(
            [component.cost_source for component in observed.observation.components],
            [
                CostSource.TOKEN_REGISTRY,
                CostSource.UPSTREAM,
                CostSource.TOKEN_REGISTRY,
                CostSource.UPSTREAM,
            ],
        )
        self.assertEqual(observed.observation.components[-1].usage.cost, 0.0)

    def test_main_failure_preserves_panel_and_judge_usage_tracker(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())

        async def fail_main(client, url, headers, payload, is_streaming):
            if payload["model"] == "main":
                return None, "main rate limited"
            return await _fake_make_llm_request(client, url, headers, payload, is_streaming)

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=fail_main,
        ):
            with self.assertRaises(HTTPException) as ctx:
                run_async(
                    service.run(
                        request=request,
                        gateway_model_name="llmgateway/fusion-test",
                        fusion_config=self._fusion_config(judge_model={"provider": "p1", "model": "judge"}),
                        request_body={"messages": [{"role": "user", "content": "hi"}]},
                        http_client=None,
                        proxy_http_clients={},
                    )
                )

        self.assertEqual(ctx.exception.status_code, 502)
        self.assertEqual(request.state.usage_tracker["prompt_tokens"], 3)
        self.assertEqual(request.state.usage_tracker["completion_tokens"], 3)
        self.assertEqual(request.state.usage_tracker["total_tokens"], 6)

    def test_all_panel_members_failing_raises_502(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())

        async def always_fail(client, url, headers, payload, is_streaming):
            return (None, "upstream boom")

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=always_fail,
        ):
            with self.assertRaises(HTTPException) as ctx:
                run_async(
                    service.run(
                        request=request,
                        gateway_model_name="llmgateway/fusion-test",
                        fusion_config=self._fusion_config(),
                        request_body={"messages": [{"role": "user", "content": "hi"}]},
                        http_client=None,
                        proxy_http_clients={},
                    )
                )
        self.assertEqual(ctx.exception.status_code, 502)

    def test_include_details_false_omits_panel_content_and_footer(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=_fake_make_llm_request,
        ):
            result = run_async(
                service.run(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._fusion_config(),
                    request_body={
                        "messages": [{"role": "user", "content": "hi"}],
                        "fusion": {"include_details": False},
                    },
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        content = result["choices"][0]["message"]["content"]
        self.assertEqual(content, "FINAL ANSWER")
        self.assertTrue(all("content" not in entry for entry in result["fusion"]["panel"]))
        # The structured analysis is still returned even without full details.
        self.assertEqual(result["fusion"]["analysis"]["agreements"], ["both agree"])

    def test_partial_panel_failure_records_error_but_succeeds(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())

        async def fail_m2(client, url, headers, payload, is_streaming):
            if payload["model"] == "m2":
                return (None, "m2 down")
            return await _fake_make_llm_request(client, url, headers, payload, is_streaming)

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=fail_m2,
        ):
            result = run_async(
                service.run(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._fusion_config(),
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )
        panel = result["fusion"]["panel"]
        errored = [entry for entry in panel if "error" in entry]
        self.assertEqual(len(errored), 1)
        self.assertEqual(errored[0]["model"], "m2")


class FusionObservedAccountingTests(unittest.TestCase):
    def _service(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(
                    baseUrl="https://p1.example",
                    apikey="K",
                    type="openai",
                    models={
                        model: {"input_rate": 1.0, "output_rate": 1.0}
                        for model in ("m1", "m2", "judge", "main")
                    },
                ),
            }
        )
        return FusionEnsembleService(
            config_loader,
            cost_rate_registry=_frozen_cost_rate_registry(config_loader),
        )

    @staticmethod
    def _config():
        return {
            "panel": [
                {"provider": "p1", "model": "m1"},
                {"provider": "p1", "model": "m2"},
            ],
            "judge_model": {"provider": "p1", "model": "judge"},
            "main_model": {"provider": "p1", "model": "main"},
            "include_details_default": False,
        }

    @staticmethod
    def _priced_response(content, *, cost):
        return _panel_response(
            content,
            {
                "prompt_tokens": 1,
                "completion_tokens": 1,
                "total_tokens": 2,
                "cost": cost,
            },
        )

    def test_observed_manifest_keeps_config_order_after_reverse_panel_completion(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        m2_finished = asyncio.Event()
        completion_order = []

        async def reverse_completion(client, url, headers, payload, is_streaming):
            model = payload["model"]
            if model == "m1":
                await m2_finished.wait()
                completion_order.append(model)
                return self._priced_response("m1 answer", cost=0.01)
            if model == "m2":
                completion_order.append(model)
                m2_finished.set()
                return self._priced_response("m2 answer", cost=0.02)
            if model == "judge":
                return self._priced_response(
                    json.dumps({"agreements": [], "disputes": []}),
                    cost=0.03,
                )
            return self._priced_response("FINAL", cost=0.04)

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=reverse_completion,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._config(),
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        self.assertEqual(completion_order, ["m2", "m1"])
        self.assertEqual(
            [(component.provider, component.model) for component in observed.observation.components],
            [("p1", "m1"), ("p1", "m2"), ("p1", "judge"), ("p1", "main")],
        )
        self.assertEqual(
            [component.cost_source for component in observed.observation.components],
            [CostSource.UPSTREAM] * 4,
        )
        self.assertEqual(observed.observation.usage.total_tokens, 8)
        self.assertAlmostEqual(observed.observation.usage.cost, 0.1)
        self.assertEqual(observed.response["usage"]["total_tokens"], 8)
        self.assertAlmostEqual(observed.response["usage"]["cost"], 0.1)
        self.assertEqual(observed.observation.top_provider, "p1")
        self.assertEqual(observed.observation.top_model, "main")
        fingerprints = [
            component.billing_fingerprint for component in observed.observation.components
        ]
        self.assertEqual(len(set(fingerprints)), 4)

    def test_observed_manifest_retains_only_successful_panels_and_skips_failed_judge(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())

        async def partial_success(client, url, headers, payload, is_streaming):
            model = payload["model"]
            if model in {"m2", "judge"}:
                return None, f"{model} unavailable"
            return self._priced_response(f"answer from {model}", cost=0.0)

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=partial_success,
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=self._config(),
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        self.assertEqual(
            [component.model for component in observed.observation.components],
            ["m1", "main"],
        )
        self.assertEqual(observed.observation.usage.total_tokens, 4)

    def test_main_failure_does_not_construct_terminal_observation(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())

        async def fail_main(client, url, headers, payload, is_streaming):
            if payload["model"] == "main":
                return None, "main unavailable"
            return self._priced_response(
                json.dumps({"agreements": [], "disputes": []}),
                cost=0.0,
            )

        with (
            patch(
                "llm_gateway_core.services.fusion_ensemble.make_llm_request",
                side_effect=fail_main,
            ),
            patch(
                "llm_gateway_core.services.fusion_ensemble.ChatTerminalObservation"
            ) as observation_type,
        ):
            with self.assertRaises(HTTPException):
                run_async(
                    service.run_observed(
                        request=request,
                        gateway_model_name="llmgateway/fusion-test",
                        fusion_config=self._config(),
                        request_body={"messages": [{"role": "user", "content": "hi"}]},
                        http_client=None,
                        proxy_http_clients={},
                    )
                )

        observation_type.assert_not_called()
        self.assertFalse(hasattr(request.state, "chat_terminal_observation"))

    def test_observed_web_tools_preserve_invocation_component_order(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        config = self._config()
        config["panel"] = [{"provider": "p1", "model": "m1"}]
        config["web_tools"] = {"search_model": "llmgateway/web-search"}
        operation_component = BillingComponent(
            provider=None,
            model=None,
            usage=AccountingUsage(cost=0.2),
            cost_source=CostSource.OPERATION_CONFIGURED,
            component_kind=BillingComponentKind.OPERATION,
            operation="web_search",
            gateway_model="llmgateway/web-search",
        )
        internal_component = BillingComponent(
            provider="query-provider",
            model="query-model",
            usage=AccountingUsage(
                prompt_tokens=2,
                completion_tokens=1,
                total_tokens=3,
                cost=0.05,
            ),
            cost_source=CostSource.UPSTREAM,
        )

        async def web_tool_members(client, url, headers, payload, is_streaming):
            model = payload["model"]
            if model == "m1":
                has_tool_result = any(
                    message.get("role") == "tool" for message in payload["messages"]
                )
                if not has_tool_result:
                    response, error = _tool_call_response("web_search", {"query": "latest"})
                    response["usage"]["cost"] = 0.01
                    return response, error
                return self._priced_response("panel answer", cost=0.02)
            if model == "judge":
                return self._priced_response(
                    json.dumps({"agreements": [], "disputes": []}),
                    cost=0.03,
                )
            return self._priced_response("FINAL", cost=0.04)

        observed_search = AsyncMock(
            return_value=web_api._ObservedWebToolResult(
                result=[
                    {
                        "url": "https://example.test",
                        "title": "Title",
                        "snippet": "Snippet",
                    }
                ],
                components=(operation_component, internal_component),
            )
        )
        with (
            patch(
                "llm_gateway_core.services.fusion_ensemble.make_llm_request",
                side_effect=web_tool_members,
            ),
            patch.object(web_api, "_search_with_model_observed", observed_search),
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=config,
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        self.assertEqual(
            [
                (component.component_kind, component.provider, component.model)
                for component in observed.observation.components
            ],
            [
                (BillingComponentKind.MODEL, "p1", "m1"),
                (BillingComponentKind.OPERATION, None, None),
                (BillingComponentKind.MODEL, "query-provider", "query-model"),
                (BillingComponentKind.MODEL, "p1", "m1"),
                (BillingComponentKind.MODEL, "p1", "judge"),
                (BillingComponentKind.MODEL, "p1", "main"),
            ],
        )
        self.assertEqual(observed.observation.components[1], operation_component)
        self.assertEqual(observed.observation.components[2], internal_component)
        self.assertAlmostEqual(observed.observation.usage.cost, 0.35)
        self.assertAlmostEqual(observed.response["usage"]["cost"], 0.35)
        observed_search.assert_awaited_once()

    def test_observed_failed_web_tool_adds_no_operation_component(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        config = self._config()
        config["panel"] = [{"provider": "p1", "model": "m1"}]
        config["web_tools"] = {"search_model": "llmgateway/web-search"}

        async def web_tool_members(client, url, headers, payload, is_streaming):
            model = payload["model"]
            if model == "m1":
                has_tool_result = any(
                    message.get("role") == "tool" for message in payload["messages"]
                )
                if not has_tool_result:
                    response, error = _tool_call_response("web_search", {"query": "latest"})
                    response["usage"]["cost"] = 0.01
                    return response, error
                return self._priced_response("panel answer after tool error", cost=0.02)
            if model == "judge":
                return self._priced_response(
                    json.dumps({"agreements": [], "disputes": []}),
                    cost=0.03,
                )
            return self._priced_response("FINAL", cost=0.04)

        with (
            patch(
                "llm_gateway_core.services.fusion_ensemble.make_llm_request",
                side_effect=web_tool_members,
            ),
            patch.object(
                web_api,
                "_search_with_model_observed",
                AsyncMock(
                    side_effect=HTTPException(
                        status_code=503,
                        detail="search failed",
                    )
                ),
                create=True,
            ),
        ):
            observed = run_async(
                service.run_observed(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=config,
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

        self.assertTrue(
            all(
                component.component_kind is BillingComponentKind.MODEL
                for component in observed.observation.components
            )
        )

    def test_failed_web_tool_keeps_completed_internal_model_without_operation(self):
        internal_component = BillingComponent(
            provider="query-provider",
            model="query-model",
            usage=AccountingUsage(
                prompt_tokens=2,
                completion_tokens=1,
                total_tokens=3,
                cost=0.05,
            ),
            cost_source=CostSource.UPSTREAM,
        )

        async def failed_search(*_args, _component_accumulator, **_kwargs):
            _component_accumulator.append(internal_component)
            raise HTTPException(status_code=503, detail="search failed")

        with patch.object(
            web_api,
            "_search_with_model_observed",
            side_effect=failed_search,
        ):
            result_text, _usage, components = run_async(
                FusionEnsembleService._execute_web_tool(
                    SimpleNamespace(),
                    {
                        "function": {
                            "name": "web_search",
                            "arguments": json.dumps({"query": "latest"}),
                        }
                    },
                    {"search_model": "llmgateway/web-search"},
                    observe=True,
                )
            )

        self.assertEqual(result_text, "web_search error: search failed")
        self.assertEqual(components, (internal_component,))
        self.assertTrue(
            all(
                component.component_kind is BillingComponentKind.MODEL
                for component in components
            )
        )

    def test_invalid_panel_cost_fails_closed_without_terminal_observation(self):
        service = self._service()
        request = SimpleNamespace(state=SimpleNamespace())
        seen_models = []

        async def invalid_panel_cost(client, url, headers, payload, is_streaming):
            model = payload["model"]
            seen_models.append(model)
            response, error = self._priced_response(f"answer from {model}", cost=0.0)
            if model == "m1":
                response["usage"]["cost"] = "invalid"
            return response, error

        with (
            patch(
                "llm_gateway_core.services.fusion_ensemble.make_llm_request",
                side_effect=invalid_panel_cost,
            ),
            patch(
                "llm_gateway_core.services.fusion_ensemble.ChatTerminalObservation"
            ) as observation_type,
        ):
            with self.assertRaises(AccountingCostError):
                run_async(
                    service.run_observed(
                        request=request,
                        gateway_model_name="llmgateway/fusion-test",
                        fusion_config=self._config(),
                        request_body={"messages": [{"role": "user", "content": "hi"}]},
                        http_client=None,
                        proxy_http_clients={},
                    )
                )

        self.assertCountEqual(seen_models, ["m1", "m2"])
        observation_type.assert_not_called()


class FusionBaseContextTests(unittest.TestCase):
    def _service(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(baseUrl="https://p1.example", apikey="K", type="openai"),
            }
        )
        return FusionEnsembleService(
            config_loader,
            cost_rate_registry=_frozen_cost_rate_registry(config_loader),
        )

    def _fusion_config(self, **overrides):
        config = {
            "panel": [
                {"provider": "p1", "model": "m1"},
                {"provider": "p1", "model": "m2"},
            ],
            "main_model": {"provider": "p1", "model": "main"},
            "include_details_default": True,
        }
        config.update(overrides)
        return config

    def _run_capturing(self, fusion_config):
        request = SimpleNamespace(state=SimpleNamespace())
        records = []

        async def recorder(client, url, headers, payload, is_streaming):
            records.append(payload)
            return await _fake_make_llm_request(client, url, headers, payload, is_streaming)

        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=recorder,
        ):
            run_async(
                self._service().run(
                    request=request,
                    gateway_model_name="llmgateway/fusion-test",
                    fusion_config=fusion_config,
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )
        return records

    def test_temporal_context_format_uses_injected_local_time(self):
        moment = time.localtime(1600000000)
        ctx = fusion_ensemble._temporal_context(moment)
        expected = (
            "Current date and time: "
            f"{time.strftime('%Y-%m-%dT%H:%M %Z', moment).strip()} "
            f"({time.strftime('%A', moment)})."
        )
        self.assertEqual(ctx, expected)

    def test_temporal_context_injected_into_all_roles_without_web_hint(self):
        records = self._run_capturing(self._fusion_config())
        # Every member call (2 panel + judge + main) carries the date/time stamp.
        self.assertEqual(len(records), 4)
        for payload in records:
            self.assertIn("Current date and time:", payload["messages"][0]["content"])
        # No web tools → the recency cue must not appear anywhere.
        self.assertNotIn("use web_search before answering", json.dumps(records))

    def test_web_recency_hint_only_for_panel_when_web_tools_enabled(self):
        config = self._fusion_config(web_tools={"search_model": "llmgateway/web-search"})
        records = self._run_capturing(config)
        panel_systems, judge_main_systems = [], []
        for payload in records:
            system = payload["messages"][0]["content"]
            if "judge of a panel" in system or "lead model of a Fusion" in system:
                judge_main_systems.append(system)
            else:
                panel_systems.append(system)
        self.assertEqual(len(panel_systems), 2)
        self.assertEqual(len(judge_main_systems), 2)
        # Panel members hold the tools → they get the actionable recency cue.
        for system in panel_systems:
            self.assertIn("Current date and time:", system)
            self.assertIn("use web_search before answering", system)
        # Judge/main have no tools → time only, no tool cue.
        for system in judge_main_systems:
            self.assertIn("Current date and time:", system)
            self.assertNotIn("use web_search before answering", system)

    def test_main_prompt_requires_compilation_instead_of_winner_selection(self):
        records = self._run_capturing(self._fusion_config())
        main_systems = [
            payload["messages"][0]["content"]
            for payload in records
            if "lead model of a Fusion" in payload["messages"][0]["content"]
        ]

        self.assertEqual(len(main_systems), 1)
        main_prompt = main_systems[0]
        self.assertIn("Compile a single final answer", main_prompt)
        self.assertIn("Do not select one panel answer as the winner", main_prompt)
        self.assertIn("Combine all useful, non-conflicting contributions", main_prompt)
        self.assertIn("preserve unique correct details", main_prompt)


class FusionWebToolsConfigTests(unittest.TestCase):
    def test_web_tools_defaults(self):
        config = FusionModelConfig(
            gateway_model_name="llmgateway/fusion",
            panel=[{"provider": "p", "model": "m"}],
            main_model={"provider": "p", "model": "main"},
            web_tools={"search_model": "llmgateway/web-search"},
        )
        self.assertIsNotNone(config.web_tools)
        self.assertEqual(config.web_tools.search_model, "llmgateway/web-search")
        self.assertIsNone(config.web_tools.read_model)
        self.assertEqual(config.web_tools.max_tool_calls, 6)
        self.assertEqual(config.web_tools.max_iterations, 4)
        self.assertEqual(config.web_tools.max_results, 5)

    def test_web_tools_requires_search_model(self):
        with self.assertRaises(pydantic.ValidationError):
            FusionModelConfig(
                gateway_model_name="llmgateway/fusion",
                panel=[{"provider": "p", "model": "m"}],
                main_model={"provider": "p", "model": "main"},
                web_tools={"search_model": "  "},
            )

    def test_web_tools_rejects_non_positive_budget(self):
        with self.assertRaises(pydantic.ValidationError):
            FusionModelConfig(
                gateway_model_name="llmgateway/fusion",
                panel=[{"provider": "p", "model": "m"}],
                main_model={"provider": "p", "model": "main"},
                web_tools={"search_model": "llmgateway/web-search", "max_tool_calls": 0},
            )

    def test_web_tools_survive_parse_and_validate(self):
        loader = ConfigLoader()
        providers = {"known": ProviderDetails(baseUrl="https://x", apikey="k")}
        payload = json.dumps(
            [
                {
                    "gateway_model_name": "llmgateway/fusion",
                    "panel": [{"provider": "known", "model": "m1"}],
                    "main_model": {"provider": "known", "model": "main"},
                    "web_tools": {
                        "search_model": "llmgateway/web-search",
                        "read_model": "llmgateway/web-read",
                        "max_tool_calls": 2,
                    },
                }
            ]
        )
        rules = loader.parse_and_validate_fusion_rules_payload(payload, providers_config=providers)
        web_tools = rules["llmgateway/fusion"]["web_tools"]
        self.assertEqual(web_tools["search_model"], "llmgateway/web-search")
        self.assertEqual(web_tools["read_model"], "llmgateway/web-read")
        self.assertEqual(web_tools["max_tool_calls"], 2)


def _tool_call_response(name, arguments):
    return (
        {
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    }
                }
            ],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
        },
        None,
    )


class FusionWebToolLoopTests(unittest.TestCase):
    def _service(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(baseUrl="https://p1.example", apikey="K", type="openai"),
            }
        )
        return FusionEnsembleService(
            config_loader,
            cost_rate_registry=_frozen_cost_rate_registry(config_loader),
        )

    def _fusion_config(self, web_tools):
        return {
            "panel": [{"provider": "p1", "model": "m1"}],
            "main_model": {"provider": "p1", "model": "main"},
            "include_details_default": False,
            "web_tools": web_tools,
        }

    def _run(self, service, fusion_config, make_llm_request):
        request = SimpleNamespace(state=SimpleNamespace())
        with patch(
            "llm_gateway_core.services.fusion_ensemble.make_llm_request",
            side_effect=make_llm_request,
        ):
            return run_async(
                service.run(
                    request=request,
                    gateway_model_name="llmgateway/fusion-web",
                    fusion_config=fusion_config,
                    request_body={"messages": [{"role": "user", "content": "hi"}]},
                    http_client=None,
                    proxy_http_clients={},
                )
            )

    def test_panel_member_runs_web_search_then_answers(self):
        service = self._service()
        search_mock = AsyncMock(return_value=[{"url": "https://e.com", "title": "T", "snippet": "S"}])

        async def fake_request(client, url, headers, payload, is_streaming):
            system_prompt = payload["messages"][0].get("content", "")
            if "judge of a panel" in system_prompt:
                return _panel_response(json.dumps({"agreements": [], "disputes": []}))
            if "lead model of a Fusion" in system_prompt:
                return _panel_response("FINAL ANSWER")
            # Panel member: ask for a search the first time, answer once it has a tool result.
            has_tool_result = any(m.get("role") == "tool" for m in payload["messages"])
            if "tools" in payload and not has_tool_result:
                return _tool_call_response("web_search", {"query": "latest"})
            return _panel_response("panel answer grounded in search")

        with patch("llm_gateway_core.api.v1.web._search_with_model", search_mock):
            result = self._run(
                service,
                self._fusion_config({"search_model": "llmgateway/web-search", "max_tool_calls": 3}),
                fake_request,
            )

        self.assertEqual(search_mock.await_count, 1)
        self.assertEqual(search_mock.await_args.kwargs["search_model"], "llmgateway/web-search")
        self.assertEqual(search_mock.await_args.kwargs["query"], "latest")
        self.assertTrue(result["choices"][0]["message"]["content"].startswith("FINAL ANSWER"))

    def test_tool_call_budget_is_enforced(self):
        service = self._service()
        # Always asks for a search whenever tools are offered.
        search_mock = AsyncMock(return_value=[{"url": "https://e.com", "title": "T", "snippet": "S"}])

        async def fake_request(client, url, headers, payload, is_streaming):
            system_prompt = payload["messages"][0].get("content", "")
            if "judge of a panel" in system_prompt:
                return _panel_response(json.dumps({"agreements": [], "disputes": []}))
            if "lead model of a Fusion" in system_prompt:
                return _panel_response("FINAL ANSWER")
            if "tools" in payload:
                return _tool_call_response("web_search", {"query": "again"})
            return _panel_response("forced final panel answer")

        with patch("llm_gateway_core.api.v1.web._search_with_model", search_mock):
            result = self._run(
                service,
                self._fusion_config(
                    {"search_model": "llmgateway/web-search", "max_tool_calls": 1, "max_iterations": 5}
                ),
                fake_request,
            )

        # max_tool_calls=1 → exactly one search executed despite the model always asking.
        self.assertEqual(search_mock.await_count, 1)
        self.assertTrue(result["choices"][0]["message"]["content"].startswith("FINAL ANSWER"))


class FusionPlaygroundModelsTests(unittest.TestCase):
    def test_fusion_models_listed_in_chat_and_fusion_sections(self):
        from llm_gateway_core.api.v1.rules_editor import _build_playground_models

        config_loader = SimpleNamespace(
            operation_rules={},
            fallback_rules={"llmgateway/light": {}},
            fusion_rules={"llmgateway/fusion-quality": {}, "llmgateway/fusion-fast": {}},
            router_rules={"llmgateway/router": {}},
        )
        models = _build_playground_models(config_loader)
        # Fusion models are callable as chat models, so they appear in the chat list...
        self.assertIn("llmgateway/fusion-quality", models["chat"])
        self.assertIn("llmgateway/fusion-fast", models["chat"])
        self.assertIn("llmgateway/light", models["chat"])
        self.assertIn("llmgateway/router", models["chat"])
        # ...and are also exposed separately so the UI can mark them.
        self.assertEqual(
            models["fusion"], ["llmgateway/fusion-fast", "llmgateway/fusion-quality"]
        )

    def test_no_fusion_models_yields_empty_fusion_list(self):
        from llm_gateway_core.api.v1.rules_editor import _build_playground_models

        config_loader = SimpleNamespace(
            operation_rules={},
            fallback_rules={"llmgateway/light": {}},
            fusion_rules={},
            router_rules={},
        )
        models = _build_playground_models(config_loader)
        self.assertEqual(models["fusion"], [])
        self.assertEqual(models["chat"], ["llmgateway/light"])


class FusionDispatchIntegrationTests(unittest.TestCase):
    def setUp(self):
        super().setUp()
        self._lifespan_events = []
        coordinator = Mock()
        coordinator.close = AsyncMock()
        accounting_service = create_autospec(
            AccountingService,
            instance=True,
            spec_set=True,
        )
        accounting_service.start.side_effect = lambda: self._lifespan_events.append(
            "accounting.start"
        )
        accounting_service.stop.side_effect = lambda: self._lifespan_events.append(
            "accounting.stop"
        )
        accounting_service.reset_due_budgets.return_value = ()
        self._accounting_events = []

        async def reserve(**kwargs):
            return AccountingReservation(
                reservation_id=f"reservation-{kwargs['request_id']}",
                request_id=kwargs["request_id"],
                api_key_id=kwargs["api_key_id"],
                reserved_usd=kwargs["estimate_usd"],
            )

        async def commit(_reservation, event):
            self._accounting_events.append(event)
            return AccountingReceipt(
                source_status=SourceStatus.ACCEPTED,
                projection_status=ProjectionStatus.APPLIED,
                event_id=event.event_id,
                billing_fingerprint=event.billing_fingerprint,
            )

        accounting_service.reserve.side_effect = reserve
        accounting_service.commit.side_effect = commit
        accounting_service.release.return_value = True
        self.accounting_service = accounting_service

        def write_batcher_factory(db_path, *, queue_maxsize):
            self.assertEqual(queue_maxsize, main.settings.write_batcher_queue_maxsize)
            batcher = Mock(name="fusion-write-batcher")
            batcher.db_path = db_path
            batcher.start = AsyncMock(
                side_effect=lambda: self._lifespan_events.append("write-batcher.start")
            )
            batcher.stop = AsyncMock(
                side_effect=lambda: self._lifespan_events.append("write-batcher.stop")
            )
            return batcher

        recovery_patcher = patch.object(
            main.AtomicConfigFileTransaction,
            "recover_pending",
        )
        coordinator_patcher = patch.object(
            main,
            "ConfigUpdateCoordinator",
            return_value=coordinator,
        )
        write_batcher_patcher = patch.object(
            main,
            "WriteBatcher",
            side_effect=write_batcher_factory,
        )
        accounting_patcher = patch.object(
            main,
            "AccountingService",
            return_value=accounting_service,
        )
        recovery_patcher.start()
        coordinator_patcher.start()
        write_batcher_patcher.start()
        accounting_patcher.start()
        self.addCleanup(self._assert_accounting_lifecycle_order)
        self.addCleanup(accounting_patcher.stop)
        self.addCleanup(write_batcher_patcher.stop)
        self.addCleanup(coordinator_patcher.stop)
        self.addCleanup(recovery_patcher.stop)

    def _assert_accounting_lifecycle_order(self):
        self.assertLess(
            self._lifespan_events.index("write-batcher.start"),
            self._lifespan_events.index("accounting.start"),
        )
        self.assertLess(
            self._lifespan_events.index("accounting.stop"),
            self._lifespan_events.index("write-batcher.stop"),
        )

    def _fake_config_loader(self):
        fake = Mock()
        fake.providers_config = {
            "p1": SimpleNamespace(baseUrl="https://p1.example", apikey="K", type="openai"),
        }
        fake.fallback_rules = {}
        fake.fusion_rules = {
            "llmgateway/fusion-test": {
                "panel": [
                    {"provider": "p1", "model": "m1"},
                    {"provider": "p1", "model": "m2"},
                ],
                "main_model": {"provider": "p1", "model": "main"},
                "include_details_default": True,
            }
        }
        fake.operation_rules = {}
        fake.load_providers.return_value = fake.providers_config
        fake.load_fallback_rules.return_value = fake.fallback_rules
        fake.load_fusion_rules.return_value = fake.fusion_rules
        fake.load_complete.return_value = fake
        return fake

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fusion_model_routes_to_ensemble(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = self._fake_config_loader()
        fake_config_loader.fusion_rules["llmgateway/fusion-test"]["judge_model"] = {
            "provider": "p1",
            "model": "judge",
        }
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = _fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llmgateway/fusion-test",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "llmgateway/fusion-test")
        self.assertTrue(body["choices"][0]["message"]["content"].startswith("FINAL ANSWER"))
        self.assertEqual(len(body["fusion"]["panel"]), 2)
        self.accounting_service.commit.assert_awaited_once()
        _tokens_usage_db.return_value.insert_usage.assert_not_called()

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fusion_main_failure_records_partial_usage(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = self._fake_config_loader()
        fake_config_loader.fusion_rules["llmgateway/fusion-test"]["judge_model"] = {
            "provider": "p1",
            "model": "judge",
        }
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def fail_main(client, url, headers, payload, is_streaming):
            if payload["model"] == "main":
                return None, "main rate limited"
            return await _fake_make_llm_request(client, url, headers, payload, is_streaming)

        make_llm_request_mock.side_effect = fail_main

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llmgateway/fusion-test",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 502)
        self.accounting_service.commit.assert_not_awaited()
        self.accounting_service.release.assert_awaited_once()
        _tokens_usage_db.return_value.insert_usage.assert_not_called()

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fusion_main_failure_does_not_price_partial_usage_as_main_model(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        fake_config_loader = self._fake_config_loader()
        fake_config_loader.fusion_rules["llmgateway/fusion-test"]["judge_model"] = {
            "provider": "p1",
            "model": "judge",
        }
        fake_config_loader.providers_config["p1"].models = {
            "main": {"input_rate": 1000, "output_rate": 1000},
        }
        config_loader_cls.return_value = fake_config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        async def fail_main(client, url, headers, payload, is_streaming):
            if payload["model"] == "main":
                return None, "main rate limited"
            return await _fake_make_llm_request(client, url, headers, payload, is_streaming)

        make_llm_request_mock.side_effect = fail_main

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llmgateway/fusion-test",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 502)
        self.accounting_service.commit.assert_not_awaited()
        self.accounting_service.release.assert_awaited_once()
        _tokens_usage_db.return_value.insert_usage.assert_not_called()

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_nested_fusion_call_is_rejected(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = _fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llmgateway/fusion-test",
                        "messages": [{"role": "user", "content": "hello"}],
                    },
                    headers={
                        "Authorization": "Bearer test-gateway-key",
                        "X-LLMGateway-Fusion": "1",
                    },
                )

        self.assertEqual(response.status_code, 400)
        self.accounting_service.commit.assert_not_awaited()
        self.accounting_service.release.assert_awaited_once()

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("llm_gateway_core.services.http_client_factory.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_streaming_fusion_request_is_rejected(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader_cls.return_value = self._fake_config_loader()
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        make_llm_request_mock.side_effect = _fake_make_llm_request

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/chat/completions",
                    json={
                        "model": "llmgateway/fusion-test",
                        "messages": [{"role": "user", "content": "hello"}],
                        "stream": True,
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 400)
        self.accounting_service.commit.assert_not_awaited()
        self.accounting_service.release.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
