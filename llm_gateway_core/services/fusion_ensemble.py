"""Fusion ensemble service.

A Fusion gateway model runs a small agentic pipeline instead of a single
upstream call:

1. **Fan-out** — every model on the *panel* answers the user request in
   parallel.
2. **Fan-in (judge)** — a judge model compares the panel answers and returns a
   *structured* analysis (agreements, disputes, per-model insights, blind
   spots).
3. **Main model** — compiles the final answer from the panel answers and
   judge analysis.

Each panel/judge/main member is addressed as a ``{provider, model}`` pair, the
same shape as a fallback target, so the gateway itself can be registered as a
provider (self loopback) to reach gateway models, or any upstream provider can
be called directly.

When the Fusion config sets ``web_tools``, every *panel* member additionally
runs in an agentic tool loop: it may call a ``web_search`` tool (and, when a
``read_model`` is configured, a ``web_fetch`` tool) before answering, bounded by
per-member ``max_tool_calls`` / ``max_iterations`` budgets. The judge and main
members never receive tools.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Mapping
from typing import Any, Dict, List, Optional, Tuple
from uuid import uuid4

from fastapi import HTTPException, Request

from ..config.loader import (
    ANTHROPIC_API_VERSION,
    resolve_provider_config_api_key,
    resolve_provider_config_auth_headers,
)
from ..services.provider_auth import resolve_provider_auth_material
from ..services.request_handler import make_llm_request
from .accounting import (
    AccountingError,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
)
from .chat_accounting import ChatTerminalObservation, ObservedChatResponse
from .operation_accounting import build_token_model_component
from ..utils.usage_tracking import (
    ModelCostRates,
    RATE_BASED_COST_SKIP_KEY,
    UPSTREAM_COST_PRESENT_KEY,
    calculate_model_cost_usd,
    extract_tokens_usage,
)

logger = logging.getLogger(__name__)

# Header set on every internal sub-request so that a Fusion model reached via a
# self-loopback provider cannot recursively trigger another Fusion pipeline.
FUSION_DEPTH_HEADER = "X-LLMGateway-Fusion"

# Anthropic requires an explicit ``max_tokens``; use this when a member does not
# set ``max_completion_tokens``.
_DEFAULT_ANTHROPIC_MAX_TOKENS = 4096

_JUDGE_SYSTEM_PROMPT = (
    "You are the judge of a panel of AI models that independently answered the "
    "same user request. Compare their answers and return ONLY a JSON object "
    "(no markdown, no prose) with exactly these keys:\n"
    '  "agreements": array of strings — points the models agree on;\n'
    '  "disputes": array of strings — points where they disagree or conflict;\n'
    '  "per_model_insights": array of objects {"model": string, "insight": string} '
    "— the unique contribution of each model;\n"
    '  "blind_spots": array of strings — important aspects no model covered.\n'
    "Write the string values in the same language as the user request."
)

_MAIN_SYSTEM_PROMPT = (
    "You are the lead model of a Fusion ensemble. Several models answered the "
    "user request and a judge analysed their answers. Compile a single final "
    "answer for the user from the panel answers and the judge analysis. Do not "
    "select one panel answer as the winner or merely restate the strongest "
    "answer. Combine all useful, non-conflicting contributions, preserve unique "
    "correct details, resolve disagreements on the merits, and cover the blind "
    "spots. Reply in the user's language and do not mention this internal "
    "process unless asked."
)

# Appended to a panel member's base context when the Fusion model enables web
# tools, so the date stamp becomes an actionable cue to look things up.
_WEB_RECENCY_HINT = (
    "If the answer depends on recent or changing information, "
    "use web_search before answering."
)


def _temporal_context(now: Optional[time.struct_time] = None) -> str:
    """One-line current date/time in the server's local timezone.

    Example: ``Current date and time: 2026-06-14T15:30 MSK (Saturday).``
    ``now`` is injectable so tests stay deterministic.
    """
    moment = now if now is not None else time.localtime()
    stamp = time.strftime("%Y-%m-%dT%H:%M %Z", moment).strip()
    weekday = time.strftime("%A", moment)
    return f"Current date and time: {stamp} ({weekday})."


def _extract_text(openai_response: Dict[str, Any]) -> str:
    """Pull the assistant text out of an OpenAI chat.completion response."""
    choices = openai_response.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content")
    if isinstance(content, str):
        return content
    # Some providers return content as a list of parts.
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        return "".join(parts)
    return ""


def _messages_to_text(messages: List[Dict[str, Any]]) -> str:
    """Flatten a chat conversation into plain text for judge/main prompts."""
    lines: List[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        role = message.get("role", "user")
        content = message.get("content")
        if isinstance(content, list):
            content = "".join(
                part.get("text", "")
                for part in content
                if isinstance(part, dict) and part.get("type") in (None, "text")
            )
        if content is None:
            continue
        lines.append(f"{role}: {content}")
    return "\n".join(lines)


def _add_usage(
    accumulator: Dict[str, Any],
    usage: Mapping[str, Any] | None,
    *,
    member: Mapping[str, Any] | None = None,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates] | None = None,
) -> None:
    if not isinstance(usage, Mapping):
        return
    extracted = extract_tokens_usage({"usage": dict(usage)})
    raw_cost_incomplete = bool(usage.get("_cost_incomplete"))
    prompt = int(extracted.get("prompt_tokens") or 0)
    completion = int(extracted.get("completion_tokens") or 0)
    total = int(extracted.get("total_tokens") or (prompt + completion))
    accumulator["prompt_tokens"] += prompt
    accumulator["completion_tokens"] += completion
    accumulator["total_tokens"] += total

    cost: float | None = None
    if raw_cost_incomplete:
        accumulator["_cost_incomplete"] = True
    elif extracted.get(UPSTREAM_COST_PRESENT_KEY):
        cost = float(extracted.get("cost") or 0.0)
    elif member is not None and cost_rate_registry is not None and (prompt > 0 or completion > 0):
        cost = calculate_model_cost_usd(
            prompt_tokens=prompt,
            completion_tokens=completion,
            rates=cost_rate_registry.get((str(member.get("provider")), str(member.get("model")))),
        )

    if cost is not None:
        accumulator["cost"] = float(accumulator.get("cost") or 0.0) + cost
    elif prompt > 0 or completion > 0:
        accumulator["_cost_incomplete"] = True


def _sync_request_usage_tracker(request: Request, usage_total: Dict[str, Any]) -> None:
    tracker = getattr(request.state, "usage_tracker", None)
    if not isinstance(tracker, dict):
        tracker = {}
        request.state.usage_tracker = tracker

    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"):
        value = usage_total.get(key)
        if isinstance(value, (int, float)) and value > 0:
            tracker[key] = int(value)

    cost = usage_total.get("cost")
    if isinstance(cost, (int, float)) and cost >= 0:
        tracker["cost"] = float(cost)
    if usage_total.get("_cost_incomplete") or isinstance(cost, (int, float)):
        tracker[RATE_BASED_COST_SKIP_KEY] = True


def _public_usage(usage_total: Dict[str, Any]) -> Dict[str, Any]:
    return {key: value for key, value in usage_total.items() if not key.startswith("_")}


def _public_component_usage(usage: AccountingUsage) -> Dict[str, Any]:
    return {
        "prompt_tokens": usage.prompt_tokens,
        "completion_tokens": usage.completion_tokens,
        "total_tokens": usage.total_tokens,
        "cost": usage.cost,
    }


def _parse_judge_analysis(raw_text: str) -> Dict[str, Any]:
    """Best-effort parse of the judge's JSON analysis.

    Returns the parsed object, or ``{"raw": <text>}`` when the model did not
    return valid JSON so the analysis is never silently lost.
    """
    text = (raw_text or "").strip()
    if not text:
        return {"raw": ""}
    # Strip a fenced code block if present.
    if text.startswith("```"):
        text = text.strip("`")
        newline = text.find("\n")
        if newline != -1:
            text = text[newline + 1:]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidate = text[start:end + 1]
        try:
            parsed = json.loads(candidate)
            if isinstance(parsed, dict):
                return parsed
        except (ValueError, TypeError):
            pass
    logger.warning("Fusion judge did not return valid JSON; keeping raw analysis.")
    return {"raw": raw_text}


class FusionEnsembleService:
    def __init__(
        self,
        config_loader: Any,
        *,
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    ) -> None:
        self._config_loader = config_loader
        self._cost_rate_registry = cost_rate_registry

    async def run(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        fusion_config: Dict[str, Any],
        request_body: Dict[str, Any],
        http_client: Any,
        proxy_http_clients: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        if fusion_config.get("web_tools"):
            result = await self._run_pipeline(
                request=request,
                gateway_model_name=gateway_model_name,
                fusion_config=fusion_config,
                request_body=request_body,
                http_client=http_client,
                proxy_http_clients=proxy_http_clients,
                observe=False,
            )
            if not isinstance(result, dict):
                raise RuntimeError("legacy Fusion pipeline returned an invalid result")
            return result
        observed = await self.run_observed(
            request=request,
            gateway_model_name=gateway_model_name,
            fusion_config=fusion_config,
            request_body=request_body,
            http_client=http_client,
            proxy_http_clients=proxy_http_clients,
        )
        return observed.response

    async def run_observed(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        fusion_config: Dict[str, Any],
        request_body: Dict[str, Any],
        http_client: Any,
        proxy_http_clients: Optional[Dict[str, Any]] = None,
    ) -> ObservedChatResponse:
        result = await self._run_pipeline(
            request=request,
            gateway_model_name=gateway_model_name,
            fusion_config=fusion_config,
            request_body=request_body,
            http_client=http_client,
            proxy_http_clients=proxy_http_clients,
            observe=True,
        )
        if not isinstance(result, ObservedChatResponse):
            raise RuntimeError("observed Fusion pipeline returned an invalid result")
        return result

    async def _run_pipeline(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        fusion_config: Dict[str, Any],
        request_body: Dict[str, Any],
        http_client: Any,
        proxy_http_clients: Optional[Dict[str, Any]],
        observe: bool,
    ) -> Dict[str, Any] | ObservedChatResponse:
        proxy_http_clients = proxy_http_clients or {}

        base_messages = request_body.get("messages")
        if not isinstance(base_messages, list) or not base_messages:
            raise HTTPException(status_code=400, detail="Fusion request requires a non-empty 'messages' list.")

        panel = fusion_config.get("panel") or []
        main_member = fusion_config.get("main_model") or {}
        judge_member = fusion_config.get("judge_model") or main_member
        web_tools = fusion_config.get("web_tools") or None
        include_details = self._resolve_include_details(request_body, fusion_config)

        usage_total: Dict[str, Any] = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        cost_rate_registry = self._cost_rate_registry
        components: List[BillingComponent] = []

        # Base context shared by all members of this run (single timestamp so
        # panel/judge/main agree). Panel members also get a recency cue when web
        # tools are available; judge/main have no tools, so they only get time.
        temporal = _temporal_context()
        panel_context = temporal + (f" {_WEB_RECENCY_HINT}" if web_tools else "")
        panel_messages = [{"role": "system", "content": panel_context}, *base_messages]

        # 1. Fan-out: every panel member answers in parallel. When the Fusion
        #    model enables web tools, panel members run inside an agentic loop.
        if web_tools:
            panel_calls = [
                self._call_member_with_tools(
                    member,
                    panel_messages,
                    http_client,
                    proxy_http_clients,
                    request,
                    web_tools,
                    cost_rate_registry,
                    observe=observe,
                )
                for member in panel
            ]
        elif observe:
            panel_calls = [
                self._call_member_observed(
                    member,
                    panel_messages,
                    http_client,
                    proxy_http_clients,
                    request,
                )
                for member in panel
            ]
        else:
            panel_calls = [
                self._call_member(member, panel_messages, http_client, proxy_http_clients, request)
                for member in panel
            ]
        panel_raw = await asyncio.gather(*panel_calls, return_exceptions=True)

        panel_results: List[Dict[str, Any]] = []
        successful: List[Dict[str, Any]] = []
        for member, outcome in zip(panel, panel_raw):
            entry: Dict[str, Any] = {"provider": member.get("provider"), "model": member.get("model")}
            if isinstance(outcome, Exception):
                if isinstance(outcome, AccountingError):
                    raise outcome
                entry["error"] = str(outcome)
                logger.warning(
                    "Fusion panel member %s/%s failed: %s",
                    member.get("provider"), member.get("model"), outcome,
                )
            else:
                if observe:
                    content, usage, member_components = outcome
                    components.extend(member_components)
                else:
                    content, usage = outcome
                entry["content"] = content
                _add_usage(usage_total, usage, member=member, cost_rate_registry=cost_rate_registry)
                successful.append({"provider": member.get("provider"), "model": member.get("model"), "content": content})
            panel_results.append(entry)

        if not successful:
            raise HTTPException(
                status_code=502,
                detail=f"All Fusion panel members failed for model '{gateway_model_name}'.",
            )

        conversation_text = _messages_to_text(base_messages)
        panel_text = _format_panel_answers(successful)
        _sync_request_usage_tracker(request, usage_total)

        # 2. Fan-in: the judge returns a structured analysis.
        analysis: Dict[str, Any]
        judge_messages = [
            {"role": "system", "content": f"{_JUDGE_SYSTEM_PROMPT}\n\n{temporal}"},
            {
                "role": "user",
                "content": (
                    f"# User request\n{conversation_text}\n\n"
                    f"# Panel answers\n{panel_text}\n\n"
                    "Return the JSON analysis now."
                ),
            },
        ]
        try:
            if observe:
                judge_text, judge_usage, judge_components = await self._call_member_observed(
                    judge_member,
                    judge_messages,
                    http_client,
                    proxy_http_clients,
                    request,
                )
                components.extend(judge_components)
            else:
                judge_text, judge_usage = await self._call_member(
                    judge_member, judge_messages, http_client, proxy_http_clients, request
                )
            _add_usage(usage_total, judge_usage, member=judge_member, cost_rate_registry=cost_rate_registry)
            _sync_request_usage_tracker(request, usage_total)
            analysis = _parse_judge_analysis(judge_text)
        except AccountingError:
            raise
        except Exception as exc:  # judge analysis is auxiliary — never fail the request on it
            logger.warning("Fusion judge failed: %s", exc)
            analysis = {"error": str(exc)}

        # 3. Main model: write the final answer using the analysis.
        main_messages = [
            {"role": "system", "content": f"{_MAIN_SYSTEM_PROMPT}\n\n{temporal}"},
            {
                "role": "user",
                "content": (
                    f"# User request\n{conversation_text}\n\n"
                    f"# Panel answers\n{panel_text}\n\n"
                    f"# Judge analysis (JSON)\n{json.dumps(analysis, ensure_ascii=False)}\n\n"
                    "Write the final answer for the user now."
                ),
            },
        ]
        # Record the main model on request.state so usage logging attributes the
        # aggregated call to it.
        request.state.llmgateway_provider = main_member.get("provider")
        request.state.llmgateway_provider_model = main_member.get("model")

        try:
            if observe:
                final_content, main_usage, main_components = await self._call_member_observed(
                    main_member,
                    main_messages,
                    http_client,
                    proxy_http_clients,
                    request,
                )
            else:
                final_content, main_usage = await self._call_member(
                    main_member, main_messages, http_client, proxy_http_clients, request
                )
                main_components = ()
        except AccountingError:
            raise
        except Exception as exc:
            _sync_request_usage_tracker(request, usage_total)
            raise HTTPException(
                status_code=502,
                detail=f"Fusion main model failed for model '{gateway_model_name}'.",
            ) from exc

        _add_usage(usage_total, main_usage, member=main_member, cost_rate_registry=cost_rate_registry)
        _sync_request_usage_tracker(request, usage_total)
        components.extend(main_components)

        if include_details:
            footer = _render_analysis_footer(analysis)
            if footer:
                final_content = f"{final_content}\n\n{footer}"

        observation: ChatTerminalObservation | None = None
        response_usage = usage_total
        if observe:
            observation = ChatTerminalObservation(
                top_provider=main_member.get("provider"),
                top_model=main_member.get("model"),
                components=tuple(components),
            )
            response_usage = _public_component_usage(observation.usage)
            _sync_request_usage_tracker(request, response_usage)

        response = self._build_response(
            gateway_model_name=gateway_model_name,
            final_content=final_content,
            usage_total=response_usage,
            panel_results=panel_results,
            analysis=analysis,
            main_member=main_member,
            judge_member=judge_member,
            include_details=include_details,
        )
        if observation is None:
            return response
        return ObservedChatResponse(response=response, observation=observation)

    @staticmethod
    def _resolve_include_details(request_body: Dict[str, Any], fusion_config: Dict[str, Any]) -> bool:
        default = bool(fusion_config.get("include_details_default", True))
        fusion_opts = request_body.get("fusion")
        if isinstance(fusion_opts, dict) and "include_details" in fusion_opts:
            return bool(fusion_opts.get("include_details"))
        return default

    async def _call_member(
        self,
        member: Dict[str, Any],
        messages: List[Dict[str, Any]],
        http_client: Any,
        proxy_http_clients: Dict[str, Any],
        request: Request,
    ) -> Tuple[str, Dict[str, Any]]:
        """Call a single ``{provider, model}`` member and return (text, usage)."""
        openai_response = await self._raw_chat_call(
            member, messages, http_client, proxy_http_clients, request
        )
        return _extract_text(openai_response), openai_response.get("usage") or {}

    async def _call_member_observed(
        self,
        member: Dict[str, Any],
        messages: List[Dict[str, Any]],
        http_client: Any,
        proxy_http_clients: Dict[str, Any],
        request: Request,
    ) -> Tuple[str, Dict[str, Any], tuple[BillingComponent, ...]]:
        openai_response = await self._raw_chat_call(
            member,
            messages,
            http_client,
            proxy_http_clients,
            request,
        )
        provider = member.get("provider")
        model = member.get("model")
        if not isinstance(provider, str) or not isinstance(model, str):
            raise AccountingValidationError
        component = build_token_model_component(
            openai_response,
            provider=provider,
            model=model,
            cost_rate_registry=self._cost_rate_registry,
        )
        return (
            _extract_text(openai_response),
            openai_response.get("usage") or {},
            (component,),
        )

    async def _raw_chat_call(
        self,
        member: Dict[str, Any],
        messages: List[Dict[str, Any]],
        http_client: Any,
        proxy_http_clients: Dict[str, Any],
        request: Request | None = None,
        tools: Optional[List[Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Perform one chat call for a member and return the full OpenAI response.

        When ``tools`` is supplied it is passed through (and transparently
        translated for ``anthropic`` providers), so the returned message may
        contain ``tool_calls``.
        """
        provider_name = member.get("provider")
        model = member.get("model")
        provider_config = self._config_loader.providers_config.get(provider_name)
        if provider_config is None:
            raise HTTPException(
                status_code=500,
                detail=f"Provider '{provider_name}' referenced by a Fusion member is not configured.",
            )

        client = proxy_http_clients.get(provider_name, http_client)
        if request is None:
            api_key = resolve_provider_config_api_key(provider_config)
            auth_headers = resolve_provider_config_auth_headers(provider_config, api_key)
        else:
            auth_material = await resolve_provider_auth_material(
                request,
                provider_name=str(provider_name),
                provider_config=provider_config,
            )
            auth_headers = auth_material.headers

        payload: Dict[str, Any] = {"model": model, "messages": messages, "stream": False}
        if member.get("temperature") is not None:
            payload["temperature"] = member["temperature"]
        if member.get("reasoning") is not None:
            payload["reasoning"] = member["reasoning"]
        if member.get("max_completion_tokens") is not None:
            payload["max_completion_tokens"] = member["max_completion_tokens"]
        if tools:
            payload["tools"] = tools

        is_anthropic = getattr(provider_config, "type", "openai") == "anthropic"
        base_url = provider_config.baseUrl.rstrip("/")

        if is_anthropic:
            # Lazy import to avoid an import cycle with the chat API module.
            from ..api.v1.chat import (
                _anthropic_response_to_openai,
                _openai_request_to_anthropic_payload,
            )

            anthropic_payload = _openai_request_to_anthropic_payload(payload)
            anthropic_payload["model"] = model
            anthropic_payload.setdefault(
                "max_tokens", member.get("max_completion_tokens") or _DEFAULT_ANTHROPIC_MAX_TOKENS
            )
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_API_VERSION,
                FUSION_DEPTH_HEADER: "1",
                **auth_headers,
            }
            target_url = f"{base_url}/v1/messages"
            response_data, error_detail = await make_llm_request(
                client, target_url, headers, anthropic_payload, False
            )
            if error_detail or not isinstance(response_data, dict):
                raise RuntimeError(_format_error(error_detail))
            return _anthropic_response_to_openai(response_data, model)

        headers = {
            "Content-Type": "application/json",
            FUSION_DEPTH_HEADER: "1",
            **auth_headers,
        }
        target_url = f"{base_url}/chat/completions"
        response_data, error_detail = await make_llm_request(
            client, target_url, headers, payload, False
        )
        if error_detail or not isinstance(response_data, dict):
            raise RuntimeError(_format_error(error_detail))
        return response_data

    async def _call_member_with_tools(
        self,
        member: Dict[str, Any],
        messages: List[Dict[str, Any]],
        http_client: Any,
        proxy_http_clients: Dict[str, Any],
        request: Request,
        web_tools: Dict[str, Any],
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
        *,
        observe: bool = False,
    ) -> (
        Tuple[str, Dict[str, Any]]
        | Tuple[str, Dict[str, Any], tuple[BillingComponent, ...]]
    ):
        """Run a panel member in an agentic web-tool loop and return (text, usage).

        The member is offered ``web_search`` (+ optional ``web_fetch``) tools.
        Tool calls are executed in-process and fed back until the member answers
        without requesting tools or the budgets are exhausted, at which point a
        final tool-less call forces a text answer.
        """
        tool_schemas = _build_web_tool_schemas(web_tools)
        max_iterations = int(web_tools.get("max_iterations") or 4)
        max_tool_calls = int(web_tools.get("max_tool_calls") or 6)

        convo: List[Dict[str, Any]] = list(messages)
        usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        components: List[BillingComponent] = []
        tool_calls_made = 0

        for _iteration in range(max_iterations):
            offer_tools = tool_calls_made < max_tool_calls
            response = await self._raw_chat_call(
                member, convo, http_client, proxy_http_clients, request,
                tools=tool_schemas if offer_tools else None,
            )
            if observe:
                provider = member.get("provider")
                model = member.get("model")
                if not isinstance(provider, str) or not isinstance(model, str):
                    raise AccountingValidationError
                components.append(
                    build_token_model_component(
                        response,
                        provider=provider,
                        model=model,
                        cost_rate_registry=self._cost_rate_registry,
                    )
                )
            _add_usage(
                usage_total,
                response.get("usage") or {},
                member=member,
                cost_rate_registry=cost_rate_registry,
            )
            message = ((response.get("choices") or [{}])[0] or {}).get("message") or {}
            tool_calls = message.get("tool_calls") if offer_tools else None
            if not tool_calls:
                if observe:
                    return _extract_text(response), usage_total, tuple(components)
                return _extract_text(response), usage_total

            # Record the assistant turn that requested the tools.
            convo.append({
                "role": "assistant",
                "content": message.get("content") or "",
                "tool_calls": tool_calls,
            })
            for tool_call in tool_calls:
                if tool_calls_made >= max_tool_calls:
                    result_text = (
                        "Web tool budget exhausted. Answer with the information "
                        "already gathered."
                    )
                else:
                    tool_result = await self._execute_web_tool(
                        request,
                        tool_call,
                        web_tools,
                        observe=observe,
                    )
                    if observe:
                        result_text, tool_usage, tool_components = tool_result
                        components.extend(tool_components)
                    else:
                        result_text, tool_usage = tool_result
                    _add_usage(usage_total, tool_usage)
                    tool_calls_made += 1
                convo.append({
                    "role": "tool",
                    "tool_call_id": tool_call.get("id"),
                    "content": result_text,
                })

        # Iterations exhausted while still requesting tools — force a final answer.
        final_response = await self._raw_chat_call(
            member, convo, http_client, proxy_http_clients, request=request, tools=None
        )
        if observe:
            provider = member.get("provider")
            model = member.get("model")
            if not isinstance(provider, str) or not isinstance(model, str):
                raise AccountingValidationError
            components.append(
                build_token_model_component(
                    final_response,
                    provider=provider,
                    model=model,
                    cost_rate_registry=self._cost_rate_registry,
                )
            )
        _add_usage(
            usage_total,
            final_response.get("usage") or {},
            member=member,
            cost_rate_registry=cost_rate_registry,
        )
        if observe:
            return _extract_text(final_response), usage_total, tuple(components)
        return _extract_text(final_response), usage_total

    @staticmethod
    async def _execute_web_tool(
        request: Request,
        tool_call: Dict[str, Any],
        web_tools: Dict[str, Any],
        *,
        observe: bool = False,
    ) -> (
        Tuple[str, Dict[str, Any]]
        | Tuple[str, Dict[str, Any], tuple[BillingComponent, ...]]
    ):
        """Execute one web tool call in-process and return (result_text, usage)."""
        function = tool_call.get("function") or {}
        name = function.get("name")
        try:
            arguments = json.loads(function.get("arguments") or "{}")
        except (ValueError, TypeError):
            arguments = {}
        if not isinstance(arguments, dict):
            arguments = {}

        # Lazy import to avoid an import cycle with the web API module.
        from ..api.v1.web import (
            _UsageAccumulator,
            _read_with_model,
            _read_with_model_observed,
            _search_with_model,
            _search_with_model_observed,
        )

        accumulator = _UsageAccumulator()
        internal_components: list[BillingComponent] = []

        def _result(
            text: str,
            components: tuple[BillingComponent, ...] = (),
        ) -> (
            Tuple[str, Dict[str, Any]]
            | Tuple[str, Dict[str, Any], tuple[BillingComponent, ...]]
        ):
            usage = dict(accumulator.usage)
            if observe:
                return text, usage, components
            return text, usage

        try:
            if name == "web_search":
                query = arguments.get("query")
                if not isinstance(query, str) or not query.strip():
                    return _result("web_search error: 'query' is required.")
                if observe:
                    observed = await _search_with_model_observed(
                        request,
                        search_model=web_tools["search_model"],
                        query=query.strip(),
                        max_results=int(web_tools.get("max_results") or 5),
                        num_queries=1,
                        language="en",
                        usage_accumulator=accumulator,
                        expand_query=False,
                        _component_accumulator=internal_components,
                    )
                    return _result(
                        _format_search_results(observed.result),
                        observed.components,
                    )
                results = await _search_with_model(
                    request,
                    search_model=web_tools["search_model"],
                    query=query.strip(),
                    max_results=int(web_tools.get("max_results") or 5),
                    num_queries=1,
                    language="en",
                    usage_accumulator=accumulator,
                    expand_query=False,
                )
                return _result(_format_search_results(results))
            if name == "web_fetch":
                read_model = web_tools.get("read_model")
                if not read_model:
                    return _result(
                        "web_fetch error: this Fusion model has no read_model configured."
                    )
                url = arguments.get("url")
                if not isinstance(url, str) or not url.strip():
                    return _result("web_fetch error: 'url' is required.")
                if observe:
                    observed = await _read_with_model_observed(
                        request,
                        read_model=read_model,
                        url=url.strip(),
                        output_format="markdown",
                    )
                    return _result(
                        _format_read_result(observed.result),
                        observed.components,
                    )
                article = await _read_with_model(
                    request,
                    read_model=read_model,
                    url=url.strip(),
                    output_format="markdown",
                )
                return _result(_format_read_result(article))
        except AccountingError:
            raise
        except HTTPException as exc:
            return _result(
                f"{name} error: {exc.detail}",
                tuple(internal_components),
            )
        except Exception as exc:  # surface the failure to the model, do not abort the panel member
            logger.warning("Fusion web tool '%s' failed: %s", name, exc)
            return _result(
                f"{name} error: {exc}",
                tuple(internal_components),
            )
        return _result(f"Unknown tool '{name}'.")

    @staticmethod
    def _build_response(
        *,
        gateway_model_name: str,
        final_content: str,
        usage_total: Dict[str, int],
        panel_results: List[Dict[str, Any]],
        analysis: Dict[str, Any],
        main_member: Dict[str, Any],
        judge_member: Dict[str, Any],
        include_details: bool,
    ) -> Dict[str, Any]:
        if include_details:
            panel_payload = panel_results
        else:
            panel_payload = [
                {k: v for k, v in entry.items() if k != "content"}
                for entry in panel_results
            ]

        fusion_block: Dict[str, Any] = {
            "panel": panel_payload,
            "analysis": analysis,
            "main": {"provider": main_member.get("provider"), "model": main_member.get("model")},
            "judge": {"provider": judge_member.get("provider"), "model": judge_member.get("model")},
        }

        return {
            "id": f"chatcmpl-fusion-{uuid4().hex}",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": gateway_model_name,
            "choices": [
                {
                    "index": 0,
                    "message": {"role": "assistant", "content": final_content},
                    "finish_reason": "stop",
                }
            ],
            "usage": _public_usage(usage_total),
            "fusion": fusion_block,
        }


