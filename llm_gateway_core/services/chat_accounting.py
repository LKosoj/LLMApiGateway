"""Typed terminal accounting observations for composite chat services."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from ..middleware.accounting_admission import AccountingRequestContext
from ..utils.usage_tracking import ModelCostRates
from .accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingEvent,
    AccountingEventKind,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
    BillingComponentKind,
    BillingUnit,
    CostSource,
    build_component_sum_usage,
)
from .operation_accounting import build_token_model_component


@dataclass(frozen=True, slots=True, repr=False)
class ChatTerminalObservation:
    """One successful composite chat result and its ordered billing manifest."""

    top_provider: str
    top_model: str
    components: tuple[BillingComponent, ...]
    ttft_ms: int | None = None

    def __post_init__(self) -> None:
        try:
            components = tuple(self.components)
        except TypeError:
            raise AccountingValidationError from None
        if not components or any(not isinstance(component, BillingComponent) for component in components):
            raise AccountingValidationError from None
        final_component = components[-1]
        if (
            final_component.component_kind is not BillingComponentKind.MODEL
            or not isinstance(self.top_provider, str)
            or not isinstance(self.top_model, str)
            or self.top_provider != final_component.provider
            or self.top_model != final_component.model
        ):
            raise AccountingValidationError from None
        if self.ttft_ms is not None:
            if isinstance(self.ttft_ms, bool) or not isinstance(self.ttft_ms, int) or self.ttft_ms < 0:
                raise AccountingValidationError from None
        object.__setattr__(self, "top_provider", final_component.provider)
        object.__setattr__(self, "top_model", final_component.model)
        object.__setattr__(self, "components", components)

    @property
    def usage(self) -> AccountingUsage:
        return build_component_sum_usage(self.components)

    def __repr__(self) -> str:
        return "<ChatTerminalObservation>"


@dataclass(frozen=True, slots=True, repr=False)
class ObservedChatResponse:
    """Internal pairing of a public response dict and trusted observation."""

    response: dict[str, Any]
    observation: ChatTerminalObservation

    def __post_init__(self) -> None:
        if not isinstance(self.response, dict) or not isinstance(
            self.observation,
            ChatTerminalObservation,
        ):
            raise AccountingValidationError from None

    def __repr__(self) -> str:
        return "<ObservedChatResponse>"


def build_direct_chat_terminal_observation(
    payload: Mapping[str, object],
    *,
    provider: str,
    model: str,
    cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
    duration_ms: int | None = None,
    ttft_ms: int | None = None,
) -> ChatTerminalObservation:
    """Build one strict terminal observation from an upstream chat response."""
    component = build_token_model_component(
        payload,
        provider=provider,
        model=model,
        cost_rate_registry=cost_rate_registry,
        duration_ms=duration_ms,
    )
    return ChatTerminalObservation(
        top_provider=component.provider,
        top_model=component.model,
        components=(component,),
        ttft_ms=ttft_ms,
    )


def build_chat_accounting_event(
    context: AccountingRequestContext,
    observation: ChatTerminalObservation,
    *,
    gateway_model: str,
    occurred_at: datetime,
    duration_ms: int | None = None,
) -> AccountingEvent:
    """Build the canonical charge event for one successful chat response."""
    if (
        not isinstance(context, AccountingRequestContext)
        or context.policy.operation != "chat"
        or context.policy.unit is not BillingUnit.TOKEN
        or not isinstance(observation, ChatTerminalObservation)
    ):
        raise AccountingValidationError
    usage = build_component_sum_usage(
        observation.components,
        duration_ms=duration_ms,
        ttft_ms=observation.ttft_ms,
    )
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=context.request_id,
        kind=AccountingEventKind.CHARGE,
        api_key_id=context.reservation.api_key_id,
        method=context.method,
        route_template=context.route_template,
        operation=context.policy.operation,
        gateway_model=gateway_model,
        provider=observation.top_provider,
        model=observation.top_model,
        usage=usage,
        cost_source=CostSource.COMPONENT_SUM,
        occurred_at=occurred_at,
        request_id=context.request_id,
        parent_event_id=context.parent_event_id,
        components=observation.components,
    )
