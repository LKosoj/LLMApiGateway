import os
import re
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from fastapi.testclient import TestClient

from tests._async_compat import run_async

from llm_gateway_core.api.v1.rules_editor import editor_router
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.middleware.auth import ROLE_MASTER, ROLE_USER
from llm_gateway_core.services.openrouter_free_models import (
    HEALTH_PROBE_MAX_TOKENS,
    OpenRouterFreeModelsNotConfigured,
    OpenRouterFreeModelsService,
    ScoredOpenRouterModel,
    _catalog_fingerprint,
    _run_sum_even_squares_tests,
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
            request = httpx.Request("POST", "https://openrouter.example/chat/completions")
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError(f"unexpected HTTP {self.status_code}", request=request, response=response)


class FakeOpenRouterClient:
    def __init__(self, catalog, *, health_status_code=200):
        self.catalog = catalog
        self.health_status_code = health_status_code
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
            if self.health_status_code >= 400:
                return FakeResponse({"error": {"message": "rate limited"}}, status_code=self.health_status_code)
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
    def test_code_eval_subprocess_uses_posix_rlimits(self):
        captured: dict[str, object] = {}

        class FakeProcess:
            returncode = 0

            async def communicate(self, input_data):
                captured["input"] = input_data
                return b"", b""

        async def fake_create_subprocess_exec(*args, **kwargs):
            captured["args"] = args
            captured["kwargs"] = kwargs
            return FakeProcess()

        with patch(
            "llm_gateway_core.services.openrouter_free_models.asyncio.create_subprocess_exec",
            side_effect=fake_create_subprocess_exec,
        ):
            passed, stderr = run_async(_run_sum_even_squares_tests("def sum_even_squares(nums):\n    return 0\n"))

        self.assertTrue(passed)
        self.assertEqual(stderr, "")
        kwargs = captured["kwargs"]
        self.assertIsInstance(kwargs, dict)
        if os.name == "posix":
            self.assertTrue(callable(kwargs.get("preexec_fn")))
        else:
            self.assertNotIn("preexec_fn", kwargs)

    @unittest.skipIf(os.name != "posix", "POSIX rlimits are required for this regression test")
    def test_code_eval_rlimit_rejects_large_memory_allocation(self):
        code = "def sum_even_squares(nums):\n    waste = [0] * (10**12)\n    return len(waste)\n"

        passed, stderr = run_async(_run_sum_even_squares_tests(code))

        self.assertFalse(passed)
        self.assertIn("MemoryError", stderr)

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
        for model, health_score, health_status in (
            (first, 400, "passed"),
            (second, 250, "imperfect"),
            (third, 400, "passed"),
        ):
            model.health_score = health_score
            model.health_status = health_status
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

    def test_rate_limited_health_probe_keeps_partial_health_without_lite_eval(self):
        catalog = [_model_entry("provider/a:free")]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        fake_client = FakeOpenRouterClient(catalog, health_status_code=429)
        service._http_client = fake_client

        run_async(service.refresh_once())

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

    def test_recalculate_score_weights_eval_heavier_than_other_signals(self):
        weak_eval = ScoredOpenRouterModel(
            "provider/big-context-no-eval",
            "weak",
            metadata_score=1000,
            health_score=400,
            latency_score=75,
            lite_eval_score=0,
            instability_penalty=0,
        )
        strong_eval = ScoredOpenRouterModel(
            "provider/small-context-good-eval",
            "strong",
            metadata_score=200,
            health_score=400,
            latency_score=75,
            lite_eval_score=750,
            instability_penalty=0,
        )

        weak_eval.recalculate_score()
        strong_eval.recalculate_score()

        # non_eval * 0.8 + eval * 1.6
        self.assertEqual(weak_eval.score, round(1475 * 0.8 + 0 * 1.6))
        self.assertEqual(strong_eval.score, round(675 * 0.8 + 750 * 1.6))
        self.assertGreater(strong_eval.score, weak_eval.score)


    def test_latency_only_refresh_catches_up_lite_eval_for_recovered_models(self):
        catalog = [_model_entry("provider/a:free")]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        fake_client = FakeOpenRouterClient(catalog, health_status_code=429)
        service._http_client = fake_client

        run_async(service.refresh_once())
        first_status = run_async(service.get_status())
        self.assertEqual(first_status["snapshot"]["refreshMode"], "fullEval")
        self.assertEqual(first_status["snapshot"]["evaluatedCount"], 0)

        fake_client.health_status_code = 200
        run_async(service.refresh_once())
        second_status = run_async(service.get_status())
        snapshot = second_status["snapshot"]
        self.assertEqual(snapshot["refreshMode"], "latencyOnly")
        self.assertEqual(snapshot["evaluatedCount"], 1)
        self.assertEqual(snapshot["models"][0]["evalSummary"]["status"], "completed")
        self.assertTrue(
            any("Lite eval was additionally run" in note for note in snapshot["notes"])
        )

    def test_force_full_refresh_reruns_full_eval_even_when_catalog_unchanged(self):
        catalog = [_model_entry("provider/a:free")]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        service._http_client = FakeOpenRouterClient(catalog)

        run_async(service.refresh_once())
        run_async(service.refresh_once())
        intermediate_status = run_async(service.get_status())
        self.assertEqual(intermediate_status["snapshot"]["refreshMode"], "latencyOnly")

        run_async(service.refresh_once(force_full=True))
        forced_status = run_async(service.get_status())
        self.assertEqual(forced_status["snapshot"]["refreshMode"], "fullEval")

    def test_start_manual_full_refresh_returns_false_when_already_running(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._manual_refresh_running = True

        result = run_async(service.start_manual_full_refresh())
        self.assertFalse(result)

    def test_start_manual_full_refresh_raises_when_not_configured(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)

        with self.assertRaises(OpenRouterFreeModelsNotConfigured):
            run_async(service.start_manual_full_refresh())

    def test_get_status_exposes_manual_refresh_running_flag(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True

        idle_status = run_async(service.get_status())
        self.assertFalse(idle_status["manualRefreshRunning"])

        service._manual_refresh_running = True
        running_status = run_async(service.get_status())
        self.assertTrue(running_status["manualRefreshRunning"])


class OpenRouterFreeModelsApiTests(unittest.TestCase):
    def _app_with_role(self, role: str) -> FastAPI:
        app = FastAPI()

        @app.middleware("http")
        async def set_role(request, call_next):
            request.state.api_key_role = role
            return await call_next(request)

        app.include_router(editor_router, prefix="/v1")
        return app

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

    def test_run_endpoint_returns_409_when_already_running(self):
        class FakeService:
            async def start_manual_full_refresh(self):
                return False

            async def get_status(self):
                return {"configured": True, "manualRefreshRunning": True}

        app = self._app_with_role(ROLE_MASTER)
        app.state.openrouter_free_models_service = FakeService()
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 409)

    def test_run_endpoint_returns_status_when_started(self):
        class FakeService:
            def __init__(self):
                self.started = False

            async def start_manual_full_refresh(self):
                self.started = True
                return True

            async def get_status(self):
                return {"configured": True, "manualRefreshRunning": self.started}

        app = self._app_with_role(ROLE_MASTER)
        app.state.openrouter_free_models_service = FakeService()
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["manualRefreshRunning"])

    def test_run_endpoint_returns_503_when_not_configured(self):
        class FakeService:
            async def start_manual_full_refresh(self):
                raise OpenRouterFreeModelsNotConfigured("not configured")

        app = self._app_with_role(ROLE_MASTER)
        app.state.openrouter_free_models_service = FakeService()
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 503)

    def test_run_endpoint_returns_503_when_service_missing(self):
        app = self._app_with_role(ROLE_MASTER)
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 503)

    def test_run_endpoint_rejects_non_master_role(self):
        app = self._app_with_role(ROLE_USER)
        app.state.openrouter_free_models_service = object()

        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )
