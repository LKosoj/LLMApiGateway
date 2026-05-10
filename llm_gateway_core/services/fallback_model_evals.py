from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config.loader import ProviderDetails, SECURITY_HEADERS, resolve_provider_api_key
from .openrouter_free_models import (
    HEALTH_PROBE_TIMEOUT_SECONDS,
    LITE_EVAL_TIMEOUT_SECONDS,
    REFRESH_INTERVAL_SECONDS,
    LiteEvalTaskResult,
    _eval_payload,
    _extract_chat_content,
    _extract_json_object,
    _extract_python_code,
    _first_int,
    _latency_score,
    _normalize_simple_answer,
    _not_evaluated_summary,
    _python_code_safety_error,
    _rank_sort_key,
    _run_sum_even_squares_tests,
    _symbolic_math_values,
    _utc_now_iso,
)

logger = logging.getLogger(__name__)


@dataclass
class FallbackEvalTarget:
    provider: str
    model: str
    gateway_models: list[str] = field(default_factory=list)
    route: dict[str, Any] = field(default_factory=dict)


@dataclass
class ScoredFallbackModel:
    id: str
    name: str
    provider: str
    model: str
    gateway_models: list[str]
    metadata_score: int = 0
    health_score: int = 0
    latency_score: int = 0
    lite_eval_score: int = 0
    instability_penalty: int = 0
    score: int = 0
    context_length: int = 0
    supports_response_format: bool = False
    latency_ms: int | None = None
    health_status: str = "not_probed"
    reason: str = ""
    eval_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def base_score(self) -> int:
        return self.metadata_score + self.health_score + self.latency_score - self.instability_penalty

    def recalculate_score(self) -> None:
        self.score = self.base_score + self.lite_eval_score

    def to_dict(self, rank: int) -> dict[str, Any]:
        return {
            "rank": rank,
            "id": self.id,
            "name": self.name,
            "provider": self.provider,
            "model": self.model,
            "gatewayModels": list(self.gateway_models),
            "score": self.score,
            "metadataScore": self.metadata_score,
            "healthScore": self.health_score,
            "latencyScore": self.latency_score,
            "liteEvalScore": self.lite_eval_score,
            "instabilityPenalty": self.instability_penalty,
            "contextLength": self.context_length,
            "latencyMs": self.latency_ms,
            "healthStatus": self.health_status,
            "evalSummary": self.eval_summary,
            "reason": self.reason,
        }


@dataclass
class FallbackModelEvalSnapshot:
    updated_at: str
    source: str
    refresh_mode: str
    ranking_version: str
    configured_count: int
    evaluated_count: int
    models: list[ScoredFallbackModel]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "updatedAt": self.updated_at,
            "source": self.source,
            "refreshMode": self.refresh_mode,
            "rankingVersion": self.ranking_version,
            "configuredCount": self.configured_count,
            "evaluatedCount": self.evaluated_count,
            "models": [model.to_dict(index + 1) for index, model in enumerate(self.models)],
            "notes": list(self.notes),
        }


class FallbackModelEvalAlreadyRunning(RuntimeError):
    pass


