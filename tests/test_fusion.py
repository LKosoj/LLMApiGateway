import json
import time
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pydantic
from fastapi import HTTPException
from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader, FusionModelConfig, ProviderDetails
from llm_gateway_core.services import fusion_ensemble
from llm_gateway_core.services.fusion_ensemble import FusionEnsembleService
from tests._async_compat import run_async


def _panel_response(content):
    return (
        {
            "choices": [{"message": {"role": "assistant", "content": content}}],
            "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
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
        return FusionEnsembleService(config_loader)

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


class FusionBaseContextTests(unittest.TestCase):
    def _service(self):
        config_loader = SimpleNamespace(
            providers_config={
                "p1": SimpleNamespace(baseUrl="https://p1.example", apikey="K", type="openai"),
            }
        )
        return FusionEnsembleService(config_loader)

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
        return FusionEnsembleService(config_loader)

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
        return fake

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
    @patch("main.ConfigLoader")
    def test_fusion_model_routes_to_ensemble(
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
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["model"], "llmgateway/fusion-test")
        self.assertTrue(body["choices"][0]["message"]["content"].startswith("FINAL ANSWER"))
        self.assertEqual(len(body["fusion"]["panel"]), 2)

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
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

    @patch("llm_gateway_core.services.fusion_ensemble.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.httpx.AsyncClient")
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


if __name__ == "__main__":
    unittest.main()
