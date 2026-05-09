import functools
import json
import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

import tiktoken
from fastapi import Request

logger = logging.getLogger(__name__)

TIKTOKEN_ENCODING_FALLBACKS = ("o200k_base", "cl100k_base")

DEFAULT_TOKENS_USAGE = {
    "prompt_tokens": 0,
    "completion_tokens": 0,
    "total_tokens": 0,
    "reasoning_tokens": 0,
    "cached_tokens": 0,
    "cost": 0,
    "cost_saved": 0,
    "is_estimated": False,
}

TOKEN_RATE_SCALE = 1_000_000
FALLBACK_USAGE_SOURCE = "estimate_fallback"


@dataclass(frozen=True)
class TokensEstimator:
    encoding: tiktoken.Encoding
    is_fallback: bool = False

    def encode(self, text: str) -> list[int]:
        return self.encoding.encode(text)


@dataclass(frozen=True)
class ModelCostRates:
    input_rate: float
    output_rate: float


def initialize_tokens_usage() -> dict[str, Any]:
    return dict(DEFAULT_TOKENS_USAGE)


def _as_non_negative_finite_float(value: Any) -> float | None:
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate < 0:
        return None
    return rate


def _as_non_negative_int(value: Any) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _first_present(mapping: Mapping[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def _coerce_model_cost_rates(raw_rates: Any) -> ModelCostRates | None:
    if isinstance(raw_rates, ModelCostRates):
        input_rate = _as_non_negative_finite_float(raw_rates.input_rate)
        output_rate = _as_non_negative_finite_float(raw_rates.output_rate)
        if input_rate is None or output_rate is None:
            return None
        return ModelCostRates(input_rate=input_rate, output_rate=output_rate)

    input_rate_raw: Any = None
    output_rate_raw: Any = None

    if isinstance(raw_rates, (list, tuple)) and len(raw_rates) >= 2:
        input_rate_raw = raw_rates[0]
        output_rate_raw = raw_rates[1]
    elif isinstance(raw_rates, Mapping):
        rates_mapping = raw_rates
        if "pricing" in raw_rates and not any(
            key in raw_rates for key in ("input_rate", "output_rate", "prompt_rate", "completion_rate")
        ):
            pricing = raw_rates["pricing"]
            if isinstance(pricing, Mapping):
                rates_mapping = pricing
        input_rate_raw = _first_present(
            rates_mapping,
            (
                "input_rate",
                "input_cost_per_million",
                "prompt_rate",
                "prompt_cost_per_million",
            ),
        )
        output_rate_raw = _first_present(
            rates_mapping,
            (
                "output_rate",
                "output_cost_per_million",
                "completion_rate",
                "completion_cost_per_million",
            ),
        )

    input_rate = _as_non_negative_finite_float(input_rate_raw)
    output_rate = _as_non_negative_finite_float(output_rate_raw)
    if input_rate is None or output_rate is None:
        return None
    return ModelCostRates(input_rate=input_rate, output_rate=output_rate)


def build_model_cost_rate_registry(
    providers_config: Mapping[str, Any] | None,
) -> dict[tuple[str, str], ModelCostRates]:
    """Build a provider/model -> per-million-token rate registry.

    ``providers.json`` may define rates under each provider's optional
    ``models`` block, either as a mapping keyed by model name or as a list of
    model objects containing ``model``/``name``/``id``.
    """
    registry: dict[tuple[str, str], ModelCostRates] = {}
    if not isinstance(providers_config, Mapping):
        return registry

    for provider_name, provider_config in providers_config.items():
        if isinstance(provider_config, Mapping):
            models = provider_config.get("models")
        else:
            models = getattr(provider_config, "models", None)
        if isinstance(models, Mapping):
            for model_name, model_rates in models.items():
                rates = _coerce_model_cost_rates(model_rates)
                if rates is not None:
                    registry[(str(provider_name), str(model_name))] = rates
        elif isinstance(models, list):
            for model_entry in models:
                if not isinstance(model_entry, Mapping):
                    continue
                model_name = _first_present(model_entry, ("model", "name", "id"))
                if model_name is None:
                    continue
                rates = _coerce_model_cost_rates(model_entry)
                if rates is not None:
                    registry[(str(provider_name), str(model_name))] = rates
    return registry


def _model_cost_rates_from_registry(
    registry: Mapping[tuple[str, str], Any] | None,
    provider: str | None,
    model: str | None,
) -> ModelCostRates | None:
    if not registry or not provider or not model:
        return None
    return _coerce_model_cost_rates(registry.get((provider, model)))


def _estimate_cost_saved(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    primary_rate: ModelCostRates | tuple[float, float] | Mapping[str, Any] | None,
    fallback_rate: ModelCostRates | tuple[float, float] | Mapping[str, Any] | None,
) -> float | None:
    """Estimate fallback savings using separate input/output per-million rates."""
    primary_rates = _coerce_model_cost_rates(primary_rate)
    fallback_rates = _coerce_model_cost_rates(fallback_rate)
    if primary_rates is None or fallback_rates is None:
        return None

    prompt = _as_non_negative_int(prompt_tokens)
    completion = _as_non_negative_int(completion_tokens)
    primary_cost = (
        prompt * primary_rates.input_rate
        + completion * primary_rates.output_rate
    ) / TOKEN_RATE_SCALE
    fallback_cost = (
        prompt * fallback_rates.input_rate
        + completion * fallback_rates.output_rate
    ) / TOKEN_RATE_SCALE
    return primary_cost - fallback_cost


def _usage_details_as_dict(usage: dict[str, Any], details_key: str) -> dict[str, Any]:
    details = usage.get(details_key)
    if isinstance(details, dict):
        return details
    return {}


def _first_fallback_rule_for_gateway_model(config_loader: Any, gateway_model: str | None) -> Mapping[str, Any] | None:
    if not gateway_model:
        return None
    fallback_rules = getattr(config_loader, "fallback_rules", None)
    if not isinstance(fallback_rules, Mapping):
        return None
    model_config = fallback_rules.get(gateway_model)
    if not isinstance(model_config, Mapping):
        return None
    fallback_models = model_config.get("fallback_models")
    if not isinstance(fallback_models, list) or not fallback_models:
        return None
    first_rule = fallback_models[0]
    if isinstance(first_rule, Mapping):
        return first_rule
    return None


def _string_value(value: Any) -> str | None:
    return value if isinstance(value, str) and value else None


def _apply_rate_based_cost_saved(
    tokens_usage: dict[str, Any],
    request: Request,
    gateway_model: str | None,
) -> None:
    if tokens_usage.get("cost_saved"):
        return

    app_state = getattr(getattr(request, "app", None), "state", None)
    config_loader = getattr(app_state, "config_loader", None)
    registry = build_model_cost_rate_registry(getattr(config_loader, "providers_config", None))
    if not registry:
        return

    primary_rule = _first_fallback_rule_for_gateway_model(config_loader, gateway_model)
    if primary_rule is None:
        return

    primary_provider = _string_value(primary_rule.get("provider"))
    primary_model = _string_value(primary_rule.get("model"))
    fallback_provider = _string_value(tokens_usage.get("provider"))
    fallback_model = _string_value(tokens_usage.get("model"))
    estimated_cost_saved = _estimate_cost_saved(
        prompt_tokens=tokens_usage.get("prompt_tokens") or 0,
        completion_tokens=tokens_usage.get("completion_tokens") or 0,
        primary_rate=_model_cost_rates_from_registry(
            registry,
            primary_provider,
            primary_model,
        ),
        fallback_rate=_model_cost_rates_from_registry(
            registry,
            fallback_provider,
            fallback_model,
        ),
    )
    if estimated_cost_saved is not None:
        tokens_usage["cost_saved"] = estimated_cost_saved
    elif primary_provider and primary_model and fallback_provider and fallback_model:
        tokens_usage["cost_saved"] = None


def extract_tokens_usage(
    payload: Mapping[str, Any] | None,
    *,
    cost_rate_registry: Mapping[tuple[str, str], Any] | None = None,
    primary_provider: str | None = None,
    primary_model: str | None = None,
    fallback_provider: str | None = None,
    fallback_model: str | None = None,
) -> dict[str, Any]:
    tokens_usage = initialize_tokens_usage()
    if not isinstance(payload, Mapping):
        return tokens_usage

    try:
        usage = payload.get("usage")

        # If not found at top level, check inside 'message' (Anthropic format)
        if not usage and "message" in payload and isinstance(payload["message"], dict):
            usage = payload["message"].get("usage")
        if not usage and "response" in payload and isinstance(payload["response"], Mapping):
            usage = payload["response"].get("usage")

        if isinstance(usage, dict):
            prompt_tokens_details = _usage_details_as_dict(usage, "prompt_tokens_details")
            completion_tokens_details = _usage_details_as_dict(usage, "completion_tokens_details")

            # OpenAI format
            if "prompt_tokens" in usage:
                tokens_usage["prompt_tokens"] = _as_non_negative_int(usage["prompt_tokens"])
            if "completion_tokens" in usage:
                tokens_usage["completion_tokens"] = _as_non_negative_int(usage["completion_tokens"])
            if "total_tokens" in usage:
                tokens_usage["total_tokens"] = _as_non_negative_int(usage["total_tokens"])

            # Anthropic format (input_tokens/output_tokens) - use only if OpenAI fields not present.
            # Anthropic-compatible providers split prompt usage into uncached input,
            # cache creation, and cache read buckets; OpenAI's prompt_tokens should
            # represent the full prompt size while cached_tokens remains the cache-read subset.
            if "prompt_tokens" not in usage and any(
                key in usage
                for key in (
                    "input_tokens",
                    "cache_creation_input_tokens",
                    "cache_read_input_tokens",
                )
            ):
                input_tokens = _as_non_negative_int(usage.get("input_tokens"))
                cache_creation_input_tokens = _as_non_negative_int(
                    usage.get("cache_creation_input_tokens")
                )
                cache_read_input_tokens = _as_non_negative_int(
                    usage.get("cache_read_input_tokens")
                )
                tokens_usage["prompt_tokens"] = (
                    input_tokens + cache_creation_input_tokens + cache_read_input_tokens
                )
                if cache_read_input_tokens:
                    tokens_usage["cached_tokens"] = cache_read_input_tokens
            if "completion_tokens" not in usage and "output_tokens" in usage:
                tokens_usage["completion_tokens"] = _as_non_negative_int(usage["output_tokens"])

            # Recalculate total if not provided but input/output are present
            if "total_tokens" not in usage and (
                tokens_usage["prompt_tokens"] > 0 or tokens_usage["completion_tokens"] > 0
            ):
                tokens_usage["total_tokens"] = tokens_usage["prompt_tokens"] + tokens_usage["completion_tokens"]

            if "cost" in usage:
                cost_value = _as_non_negative_finite_float(usage["cost"])
                if cost_value is not None:
                    tokens_usage["cost"] = cost_value
            if "reasoning_tokens" in completion_tokens_details:
                tokens_usage["reasoning_tokens"] = _as_non_negative_int(
                    completion_tokens_details["reasoning_tokens"]
                )
            if "cached_tokens" in prompt_tokens_details:
                tokens_usage["cached_tokens"] = _as_non_negative_int(prompt_tokens_details["cached_tokens"])

            if "cache_read_input_tokens" in usage:
                tokens_usage["cached_tokens"] = _as_non_negative_int(usage["cache_read_input_tokens"])

            # Cost savings. Prefer an explicit upstream field when available;
            # otherwise estimate only when both model rates are known.
            cost_saved_candidate = usage.get("cost_saved")
            if cost_saved_candidate is None:
                cost_saved_candidate = usage.get("saved_cost")
            cost_saved_value = _as_non_negative_finite_float(cost_saved_candidate)
            if cost_saved_value is not None:
                tokens_usage["cost_saved"] = cost_saved_value
            else:
                payload_provider = payload.get("provider") if isinstance(payload.get("provider"), str) else None
                payload_model = payload.get("model") if isinstance(payload.get("model"), str) else None
                estimated_cost_saved = _estimate_cost_saved(
                    prompt_tokens=tokens_usage.get("prompt_tokens") or 0,
                    completion_tokens=tokens_usage.get("completion_tokens") or 0,
                    primary_rate=_model_cost_rates_from_registry(
                        cost_rate_registry,
                        primary_provider,
                        primary_model,
                    ),
                    fallback_rate=_model_cost_rates_from_registry(
                        cost_rate_registry,
                        fallback_provider or payload_provider,
                        fallback_model or payload_model,
                    ),
                )
                if estimated_cost_saved is not None:
                    tokens_usage["cost_saved"] = estimated_cost_saved
                elif cost_rate_registry is not None:
                    tokens_usage["cost_saved"] = None

        if "provider" in payload:
            tokens_usage["provider"] = payload["provider"]
        if "model" in payload:
            tokens_usage["model"] = payload["model"]
        if "request_id" in payload:
            tokens_usage["request_id"] = payload["request_id"]
    except Exception as exc:
        logger.error("Failed to extract token usage from payload: %s", exc, exc_info=True)

    return tokens_usage


def enrich_tokens_usage(
    tokens_usage: dict[str, Any],
    request: Request,
    *,
    gateway_model: str | None = None,
    operation: str | None = None,
) -> dict[str, Any]:
    enriched_tokens_usage = dict(tokens_usage) if isinstance(tokens_usage, dict) else initialize_tokens_usage()

    provider_name = getattr(request.state, "llmgateway_provider", None)
    provider_model = getattr(request.state, "llmgateway_provider_model", None)
    requested_gateway_model = gateway_model or getattr(request.state, "llmgateway_gateway_model", None)
    request_operation = operation or getattr(request.state, "llmgateway_operation", None)

    request_id = getattr(request.state, "llmgateway_request_id", None)
    api_key_id = getattr(request.state, "api_key_id", None)

    if provider_name:
        enriched_tokens_usage["provider"] = provider_name
    if provider_model:
        enriched_tokens_usage["model"] = provider_model
    if requested_gateway_model:
        enriched_tokens_usage["gateway_model"] = requested_gateway_model
    if request_operation:
        enriched_tokens_usage["operation"] = request_operation
    if request_id:
        enriched_tokens_usage["request_id"] = request_id
    if api_key_id is not None:
        enriched_tokens_usage["api_key_id"] = api_key_id

    _apply_rate_based_cost_saved(
        enriched_tokens_usage,
        request,
        requested_gateway_model,
    )

    return enriched_tokens_usage


# ---------------------------------------------------------------------------
# Tiktoken-based token estimation (fallback when provider returns 0)
# ---------------------------------------------------------------------------

@functools.lru_cache(maxsize=64)
def _resolve_encoding(model_name: str | None) -> TokensEstimator | None:
    """Resolve a tiktoken encoding for *model_name*, marking fallback estimates."""
    if model_name:
        try:
            return TokensEstimator(tiktoken.encoding_for_model(model_name))
        except KeyError:
            logger.warning("Unknown token encoding for model_hint=%r; using fallback estimator", model_name)
    else:
        logger.warning("Missing token encoding model_hint=%r; using fallback estimator", model_name)

    for name in TIKTOKEN_ENCODING_FALLBACKS:
        try:
            return TokensEstimator(tiktoken.get_encoding(name), is_fallback=True)
        except ValueError:
            continue
    return None


def _estimate_prompt_tokens_with_estimator(
    request_body_str: str,
    estimator: TokensEstimator | None,
) -> int:
    """Estimate prompt token count from the raw request body JSON string."""
    if estimator is None or not request_body_str:
        return 0
    try:
        payload = json.loads(request_body_str)
    except (json.JSONDecodeError, TypeError):
        return 0

    # Build a compact representation that includes only the token-bearing fields.
    token_fields: dict[str, Any] = {}
    if "messages" in payload:
        token_fields["messages"] = payload["messages"]
    for field in ("tools", "tool_choice", "system"):
        if field in payload:
            token_fields[field] = payload[field]
    if not token_fields:
        token_fields = payload

    serialized = json.dumps(token_fields, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return len(estimator.encode(serialized))


def estimate_prompt_tokens(request_body_str: str, model_name: str | None = None) -> int:
    """Estimate prompt token count from the raw request body JSON string."""
    return _estimate_prompt_tokens_with_estimator(request_body_str, _resolve_encoding(model_name))


def _estimate_token_count_with_estimator(text: str, estimator: TokensEstimator | None) -> int:
    if estimator is None or not text:
        return 0
    return len(estimator.encode(text))


def estimate_token_count(text: str, model_name: str | None = None) -> int:
    """Estimate token count for an arbitrary text string."""
    return _estimate_token_count_with_estimator(text, _resolve_encoding(model_name))


# Markers injected by ChunkProcessor when accumulating streamed text.
_REASONING_MARKER = "\n\n[Reasoning]:\n"
_RESPONSE_MARKER = "\n\n[Response]:\n"
_TOOL_USE_MARKER = "\n\n[Tool Use:"


def _split_response_text(response_text: str) -> tuple[str, str]:
    """Split accumulated response text into (reasoning_part, completion_part).

    The markers ``[Reasoning]:`` and ``[Response]:`` are inserted by
    ``ChunkProcessor`` during streaming.  This helper strips the
    markers themselves so that only the actual content is counted.

    Returns ``("", full_text)`` when no reasoning marker is present.
    """
    reasoning_pos = response_text.find("[Reasoning]:")
    if reasoning_pos == -1:
        return "", response_text

    # Everything before the [Reasoning]: marker is regular response text
    # (usually empty, but be defensive).
    before_reasoning = response_text[:reasoning_pos].strip()

    after_reasoning_marker = response_text[reasoning_pos + len("[Reasoning]:"):]

    response_pos = after_reasoning_marker.find("[Response]:")
    if response_pos == -1:
        # No [Response]: marker — everything after [Reasoning]: is reasoning.
        reasoning_text = after_reasoning_marker.strip()
        completion_text = before_reasoning
    else:
        reasoning_text = after_reasoning_marker[:response_pos].strip()
        after_response_marker = after_reasoning_marker[response_pos + len("[Response]:"):]
        completion_text = (before_reasoning + " " + after_response_marker).strip() if before_reasoning else after_response_marker.strip()

    # Strip tool use blocks from completion text — they are not regular text tokens.
    if _TOOL_USE_MARKER in completion_text:
        tool_pos = completion_text.find(_TOOL_USE_MARKER)
        completion_text = completion_text[:tool_pos].strip()

    return reasoning_text, completion_text


def backfill_zero_token_counts(
    tokens_usage: dict[str, Any],
    request_body_str: str,
    response_text: str,
) -> bool:
    """Fill in zero-valued token fields via tiktoken estimation.

    Parses ``[Reasoning]:`` / ``[Response]:`` markers in *response_text*
    to separately estimate ``reasoning_tokens`` and ``completion_tokens``.

    Mutates *tokens_usage* in place.  Returns ``True`` if any field was
    backfilled so callers can log the event.
    """
    model_name = tokens_usage.get("model") or tokens_usage.get("gateway_model")
    estimator: TokensEstimator | None = None
    estimator_resolved = False
    changed = False

    def get_estimator() -> TokensEstimator | None:
        nonlocal estimator, estimator_resolved
        if not estimator_resolved:
            estimator = _resolve_encoding(model_name)
            estimator_resolved = True
        return estimator

    if not tokens_usage.get("prompt_tokens") and request_body_str:
        estimated = _estimate_prompt_tokens_with_estimator(request_body_str, get_estimator())
        if estimated > 0:
            tokens_usage["prompt_tokens"] = estimated
            changed = True

    needs_completion = not tokens_usage.get("completion_tokens")
    needs_reasoning = not tokens_usage.get("reasoning_tokens")

    if (needs_completion or needs_reasoning) and response_text:
        reasoning_text, completion_text = _split_response_text(response_text)

        if needs_completion and completion_text:
            estimated = _estimate_token_count_with_estimator(completion_text, get_estimator())
            if estimated > 0:
                tokens_usage["completion_tokens"] = estimated
                changed = True

        if needs_reasoning and reasoning_text:
            estimated = _estimate_token_count_with_estimator(reasoning_text, get_estimator())
            if estimated > 0:
                tokens_usage["reasoning_tokens"] = estimated
                changed = True

    if changed:
        tokens_usage["total_tokens"] = (
            tokens_usage.get("prompt_tokens", 0) + tokens_usage.get("completion_tokens", 0)
        )
        tokens_usage["is_estimated"] = True
        if estimator is not None and estimator.is_fallback:
            tokens_usage["usage_source"] = FALLBACK_USAGE_SOURCE

    return changed
