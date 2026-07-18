from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import datetime, timezone
from types import MappingProxyType

import pytest

from llm_gateway_core.middleware.accounting_admission import AccountingRequestContext
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    DEFAULT_OPERATION_COST_USD,
    AccountingCostError,
    AccountingErrorCode,
    AccountingReservation,
    AccountingUsage,
    AccountingValidationError,
    BillingComponentKind,
    CostSource,
    OperationCostCalculator,
    classify_billing_policy,
)
from llm_gateway_core.services.operation_accounting import (
    OperationEventProvenance,
    OperationTerminalObservation,
    build_operation_accounting_event,
    build_token_model_component,
    parse_synthesized_operation_observation,
    parse_upstream_operation_observation,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)
TOKEN_RATES = MappingProxyType(
    {("trusted-provider", "trusted-model"): ModelCostRates(2.0, 4.0)}
)


def _policy(method: str, route: str):
    policy = classify_billing_policy(method, route)
    assert policy is not None
    return policy


def _parse_token(payload: object) -> OperationTerminalObservation:
    return parse_upstream_operation_observation(
        payload,
        policy=_policy("POST", "/v1/embeddings"),
        gateway_model="gateway-model",
        provider="trusted-provider",
        model="trusted-model",
        cost_rate_registry=TOKEN_RATES,
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )


def _parse_flat(
    payload: object,
    *,
    calculators: MappingProxyType = MappingProxyType({}),
) -> OperationTerminalObservation:
    return parse_upstream_operation_observation(
        payload,
        policy=_policy("POST", "/v1/images/generations"),
        gateway_model="image-model",
        provider="trusted-provider",
        model="trusted-model",
        cost_rate_registry=TOKEN_RATES,
        operation_cost_calculator_registry=calculators,
        occurred_at=NOW,
    )


def test_token_observation_is_frozen_safe_repr_and_uses_trusted_identity() -> None:
    observation = _parse_token(
        {
            "provider": "spoofed-provider",
            "model": "spoofed-model",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 2,
                "total_tokens": 5,
                "cost": 0,
            },
        }
    )

    assert repr(observation) == "<OperationTerminalObservation>"
    assert observation.provider == "trusted-provider"
    assert observation.model == "trusted-model"
    assert observation.usage.cost == 0.0
    assert observation.cost_source is CostSource.UPSTREAM
    with pytest.raises(FrozenInstanceError):
        observation.gateway_model = "changed"  # type: ignore[misc]


@pytest.mark.parametrize("gateway_model", ["bad\x00model", "м" * 128])
def test_token_observation_rejects_gateway_model_outside_event_boundary(
    gateway_model: str,
) -> None:
    with pytest.raises(AccountingValidationError):
        parse_upstream_operation_observation(
            {"usage": {"prompt_tokens": 3, "completion_tokens": 0, "cost": 0}},
            policy=_policy("POST", "/v1/embeddings"),
            gateway_model=gateway_model,
            provider="trusted-provider",
            model="trusted-model",
            cost_rate_registry=TOKEN_RATES,
            operation_cost_calculator_registry=MappingProxyType({}),
            occurred_at=NOW,
        )


def test_direct_token_observation_requires_actual_usage() -> None:
    with pytest.raises(AccountingValidationError):
        OperationTerminalObservation(
            policy=_policy("POST", "/v1/embeddings"),
            gateway_model="gateway-model",
            provider="trusted-provider",
            model="trusted-model",
            usage=AccountingUsage(cost=0),
            cost_source=CostSource.UPSTREAM,
            occurred_at=NOW,
        )


@pytest.mark.parametrize(
    ("usage", "expected"),
    [
        (
            {"input_tokens": 3, "output_tokens": 2},
            (3, 2, 5, 0),
        ),
        (
            {"total_tokens": 3},
            (3, 0, 3, 0),
        ),
        (
            {
                "input_tokens": 3,
                "cache_creation_input_tokens": 4,
                "cache_read_input_tokens": 5,
                "output_tokens": 2,
                "total_tokens": 14,
            },
            (12, 2, 14, 5),
        ),
        (
            {
                "prompt_tokens": 3,
                "input_tokens": 3,
                "completion_tokens": 2,
                "output_tokens": 2,
                "total_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 1},
                "completion_tokens_details": {"reasoning_tokens": 1},
            },
            (3, 2, 5, 1),
        ),
        (
            {
                "prompt_tokens": 3,
                "completion_tokens": 0,
                "total_tokens": 3,
                "prompt_tokens_details": None,
                "completion_tokens_details": None,
            },
            (3, 0, 3, 0),
        ),
    ],
)
def test_token_aliases_and_missing_total_are_strictly_normalized(
    usage: dict[str, object],
    expected: tuple[int, int, int, int],
) -> None:
    observation = _parse_token({"usage": usage})

    assert (
        observation.usage.prompt_tokens,
        observation.usage.completion_tokens,
        observation.usage.total_tokens,
        observation.usage.cached_tokens,
    ) == expected
    assert observation.usage.cost == pytest.approx(
        (expected[0] * 2.0 + expected[1] * 4.0) / 1_000_000
    )
    assert observation.cost_source is CostSource.TOKEN_REGISTRY


@pytest.mark.parametrize(
    "usage",
    [
        {"prompt_tokens": 2, "input_tokens": 3, "completion_tokens": 1},
        {"prompt_tokens": True, "completion_tokens": 1},
        {"prompt_tokens": "2", "completion_tokens": 1},
        {"prompt_tokens": -1, "completion_tokens": 1},
    ],
)
def test_invalid_or_missing_actual_token_buckets_fail_closed(
    usage: dict[str, object],
) -> None:
    with pytest.raises(AccountingValidationError):
        _parse_token({"usage": usage})


def test_total_tokens_mismatch_is_tolerated_using_derived_total() -> None:
    """A ``total_tokens`` that disagrees with prompt+completion is no longer
    fatal: the upstream call already succeeded, so the response is accepted
    and the derived total (prompt+completion) is used instead of raising."""
    observation = _parse_token(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 4}}
    )

    assert observation.usage.prompt_tokens == 2
    assert observation.usage.completion_tokens == 1
    assert observation.usage.total_tokens == 3
    assert observation.usage.cost == pytest.approx((2 * 2.0 + 1 * 4.0) / 1_000_000)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"usage": {}},
        {"usage": {"prompt_tokens": 0, "completion_tokens": 0}},
    ],
)
def test_missing_cost_without_actual_token_usage_is_cost_unavailable(
    payload: dict[str, object],
) -> None:
    with pytest.raises(AccountingCostError) as exc_info:
        _parse_token(payload)

    assert exc_info.value.code is AccountingErrorCode.COST_UNAVAILABLE


def test_explicit_cost_does_not_permit_missing_token_usage_for_tpm() -> None:
    with pytest.raises(AccountingValidationError):
        _parse_token({"usage": {"cost": 0}})


@pytest.mark.parametrize(
    "invalid_cost",
    [True, "0", -1, float("nan"), float("inf")],
)
def test_present_invalid_token_cost_is_not_replaced_by_registry(
    invalid_cost: object,
) -> None:
    with pytest.raises(AccountingCostError) as exc_info:
        _parse_token(
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 1,
                    "cost": invalid_cost,
                }
            }
        )

    assert exc_info.value.code is AccountingErrorCode.INVALID_UPSTREAM_COST


def test_null_token_cost_uses_registry_price() -> None:
    observation = _parse_token(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": None,
            }
        }
    )

    assert observation.usage.cost == pytest.approx((2 * 2.0 + 1 * 4.0) / 1_000_000)
    assert observation.cost_source is CostSource.TOKEN_REGISTRY


def test_missing_token_rate_uses_default_cost() -> None:
    component = build_token_model_component(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 1}},
        provider="trusted-provider",
        model="missing-model",
        cost_rate_registry=MappingProxyType({}),
    )

    assert component.usage.cost == DEFAULT_OPERATION_COST_USD
    assert component.usage.cost_unavailable is False
    assert component.cost_source is CostSource.OPERATION_DEFAULT


def test_generic_token_builder_rejects_total_only_usage() -> None:
    with pytest.raises(AccountingValidationError):
        build_token_model_component(
            {"usage": {"total_tokens": 3}},
            provider="trusted-provider",
            model="trusted-model",
            cost_rate_registry=TOKEN_RATES,
        )


