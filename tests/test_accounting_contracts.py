from __future__ import annotations

import math
import traceback
from collections.abc import Callable
from dataclasses import FrozenInstanceError, replace
from datetime import datetime, timedelta, timezone
from types import MappingProxyType

import pytest

from llm_gateway_core.services.accounting import (
    ACCOUNTING_AUDIT_MAX_PAGE_SIZE,
    DEFAULT_OPERATION_COST_USD,
    AccountingReservation,
    AccountingOwnerAuditRow,
    AccountingOwnerState,
    AccountingParentLinkState,
    AccountingCostError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingHealthSnapshot,
    AccountingHealthState,
    AccountingProjectionMark,
    AccountingReceipt,
    AccountingSinkAuditRow,
    AccountingSourceAuditKind,
    AccountingSourceAuditRow,
    AccountingUsage,
    AccountingValidationError,
    BillingComponent,
    BillingComponentKind,
    BillingUnit,
    CostSource,
    OperationCostCalculator,
    ProjectionStatus,
    ReconciliationMode,
    ReconciliationReport,
    ResolvedCost,
    SourceAcceptance,
    SourceStatus,
    StoredAccountingEvent,
    build_operation_cost_calculator_registry,
    build_component_sum_usage,
    classify_billing_policy,
    resolve_operation_cost,
)


def _usage(*, cost: float = 0.25, cost_saved: float = 0.0) -> AccountingUsage:
    return AccountingUsage(
        prompt_tokens=3,
        completion_tokens=2,
        total_tokens=5,
        reasoning_tokens=1,
        cached_tokens=1,
        cost=cost,
        cost_saved=cost_saved,
        duration_ms=20,
    )


def _event(
    *,
    components: tuple[BillingComponent, ...] = (),
    occurred_at: datetime | None = None,
    request_id: str | None = "transport-request-1",
    usage: AccountingUsage | None = None,
    cost_source: CostSource = CostSource.OPERATION_CONFIGURED,
) -> AccountingEvent:
    return AccountingEvent(
        version=1,
        event_id="usage:v1:http:server-generated-id",
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/images/generations",
        operation="images_generation",
        gateway_model="gateway/image",
        provider="provider-a",
        model="image-a",
        usage=usage or _usage(),
        cost_source=cost_source,
        occurred_at=occurred_at or datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
        request_id=request_id,
        components=components,
    )


def _rollup_event() -> AccountingEvent:
    return AccountingEvent(
        version=1,
        event_id="usage:v1:http:rollup",
        kind=AccountingEventKind.ROLLUP,
        api_key_id=7,
        method="POST",
        route_template="/v1/web/deep-research",
        operation="web_deep_research",
        gateway_model="gateway/deep",
        provider=None,
        model=None,
        usage=AccountingUsage(),
        cost_source=CostSource.RECEIPT_ROLLUP,
        occurred_at=datetime(2026, 7, 13, 8, 0, tzinfo=timezone.utc),
        parent_event_id="usage:v1:http:parent",
        child_event_ids=("usage:v1:http:child-a", "usage:v1:http:child-b"),
        child_fingerprints=("a" * 64, "b" * 64),
    )


def _replace_usage(event: AccountingEvent, **changes: object) -> AccountingEvent:
    return replace(event, usage=replace(event.usage, **changes))


def test_operation_cost_calculator_is_frozen_and_normalizes_numeric_rate():
    calculator = OperationCostCalculator(unit="operation", rate_usd=1)

    assert calculator.rate_usd == 1.0
    with pytest.raises(FrozenInstanceError):
        calculator.rate_usd = 2.0  # type: ignore[misc]


@pytest.mark.parametrize(
    ("unit", "rate"),
    [
        ("token", 1.0),
        ("operation", True),
        ("operation", "0.1"),
        ("operation", -0.01),
        ("operation", float("nan")),
        ("operation", float("inf")),
    ],
)
def test_operation_cost_calculator_rejects_invalid_contract(unit: str, rate: object):
    with pytest.raises(AccountingValidationError) as error:
        OperationCostCalculator(unit=unit, rate_usd=rate)  # type: ignore[arg-type]

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
    assert repr(rate) not in str(error.value)


def test_operation_cost_calculator_rejects_huge_integer_with_typed_error():
    with pytest.raises(AccountingValidationError) as error:
        OperationCostCalculator(unit="operation", rate_usd=10**10000)

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


@pytest.mark.parametrize("upstream_cost", [0, 0.75])
def test_operation_cost_resolver_prefers_valid_present_upstream_including_zero(upstream_cost: float):
    resolved = resolve_operation_cost(
        upstream_cost_present=True,
        upstream_cost=upstream_cost,
        calculator=OperationCostCalculator(unit="operation", rate_usd=2.0),
    )

    assert resolved.cost_usd == float(upstream_cost)
    assert resolved.source is CostSource.UPSTREAM
    assert resolved.unit == "operation"
    assert resolved.quantity == 1


