"""Terminal observation owner for non-chat accounting."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from enum import StrEnum

from fastapi import Request
from starlette.responses import Response

from ...middleware.accounting_admission import (
    AccountingRequestContext,
    get_accounting_request_context,
    publish_accounting_request_context,
    take_accounting_request_context,
)
from ...middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    TerminalReason,
    TerminalSignal,
    publish_response_observation,
    response_observation_published,
)
from ...services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReceipt,
    AccountingValidationError,
    OperationCostCalculator,
)
from ...services.accounting_service import AccountingService
from ...services.active_requests import ActiveRequestsRegistry, active_request_id
from ...services.operation_accounting import (
    OperationEventProvenance,
    OperationTerminalObservation,
    build_operation_accounting_event,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot
from ...services.stream_observation import SSEEvent
from ...utils.usage_tracking import ModelCostRates


class _OwnerState(StrEnum):
    OPEN = "open"
    COMMITTING = "committing"
    RELEASING = "releasing"
    CLOSED = "closed"


class OperationTerminalOwner:
    """One-shot owner of one admitted non-chat accounting reservation."""

    __slots__ = (
        "_accounting_service",
        "_context",
        "_cost_rate_registry",
        "_operation_cost_calculator_registry",
        "_active_request_id",
        "_active_requests_registry",
        "_provenance",
        "_released_without_charge",
        "_state",
        "_stream_observer",
        "_terminal_observation",
        "_transport_finished",
    )

    def __init__(
        self,
        *,
        accounting_service: AccountingService,
        context: AccountingRequestContext,
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
        operation_cost_calculator_registry: Mapping[
            tuple[str, str], OperationCostCalculator
        ],
        active_requests_registry: ActiveRequestsRegistry,
        active_request_id: str | None,
    ) -> None:
        self._accounting_service = accounting_service
        self._context = context
        self._cost_rate_registry = cost_rate_registry
        self._operation_cost_calculator_registry = operation_cost_calculator_registry
        self._active_requests_registry = active_requests_registry
        self._active_request_id = active_request_id
        self._provenance: OperationEventProvenance | None = None
        self._released_without_charge = False
        self._state = _OwnerState.OPEN
        self._stream_observer: (
            Callable[[SSEEvent], OperationTerminalObservation | None] | None
        ) = None
        self._terminal_observation: OperationTerminalObservation | None = None
        self._transport_finished = False

    @property
    def accounting_service(self) -> AccountingService:
        return self._accounting_service

    @property
    def context(self) -> AccountingRequestContext:
        return self._context

    @property
    def cost_rate_registry(self) -> Mapping[tuple[str, str], ModelCostRates]:
        return self._cost_rate_registry

    @property
    def operation_cost_calculator_registry(
        self,
    ) -> Mapping[tuple[str, str], OperationCostCalculator]:
        return self._operation_cost_calculator_registry

    def _require_open(self) -> None:
        if self._state is not _OwnerState.OPEN:
            raise AccountingValidationError

    def _bind_terminal_observation(
        self,
        observation: OperationTerminalObservation,
        *,
        provenance: OperationEventProvenance | None,
    ) -> None:
        self._require_open()
        if (
            not isinstance(observation, OperationTerminalObservation)
            or self._terminal_observation is not None
            or self._stream_observer is not None
        ):
            raise AccountingValidationError
        if provenance is not None and not isinstance(
            provenance,
            OperationEventProvenance,
        ):
            raise AccountingValidationError
        self._terminal_observation = observation
        self._provenance = provenance

    def _bind_stream_observer(
        self,
        observer: Callable[[SSEEvent], OperationTerminalObservation | None],
        *,
        provenance: OperationEventProvenance | None,
    ) -> None:
        self._require_open()
        if (
            not callable(observer)
            or self._stream_observer is not None
            or self._terminal_observation is not None
        ):
            raise AccountingValidationError
        if provenance is not None and not isinstance(
            provenance,
            OperationEventProvenance,
        ):
            raise AccountingValidationError
        self._stream_observer = observer
        self._provenance = provenance

    async def _commit(
        self,
        observation: OperationTerminalObservation,
        *,
        provenance: OperationEventProvenance | None,
    ) -> AccountingReceipt:
        self._require_open()
        event = build_operation_accounting_event(
            self._context,
            observation,
            provenance=provenance,
        )
        self._state = _OwnerState.COMMITTING
        try:
            receipt = await self._accounting_service.commit(
                self._context.reservation,
                event,
            )
            if not isinstance(receipt, AccountingReceipt):
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            return receipt
        finally:
            self._state = _OwnerState.CLOSED

    async def _release(self) -> bool:
        self._require_open()
        self._state = _OwnerState.RELEASING
        try:
            released = await self._accounting_service.release(
                self._context.reservation
            )
            if not isinstance(released, bool):
                raise AccountingValidationError
            if not released:
                raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED)
            self._released_without_charge = True
            return released
        finally:
            self._state = _OwnerState.CLOSED

    async def _release_if_open(self) -> None:
        if self._state is _OwnerState.OPEN:
            await self._release()

    @staticmethod
    def _response_status_code(response_start: object) -> int:
        status_code = getattr(response_start, "status_code", None)
        if (
            isinstance(status_code, bool)
            or not isinstance(status_code, int)
            or not 100 <= status_code <= 599
        ):
            raise AccountingValidationError
        return status_code

    def _classify_bound_response(self, response_start: object) -> TerminalReason:
        if self._response_status_code(response_start) >= 400:
            return TerminalReason.UPSTREAM_ERROR
        if self._terminal_observation is None and not self._released_without_charge:
            return TerminalReason.PROTOCOL_ERROR
        return TerminalReason.COMPLETE

    async def _classify_json(
        self,
        response_start: object,
        _body: bytes,
    ) -> TerminalReason:
        return self._classify_bound_response(response_start)

    async def _classify_opaque(self, response_start: object) -> TerminalReason:
        return self._classify_bound_response(response_start)

    async def _classify_sse(
        self,
        response_start: object,
        event: SSEEvent,
    ) -> TerminalReason | None:
        if self._response_status_code(response_start) >= 400:
            return TerminalReason.UPSTREAM_ERROR
        if not isinstance(event, SSEEvent) or self._stream_observer is None:
            raise AccountingValidationError
        observation = self._stream_observer(event)
        if observation is None:
            return None
        if (
            not isinstance(observation, OperationTerminalObservation)
            or self._terminal_observation is not None
        ):
            raise AccountingValidationError
        self._terminal_observation = observation
        return TerminalReason.COMPLETE

    async def _finalize(self, signal: TerminalSignal) -> object:
        if not isinstance(signal, TerminalSignal):
            raise AccountingValidationError
        if signal.reason is TerminalReason.COMPLETE:
            if self._terminal_observation is not None:
                try:
                    return await self._commit(
                        self._terminal_observation,
                        provenance=self._provenance,
                    )
                except BaseException:
                    try:
                        await self._release_if_open()
                    except BaseException:
                        pass
                    raise
            if self._released_without_charge:
                return True
            raise AccountingValidationError
        await self._release_if_open()
        return False

    async def _finish_transport(self, _result: object) -> None:
        if self._transport_finished:
            return
        self._transport_finished = True
        self._active_requests_registry.finish(self._active_request_id)

    def __repr__(self) -> str:
        return "<OperationTerminalOwner>"


def take_operation_terminal_owner(request: Request) -> OperationTerminalOwner:
    """Capture the exact process/runtime generation and consume its context."""
    if not isinstance(request, Request):
        raise AccountingValidationError
    try:
        services = request.app.state.services
        snapshot = request.state.runtime_snapshot
    except (AttributeError, KeyError, TypeError):
        raise AccountingValidationError from None
    if not isinstance(services, AppServices) or not isinstance(snapshot, RuntimeSnapshot):
        raise AccountingValidationError
    if not isinstance(services.accounting_service, AccountingService):
        raise AccountingValidationError
    if response_observation_published(request.scope):
        raise AccountingValidationError

    context = get_accounting_request_context(request.scope)
    if context is None or context.policy.operation == "chat":
        raise AccountingValidationError
    taken = take_accounting_request_context(request.scope)
    if taken is not context:
        raise AccountingValidationError
    owner = OperationTerminalOwner(
        accounting_service=services.accounting_service,
        context=context,
        cost_rate_registry=snapshot.cost_rate_registry,
        operation_cost_calculator_registry=(
            snapshot.operation_cost_calculator_registry
        ),
        active_requests_registry=services.active_requests_registry,
        active_request_id=active_request_id(request),
    )
    try:
        finalizer = ResponseFinalizer(
            services.task_supervisor,
            owner._finalize,
            abort=owner._release_if_open,
        )
        publish_response_observation(
            request.scope,
            ResponseObservation(
                finalizer=finalizer,
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


async def release_operation(owner: OperationTerminalOwner) -> bool:
    if not isinstance(owner, OperationTerminalOwner):
        raise AccountingValidationError
    return await owner._release()


async def release_operation_if_open(
    owner: OperationTerminalOwner,
    *,
    primary_error: BaseException | None = None,
) -> None:
    """Release an open owner without replacing an in-flight primary error."""
    if not isinstance(owner, OperationTerminalOwner):
        raise AccountingValidationError
    if primary_error is not None and not isinstance(primary_error, BaseException):
        raise AccountingValidationError
    try:
        await owner._release_if_open()
    except BaseException:
        if primary_error is None:
            raise


async def finalize_buffered_operation(
    owner: OperationTerminalOwner,
    response: Response,
    observation: OperationTerminalObservation,
    *,
    provenance: OperationEventProvenance | None = None,
) -> Response:
    """Bind a trusted materialized observation to the ASGI finalizer."""
    if not isinstance(owner, OperationTerminalOwner):
        raise AccountingValidationError
    owner._require_open()
    try:
        if not isinstance(response, Response):
            raise AccountingValidationError
        owner._bind_terminal_observation(observation, provenance=provenance)
    except BaseException as exc:
        await release_operation_if_open(owner, primary_error=exc)
        raise
    return response


def bind_streaming_operation(
    owner: OperationTerminalOwner,
    terminal_observer: Callable[
        [SSEEvent],
        OperationTerminalObservation | None,
    ],
    *,
    provenance: OperationEventProvenance | None = None,
) -> None:
    """Bind the canonical SSE event observer without wrapping the response."""
    if (
        not isinstance(owner, OperationTerminalOwner)
        or not callable(terminal_observer)
    ):
        raise AccountingValidationError
    owner._bind_stream_observer(
        terminal_observer,
        provenance=provenance,
    )
