from __future__ import annotations

import asyncio
from dataclasses import FrozenInstanceError
from datetime import datetime, timedelta, timezone

import pytest

from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingError,
    AccountingErrorCode,
    AccountingEvent,
    AccountingEventKind,
    AccountingReceipt,
    AccountingReservation,
    AccountingUsage,
    AccountingValidationError,
    CostSource,
    ProjectionStatus,
    SourceStatus,
)
from llm_gateway_core.services.deep_research_accounting import (
    DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES,
    DeepResearchAccountingRegistry,
    DeepResearchAuthIdentity,
    DeepResearchChildSeal,
    DeepResearchContextError,
    DeepResearchContextErrorCode,
    DeepResearchContextTokenCodec,
    DeepResearchDelegatedIdentity,
    DeepResearchParentHandle,
    build_deep_research_rollup_event,
    seal_deep_research_children,
)
from tests._async_compat import run_async


NOW = datetime(2026, 7, 14, 12, 0, tzinfo=timezone.utc)


def _reservation() -> AccountingReservation:
    return AccountingReservation(
        reservation_id="reservation-parent",
        request_id="usage:v1:http:deep-research-parent",
        api_key_id=7,
        reserved_usd=0.1,
    )


def _receipt(name: str, fingerprint_character: str) -> AccountingReceipt:
    return AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=f"usage:v1:http:{name}",
        billing_fingerprint=fingerprint_character * 64,
        usage_row_id=1,
    )


def _child_event(
    name: str,
    *,
    request_id: str,
    parent_event_id: str,
    cost: float,
) -> AccountingEvent:
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=f"usage:v1:http:{name}",
        kind=AccountingEventKind.CHARGE,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/model",
        provider="provider",
        model="model",
        usage=AccountingUsage(
            prompt_tokens=1,
            completion_tokens=1,
            total_tokens=2,
            cost=cost,
        ),
        cost_source=CostSource.UPSTREAM,
        occurred_at=NOW,
        request_id=request_id,
        parent_event_id=parent_event_id,
    )


def _parent() -> DeepResearchParentHandle:
    return DeepResearchParentHandle(
        context_id="a" * 32,
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        expires_at=NOW + timedelta(minutes=5),
    )


def test_context_token_round_trip_is_ascii_bounded_and_utc() -> None:
    codec = DeepResearchContextTokenCodec(b"s" * 32)
    expires_at = (NOW + timedelta(minutes=5)).astimezone(
        timezone(timedelta(hours=3))
    )

    handle, token = codec.issue_parent(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        expires_at=expires_at,
    )
    decoded = codec.decode(token, now=NOW)

    assert token.isascii()
    assert len(token.encode("ascii")) <= DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES
    assert decoded == handle.delegated_identity
    assert decoded.expires_at == expires_at.astimezone(timezone.utc)
    assert decoded.expires_at.tzinfo is timezone.utc


def test_context_ids_are_opaque_and_fresh() -> None:
    codec = DeepResearchContextTokenCodec.create_process_local()

    first, _ = codec.issue_parent(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        expires_at=NOW + timedelta(minutes=5),
    )
    second, _ = codec.issue_parent(
        reservation=AccountingReservation(
            reservation_id="reservation-second",
            request_id="usage:v1:http:deep-research-second",
            api_key_id=7,
            reserved_usd=0.1,
        ),
        gateway_model="gateway/deep-research",
        expires_at=NOW + timedelta(minutes=5),
    )

    assert first.context_id != second.context_id
    assert len(first.context_id) == 32
    assert first.reservation.request_id not in first.context_id


@pytest.mark.parametrize("part", [1, 2, 3])
def test_context_token_tampering_fails_with_safe_error(part: int) -> None:
    codec = DeepResearchContextTokenCodec(b"s" * 32)
    _, token = codec.issue_parent(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        expires_at=NOW + timedelta(minutes=5),
    )
    parts = token.split(".")
    original = parts[part]
    parts[part] = ("a" if original[0] != "a" else "b") + original[1:]
    tampered = ".".join(parts)

    with pytest.raises(DeepResearchContextError) as error:
        codec.decode(tampered, now=NOW)

    assert error.value.code is DeepResearchContextErrorCode.INVALID
    assert str(error.value) == "invalid_deep_research_context"
    assert tampered not in str(error.value)