@pytest.mark.parametrize("configured_rate", [0, 0.35])
def test_operation_cost_resolver_uses_configured_rate_when_upstream_cost_is_absent(configured_rate: float):
    resolved = resolve_operation_cost(
        upstream_cost_present=False,
        upstream_cost=0,
        calculator=OperationCostCalculator(unit="operation", rate_usd=configured_rate),
    )

    assert resolved.cost_usd == float(configured_rate)
    assert resolved.source is CostSource.OPERATION_CONFIGURED


def test_operation_cost_resolver_uses_explicit_default_only_when_calculator_is_missing():
    resolved = resolve_operation_cost(
        upstream_cost_present=False,
        upstream_cost=0,
        calculator=None,
    )

    assert resolved.cost_usd == DEFAULT_OPERATION_COST_USD == 0.1
    assert resolved.source is CostSource.OPERATION_DEFAULT


@pytest.mark.parametrize("quantity", [True, 1.0])
def test_resolved_operation_cost_rejects_non_integer_quantity(quantity: object) -> None:
    with pytest.raises(AccountingValidationError) as error:
        ResolvedCost(
            cost_usd=0.1,
            source=CostSource.OPERATION_DEFAULT,
            quantity=quantity,  # type: ignore[arg-type]
        )

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_invalid_contract_suppresses_sensitive_conversion_context() -> None:
    sensitive_source = "TOP_" + "SECRET_SOURCE"

    with pytest.raises(AccountingValidationError) as error:
        ResolvedCost(
            cost_usd=0.1,
            source=sensitive_source,  # type: ignore[arg-type]
            quantity=1,
        )

    rendered = "".join(traceback.format_exception(error.value))
    assert sensitive_source not in rendered
    assert error.value.__suppress_context__ is True


@pytest.mark.parametrize("invalid_cost", [None, True, "secret-cost", -1, float("nan"), float("inf")])
def test_operation_cost_resolver_rejects_present_invalid_upstream_without_fallback(invalid_cost: object):
    with pytest.raises(AccountingCostError) as error:
        resolve_operation_cost(
            upstream_cost_present=True,
            upstream_cost=invalid_cost,
            calculator=OperationCostCalculator(unit="operation", rate_usd=3.0),
        )

    assert error.value.code is AccountingErrorCode.INVALID_UPSTREAM_COST
    assert "secret-cost" not in str(error.value)
    assert "secret-cost" not in repr(error.value)


def test_operation_cost_resolver_maps_huge_present_upstream_to_typed_cost_error():
    with pytest.raises(AccountingCostError) as error:
        resolve_operation_cost(
            upstream_cost_present=True,
            upstream_cost=10**10000,
            calculator=OperationCostCalculator(unit="operation", rate_usd=3.0),
        )

    assert error.value.code is AccountingErrorCode.INVALID_UPSTREAM_COST


def test_operation_cost_registry_contains_only_explicit_flat_operation_calculators():
    flat_sections = {
        "images_generations": "images_generation",
        "images_edits": "images_edit",
        "audio_speech": "audio_speech",
        "audio_transcriptions": "audio_transcription",
        "pdf_conversions": "pdf_conversion",
        "web_search": "web_search",
        "web_read": "web_read",
        "web_research": "web_research",
        "web_deep_research": "web_deep_research",
    }
    rates_by_section = {section: index / 10 for index, section in enumerate(flat_sections, start=1)}
    rates_by_section["audio_transcriptions"] = 0.0
    rules = {
        section: {
            f"gateway/{section}": {
                "cost_calculator": {
                    "unit": "operation",
                    "rate_usd": rates_by_section[section],
                },
                "routes": [],
            }
        }
        for section in flat_sections
    }
    rules["images_generations"]["gateway/default-image"] = {"routes": []}
    rules.update(
        {
            "embeddings": {
                "gateway/embed": {
                    "cost_calculator": {"unit": "operation", "rate_usd": 99},
                    "routes": [],
                }
            },
            "rerank": {
                "gateway/rerank": {
                    "cost_calculator": {"unit": "operation", "rate_usd": 99},
                    "routes": [],
                }
            },
        }
    )

    registry = build_operation_cost_calculator_registry(rules)

    assert isinstance(registry, MappingProxyType)
    assert registry == {
        (operation, f"gateway/{section}"): OperationCostCalculator(
            "operation",
            rates_by_section[section],
        )
        for section, operation in flat_sections.items()
    }
    assert ("images_generation", "gateway/default-image") not in registry
    assert not any(operation in {"embeddings", "rerank"} for operation, _model in registry)
    with pytest.raises(TypeError):
        registry[("web_read", "gateway/read")] = OperationCostCalculator("operation", 1.0)  # type: ignore[index]


@pytest.mark.parametrize(
    "gateway_model",
    [" gateway/read", "gateway/read ", "gateway /read", "gateway/\tread"],
)
def test_operation_cost_registry_rejects_gateway_model_whitespace_without_normalizing(
    gateway_model: str,
):
    with pytest.raises(AccountingValidationError):
        build_operation_cost_calculator_registry(
            {"web_read": {gateway_model: {"cost_calculator": {"unit": "operation", "rate_usd": 0.5}}}}
        )


