"""Router model service.

A Router gateway model asks a configured decisions gateway model to pick one
explicit target from an allowlist (a ``choice`` question), then delegates the
original request to that target, streaming or not. Targets are either full gateway chat models or a specific fallback-chain
entry inside a gateway chat model.
"""

from __future__ import annotations

import copy
import dataclasses
import logging
import math
import re
from collections.abc import Mapping
from typing import Any

from fastapi import HTTPException, Request

from ..utils.usage_tracking import ModelCostRates
from .accounting import AccountingValidationError
from .chat_accounting import ChatTerminalObservation, ObservedChatResponse
from .operation_accounting import build_token_model_component

logger = logging.getLogger(__name__)

_RECENT_TOOL_WINDOW = 3
_TOOL_ERROR_MARKERS = (
    "out of memory",
    "cannot allocate memory",
    "connection refused",
    "traceback (most recent call last)",
    "modulenotfounderror:",
    "importerror:",
    "assertionerror",
    "syntaxerror:",
    "timeouterror",
    "deadline exceeded",
    "filenotfounderror:",
    "no such file or directory",
    "exit code 1",
    "exit code 2",
    "exit status 1",
    "returned non-zero",
)
_TEST_PASS_MARKERS = (
    "tests passed",
    "all tests passed",
    "passed in",
    "test result: ok",
)
_EDIT_TOOL_NAMES = {
    "apply_patch",
    "create_file",
    "edit",
    "multiedit",
    "new_file",
    "notebookedit",
    "patch",
    "str_replace",
    "text_editor",
    "write",
    "write_file",
}
_NONZERO_TEST_FAILURE = re.compile(r"\b[1-9]\d*\s+(?:failed|failures?|errors?)\b")

_DECISION_QUESTION = "route"
_DECISION_INSTRUCTIONS = (
    "Choose the candidate model that should answer the chat request in the state. "
    "Each option describes one candidate."
)


def _decision_instructions(routing_policy: str | None) -> str:
    if not routing_policy:
        return _DECISION_INSTRUCTIONS
    return f"{_DECISION_INSTRUCTIONS}\n\nRouting policy configured by the gateway operator:\n{routing_policy}"


def _candidate_criterion(candidate: dict[str, Any]) -> str:
    """Option text for one candidate: operator hints plus the models it runs on."""
    chain = candidate.get("fallback_chain") or candidate.get("remaining_fallback_chain") or []
    models = ", ".join(f"{item.get('provider')}/{item.get('model')}" for item in chain)
    parts = [candidate.get("description"), f"Cost tier: {candidate['cost_hint']}." if candidate.get("cost_hint") else None]
    parts.append(f"Models: {models}." if models else None)
    return " ".join(part for part in parts if part) or str(candidate["id"])


def _decision_payload(
    selector_model: str,
    routing_policy: str | None,
    candidates: list[dict[str, Any]],
    request_body: dict[str, Any],
) -> dict[str, Any]:
    return {
        "model": selector_model,
        "state": {"request": _request_summary(request_body)},
        "questions": {
            _DECISION_QUESTION: {
                "type": "choice",
                "instructions": _decision_instructions(routing_policy),
                "criteria": {candidate["id"]: _candidate_criterion(candidate) for candidate in candidates},
            }
        },
    }


def _parse_decision_answer(response: dict[str, Any]) -> dict[str, Any]:
    answer = (response.get("answers") or {}).get(_DECISION_QUESTION)
    if not isinstance(answer, dict):
        raise ValueError(f"decision response has no '{_DECISION_QUESTION}' answer")
    candidate_id = answer.get("choice")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("decision answer must contain non-empty 'choice'")
    return {"candidate_id": candidate_id.strip(), "confidence": answer.get("confidence")}


def _provider_model_metadata(provider_config: Any, model: str) -> dict[str, Any]:
    models_meta = getattr(provider_config, "models", None)
    if not isinstance(models_meta, dict):
        return {}
    model_meta = models_meta.get(model)
    if not isinstance(model_meta, dict):
        return {}
    metadata: dict[str, Any] = {}
    for key in ("input_rate", "output_rate", "context_length", "max_completion_tokens"):
        if key in model_meta:
            metadata[key] = model_meta[key]
    upstream_limits = model_meta.get("upstream_limits")
    if isinstance(upstream_limits, dict):
        metadata["upstream_limits"] = upstream_limits
    return metadata


