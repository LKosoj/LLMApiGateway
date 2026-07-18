import asyncio
import inspect
import os
import re
import unittest
from unittest.mock import AsyncMock, Mock, patch

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
    OpenRouterFreeModelsStateError,
    OpenRouterFreeModelsStopError,
    ScoredOpenRouterModel,
    _catalog_fingerprint,
    _run_sum_even_squares_tests,
    _is_eligible_free_text_model,
    _score_metadata,
    parse_capability_metadata,
)
from tests.runtime_test_support import make_app_services, make_runtime_snapshot


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
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True
            service._manual_refresh_running = True

            self.assertFalse(await service.start_manual_full_refresh())
            await service.stop()

        run_async(scenario())

    def test_start_manual_full_refresh_raises_when_not_configured(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())

            with self.assertRaises(OpenRouterFreeModelsNotConfigured):
                await service.start_manual_full_refresh()
            await service.stop()

        run_async(scenario())

    def test_get_status_exposes_manual_refresh_running_flag(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True

        idle_status = run_async(service.get_status())
        self.assertFalse(idle_status["manualRefreshRunning"])

        service._manual_refresh_running = True
        running_status = run_async(service.get_status())
        self.assertTrue(running_status["manualRefreshRunning"])

    def test_stop_waits_for_blocked_periodic_and_manual_refresh_tasks(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True
            service._running = True
            http_client = Mock()
            http_client.aclose = AsyncMock()
            service._http_client = http_client
            periodic_started = asyncio.Event()
            manual_started = asyncio.Event()
            periodic_finished = asyncio.Event()
            manual_finished = asyncio.Event()

            async def blocked_refresh(*, force_full: bool = False) -> None:
                started = manual_started if force_full else periodic_started
                finished = manual_finished if force_full else periodic_finished
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await asyncio.sleep(0)
                    finished.set()

            service.refresh_once = blocked_refresh  # type: ignore[method-assign]
            periodic_task = asyncio.create_task(
                service._run_loop(),
                name="openrouter-free-models-scoring",
            )
            service._task = periodic_task
            self.assertTrue(await service.start_manual_full_refresh())
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await periodic_started.wait()
            await manual_started.wait()

            await service.stop()

            self.assertTrue(periodic_finished.is_set())
            self.assertTrue(manual_finished.is_set())
            self.assertTrue(periodic_task.done())
            self.assertTrue(manual_task.done())
            self.assertIsNone(service._task)
            self.assertIsNone(service._manual_refresh_task)
            self.assertFalse(service._running)
            self.assertFalse(service._manual_refresh_running)
            http_client.aclose.assert_not_awaited()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_stop_waits_for_active_manual_refresh_without_periodic_task(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True
            started = asyncio.Event()
            finished = asyncio.Event()

            async def blocked_refresh(*, force_full: bool = False) -> None:
                self.assertTrue(force_full)
                started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    finished.set()

            service.refresh_once = blocked_refresh  # type: ignore[method-assign]
            self.assertTrue(await service.start_manual_full_refresh())
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await started.wait()

            await service.stop()

            self.assertTrue(finished.is_set())
            self.assertTrue(manual_task.done())
            self.assertIsNone(service._manual_refresh_task)
            self.assertFalse(service._manual_refresh_running)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_completed_tasks_and_manual_task_reference_are_cleared(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True

            async def completed_refresh(*, force_full: bool = False) -> None:
                self.assertTrue(force_full)

            service.refresh_once = completed_refresh  # type: ignore[method-assign]
            self.assertTrue(await service.start_manual_full_refresh())
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await manual_task
            self.assertIsNone(service._manual_refresh_task)
            self.assertFalse(service._manual_refresh_running)

            periodic_task = asyncio.create_task(asyncio.sleep(0))
            service._task = periodic_task
            await periodic_task
            await service.stop()

            self.assertIsNone(service._task)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_concurrent_and_repeated_stop_share_cleanup(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            periodic_started = asyncio.Event()
            manual_started = asyncio.Event()

            async def blocked(started: asyncio.Event) -> None:
                started.set()
                await asyncio.Event().wait()

            periodic_task = asyncio.create_task(blocked(periodic_started))
            manual_task = asyncio.create_task(blocked(manual_started))
            service._running = True
            service._manual_refresh_running = True
            service._task = periodic_task
            service._manual_refresh_task = manual_task
            await periodic_started.wait()
            await manual_started.wait()

            await asyncio.gather(service.stop(), service.stop())
            await service.stop()

            self.assertTrue(periodic_task.done())
            self.assertTrue(manual_task.done())
            self.assertIsNone(service._stop_task)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_stop_awaits_other_task_and_reports_safe_failure(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            periodic_started = asyncio.Event()
            manual_started = asyncio.Event()
            allow_manual_cleanup = asyncio.Event()
            manual_finished = asyncio.Event()

            async def failing_periodic() -> None:
                periodic_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise RuntimeError(
                        "https://user:SUPER_SECRET@proxy.invalid"
                    ) from None

            async def delayed_manual_cleanup() -> None:
                manual_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    await allow_manual_cleanup.wait()
                    manual_finished.set()

            periodic_task = asyncio.create_task(failing_periodic())
            manual_task = asyncio.create_task(delayed_manual_cleanup())
            service._task = periodic_task
            service._manual_refresh_task = manual_task
            service._running = True
            service._manual_refresh_running = True
            await periodic_started.wait()
            await manual_started.wait()

            with self.assertLogs(
                "llm_gateway_core.services.openrouter_free_models",
                level="ERROR",
            ) as captured:
                stop_task = asyncio.create_task(service.stop())
                await asyncio.sleep(0)
                await asyncio.sleep(0)
                self.assertFalse(stop_task.done())
                self.assertFalse(manual_finished.is_set())
                allow_manual_cleanup.set()
                with self.assertRaises(OpenRouterFreeModelsStopError) as raised:
                    await stop_task

            self.assertEqual(
                raised.exception.failures,
                (("periodic", "RuntimeError"),),
            )
            self.assertNotIn("SUPER_SECRET", str(raised.exception))
            self.assertNotIn("SUPER_SECRET", "\n".join(captured.output))
            self.assertTrue(manual_finished.is_set())
            self.assertTrue(manual_task.done())
            self.assertIsNone(service._task)
            self.assertIsNone(service._manual_refresh_task)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_stop_called_from_owned_task_does_not_cancel_or_await_itself(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            start_stop = asyncio.Event()
            stop_returned = asyncio.Event()

            async def owned_task() -> None:
                await start_stop.wait()
                await service.stop()
                stop_returned.set()

            task = asyncio.create_task(owned_task())
            service._manual_refresh_task = task
            service._manual_refresh_running = True
            start_stop.set()
            await task
            await service.stop()

            self.assertTrue(stop_returned.is_set())
            self.assertIsNone(service._manual_refresh_task)
            self.assertFalse(service._manual_refresh_running)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_self_manual_stop_keeps_terminal_open_until_self_task_finishes(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True
            http_client = Mock()
            http_client.aclose = AsyncMock()
            service._http_client = http_client
            self_stop_returned = asyncio.Event()
            release_self = asyncio.Event()
            self_finished = asyncio.Event()

            async def self_stopping_refresh(*, force_full: bool = False) -> None:
                self.assertTrue(force_full)
                await service.stop()
                self_stop_returned.set()
                await release_self.wait()
                self_finished.set()

            service.refresh_once = self_stopping_refresh  # type: ignore[method-assign]
            self.assertTrue(await service.start_manual_full_refresh())
            manual_task = service._manual_refresh_task
            self.assertIsNotNone(manual_task)
            await self_stop_returned.wait()

            self.assertEqual(service._lifecycle_state.value, "stopping")
            self.assertIn(manual_task, service._stop_participants)
            self.assertIsNotNone(service._stop_cleanup_task)
            self.assertIsNotNone(service._stop_task)
            self.assertFalse(service._stop_task.done())
            http_client.aclose.assert_not_awaited()

            external_stop = asyncio.create_task(service.stop())
            await asyncio.sleep(0)
            self.assertFalse(external_stop.done())
            self.assertFalse(self_finished.is_set())

            release_self.set()
            await external_stop

            self.assertTrue(self_finished.is_set())
            self.assertTrue(manual_task.done())
            self.assertEqual(service._lifecycle_state.value, "stopped")
            self.assertIsNone(service._stop_cleanup_task)
            self.assertIsNone(service._stop_task)
            self.assertEqual(service._stop_participants, frozenset())
            http_client.aclose.assert_not_awaited()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    @unittest.skipUnless(
        hasattr(asyncio, "eager_task_factory"),
        "Python 3.12 eager task factory is required",
    )
    def test_eager_task_factory_cannot_run_refresh_before_start_commit(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()
            allow_refresh = asyncio.Event()
            http_client = Mock()

            async def assert_committed_refresh(*, force_full: bool = False) -> None:
                self.assertFalse(force_full)
                self.assertEqual(service._lifecycle_state.value, "running")
                self.assertEqual(service._lifecycle_epoch, 1)
                self.assertTrue(service._running)
                self.assertTrue(service._configured)
                self.assertIs(service._http_client, http_client)
                self.assertIs(service._task, asyncio.current_task())
                refresh_started.set()
                await allow_refresh.wait()

            service.refresh_once = assert_committed_refresh  # type: ignore[method-assign]
            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                await service.start(
                    providers_config={
                        "openrouter": ProviderDetails(
                            baseUrl="https://openrouter.ai/api/v1",
                            apikey="key",
                        )
                    },
                    http_client=http_client,
                )
            finally:
                loop.set_task_factory(previous_factory)

            periodic_task = service._task
            self.assertIsNotNone(periodic_task)
            self.assertFalse(periodic_task.done())
            await refresh_started.wait()
            allow_refresh.set()
            await service.stop()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    @unittest.skipUnless(
        hasattr(asyncio, "eager_task_factory"),
        "Python 3.12 eager task factory is required",
    )
    def test_eager_task_factory_cannot_finish_manual_before_task_commit(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            await service.start(providers_config={}, http_client=Mock())
            service._configured = True
            refresh_finished = asyncio.Event()

            async def immediate_refresh(*, force_full: bool = False) -> None:
                self.assertTrue(force_full)
                self.assertTrue(service._manual_refresh_running)
                self.assertIs(service._manual_refresh_task, asyncio.current_task())
                refresh_finished.set()

            service.refresh_once = immediate_refresh  # type: ignore[method-assign]
            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            loop.set_task_factory(asyncio.eager_task_factory)
            try:
                self.assertTrue(await service.start_manual_full_refresh())
            finally:
                loop.set_task_factory(previous_factory)

            committed_task = service._manual_refresh_task
            self.assertIsNotNone(committed_task)
            await refresh_finished.wait()
            await committed_task
            self.assertIsNone(service._manual_refresh_task)
            self.assertFalse(service._manual_refresh_running)
            await service.stop()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_task_factory_failure_leaves_clean_pre_start_state(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            loop = asyncio.get_running_loop()
            previous_factory = loop.get_task_factory()
            observed_coroutines: list[object] = []

            def failing_factory(_loop, coroutine, **_kwargs):
                observed_coroutines.append(coroutine)
                raise RuntimeError("task factory failed")

            loop.set_task_factory(failing_factory)
            try:
                with self.assertRaisesRegex(RuntimeError, "task factory failed"):
                    await service.start(
                        providers_config={
                            "openrouter": ProviderDetails(
                                baseUrl="https://openrouter.ai/api/v1",
                                apikey="key",
                            )
                        },
                        http_client=Mock(),
                    )
            finally:
                loop.set_task_factory(previous_factory)

            self.assertEqual(len(observed_coroutines), 1)
            self.assertEqual(
                inspect.getcoroutinestate(observed_coroutines[0]),
                inspect.CORO_CLOSED,
            )
            self.assertEqual(service._lifecycle_state.value, "new")
            self.assertEqual(service._lifecycle_epoch, 0)
            self.assertFalse(service._configured)
            self.assertFalse(service._running)
            self.assertIsNone(service._task)
            self.assertIsNone(service._http_client)
            self.assertIsNone(service._stop_task)

            await service.start(providers_config={}, http_client=Mock())
            await service.stop()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_duplicate_start_fails_without_overwriting_periodic_task(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()

            async def blocked_refresh(*, force_full: bool = False) -> None:
                self.assertFalse(force_full)
                refresh_started.set()
                await asyncio.Event().wait()

            service.refresh_once = blocked_refresh  # type: ignore[method-assign]
            providers = {
                "openrouter": ProviderDetails(
                    baseUrl="https://openrouter.ai/api/v1",
                    apikey="key",
                )
            }
            http_client = Mock()
            await service.start(
                providers_config=providers,
                http_client=http_client,
            )
            await refresh_started.wait()
            original_task = service._task
            original_epoch = service._lifecycle_epoch

            with self.assertRaises(OpenRouterFreeModelsStateError):
                await service.start(
                    providers_config=providers,
                    http_client=Mock(),
                )

            self.assertIs(service._task, original_task)
            self.assertEqual(service._lifecycle_epoch, original_epoch)
            self.assertIs(service._http_client, http_client)
            await service.stop()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_start_during_blocked_stop_fails_without_new_epoch_or_task(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()
            cleanup_started = asyncio.Event()
            allow_cleanup = asyncio.Event()

            async def blocked_refresh(*, force_full: bool = False) -> None:
                refresh_started.set()
                try:
                    await asyncio.Event().wait()
                finally:
                    cleanup_started.set()
                    await allow_cleanup.wait()

            service.refresh_once = blocked_refresh  # type: ignore[method-assign]
            providers = {
                "openrouter": ProviderDetails(
                    baseUrl="https://openrouter.ai/api/v1",
                    apikey="key",
                )
            }
            await service.start(providers_config=providers, http_client=Mock())
            await refresh_started.wait()
            original_task = service._task
            original_epoch = service._lifecycle_epoch
            stop_waiter = asyncio.create_task(service.stop())
            await cleanup_started.wait()

            with self.assertRaises(OpenRouterFreeModelsStateError):
                await service.start(providers_config=providers, http_client=Mock())

            self.assertEqual(service._lifecycle_epoch, original_epoch)
            self.assertIn(original_task, service._stop_participants)
            self.assertIsNone(service._task)
            allow_cleanup.set()
            await stop_waiter
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_cancel_handler_reentrant_stop_does_not_deadlock_shared_stop(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()
            reentrant_stop_started = asyncio.Event()
            reentrant_stop_returned = asyncio.Event()

            async def reentrant_refresh(*, force_full: bool = False) -> None:
                refresh_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    reentrant_stop_started.set()
                    await service.stop()
                    reentrant_stop_returned.set()
                    raise

            service.refresh_once = reentrant_refresh  # type: ignore[method-assign]
            await service.start(
                providers_config={
                    "openrouter": ProviderDetails(
                        baseUrl="https://openrouter.ai/api/v1",
                        apikey="key",
                    )
                },
                http_client=Mock(),
            )
            await refresh_started.wait()

            await asyncio.wait_for(service.stop(), timeout=1)

            self.assertTrue(reentrant_stop_started.is_set())
            self.assertTrue(reentrant_stop_returned.is_set())
            self.assertIsNone(service._stop_task)
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_cancelled_only_stop_waiter_does_not_retain_internal_stop_task(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()
            cleanup_started = asyncio.Event()
            allow_cleanup = asyncio.Event()

            async def blocked_cleanup(*, force_full: bool = False) -> None:
                refresh_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    cleanup_started.set()
                    await allow_cleanup.wait()
                    raise RuntimeError("SUPER_SECRET_ORPHAN_FAILURE") from None

            service.refresh_once = blocked_cleanup  # type: ignore[method-assign]
            await service.start(
                providers_config={
                    "openrouter": ProviderDetails(
                        baseUrl="https://openrouter.ai/api/v1",
                        apikey="key",
                    )
                },
                http_client=Mock(),
            )
            await refresh_started.wait()
            stop_waiter = asyncio.create_task(service.stop())
            await cleanup_started.wait()
            internal_stop_task = service._stop_task
            self.assertIsNotNone(internal_stop_task)

            stop_waiter.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await stop_waiter
            self.assertFalse(internal_stop_task.done())

            allow_cleanup.set()
            for _ in range(20):
                if service._stop_task is None:
                    break
                await asyncio.sleep(0)

            self.assertTrue(internal_stop_task.done())
            self.assertIsNone(service._stop_task)
            self.assertEqual(service._lifecycle_state.value, "stopped")
            with self.assertRaises(OpenRouterFreeModelsStopError) as replayed:
                await service.stop()
            self.assertEqual(
                replayed.exception.failures,
                (("periodic", "RuntimeError"),),
            )
            self.assertNotIn("SUPER_SECRET", str(replayed.exception))
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def test_instance_is_permanently_bound_to_first_lifecycle_loop(self):
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        run_async(service.stop())
        original_state = (
            service._loop,
            service._lifecycle_state,
            service._lifecycle_epoch,
            service._stop_task,
            service._task,
            service._manual_refresh_task,
        )

        with self.assertRaises(OpenRouterFreeModelsStateError):
            run_async(service.start(providers_config={}, http_client=Mock()))
        with self.assertRaises(OpenRouterFreeModelsStateError):
            run_async(service.stop())

        self.assertEqual(
            (
                service._loop,
                service._lifecycle_state,
                service._lifecycle_epoch,
                service._stop_task,
                service._task,
                service._manual_refresh_task,
            ),
            original_state,
        )

    def test_stop_failure_replays_until_successful_start_resets_epoch(self):
        async def scenario() -> None:
            service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
            refresh_started = asyncio.Event()

            async def failing_on_cancel(*, force_full: bool = False) -> None:
                refresh_started.set()
                try:
                    await asyncio.Event().wait()
                except asyncio.CancelledError:
                    raise RuntimeError("SUPER_SECRET_STOP_FAILURE") from None

            service.refresh_once = failing_on_cancel  # type: ignore[method-assign]
            await service.start(
                providers_config={
                    "openrouter": ProviderDetails(
                        baseUrl="https://openrouter.ai/api/v1",
                        apikey="key",
                    )
                },
                http_client=Mock(),
            )
            failed_epoch = service._lifecycle_epoch
            await refresh_started.wait()

            with self.assertRaises(OpenRouterFreeModelsStopError) as first:
                await service.stop()
            with self.assertRaises(OpenRouterFreeModelsStopError) as replayed:
                await service.stop()

            self.assertEqual(first.exception.failures, (("periodic", "RuntimeError"),))
            self.assertEqual(replayed.exception.failures, first.exception.failures)
            self.assertNotIn("SUPER_SECRET", str(first.exception))
            self.assertEqual(service._lifecycle_epoch, failed_epoch)

            await service.start(providers_config={}, http_client=Mock())
            self.assertEqual(service._lifecycle_epoch, failed_epoch + 1)
            self.assertIsNone(service._stop_failures)
            await service.stop()
            await service.stop()
            self._assert_no_pending_openrouter_tasks()

        run_async(scenario())

    def _assert_no_pending_openrouter_tasks(self) -> None:
        current_task = asyncio.current_task()
        pending_names = [
            task.get_name()
            for task in asyncio.all_tasks()
            if task is not current_task
            and not task.done()
            and task.get_name().startswith("openrouter-free-models")
        ]
        self.assertEqual(pending_names, [])


class OpenRouterFreeModelsApiTests(unittest.TestCase):
    def _app_with_role(self, role: str, service=None) -> FastAPI:
        app = FastAPI()
        services = make_app_services(
            openrouter_free_models_service=(
                service if service is not None else OpenRouterFreeModelsService()
            )
        )
        runtime_snapshot = make_runtime_snapshot(http_client=services.http_client)
        app.state.services = services
        app.state.openrouter_free_models_service = object()

        @app.middleware("http")
        async def set_role(request, call_next):
            request.state.api_key_role = role
            request.state.runtime_snapshot = runtime_snapshot
            return await call_next(request)

        app.include_router(editor_router, prefix="/v1")
        return app

    def test_status_endpoint_fails_closed_without_typed_runtime(self):
        app = FastAPI()
        app.include_router(editor_router, prefix="/v1")
        response = TestClient(app).get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 500)

    def test_status_endpoint_returns_service_payload(self):
        class FakeService:
            async def get_status(self):
                return {"configured": True, "snapshot": {"models": []}}

        app = self._app_with_role(ROLE_USER, FakeService())
        response = TestClient(app).get("/v1/openrouter/free-models")

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["configured"])

    def test_run_endpoint_returns_409_when_already_running(self):
        class FakeService:
            async def start_manual_full_refresh(self):
                return False

            async def get_status(self):
                return {"configured": True, "manualRefreshRunning": True}

        app = self._app_with_role(ROLE_MASTER, FakeService())
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

        app = self._app_with_role(ROLE_MASTER, FakeService())
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertTrue(payload["manualRefreshRunning"])

    def test_run_endpoint_returns_503_when_not_configured(self):
        class FakeService:
            async def start_manual_full_refresh(self):
                raise OpenRouterFreeModelsNotConfigured("not configured")

        app = self._app_with_role(ROLE_MASTER, FakeService())
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 503)

    def test_run_endpoint_fails_closed_when_typed_runtime_is_missing(self):
        app = FastAPI()

        @app.middleware("http")
        async def set_role(request, call_next):
            request.state.api_key_role = ROLE_MASTER
            return await call_next(request)

        app.include_router(editor_router, prefix="/v1")
        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 500)

    def test_run_endpoint_rejects_non_master_role(self):
        app = self._app_with_role(ROLE_USER)
        app.state.openrouter_free_models_service = object()

        response = TestClient(app).post("/v1/openrouter/free-models/run")

        self.assertEqual(response.status_code, 403)
        self.assertEqual(
            response.json(),
            {"detail": "This endpoint is reserved for the master API key"},
        )


class ParseCapabilityMetadataTests(unittest.TestCase):
    """F-auto: pure parsing of supports_vision/supports_tools/context_window."""

    def test_vision_tools_and_context_window_from_full_entry(self):
        entry = {
            "id": "vendor/model-a",
            "architecture": {"input_modalities": ["text", "image"]},
            "supported_parameters": ["tools", "response_format"],
            "context_length": 128000,
        }
        metadata = parse_capability_metadata(entry)
        self.assertTrue(metadata.supports_vision)
        self.assertTrue(metadata.supports_tools)
        self.assertEqual(metadata.context_window, 128000)

    def test_no_image_input_modality_is_false_not_none(self):
        entry = {
            "id": "vendor/model-b",
            "architecture": {"input_modalities": ["text"]},
            "supported_parameters": [],
        }
        metadata = parse_capability_metadata(entry)
        self.assertFalse(metadata.supports_vision)
        self.assertFalse(metadata.supports_tools)

    def test_missing_architecture_leaves_supports_vision_none(self):
        entry = {"id": "vendor/model-c", "supported_parameters": ["tools"]}
        metadata = parse_capability_metadata(entry)
        self.assertIsNone(metadata.supports_vision)
        self.assertTrue(metadata.supports_tools)

    def test_context_window_falls_back_to_top_provider_context_length(self):
        entry = {"id": "vendor/model-d", "top_provider": {"context_length": 64000}}
        metadata = parse_capability_metadata(entry)
        self.assertEqual(metadata.context_window, 64000)

    def test_missing_context_length_is_none(self):
        entry = {"id": "vendor/model-e"}
        metadata = parse_capability_metadata(entry)
        self.assertIsNone(metadata.context_window)

    def test_missing_supported_parameters_leaves_supports_tools_none(self):
        # A catalog entry with no `supported_parameters` key at all (plain
        # OpenAI-style /models response) must not be read as "tools not
        # supported" -- that's a materialized False, not "the catalog didn't
        # say". Only a present `supported_parameters` list may resolve
        # supports_tools to True/False.
        entry = {"id": "vendor/model-f"}
        metadata = parse_capability_metadata(entry)
        self.assertIsNone(metadata.supports_tools)

    def test_non_list_supported_parameters_leaves_supports_tools_none(self):
        entry = {"id": "vendor/model-g", "supported_parameters": None}
        metadata = parse_capability_metadata(entry)
        self.assertIsNone(metadata.supports_tools)


class OpenRouterCapabilityIndexTests(unittest.TestCase):
    """F-auto: full-catalog capability index built during _refresh_once."""

    def test_capability_index_covers_full_catalog_including_paid_models(self):
        free_entry = _model_entry("provider/free-model:free")
        paid_entry = {
            "id": "provider/paid-model",
            "name": "provider/paid-model",
            "created": 1_760_000_000,
            "context_length": 200000,
            "pricing": {"prompt": "0.000003", "completion": "0.000006"},
            "supported_parameters": ["tools"],
            "architecture": {
                "input_modalities": ["text", "image"],
                "output_modalities": ["text"],
            },
            "expiration_date": None,
        }
        catalog = [free_entry, paid_entry]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        service._http_client = FakeOpenRouterClient(catalog)

        run_async(service.refresh_once())

        status = run_async(service.get_status())
        scored_model_ids = {model["id"] for model in status["snapshot"]["models"]}
        self.assertNotIn("provider/paid-model", scored_model_ids)

        index = run_async(service.get_capability_index())
        self.assertIn("free-model", index)
        self.assertIn("paid-model", index)
        self.assertTrue(index["paid-model"].supports_vision)
        self.assertTrue(index["paid-model"].supports_tools)
        self.assertEqual(index["paid-model"].context_window, 200000)

    def test_capability_index_tie_break_keeps_larger_context_window(self):
        entry_small = {
            "id": "vendorA/shared-model",
            "context_length": 8000,
            "supported_parameters": [],
        }
        entry_large = {
            "id": "vendorB/shared-model",
            "context_length": 128000,
            "supported_parameters": ["tools"],
        }
        catalog = [entry_small, entry_large]
        service = OpenRouterFreeModelsService(time_func=lambda: 1_770_000_000)
        service._configured = True
        service._provider_config = ProviderDetails(baseUrl="https://openrouter.ai/api/v1", apikey="key")
        service._provider_api_key = "key"
        service._http_client = FakeOpenRouterClient(catalog)

        run_async(service.refresh_once())
        index = run_async(service.get_capability_index())

        self.assertEqual(index["shared-model"].context_window, 128000)
        self.assertTrue(index["shared-model"].supports_tools)

    def test_capability_index_defaults_to_empty_before_first_refresh(self):
        service = OpenRouterFreeModelsService()
        index = run_async(service.get_capability_index())
        self.assertEqual(index, {})