def test_operation_cost_registry_rejects_whitespace_identity_collision():
    with pytest.raises(AccountingValidationError):
        build_operation_cost_calculator_registry(
            {
                "web_read": {
                    "gateway/read": {"cost_calculator": {"unit": "operation", "rate_usd": 0.5}},
                    " gateway/read ": {"cost_calculator": {"unit": "operation", "rate_usd": 0.9}},
                }
            }
        )


def test_operation_cost_registry_detaches_from_mutable_input():
    calculator = {"unit": "operation", "rate_usd": 0.5}
    rules = {"web_read": {"gateway/read": {"cost_calculator": calculator}}}

    registry = build_operation_cost_calculator_registry(rules)
    calculator["rate_usd"] = 9.0

    assert registry[("web_read", "gateway/read")].rate_usd == 0.5


def test_operation_cost_registry_rejects_malformed_supported_section_without_leaking_value():
    with pytest.raises(AccountingValidationError) as error:
        build_operation_cost_calculator_registry(
            {"web_read": {"gateway/read": {"cost_calculator": {"unit": "operation", "rate_usd": "secret"}}}}
        )

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT
    assert "secret" not in str(error.value)


def test_accounting_usage_accepts_signed_cost_saved_and_enforces_token_total():
    usage = _usage(cost_saved=-0.3)

    assert usage.cost_saved == -0.3
    with pytest.raises(AccountingValidationError):
        replace(usage, total_tokens=4)


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("prompt_tokens", True),
        ("completion_tokens", -1),
        ("cost", float("nan")),
        ("cost", -0.01),
        ("cost_saved", float("inf")),
        ("duration_ms", -1),
        ("is_estimated", 1),
    ],
)
def test_accounting_usage_rejects_invalid_values(field: str, value: object):
    values = {
        "prompt_tokens": 3,
        "completion_tokens": 2,
        "total_tokens": 5,
        "reasoning_tokens": 1,
        "cached_tokens": 1,
        "cost": 0.25,
        "cost_saved": 0.0,
        "duration_ms": 20,
        "is_estimated": False,
    }
    values[field] = value

    with pytest.raises(AccountingValidationError):
        AccountingUsage(**values)  # type: ignore[arg-type]


def test_persisted_integer_contracts_enforce_sqlite_int64_bounds():
    sqlite_int64_max = (1 << 63) - 1

    usage = AccountingUsage(prompt_tokens=sqlite_int64_max, total_tokens=sqlite_int64_max)
    event = replace(_event(), api_key_id=sqlite_int64_max)

    assert usage.prompt_tokens == sqlite_int64_max
    assert event.api_key_id == sqlite_int64_max
    with pytest.raises(AccountingValidationError):
        AccountingUsage(prompt_tokens=sqlite_int64_max + 1, total_tokens=sqlite_int64_max + 1)
    with pytest.raises(AccountingValidationError):
        replace(event, api_key_id=sqlite_int64_max + 1)


def test_accounting_event_rejects_gateway_model_whitespace_without_normalizing():
    with pytest.raises(AccountingValidationError):
        replace(_event(), gateway_model=" gateway/image ")


@pytest.mark.parametrize(
    ("provider", "model"),
    (("provider\x00suffix", "model"), ("provider", "model\x00suffix")),
)
def test_billing_component_rejects_nul_identity_for_schema_parity(
    provider: str,
    model: str,
):
    with pytest.raises(AccountingValidationError):
        BillingComponent(
            provider=provider,
            model=model,
            usage=_usage(),
            cost_source=CostSource.UPSTREAM,
        )


@pytest.mark.parametrize("edge_whitespace", ("\t", "\n", "\u00a0"))
def test_billing_component_canonicalizes_edge_whitespace_before_storage(
    edge_whitespace: str,
):
    component = BillingComponent(
        provider=f"{edge_whitespace}provider{edge_whitespace}",
        model=f"{edge_whitespace}model{edge_whitespace}",
        usage=_usage(),
        cost_source=CostSource.UPSTREAM,
    )

    assert component.provider == "provider"
    assert component.model == "model"


def test_model_billing_component_keeps_positional_contract_and_v1_fingerprint():
    component = BillingComponent(
        "provider-a",
        "model-a",
        _usage(),
        CostSource.UPSTREAM,
    )

    assert component.component_kind is BillingComponentKind.MODEL
    assert component.operation is None
    assert component.gateway_model is None
    assert component.billing_fingerprint == (
        "393b7f30cc58858db0954de4aa5580c85ca6666550365e329d02fd3243f6d244"
    )


def test_operation_billing_component_has_typed_identity_and_fingerprint():
    usage = AccountingUsage(cost=0.1)
    component = BillingComponent(
        None,
        None,
        usage,
        CostSource.OPERATION_DEFAULT,
        component_kind="operation",
        operation="web_search",
        gateway_model="gateway/search",
    )

    assert component.component_kind is BillingComponentKind.OPERATION
    assert component.provider is None
    assert component.model is None
    assert component.operation == "web_search"
    assert component.gateway_model == "gateway/search"
    assert component.billing_fingerprint == (
        "de1c9ac887ce09c2c3281bca0a7ae3f4e5acb23a65a75649b6c246f329814b46"
    )
    assert component.billing_fingerprint != replace(
        component,
        gateway_model="gateway/search-v2",
    ).billing_fingerprint
    assert component.billing_fingerprint != replace(
        component,
        cost_source=CostSource.OPERATION_CONFIGURED,
    ).billing_fingerprint


