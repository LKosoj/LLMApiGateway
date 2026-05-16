from __future__ import annotations

import asyncio
import copy
import logging
import re
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlparse

import httpx

from ..config.loader import (
    ANTHROPIC_API_VERSION,
    ProviderDetails,
    SECURITY_HEADERS,
    resolve_provider_api_key,
    resolve_provider_api_key_value,
)
from ..utils.api_keys import has_api_key
from .openrouter_free_models import (
    EVAL_SCORE_WEIGHT,
    HEALTH_PROBE_TIMEOUT_SECONDS,
    HEALTH_PROBE_MAX_TOKENS,
    LITE_EVAL_TIMEOUT_SECONDS,
    NON_EVAL_SCORE_WEIGHT,
    OPENROUTER_HOST,
    OPENROUTER_PROVIDER_NAME,
    REFRESH_INTERVAL_SECONDS,
    LiteEvalTaskResult,
    _eval_payload,
    _extract_chat_content,
    _extract_json_object,
    _extract_python_code,
    _first_int,
    _health_status_allows_lite_eval,
    _latency_score,
    _lite_eval_skip_reason,
    _normalize_simple_answer,
    _not_evaluated_summary,
    _python_code_safety_error,
    _rank_sort_key,
    _run_sum_even_squares_tests,
    _score_metadata,
    _symbolic_math_values,
    _utc_now_iso,
)

logger = logging.getLogger(__name__)