def test_context_token_is_expired_at_exact_utc_deadline() -> None:
    codec = DeepResearchContextTokenCodec(b"s" * 32)
    _, token = codec.issue_parent(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        expires_at=NOW,
    )

    with pytest.raises(DeepResearchContextError) as error:
        codec.decode(token, now=NOW)

    assert error.value.code is DeepResearchContextErrorCode.EXPIRED
    assert str(error.value) == "expired_deep_research_context"


@pytest.mark.parametrize(
    "token",
    [
        "dr1." + "а" * 32 + ".20260714T120000000000Z." + "0" * 64,
        "x" * (DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES + 1),
    ],
)
def test_context_token_rejects_non_ascii_and_oversize(token: str) -> None:
    codec = DeepResearchContextTokenCodec(b"s" * 32)

    with pytest.raises(DeepResearchContextError) as error:
        codec.decode(token, now=NOW)

    assert error.value.code is DeepResearchContextErrorCode.INVALID
    assert token not in str(error.value)


def test_child_seal_preserves_caller_order() -> None:
    first = _receipt("child-a", "a")
    second = _receipt("child-b", "b")

    seal = seal_deep_research_children(
        (second, first),
        aggregate_usage=AccountingUsage(cost=0.75),
    )

    assert seal.receipts == (second, first)
    assert seal.child_event_ids == (second.event_id, first.event_id)
    assert seal.child_fingerprints == (
        second.billing_fingerprint,
        first.billing_fingerprint,
    )
    assert seal.diagnostic_child_cost_usd == 0.75


def test_zero_cost_rollup_is_canonical_and_order_sensitive() -> None:
    first = _receipt("child-a", "a")
    second = _receipt("child-b", "b")
    parent = _parent()

    event = build_deep_research_rollup_event(
        parent,
        seal_deep_research_children(
            (first, second),
            aggregate_usage=AccountingUsage(cost=0.75),
        ),
        occurred_at=NOW,
    )
    same_billing_identity = build_deep_research_rollup_event(
        parent,
        seal_deep_research_children(
            (first, second),
            aggregate_usage=AccountingUsage(cost=0.75),
        ),
        occurred_at=NOW + timedelta(seconds=1),
    )
    reversed_children = build_deep_research_rollup_event(
        parent,
        seal_deep_research_children(
            (second, first),
            aggregate_usage=AccountingUsage(cost=0.75),
        ),
        occurred_at=NOW,
    )

    assert event.kind is AccountingEventKind.ROLLUP
    assert event.event_id == parent.reservation.request_id
    assert event.request_id == parent.reservation.request_id
    assert event.usage.cost == 0.0
    assert event.usage.total_tokens == 0
    assert event.components == ()
    assert event.cost_source is CostSource.RECEIPT_ROLLUP
    assert event.child_event_ids == (first.event_id, second.event_id)
    assert event.billing_fingerprint == same_billing_identity.billing_fingerprint
    assert event.billing_fingerprint != reversed_children.billing_fingerprint

    different_diagnostic_total = build_deep_research_rollup_event(
        parent,
        seal_deep_research_children(
            (first, second),
            aggregate_usage=AccountingUsage(cost=1.5),
        ),
        occurred_at=NOW,
    )
    assert (
        event.billing_fingerprint
        == different_diagnostic_total.billing_fingerprint
    )


def test_context_values_are_frozen_and_reprs_hide_identity() -> None:
    codec = DeepResearchContextTokenCodec(b"s" * 32)
    parent = _parent()
    identity = parent.delegated_identity
    seal = seal_deep_research_children(
        (_receipt("child-a", "a"),),
        aggregate_usage=AccountingUsage(cost=0.25),
    )

    with pytest.raises(FrozenInstanceError):
        parent.context_id = "b" * 32  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        identity.context_id = "b" * 32  # type: ignore[misc]
    with pytest.raises(FrozenInstanceError):
        seal.receipts = ()  # type: ignore[misc]

    assert repr(codec) == "<DeepResearchContextTokenCodec>"
    assert repr(parent) == "<DeepResearchParentHandle>"
    assert repr(identity) == "<DeepResearchDelegatedIdentity>"
    assert repr(seal) == "<DeepResearchChildSeal>"
    assert parent.context_id not in repr(parent)