@pytest.mark.parametrize(
    "changes",
    (
        {"provider": None},
        {"model": None},
        {"operation": "web_search"},
        {"gateway_model": "gateway/search"},
        {"component_kind": "unknown"},
    ),
)
def test_model_billing_component_rejects_mixed_or_invalid_identity(
    changes: dict[str, object],
):
    with pytest.raises(AccountingValidationError):
        replace(
            BillingComponent(
                "provider-a",
                "model-a",
                _usage(),
                CostSource.UPSTREAM,
            ),
            **changes,
        )


@pytest.mark.parametrize(
    "changes",
    (
        {"provider": "provider-a"},
        {"model": "model-a"},
        {"operation": None},
        {"gateway_model": None},
        {"gateway_model": " gateway/search "},
        {"gateway_model": "gateway/search\x00hidden"},
        {"cost_source": CostSource.TOKEN_REGISTRY},
        {"cost_source": CostSource.COMPONENT_SUM},
        {"usage": AccountingUsage(prompt_tokens=1, total_tokens=1, cost=0.1)},
        {"usage": AccountingUsage(reasoning_tokens=1, cost=0.1)},
        {"usage": AccountingUsage(cached_tokens=1, cost=0.1)},
    ),
)
def test_operation_billing_component_rejects_mixed_identity_tokens_and_sources(
    changes: dict[str, object],
):
    component = BillingComponent(
        None,
        None,
        AccountingUsage(cost=0.1),
        CostSource.OPERATION_DEFAULT,
        component_kind=BillingComponentKind.OPERATION,
        operation="web_search",
        gateway_model="gateway/search",
    )

    with pytest.raises(AccountingValidationError):
        replace(component, **changes)


@pytest.mark.parametrize(
    "method",
    ("P0ST", "P OST", "POST!", "POST\x00BAD"),
)
def test_accounting_event_rejects_http_methods_the_schema_cannot_persist(
    method: str,
):
    with pytest.raises(AccountingValidationError):
        replace(_event(), method=method)


def test_accounting_event_normalizes_letters_only_http_method_for_storage():
    assert replace(_event(), method="post").method == "POST"


@pytest.mark.parametrize(
    "invalid_event_id",
    ("event\nchild", "event\x00child", "event\x7fchild"),
)
def test_event_and_receipt_ids_reject_nonprintable_ascii_for_schema_parity(
    invalid_event_id: str,
):
    with pytest.raises(AccountingValidationError):
        replace(_event(), event_id=invalid_event_id)
    with pytest.raises(AccountingValidationError):
        replace(_event(), parent_event_id=invalid_event_id)

    rollup = _rollup_event()
    with pytest.raises(AccountingValidationError):
        replace(
            rollup,
            child_event_ids=(invalid_event_id, rollup.child_event_ids[1]),
        )

    receipt = AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.PENDING,
        event_id="persistable-event",
        billing_fingerprint="a" * 64,
    )
    with pytest.raises(AccountingValidationError):
        replace(receipt, event_id=invalid_event_id)
    with pytest.raises(AccountingValidationError):
        replace(
            receipt,
            child_event_ids=(invalid_event_id,),
            child_fingerprints=("b" * 64,),
        )


@pytest.mark.parametrize(
    "route_template",
    ("/v1/images\x00hidden", "/v1/images\nnext", "/v1/images?size=1"),
)
def test_accounting_event_rejects_route_templates_the_schema_cannot_persist(
    route_template: str,
):
    with pytest.raises(AccountingValidationError):
        replace(_event(), route_template=route_template)


@pytest.mark.parametrize(
    "field_name",
    ("components", "child_event_ids", "child_fingerprints"),
)
def test_accounting_event_maps_noniterable_collections_to_typed_error(
    field_name: str,
):
    with pytest.raises(AccountingValidationError):
        replace(_event(), **{field_name: None})


@pytest.mark.parametrize("field_name", ("child_event_ids", "child_fingerprints"))
def test_accounting_receipt_maps_noniterable_collections_to_typed_error(
    field_name: str,
):
    event = _event()
    receipt = AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.PENDING,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
    )

    with pytest.raises(AccountingValidationError):
        replace(receipt, **{field_name: None})


@pytest.mark.parametrize("costs", [(0.1, 0.2), (0.1,) * 10])
def test_component_sum_builder_uses_canonical_fsum(costs: tuple[float, ...]):
    components = tuple(
        BillingComponent(
            provider="provider-a",
            model=f"model-{index}",
            usage=_usage(cost=cost),
            cost_source=CostSource.UPSTREAM,
        )
        for index, cost in enumerate(costs)
    )

    aggregate = build_component_sum_usage(components)
    event = _event(
        components=components,
        usage=aggregate,
        cost_source=CostSource.COMPONENT_SUM,
    )

    assert aggregate.cost == math.fsum(costs)
    assert event.usage == aggregate


