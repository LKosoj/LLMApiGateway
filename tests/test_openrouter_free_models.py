import re
import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._async_compat import run_async

from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.openrouter_free_models import (
    HEALTH_PROBE_MAX_TOKENS,
    OpenRouterFreeModelsService,
    ScoredOpenRouterModel,
    _catalog_fingerprint,
    _is_eligible_free_text_model,
    _score_metadata,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise AssertionError(f"unexpected HTTP {self.status_code}")


class FakeOpenRouterClient:
    def __init__(self, catalog):
        self.catalog = catalog
        self.posts = []
        self.get_headers = []
        self.post_headers = []

    async def get(self, url, headers=None):
        self.get_headers.append(headers or {})
        return FakeResponse({"data": self.catalog})

    async def post(self, url, headers=None, json=None, timeout=None):
        self.posts.append(json)
        self.post_headers.append(headers or {})
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


def _model_entry(model_id, *, price="0", context=262144, parameters=None, output=None):
    return {
        "id": model_id,
        "name": model_id,
        "created": 1_760_000_000,
        "context_length": context,
        "top_provider": {"max_completion_tokens": 32768},
        "pricing": {"prompt": price, "completion": price},
        "supported_parameters": parameters or ["tools", "structured_outputs", "response_format", "seed", "stop"],
        "architecture": {"output_modalities": output or ["text"]},
        "expiration_date": None,
    }


class OpenRouterFreeModelsServiceTests(unittest.TestCase):
    def test_eligible_filter_requires_free_text_model(self):
        free_text = _model_entry("provider/free:free")
        paid_text = _model_entry("provider/paid", price="0.1")
        image_model = _model_entry("provider/image", output=["image"])
        tiny_context = _model_entry("provider/tiny", context=4096)

        self.assertTrue(_is_eligible_free_text_model(free_text, 1_700_000_000))
        self.assertFalse(_is_eligible_free_text_model(paid_text, 1_700_000_000))
        self.assertFalse(_is_eligible_free_text_model(image_model, 1_700_000_000))
        self.assertFalse(_is_eligible_free_text_model(tiny_context, 1_700_000_000))

    def test_metadata_score_prefers_capable_large_context_models(self):
        strong = _score_metadata(_model_entry("provider/strong:free"), 1_770_000_000)
        weak = _score_metadata(
            _model_entry(
                "provider/weak:free",
                context=8192,
                parameters=["max_tokens"],
            ),
            1_770_000_000,
        )

        self.assertGreater(strong.metadata_score, weak.metadata_score)
        self.assertTrue(strong.supports_tools)
        self.assertTrue(strong.supports_structured_outputs)

    def test_refresh_switches_to_latency_only_when_model_list_is_unchanged(self):
        catalog = [
            _model_entry("provider/a:free"),
            _model_entry("provider/b:free", parameters=["tools", "response_format"]),
        ]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        service._http_client = FakeOpenRouterClient(catalog)

        run_async(service.refresh_once())
        first_status = run_async(service.get_status())
        self.assertEqual(first_status["snapshot"]["refreshMode"], "fullEval")
        self.assertEqual(first_status["snapshot"]["eligibleCount"], 2)
        task_ids = [
            task["id"]
            for task in first_status["snapshot"]["models"][0]["evalSummary"]["tasks"]
        ]
        self.assertEqual(
            task_ids,
            [
                "instruction_following_lite",
                "tool_call_lite",
                "code_unit_lite",
                "symbolic_math_lite",
                "simpleqa_lite",
            ],
        )

        run_async(service.refresh_once())
        second_status = run_async(service.get_status())
        self.assertEqual(second_status["snapshot"]["refreshMode"], "latencyOnly")
        self.assertEqual(
            second_status["snapshot"]["catalogFingerprint"],
            _catalog_fingerprint(catalog),
        )

    def test_refresh_full_probes_and_evaluates_all_eligible_models(self):
        catalog = [_model_entry(f"provider/model-{index}:free") for index in range(21)]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        fake_client = FakeOpenRouterClient(catalog)
        service._http_client = fake_client

        run_async(service.refresh_once())

        status = run_async(service.get_status())
        snapshot = status["snapshot"]
        self.assertEqual(snapshot["eligibleCount"], 21)
        self.assertEqual(snapshot["evaluatedCount"], 21)
        self.assertEqual(len(snapshot["models"]), 21)
        self.assertTrue(all(model["evalSummary"]["status"] == "completed" for model in snapshot["models"]))
        health_posts = [
            payload for payload in fake_client.posts
            if "Reply with exactly OK" in payload["messages"][0]["content"]
        ]
        self.assertTrue(health_posts)
        self.assertTrue(all(payload["max_tokens"] == HEALTH_PROBE_MAX_TOKENS for payload in health_posts))

    def test_refresh_uses_round_robin_openrouter_keys(self):
        catalog = [_model_entry("provider/a:free")]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "openrouter-rr-a, openrouter-rr-b"
        fake_client = FakeOpenRouterClient(catalog)
        service._http_client = fake_client

        run_async(service.refresh_once())

        self.assertEqual(fake_client.get_headers[0]["Authorization"], "Bearer openrouter-rr-a")
        self.assertTrue(
            any(headers["Authorization"] == "Bearer openrouter-rr-b" for headers in fake_client.post_headers)
        )

    def test_lite_eval_runs_for_all_available_models_without_rank_cutoff(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        first = ScoredOpenRouterModel("provider/a:free", "a", metadata_score=5000)
        second = ScoredOpenRouterModel("provider/b:free", "b", metadata_score=0)
        third = ScoredOpenRouterModel("provider/c:free", "c", metadata_score=0)
        for model, health_score in ((first, 400), (second, 250), (third, 400)):
            model.health_score = health_score
            model.latency_score = 75
            model.recalculate_score()

        async def fake_suite(model):
            return {
                "points": 750 if model.id == "provider/a:free" else 0,
                "maxPoints": 750,
                "tasks": [],
            }

        service._run_lite_eval_suite = fake_suite

        evaluated_count = run_async(service._apply_lite_evals([first, second, third]))

        self.assertEqual(evaluated_count, 3)
        self.assertEqual(first.lite_eval_score, 750)
        self.assertEqual(second.lite_eval_score, 0)
        self.assertEqual(third.lite_eval_score, 0)


class OpenRouterFreeModelsApiTests(unittest.TestCase):
    def test_status_endpoint_returns_disabled_payload_without_service(self):
        app = FastAPI()
        app.include_router(editor_router, prefix="/v1")
        response = TestClient(app).get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()["configured"])

    def test_status_endpoint_returns_service_payload(self):
        class FakeService:
            async def get_status(self):
                return {"configured": True, "snapshot": {"models": []}}

        app = FastAPI()
        app.state.openrouter_free_models_service = FakeService()
        app.include_router(editor_router, prefix="/v1")
        response = TestClient(app).get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])
