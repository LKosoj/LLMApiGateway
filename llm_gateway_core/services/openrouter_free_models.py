from __future__ import annotations

import asyncio
import ast
import copy
import hashlib
import json
import logging
import math
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config.loader import ProviderDetails, resolve_provider_api_key_value
from ..utils.api_keys import has_api_key, select_next_api_key

logger = logging.getLogger(__name__)

OPENROUTER_PROVIDER_NAME = "openrouter"
OPENROUTER_HOST = "openrouter.ai"
REFRESH_INTERVAL_SECONDS = 8 * 60 * 60
MIN_CONTEXT_LENGTH = 8192
MAX_LITE_EVAL_POINTS = 750
# Eval тесты весомее остальных метрик: модель с большим контекстом, но
# проваленными тестами не должна обгонять модель с реально работающими навыками.
NON_EVAL_SCORE_WEIGHT = 0.8
EVAL_SCORE_WEIGHT = 1.6
HEALTH_PROBE_TIMEOUT_SECONDS = 30.0
HEALTH_PROBE_MAX_TOKENS = 16
LITE_EVAL_TIMEOUT_SECONDS = 45.0
CODE_EVAL_TIMEOUT_SECONDS = 2.0


class OpenRouterFreeModelsNotConfigured(RuntimeError):
    """Поднимается при попытке управления сервисом, который не сконфигурирован."""


@dataclass
class LiteEvalTaskResult:
    id: str
    points: int
    max_points: int
    status: str
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "points": self.points,
            "maxPoints": self.max_points,
            "status": self.status,
            "details": self.details,
        }


@dataclass
class ScoredOpenRouterModel:
    id: str
    name: str
    metadata_score: int
    health_score: int = 0
    latency_score: int = 0
    lite_eval_score: int = 0
    instability_penalty: int = 0
    score: int = 0
    context_length: int = 0
    max_completion_tokens: int | None = None
    created: int | None = None
    pricing: dict[str, Any] = field(default_factory=dict)
    supported_parameters: list[str] = field(default_factory=list)
    supports_tools: bool = False
    supports_tool_choice: bool = False
    supports_structured_outputs: bool = False
    supports_response_format: bool = False
    supports_reasoning: bool = False
    supports_include_reasoning: bool = False
    supports_seed: bool = False
    supports_stop: bool = False
    latency_ms: int | None = None
    health_status: str = "not_probed"
    reason: str = ""
    eval_summary: dict[str, Any] = field(default_factory=dict)

    def recalculate_score(self) -> None:
        non_eval = self.metadata_score + self.health_score + self.latency_score - self.instability_penalty
        self.score = round(non_eval * NON_EVAL_SCORE_WEIGHT + self.lite_eval_score * EVAL_SCORE_WEIGHT)

    @property
    def base_score(self) -> int:
        return self.metadata_score + self.health_score + self.latency_score - self.instability_penalty

    def to_dict(self, rank: int) -> dict[str, Any]:
        return {
            "rank": rank,
            "id": self.id,
            "name": self.name,
            "score": self.score,
            "metadataScore": self.metadata_score,
            "healthScore": self.health_score,
            "latencyScore": self.latency_score,
            "liteEvalScore": self.lite_eval_score,
            "instabilityPenalty": self.instability_penalty,
            "contextLength": self.context_length,
            "maxCompletionTokens": self.max_completion_tokens,
            "supportsTools": self.supports_tools,
            "supportsToolChoice": self.supports_tool_choice,
            "supportsStructuredOutputs": self.supports_structured_outputs,
            "supportsResponseFormat": self.supports_response_format,
            "supportsReasoning": self.supports_reasoning,
            "supportsIncludeReasoning": self.supports_include_reasoning,
            "supportsSeed": self.supports_seed,
            "supportsStop": self.supports_stop,
            "latencyMs": self.latency_ms,
            "healthStatus": self.health_status,
            "evalSummary": self.eval_summary,
            "reason": self.reason,
            "pricing": self.pricing,
            "created": self.created,
        }


@dataclass
class OpenRouterFreeModelsSnapshot:
    updated_at: str
    source: str
    refresh_mode: str
    ranking_version: str
    catalog_fingerprint: str
    catalog_count: int
    eligible_count: int
    evaluated_count: int
    base_url: str
    models: list[ScoredOpenRouterModel]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updatedAt": self.updated_at,
            "source": self.source,
            "refreshMode": self.refresh_mode,
            "rankingVersion": self.ranking_version,
            "catalogFingerprint": self.catalog_fingerprint,
            "catalogCount": self.catalog_count,
            "eligibleCount": self.eligible_count,
            "evaluatedCount": self.evaluated_count,
            "baseUrl": self.base_url,
            "models": [model.to_dict(index + 1) for index, model in enumerate(self.models)],
            "notes": list(self.notes),
        }