def test_component_sum_builder_maps_fsum_overflow_to_typed_validation_error():
    components = tuple(
        BillingComponent(
            provider="provider-a",
            model=f"model-{index}",
            usage=_usage(cost=1e308),
            cost_source=CostSource.UPSTREAM,
        )
        for index in range(2)
    )

    with pytest.raises(AccountingValidationError):
        build_component_sum_usage(components)


def test_component_sum_event_rejects_noncanonical_near_equal_cost():
    components = (
        BillingComponent("provider-a", "model-a", _usage(cost=0.1), CostSource.UPSTREAM),
        BillingComponent("provider-b", "model-b", _usage(cost=0.2), CostSource.UPSTREAM),
    )
    canonical_usage = build_component_sum_usage(components)

    assert canonical_usage.cost == math.fsum((0.1, 0.2))
    assert canonical_usage.cost != 0.3
    with pytest.raises(AccountingValidationError):
        _event(
            components=components,
            usage=replace(canonical_usage, cost=0.3),
            cost_source=CostSource.COMPONENT_SUM,
        )


def test_accounting_event_fingerprint_excludes_non_billing_identity_and_diagnostics():
    original = _event()
    retried = replace(
        _event(
            occurred_at=original.occurred_at + timedelta(minutes=10),
            request_id="different-transport-request",
        ),
        event_id="usage:v1:http:different-server-id",
        usage=replace(
            original.usage,
            cost_saved=9.0,
            duration_ms=999,
            is_estimated=True,
        ),
    )

    assert original.billing_fingerprint == retried.billing_fingerprint
    assert len(original.billing_fingerprint) == 64


def test_accounting_event_fingerprint_v1_golden_vectors():
    assert _event().billing_fingerprint == "c04aea32503b405a1fa798b03b73daed2694a66f87ca58ddf079a644ef3b3648"
    assert _rollup_event().billing_fingerprint == "ca91dbbb3e58806f57f279fa4431fb74f30fc5cc0c956ca23b8d92513782ff7c"


@pytest.mark.parametrize(
    "mutate",
    [
        pytest.param(lambda event: replace(event, api_key_id=8), id="api-key-id"),
        pytest.param(lambda event: replace(event, method="PUT"), id="method"),
        pytest.param(
            lambda event: replace(event, route_template="/v1/images/edits"),
            id="route-template",
        ),
        pytest.param(
            lambda event: replace(event, operation="images_edit"),
            id="operation",
        ),
        pytest.param(
            lambda event: replace(event, gateway_model="gateway/image-v2"),
            id="gateway-model",
        ),
        pytest.param(lambda event: replace(event, provider="provider-b"), id="provider"),
        pytest.param(lambda event: replace(event, model="image-b"), id="model"),
        pytest.param(
            lambda event: replace(event, cost_source=CostSource.OPERATION_DEFAULT),
            id="cost-source",
        ),
        pytest.param(
            lambda event: replace(event, parent_event_id="usage:v1:http:parent"),
            id="parent-event-id",
        ),
        pytest.param(
            lambda event: _replace_usage(event, prompt_tokens=4, total_tokens=6),
            id="prompt-tokens",
        ),
        pytest.param(
            lambda event: _replace_usage(event, completion_tokens=3, total_tokens=6),
            id="completion-tokens",
        ),
        pytest.param(
            lambda event: _replace_usage(event, reasoning_tokens=2),
            id="reasoning-tokens",
        ),
        pytest.param(
            lambda event: _replace_usage(event, cached_tokens=2),
            id="cached-tokens",
        ),
        pytest.param(lambda event: _replace_usage(event, cost=0.3), id="cost"),
    ],
)
def test_accounting_event_fingerprint_changes_for_each_billing_field(
    mutate: Callable[[AccountingEvent], AccountingEvent],
):
    original = _event()

    assert mutate(original).billing_fingerprint != original.billing_fingerprint


def test_accounting_event_fingerprint_normalizes_negative_zero():
    positive_zero = _event(usage=_usage(cost=0.0))
    negative_zero = _event(usage=_usage(cost=-0.0))

    assert positive_zero.billing_fingerprint == negative_zero.billing_fingerprint


def test_accounting_event_fingerprint_is_sensitive_to_ordered_components():
    first = BillingComponent(
        provider="provider-a",
        model="model-a",
        usage=_usage(cost=0.1),
        cost_source=CostSource.UPSTREAM,
    )
    second = BillingComponent(
        provider="provider-b",
        model="model-b",
        usage=_usage(cost=0.15),
        cost_source=CostSource.TOKEN_REGISTRY,
    )
    usage = AccountingUsage(
        prompt_tokens=6,
        completion_tokens=4,
        total_tokens=10,
        reasoning_tokens=2,
        cached_tokens=2,
        cost=0.25,
        duration_ms=20,
    )

    forward = _event(components=(first, second), usage=usage, cost_source=CostSource.COMPONENT_SUM)
    reversed_components = _event(components=(second, first), usage=usage, cost_source=CostSource.COMPONENT_SUM)

    assert forward.billing_fingerprint != reversed_components.billing_fingerprint
    assert first.billing_fingerprint != second.billing_fingerprint