class FallbackModelEvalService:
    def __init__(self, *, time_func=time.time) -> None:
        self._time_func = time_func
        self._lock = asyncio.Lock()
        self._running = False
        self._last_error: str | None = None
        self._last_checked_at: str | None = None
        self._snapshot: FallbackModelEvalSnapshot | None = None
        self._task: asyncio.Task | None = None

    async def get_status(self) -> dict[str, Any]:
        async with self._lock:
            snapshot = self._snapshot.to_dict() if self._snapshot else None
            return {
                "configured": True,
                "running": self._running,
                "lastCheckedAt": self._last_checked_at,
                "lastError": self._last_error,
                "snapshot": snapshot,
            }

    async def start_eval(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        fallback_rules: dict[str, dict[str, Any]],
        http_client: httpx.AsyncClient,
        proxy_http_clients: dict[str, httpx.AsyncClient] | None = None,
    ) -> None:
        async with self._lock:
            if self._running:
                raise FallbackModelEvalAlreadyRunning("Fallback model eval is already running.")
            self._running = True
            self._last_error = None
            self._task = asyncio.create_task(
                self._run_eval_task(
                    providers_config=copy.deepcopy(providers_config),
                    fallback_rules=copy.deepcopy(fallback_rules),
                    http_client=http_client,
                    proxy_http_clients=proxy_http_clients or {},
                ),
                name="fallback-model-eval",
            )

    async def stop(self) -> None:
        task = self._task
        self._task = None
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        async with self._lock:
            self._running = False

    async def run_once(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        fallback_rules: dict[str, dict[str, Any]],
        http_client: httpx.AsyncClient,
        proxy_http_clients: dict[str, httpx.AsyncClient] | None = None,
    ) -> None:
        async with self._lock:
            self._running = True
            self._last_error = None
        try:
            snapshot = await self._build_snapshot(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=http_client,
                proxy_http_clients=proxy_http_clients or {},
            )
            async with self._lock:
                self._snapshot = snapshot
                self._last_checked_at = snapshot.updated_at
                self._last_error = None
        except Exception as exc:
            logger.exception("Fallback model eval failed.")
            async with self._lock:
                self._last_checked_at = _utc_now_iso(self._time_func)
                self._last_error = str(exc)
        finally:
            async with self._lock:
                self._running = False

    async def _run_eval_task(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        fallback_rules: dict[str, dict[str, Any]],
        http_client: httpx.AsyncClient,
        proxy_http_clients: dict[str, httpx.AsyncClient],
    ) -> None:
        try:
            await self.run_once(
                providers_config=providers_config,
                fallback_rules=fallback_rules,
                http_client=http_client,
                proxy_http_clients=proxy_http_clients,
            )
        finally:
            async with self._lock:
                self._task = None

    async def _build_snapshot(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        fallback_rules: dict[str, dict[str, Any]],
        http_client: httpx.AsyncClient,
        proxy_http_clients: dict[str, httpx.AsyncClient],
    ) -> FallbackModelEvalSnapshot:
        targets = _collect_unique_fallback_targets(fallback_rules)
        models = [
            ScoredFallbackModel(
                id=f"{target.provider}:{target.model}",
                name=target.model,
                provider=target.provider,
                model=target.model,
                gateway_models=target.gateway_models,
                reason="Configured fallback model; metadata score is 0 because this eval does not normalize provider catalogs.",
            )
            for target in targets
        ]

        evaluated_count = 0
        for target, model in zip(targets, models, strict=False):
            provider_config = providers_config.get(target.provider)
            if provider_config is None:
                model.health_status = "missing_provider"
                model.reason = f"Provider '{target.provider}' is not configured."
                model.eval_summary = _not_evaluated_summary("missing_provider")
                model.recalculate_score()
                continue
            if getattr(provider_config, "type", "openai") == "anthropic":
                model.health_status = "unsupported_provider_type"
                model.reason = "Native Anthropic fallback routes are not evaluated by this OpenAI-compatible lite eval runner."
                model.eval_summary = _not_evaluated_summary("unsupported_provider_type")
                model.recalculate_score()
                continue

            provider_http_client = proxy_http_clients.get(target.provider, http_client)
            await self._apply_health_probe(model, target, provider_config, provider_http_client)
            if model.health_score > 0:
                model.eval_summary = await self._run_lite_eval_suite(model, target, provider_config, provider_http_client)
                model.lite_eval_score = int(model.eval_summary.get("points", 0))
                evaluated_count += 1
            else:
                model.eval_summary = _not_evaluated_summary("health_probe_failed")
            model.recalculate_score()

        models.sort(key=_rank_sort_key)
        return FallbackModelEvalSnapshot(
            updated_at=_utc_now_iso(self._time_func),
            source="models-fallback-rules",
            refresh_mode="manualEval",
            ranking_version="fallback-lite-eval-v1",
            configured_count=len(models),
            evaluated_count=evaluated_count,
            models=models,
            notes=[
                "Unique fallback targets are grouped by provider and model.",
                "Final score = healthScore + latencyScore + liteEvalScore - instabilityPenalty; metadataScore is 0.",
                "Native Anthropic provider routes are marked unsupported by this direct provider eval.",
            ],
        )

    async def _apply_health_probe(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> None:
        model.health_score = 0
        model.latency_score = 0
        model.latency_ms = None
        model.health_status = "not_probed"
        model.instability_penalty = 0
        try:
            started_at = self._time_func()
            payload = {
                "model": model.model,
                "messages": [{"role": "user", "content": "Reply with exactly OK."}],
                "max_tokens": 4,
                "temperature": 0,
            }
            response = await self._chat_completion(target, provider_config, http_client, payload, timeout=HEALTH_PROBE_TIMEOUT_SECONDS)
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
            if status_code == 429:
                model.instability_penalty += 25
        except Exception as exc:
            model.health_status = exc.__class__.__name__
            model.instability_penalty += 50
        finally:
            model.recalculate_score()

    async def _run_lite_eval_suite(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> dict[str, Any]:
        tasks: list[LiteEvalTaskResult] = []
        for task_id, max_points, runner in (
            ("instruction_following_lite", 200, self._run_instruction_following_lite_task),
            ("tool_call_lite", 200, self._run_tool_call_lite_task),
            ("code_unit_lite", 200, self._run_code_unit_lite_task),
            ("symbolic_math_lite", 100, self._run_symbolic_math_lite_task),
            ("simpleqa_lite", 50, self._run_simpleqa_lite_task),
        ):
            try:
                tasks.append(await runner(model, target, provider_config, http_client))
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

    async def _run_instruction_following_lite_task(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> LiteEvalTaskResult:
        prompt = (
            "Return exactly 4 lines.\n"
            "Line 1 must be exactly: STATUS: READY\n"
            "Line 2 must contain the word ROUTER exactly twice.\n"
            "Line 3 must be valid JSON with exactly keys \"mode\" and \"count\"; "
            "\"mode\" must be \"eval\" and \"count\" must be 3.\n"
            "Line 4 must be exactly: DONE\n"
            "No markdown and no extra text."
        )
        response = await self._chat_completion(target, provider_config, http_client, _eval_payload(model, prompt, max_tokens=120), timeout=LITE_EVAL_TIMEOUT_SECONDS)
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
        points = round(200 * sum(1 for ok in details.values() if ok) / len(details))
        return LiteEvalTaskResult("instruction_following_lite", points, 200, "passed" if points == 200 else "failed", details)

    async def _run_tool_call_lite_task(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> LiteEvalTaskResult:
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
        response = await self._chat_completion(target, provider_config, http_client, _eval_payload(model, prompt, max_tokens=220), timeout=LITE_EVAL_TIMEOUT_SECONDS)
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
        points = round(200 * sum(1 for ok in details.values() if ok) / len(details))
        return LiteEvalTaskResult("tool_call_lite", points, 200, "passed" if points == 200 else "failed", details)

    async def _run_code_unit_lite_task(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> LiteEvalTaskResult:
        prompt = (
            "Return only JSON with one key \"code\". The value must be Python code defining:\n"
            "def sum_even_squares(nums: list[int]) -> int:\n"
            "It must return the sum of squares of even integers in nums. No imports."
        )
        response = await self._chat_completion(target, provider_config, http_client, _eval_payload(model, prompt, max_tokens=320), timeout=LITE_EVAL_TIMEOUT_SECONDS)
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
        checks = (details["codePresent"], details["safeAst"], details["unitTestsPassed"])
        points = round(200 * sum(1 for ok in checks if ok) / len(checks))
        return LiteEvalTaskResult("code_unit_lite", points, 200, "passed" if points == 200 else "failed", details)

    async def _run_symbolic_math_lite_task(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> LiteEvalTaskResult:
        values = _symbolic_math_values(int(self._time_func()) // REFRESH_INTERVAL_SECONDS)
        prompt = (
            f"A notebook has {values['total_pages']} pages. Mira writes {values['weekday_pages']} pages every weekday "
            f"for {values['weeks']} weeks and {values['weekend_pages']} pages on each weekend day. "
            "How many pages remain? Return only the integer."
        )
        response = await self._chat_completion(target, provider_config, http_client, _eval_payload(model, prompt, max_tokens=80), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        answer = _extract_chat_content(response)
        parsed_answer = _first_int(answer)
        expected = values["remaining_pages"]
        details = {"expected": expected, "received": answer[:80], "parsedAnswer": parsed_answer}
        return LiteEvalTaskResult(
            "symbolic_math_lite",
            100 if parsed_answer == expected else 0,
            100,
            "passed" if parsed_answer == expected else "failed",
            details,
        )

    async def _run_simpleqa_lite_task(
        self,
        model: ScoredFallbackModel,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> LiteEvalTaskResult:
        prompt = (
            "Who wrote the novel \"The Left Hand of Darkness\"? "
            "If unsure, answer UNKNOWN. Return only the answer."
        )
        response = await self._chat_completion(target, provider_config, http_client, _eval_payload(model, prompt, max_tokens=40), timeout=LITE_EVAL_TIMEOUT_SECONDS)
        answer = _normalize_simple_answer(_extract_chat_content(response))
        is_correct = answer in {"ursulakleguin", "ursulaleguin"}
        is_unknown = answer == "unknown"
        details = {
            "expected": "Ursula K. Le Guin",
            "normalizedAnswer": answer,
            "correct": is_correct,
            "unknown": is_unknown,
        }
        return LiteEvalTaskResult(
            "simpleqa_lite",
            50 if is_correct else 20 if is_unknown else 0,
            50,
            "passed" if is_correct else "not_attempted" if is_unknown else "failed",
            details,
        )

    async def _chat_completion(
        self,
        target: FallbackEvalTarget,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
        payload: dict[str, Any],
        *,
        timeout: float,
    ) -> dict[str, Any]:
        api_key = resolve_provider_api_key(provider_config.apikey)
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
            "X-Title": "LLMGateway Fallback Model Eval",
            **({"Authorization": f"Bearer {api_key}"} if api_key else {}),
        }
        for key, value in target.route.get("custom_headers", {}).items():
            if key.lower() not in SECURITY_HEADERS:
                headers[key] = value

        provider_payload = copy.deepcopy(payload)
        provider_payload["model"] = target.model
        if target.provider == "openrouter" and "usage" not in provider_payload:
            provider_payload["usage"] = {"include": True}
        providers_order = target.route.get("providers_order")
        if isinstance(providers_order, list) and providers_order:
            provider_payload["provider"] = {"order": providers_order}
            provider_payload["allow_fallbacks"] = False
        for key, value in target.route.get("custom_body_params", {}).items():
            provider_payload[key] = value

        response = await http_client.post(
            f"{provider_config.baseUrl.rstrip('/')}/chat/completions",
            headers=headers,
            json=provider_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return response.json()


def _collect_unique_fallback_targets(fallback_rules: dict[str, dict[str, Any]]) -> list[FallbackEvalTarget]:
    targets: dict[tuple[str, str], FallbackEvalTarget] = {}
    for gateway_model_name, config in fallback_rules.items():
        for route in _iter_fallback_routes(config):
            provider = str(route.get("provider") or "").strip()
            model = str(route.get("model") or "").strip()
            if not provider or not model:
                continue
            key = (provider, model)
            if key not in targets:
                targets[key] = FallbackEvalTarget(provider=provider, model=model, route=copy.deepcopy(route))
            if gateway_model_name not in targets[key].gateway_models:
                targets[key].gateway_models.append(gateway_model_name)
    return list(targets.values())


def _iter_fallback_routes(config: dict[str, Any]) -> list[dict[str, Any]]:
    routes = [route for route in config.get("fallback_models", []) if isinstance(route, dict)]
    context_overflow_fallback = config.get("context_overflow_fallback")
    if isinstance(context_overflow_fallback, dict):
        routes.append(context_overflow_fallback)
    return routes
