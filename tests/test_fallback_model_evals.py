import re
import unittest
from types import SimpleNamespace

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.fallback_model_evals import (
    FallbackModelEvalService,
    HEALTH_PROBE_MAX_TOKENS,
    _collect_unique_fallback_targets,
)
from tests._async_compat import run_async


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            request = httpx.Request("POST", "https://provider.example/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"unexpected HTTP {self.status_code}", request=request, response=response)


class FakeFallbackEvalClient:
    def __init__(self, catalog=None, *, health_content="OK", health_status_code=200):
        self.catalog = catalog or []
        self.health_content = health_content
        self.health_status_code = health_status_code
        self.gets = []
        self.get_headers = []
        self.posts = []

    async def get(self, url, headers=None, timeout=None):
        self.gets.append({"url": url, "headers": headers or {}, "timeout": timeout})
        self.get_headers.append(headers or {})
        return FakeResponse({"data": self.catalog})

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
                "timeout": timeout,
            }
        )
        prompt = _first_message_text(json)
        if "Reply with exactly OK" in prompt:
            if self.health_status_code >= 400:
                return FakeResponse({"error": {"message": "rate limited"}}, status_code=self.health_status_code)
            content = self.health_content
        elif "Return exactly 4 lines" in prompt:
            content = 'STATUS: READY\nROUTER and ROUTER\n{"mode":"eval","count":3}\nDONE'
        elif "Available tools" in prompt and "create_ticket" in prompt:
            content = (
                '{"tool":"create_ticket","arguments":{'
                '"title":"Login fails after password reset",'
                '"priority":"high","assignee":"Ana","due_date":"2026-05-12"}}'
            )
        elif "sum_even_squares" in prompt:
            content = (
                '{"code":"def sum_even_squares(nums: list[int]) -> int:\\n'
                '    total = 0\\n'
                '    for value in nums:\\n'
                '        if value % 2 == 0:\\n'
                '            total += value * value\\n'
                '    return total\\n"}'
            )
        elif "A notebook has" in prompt:
            numbers = [int(value) for value in re.findall(r"\d+", prompt)]
            total_pages, weekday_pages, weeks, weekend_pages = numbers[:4]
            content = str(total_pages - weeks * 5 * weekday_pages - weeks * 2 * weekend_pages)
        elif "The Left Hand of Darkness" in prompt:
            content = "Ursula K. Le Guin"
        else:
            content = ""
        if url.endswith("/v1/messages"):
            return FakeResponse({"content": [{"type": "text", "text": content}], "model": json.get("model")})
        return FakeResponse({"choices": [{"message": {"content": content}}]})


def _first_message_text(payload):
    messages = payload.get("messages") or []
    if not messages:
        return ""
    content = messages[0].get("content") if isinstance(messages[0], dict) else ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )
    return ""


def _openrouter_entry(model_id):
    return {
        "id": model_id,
        "name": model_id,
        "created": 1_769_000_000,
        "context_length": 131072,
        "top_provider": {"max_completion_tokens": 32768},
        "pricing": {"prompt": "0", "completion": "0"},
        "supported_parameters": ["tools", "structured_outputs", "response_format", "seed", "stop"],
        "architecture": {"output_modalities": ["text"]},
    }


def _fallback_rules():
    return {
        "gateway/a": {
            "fallback_models": [
                {
                    "provider": "provider-a",
                    "model": "model-one",
                    "custom_headers": {
                        "X-Eval": "yes",
                        "Authorization": "ignored",
                    },
                    "custom_body_params": {"top_p": 0.9},
                    "providers_order": ["SubProviderA"],
                },
                {"provider": "provider-a", "model": "model-one"},
            ],
            "context_overflow_fallback": {"provider": "provider-a", "model": "model-overflow"},
        },
        "gateway/b": {
            "fallback_models": [
                {"provider": "provider-a", "model": "model-one"},
            ],
        },
        "gateway/c": {
            "fallback_models": [
                {"provider": "missing-provider", "model": "model-missing"},
                {"provider": "anthropic-provider", "model": "claude-native"},
            ],
        },
    }