def test_rollup_event_requires_zero_usage_and_ordered_child_references():
    rollup = _rollup_event()

    assert rollup.usage.cost == 0.0
    assert (
        rollup.billing_fingerprint
        != replace(
            rollup,
            child_event_ids=tuple(reversed(rollup.child_event_ids)),
            child_fingerprints=tuple(reversed(rollup.child_fingerprints)),
        ).billing_fingerprint
    )
    with pytest.raises(AccountingValidationError):
        replace(rollup, usage=_usage())


def test_correlation_contracts_have_credential_safe_repr():
    event = _event(request_id="raw-secret-transport-id")
    receipt = AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.PENDING,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
        usage_row_id=11,
    )

    assert repr(event) == "<AccountingEvent>"
    assert repr(receipt) == "<AccountingReceipt>"
    assert "raw-secret-transport-id" not in repr(event)


def test_stored_event_and_source_acceptance_are_frozen_safe_contracts():
    event = _event(request_id="raw-secret-transport-id")
    stored = StoredAccountingEvent(
        event=event,
        usage_row_id=11,
        created_at=datetime(2026, 7, 13, 8, 1, tzinfo=timezone.utc),
        projection_attempts=2,
        last_error_code=AccountingErrorCode.PROJECTION_WRITE_FAILED,
    )
    acceptance = SourceAcceptance(
        status=SourceStatus.ACCEPTED,
        stored_event=stored,
    )

    assert stored.billing_fingerprint == event.billing_fingerprint
    assert repr(stored) == "<StoredAccountingEvent>"
    assert repr(acceptance) == "<SourceAcceptance>"
    assert "raw-secret-transport-id" not in repr((stored, acceptance))
    with pytest.raises(FrozenInstanceError):
        stored.usage_row_id = 12  # type: ignore[misc]


def test_projection_mark_is_frozen_and_credential_safe() -> None:
    attempted_at = datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc)
    mark = AccountingProjectionMark(
        event_id="raw-secret-event-id",
        billing_fingerprint="a" * 64,
        projected_at=attempted_at,
        projection_attempts=1,
        last_attempt_at=attempted_at,
        last_error_code=None,
    )

    assert repr(mark) == "<AccountingProjectionMark>"
    assert "raw-secret-event-id" not in repr(mark)
    with pytest.raises(FrozenInstanceError):
        mark.projection_attempts = 2  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    (
        {"event_id": ""},
        {"billing_fingerprint": "secret-fingerprint"},
        {"projected_at": datetime(2026, 7, 13, 8, 2)},
        {"projection_attempts": True},
        {"projection_attempts": -1},
        {"projection_attempts": 0, "last_attempt_at": datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc)},
        {"projection_attempts": 1, "last_attempt_at": None},
        {
            "projection_attempts": 1,
            "last_attempt_at": datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
            "last_error_code": AccountingErrorCode.PROJECTION_WRITE_FAILED,
        },
    ),
)
def test_projection_mark_rejects_invalid_contract(changes: dict[str, object]) -> None:
    mark = AccountingProjectionMark(
        event_id="event-id",
        billing_fingerprint="a" * 64,
        projected_at=datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
        projection_attempts=1,
        last_attempt_at=datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
        last_error_code=None,
    )

    with pytest.raises(AccountingValidationError):
        replace(mark, **changes)


@pytest.mark.parametrize(
    "changes",
    (
        {"event": object()},
        {"usage_row_id": None},
        {"usage_row_id": 0},
        {"usage_row_id": True},
        {"created_at": datetime(2026, 7, 13, 8, 1)},
        {"projection_attempts": -1},
        {"last_error_code": "secret-error"},
    ),
)
def test_stored_event_rejects_invalid_contract(changes: dict[str, object]):
    stored = StoredAccountingEvent(
        event=_event(),
        usage_row_id=11,
        created_at=datetime(2026, 7, 13, 8, 1, tzinfo=timezone.utc),
    )

    with pytest.raises(AccountingValidationError):
        replace(stored, **changes)


def test_stored_projected_event_allows_retained_usage_row_to_be_absent():
    stored = StoredAccountingEvent(
        event=_event(),
        usage_row_id=None,
        created_at=datetime(2026, 7, 13, 8, 1, tzinfo=timezone.utc),
        projected_at=datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
    )

    assert stored.usage_row_id is None


def test_source_acceptance_rejects_non_stored_event():
    with pytest.raises(AccountingValidationError):
        SourceAcceptance(
            status=SourceStatus.ACCEPTED,
            stored_event=object(),  # type: ignore[arg-type]
        )