ANTHROPIC_DEFAULT_MAX_TOKENS = 32768


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
    metadata_source: str | None = None
    metadata_matched_model: str | None = None
    latency_ms: int | None = None
    health_status: str = "not_probed"
    reason: str = ""
    eval_summary: dict[str, Any] = field(default_factory=dict)

    @property
    def base_score(self) -> int:
        return self.metadata_score + self.health_score + self.latency_score - self.instability_penalty

    def recalculate_score(self) -> None:
        self.score = round(
            self.base_score * NON_EVAL_SCORE_WEIGHT + self.lite_eval_score * EVAL_SCORE_WEIGHT
        )

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
            "maxCompletionTokens": self.max_completion_tokens,
            "supportsTools": self.supports_tools,
            "supportsToolChoice": self.supports_tool_choice,
            "supportsStructuredOutputs": self.supports_structured_outputs,
            "supportsResponseFormat": self.supports_response_format,
            "supportsReasoning": self.supports_reasoning,
            "supportsIncludeReasoning": self.supports_include_reasoning,
            "supportsSeed": self.supports_seed,
            "supportsStop": self.supports_stop,
            "metadataSource": self.metadata_source,
            "metadataMatchedModel": self.metadata_matched_model,
            "latencyMs": self.latency_ms,
            "healthStatus": self.health_status,
            "evalSummary": self.eval_summary,
            "reason": self.reason,
            "pricing": self.pricing,
            "created": self.created,
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
            )
            for target in targets
        ]
        openrouter_metadata = await self._load_openrouter_metadata(
            providers_config=providers_config,
            http_client=http_client,
            proxy_http_clients=proxy_http_clients,
        ) if targets else {}
        for target, model in zip(targets, models, strict=False):
            metadata_model = openrouter_metadata.get(_openrouter_metadata_key(target.model))
            if metadata_model is not None:
                _apply_openrouter_metadata(model, metadata_model)
        _apply_missing_openrouter_metadata_median(models)

        evaluated_count = 0
        for target, model in zip(targets, models, strict=False):
            provider_config = providers_config.get(target.provider)
            if provider_config is None:
                model.health_status = "missing_provider"
                model.reason = f"Provider '{target.provider}' is not configured."
                model.eval_summary = _not_evaluated_summary("missing_provider")
                model.recalculate_score()
                continue
            provider_http_client = proxy_http_clients.get(target.provider, http_client)
            await self._apply_health_probe(model, target, provider_config, provider_http_client)
            if _health_status_allows_lite_eval(model.health_status):
                model.eval_summary = await self._run_lite_eval_suite(model, target, provider_config, provider_http_client)
                model.lite_eval_score = int(model.eval_summary.get("points", 0))
                evaluated_count += 1
            else:
                model.eval_summary = _not_evaluated_summary(_lite_eval_skip_reason(model.health_status))
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
                "OpenRouter metadata is matched by model basename when the official openrouter provider and API key are configured; unmatched targets use the median known metadata score.",
                "Final score = round((metadataScore + healthScore + latencyScore - instabilityPenalty) * 0.8 + liteEvalScore * 1.6) — eval tests weigh twice the other metrics.",
                "OpenAI-compatible providers are evaluated through /chat/completions; Anthropic providers are evaluated through /v1/messages.",
            ],
        )

    async def _load_openrouter_metadata(
        self,
        *,
        providers_config: dict[str, ProviderDetails],
        http_client: httpx.AsyncClient,
        proxy_http_clients: dict[str, httpx.AsyncClient],
    ) -> dict[str, Any]:
        provider_config = providers_config.get(OPENROUTER_PROVIDER_NAME)
        if provider_config is None or not _is_official_openrouter_url(provider_config.baseUrl):
            return {}
        try:
            raw_api_key = resolve_provider_api_key_value(provider_config.apikey)
        except Exception:
            return {}
        if not has_api_key(raw_api_key):
            return {}

        api_key = resolve_provider_api_key(provider_config.apikey)
        if not api_key:
            return {}

        openrouter_client = proxy_http_clients.get(OPENROUTER_PROVIDER_NAME, http_client)
        response = await openrouter_client.get(
            f"{provider_config.baseUrl.rstrip('/')}/models",
            headers={
                "Authorization": f"Bearer {api_key}",
                "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
                "X-Title": "LLMGateway Fallback Model Eval",
            },
            timeout=HEALTH_PROBE_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
        entries = payload.get("data")
        if not isinstance(entries, list):
            raise ValueError("OpenRouter /models response does not contain a data list.")

        metadata_by_name: dict[str, Any] = {}
        for entry in entries:
            if not isinstance(entry, dict):
                continue
            scored_model = _score_metadata(entry, self._time_func())
            key = _openrouter_metadata_key(scored_model.id)
            if not key:
                continue
            existing = metadata_by_name.get(key)
            if existing is None or _openrouter_metadata_rank(scored_model) > _openrouter_metadata_rank(existing):
                metadata_by_name[key] = scored_model
        return metadata_by_name

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
                "max_tokens": HEALTH_PROBE_MAX_TOKENS,
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
        if getattr(provider_config, "type", "openai") == "anthropic":
            return await self._anthropic_completion(target, provider_config, http_client, payload, timeout=timeout)

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

    async def _anthropic_completion(
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
            "anthropic-version": ANTHROPIC_API_VERSION,
            **({"x-api-key": api_key} if api_key else {}),
        }
        for key, value in target.route.get("custom_headers", {}).items():
            if key.lower() not in SECURITY_HEADERS:
                headers[key] = value

        provider_payload = _openai_eval_payload_to_anthropic(payload)
        provider_payload["model"] = target.model
        for key, value in target.route.get("custom_body_params", {}).items():
            provider_payload[key] = value

        response = await http_client.post(
            f"{provider_config.baseUrl.rstrip('/')}/v1/messages",
            headers=headers,
            json=provider_payload,
            timeout=timeout,
        )
        response.raise_for_status()
        return _anthropic_response_to_chat_completion(response.json(), target.model)


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


def _openai_eval_payload_to_anthropic(payload: dict[str, Any]) -> dict[str, Any]:
    system_parts: list[str] = []
    messages: list[dict[str, str]] = []
    for message in payload.get("messages", []):
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = _plain_text_content(message.get("content"))
        if role == "system":
            if content:
                system_parts.append(content)
            continue
        messages.append({
            "role": "assistant" if role == "assistant" else "user",
            "content": content,
        })

    if not messages:
        messages.append({"role": "user", "content": ""})

    anthropic_payload: dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages,
        "max_tokens": _positive_int(payload.get("max_tokens"), ANTHROPIC_DEFAULT_MAX_TOKENS),
    }
    if system_parts:
        anthropic_payload["system"] = "\n\n".join(system_parts)
    if "temperature" in payload:
        anthropic_payload["temperature"] = payload["temperature"]
    if "top_p" in payload:
        anthropic_payload["top_p"] = payload["top_p"]
    stop_value = payload.get("stop")
    if isinstance(stop_value, str):
        anthropic_payload["stop_sequences"] = [stop_value]
    elif isinstance(stop_value, list):
        anthropic_payload["stop_sequences"] = [item for item in stop_value if isinstance(item, str)]
    return anthropic_payload


