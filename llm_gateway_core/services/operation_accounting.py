"""Strict terminal observations for non-chat accounting owners."""

from __future__ import annotations

import logging
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from ..middleware.accounting_admission import AccountingRequestContext
from ..utils.usage_tracking import ModelCostRates, calculate_model_cost_usd
from .accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingCostError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
    BillingComponentKind,
    BillingPolicy,
    BillingUnit,
    CostSource,
    OperationCostCalculator,
    classify_billing_policy,
    resolve_operation_cost,
)

logger = logging.getLogger(__name__)

_INPUT_ONLY_OPERATIONS = frozenset({"embeddings", "rerank"})
_INT64_MAX = (1 << 63) - 1
_TOKEN_USAGE_KEYS = frozenset(
    {
        "prompt_tokens",
        "input_tokens",
        "cache_creation_input_tokens",
        "cache_read_input_tokens",
        "completion_tokens",
        "output_tokens",
        "total_tokens",
    }
)


@dataclass(frozen=True, slots=True, repr=False)
class OperationTerminalObservation:
    """Trusted terminal result before reservation ownership is committed."""

    policy: BillingPolicy
    gateway_model: str
    provider: str | None
    model: str | None
    usage: AccountingUsage
    cost_source: CostSource
    occurred_at: datetime

    def __post_init__(self) -> None:
        if not isinstance(self.policy, BillingPolicy):
            raise AccountingValidationError
        if not isinstance(self.usage, AccountingUsage):
            raise AccountingValidationError
        if not isinstance(self.cost_source, CostSource):
            raise AccountingValidationError
        if (
            not isinstance(self.gateway_model, str)
            or not self.gateway_model
            or self.gateway_model != self.gateway_model.strip()
            or any(character.isspace() for character in self.gateway_model)
            or "\x00" in self.gateway_model
            or len(self.gateway_model.encode("utf-8")) > 255
        ):
            raise AccountingValidationError
        if (
            not isinstance(self.occurred_at, datetime)
            or self.occurred_at.tzinfo is None
            or self.occurred_at.utcoffset() is None
        ):
            raise AccountingValidationError
        object.__setattr__(self, "occurred_at", self.occurred_at.astimezone(timezone.utc))

        if self.policy.unit is BillingUnit.TOKEN:
            if self.usage.total_tokens == 0:
                raise AccountingValidationError
            BillingComponent(
                provider=self.provider,
                model=self.model,
                usage=self.usage,
                cost_source=self.cost_source,
            )
            if self.cost_source not in {CostSource.UPSTREAM, CostSource.TOKEN_REGISTRY}:
                raise AccountingValidationError
            return

        BillingComponent(
            provider=None,
            model=None,
            usage=self.usage,
            cost_source=self.cost_source,
            component_kind=BillingComponentKind.OPERATION,
            operation=self.policy.operation,
            gateway_model=self.gateway_model,
        )
        if self.provider is not None or self.model is not None:
            raise AccountingValidationError

    def __repr__(self) -> str:
        return "<OperationTerminalObservation>"


@dataclass(frozen=True, slots=True, repr=False)
class OperationEventProvenance:
    """Stable event identity that may differ from the admitting HTTP request."""

    event_id: str
    method: str
    route_template: str
    provider: str | None = None
    model: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.event_id, str)
            or not self.event_id
            or self.event_id != self.event_id.strip()
            or len(self.event_id.encode("ascii", errors="ignore")) != len(self.event_id)
            or len(self.event_id.encode("ascii")) > 255
            or any(not 0x20 <= ord(character) <= 0x7E for character in self.event_id)
            or not isinstance(self.method, str)
            or not self.method
            or self.method != self.method.upper()
            or not isinstance(self.route_template, str)
            or not self.route_template.startswith("/")
        ):
            raise AccountingValidationError
        if (self.provider is None) != (self.model is None):
            raise AccountingValidationError
        for value in (self.provider, self.model):
            if value is None:
                continue
            try:
                encoded = value.encode("utf-8")
            except UnicodeEncodeError:
                raise AccountingValidationError from None
            if (
                not value
                or value != value.strip()
                or "\x00" in value
                or len(encoded) > 512
            ):
                raise AccountingValidationError

    def __repr__(self) -> str:
        return "<OperationEventProvenance>"