def test_accounting_health_snapshot_rejects_negative_counters():
    healthy = AccountingHealthSnapshot(
        state=AccountingHealthState.RUNNING,
        initialized=True,
        accepting=True,
        active_sessions=1,
        pending_source_events=0,
        projection_attempts=2,
        fingerprint_conflicts=0,
        source_orphans=0,
        sink_orphans=0,
        unrolled_children=0,
    )

    assert healthy.accepting is True
    with pytest.raises(AccountingValidationError):
        replace(healthy, pending_source_events=-1)


def test_accounting_reservation_is_frozen_opaque_and_repr_safe() -> None:
    reservation = AccountingReservation(
        reservation_id="opaque-reservation-id",
        request_id="server-request-id",
        api_key_id=7,
        reserved_usd=0.25,
    )

    assert reservation.request_id == "server-request-id"
    assert repr(reservation) == "<AccountingReservation>"
    with pytest.raises(FrozenInstanceError):
        reservation.reserved_usd = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    (
        {"reservation_id": ""},
        {"request_id": "request\nsecret"},
        {"api_key_id": True},
        {"api_key_id": 0},
        {"reserved_usd": -0.01},
        {"reserved_usd": float("nan")},
    ),
)
def test_accounting_reservation_rejects_invalid_contract(
    changes: dict[str, object],
) -> None:
    values = {
        "reservation_id": "opaque-reservation-id",
        "request_id": "server-request-id",
        "api_key_id": 7,
        "reserved_usd": 0.25,
    }
    values.update(changes)

    with pytest.raises(AccountingValidationError) as error:
        AccountingReservation(**values)  # type: ignore[arg-type]

    assert error.value.code is AccountingErrorCode.INVALID_CONTRACT


def test_health_snapshot_publishes_only_consistent_full_reconciliation() -> None:
    report = ReconciliationReport(
        mode=ReconciliationMode.FULL,
        unrolled_children=2,
    )
    snapshot = AccountingHealthSnapshot(
        last_full_reconciliation=report,
        unrolled_children=2,
    )

    assert snapshot.last_full_reconciliation is report
    with pytest.raises(AccountingValidationError):
        replace(snapshot, unrolled_children=1)
    with pytest.raises(AccountingValidationError):
        replace(
            snapshot,
            last_full_reconciliation=ReconciliationReport(
                mode=ReconciliationMode.RUNTIME,
            ),
            unrolled_children=0,
        )


def test_reconciliation_contracts_are_bounded_frozen_and_credential_safe() -> None:
    source = AccountingSourceAuditRow(
        event_id="sensitive-source-event",
        row_kind=AccountingSourceAuditKind.OUTBOX,
        event_kind=AccountingEventKind.CHARGE,
        billing_fingerprint="a" * 64,
        api_key_id=7,
        spend_usd=0.25,
        projected_at=datetime(2026, 7, 13, 8, 2, tzinfo=timezone.utc),
        usage_present=True,
        parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
    )
    sink = AccountingSinkAuditRow(
        event_id="sensitive-sink-event",
        billing_fingerprint="b" * 64,
        api_key_id=7,
        spend_usd=0.25,
        applied_at=datetime(2026, 7, 13, 8, 3, tzinfo=timezone.utc),
        sink_kind="active",
        owner_state=AccountingOwnerState.TOMBSTONE,
    )
    owner = AccountingOwnerAuditRow(
        api_key_id=7,
        owner_state=AccountingOwnerState.TOMBSTONE,
    )

    assert ACCOUNTING_AUDIT_MAX_PAGE_SIZE == 256
    assert repr(source) == "<AccountingSourceAuditRow>"
    assert repr(sink) == "<AccountingSinkAuditRow>"
    assert repr(owner) == "<AccountingOwnerAuditRow>"
    assert "sensitive" not in repr((source, sink, owner))
    with pytest.raises(FrozenInstanceError):
        source.spend_usd = 1.0  # type: ignore[misc]


@pytest.mark.parametrize(
    "changes",
    (
        {"billing_fingerprint": None},
        {"spend_usd": -0.01},
        {"usage_present": 1},
        {"projected_at": datetime(2026, 7, 13, 8, 2)},
        {"api_key_id": 0},
        {
            "row_kind": AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX,
            "billing_fingerprint": "a" * 64,
        },
    ),
)
def test_source_audit_row_rejects_invalid_combinations(changes: dict[str, object]) -> None:
    row = AccountingSourceAuditRow(
        event_id="event-id",
        row_kind=AccountingSourceAuditKind.OUTBOX,
        event_kind=AccountingEventKind.CHARGE,
        billing_fingerprint="a" * 64,
        api_key_id=7,
        spend_usd=0.25,
        projected_at=None,
        usage_present=True,
        parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
    )

    with pytest.raises(AccountingValidationError):
        replace(row, **changes)


def test_usage_without_outbox_contract_has_no_fabricated_source_identity() -> None:
    row = AccountingSourceAuditRow(
        event_id="legacy-event",
        row_kind=AccountingSourceAuditKind.USAGE_WITHOUT_OUTBOX,
        event_kind=AccountingEventKind.CHARGE,
        billing_fingerprint=None,
        api_key_id=None,
        spend_usd=0.1,
        projected_at=None,
        usage_present=True,
        parent_link_state=AccountingParentLinkState.NOT_APPLICABLE,
    )

    assert row.billing_fingerprint is None
    with pytest.raises(AccountingValidationError):
        replace(row, usage_present=False)