def test_reusable_token_component_has_model_identity_and_duration() -> None:
    component = build_token_model_component(
        {
            "usage": {
                "input_tokens": 2,
                "output_tokens": 1,
                "cost": 0.25,
                "cost_saved": -0.05,
                "duration_ms": 9,
                "is_estimated": True,
            }
        },
        provider="trusted-provider",
        model="trusted-model",
        cost_rate_registry=TOKEN_RATES,
        duration_ms=9,
    )

    assert component.component_kind is BillingComponentKind.MODEL
    assert component.provider == "trusted-provider"
    assert component.model == "trusted-model"
    assert component.usage.cost == 0.25
    assert component.usage.cost_saved == -0.05
    assert component.usage.duration_ms == 9
    assert component.usage.is_estimated is True
    assert component.cost_source is CostSource.UPSTREAM


@pytest.mark.parametrize(
    "diagnostics",
    [
        {"cost_saved": True},
        {"cost_saved": float("nan")},
        {"duration_ms": True},
        {"duration_ms": -1},
        {"is_estimated": 1},
    ],
)
def test_invalid_token_diagnostics_fail_closed(
    diagnostics: dict[str, object],
) -> None:
    usage = {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "cost": 0,
        **diagnostics,
    }
    with pytest.raises(AccountingValidationError):
        _parse_token({"usage": usage})


def test_flat_cost_precedence_is_upstream_then_configured_then_default() -> None:
    calculators = MappingProxyType(
        {
            ("images_generation", "image-model"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.35,
            )
        }
    )

    upstream = _parse_flat({"usage": {"cost": 0}}, calculators=calculators)
    configured = _parse_flat({"usage": {}}, calculators=calculators)
    defaulted = _parse_flat({})

    assert (upstream.usage.cost, upstream.cost_source) == (0.0, CostSource.UPSTREAM)
    assert (configured.usage.cost, configured.cost_source) == (
        0.35,
        CostSource.OPERATION_CONFIGURED,
    )
    assert (defaulted.usage.cost, defaulted.cost_source) == (
        DEFAULT_OPERATION_COST_USD,
        CostSource.OPERATION_DEFAULT,
    )
    assert upstream.provider is None
    assert upstream.model is None


def test_null_flat_cost_uses_configured_then_default_cost() -> None:
    calculators = MappingProxyType(
        {
            ("images_generation", "image-model"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.35,
            )
        }
    )
    configured = _parse_flat({"usage": {"cost": None}}, calculators=calculators)
    defaulted = _parse_flat({"usage": {"cost": None}})

    assert configured.usage.cost == 0.35
    assert configured.cost_source is CostSource.OPERATION_CONFIGURED
    assert defaulted.usage.cost == DEFAULT_OPERATION_COST_USD
    assert defaulted.cost_source is CostSource.OPERATION_DEFAULT


def test_null_optional_usage_fields_are_treated_as_absent() -> None:
    observation = _parse_token(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 0,
                "cost": 0,
                "cost_saved": None,
                "duration_ms": None,
                "is_estimated": None,
            }
        }
    )
    flat_observation = _parse_flat({"usage": None})

    assert observation.usage.cost_saved == 0.0
    assert observation.usage.duration_ms is None
    assert observation.usage.is_estimated is False
    assert flat_observation.usage.cost == DEFAULT_OPERATION_COST_USD
    assert flat_observation.cost_source is CostSource.OPERATION_DEFAULT


def test_flat_observation_preserves_strict_usage_diagnostics() -> None:
    observation = _parse_flat(
        {
            "usage": {
                "cost": 0.2,
                "cost_saved": 0.05,
                "duration_ms": 11,
                "is_estimated": True,
            }
        }
    )

    assert observation.usage.cost_saved == 0.05
    assert observation.usage.duration_ms == 11
    assert observation.usage.is_estimated is True


@pytest.mark.parametrize(
    "invalid_cost",
    [True, "0", -1, float("nan"), float("inf")],
)
def test_present_invalid_flat_cost_is_not_replaced_by_calculator(
    invalid_cost: object,
) -> None:
    calculators = MappingProxyType(
        {
            ("images_generation", "image-model"): OperationCostCalculator(
                unit="operation",
                rate_usd=0.35,
            )
        }
    )
    with pytest.raises(AccountingCostError) as exc_info:
        _parse_flat({"usage": {"cost": invalid_cost}}, calculators=calculators)

    assert exc_info.value.code is AccountingErrorCode.INVALID_UPSTREAM_COST