class FallbackModelEvalServiceTests(unittest.TestCase):
    def test_collect_unique_fallback_targets_groups_by_provider_and_model(self):
        targets = _collect_unique_fallback_targets(_fallback_rules())

        keys = [(target.provider, target.model) for target in targets]
        self.assertEqual(
            keys,
            [
                ("provider-a", "model-one"),
                ("provider-a", "model-overflow"),
                ("missing-provider", "model-missing"),
                ("anthropic-provider", "claude-native"),
            ],
        )
        self.assertEqual(targets[0].gateway_models, ["gateway/a", "gateway/b"])
        self.assertEqual(targets[1].gateway_models, ["gateway/a"])

    def test_run_once_scores_openai_compatible_unique_targets(self):
        providers_config = {
            "provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="fallback-rr-a, fallback-rr-b"),
            "anthropic-provider": ProviderDetails(
                baseUrl="https://anthropic.example",
                apikey="anthropic-key",
                type="anthropic",
            ),
        }
        fake_client = FakeFallbackEvalClient()
        service = FallbackModelEvalService(time_func=lambda: 1_770_000_000)

        run_async(
            service.run_once(
                providers_config=providers_config,
                fallback_rules=_fallback_rules(),
                http_client=fake_client,
            )
        )

        status = run_async(service.get_status())
        snapshot = status["snapshot"]
        self.assertFalse(status["running"])
        self.assertEqual(snapshot["configuredCount"], 4)
        self.assertEqual(snapshot["evaluatedCount"], 3)
        models = {model["id"]: model for model in snapshot["models"]}
        self.assertEqual(models["provider-a:model-one"]["healthStatus"], "passed")
        self.assertEqual(models["provider-a:model-one"]["liteEvalScore"], 750)
        self.assertEqual(models["provider-a:model-one"]["metadataScore"], 0)
        self.assertEqual(models["provider-a:model-one"]["reason"], "")
        self.assertEqual(models["provider-a:model-one"]["gatewayModels"], ["gateway/a", "gateway/b"])
        self.assertNotIn("metadata score", models["provider-a:model-one"]["reason"].lower())
        self.assertEqual(models["missing-provider:model-missing"]["healthStatus"], "missing_provider")
        self.assertEqual(models["anthropic-provider:claude-native"]["healthStatus"], "passed")
        self.assertEqual(models["anthropic-provider:claude-native"]["liteEvalScore"], 750)

        authorizations = [post["headers"].get("Authorization") for post in fake_client.posts]
        self.assertIn("Bearer fallback-rr-a", authorizations)
        self.assertIn("Bearer fallback-rr-b", authorizations)
        anthropic_posts = [post for post in fake_client.posts if post["url"] == "https://anthropic.example/v1/messages"]
        self.assertTrue(anthropic_posts)
        self.assertTrue(all(post["headers"].get("x-api-key") == "anthropic-key" for post in anthropic_posts))
        self.assertTrue(all(post["headers"].get("anthropic-version") == "2023-06-01" for post in anthropic_posts))
        self.assertTrue(all(post["json"].get("model") == "claude-native" for post in anthropic_posts))
        self.assertTrue(all("max_tokens" in post["json"] for post in anthropic_posts))

        model_one_posts = [post for post in fake_client.posts if post["json"].get("model") == "model-one"]
        self.assertTrue(model_one_posts)
        self.assertTrue(all(post["url"] == "https://provider.example/v1/chat/completions" for post in model_one_posts))
        self.assertTrue(all(post["headers"].get("X-Eval") == "yes" for post in model_one_posts))
        self.assertTrue(all(post["headers"].get("Authorization") != "ignored" for post in model_one_posts))
        self.assertTrue(all(post["json"].get("top_p") == 0.9 for post in model_one_posts))
        self.assertTrue(all(post["json"].get("provider") == {"order": ["SubProviderA"]} for post in model_one_posts))
        self.assertTrue(all(post["json"].get("allow_fallbacks") is False for post in model_one_posts))
        health_posts = [
            post for post in fake_client.posts
            if "Reply with exactly OK" in _first_message_text(post["json"])
        ]
        self.assertTrue(health_posts)
        self.assertTrue(all(post["json"].get("max_tokens") == HEALTH_PROBE_MAX_TOKENS for post in health_posts))

    def test_run_once_evaluates_imperfect_health_targets(self):
        providers_config = {
            "provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="provider-key"),
        }
        fallback_rules = {
            "gateway/a": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-one"},
                ],
            },
        }
        fake_client = FakeFallbackEvalClient(health_content="READY")
        service = FallbackModelEvalService(time_func=lambda: 1_770_000_000)

        run_async(
            service.run_once(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=fake_client,
            )
        )

        status = run_async(service.get_status())
        snapshot = status["snapshot"]
        model = snapshot["models"][0]
        self.assertEqual(snapshot["evaluatedCount"], 1)
        self.assertEqual(model["healthStatus"], "imperfect")
        self.assertEqual(model["healthScore"], 250)
        self.assertEqual(model["evalSummary"]["status"], "completed")
        self.assertEqual(model["liteEvalScore"], 750)

    def test_run_once_skips_lite_eval_for_rate_limited_health_targets(self):
        providers_config = {
            "provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="provider-key"),
        }
        fallback_rules = {
            "gateway/a": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-one"},
                ],
            },
        }
        fake_client = FakeFallbackEvalClient(health_status_code=429)
        service = FallbackModelEvalService(time_func=lambda: 1_770_000_000)

        run_async(
            service.run_once(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=fake_client,
            )
        )

        status = run_async(service.get_status())
        snapshot = status["snapshot"]
        model = snapshot["models"][0]
        self.assertEqual(snapshot["evaluatedCount"], 0)
        self.assertEqual(model["healthStatus"], "http_429")
        self.assertEqual(model["healthScore"], 100)
        self.assertEqual(model["instabilityPenalty"], 25)
        self.assertEqual(model["liteEvalScore"], 0)
        self.assertEqual(model["evalSummary"]["status"], "not_evaluated")
        self.assertEqual(model["evalSummary"]["reason"], "health_probe_rate_limited")
        self.assertEqual(len(fake_client.posts), 1)

    def test_run_once_enriches_metadata_from_openrouter_basename_when_key_configured(self):
        providers_config = {
            "provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="provider-key"),
            "openrouter": ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="openrouter-metadata-key"),
        }
        fallback_rules = {
            "gateway/a": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "model-one"},
                ],
            },
        }
        fake_client = FakeFallbackEvalClient(catalog=[_openrouter_entry("openai/model-one:free")])
        service = FallbackModelEvalService(time_func=lambda: 1_770_000_000)

        run_async(
            service.run_once(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=fake_client,
            )
        )

        status = run_async(service.get_status())
        model = status["snapshot"]["models"][0]
        self.assertGreater(model["metadataScore"], 0)
        self.assertEqual(model["contextLength"], 131072)
        self.assertEqual(model["maxCompletionTokens"], 32768)
        self.assertTrue(model["supportsTools"])
        self.assertTrue(model["supportsStructuredOutputs"])
        self.assertEqual(model["metadataSource"], "openrouter")
        self.assertEqual(model["metadataMatchedModel"], "openai/model-one:free")
        self.assertIn("OpenRouter metadata: openai/model-one:free", model["reason"])
        self.assertEqual(fake_client.gets[0]["url"], "https://openrouter.ai/api/v1/models")
        self.assertEqual(fake_client.get_headers[0]["Authorization"], "Bearer openrouter-metadata-key")

    def test_run_once_uses_median_known_metadata_score_for_unmatched_models(self):
        providers_config = {
            "provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="provider-key"),
            "openrouter": ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="openrouter-metadata-key"),
        }
        fallback_rules = {
            "gateway/a": {
                "fallback_models": [
                    {"provider": "provider-a", "model": "known-one"},
                    {"provider": "provider-a", "model": "known-two"},
                    {"provider": "provider-a", "model": "unmatched-model"},
                ],
            },
        }
        weak_entry = _openrouter_entry("provider/known-two:free")
        weak_entry["context_length"] = 4096
        weak_entry["top_provider"] = {"max_completion_tokens": 1024}
        weak_entry["supported_parameters"] = ["stop"]
        fake_client = FakeFallbackEvalClient(
            catalog=[
                _openrouter_entry("provider/known-one:free"),
                weak_entry,
            ]
        )
        service = FallbackModelEvalService(time_func=lambda: 1_770_000_000)

        run_async(
            service.run_once(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=fake_client,
            )
        )

        status = run_async(service.get_status())
        models = {model["model"]: model for model in status["snapshot"]["models"]}
        known_scores = sorted([models["known-one"]["metadataScore"], models["known-two"]["metadataScore"]])
        self.assertEqual(models["unmatched-model"]["metadataScore"], sum(known_scores) // 2)
        self.assertEqual(models["unmatched-model"]["metadataSource"], "openrouter_median")
        self.assertIsNone(models["unmatched-model"]["metadataMatchedModel"])
        self.assertEqual(models["unmatched-model"]["contextLength"], 0)
        self.assertIn("median metadata score", models["unmatched-model"]["reason"])


class FallbackModelEvalApiTests(unittest.TestCase):
    def test_status_endpoint_returns_disabled_payload_without_service(self):
        app = FastAPI()
        app.include_router(editor_router, prefix="/v1")

        response = TestClient(app).get("/v1/fallback-model-evals")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])

    def test_run_endpoint_starts_service_with_current_config(self):
        class FakeService:
            def __init__(self):
                self.called = False
                self.providers_config = None
                self.fallback_rules = None
                self.http_client = None
                self.proxy_http_clients = None

            async def start_eval(self, *, providers_config, fallback_rules, http_client, proxy_http_clients=None):
                self.called = True
                self.providers_config = providers_config
                self.fallback_rules = fallback_rules
                self.http_client = http_client
                self.proxy_http_clients = proxy_http_clients

            async def get_status(self):
                return {"configured": True, "running": True, "snapshot": None}

        service = FakeService()
        config_loader = SimpleNamespace(
            providers_config={"provider-a": ProviderDetails(baseUrl="https://provider.example/v1", apikey="key")},
            fallback_rules={"gateway/a": {"fallback_models": [{"provider": "provider-a", "model": "model-one"}]}},
        )
        shared_http_client = object()
        proxy_client = object()

        app = FastAPI()
        app.state.fallback_model_eval_service = service
        app.state.config_loader = config_loader
        app.state.http_client = shared_http_client
        app.state.proxy_http_clients = {"provider-a": proxy_client}
        app.include_router(editor_router, prefix="/v1")

        response = TestClient(app).post("/v1/fallback-model-evals/run")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["running"])
        self.assertTrue(service.called)
        self.assertIs(service.providers_config, config_loader.providers_config)
        self.assertIs(service.fallback_rules, config_loader.fallback_rules)
        self.assertIs(service.http_client, shared_http_client)
        self.assertEqual(service.proxy_http_clients, {"provider-a": proxy_client})