@pytest.mark.parametrize(
    "row",
    (
        AccountingSinkAuditRow(
            event_id="event-id",
            billing_fingerprint="a" * 64,
            api_key_id=7,
            spend_usd=0.25,
            applied_at=datetime(2026, 7, 13, 8, 3, tzinfo=timezone.utc),
            sink_kind="active",
            owner_state=AccountingOwnerState.ACTIVE,
        ),
        AccountingOwnerAuditRow(
            api_key_id=7,
            owner_state=AccountingOwnerState.MISSING,
        ),
    ),
)
def test_sink_and_owner_audit_rows_reject_invalid_key_ids(row: object) -> None:
    with pytest.raises(AccountingValidationError):
        replace(row, api_key_id=True)


def test_reconciliation_report_clean_ignores_only_observation_counters() -> None:
    clean = ReconciliationReport(
        mode=ReconciliationMode.FULL,
        source_rows_scanned=2,
        sink_rows_scanned=1,
        unrolled_children=3,
    )

    assert clean.clean is True
    assert clean.full is True
    assert replace(clean, mode=ReconciliationMode.RUNTIME).full is False
    with pytest.raises(AccountingValidationError):
        replace(clean, source_rows_scanned=True)


@pytest.mark.parametrize(
    "field_name",
    (
        "retained_events",
        "pending_source_events",
        "usage_without_outbox",
        "pending_without_usage",
        "projected_without_receipt",
        "receipt_without_source",
        "unexpected_receipts",
        "fingerprint_mismatches",
        "api_key_mismatches",
        "cost_mismatches",
        "owner_orphans",
    ),
)
def test_each_structural_reconciliation_counter_blocks_clean(
    field_name: str,
) -> None:
    report = ReconciliationReport(mode=ReconciliationMode.FULL)

    assert replace(report, **{field_name: 1}).clean is False


@pytest.mark.parametrize(
    ("method", "route_template", "operation", "unit"),
    [
        ("POST", "/v1/chat/completions", "chat", BillingUnit.TOKEN),
        ("POST", "/v1/v1/chat/completions", "chat", BillingUnit.TOKEN),
        ("POST", "/v1/messages", "chat", BillingUnit.TOKEN),
        ("POST", "/v1/v1/messages", "chat", BillingUnit.TOKEN),
        ("POST", "/v1/responses", "chat", BillingUnit.TOKEN),
        ("POST", "/v1/embeddings", "embeddings", BillingUnit.TOKEN),
        ("POST", "/v1/rerank", "rerank", BillingUnit.TOKEN),
        ("POST", "/v1/images/generations", "images_generation", BillingUnit.OPERATION),
        ("POST", "/v1/images", "images_generation", BillingUnit.OPERATION),
        ("POST", "/v1/images/edits", "images_edit", BillingUnit.OPERATION),
        ("POST", "/v1/audio/speech", "audio_speech", BillingUnit.OPERATION),
        ("POST", "/v1/audio/transcriptions", "audio_transcription", BillingUnit.OPERATION),
        ("POST", "/v1/pdf/convert", "pdf_conversion", BillingUnit.OPERATION),
        ("POST", "/v1/pdf/jobs", "pdf_conversion", BillingUnit.OPERATION),
        ("GET", "/v1/pdf/jobs/{job_id}", "pdf_conversion", BillingUnit.OPERATION),
        ("POST", "/v1/web/search", "web_search", BillingUnit.OPERATION),
        ("POST", "/v1/tavily/search", "web_search", BillingUnit.OPERATION),
        ("POST", "/v1/web/read", "web_read", BillingUnit.OPERATION),
        ("POST", "/v1/tavily/extract", "web_read", BillingUnit.OPERATION),
        ("POST", "/v1/web/research", "web_research", BillingUnit.OPERATION),
        ("POST", "/v1/web/deep-research", "web_deep_research", BillingUnit.OPERATION),
    ],
)
def test_billing_policy_classifier_uses_exact_method_and_matched_route_template(
    method: str,
    route_template: str,
    operation: str,
    unit: BillingUnit,
):
    policy = classify_billing_policy(method, route_template)

    assert policy is not None
    assert policy.operation == operation
    assert policy.unit is unit


@pytest.mark.parametrize(
    ("method", "route_template"),
    [
        ("GET", "/v1/chat/completions"),
        ("POST", "/v1/pdf/jobs/{job_id}"),
        ("GET", "/v1/pdf/jobs/concrete-id"),
        ("GET", "/v1/pdf/jobs/{job_id}/result"),
        ("POST", "/v1/foo/chat/completions"),
        ("POST", "/mounted/v1/images/generations"),
        ("POST", "/v1/images/generations/"),
        ("POST", "/v1/models"),
    ],
)
def test_billing_policy_classifier_rejects_method_mismatches_and_suffix_lookalikes(
    method: str,
    route_template: str,
):
    assert classify_billing_policy(method, route_template) is None
