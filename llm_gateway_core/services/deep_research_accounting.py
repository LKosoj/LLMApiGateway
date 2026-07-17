"""Pure parent/child accounting contracts for deep research."""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import StrEnum

from .accounting import (
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
    _normalize_gateway_model,
    _normalize_utc_datetime,
)


DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES = 256
DEEP_RESEARCH_CONTEXT_TOKEN_PREFIX = "dr1."

_CONTEXT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{32}$")
_EXPIRY_RE = re.compile(r"^[0-9]{8}T[0-9]{12}Z$")
_SIGNATURE_RE = re.compile(r"^[0-9a-f]{64}$")
_TOKEN_VERSION = DEEP_RESEARCH_CONTEXT_TOKEN_PREFIX[:-1]
_PROCESS_SECRET_BYTES = 32
_DEFAULT_CONTEXT_TTL_SECONDS = 3600.0


class DeepResearchContextErrorCode(StrEnum):
    INVALID = "invalid_deep_research_context"
    EXPIRED = "expired_deep_research_context"


class DeepResearchContextError(RuntimeError):
    """Stable, credential-safe context-token failure."""

    def __init__(self, code: DeepResearchContextErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


def _invalid_context() -> None:
    raise DeepResearchContextError(DeepResearchContextErrorCode.INVALID) from None


def _normalize_context_id(value: object) -> str:
    if not isinstance(value, str) or _CONTEXT_ID_RE.fullmatch(value) is None:
        raise AccountingValidationError from None
    return value


def _normalize_request_id(value: object) -> str:
    if (
        not isinstance(value, str)
        or not value
        or value != value.strip()
        or len(value.encode("ascii", errors="ignore")) != len(value)
        or len(value.encode("ascii")) > 255
        or any(not 0x20 <= ord(character) <= 0x7E for character in value)
    ):
        raise AccountingValidationError
    return value


def _format_expiry(value: datetime) -> str:
    return (
        f"{value.year:04d}{value.month:02d}{value.day:02d}T"
        f"{value.hour:02d}{value.minute:02d}{value.second:02d}"
        f"{value.microsecond:06d}Z"
    )


def _parse_expiry(value: str) -> datetime:
    if _EXPIRY_RE.fullmatch(value) is None:
        _invalid_context()
    try:
        return datetime(
            year=int(value[0:4]),
            month=int(value[4:6]),
            day=int(value[6:8]),
            hour=int(value[9:11]),
            minute=int(value[11:13]),
            second=int(value[13:15]),
            microsecond=int(value[15:21]),
            tzinfo=timezone.utc,
        )
    except ValueError:
        _invalid_context()


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchDelegatedIdentity:
    """Only the opaque lookup identity that may cross a process boundary."""

    context_id: str
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_id", _normalize_context_id(self.context_id))
        object.__setattr__(
            self,
            "expires_at",
            _normalize_utc_datetime(self.expires_at),
        )

    def __repr__(self) -> str:
        return "<DeepResearchDelegatedIdentity>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchAuthIdentity:
    """Sanitized caller identity retained only in the parent process."""

    api_key_id: int | None
    allowed_models: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.api_key_id is not None and (
            isinstance(self.api_key_id, bool)
            or not isinstance(self.api_key_id, int)
            or self.api_key_id <= 0
        ):
            raise AccountingValidationError
        try:
            allowed_models = tuple(self.allowed_models)
        except TypeError:
            raise AccountingValidationError from None
        for model in allowed_models:
            if not isinstance(model, str):
                raise AccountingValidationError
            try:
                encoded_model = model.encode("utf-8")
            except UnicodeEncodeError:
                raise AccountingValidationError from None
            if not model or model != model.strip() or len(encoded_model) > 255:
                raise AccountingValidationError
        object.__setattr__(self, "allowed_models", allowed_models)

    def __repr__(self) -> str:
        return "<DeepResearchAuthIdentity>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchParentHandle:
    """Process-local parent ownership; never serialize this value."""

    context_id: str
    reservation: AccountingReservation
    gateway_model: str
    expires_at: datetime

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_id", _normalize_context_id(self.context_id))
        if not isinstance(self.reservation, AccountingReservation):
            raise AccountingValidationError
        object.__setattr__(
            self,
            "gateway_model",
            _normalize_gateway_model(self.gateway_model),
        )
        object.__setattr__(
            self,
            "expires_at",
            _normalize_utc_datetime(self.expires_at),
        )

    @property
    def delegated_identity(self) -> DeepResearchDelegatedIdentity:
        return DeepResearchDelegatedIdentity(
            context_id=self.context_id,
            expires_at=self.expires_at,
        )

    @property
    def rollup_event_id(self) -> str:
        return self.reservation.request_id

    def __repr__(self) -> str:
        return "<DeepResearchParentHandle>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchResolvedContext:
    context_id: str
    parent_event_id: str
    auth_identity: DeepResearchAuthIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_id", _normalize_context_id(self.context_id))
        object.__setattr__(
            self,
            "parent_event_id",
            _normalize_request_id(self.parent_event_id),
        )
        if not isinstance(self.auth_identity, DeepResearchAuthIdentity):
            raise AccountingValidationError

    def __repr__(self) -> str:
        return "<DeepResearchResolvedContext>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchPreparedChild:
    context_id: str
    request_id: str
    ordinal: int
    parent_event_id: str
    auth_identity: DeepResearchAuthIdentity

    def __post_init__(self) -> None:
        object.__setattr__(self, "context_id", _normalize_context_id(self.context_id))
        object.__setattr__(self, "request_id", _normalize_request_id(self.request_id))
        object.__setattr__(
            self,
            "parent_event_id",
            _normalize_request_id(self.parent_event_id),
        )
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
            or not isinstance(self.auth_identity, DeepResearchAuthIdentity)
        ):
            raise AccountingValidationError

    def __repr__(self) -> str:
        return "<DeepResearchPreparedChild>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchChildAdmission:
    reservation: AccountingReservation
    parent_event_id: str
    ordinal: int

    def __post_init__(self) -> None:
        if not isinstance(self.reservation, AccountingReservation):
            raise AccountingValidationError
        object.__setattr__(
            self,
            "parent_event_id",
            _normalize_request_id(self.parent_event_id),
        )
        if (
            isinstance(self.ordinal, bool)
            or not isinstance(self.ordinal, int)
            or self.ordinal < 0
        ):
            raise AccountingValidationError

    def __repr__(self) -> str:
        return "<DeepResearchChildAdmission>"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchChildSeal:
    """Immutable, ordinal-sensitive child receipts accepted by the parent."""

    receipts: tuple[AccountingReceipt, ...]
    aggregate_usage: AccountingUsage

    def __post_init__(self) -> None:
        try:
            receipts = tuple(self.receipts)
        except TypeError:
            raise AccountingValidationError from None
        if not receipts or any(
            not isinstance(receipt, AccountingReceipt) for receipt in receipts
        ):
            raise AccountingValidationError
        event_ids = tuple(receipt.event_id for receipt in receipts)
        if len(set(event_ids)) != len(event_ids):
            raise AccountingValidationError
        object.__setattr__(self, "receipts", receipts)
        if not isinstance(self.aggregate_usage, AccountingUsage):
            raise AccountingValidationError

    @property
    def diagnostic_child_cost_usd(self) -> float:
        return self.aggregate_usage.cost

    @property
    def child_event_ids(self) -> tuple[str, ...]:
        return tuple(receipt.event_id for receipt in self.receipts)

    @property
    def child_fingerprints(self) -> tuple[str, ...]:
        return tuple(receipt.billing_fingerprint for receipt in self.receipts)

    def __repr__(self) -> str:
        return "<DeepResearchChildSeal>"