def test_child_seal_rejects_empty_and_duplicate_receipts() -> None:
    receipt = _receipt("child-a", "a")

    with pytest.raises(AccountingValidationError) as empty_error:
        DeepResearchChildSeal((), aggregate_usage=AccountingUsage())
    with pytest.raises(AccountingValidationError) as duplicate_error:
        seal_deep_research_children(
            (receipt, receipt),
            aggregate_usage=AccountingUsage(),
        )

    assert str(empty_error.value) == "invalid_contract"
    assert str(duplicate_error.value) == "invalid_contract"


@pytest.mark.parametrize(
    "invalid_usage",
    [-0.1, float("nan"), float("inf"), True],
)
def test_child_seal_rejects_invalid_aggregate_usage(invalid_usage: object) -> None:
    with pytest.raises(AccountingValidationError):
        seal_deep_research_children(
            (_receipt("child-a", "a"),),
            aggregate_usage=invalid_usage,  # type: ignore[arg-type]
        )


def test_delegated_identity_contains_no_api_key_material() -> None:
    identity = DeepResearchDelegatedIdentity(
        context_id="a" * 32,
        expires_at=NOW + timedelta(minutes=5),
    )

    assert set(identity.__slots__) == {"context_id", "expires_at"}


def test_registry_seals_reverse_completions_in_admission_order() -> None:
    async def scenario() -> None:
        registry = DeepResearchAccountingRegistry(
            max_contexts=2,
            max_children_per_context=2,
            clock=lambda: NOW,
            codec=DeepResearchContextTokenCodec(b"s" * 32),
        )
        parent, token = registry.begin(
            reservation=_reservation(),
            gateway_model="gateway/deep-research",
            auth_identity=DeepResearchAuthIdentity(
                api_key_id=7,
                allowed_models=("gateway/model",),
            ),
        )
        first_prepared = registry.prepare_child(token, request_id="child-request-a")
        second_prepared = registry.prepare_child(token, request_id="child-request-b")
        first = registry.bind_child(
            first_prepared,
            AccountingReservation(
                reservation_id="child-reservation-a",
                request_id="child-request-a",
                api_key_id=7,
                reserved_usd=0.1,
            ),
        )
        second = registry.bind_child(
            second_prepared,
            AccountingReservation(
                reservation_id="child-reservation-b",
                request_id="child-request-b",
                api_key_id=7,
                reserved_usd=0.1,
            ),
        )
        first_event = _child_event(
            "child-a",
            request_id=first.reservation.request_id,
            parent_event_id=parent.rollup_event_id,
            cost=0.25,
        )
        second_event = _child_event(
            "child-b",
            request_id=second.reservation.request_id,
            parent_event_id=parent.rollup_event_id,
            cost=0.5,
        )
        second_receipt = AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id=second_event.event_id,
            billing_fingerprint=second_event.billing_fingerprint,
            usage_row_id=2,
        )
        first_receipt = AccountingReceipt(
            source_status=SourceStatus.ACCEPTED,
            projection_status=ProjectionStatus.APPLIED,
            event_id=first_event.event_id,
            billing_fingerprint=first_event.billing_fingerprint,
            usage_row_id=1,
        )

        registry.finish_child_commit(second.reservation, second_event, second_receipt)
        registry.finish_child_commit(first.reservation, first_event, first_receipt)
        sealed = await registry.seal(parent)

        assert sealed.receipts == (first_receipt, second_receipt)
        assert sealed.diagnostic_child_cost_usd == 0.75
        assert sealed.aggregate_usage.prompt_tokens == 2
        assert sealed.aggregate_usage.completion_tokens == 2
        assert sealed.aggregate_usage.total_tokens == 4
        with pytest.raises(DeepResearchContextError):
            registry.resolve(token)

    run_async(scenario())


def test_registry_cancel_waits_for_accepted_child_terminal() -> None:
    async def scenario() -> None:
        registry = DeepResearchAccountingRegistry(
            max_contexts=1,
            max_children_per_context=1,
            clock=lambda: NOW,
            codec=DeepResearchContextTokenCodec(b"s" * 32),
        )
        parent, token = registry.begin(
            reservation=_reservation(),
            gateway_model="gateway/deep-research",
            auth_identity=DeepResearchAuthIdentity(api_key_id=7),
        )
        prepared = registry.prepare_child(token, request_id="child-request")
        child = registry.bind_child(
            prepared,
            AccountingReservation(
                reservation_id="child-reservation",
                request_id="child-request",
                api_key_id=7,
                reserved_usd=0.1,
            ),
        )

        cancel_task = asyncio.create_task(registry.cancel(parent))
        await asyncio.sleep(0)
        assert not cancel_task.done()
        with pytest.raises(DeepResearchContextError):
            registry.prepare_child(token, request_id="late-child")

        registry.finish_child_release(child.reservation)
        await cancel_task
        assert not registry.has_context(parent)

    run_async(scenario())