def _plain_text_content(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for item in content:
            if isinstance(item, dict):
                text = item.get("text")
                if isinstance(text, str):
                    parts.append(text)
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(parts)
    return "" if content is None else str(content)


def _positive_int(value: Any, default: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return parsed if parsed > 0 else default


def _anthropic_response_to_chat_completion(payload: dict[str, Any], model: str) -> dict[str, Any]:
    content = payload.get("content")
    text_parts: list[str] = []
    if isinstance(content, str):
        text_parts.append(content)
    elif isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
    return {
        "model": payload.get("model") or model,
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": "".join(text_parts),
                }
            }
        ],
    }


def _apply_openrouter_metadata(model: ScoredFallbackModel, openrouter_model: Any) -> None:
    model.metadata_score = openrouter_model.metadata_score
    model.context_length = openrouter_model.context_length
    model.max_completion_tokens = openrouter_model.max_completion_tokens
    model.created = openrouter_model.created
    model.pricing = dict(openrouter_model.pricing)
    model.supported_parameters = list(openrouter_model.supported_parameters)
    model.supports_tools = openrouter_model.supports_tools
    model.supports_tool_choice = openrouter_model.supports_tool_choice
    model.supports_structured_outputs = openrouter_model.supports_structured_outputs
    model.supports_response_format = openrouter_model.supports_response_format
    model.supports_reasoning = openrouter_model.supports_reasoning
    model.supports_include_reasoning = openrouter_model.supports_include_reasoning
    model.supports_seed = openrouter_model.supports_seed
    model.supports_stop = openrouter_model.supports_stop
    model.metadata_source = OPENROUTER_PROVIDER_NAME
    model.metadata_matched_model = openrouter_model.id
    metadata_reason = openrouter_model.reason
    model.reason = (
        f"OpenRouter metadata: {openrouter_model.id}"
        f"{f' ({metadata_reason})' if metadata_reason else ''}."
    )
    model.recalculate_score()


def _apply_missing_openrouter_metadata_median(models: list[ScoredFallbackModel]) -> None:
    known_scores = [
        model.metadata_score
        for model in models
        if model.metadata_source == OPENROUTER_PROVIDER_NAME
    ]
    median_score = _median_score(known_scores)
    if median_score is None or median_score <= 0:
        return
    for model in models:
        if model.metadata_source is not None:
            continue
        model.metadata_score = median_score
        model.metadata_source = "openrouter_median"
        model.reason = "OpenRouter metadata not matched; using median metadata score from matched fallback models."
        model.recalculate_score()


def _median_score(scores: list[int]) -> int | None:
    if not scores:
        return None
    ordered = sorted(scores)
    midpoint = len(ordered) // 2
    if len(ordered) % 2 == 1:
        return ordered[midpoint]
    return (ordered[midpoint - 1] + ordered[midpoint]) // 2


def _is_official_openrouter_url(base_url: str) -> bool:
    parsed = urlparse(base_url)
    return parsed.scheme == "https" and parsed.hostname == OPENROUTER_HOST


def _openrouter_metadata_key(model_id: str) -> str:
    basename = str(model_id or "").strip().rsplit("/", 1)[-1].strip()
    return basename.split(":", 1)[0].lower()


def _openrouter_metadata_rank(openrouter_model: Any) -> tuple[int, int, str]:
    return (
        int(getattr(openrouter_model, "metadata_score", 0) or 0),
        int(getattr(openrouter_model, "context_length", 0) or 0),
        str(getattr(openrouter_model, "id", "")),
    )
