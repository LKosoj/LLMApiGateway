import json
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import HTTPException

from llm_gateway_core.api.v1.chat import _dispatch_chat_request
from llm_gateway_core.config.loader import ConfigLoader, ProviderDetails
from llm_gateway_core.services.router_model import RouterModelService
from tests._async_compat import run_async


def _openai_response(content: str, *, prompt_tokens: int = 1, completion_tokens: int = 1):
    return (
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "total_tokens": prompt_tokens + completion_tokens,
            },
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


class RouterDispatchTests(unittest.TestCase):
    def _config_loader(self, router_targets):
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
        }
        loader.fusion_rules = {}
        loader.router_rules = {
            "gateway/router": {
                "selector_model": "gateway/selector",
                "targets": router_targets,
            }
        }
        loader.model_rules = {}
        return loader

    def _request(self, config_loader):
        app = SimpleNamespace(
            state=SimpleNamespace(
                config_loader=config_loader,
                http_client=object(),
                proxy_http_clients={},
                fallback_events_db=None,
                upstream_routing_state=None,
                router_model_service=RouterModelService(config_loader),
            )
        )
        return SimpleNamespace(app=app, state=SimpleNamespace(), headers={})

    def test_fallback_entry_target_starts_at_selected_index_and_continues_chain(self):
        config_loader = self._config_loader(
            [{"type": "fallback_entry", "gateway_model": "gateway/high", "index": 1}]
        )
        request = self._request(config_loader)
        seen_models = []

        async def fake_make_llm_request(client, url, headers, payload, is_streaming):
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

        async def fake_make_llm_request(client, url, headers, payload, is_streaming):
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

        async def fake_make_llm_request(client, url, headers, payload, is_streaming):
            if payload["model"] == "selector-upstream":
                response, error = _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
                )
                response["usage"]["cost"] = 0.01
                return response, error
            return _openai_response("final from model-a", prompt_tokens=3, completion_tokens=4)

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

    def test_selector_usage_is_kept_when_delegate_usage_is_empty_or_missing(self):
        for delegate_usage in ({}, None):
            with self.subTest(delegate_usage=delegate_usage):
                config_loader = self._config_loader(
                    [{"type": "gateway_model", "model": "gateway/high"}]
                )
                request = self._request(config_loader)

                async def fake_make_llm_request(client, url, headers, payload, is_streaming):
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
                    response = run_async(
                        _dispatch_chat_request(
                            request,
                            {"model": "gateway/router", "messages": [{"role": "user", "content": "hard task"}]},
                        )
                    )

                self.assertEqual(response["usage"]["prompt_tokens"], 2)
                self.assertEqual(response["usage"]["completion_tokens"], 1)
                self.assertEqual(response["usage"]["total_tokens"], 3)
                self.assertNotIn("cost", response["usage"])

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

        async def fake_make_llm_request(client, url, headers, payload, is_streaming):
            if payload["model"] == "selector-upstream":
                return _openai_response(
                    json.dumps({"candidate_id": "gateway:gateway/high"}),
                    prompt_tokens=2,
                    completion_tokens=1,
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

        async def fake_make_llm_request(client, url, headers, payload, is_streaming):
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


if __name__ == "__main__":
    unittest.main()
