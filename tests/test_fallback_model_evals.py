import re
import unittest
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.fallback_model_evals import (
    FallbackModelEvalService,
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
            raise AssertionError(f"unexpected HTTP {self.status_code}")


class FakeFallbackEvalClient:
    def __init__(self):
        self.posts = []

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(
            {
                "url": url,
                "headers": headers or {},
                "json": json or {},
                "timeout": timeout,
            }
        )
        prompt = json["messages"][0]["content"]
        if "Reply with exactly OK" in prompt:
            content = "OK"
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
        return FakeResponse({"choices": [{"message": {"content": content}}]})


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
                baseUrl="https://anthropic.example/v1",
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
        self.assertEqual(snapshot["evaluatedCount"], 2)
        models = {model["id"]: model for model in snapshot["models"]}
        self.assertEqual(models["provider-a:model-one"]["healthStatus"], "passed")
        self.assertEqual(models["provider-a:model-one"]["liteEvalScore"], 750)
        self.assertEqual(models["provider-a:model-one"]["metadataScore"], 0)
        self.assertEqual(models["provider-a:model-one"]["gatewayModels"], ["gateway/a", "gateway/b"])
        self.assertEqual(models["missing-provider:model-missing"]["healthStatus"], "missing_provider")
        self.assertEqual(models["anthropic-provider:claude-native"]["healthStatus"], "unsupported_provider_type")

        authorizations = [post["headers"].get("Authorization") for post in fake_client.posts]
        self.assertIn("Bearer fallback-rr-a", authorizations)
        self.assertIn("Bearer fallback-rr-b", authorizations)

        model_one_posts = [post for post in fake_client.posts if post["json"].get("model") == "model-one"]
        self.assertTrue(model_one_posts)
        self.assertTrue(all(post["url"] == "https://provider.example/v1/chat/completions" for post in model_one_posts))
        self.assertTrue(all(post["headers"].get("X-Eval") == "yes" for post in model_one_posts))
        self.assertTrue(all(post["headers"].get("Authorization") != "ignored" for post in model_one_posts))
        self.assertTrue(all(post["json"].get("top_p") == 0.9 for post in model_one_posts))
        self.assertTrue(all(post["json"].get("provider") == {"order": ["SubProviderA"]} for post in model_one_posts))
        self.assertTrue(all(post["json"].get("allow_fallbacks") is False for post in model_one_posts))


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
