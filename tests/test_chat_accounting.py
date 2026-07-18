import json
from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from llm_gateway_core.api.v1.chat_accounting import ChatStreamDialect
from llm_gateway_core.api.v1.chat_streaming import _DirectChatStreamObservationBuilder
from llm_gateway_core.middleware.accounting_admission import AccountingRequestContext
from llm_gateway_core.services.accounting import (
    AccountingCostError,
    AccountingEventKind,
    AccountingReservation,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
    BillingComponentKind,
    CostSource,
    classify_billing_policy,
)
from llm_gateway_core.services.chat_accounting import (
    ChatTerminalObservation,
    ObservedChatResponse,
    build_chat_accounting_event,
    build_direct_chat_terminal_observation,
)
from llm_gateway_core.services.stream_observation import SSEEvent
from llm_gateway_core.utils.usage_tracking import ModelCostRates


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _component(
    provider: str,
    model: str,
    *,
    prompt_tokens: int,
    completion_tokens: int,
    cost: float,
) -> BillingComponent:
    return BillingComponent(
        provider=provider,
        model=model,
        usage=AccountingUsage(
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=prompt_tokens + completion_tokens,
            cost=cost,
        ),
        cost_source=CostSource.UPSTREAM,
    )


def _context(
    route_template: str = "/v1/chat/completions",
    *,
    parent_event_id: str | None = None,
) -> AccountingRequestContext:
    policy = classify_billing_policy("POST", route_template)
    assert policy is not None
    return AccountingRequestContext(
        method="POST",
        route_template=route_template,
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
        parent_event_id=parent_event_id,
    )


def test_chat_terminal_observation_freezes_order_and_builds_component_sum_usage():
    selector = _component(
        "provider-a",
        "selector",
        prompt_tokens=2,
        completion_tokens=1,
        cost=0.01,
    )
    delegate = _component(
        "provider-b",
        "delegate",
        prompt_tokens=3,
        completion_tokens=4,
        cost=0.02,
    )

    observation = ChatTerminalObservation(
        top_provider="provider-b",
        top_model="delegate",
        components=[selector, delegate],
    )

    assert observation.components == (selector, delegate)
    assert observation.usage == AccountingUsage(
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
        cost=0.03,
    )
    with pytest.raises(FrozenInstanceError):
        observation.top_model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("top_provider", "top_model", "components"),
    [
        (
            "",
            "model",
            (_component("p", "m", prompt_tokens=1, completion_tokens=1, cost=0.0),),
        ),
        (
            "provider",
            " ",
            (_component("p", "m", prompt_tokens=1, completion_tokens=1, cost=0.0),),
        ),
        ("provider", "model", ()),
        ("provider", "model", (object(),)),
        (
            object(),
            "model",
            (_component("p", "m", prompt_tokens=1, completion_tokens=1, cost=0.0),),
        ),
    ],
)
def test_chat_terminal_observation_rejects_invalid_contract(
    top_provider: object,
    top_model: object,
    components: tuple[object, ...],
):
    with pytest.raises(AccountingValidationError):
        ChatTerminalObservation(
            top_provider=top_provider,  # type: ignore[arg-type]
            top_model=top_model,  # type: ignore[arg-type]
            components=components,  # type: ignore[arg-type]
        )


def test_chat_terminal_observation_requires_a_model_as_the_final_component():
    operation = BillingComponent(
        provider=None,
        model=None,
        usage=AccountingUsage(cost=0.1),
        cost_source=CostSource.OPERATION_DEFAULT,
        component_kind=BillingComponentKind.OPERATION,
        operation="web_search",
        gateway_model="gateway/search",
    )

    with pytest.raises(AccountingValidationError):
        ChatTerminalObservation(
            top_provider="provider",
            top_model="model",
            components=(operation,),
        )


def test_observed_chat_response_keeps_public_dict_separate_from_safe_observation_repr():
    component = _component(
        "provider-secret",
        "model-secret",
        prompt_tokens=1,
        completion_tokens=1,
        cost=0.0,
    )
    observation = ChatTerminalObservation(
        top_provider="provider-secret",
        top_model="model-secret",
        components=(component,),
    )
    response = {"choices": [], "secret": "response-secret"}

    observed = ObservedChatResponse(response=response, observation=observation)

    assert observed.response is response
    assert observed.observation is observation
    assert repr(observation) == "<ChatTerminalObservation>"
    assert repr(observed) == "<ObservedChatResponse>"
    assert "secret" not in repr((observation, observed))
    with pytest.raises(FrozenInstanceError):
        observed.observation = observation  # type: ignore[misc]


@pytest.mark.parametrize(
    ("response", "observation"),
    [
        ([], object()),
        ({}, object()),
    ],
)
def test_observed_chat_response_rejects_invalid_contract(response: object, observation: object):
    with pytest.raises(AccountingValidationError):
        ObservedChatResponse(
            response=response,  # type: ignore[arg-type]
            observation=observation,  # type: ignore[arg-type]
        )


def test_direct_chat_observation_uses_strict_token_component_policy():
    registry = MappingProxyType(
        {("provider", "model"): ModelCostRates(input_rate=1.0, output_rate=2.0)}
    )

    upstream = build_direct_chat_terminal_observation(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost": 0.0,
            }
        },
        provider="provider",
        model="model",
        cost_rate_registry=registry,
    )
    local = build_direct_chat_terminal_observation(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
            }
        },
        provider="provider",
        model="model",
        cost_rate_registry=registry,
    )

    assert upstream.usage.cost == 0.0
    assert upstream.components[0].cost_source is CostSource.UPSTREAM
    assert local.usage.cost == pytest.approx(0.000008)
    assert local.components[0].cost_source is CostSource.TOKEN_REGISTRY

    with pytest.raises(AccountingCostError):
        build_direct_chat_terminal_observation(
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "cost": "invalid",
                }
            },
            provider="provider",
            model="model",
            cost_rate_registry=registry,
        )


