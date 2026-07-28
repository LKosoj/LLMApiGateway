"""One-shot parent and in-process child accounting for Deep Research."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable, Mapping
from datetime import datetime, timezone
from enum import StrEnum
from typing import TypeVar

from fastapi import Request

from ...db.api_keys_db import ApiKeyRecord
from ...middleware.accounting_admission import (
    AccountingRequestContext,
    get_accounting_request_context,
    publish_accounting_request_context,
    take_accounting_request_context,
)
from ...middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    ResponseStart,
    SSEEvent,
    TerminalReason,
    TerminalSignal,
    publish_response_observation,
    response_observation_published,
)
from ...services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingReservation,
    AccountingValidationError,
    OperationCostCalculator,
    classify_billing_policy,
)
from ...services.accounting_service import AccountingService
from ...services.active_requests import (
    ActiveRequestsRegistry,
    active_request_id,
)
from ...services.deep_research_accounting import (
    DeepResearchAuthIdentity,
    DeepResearchChildAdmission,
    DeepResearchChildSeal,
    DeepResearchParentHandle,
    build_deep_research_rollup_event,
)
from ...services.operation_accounting import (
    build_operation_accounting_event,
    parse_synthesized_operation_observation,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ...utils.usage_tracking import ModelCostRates


T = TypeVar("T")

logger = logging.getLogger(__name__)

# `admission_closed` is the one transient reason a child reservation fails: the
# accounting service blocks admission on a single failed source write and
# reopens as soon as the projector settles the event, which is usually well
# under a second. Child callbacks have no transport-level retry — the child's
# IPC client latches the first reported failure and every later search/read
# fails without reaching the gateway — so one blip would otherwise kill a
# research run that takes minutes. Every other AccountingError (budget, rate
# limit, child cap) is a real verdict and must stay terminal.
_CHILD_ADMISSION_ATTEMPTS = 3
_CHILD_ADMISSION_BACKOFF_SECONDS = 0.3


class _OwnerState(StrEnum):
    OPEN = "open"
    ACTIVE = "active"
    SEALING = "sealing"
    READY = "ready"
    COMMITTING = "committing"
    RELEASING = "releasing"
    CLOSED = "closed"


class DeepResearchTerminalOwner:
    """Own the parent reservation and every in-process child terminal path."""

    __slots__ = (
        "_accounting_service",
        "_active_request_id",
        "_active_requests_registry",
        "_auth_identity",
        "_context",
        "_cost_rate_registry",
        "_estimate_usd",
        "_handle",
        "_operation_cost_calculator_registry",
        "_rollup_event",
        "_state",
        "_token",
        "_transport_finished",
    )

    def __init__(
        self,
        *,
        accounting_service: AccountingService,
        context: AccountingRequestContext,
        auth_identity: DeepResearchAuthIdentity,
        estimate_usd: float,
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
        operation_cost_calculator_registry: Mapping[
            tuple[str, str], OperationCostCalculator
        ],
        active_requests_registry: ActiveRequestsRegistry,
        active_request_id: str | None,
    ) -> None:
        self._accounting_service = accounting_service
        self._context = context
        self._auth_identity = auth_identity
        self._estimate_usd = estimate_usd
        self._cost_rate_registry = cost_rate_registry
        self._operation_cost_calculator_registry = (
            operation_cost_calculator_registry
        )
        self._active_requests_registry = active_requests_registry
        self._active_request_id = active_request_id
        self._handle: DeepResearchParentHandle | None = None
        self._token: str | None = None
        self._rollup_event = None
        self._state = _OwnerState.OPEN
        self._transport_finished = False

    @property
    def token(self) -> str:
        if self._state is not _OwnerState.ACTIVE or self._token is None:
            raise AccountingValidationError
        return self._token

    @property
    def is_closed(self) -> bool:
        return self._state is _OwnerState.CLOSED

    @property
    def is_ready(self) -> bool:
        return self._state is _OwnerState.READY

    def begin(self, gateway_model: str) -> str:
        if self._state is not _OwnerState.OPEN:
            raise AccountingValidationError
        handle, token = self._accounting_service.begin_deep_research_parent(
            self._context.reservation,
            gateway_model=gateway_model,
            auth_identity=self._auth_identity,
        )
        self._handle = handle
        self._token = token
        self._state = _OwnerState.ACTIVE
        return token

    async def _release_after_failed_commit(
        self,
        reservation: AccountingReservation,
    ) -> None:
        try:
            await self._accounting_service.release(reservation)
        except BaseException:
            pass

    async def _reserve_child(
        self,
        *,
        request_id: str,
        route_template: str,
    ) -> DeepResearchChildAdmission:
        # Retrying the same request_id is safe: reserve_deep_research_child()
        # aborts the prepared slot before it propagates the failure, so no
        # child slot leaks into the per-run cap.
        attempt = 1
        while True:
            try:
                return await self._accounting_service.reserve_deep_research_child(
                    self.token,
                    request_id=request_id,
                    estimate_usd=self._estimate_usd,
                )
            except AccountingError as exc:
                if (
                    exc.code is not AccountingErrorCode.ADMISSION_CLOSED
                    or attempt >= _CHILD_ADMISSION_ATTEMPTS
                ):
                    raise
                logger.warning(
                    "Deep research child admission for %s is closed; "
                    "retrying (%s/%s).",
                    route_template,
                    attempt,
                    _CHILD_ADMISSION_ATTEMPTS - 1,
                )
                await asyncio.sleep(_CHILD_ADMISSION_BACKOFF_SECONDS * attempt)
                attempt += 1

    async def run_flat_operation_child(
        self,
        *,
        route_template: str,
        gateway_model: str,
        work: Callable[[], Awaitable[T]],
    ) -> T:
        if self._state is not _OwnerState.ACTIVE:
            raise AccountingValidationError
        policy = classify_billing_policy("POST", route_template)
        if policy is None or policy.operation not in {"web_search", "web_read"}:
            raise AccountingValidationError
        request_id = str(uuid.uuid4())
        admission = await self._reserve_child(
            request_id=request_id,
            route_template=route_template,
        )
        context = AccountingRequestContext(
            method="POST",
            route_template=route_template,
            policy=policy,
            request_id=request_id,
            reservation=admission.reservation,
            parent_event_id=admission.parent_event_id,
        )
        started_at = time.monotonic()
        try:
            result = await work()
            observation = parse_synthesized_operation_observation(
                {},
                policy=policy,
                gateway_model=gateway_model,
                provider=None,
                model=None,
                cost_rate_registry=self._cost_rate_registry,
                operation_cost_calculator_registry=(
                    self._operation_cost_calculator_registry
                ),
                occurred_at=datetime.now(timezone.utc),
                duration_ms=int((time.monotonic() - started_at) * 1000),
            )
            event = build_operation_accounting_event(context, observation)
        except BaseException:
            try:
                await self._accounting_service.release(admission.reservation)
            except BaseException:
                pass
            raise

        try:
            receipt = await self._accounting_service.commit(
                admission.reservation,
                event,
            )
            if not isinstance(receipt, AccountingReceipt):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        except BaseException:
            await self._release_after_failed_commit(admission.reservation)
            raise
        return result

    async def seal_for_response(self) -> DeepResearchChildSeal:
        if self._state is not _OwnerState.ACTIVE or self._handle is None:
            raise AccountingValidationError
        self._state = _OwnerState.SEALING
        try:
            seal = await self._accounting_service.seal_deep_research_children(
                self._handle
            )
        except BaseException:
            self._state = _OwnerState.ACTIVE
            raise
        self._rollup_event = build_deep_research_rollup_event(
            self._handle,
            seal,
            occurred_at=datetime.now(timezone.utc),
        )
        self._state = _OwnerState.READY
        return seal

    @staticmethod
    def _response_status_code(response_start: ResponseStart) -> int:
        if not isinstance(response_start, ResponseStart):
            raise AccountingValidationError
        return response_start.status_code

    async def _classify_json(
        self,
        response_start: ResponseStart,
        _body: bytes,
    ) -> TerminalReason:
        status_code = self._response_status_code(response_start)
        if not 200 <= status_code < 300:
            return TerminalReason.UPSTREAM_ERROR
        if not self.is_ready or self._rollup_event is None:
            return TerminalReason.PROTOCOL_ERROR
        return TerminalReason.COMPLETE

    async def _classify_sse(
        self,
        response_start: ResponseStart,
        event: SSEEvent,
    ) -> TerminalReason | None:
        if not isinstance(event, SSEEvent):
            raise AccountingValidationError
        if not 200 <= self._response_status_code(response_start) < 300:
            return TerminalReason.UPSTREAM_ERROR
        return TerminalReason.PROTOCOL_ERROR

    async def _classify_opaque(
        self,
        response_start: ResponseStart,
    ) -> TerminalReason:
        if not 200 <= self._response_status_code(response_start) < 300:
            return TerminalReason.UPSTREAM_ERROR
        return TerminalReason.PROTOCOL_ERROR

    async def _commit_rollup(self) -> AccountingReceipt:
        if self._state is not _OwnerState.READY or self._rollup_event is None:
            raise AccountingValidationError
        self._state = _OwnerState.COMMITTING
        try:
            receipt = await self._accounting_service.commit(
                self._context.reservation,
                self._rollup_event,
            )
            if not isinstance(receipt, AccountingReceipt):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        except BaseException:
            await self._release_after_failed_commit(self._context.reservation)
            raise
        finally:
            self._state = _OwnerState.CLOSED
        return receipt

    async def _finalize(self, signal: TerminalSignal) -> object:
        if not isinstance(signal, TerminalSignal):
            raise AccountingValidationError
        if signal.reason is TerminalReason.COMPLETE:
            return await self._commit_rollup()
        await self._release_if_open()
        return False

    async def _finish_transport(self, _result: object) -> None:
        if self._transport_finished:
            return
        self._transport_finished = True
        self._active_requests_registry.finish(self._active_request_id)

    async def _release_if_open(self) -> None:
        if self._state in {
            _OwnerState.CLOSED,
            _OwnerState.COMMITTING,
            _OwnerState.RELEASING,
        }:
            return
        previous_state = self._state
        self._state = _OwnerState.RELEASING
        cleanup_error: BaseException | None = None
        if previous_state in {_OwnerState.ACTIVE, _OwnerState.SEALING} and self._handle is not None:
            try:
                await self._accounting_service.cancel_deep_research_children(
                    self._handle
                )
            except BaseException as exc:
                cleanup_error = exc
        try:
            released = await self._accounting_service.release(
                self._context.reservation
            )
            if not isinstance(released, bool) or not released:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
        except BaseException as exc:
            if cleanup_error is None:
                cleanup_error = exc
        finally:
            self._state = _OwnerState.CLOSED
        if cleanup_error is not None:
            raise cleanup_error

    async def release_if_open(
        self,
        *,
        primary_error: BaseException | None = None,
    ) -> None:
        try:
            await self._release_if_open()
        except BaseException:
            if primary_error is None:
                raise

    def __repr__(self) -> str:
        return "<DeepResearchTerminalOwner>"


def take_deep_research_terminal_owner(request: Request) -> DeepResearchTerminalOwner:
    if not isinstance(request, Request):
        raise AccountingValidationError
    try:
        services = request.app.state.services
        snapshot = request.state.runtime_snapshot
    except (AttributeError, KeyError, TypeError):
        raise AccountingValidationError from None
    if not isinstance(services, AppServices) or not isinstance(snapshot, RuntimeSnapshot):
        raise AccountingValidationError
    accounting_service = services.accounting_service
    if not isinstance(accounting_service, AccountingService):
        raise AccountingValidationError
    context = get_accounting_request_context(request.scope)
    if context is None or context.policy.operation != "web_deep_research":
        raise AccountingValidationError
    if response_observation_published(request.scope):
        raise AccountingValidationError

    record = getattr(request.state, "api_key_record", None)
    if context.reservation.api_key_id is None:
        if record is not None:
            raise AccountingValidationError
        auth_identity = DeepResearchAuthIdentity(api_key_id=None)
    else:
        if not isinstance(record, ApiKeyRecord) or record.id != context.reservation.api_key_id:
            raise AccountingValidationError
        auth_identity = DeepResearchAuthIdentity(
            api_key_id=record.id,
            allowed_models=tuple(record.allowed_models),
        )
    taken = take_accounting_request_context(request.scope)
    if taken is not context:
        raise AccountingValidationError
    owner = DeepResearchTerminalOwner(
        accounting_service=accounting_service,
        context=context,
        auth_identity=auth_identity,
        estimate_usd=services.usd_budget_ledger.default_estimate_usd,
        cost_rate_registry=snapshot.cost_rate_registry,
        operation_cost_calculator_registry=(
            snapshot.operation_cost_calculator_registry
        ),
        active_requests_registry=services.active_requests_registry,
        active_request_id=active_request_id(request),
    )
    try:
        publish_response_observation(
            request.scope,
            ResponseObservation(
                finalizer=ResponseFinalizer(
                    services.task_supervisor,
                    owner._finalize,
                    abort=owner.release_if_open,
                ),
                classify_json=owner._classify_json,
                classify_sse=owner._classify_sse,
                classify_opaque=owner._classify_opaque,
                transport_finished=owner._finish_transport,
            ),
        )
    except BaseException:
        publish_accounting_request_context(request.scope, context)
        raise
    return owner