def seal_deep_research_children(
    receipts: Iterable[AccountingReceipt],
    *,
    aggregate_usage: AccountingUsage,
) -> DeepResearchChildSeal:
    """Freeze child receipts without changing their caller-defined order."""
    try:
        normalized = tuple(receipts)
    except TypeError:
        raise AccountingValidationError from None
    return DeepResearchChildSeal(
        normalized,
        aggregate_usage=aggregate_usage,
    )


def aggregate_deep_research_child_usage(
    usages: Iterable[AccountingUsage],
) -> AccountingUsage:
    try:
        normalized = tuple(usages)
    except TypeError:
        raise AccountingValidationError from None
    if not normalized or any(
        not isinstance(usage, AccountingUsage) for usage in normalized
    ):
        raise AccountingValidationError
    try:
        cost = math.fsum(usage.cost for usage in normalized)
        cost_saved = math.fsum(usage.cost_saved for usage in normalized)
    except (OverflowError, ValueError):
        raise AccountingValidationError from None
    return AccountingUsage(
        prompt_tokens=sum(usage.prompt_tokens for usage in normalized),
        completion_tokens=sum(usage.completion_tokens for usage in normalized),
        total_tokens=sum(usage.total_tokens for usage in normalized),
        reasoning_tokens=sum(usage.reasoning_tokens for usage in normalized),
        cached_tokens=sum(usage.cached_tokens for usage in normalized),
        cost=cost,
        cost_saved=cost_saved,
        is_estimated=any(usage.is_estimated for usage in normalized),
    )