def _build_web_tool_schemas(web_tools: Dict[str, Any]) -> List[Dict[str, Any]]:
    """OpenAI-shaped tool schemas offered to a panel member.

    ``web_fetch`` is only offered when a ``read_model`` is configured.
    """
    schemas: List[Dict[str, Any]] = [
        {
            "type": "function",
            "function": {
                "name": "web_search",
                "description": (
                    "Search the web for up-to-date information. Returns a list of "
                    "results with url, title and snippet."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "query": {"type": "string", "description": "The search query."},
                    },
                    "required": ["query"],
                },
            },
        }
    ]
    if web_tools.get("read_model"):
        schemas.append({
            "type": "function",
            "function": {
                "name": "web_fetch",
                "description": "Fetch and read the full text content of a web page by its URL.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "url": {"type": "string", "description": "The absolute URL to fetch."},
                    },
                    "required": ["url"],
                },
            },
        })
    return schemas


def _format_search_results(results: List[Dict[str, Any]]) -> str:
    """Render web_search results into compact text for the tool message."""
    if not results:
        return "No results."
    lines: List[str] = []
    for index, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        snippet = item.get("snippet") or ""
        lines.append(f"[{index}] {title}\n{url}\n{snippet}".strip())
    return "\n\n".join(lines)


def _format_read_result(article: Dict[str, Any]) -> str:
    """Render a web_fetch result into text for the tool message."""
    if not isinstance(article, dict):
        return str(article)
    title = article.get("title") or ""
    url = article.get("url") or ""
    content = article.get("content") or ""
    header = "\n".join(part for part in (title, url) if part)
    return f"{header}\n\n{content}".strip() if header else content


def _format_error(error_detail: Any) -> str:
    if error_detail is None:
        return "Provider returned an empty response."
    return str(error_detail)


def _format_panel_answers(successful: List[Dict[str, Any]]) -> str:
    blocks: List[str] = []
    for index, item in enumerate(successful, start=1):
        label = f"Model {index} ({item.get('provider')}/{item.get('model')})"
        blocks.append(f"## {label}\n{item.get('content', '')}")
    return "\n\n".join(blocks)


def _render_analysis_footer(analysis: Dict[str, Any]) -> str:
    """Render a compact human-readable summary appended to the final answer."""
    if not isinstance(analysis, dict) or analysis.get("raw") is not None or analysis.get("error"):
        return ""

    def _section(title: str, values: Any) -> Optional[str]:
        if not isinstance(values, list) or not values:
            return None
        items = "\n".join(f"  - {str(v)}" for v in values)
        return f"{title}:\n{items}"

    sections = [
        _section("Agreements", analysis.get("agreements")),
        _section("Disputes", analysis.get("disputes")),
        _section("Blind spots", analysis.get("blind_spots")),
    ]
    rendered = [section for section in sections if section]
    if not rendered:
        return ""
    return "---\n**Fusion panel analysis**\n" + "\n".join(rendered)