@pytest.mark.parametrize(
    "route_template",
    ["/v1/chat/completions", "/v1/messages", "/v1/responses"],
)
def test_chat_event_uses_context_identity_and_ordered_component_sum(
    route_template: str,
):
    first = _component(
        "provider-a",
        "panel",
        prompt_tokens=2,
        completion_tokens=1,
        cost=0.01,
    )
    final = _component(
        "provider-b",
        "main",
        prompt_tokens=3,
        completion_tokens=4,
        cost=0.02,
    )
    observation = ChatTerminalObservation(
        top_provider="provider-b",
        top_model="main",
        components=(first, final),
    )

    event = build_chat_accounting_event(
        _context(route_template),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
        duration_ms=17,
    )

    assert event.kind is AccountingEventKind.CHARGE
    assert event.event_id == "request-1"
    assert event.request_id == "request-1"
    assert event.parent_event_id is None
    assert event.api_key_id == 7
    assert event.method == "POST"
    assert event.route_template == route_template
    assert event.operation == "chat"
    assert event.gateway_model == "gateway-model"
    assert event.provider == "provider-b"
    assert event.model == "main"
    assert event.cost_source is CostSource.COMPONENT_SUM
    assert event.components == (first, final)
    assert event.usage == AccountingUsage(
        prompt_tokens=5,
        completion_tokens=5,
        total_tokens=10,
        cost=0.03,
        duration_ms=17,
    )


def test_chat_event_propagates_parent_and_none_preserves_fingerprint():
    component = _component(
        "provider",
        "model",
        prompt_tokens=1,
        completion_tokens=1,
        cost=0.01,
    )
    observation = ChatTerminalObservation(
        top_provider="provider",
        top_model="model",
        components=(component,),
    )

    omitted = build_chat_accounting_event(
        _context(),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
    )
    explicit_none = build_chat_accounting_event(
        _context(parent_event_id=None),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
    )
    child = build_chat_accounting_event(
        _context(parent_event_id="usage:v1:http:parent"),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
    )

    assert omitted.parent_event_id is explicit_none.parent_event_id is None
    assert omitted.billing_fingerprint == explicit_none.billing_fingerprint
    assert child.parent_event_id == "usage:v1:http:parent"
    assert child.billing_fingerprint != omitted.billing_fingerprint


def test_chat_event_rejects_non_chat_context():
    policy = classify_billing_policy("POST", "/v1/embeddings")
    assert policy is not None
    context = AccountingRequestContext(
        method="POST",
        route_template="/v1/embeddings",
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1.0,
        ),
    )

    with pytest.raises(AccountingValidationError):
        build_chat_accounting_event(
            context,
            ChatTerminalObservation(
                top_provider="provider",
                top_model="model",
                components=(
                    _component(
                        "provider",
                        "model",
                        prompt_tokens=1,
                        completion_tokens=1,
                        cost=0.0,
                    ),
                ),
            ),
            gateway_model="gateway-model",
            occurred_at=NOW,
        )


@pytest.mark.parametrize("invalid_ttft_ms", [-1, True])
def test_chat_terminal_observation_rejects_invalid_ttft_ms(invalid_ttft_ms: object):
    with pytest.raises(AccountingValidationError):
        ChatTerminalObservation(
            top_provider="provider",
            top_model="model",
            components=(
                _component(
                    "provider",
                    "model",
                    prompt_tokens=1,
                    completion_tokens=1,
                    cost=0.0,
                ),
            ),
            ttft_ms=invalid_ttft_ms,  # type: ignore[arg-type]
        )


def test_build_direct_chat_terminal_observation_defaults_ttft_ms_to_none():
    registry = MappingProxyType(
        {("provider", "model"): ModelCostRates(input_rate=1.0, output_rate=2.0)}
    )

    observation = build_direct_chat_terminal_observation(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 3,
                "total_tokens": 5,
                "cost": 0.0,
            }
        },
        provider="provider",
        model="model",
        cost_rate_registry=registry,
    )

    assert observation.ttft_ms is None

    event = build_chat_accounting_event(
        _context(),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
    )
    assert event.usage.ttft_ms is None


def test_direct_chat_stream_observation_builder_propagates_ttft_ms_to_usage():
    registry = MappingProxyType(
        {("provider", "model"): ModelCostRates(input_rate=1.0, output_rate=2.0)}
    )
    builder = _DirectChatStreamObservationBuilder(
        dialect=ChatStreamDialect.OPENAI,
        provider="provider",
        model="model",
        cost_rate_registry=registry,
        ttft_ms=123,
    )

    usage_event = SSEEvent(
        data=json.dumps(
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 3,
                    "total_tokens": 5,
                    "cost": 0.0,
                }
            }
        )
    )
    done_event = SSEEvent(data="[DONE]", done=True)

    assert builder.observe(usage_event) is None
    observation = builder.observe(done_event)

    assert observation is not None
    assert observation.ttft_ms == 123

    event = build_chat_accounting_event(
        _context(),
        observation,
        gateway_model="gateway-model",
        occurred_at=NOW,
    )
    assert event.usage.ttft_ms == 123