def test_synthesized_payload_cost_is_never_treated_as_upstream_cost() -> None:
    observation = parse_synthesized_operation_observation(
        {"usage": {"cost": 99.0}},
        policy=_policy("POST", "/v1/images/generations"),
        gateway_model="image-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )

    assert observation.usage.cost == DEFAULT_OPERATION_COST_USD
    assert observation.cost_source is CostSource.OPERATION_DEFAULT


def test_operation_parsers_preserve_external_duration_without_mutating_payload() -> None:
    upstream_payload = {
        "usage": {
            "prompt_tokens": 2,
            "completion_tokens": 0,
            "cost": 0,
        }
    }
    upstream = parse_upstream_operation_observation(
        upstream_payload,
        policy=_policy("POST", "/v1/embeddings"),
        gateway_model="gateway-model",
        provider="trusted-provider",
        model="trusted-model",
        cost_rate_registry=TOKEN_RATES,
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
        duration_ms=17,
    )
    synthesized_payload = {"usage": {"cost": 99.0}}
    synthesized = parse_synthesized_operation_observation(
        synthesized_payload,
        policy=_policy("POST", "/v1/images/generations"),
        gateway_model="image-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
        duration_ms=23,
    )

    assert upstream.usage.duration_ms == 17
    assert synthesized.usage.duration_ms == 23
    assert "duration_ms" not in upstream_payload["usage"]
    assert "duration_ms" not in synthesized_payload["usage"]


def test_operation_parser_rejects_conflicting_external_duration() -> None:
    with pytest.raises(AccountingValidationError):
        parse_upstream_operation_observation(
            {
                "usage": {
                    "prompt_tokens": 2,
                    "completion_tokens": 0,
                    "cost": 0,
                    "duration_ms": 16,
                }
            },
            policy=_policy("POST", "/v1/embeddings"),
            gateway_model="gateway-model",
            provider="trusted-provider",
            model="trusted-model",
            cost_rate_registry=TOKEN_RATES,
            operation_cost_calculator_registry=MappingProxyType({}),
            occurred_at=NOW,
            duration_ms=17,
        )


def _context(
    method: str,
    route: str,
    *,
    request_id: str = "request-1",
    parent_event_id: str | None = None,
) -> AccountingRequestContext:
    return AccountingRequestContext(
        method=method,
        route_template=route,
        policy=_policy(method, route),
        request_id=request_id,
        reservation=AccountingReservation(
            reservation_id=f"reservation-{request_id}",
            request_id=request_id,
            api_key_id=7,
            reserved_usd=1.0,
        ),
        parent_event_id=parent_event_id,
    )


def test_event_uses_reservation_identity_and_default_request_provenance() -> None:
    context = _context("POST", "/v1/embeddings")
    event = build_operation_accounting_event(context, _parse_token({"usage": {
        "prompt_tokens": 2,
        "completion_tokens": 1,
        "cost": 0,
    }}))

    assert event.version == ACCOUNTING_EVENT_VERSION
    assert event.event_id == context.request_id
    assert event.request_id == context.reservation.request_id
    assert event.parent_event_id is None
    assert event.api_key_id == context.reservation.api_key_id
    assert event.method == context.method
    assert event.route_template == context.route_template


def test_operation_event_propagates_parent_and_none_preserves_fingerprint() -> None:
    observation = _parse_token(
        {
            "usage": {
                "prompt_tokens": 2,
                "completion_tokens": 1,
                "cost": 0,
            }
        }
    )

    omitted = build_operation_accounting_event(
        _context("POST", "/v1/embeddings"),
        observation,
    )
    explicit_none = build_operation_accounting_event(
        _context("POST", "/v1/embeddings", parent_event_id=None),
        observation,
    )
    child = build_operation_accounting_event(
        _context(
            "POST",
            "/v1/embeddings",
            parent_event_id="usage:v1:http:parent",
        ),
        observation,
    )

    assert omitted.parent_event_id is explicit_none.parent_event_id is None
    assert omitted.billing_fingerprint == explicit_none.billing_fingerprint
    assert child.parent_event_id == "usage:v1:http:parent"
    assert child.billing_fingerprint != omitted.billing_fingerprint


def test_policy_equivalent_pdf_provenance_override_is_immutable_and_allowed() -> None:
    context = _context("GET", "/v1/pdf/jobs/{job_id}", request_id="poll-request")
    observation = parse_upstream_operation_observation(
        {},
        policy=context.policy,
        gateway_model="pdf-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )
    provenance = OperationEventProvenance(
        event_id="stable-pdf-event",
        method="POST",
        route_template="/v1/pdf/jobs",
    )

    event = build_operation_accounting_event(
        context,
        observation,
        provenance=provenance,
    )

    assert repr(provenance) == "<OperationEventProvenance>"
    assert event.event_id == "stable-pdf-event"
    assert event.method == "POST"
    assert event.route_template == "/v1/pdf/jobs"
    assert event.request_id == "poll-request"
    with pytest.raises(FrozenInstanceError):
        provenance.event_id = "changed"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("provider", "model"),
    [
        ("converter", None),
        (None, "pdf-model"),
        (" converter", "pdf-model"),
        ("converter", "pdf-model\x00"),
    ],
)
def test_pdf_provenance_requires_strict_paired_route_identity(
    provider: str | None,
    model: str | None,
) -> None:
    with pytest.raises(AccountingValidationError):
        OperationEventProvenance(
            event_id="stable-pdf-event",
            method="POST",
            route_template="/v1/pdf/jobs",
            provider=provider,
            model=model,
        )


def test_pdf_provenance_route_identity_changes_fingerprint_not_event_id() -> None:
    context = _context("GET", "/v1/pdf/jobs/{job_id}", request_id="poll-request")
    observation = parse_upstream_operation_observation(
        {"usage": {"cost": 0.25}},
        policy=context.policy,
        gateway_model="pdf-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )

    first = build_operation_accounting_event(
        context,
        observation,
        provenance=OperationEventProvenance(
            event_id="stable-pdf-event",
            method="POST",
            route_template="/v1/pdf/jobs",
            provider="converter-a",
            model="pdf-model-a",
        ),
    )
    changed = build_operation_accounting_event(
        context,
        observation,
        provenance=OperationEventProvenance(
            event_id="stable-pdf-event",
            method="POST",
            route_template="/v1/pdf/jobs",
            provider="converter-b",
            model="pdf-model-b",
        ),
    )

    assert first.event_id == changed.event_id == "stable-pdf-event"
    assert (first.provider, first.model) == ("converter-a", "pdf-model-a")
    assert (changed.provider, changed.model) == ("converter-b", "pdf-model-b")
    assert first.billing_fingerprint != changed.billing_fingerprint


def test_token_policy_rejects_provenance_route_identity_override() -> None:
    context = _context("POST", "/v1/embeddings")
    observation = _parse_token(
        {"usage": {"prompt_tokens": 2, "completion_tokens": 1, "cost": 0}}
    )

    with pytest.raises(AccountingValidationError):
        build_operation_accounting_event(
            context,
            observation,
            provenance=OperationEventProvenance(
                event_id="stable-token-event",
                method="POST",
                route_template="/v1/embeddings",
                provider="converter",
                model="embedding-model",
            ),
        )


def test_non_equivalent_provenance_or_observation_policy_is_rejected() -> None:
    context = _context("GET", "/v1/pdf/jobs/{job_id}")
    image_observation = _parse_flat({})

    with pytest.raises(AccountingValidationError):
        build_operation_accounting_event(context, image_observation)

    pdf_observation = parse_upstream_operation_observation(
        {},
        policy=context.policy,
        gateway_model="pdf-model",
        provider=None,
        model=None,
        cost_rate_registry=MappingProxyType({}),
        operation_cost_calculator_registry=MappingProxyType({}),
        occurred_at=NOW,
    )
    with pytest.raises(AccountingValidationError):
        build_operation_accounting_event(
            context,
            pdf_observation,
            provenance=OperationEventProvenance(
                event_id="spoofed",
                method="POST",
                route_template="/v1/images/generations",
            ),
        )