def _fallback_chain_items(
    *,
    gateway_model: str,
    fallback_models: list[dict[str, Any]],
    providers_config: dict[str, Any],
    start_index: int = 0,
) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for index, fallback_model in enumerate(fallback_models[start_index:], start=start_index):
        provider = fallback_model.get("provider")
        model = fallback_model.get("model")
        item = {
            "gateway_model": gateway_model,
            "index": index,
            "provider": provider,
            "model": model,
        }
        provider_config = providers_config.get(provider)
        if provider_config is not None and isinstance(model, str):
            metadata = _provider_model_metadata(provider_config, model)
            if metadata:
                item["metadata"] = metadata
        items.append(item)
    return items


def _target_hints(target: dict[str, Any]) -> dict[str, Any]:
    """Operator-authored hints that tell the selector what a target is for."""
    hints: dict[str, Any] = {}
    for key in ("description", "cost_hint"):
        value = target.get(key)
        if value:
            hints[key] = value
    return hints


def _candidate_id(target: dict[str, Any]) -> str:
    if target.get("type") == "gateway_model":
        return f"gateway:{target.get('model')}"
    return f"fallback_entry:{target.get('gateway_model')}:{target.get('index')}"


def build_router_candidates(
    *,
    router_config: dict[str, Any],
    fallback_rules: dict[str, dict[str, Any]],
    providers_config: dict[str, Any],
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for target in router_config.get("targets", []):
        target_type = target.get("type")
        candidate_id = _candidate_id(target)
        if target_type == "gateway_model":
            gateway_model = target.get("model")
            target_rule = fallback_rules[gateway_model]
            fallback_models = target_rule.get("fallback_models") or []
            candidates.append(
                {
                    "id": candidate_id,
                    "type": "gateway_model",
                    "gateway_model": gateway_model,
                    **_target_hints(target),
                    "fallback_chain": _fallback_chain_items(
                        gateway_model=gateway_model,
                        fallback_models=fallback_models,
                        providers_config=providers_config,
                    ),
                }
            )
            continue

        gateway_model = target.get("gateway_model")
        index = int(target.get("index"))
        target_rule = fallback_rules[gateway_model]
        fallback_models = target_rule.get("fallback_models") or []
        selected_entry = fallback_models[index]
        candidates.append(
            {
                "id": candidate_id,
                "type": "fallback_entry",
                "gateway_model": gateway_model,
                "index": index,
                **_target_hints(target),
                "provider": selected_entry.get("provider"),
                "model": selected_entry.get("model"),
                "remaining_fallback_chain": _fallback_chain_items(
                    gateway_model=gateway_model,
                    fallback_models=fallback_models,
                    providers_config=providers_config,
                    start_index=index,
                ),
            }
        )
    return candidates


def _request_summary(request_body: dict[str, Any]) -> dict[str, Any]:
    summary = {
        "messages": request_body.get("messages"),
    }
    for key in ("response_format", "tools", "tool_choice", "temperature", "max_tokens", "max_completion_tokens"):
        if key in request_body:
            summary[key] = request_body[key]
    return summary


def _content_text(content: Any) -> str:
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return ""
    parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if isinstance(text, str):
            parts.append(text)
        nested = block.get("content")
        if block.get("type") == "tool_result":
            nested_text = _content_text(nested)
            if nested_text:
                parts.append(nested_text)
    return "\n".join(parts)


def _tool_history(messages: list[Any]) -> tuple[list[str], list[str]]:
    results: list[str] = []
    calls: list[str] = []
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("role") == "tool":
            results.append(_content_text(message.get("content")))
        for call in message.get("tool_calls") or []:
            if not isinstance(call, dict):
                continue
            function = call.get("function")
            if isinstance(function, dict) and isinstance(function.get("name"), str):
                calls.append(function["name"].lower())
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if not isinstance(block, dict):
                continue
            block_type = block.get("type")
            if block_type == "tool_use" and isinstance(block.get("name"), str):
                calls.append(block["name"].lower())
            elif block_type in {"tool_result", "function_call_output"}:
                results.append(_content_text(block.get("content") or block.get("output")))
    return results[-_RECENT_TOOL_WINDOW:], calls[-_RECENT_TOOL_WINDOW:]


def _tool_result_failed(text: str) -> bool:
    lower = text.lower()
    return any(marker in lower for marker in _TOOL_ERROR_MARKERS)


def _tests_passed(results: list[str]) -> bool:
    for result in results:
        lower = result.lower()
        if (
            any(marker in lower for marker in _TEST_PASS_MARKERS)
            and not _tool_result_failed(result)
            and _NONZERO_TEST_FAILURE.search(lower) is None
        ):
            return True
    return False


def _candidate_rate(candidate: dict[str, Any]) -> float | None:
    metadata = candidate.get("metadata")
    if not isinstance(metadata, dict):
        fallback_chain = candidate.get("fallback_chain") or candidate.get("remaining_fallback_chain")
        if isinstance(fallback_chain, list) and fallback_chain and isinstance(fallback_chain[0], dict):
            metadata = fallback_chain[0].get("metadata")
    if not isinstance(metadata, dict):
        return None
    input_rate = metadata.get("input_rate")
    output_rate = metadata.get("output_rate")
    if not isinstance(input_rate, (int, float)) or not isinstance(output_rate, (int, float)):
        return None
    rate = float(input_rate) + float(output_rate)
    if not math.isfinite(rate) or rate <= 0:
        return None
    return rate


def _candidate_for_cost_tier(candidates: list[dict[str, Any]], tier: str) -> dict[str, Any] | None:
    explicit = [candidate for candidate in candidates if candidate.get("cost_hint") == tier]
    if len(explicit) == 1:
        return explicit[0]
    if explicit:
        return None

    priced = [
        (rate, candidate)
        for candidate in candidates
        if candidate.get("cost_hint") is None
        if (rate := _candidate_rate(candidate)) is not None
    ]
    if len(priced) < 2:
        return None
    extreme = min(rate for rate, _candidate in priced) if tier == "cheap" else max(
        rate for rate, _candidate in priced
    )
    matches = [candidate for rate, candidate in priced if rate == extreme]
    return matches[0] if len(matches) == 1 else None


def _direct_stage_decision(
    candidates: list[dict[str, Any]], messages: list[Any]
) -> tuple[dict[str, Any], str] | None:
    results, calls = _tool_history(messages)
    if not results:
        return None
    if any(_tool_result_failed(result) for result in results):
        candidate = _candidate_for_cost_tier(candidates, "premium")
        return (candidate, "tool_error") if candidate is not None else None
    if _tests_passed(results) and any(call in _EDIT_TOOL_NAMES for call in calls):
        candidate = _candidate_for_cost_tier(candidates, "cheap")
        return (candidate, "tests_passed_after_change") if candidate is not None else None
    return None


def _coerce_token_count(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _usage_for_cost(usage: dict[str, Any]) -> dict[str, Any]:
    if "prompt_tokens" in usage or "completion_tokens" in usage:
        return usage
    prompt_tokens = (
        _coerce_token_count(usage.get("input_tokens"))
        + _coerce_token_count(usage.get("cache_creation_input_tokens"))
        + _coerce_token_count(usage.get("cache_read_input_tokens"))
    )
    completion_tokens = _coerce_token_count(usage.get("output_tokens"))
    normalized_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }
    if "cost" in usage:
        normalized_usage["cost"] = usage["cost"]
    return normalized_usage


def _add_usage(target_usage: dict[str, Any], extra_usage: dict[str, Any]) -> None:
    if "total_tokens" not in target_usage:
        target_usage["total_tokens"] = _coerce_token_count(target_usage.get("prompt_tokens")) + _coerce_token_count(
            target_usage.get("completion_tokens")
        )
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"):
        value = extra_usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            target_usage[key] = int(target_usage.get(key) or 0) + int(value)


def _add_anthropic_usage(target_usage: dict[str, Any], extra_usage: dict[str, Any]) -> None:
    normalized_extra = _usage_for_cost(extra_usage)
    input_tokens = _coerce_token_count(normalized_extra.get("prompt_tokens"))
    output_tokens = _coerce_token_count(normalized_extra.get("completion_tokens"))
    if input_tokens:
        target_usage["input_tokens"] = _coerce_token_count(target_usage.get("input_tokens")) + input_tokens
    if output_tokens:
        target_usage["output_tokens"] = _coerce_token_count(target_usage.get("output_tokens")) + output_tokens


class _PrefixedChatHandoff:
    """Chat accounting handoff that bills the decision call before the delegate's components."""

    def __init__(self, handoff: Any, components: tuple[Any, ...]) -> None:
        self._handoff = handoff
        self._components = components

    def _prefixed(self, observation: ChatTerminalObservation | None) -> ChatTerminalObservation | None:
        if observation is None:
            return None
        return dataclasses.replace(observation, components=self._components + tuple(observation.components))

    def publish(self, observation: ChatTerminalObservation) -> None:
        self._handoff.publish(self._prefixed(observation))

    def publish_stream_observer(self, observer: Any, *, build_partial: Any = None) -> None:
        self._handoff.publish_stream_observer(
            lambda event: self._prefixed(observer(event)),
            build_partial=None if build_partial is None else lambda: self._prefixed(build_partial()),
        )


class RouterModelService:
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
        router_config: dict[str, Any],
        request_body: dict[str, Any],
    ) -> Any:
        observed = await self.run_observed(
            request=request,
            gateway_model_name=gateway_model_name,
            router_config=router_config,
            request_body=request_body,
        )
        return observed.response

    async def run_stream(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        router_config: dict[str, Any],
        request_body: dict[str, Any],
        accounting_handoff: Any = None,
    ) -> Any:
        """Select a target, then return its stream; the decision cost joins the stream accounting."""
        selected_candidate, _decision, _source, selector_component, _usage = await self._select(
            request=request,
            router_config=router_config,
            request_body=request_body,
        )
        handoff = accounting_handoff
        if accounting_handoff is not None and selector_component is not None:
            handoff = _PrefixedChatHandoff(accounting_handoff, (selector_component,))
        return await self._dispatch_selected_target(
            request=request,
            request_body=request_body,
            selected_candidate=selected_candidate,
            accounting_handoff=handoff,
        )

    async def run_observed(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        router_config: dict[str, Any],
        request_body: dict[str, Any],
    ) -> ObservedChatResponse:
        if request_body.get("stream", False):
            raise HTTPException(status_code=400, detail="Use run_stream for streaming Router requests.")

        selected_candidate, decision, decision_source, selector_component, selector_usage = await self._select(
            request=request,
            router_config=router_config,
            request_body=request_body,
        )
        selected_id = selected_candidate["id"]

        response = await self._dispatch_selected_target(
            request=request,
            request_body=request_body,
            selected_candidate=selected_candidate,
        )

        if not isinstance(response, dict):
            raise HTTPException(status_code=502, detail="Router delegate returned a non-JSON response.")

        is_anthropic_raw = bool(
            getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
        )
        if "choices" not in response and not is_anthropic_raw:
            raise HTTPException(status_code=502, detail="Router delegate returned an invalid chat response.")

        delegate_provider = getattr(request.state, "llmgateway_provider", None)
        delegate_model = getattr(request.state, "llmgateway_provider_model", None)
        if not isinstance(delegate_provider, str) or not isinstance(delegate_model, str):
            raise AccountingValidationError
        delegate_component = build_token_model_component(
            response,
            provider=delegate_provider,
            model=delegate_model,
            cost_rate_registry=self._cost_rate_registry,
        )
        components = (delegate_component,) if selector_component is None else (
            selector_component,
            delegate_component,
        )
        observation = ChatTerminalObservation(
            top_provider=delegate_provider,
            top_model=delegate_model,
            components=components,
        )

        response_usage = response.get("usage")
        if not isinstance(response_usage, dict):
            response_usage = {}
            response["usage"] = response_usage
        if is_anthropic_raw and "choices" not in response:
            _add_anthropic_usage(response_usage, selector_usage)
        else:
            _add_usage(response_usage, selector_usage)
        response_usage["cost"] = observation.usage.cost
        response["router"] = {
            "model": gateway_model_name,
            "selector_model": router_config["selector_model"],
            "selected_candidate_id": selected_id,
            "selected_candidate": selected_candidate,
            "reason": decision.get("reason"),
            "confidence": decision.get("confidence"),
            "decision_source": decision_source,
        }
        return ObservedChatResponse(response=response, observation=observation)

    async def _select(
        self,
        *,
        request: Request,
        router_config: dict[str, Any],
        request_body: dict[str, Any],
    ) -> tuple[dict[str, Any], dict[str, Any], str, Any, dict[str, Any]]:
        """Pick a candidate from tool history or the decisions model.

        Returns the candidate, the decision, its source, the decision billing
        component (``None`` without a model call) and the decision usage in
        chat ``prompt_tokens``/``completion_tokens`` form.
        """
        messages = request_body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="Router request requires a non-empty 'messages' list.")

        candidates = build_router_candidates(
            router_config=router_config,
            fallback_rules=self._config_loader.fallback_rules,
            providers_config=self._config_loader.providers_config,
        )
        direct_decision = _direct_stage_decision(candidates, messages)
        if direct_decision is not None:
            selected_candidate, reason = direct_decision
            decision = {"candidate_id": selected_candidate["id"], "reason": reason, "confidence": None}
            return selected_candidate, decision, "tool_history", None, {}

        decision_response, selector_component = await self._call_decision_model(
            request=request,
            selector_model=router_config["selector_model"],
            payload=_decision_payload(
                router_config["selector_model"],
                router_config.get("routing_policy"),
                candidates,
                request_body,
            ),
        )
        try:
            decision = _parse_decision_answer(decision_response)
        except ValueError as exc:
            raise HTTPException(status_code=502, detail=f"Router decision model returned an invalid answer: {exc}") from exc

        selected_candidate = {candidate["id"]: candidate for candidate in candidates}.get(decision["candidate_id"])
        if selected_candidate is None:
            raise HTTPException(
                status_code=502,
                detail=f"Router decision model chose unknown candidate_id '{decision['candidate_id']}'.",
            )
        usage = decision_response.get("usage")
        selector_usage = _usage_for_cost(usage) if isinstance(usage, dict) else {}
        return selected_candidate, decision, "decision_model", selector_component, selector_usage

    async def _call_decision_model(
        self,
        *,
        request: Request,
        selector_model: str,
        payload: dict[str, Any],
    ) -> tuple[dict[str, Any], Any]:
        from ..api.v1.decisions import call_decision_routes

        routes = request.state.runtime_snapshot.operation_dispatcher.lookup_routes("decisions", selector_model)
        if not routes:
            raise HTTPException(
                status_code=500,
                detail=f"Router decision model '{selector_model}' has no decisions route.",
            )

        def observe(response: dict[str, Any], route: Any) -> Any:
            return build_token_model_component(
                response,
                provider=route.provider,
                model=route.model,
                cost_rate_registry=self._cost_rate_registry,
                input_only=True,
            )

        # The decisions call reuses request.state; restore the chat request identity afterwards.
        saved_state = {
            name: getattr(request.state, name, None)
            for name in ("llmgateway_gateway_model", "llmgateway_operation", "llmgateway_target_path")
        }
        try:
            response, _status_code, component = await call_decision_routes(
                request,
                selector_model,
                payload,
                routes,
                cost_rate_registry=self._cost_rate_registry,
                observe=observe,
            )
        finally:
            for name, value in saved_state.items():
                setattr(request.state, name, value)
        return response, component

    async def _dispatch_selected_target(
        self,
        *,
        request: Request,
        request_body: dict[str, Any],
        selected_candidate: dict[str, Any],
        accounting_handoff: Any = None,
    ) -> Any:
        from ..api.v1.chat import _dispatch_chat_request

        target_body = copy.deepcopy(request_body)
        target_body.pop("router", None)
        target_body["model"] = selected_candidate["gateway_model"]

        previous_start_index = getattr(request.state, "llmgateway_forced_fallback_start_index", None)
        previous_owner_model = getattr(request.state, "llmgateway_forced_fallback_owner_model", None)
        try:
            if selected_candidate["type"] == "fallback_entry":
                request.state.llmgateway_forced_fallback_start_index = selected_candidate["index"]
                request.state.llmgateway_forced_fallback_owner_model = selected_candidate["gateway_model"]
            else:
                request.state.llmgateway_forced_fallback_start_index = None
                request.state.llmgateway_forced_fallback_owner_model = None
            return await _dispatch_chat_request(request, target_body, accounting_handoff=accounting_handoff)
        finally:
            request.state.llmgateway_forced_fallback_start_index = previous_start_index
            request.state.llmgateway_forced_fallback_owner_model = previous_owner_model