def test_aborting_one_prepared_child_keeps_other_admission_stable() -> None:
    registry = DeepResearchAccountingRegistry(
        max_contexts=1,
        max_children_per_context=2,
        clock=lambda: NOW,
        codec=DeepResearchContextTokenCodec(b"s" * 32),
    )
    _parent_handle, token = registry.begin(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        auth_identity=DeepResearchAuthIdentity(api_key_id=7),
    )
    aborted = registry.prepare_child(token, request_id="child-request-a")
    retained = registry.prepare_child(token, request_id="child-request-b")

    registry.abort_child(aborted)
    admission = registry.bind_child(
        retained,
        AccountingReservation(
            reservation_id="child-reservation-b",
            request_id="child-request-b",
            api_key_id=7,
            reserved_usd=0.1,
        ),
    )

    assert admission.ordinal == 1
    assert admission.reservation.request_id == "child-request-b"


def test_auth_identity_rejects_unencodable_model_name() -> None:
    with pytest.raises(AccountingValidationError):
        DeepResearchAuthIdentity(api_key_id=7, allowed_models=("model\ud800",))


def test_auth_identity_preserves_duplicate_allowed_models() -> None:
    identity = DeepResearchAuthIdentity(
        api_key_id=7,
        allowed_models=("gateway/model", "gateway/model"),
    )

    assert identity.allowed_models == ("gateway/model", "gateway/model")


def test_registry_rejects_rollup_as_child_terminal_event() -> None:
    registry = DeepResearchAccountingRegistry(
        max_contexts=1,
        max_children_per_context=1,
        clock=lambda: NOW,
        codec=DeepResearchContextTokenCodec(b"s" * 32),
    )
    parent, token = registry.begin(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        auth_identity=DeepResearchAuthIdentity(api_key_id=7),
    )
    prepared = registry.prepare_child(token, request_id="child-request")
    child = registry.bind_child(
        prepared,
        AccountingReservation(
            reservation_id="child-reservation",
            request_id="child-request",
            api_key_id=7,
            reserved_usd=0.1,
        ),
    )
    event = AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id="child-rollup",
        kind=AccountingEventKind.ROLLUP,
        api_key_id=7,
        method="POST",
        route_template="/v1/chat/completions",
        operation="chat",
        gateway_model="gateway/model",
        provider=None,
        model=None,
        usage=AccountingUsage(),
        cost_source=CostSource.RECEIPT_ROLLUP,
        occurred_at=NOW,
        request_id=child.reservation.request_id,
        parent_event_id=parent.rollup_event_id,
        child_event_ids=("grandchild-event",),
        child_fingerprints=("f" * 64,),
    )
    receipt = AccountingReceipt(
        source_status=SourceStatus.ACCEPTED,
        projection_status=ProjectionStatus.APPLIED,
        event_id=event.event_id,
        billing_fingerprint=event.billing_fingerprint,
        usage_row_id=1,
    )

    with pytest.raises(AccountingValidationError):
        registry.finish_child_commit(child.reservation, event, receipt)


def test_registry_rejects_new_child_after_per_context_limit() -> None:
    registry = DeepResearchAccountingRegistry(
        max_contexts=1,
        max_children_per_context=1,
        clock=lambda: NOW,
        codec=DeepResearchContextTokenCodec(b"s" * 32),
    )
    _parent_handle, token = registry.begin(
        reservation=_reservation(),
        gateway_model="gateway/deep-research",
        auth_identity=DeepResearchAuthIdentity(api_key_id=7),
    )
    registry.prepare_child(token, request_id="child-request-a")

    with pytest.raises(AccountingError) as error:
        registry.prepare_child(token, request_id="child-request-b")

    assert error.value.code is AccountingErrorCode.ACTIVE_SESSION_LIMIT