def _strict_token(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise AccountingValidationError
    if value < 0 or value > _INT64_MAX:
        raise AccountingValidationError
    return value


def _consistent_alias(
    usage: Mapping[str, object],
    keys: tuple[str, ...],
) -> tuple[bool, int]:
    values = [_strict_token(usage[key]) for key in keys if key in usage]
    if not values:
        return False, 0
    if any(value != values[0] for value in values[1:]):
        raise AccountingValidationError
    return True, values[0]


def _details(
    usage: Mapping[str, object],
    key: str,
) -> Mapping[str, object]:
    value = usage.get(key)
    if value is None and key not in usage:
        return {}
    if not isinstance(value, Mapping):
        raise AccountingValidationError
    return value


def _usage_mapping(payload: Mapping[str, object]) -> Mapping[str, object] | None:
    if "usage" in payload:
        usage = payload["usage"]
        if not isinstance(usage, Mapping):
            raise AccountingValidationError
        return usage
    for container_key in ("message", "response"):
        if container_key not in payload:
            continue
        container = payload[container_key]
        if not isinstance(container, Mapping):
            raise AccountingValidationError
        if "usage" not in container:
            continue
        usage = container["usage"]
        if not isinstance(usage, Mapping):
            raise AccountingValidationError
        return usage
    return None


def _strict_token_usage(
    payload: Mapping[str, object],
    *,
    input_only: bool,
) -> tuple[Mapping[str, object], int, int, int, int, int]:
    usage = _usage_mapping(payload)
    if usage is None:
        raise AccountingValidationError

    prompt_present, canonical_prompt = _consistent_alias(usage, ("prompt_tokens",))
    input_present, input_tokens = _consistent_alias(usage, ("input_tokens",))
    cache_creation_present, cache_creation = _consistent_alias(
        usage,
        ("cache_creation_input_tokens",),
    )
    cache_read_present, cache_read = _consistent_alias(
        usage,
        ("cache_read_input_tokens",),
    )
    aliased_prompt_present = input_present or cache_creation_present or cache_read_present
    aliased_prompt = input_tokens + cache_creation + cache_read
    if prompt_present and aliased_prompt_present and canonical_prompt != aliased_prompt:
        raise AccountingValidationError
    prompt = canonical_prompt if prompt_present else aliased_prompt

    completion_present, canonical_completion = _consistent_alias(
        usage,
        ("completion_tokens",),
    )
    output_present, output_tokens = _consistent_alias(usage, ("output_tokens",))
    if completion_present and output_present and canonical_completion != output_tokens:
        raise AccountingValidationError
    completion = canonical_completion if completion_present else output_tokens

    total_present, total = _consistent_alias(usage, ("total_tokens",))
    if input_only:
        if not prompt_present and not aliased_prompt_present:
            if not total_present:
                raise AccountingValidationError
            prompt = total
        if not completion_present and not output_present:
            completion = 0
    elif (
        not (prompt_present or aliased_prompt_present)
        or not (completion_present or output_present)
    ):
        raise AccountingValidationError

    derived_total = prompt + completion
    if derived_total > _INT64_MAX:
        raise AccountingValidationError
    if total_present and total != derived_total:
        logger.warning(
            "Upstream total_tokens=%s does not match prompt_tokens+completion_tokens=%s; "
            "accepting the response and using the derived total.",
            total,
            derived_total,
        )
    prompt_details = _details(usage, "prompt_tokens_details")
    completion_details = _details(usage, "completion_tokens_details")
    cached_candidates: list[int] = []
    for mapping, key in (
        (usage, "cached_tokens"),
        (prompt_details, "cached_tokens"),
    ):
        if key in mapping:
            cached_candidates.append(_strict_token(mapping[key]))
    if cache_read_present:
        cached_candidates.append(cache_read)
    if cached_candidates and any(value != cached_candidates[0] for value in cached_candidates[1:]):
        raise AccountingValidationError
    cached_tokens = cached_candidates[0] if cached_candidates else 0

    reasoning_candidates: list[int] = []
    for mapping, key in (
        (usage, "reasoning_tokens"),
        (completion_details, "reasoning_tokens"),
    ):
        if key in mapping:
            reasoning_candidates.append(_strict_token(mapping[key]))
    if reasoning_candidates and any(
        value != reasoning_candidates[0] for value in reasoning_candidates[1:]
    ):
        raise AccountingValidationError
    reasoning_tokens = reasoning_candidates[0] if reasoning_candidates else 0
    return (
        usage,
        prompt,
        completion,
        derived_total,
        reasoning_tokens,
        cached_tokens,
    )


def _strict_upstream_cost(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccountingCostError
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise AccountingCostError from None
    if not math.isfinite(normalized) or normalized < 0:
        raise AccountingCostError
    return 0.0 if normalized == 0.0 else normalized


def _strict_diagnostics(
    usage: Mapping[str, object] | None,
    *,
    duration_ms: int | None = None,
) -> tuple[float, int | None, bool]:
    usage = usage or {}
    cost_saved = 0.0
    if "cost_saved" in usage:
        value = usage["cost_saved"]
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise AccountingValidationError
        cost_saved = float(value)
        if not math.isfinite(cost_saved):
            raise AccountingValidationError
        if cost_saved == 0.0:
            cost_saved = 0.0

    payload_duration: int | None = None
    if "duration_ms" in usage:
        payload_duration = _strict_token(usage["duration_ms"])
    if duration_ms is not None:
        normalized_duration = _strict_token(duration_ms)
        if payload_duration is not None and payload_duration != normalized_duration:
            raise AccountingValidationError
        payload_duration = normalized_duration

    is_estimated = usage.get("is_estimated", False)
    if not isinstance(is_estimated, bool):
        raise AccountingValidationError
    return cost_saved, payload_duration, is_estimated


def _registry_cost(
    *,
    prompt_tokens: int,
    completion_tokens: int,
    provider: str,
    model: str,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
) -> float | None:
    """Return the registry-derived cost, or ``None`` if no price is configured.

    A missing/invalid price must not turn an already-successful upstream call
    into a 5xx: the caller degrades this to a ``cost_unavailable`` usage row
    instead of raising.
    """
    rates = cost_rate_registry.get((provider, model))
    if not isinstance(rates, ModelCostRates):
        logger.warning(
            "No cost rate configured for provider=%s model=%s; recording cost_unavailable usage.",
            provider,
            model,
        )
        return None
    for rate in (rates.input_rate, rates.output_rate):
        if (
            isinstance(rate, bool)
            or not isinstance(rate, (int, float))
            or not math.isfinite(float(rate))
            or rate < 0
        ):
            logger.warning(
                "Invalid cost rate configured for provider=%s model=%s; recording cost_unavailable usage.",
                provider,
                model,
            )
            return None
    cost = calculate_model_cost_usd(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        rates=rates,
    )
    if cost is None or not math.isfinite(cost) or cost < 0:
        logger.warning(
            "Cost calculation failed for provider=%s model=%s; recording cost_unavailable usage.",
            provider,
            model,
        )
        return None
    return cost


def _build_token_model_component(
    payload: Mapping[str, object],
    *,
    provider: str,
    model: str,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    duration_ms: int | None,
    input_only: bool,
    accept_upstream_cost: bool,
) -> BillingComponent:
    if not isinstance(payload, Mapping) or not isinstance(cost_rate_registry, Mapping):
        raise AccountingValidationError
    candidate_usage = _usage_mapping(payload)
    if candidate_usage is None or not (_TOKEN_USAGE_KEYS & candidate_usage.keys()):
        if not (
            accept_upstream_cost
            and candidate_usage is not None
            and "cost" in candidate_usage
        ):
            raise AccountingCostError(AccountingErrorCode.COST_UNAVAILABLE)
    usage_payload, prompt, completion, total, reasoning, cached = _strict_token_usage(
        payload,
        input_only=input_only,
    )
    if total == 0:
        if accept_upstream_cost and "cost" in usage_payload:
            raise AccountingValidationError
        raise AccountingCostError(AccountingErrorCode.COST_UNAVAILABLE)
    cost_unavailable = False
    if accept_upstream_cost and "cost" in usage_payload:
        cost = _strict_upstream_cost(usage_payload["cost"])
        cost_source = CostSource.UPSTREAM
    else:
        registry_cost = _registry_cost(
            prompt_tokens=prompt,
            completion_tokens=completion,
            provider=provider,
            model=model,
            cost_rate_registry=cost_rate_registry,
        )
        cost_unavailable = registry_cost is None
        cost = 0.0 if registry_cost is None else registry_cost
        cost_source = CostSource.TOKEN_REGISTRY
    cost_saved, normalized_duration_ms, is_estimated = _strict_diagnostics(
        usage_payload,
        duration_ms=duration_ms,
    )
    return BillingComponent(
        provider=provider,
        model=model,
        usage=AccountingUsage(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=total,
            reasoning_tokens=reasoning,
            cached_tokens=cached,
            cost=cost,
            cost_unavailable=cost_unavailable,
            cost_saved=cost_saved,
            duration_ms=normalized_duration_ms,
            is_estimated=is_estimated,
        ),
        cost_source=cost_source,
    )


def build_token_model_component(
    payload: Mapping[str, object],
    *,
    provider: str,
    model: str,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    duration_ms: int | None = None,
    input_only: bool = False,
) -> BillingComponent:
    """Build one strict model component from an actual upstream response."""
    if not isinstance(input_only, bool):
        raise AccountingValidationError
    return _build_token_model_component(
        payload,
        provider=provider,
        model=model,
        cost_rate_registry=cost_rate_registry,
        duration_ms=duration_ms,
        input_only=input_only,
        accept_upstream_cost=True,
    )


def _parse_operation_observation(
    payload: Mapping[str, object],
    *,
    policy: BillingPolicy,
    gateway_model: str,
    provider: str | None,
    model: str | None,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    operation_cost_calculator_registry: Mapping[
        tuple[str, str], OperationCostCalculator
    ],
    occurred_at: datetime,
    duration_ms: int | None,
    accept_upstream_cost: bool,
) -> OperationTerminalObservation:
    if (
        not isinstance(payload, Mapping)
        or not isinstance(policy, BillingPolicy)
        or not isinstance(cost_rate_registry, Mapping)
        or not isinstance(operation_cost_calculator_registry, Mapping)
    ):
        raise AccountingValidationError

    if policy.unit is BillingUnit.TOKEN:
        if provider is None or model is None:
            raise AccountingValidationError
        component = _build_token_model_component(
            payload,
            provider=provider,
            model=model,
            cost_rate_registry=cost_rate_registry,
            duration_ms=duration_ms,
            input_only=policy.operation in _INPUT_ONLY_OPERATIONS,
            accept_upstream_cost=accept_upstream_cost,
        )
        return OperationTerminalObservation(
            policy=policy,
            gateway_model=gateway_model,
            provider=component.provider,
            model=component.model,
            usage=component.usage,
            cost_source=component.cost_source,
            occurred_at=occurred_at,
        )

    usage = _usage_mapping(payload)
    upstream_cost_present = bool(
        accept_upstream_cost and usage is not None and "cost" in usage
    )
    upstream_cost = usage["cost"] if upstream_cost_present and usage is not None else None
    resolved = resolve_operation_cost(
        upstream_cost_present=upstream_cost_present,
        upstream_cost=upstream_cost,
        calculator=operation_cost_calculator_registry.get(
            (policy.operation, gateway_model)
        ),
    )
    cost_saved, normalized_duration_ms, is_estimated = _strict_diagnostics(
        usage,
        duration_ms=duration_ms,
    )
    return OperationTerminalObservation(
        policy=policy,
        gateway_model=gateway_model,
        provider=None,
        model=None,
        usage=AccountingUsage(
            cost=resolved.cost_usd,
            cost_saved=cost_saved,
            duration_ms=normalized_duration_ms,
            is_estimated=is_estimated,
        ),
        cost_source=resolved.source,
        occurred_at=occurred_at,
    )


def parse_upstream_operation_observation(
    payload: Mapping[str, object],
    *,
    policy: BillingPolicy,
    gateway_model: str,
    provider: str | None,
    model: str | None,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    operation_cost_calculator_registry: Mapping[
        tuple[str, str], OperationCostCalculator
    ],
    occurred_at: datetime,
    duration_ms: int | None = None,
) -> OperationTerminalObservation:
    return _parse_operation_observation(
        payload,
        policy=policy,
        gateway_model=gateway_model,
        provider=provider,
        model=model,
        cost_rate_registry=cost_rate_registry,
        operation_cost_calculator_registry=operation_cost_calculator_registry,
        occurred_at=occurred_at,
        duration_ms=duration_ms,
        accept_upstream_cost=True,
    )


def parse_synthesized_operation_observation(
    payload: Mapping[str, object],
    *,
    policy: BillingPolicy,
    gateway_model: str,
    provider: str | None,
    model: str | None,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    operation_cost_calculator_registry: Mapping[
        tuple[str, str], OperationCostCalculator
    ],
    occurred_at: datetime,
    duration_ms: int | None = None,
) -> OperationTerminalObservation:
    """Parse gateway-generated aggregate usage without trusting its cost field."""
    return _parse_operation_observation(
        payload,
        policy=policy,
        gateway_model=gateway_model,
        provider=provider,
        model=model,
        cost_rate_registry=cost_rate_registry,
        operation_cost_calculator_registry=operation_cost_calculator_registry,
        occurred_at=occurred_at,
        duration_ms=duration_ms,
        accept_upstream_cost=False,
    )


def build_operation_accounting_event(
    context: AccountingRequestContext,
    observation: OperationTerminalObservation,
    *,
    provenance: OperationEventProvenance | None = None,
) -> AccountingEvent:
    if (
        not isinstance(context, AccountingRequestContext)
        or not isinstance(observation, OperationTerminalObservation)
        or observation.policy != context.policy
    ):
        raise AccountingValidationError
    if provenance is None:
        event_id = context.request_id
        method = context.method
        route_template = context.route_template
        provider = observation.provider
        model = observation.model
    else:
        if not isinstance(provenance, OperationEventProvenance):
            raise AccountingValidationError
        if classify_billing_policy(provenance.method, provenance.route_template) != context.policy:
            raise AccountingValidationError
        if provenance.provider is not None and context.policy.unit is not BillingUnit.OPERATION:
            raise AccountingValidationError
        event_id = provenance.event_id
        method = provenance.method
        route_template = provenance.route_template
        provider = provenance.provider or observation.provider
        model = provenance.model or observation.model

    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=event_id,
        kind=AccountingEventKind.CHARGE,
        api_key_id=context.reservation.api_key_id,
        method=method,
        route_template=route_template,
        operation=context.policy.operation,
        gateway_model=observation.gateway_model,
        provider=provider,
        model=model,
        usage=observation.usage,
        cost_source=observation.cost_source,
        occurred_at=observation.occurred_at,
        request_id=context.reservation.request_id,
        parent_event_id=context.parent_event_id,
    )