class OpenRouterFreeModelsService:
    def __init__(
        self,
        *,
        refresh_interval_seconds: int = REFRESH_INTERVAL_SECONDS,
        time_func=time.time,
    ) -> None:
        self.refresh_interval_seconds = refresh_interval_seconds
        self._time_func = time_func
        self._task: asyncio.Task | None = None
        self._lock = asyncio.Lock()
        self._snapshot: OpenRouterFreeModelsSnapshot | None = None
        self._configured = False
        self._running = False
        self._last_error: str | None = None
        self._last_checked_at: str | None = None
        self._next_refresh_at: str | None = None
        self._provider_name = OPENROUTER_PROVIDER_NAME
        self._provider_config: ProviderDetails | None = None
        self._provider_api_key: str | None = None
        self._http_client: httpx.AsyncClient | None = None
        self._manual_refresh_running = False
        self._manual_refresh_task: asyncio.Task | None = None

    async def start(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        http_client: httpx.AsyncClient,
    ) -> None:
        provider_config = providers_config.get(self._provider_name)
        provider_api_key = self._resolve_openrouter_api_key(provider_config)
        if provider_config is None or not has_api_key(provider_api_key) or not self._is_official_openrouter_url(provider_config.baseUrl):
            self._configured = False
            self._last_error = None
            logger.info("OpenRouter free model scoring is disabled: official OpenRouter provider/key is not configured.")
            return

        self._provider_config = provider_config
        self._provider_api_key = provider_api_key
        self._http_client = http_client
        self._configured = True
        self._running = True
        self._task = asyncio.create_task(self._run_loop(), name="openrouter-free-models-scoring")
        logger.info("OpenRouter free model scoring started with %d second interval.", self.refresh_interval_seconds)

    async def stop(self) -> None:
        self._running = False
        task = self._task
        self._task = None
        if task is not None:
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    async def get_status(self) -> dict[str, Any]:
        async with self._lock:
            snapshot = self._snapshot.to_dict() if self._snapshot else None
            return {
                "configured": self._configured,
                "running": self._running,
                "provider": self._provider_name,
                "intervalSeconds": self.refresh_interval_seconds,
                "lastCheckedAt": self._last_checked_at,
                "nextRefreshAt": self._next_refresh_at,
                "lastError": self._last_error,
                "manualRefreshRunning": self._manual_refresh_running,
                "snapshot": snapshot,
            }

    async def refresh_once(self, *, force_full: bool = False) -> None:
        if not self._configured or self._provider_config is None or not has_api_key(self._provider_api_key) or self._http_client is None:
            return

        try:
            await self._refresh_once(force_full=force_full)
        except Exception as exc:
            logger.exception("OpenRouter free model scoring refresh failed.")
            async with self._lock:
                self._last_checked_at = _utc_now_iso(self._time_func)
                self._last_error = str(exc)

    async def start_manual_full_refresh(self) -> bool:
        """Запустить полноценную переоценку в фоне. Возвращает False если уже запущено."""
        if not self._configured:
            raise OpenRouterFreeModelsNotConfigured(
                "OpenRouter free model scoring is not configured."
            )
        async with self._lock:
            if self._manual_refresh_running:
                return False
            self._manual_refresh_running = True
        self._manual_refresh_task = asyncio.create_task(
            self._run_manual_refresh(), name="openrouter-free-models-manual-refresh"
        )
        return True

    async def _run_manual_refresh(self) -> None:
        try:
            await self.refresh_once(force_full=True)
        finally:
            async with self._lock:
                self._manual_refresh_running = False

    async def _run_loop(self) -> None:
        while self._running:
            started_at = self._time_func()
            await self.refresh_once()
            next_refresh_at = started_at + self.refresh_interval_seconds
            async with self._lock:
                self._next_refresh_at = _timestamp_to_iso(next_refresh_at)
            await asyncio.sleep(max(0.0, next_refresh_at - self._time_func()))

    async def _refresh_once(self, *, force_full: bool = False) -> None:
        assert self._provider_config is not None
        assert self._provider_api_key is not None
        assert self._http_client is not None

        catalog = await self._fetch_openrouter_catalog(
            self._http_client,
            self._provider_config,
            self._select_openrouter_api_key(),
        )
        eligible_entries = [entry for entry in catalog if _is_eligible_free_text_model(entry, self._time_func())]
        fingerprint = _catalog_fingerprint(eligible_entries)

        async with self._lock:
            previous_snapshot = copy.deepcopy(self._snapshot)

        if not force_full and previous_snapshot and previous_snapshot.catalog_fingerprint == fingerprint:
            snapshot = await self._refresh_latency_only(
                previous_snapshot,
                catalog_count=len(catalog),
                eligible_count=len(eligible_entries),
            )
        else:
            snapshot = await self._refresh_full(
                eligible_entries,
                catalog_count=len(catalog),
                fingerprint=fingerprint,
            )

        async with self._lock:
            self._snapshot = snapshot
            self._last_checked_at = snapshot.updated_at
            self._last_error = None

    async def _fetch_openrouter_catalog(
        self,
        http_client: httpx.AsyncClient,
        provider_config: ProviderDetails,
        provider_api_key: str,
    ) -> list[dict[str, Any]]:
        target_url = f"{provider_config.baseUrl.rstrip('/')}/models"
        response = await http_client.get(
            target_url,
            headers={
                "Accept": "application/json",
                "Authorization": f"Bearer {provider_api_key}",
            },
        )
        response.raise_for_status()
        payload = response.json()
        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raise ValueError("OpenRouter /models response must contain a data list.")
        return [entry for entry in raw_models if isinstance(entry, dict)]

    async def _refresh_full(
        self,
        eligible_entries: list[dict[str, Any]],
        *,
        catalog_count: int,
        fingerprint: str,
    ) -> OpenRouterFreeModelsSnapshot:
        models = [_score_metadata(entry, self._time_func()) for entry in eligible_entries]
        models.sort(key=_rank_sort_key)
        for model in models:
            await self._apply_health_probe(model)

        evaluated_count = await self._apply_lite_evals(models)
        for model in models:
            if not model.eval_summary:
                model.eval_summary = _not_evaluated_summary("health_probe_failed")
            model.reason = _build_reason(model)
            model.recalculate_score()

        models.sort(key=_rank_sort_key)
        return OpenRouterFreeModelsSnapshot(
            updated_at=_utc_now_iso(self._time_func),
            source="openrouter-models-api",
            refresh_mode="fullEval",
            ranking_version="openrouter-free-v1",
            catalog_fingerprint=fingerprint,
            catalog_count=catalog_count,
            eligible_count=len(eligible_entries),
            evaluated_count=evaluated_count,
            base_url=self._provider_config.baseUrl if self._provider_config else "",
            models=models,
            notes=[
                "Eligible pool: free text OpenRouter models with at least 8k context and no expired catalog entry.",
                "Full evaluation probes every eligible model and runs lite eval for every model that passes health.",
                "Final score = round((metadataScore + healthScore + latencyScore - instabilityPenalty) * 0.8 + liteEvalScore * 1.6) — eval tests weigh twice the other metrics.",
                "When the eligible model list is unchanged, scheduled refreshes update health/latency and catch up lite eval for models recovered from a failing health probe.",
            ],
        )

    async def _refresh_latency_only(
        self,
        previous_snapshot: OpenRouterFreeModelsSnapshot,
        *,
        catalog_count: int,
        eligible_count: int,
    ) -> OpenRouterFreeModelsSnapshot:
        models = copy.deepcopy(previous_snapshot.models)
        for model in models:
            await self._apply_health_probe(model)

        # Догнать lite eval для моделей, которые в прошлый раз не прошли health
        # (поэтому evaluated_count «застревал»), а сейчас стали passed/imperfect.
        pending = [model for model in models if _needs_lite_eval(model)]
        newly_evaluated = await self._apply_lite_evals(pending)

        for model in models:
            if not model.eval_summary:
                model.eval_summary = _not_evaluated_summary("health_probe_failed")
            model.reason = _build_reason(model)
            model.recalculate_score()
        models.sort(key=_rank_sort_key)

        notes = [
            "Eligible model list did not change; only health and latency probes were refreshed.",
            "Existing metadata and lite eval scores were reused from the previous full evaluation.",
        ]
        if newly_evaluated:
            notes.append(
                f"Lite eval was additionally run for {newly_evaluated} model(s) "
                "that recovered from a previously failing health probe."
            )

        return OpenRouterFreeModelsSnapshot(
            updated_at=_utc_now_iso(self._time_func),
            source="openrouter-models-api",
            refresh_mode="latencyOnly",
            ranking_version=previous_snapshot.ranking_version,
            catalog_fingerprint=previous_snapshot.catalog_fingerprint,
            catalog_count=catalog_count,
            eligible_count=eligible_count,
            evaluated_count=previous_snapshot.evaluated_count + newly_evaluated,
            base_url=previous_snapshot.base_url,
            models=models,
            notes=notes,
        )

    async def _apply_health_probe(self, model: ScoredOpenRouterModel) -> None:
        model.health_score = 0
        model.latency_score = 0
        model.latency_ms = None
        model.health_status = "not_probed"
        model.instability_penalty = 0
        try:
            started_at = self._time_func()
            payload = {
                "model": model.id,
                "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                "max_tokens": HEALTH_PROBE_MAX_TOKENS,
                "temperature": 0,
                "usage": {"include": True},
            }
            response = await self._chat_completion(payload, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
            model.latency_ms = max(0, int((self._time_func() - started_at) * 1000))
            content = _extract_chat_content(response)
            if "OK" in content.upper():
                model.health_status = "passed"
                model.health_score = 400
            else:
                model.health_status = "imperfect"
                model.health_score = 250
            model.latency_score = _latency_score(model.latency_ms)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            model.health_status = f"http_{status_code}"
            model.health_score = 100 if status_code == 429 else 0
            model.latency_score = 0
            model.latency_ms = None
            if status_code == 429:
                model.instability_penalty += 25
        except Exception as exc:
            model.health_status = exc.__class__.__name__
            model.health_score = 0
            model.latency_score = 0
            model.latency_ms = None
            model.instability_penalty += 50
        finally:
            model.recalculate_score()

    async def _apply_lite_evals(self, models: list[ScoredOpenRouterModel]) -> int:
        evaluated_count = 0
        for model in models:
            if not _health_status_allows_lite_eval(model.health_status):
                model.eval_summary = _not_evaluated_summary(_lite_eval_skip_reason(model.health_status))
                continue
            model.eval_summary = await self._run_lite_eval_suite(model)
            model.lite_eval_score = int(model.eval_summary.get("points", 0))
            model.recalculate_score()
            evaluated_count += 1
        return evaluated_count

    async def _run_lite_eval_suite(self, model: ScoredOpenRouterModel) -> dict[str, Any]:
        task_runners = (
            ("instruction_following_lite", 200, self._run_instruction_following_lite_task),
            ("tool_call_lite", 200, self._run_tool_call_lite_task),
            ("code_unit_lite", 200, self._run_code_unit_lite_task),
            ("symbolic_math_lite", 100, self._run_symbolic_math_lite_task),
            ("simpleqa_lite", 50, self._run_simpleqa_lite_task),
        )
        tasks: list[LiteEvalTaskResult] = []
        for task_id, max_points, runner in task_runners:
            try:
                tasks.append(await runner(model))
            except Exception as exc:
                tasks.append(
                    LiteEvalTaskResult(
                        id=task_id,
                        points=0,
                        max_points=max_points,
                        status="error",
                        details={"error": exc.__class__.__name__},
                    )
                )
        points = sum(task.points for task in tasks)
        max_points = sum(task.max_points for task in tasks)
        return {
            "suite": "lite-agent-eval-v1",
            "status": "completed",
            "points": points,
            "maxPoints": max_points,
            "passed": sum(1 for task in tasks if task.status == "passed"),
            "total": len(tasks),
            "updatedAt": _utc_now_iso(self._time_func),
            "tasks": [task.to_dict() for task in tasks],
        }

    async def _run_instruction_following_lite_task(self, model: ScoredOpenRouterModel) -> LiteEvalTaskResult:
        prompt = (
            "Return exactly 4 lines.\n"
            "Line 1 must be exactly: STATUS: READY\n"
            "Line 2 must contain the word ROUTER exactly twice.\n"
            "Line 3 must be valid JSON with exactly keys \"mode\" and \"count\"; "
            "\"mode\" must be \"eval\" and \"count\" must be 3.\n"
            "Line 4 must be exactly: DONE\n"
            "No markdown and no extra text."
        )
        response = await self._chat_completion(_eval_payload(model, prompt, max_tokens=120), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        content = _extract_chat_content(response).strip()
        lines = [line.strip() for line in content.splitlines() if line.strip()]
        json_line = _extract_json_object(lines[2]) if len(lines) >= 3 else {}
        details = {
            "exactlyFourLines": len(lines) == 4,
            "lineOneExact": len(lines) >= 1 and lines[0] == "STATUS: READY",
            "routerTwice": len(lines) >= 2 and len(re.findall(r"\bROUTER\b", lines[1])) == 2,
            "jsonLineValid": json_line == {"mode": "eval", "count": 3},
            "lineFourExact": len(lines) >= 4 and lines[3] == "DONE",
        }
        score = sum(1 for ok in details.values() if ok)
        points = round(200 * score / len(details))
        return LiteEvalTaskResult(
            id="instruction_following_lite",
            points=points,
            max_points=200,
            status="passed" if points == 200 else "failed",
            details=details,
        )

    async def _run_tool_call_lite_task(self, model: ScoredOpenRouterModel) -> LiteEvalTaskResult:
        prompt = (
            "Available tools:\n"
            "create_ticket(title, priority, assignee, due_date)\n"
            "send_email(to, subject, body)\n"
            "search_docs(query)\n\n"
            "User request: Create a high-priority bug ticket titled "
            "\"Login fails after password reset\" assigned to Ana, due 2026-05-12. "
            "Do not send email.\n"
            "Return only JSON: {\"tool\":\"...\",\"arguments\":{...}}"
        )
        response = await self._chat_completion(_eval_payload(model, prompt, max_tokens=220), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        parsed = _extract_json_object(_extract_chat_content(response))
        arguments = parsed.get("arguments") if isinstance(parsed.get("arguments"), dict) else {}
        title = str(arguments.get("title", ""))
        details = {
            "jsonObject": bool(parsed),
            "toolCorrect": parsed.get("tool") == "create_ticket",
            "priorityCorrect": str(arguments.get("priority", "")).lower() == "high",
            "assigneeCorrect": arguments.get("assignee") == "Ana",
            "dueDateCorrect": arguments.get("due_date") == "2026-05-12",
            "titleCorrect": "login fails" in title.lower() and "password reset" in title.lower(),
        }
        score = sum(1 for ok in details.values() if ok)
        points = round(200 * score / len(details))
        return LiteEvalTaskResult(
            id="tool_call_lite",
            points=points,
            max_points=200,
            status="passed" if points == 200 else "failed",
            details=details,
        )

    async def _run_code_unit_lite_task(self, model: ScoredOpenRouterModel) -> LiteEvalTaskResult:
        prompt = (
            "Return only JSON with one key \"code\". The value must be Python code defining:\n"
            "def sum_even_squares(nums: list[int]) -> int:\n"
            "It must return the sum of squares of even integers in nums. No imports."
        )
        response = await self._chat_completion(_eval_payload(model, prompt, max_tokens=320), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        code = _extract_python_code(_extract_chat_content(response))
        safety_error = _python_code_safety_error(code)
        details: dict[str, Any] = {
            "codePresent": bool(code.strip()),
            "safeAst": safety_error is None,
            "safetyError": safety_error,
            "unitTestsPassed": False,
            "stderr": "",
        }
        if safety_error is None:
            passed, stderr = await _run_sum_even_squares_tests(code)
            details["unitTestsPassed"] = passed
            details["stderr"] = stderr[:240]

        scored_checks = {
            "codePresent": details["codePresent"],
            "safeAst": details["safeAst"],
            "unitTestsPassed": details["unitTestsPassed"],
        }
        score = sum(1 for ok in scored_checks.values() if ok)
        points = round(200 * score / len(scored_checks))
        return LiteEvalTaskResult(
            id="code_unit_lite",
            points=points,
            max_points=200,
            status="passed" if points == 200 else "failed",
            details=details,
        )

    async def _run_symbolic_math_lite_task(self, model: ScoredOpenRouterModel) -> LiteEvalTaskResult:
        values = _symbolic_math_values(int(self._time_func()) // REFRESH_INTERVAL_SECONDS)
        prompt = (
            f"A notebook has {values['total_pages']} pages. Mira writes {values['weekday_pages']} pages every weekday "
            f"for {values['weeks']} weeks and {values['weekend_pages']} pages on each weekend day. "
            "How many pages remain? Return only the integer."
        )
        response = await self._chat_completion(_eval_payload(model, prompt, max_tokens=80), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        answer = _extract_chat_content(response)
        parsed_answer = _first_int(answer)
        expected = values["remaining_pages"]
        details = {
            "expected": expected,
            "received": answer[:80],
            "parsedAnswer": parsed_answer,
        }
        return LiteEvalTaskResult(
            id="symbolic_math_lite",
            points=100 if parsed_answer == expected else 0,
            max_points=100,
            status="passed" if parsed_answer == expected else "failed",
            details=details,
        )

    async def _run_simpleqa_lite_task(self, model: ScoredOpenRouterModel) -> LiteEvalTaskResult:
        prompt = (
            "Who wrote the novel \"The Left Hand of Darkness\"? "
            "If unsure, answer UNKNOWN. Return only the answer."
        )
        response = await self._chat_completion(_eval_payload(model, prompt, max_tokens=40), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        answer = _normalize_simple_answer(_extract_chat_content(response))
        correct_aliases = {"ursulakleguin", "ursulaleguin"}
        is_correct = answer in correct_aliases
        is_unknown = answer == "unknown"
        details = {
            "expected": "Ursula K. Le Guin",
            "normalizedAnswer": answer,
            "correct": is_correct,
            "unknown": is_unknown,
        }
        points = 50 if is_correct else 20 if is_unknown else 0
        return LiteEvalTaskResult(
            id="simpleqa_lite",
            points=points,
            max_points=50,
            status="passed" if is_correct else "not_attempted" if is_unknown else "failed",
            details=details,
        )

    async def _chat_completion(self, payload: dict[str, Any], *, timeout: float) -> dict[str, Any]:
        assert self._http_client is not None
        assert self._provider_config is not None
        provider_api_key = self._select_openrouter_api_key()
        target_url = f"{self._provider_config.baseUrl.rstrip('/')}/chat/completions"
        response = await self._http_client.post(
            target_url,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bearer {provider_api_key}",
                "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
                "X-Title": "LLMGateway OpenRouter Free Model Scoring",
            },
            json=payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()

    def _resolve_openrouter_api_key(self, provider_config: ProviderDetails | None) -> str | None:
        if provider_config is None:
            return None
        try:
            return resolve_provider_api_key_value(provider_config.apikey)
        except Exception:
            return None

    def _select_openrouter_api_key(self) -> str:
        api_key = select_next_api_key(self._provider_api_key)
        if api_key is None:
            raise RuntimeError("OpenRouter API key is not configured.")
        return api_key

    def _is_official_openrouter_url(self, base_url: str) -> bool:
        parsed = urlparse(base_url)
        return parsed.scheme == "https" and parsed.hostname == OPENROUTER_HOST


def _score_metadata(entry: dict[str, Any], now: float) -> ScoredOpenRouterModel:
    model_id = str(entry.get("id", ""))
    supported_parameters = [
        str(value)
        for value in entry.get("supported_parameters", [])
        if isinstance(value, str)
    ]
    supported = set(supported_parameters)
    context_length = _int_value(entry.get("context_length")) or _int_value((entry.get("top_provider") or {}).get("context_length")) or 0
    max_completion_tokens = _int_value((entry.get("top_provider") or {}).get("max_completion_tokens"))
    created = _int_value(entry.get("created"))
    pricing = entry.get("pricing") if isinstance(entry.get("pricing"), dict) else {}

    score = 0
    score += 120 if "tools" in supported else 0
    score += 45 if "tool_choice" in supported else 0
    score += 120 if "structured_outputs" in supported else 0
    score += 80 if "response_format" in supported else 0
    score += 75 if "reasoning" in supported else 0
    score += 35 if "include_reasoning" in supported else 0
    score += 25 if "seed" in supported else 0
    score += 25 if "stop" in supported else 0
    score += _context_score(context_length)
    score += _max_completion_score(max_completion_tokens)
    score += _recency_score(created, now)
    score += 50 if _pricing_is_free(pricing) else 0
    score += 20 if model_id.endswith(":free") or "(free)" in str(entry.get("name", "")).lower() else 0

    model = ScoredOpenRouterModel(
        id=model_id,
        name=str(entry.get("name") or model_id),
        metadata_score=score,
        context_length=context_length,
        max_completion_tokens=max_completion_tokens,
        created=created,
        pricing=dict(pricing),
        supported_parameters=supported_parameters,
        supports_tools="tools" in supported,
        supports_tool_choice="tool_choice" in supported,
        supports_structured_outputs="structured_outputs" in supported,
        supports_response_format="response_format" in supported,
        supports_reasoning="reasoning" in supported,
        supports_include_reasoning="include_reasoning" in supported,
        supports_seed="seed" in supported,
        supports_stop="stop" in supported,
    )
    model.reason = _build_reason(model)
    model.recalculate_score()
    return model


def _is_eligible_free_text_model(entry: dict[str, Any], now: float) -> bool:
    model_id = entry.get("id")
    if not isinstance(model_id, str) or not model_id:
        return False
    if not _pricing_is_free(entry.get("pricing")):
        return False
    if not _outputs_text(entry):
        return False
    context_length = _int_value(entry.get("context_length")) or _int_value((entry.get("top_provider") or {}).get("context_length")) or 0
    if context_length < MIN_CONTEXT_LENGTH:
        return False
    expiration_date = _int_value(entry.get("expiration_date"))
    if expiration_date is not None and expiration_date <= now:
        return False
    return True


def _pricing_is_free(pricing: object) -> bool:
    if not isinstance(pricing, dict):
        return False
    for key in ("prompt", "completion"):
        value = _float_value(pricing.get(key))
        if value is None or value != 0:
            return False
    for key in ("request", "image", "web_search", "internal_reasoning"):
        value = _float_value(pricing.get(key))
        if value is not None and value > 0:
            return False
    return True


def _outputs_text(entry: dict[str, Any]) -> bool:
    architecture = entry.get("architecture") if isinstance(entry.get("architecture"), dict) else {}
    output_modalities = architecture.get("output_modalities")
    if isinstance(output_modalities, list):
        return "text" in output_modalities
    modality = architecture.get("modality")
    return isinstance(modality, str) and modality.endswith("->text")


def _catalog_fingerprint(entries: list[dict[str, Any]]) -> str:
    model_ids = sorted(str(entry.get("id")) for entry in entries if isinstance(entry.get("id"), str))
    payload = json.dumps(model_ids, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _context_score(context_length: int) -> int:
    if context_length < MIN_CONTEXT_LENGTH:
        return 0
    return min(260, int(math.log2(context_length / MIN_CONTEXT_LENGTH + 1) * 60))


def _max_completion_score(max_completion_tokens: int | None) -> int:
    if not max_completion_tokens or max_completion_tokens <= 0:
        return 0
    return min(160, int(math.log2(max_completion_tokens / 2048 + 1) * 40))


def _recency_score(created: int | None, now: float) -> int:
    if not created:
        return 0
    age_days = max(0.0, (now - created) / 86400)
    if age_days <= 30:
        return 60
    if age_days <= 180:
        return 40
    if age_days <= 365:
        return 25
    if age_days <= 730:
        return 10
    return 0


def _latency_score(latency_ms: int | None) -> int:
    if latency_ms is None:
        return 0
    if latency_ms <= 1000:
        return 75
    if latency_ms <= 3000:
        return 50
    if latency_ms <= 8000:
        return 25
    return 10


def _health_status_allows_lite_eval(health_status: str) -> bool:
    return health_status in {"passed", "imperfect"}


def _lite_eval_skip_reason(health_status: str) -> str:
    return "health_probe_rate_limited" if health_status == "http_429" else "health_probe_failed"


def _needs_lite_eval(model: ScoredOpenRouterModel) -> bool:
    """True если health сейчас ок, но прошлый lite eval не был completed."""
    if not _health_status_allows_lite_eval(model.health_status):
        return False
    return model.eval_summary.get("status") != "completed"


def _build_reason(model: ScoredOpenRouterModel) -> str:
    parts: list[str] = []
    if model.supports_tools:
        parts.append("tools")
    if model.supports_structured_outputs:
        parts.append("structured outputs")
    elif model.supports_response_format:
        parts.append("response format")
    if model.supports_reasoning or model.supports_include_reasoning:
        parts.append("reasoning")
    if model.context_length >= 128000:
        parts.append(f"{round(model.context_length / 1024)}k context")
    if model.health_status not in ("not_probed", ""):
        parts.append(f"health {model.health_status}")
    if model.lite_eval_score:
        parts.append(f"lite eval {model.lite_eval_score}/{MAX_LITE_EVAL_POINTS}")
    return ", ".join(parts) if parts else "free text model"


def _eval_payload(model: ScoredOpenRouterModel, prompt: str, *, max_tokens: int) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "model": model.id,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0,
        "max_tokens": max_tokens,
    }
    if model.supports_response_format and "JSON" in prompt:
        payload["response_format"] = {"type": "json_object"}
    return payload


def _extract_chat_content(response_json: dict[str, Any]) -> str:
    choices = response_json.get("choices")
    if not isinstance(choices, list) or not choices:
        return ""
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ""
    message = first_choice.get("message")
    if isinstance(message, dict):
        content = message.get("content")
        if isinstance(content, str):
            return content
    text = first_choice.get("text")
    return text if isinstance(text, str) else ""


def _extract_json_object(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if stripped.startswith("```"):
        stripped = re.sub(r"^```(?:json)?\s*", "", stripped, flags=re.IGNORECASE)
        stripped = re.sub(r"\s*```$", "", stripped)
    try:
        parsed = json.loads(stripped)
    except ValueError:
        match = re.search(r"\{.*\}", stripped, flags=re.DOTALL)
        if not match:
            return {}
        try:
            parsed = json.loads(match.group(0))
        except ValueError:
            return {}
    return parsed if isinstance(parsed, dict) else {}


def _extract_python_code(text: str) -> str:
    parsed = _extract_json_object(text)
    code = parsed.get("code") if isinstance(parsed.get("code"), str) else None
    if code:
        return code

    match = re.search(r"```(?:python)?\s*(.*?)\s*```", text, flags=re.IGNORECASE | re.DOTALL)
    if match:
        return match.group(1).strip()
    return text.strip()


def _python_code_safety_error(code: str) -> str | None:
    if not code.strip():
        return "empty_code"
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return "syntax_error"

    unsafe_nodes = (
        ast.AsyncFunctionDef,
        ast.Attribute,
        ast.ClassDef,
        ast.Delete,
        ast.Global,
        ast.Import,
        ast.ImportFrom,
        ast.Lambda,
        ast.Nonlocal,
        ast.Raise,
        ast.Try,
        ast.With,
    )
    unsafe_calls = {
        "__import__",
        "breakpoint",
        "compile",
        "delattr",
        "dir",
        "eval",
        "exec",
        "getattr",
        "globals",
        "input",
        "locals",
        "open",
        "setattr",
        "super",
        "type",
        "vars",
    }
    for node in ast.walk(tree):
        if isinstance(node, unsafe_nodes):
            return f"unsafe_node:{node.__class__.__name__}"
        if isinstance(node, ast.Name) and node.id.startswith("__"):
            return f"unsafe_name:{node.id}"
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id in unsafe_calls:
            return f"unsafe_call:{node.func.id}"
    return None


async def _run_sum_even_squares_tests(code: str) -> tuple[bool, str]:
    harness = """
import sys
code = sys.stdin.read()
safe_builtins = {
    "abs": abs,
    "bool": bool,
    "enumerate": enumerate,
    "int": int,
    "len": len,
    "list": list,
    "max": max,
    "min": min,
    "range": range,
    "sum": sum,
}
namespace = {}
exec(compile(code, "<model_code>", "exec"), {"__builtins__": safe_builtins}, namespace)
fn = namespace.get("sum_even_squares")
if not callable(fn):
    raise AssertionError("missing sum_even_squares")
tests = [
    ([1, 2, 3, 4], 20),
    ([-2, -1, 0, 5], 4),
    ([], 0),
    ([6], 36),
]
for values, expected in tests:
    actual = fn(values)
    if actual != expected:
        raise AssertionError(f"{values}: {actual} != {expected}")
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-I",
        "-S",
        "-c",
        harness,
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _stdout, stderr = await asyncio.wait_for(
            process.communicate(code.encode("utf-8")),
            timeout=CODE_EVAL_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        process.kill()
        await process.wait()
        return False, "timeout"
    return process.returncode == 0, stderr.decode("utf-8", errors="replace")


def _symbolic_math_values(seed: int) -> dict[str, int]:
    digest = hashlib.sha256(str(seed).encode("utf-8")).digest()
    weeks = 2 + digest[0] % 3
    weekday_pages = 2 + digest[1] % 5
    weekend_pages = 3 + digest[2] % 6
    written_pages = weeks * 5 * weekday_pages + weeks * 2 * weekend_pages
    remaining_pages = 10 + digest[3] % 40
    return {
        "weeks": weeks,
        "weekday_pages": weekday_pages,
        "weekend_pages": weekend_pages,
        "total_pages": written_pages + remaining_pages,
        "remaining_pages": remaining_pages,
    }


def _first_int(text: str) -> int | None:
    match = re.search(r"-?\d+", text)
    return int(match.group(0)) if match else None


def _normalize_simple_answer(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _not_evaluated_summary(reason: str) -> dict[str, Any]:
    return {
        "suite": "lite-agent-eval-v1",
        "status": "not_evaluated",
        "reason": reason,
        "points": 0,
        "maxPoints": 0,
        "passed": 0,
        "total": 0,
        "updatedAt": None,
        "tasks": [],
    }


def _rank_sort_key(model: ScoredOpenRouterModel) -> tuple[int, int, int, str]:
    return (-model.score, -model.metadata_score, -model.context_length, model.id)


def _float_value(value: object) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int_value(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _utc_now_iso(time_func=time.time) -> str:
    return _timestamp_to_iso(time_func())


def _timestamp_to_iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat().replace("+00:00", "Z")
