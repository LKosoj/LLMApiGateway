"""Router model service.

A Router gateway model asks a configured selector gateway model to pick one
explicit target from an allowlist, then delegates the original request to that
target. Targets are either full gateway chat models or a specific fallback-chain
entry inside a gateway chat model.
"""

from __future__ import annotations

import copy
import json
import logging
from typing import Any

from fastapi import HTTPException, Request

logger = logging.getLogger(__name__)

_SELECTOR_SYSTEM_PROMPT = (
    "You are an internal routing selector for LLMApiGateway. Choose exactly one "
    "candidate for the user request. Return ONLY a JSON object with these keys: "
    '"candidate_id" (string, must exactly match one candidate id), "reason" '
    "(short string), and \"confidence\" (number from 0 to 1). Do not answer the "
    "user request itself."
)


def _extract_text(openai_response: dict[str, Any]) -> str:
    choices = openai_response.get("choices") or []
    if not choices:
        return ""
    first_choice = choices[0] if isinstance(choices[0], dict) else {}
    message = first_choice.get("message") if isinstance(first_choice, dict) else {}
    if not isinstance(message, dict):
        return ""
    content = message.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            part.get("text", "")
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        return "".join(parts)
    return ""


def _parse_selector_decision(text: str) -> dict[str, Any]:
    stripped = (text or "").strip()
    if not stripped:
        raise ValueError("selector returned an empty response")
    if stripped.startswith("```"):
        stripped = stripped.strip("`")
        newline = stripped.find("\n")
        if newline != -1:
            stripped = stripped[newline + 1:]
    start = stripped.find("{")
    end = stripped.rfind("}")
    if start != -1 and end != -1 and end > start:
        stripped = stripped[start:end + 1]
    parsed = json.loads(stripped)
    if not isinstance(parsed, dict):
        raise ValueError("selector response must be a JSON object")
    candidate_id = parsed.get("candidate_id")
    if not isinstance(candidate_id, str) or not candidate_id.strip():
        raise ValueError("selector response must contain non-empty 'candidate_id'")
    parsed["candidate_id"] = candidate_id.strip()
    return parsed


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


def _add_usage(target_usage: dict[str, Any], extra_usage: dict[str, Any]) -> None:
    for key in ("prompt_tokens", "completion_tokens", "total_tokens", "reasoning_tokens", "cached_tokens"):
        value = extra_usage.get(key)
        if isinstance(value, (int, float)) and value > 0:
            target_usage[key] = int(target_usage.get(key) or 0) + int(value)

    extra_cost = extra_usage.get("cost")
    if isinstance(extra_cost, (int, float)):
        target_usage["cost"] = float(target_usage.get("cost") or 0.0) + float(extra_cost)


class RouterModelService:
    def __init__(self, config_loader: Any) -> None:
        self._config_loader = config_loader

    async def run(
        self,
        *,
        request: Request,
        gateway_model_name: str,
        router_config: dict[str, Any],
        request_body: dict[str, Any],
    ) -> Any:
        if request_body.get("stream", False):
            raise HTTPException(status_code=400, detail="Router models do not support streaming responses.")

        messages = request_body.get("messages")
        if not isinstance(messages, list) or not messages:
            raise HTTPException(status_code=400, detail="Router request requires a non-empty 'messages' list.")

        candidates = build_router_candidates(
            router_config=router_config,
            fallback_rules=self._config_loader.fallback_rules,
            providers_config=self._config_loader.providers_config,
        )
        candidates_by_id = {candidate["id"]: candidate for candidate in candidates}

        selector_response = await self._call_selector(
            request=request,
            selector_model=router_config["selector_model"],
            candidates=candidates,
            request_body=request_body,
        )
        selector_usage = selector_response.get("usage") if isinstance(selector_response, dict) else {}
        selector_text = _extract_text(selector_response)
        try:
            decision = _parse_selector_decision(selector_text)
        except Exception as exc:
            raise HTTPException(status_code=502, detail=f"Router selector returned invalid JSON: {exc}") from exc

        selected_id = decision["candidate_id"]
        selected_candidate = candidates_by_id.get(selected_id)
        if selected_candidate is None:
            raise HTTPException(
                status_code=502,
                detail=f"Router selector chose unknown candidate_id '{selected_id}'.",
            )

        response = await self._dispatch_selected_target(
            request=request,
            request_body=request_body,
            selected_candidate=selected_candidate,
        )

        if isinstance(response, dict):
            if isinstance(response.get("usage"), dict) and "choices" in response:
                _add_usage(response["usage"], selector_usage or {})
            response["router"] = {
                "model": gateway_model_name,
                "selector_model": router_config["selector_model"],
                "selected_candidate_id": selected_id,
                "selected_candidate": selected_candidate,
                "reason": decision.get("reason"),
                "confidence": decision.get("confidence"),
            }
        return response

    async def _call_selector(
        self,
        *,
        request: Request,
        selector_model: str,
        candidates: list[dict[str, Any]],
        request_body: dict[str, Any],
    ) -> dict[str, Any]:
        from ..api.v1.chat import _dispatch_chat_request

        selector_body = {
            "model": selector_model,
            "stream": False,
            "temperature": 0,
            "messages": [
                {"role": "system", "content": _SELECTOR_SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {
                            "candidates": candidates,
                            "request": _request_summary(request_body),
                        },
                        ensure_ascii=False,
                    ),
                },
            ],
        }

        original_anthropic_payload = getattr(request.state, "llmgateway_original_anthropic_payload", None)
        original_anthropic_raw = getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
        request.state.llmgateway_original_anthropic_payload = None
        request.state.llmgateway_response_is_anthropic_raw = False
        try:
            response = await _dispatch_chat_request(
                request,
                selector_body,
                enforce_model_access=False,
            )
        finally:
            request.state.llmgateway_original_anthropic_payload = original_anthropic_payload
            request.state.llmgateway_response_is_anthropic_raw = original_anthropic_raw

        if not isinstance(response, dict):
            raise HTTPException(status_code=502, detail="Router selector returned a non-JSON response.")
        return response

    async def _dispatch_selected_target(
        self,
        *,
        request: Request,
        request_body: dict[str, Any],
        selected_candidate: dict[str, Any],
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
            return await _dispatch_chat_request(request, target_body)
        finally:
            request.state.llmgateway_forced_fallback_start_index = previous_start_index
            request.state.llmgateway_forced_fallback_owner_model = previous_owner_model