class DeepResearchContextTokenCodec:
    """HMAC codec backed by one process-local random secret."""

    __slots__ = ("__process_secret",)

    def __init__(self, process_secret: bytes) -> None:
        if not isinstance(process_secret, bytes) or len(process_secret) != _PROCESS_SECRET_BYTES:
            raise AccountingValidationError
        self.__process_secret = process_secret

    @classmethod
    def create_process_local(cls) -> DeepResearchContextTokenCodec:
        return cls(secrets.token_bytes(_PROCESS_SECRET_BYTES))

    def issue_parent(
        self,
        *,
        reservation: AccountingReservation,
        gateway_model: str,
        expires_at: datetime,
    ) -> tuple[DeepResearchParentHandle, str]:
        handle = DeepResearchParentHandle(
            context_id=secrets.token_urlsafe(24),
            reservation=reservation,
            gateway_model=gateway_model,
            expires_at=expires_at,
        )
        return handle, self.encode(handle.delegated_identity)

    def encode(self, identity: DeepResearchDelegatedIdentity) -> str:
        if not isinstance(identity, DeepResearchDelegatedIdentity):
            raise AccountingValidationError
        unsigned = ".".join(
            (
                _TOKEN_VERSION,
                identity.context_id,
                _format_expiry(identity.expires_at),
            )
        )
        signature = hmac.new(
            self.__process_secret,
            unsigned.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        token = f"{unsigned}.{signature}"
        if len(token) > DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES:
            raise AccountingValidationError
        return token

    def decode(
        self,
        token: str,
        *,
        now: datetime,
    ) -> DeepResearchDelegatedIdentity:
        if not isinstance(token, str) or len(token) > DEEP_RESEARCH_CONTEXT_TOKEN_MAX_BYTES:
            _invalid_context()
        try:
            token.encode("ascii")
        except UnicodeEncodeError:
            _invalid_context()
        parts = token.split(".")
        if len(parts) != 4 or parts[0] != _TOKEN_VERSION:
            _invalid_context()
        _, context_id, encoded_expiry, supplied_signature = parts
        if (
            _CONTEXT_ID_RE.fullmatch(context_id) is None
            or _SIGNATURE_RE.fullmatch(supplied_signature) is None
        ):
            _invalid_context()
        expires_at = _parse_expiry(encoded_expiry)
        unsigned = ".".join(parts[:3])
        expected_signature = hmac.new(
            self.__process_secret,
            unsigned.encode("ascii"),
            hashlib.sha256,
        ).hexdigest()
        if not hmac.compare_digest(supplied_signature, expected_signature):
            _invalid_context()

        normalized_now = _normalize_utc_datetime(now)
        if normalized_now >= expires_at:
            raise DeepResearchContextError(DeepResearchContextErrorCode.EXPIRED)
        return DeepResearchDelegatedIdentity(
            context_id=context_id,
            expires_at=expires_at,
        )

    def __repr__(self) -> str:
        return "<DeepResearchContextTokenCodec>"


@dataclass(slots=True)
class _ChildSlot:
    prepared: DeepResearchPreparedChild
    reservation: AccountingReservation | None = None
    receipt: AccountingReceipt | None = None
    usage: AccountingUsage | None = None
    terminal: bool = False


@dataclass(slots=True)
class _ParentState:
    handle: DeepResearchParentHandle
    token: str
    auth_identity: DeepResearchAuthIdentity
    drained: asyncio.Event
    accepting: bool = True
    next_ordinal: int = 0
    children: list[_ChildSlot] | None = None
    by_request: dict[str, _ChildSlot] | None = None

    def __post_init__(self) -> None:
        self.children = []
        self.by_request = {}
        self.drained.set()


class DeepResearchAccountingRegistry:
    """Bounded process-local collector for one parent and its child sessions."""

    def __init__(
        self,
        *,
        max_contexts: int,
        max_children_per_context: int,
        clock,
        ttl_seconds: float = _DEFAULT_CONTEXT_TTL_SECONDS,
        codec: DeepResearchContextTokenCodec | None = None,
    ) -> None:
        if (
            isinstance(max_contexts, bool)
            or not isinstance(max_contexts, int)
            or max_contexts <= 0
            or isinstance(max_children_per_context, bool)
            or not isinstance(max_children_per_context, int)
            or max_children_per_context <= 0
            or isinstance(ttl_seconds, bool)
            or not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(float(ttl_seconds))
            or float(ttl_seconds) <= 0
            or not callable(clock)
        ):
            raise AccountingValidationError
        self._max_contexts = max_contexts
        self._max_children_per_context = max_children_per_context
        self._clock = clock
        self._ttl_seconds = float(ttl_seconds)
        self._codec = codec or DeepResearchContextTokenCodec.create_process_local()
        self._lock = threading.Lock()
        self._contexts: dict[str, _ParentState] = {}
        self._by_parent_reservation: dict[str, str] = {}
        self._by_child_reservation: dict[str, _ChildSlot] = {}

    def _now(self) -> datetime:
        return _normalize_utc_datetime(self._clock())

    def begin(
        self,
        *,
        reservation: AccountingReservation,
        gateway_model: str,
        auth_identity: DeepResearchAuthIdentity,
    ) -> tuple[DeepResearchParentHandle, str]:
        if (
            not isinstance(reservation, AccountingReservation)
            or not isinstance(auth_identity, DeepResearchAuthIdentity)
            or reservation.api_key_id != auth_identity.api_key_id
        ):
            raise AccountingValidationError
        with self._lock:
            if reservation.reservation_id in self._by_parent_reservation:
                raise AccountingValidationError
            if len(self._contexts) >= self._max_contexts:
                raise AccountingValidationError
            handle, token = self._codec.issue_parent(
                reservation=reservation,
                gateway_model=gateway_model,
                expires_at=self._now() + timedelta(seconds=self._ttl_seconds),
            )
            state = _ParentState(
                handle=handle,
                token=token,
                auth_identity=auth_identity,
                drained=asyncio.Event(),
            )
            self._contexts[handle.context_id] = state
            self._by_parent_reservation[reservation.reservation_id] = handle.context_id
        return handle, token

    def resolve(self, token: str) -> DeepResearchResolvedContext:
        delegated = self._codec.decode(token, now=self._now())
        with self._lock:
            state = self._contexts.get(delegated.context_id)
            if (
                state is None
                or not state.accepting
                or state.token != token
                or state.handle.expires_at != delegated.expires_at
            ):
                _invalid_context()
            return DeepResearchResolvedContext(
                context_id=delegated.context_id,
                parent_event_id=state.handle.rollup_event_id,
                auth_identity=state.auth_identity,
            )

    def prepare_child(
        self,
        token: str,
        *,
        request_id: str,
    ) -> DeepResearchPreparedChild:
        resolved = self.resolve(token)
        normalized_request_id = _normalize_request_id(request_id)
        with self._lock:
            state = self._contexts.get(resolved.context_id)
            if state is None or not state.accepting or state.token != token:
                _invalid_context()
            assert state.children is not None
            assert state.by_request is not None
            existing = state.by_request.get(normalized_request_id)
            if existing is not None:
                if existing.terminal:
                    _invalid_context()
                return existing.prepared
            if len(state.children) >= self._max_children_per_context:
                raise AccountingError(AccountingErrorCode.ACTIVE_SESSION_LIMIT)
            prepared = DeepResearchPreparedChild(
                context_id=resolved.context_id,
                request_id=normalized_request_id,
                ordinal=state.next_ordinal,
                parent_event_id=resolved.parent_event_id,
                auth_identity=resolved.auth_identity,
            )
            state.next_ordinal += 1
            slot = _ChildSlot(prepared=prepared)
            state.children.append(slot)
            state.by_request[normalized_request_id] = slot
            state.drained.clear()
            return prepared

    def bind_child(
        self,
        prepared: DeepResearchPreparedChild,
        reservation: AccountingReservation,
    ) -> DeepResearchChildAdmission:
        if (
            not isinstance(prepared, DeepResearchPreparedChild)
            or not isinstance(reservation, AccountingReservation)
            or reservation.request_id != prepared.request_id
            or reservation.api_key_id != prepared.auth_identity.api_key_id
        ):
            raise AccountingValidationError
        with self._lock:
            state = self._contexts.get(prepared.context_id)
            if state is None or state.by_request is None:
                _invalid_context()
            slot = state.by_request.get(prepared.request_id)
            if slot is None or slot.prepared != prepared or slot.terminal:
                _invalid_context()
            if slot.reservation is None:
                if reservation.reservation_id in self._by_child_reservation:
                    raise AccountingValidationError
                slot.reservation = reservation
                self._by_child_reservation[reservation.reservation_id] = slot
            elif slot.reservation != reservation:
                raise AccountingValidationError
        return DeepResearchChildAdmission(
            reservation=reservation,
            parent_event_id=prepared.parent_event_id,
            ordinal=prepared.ordinal,
        )

    def abort_child(self, prepared: DeepResearchPreparedChild) -> None:
        if not isinstance(prepared, DeepResearchPreparedChild):
            raise AccountingValidationError
        with self._lock:
            state = self._contexts.get(prepared.context_id)
            if state is None or state.children is None or state.by_request is None:
                return
            slot = state.by_request.get(prepared.request_id)
            if slot is None or slot.prepared != prepared or slot.reservation is not None:
                return
            state.by_request.pop(prepared.request_id, None)
            state.children.remove(slot)
            if all(child.terminal for child in state.children):
                state.drained.set()

    def finish_child_commit(
        self,
        reservation: AccountingReservation,
        event: AccountingEvent,
        receipt: AccountingReceipt,
    ) -> None:
        if (
            not isinstance(reservation, AccountingReservation)
            or not isinstance(event, AccountingEvent)
            or not isinstance(receipt, AccountingReceipt)
        ):
            raise AccountingValidationError
        with self._lock:
            slot = self._by_child_reservation.get(reservation.reservation_id)
            if slot is None:
                return
            if slot.reservation != reservation:
                raise AccountingValidationError
            if slot.terminal:
                if slot.receipt != receipt:
                    raise AccountingValidationError
                return
            if (
                event.kind is not AccountingEventKind.CHARGE
                or event.request_id != reservation.request_id
                or event.api_key_id != reservation.api_key_id
                or event.parent_event_id != slot.prepared.parent_event_id
                or receipt.event_id != event.event_id
                or receipt.billing_fingerprint != event.billing_fingerprint
            ):
                raise AccountingValidationError
            slot.receipt = receipt
            slot.usage = event.usage
            self._finish_slot_locked(slot)

    def finish_child_release(self, reservation: AccountingReservation) -> None:
        if not isinstance(reservation, AccountingReservation):
            raise AccountingValidationError
        with self._lock:
            slot = self._by_child_reservation.get(reservation.reservation_id)
            if slot is None or slot.terminal:
                return
            if slot.reservation != reservation:
                raise AccountingValidationError
            self._finish_slot_locked(slot)

    def _finish_slot_locked(self, slot: _ChildSlot) -> None:
        slot.terminal = True
        if slot.reservation is not None:
            self._by_child_reservation.pop(slot.reservation.reservation_id, None)
        state = self._contexts.get(slot.prepared.context_id)
        if state is not None and state.children is not None and all(
            child.terminal for child in state.children
        ):
            state.drained.set()

    async def seal(self, handle: DeepResearchParentHandle) -> DeepResearchChildSeal:
        state = self._start_drain(handle)
        await state.drained.wait()
        with self._lock:
            current = self._contexts.get(handle.context_id)
            if current is not state or current.children is None:
                _invalid_context()
            receipts = tuple(
                child.receipt
                for child in current.children
                if child.receipt is not None
            )
            aggregate_usage = aggregate_deep_research_child_usage(
                child.usage
                for child in current.children
                if child.receipt is not None and child.usage is not None
            )
            self._remove_context_locked(current)
        return seal_deep_research_children(
            receipts,
            aggregate_usage=aggregate_usage,
        )

    async def cancel(self, handle: DeepResearchParentHandle) -> None:
        state = self._start_drain(handle)
        await state.drained.wait()
        with self._lock:
            current = self._contexts.get(handle.context_id)
            if current is state:
                self._remove_context_locked(current)

    def _start_drain(self, handle: DeepResearchParentHandle) -> _ParentState:
        if not isinstance(handle, DeepResearchParentHandle):
            raise AccountingValidationError
        with self._lock:
            state = self._contexts.get(handle.context_id)
            if state is None or state.handle != handle:
                _invalid_context()
            state.accepting = False
            return state

    def _remove_context_locked(self, state: _ParentState) -> None:
        self._contexts.pop(state.handle.context_id, None)
        self._by_parent_reservation.pop(state.handle.reservation.reservation_id, None)

    def has_context(self, handle: DeepResearchParentHandle) -> bool:
        if not isinstance(handle, DeepResearchParentHandle):
            raise AccountingValidationError
        with self._lock:
            return self._contexts.get(handle.context_id, None) is not None

    def __repr__(self) -> str:
        return "<DeepResearchAccountingRegistry>"


def build_deep_research_rollup_event(
    parent: DeepResearchParentHandle,
    children: DeepResearchChildSeal,
    *,
    occurred_at: datetime,
) -> AccountingEvent:
    """Build a canonical zero-cost rollup over already charged children."""
    if not isinstance(parent, DeepResearchParentHandle) or not isinstance(
        children,
        DeepResearchChildSeal,
    ):
        raise AccountingValidationError
    return AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION,
        event_id=parent.rollup_event_id,
        kind=AccountingEventKind.ROLLUP,
        api_key_id=parent.reservation.api_key_id,
        method="POST",
        route_template="/v1/web/deep-research",
        operation="web_deep_research",
        gateway_model=parent.gateway_model,
        provider=None,
        model=None,
        usage=AccountingUsage(),
        cost_source=CostSource.RECEIPT_ROLLUP,
        occurred_at=occurred_at,
        request_id=parent.reservation.request_id,
        child_event_ids=children.child_event_ids,
        child_fingerprints=children.child_fingerprints,
    )
