"""Immutable accounting value contracts and pure billing policy helpers."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Literal, NoReturn

DEFAULT_OPERATION_COST_USD = 0.1
ACCOUNTING_EVENT_VERSION = 1
ACCOUNTING_AUDIT_MAX_PAGE_SIZE = 256

_FINGERPRINT_RE = re.compile(r"^[0-9a-f]{64}$")
_HTTP_METHOD_RE = re.compile(r"^[A-Z]{1,16}$")
_MAX_ID_BYTES = 255
_MAX_TEXT_BYTES = 512
_SQLITE_INT64_MIN = -(1 << 63)
_SQLITE_INT64_MAX = (1 << 63) - 1


class CostSource(StrEnum):
    UPSTREAM = "upstream"
    TOKEN_REGISTRY = "token_registry"
    OPERATION_CONFIGURED = "operation_configured"
    OPERATION_DEFAULT = "operation_default"
    COMPONENT_SUM = "component_sum"
    RECEIPT_ROLLUP = "receipt_rollup"


class AccountingErrorCode(StrEnum):
    INVALID_CONTRACT = "invalid_contract"
    INVALID_UPSTREAM_COST = "invalid_upstream_cost"
    COST_UNAVAILABLE = "cost_unavailable"
    SOURCE_WRITE_FAILED = "source_write_failed"
    PROJECTION_WRITE_FAILED = "projection_write_failed"
    FINGERPRINT_CONFLICT = "fingerprint_conflict"
    ORPHAN_SOURCE = "orphan_source"
    ORPHAN_SINK = "orphan_sink"
    SCHEMA_MISMATCH = "schema_mismatch"
    RECONCILE_FAILED = "reconcile_failed"
    DRAIN_FAILED = "drain_failed"
    ACCOUNTING_FAILED = "accounting_failed"
    ADMISSION_CLOSED = "admission_closed"
    ACTIVE_SESSION_LIMIT = "active_session_limit"
    BUDGET_EXHAUSTED = "budget_exhausted"
    RATE_LIMITED = "rate_limited"
    RESERVATION_CONFLICT = "reservation_conflict"
    ACCOUNTING_IN_FLIGHT = "accounting_in_flight"
    BUDGET_RESET_IN_PROGRESS = "budget_reset_in_progress"


class AccountingError(RuntimeError):
    """Credential-safe accounting failure with a stable reason code."""

    def __init__(self, code: AccountingErrorCode) -> None:
        self.code = code
        super().__init__(code.value)


class AccountingValidationError(AccountingError):
    def __init__(self) -> None:
        super().__init__(AccountingErrorCode.INVALID_CONTRACT)


class AccountingCostError(AccountingError):
    def __init__(
        self,
        code: AccountingErrorCode = AccountingErrorCode.INVALID_UPSTREAM_COST,
    ) -> None:
        if code not in {
            AccountingErrorCode.INVALID_UPSTREAM_COST,
            AccountingErrorCode.COST_UNAVAILABLE,
        }:
            raise AccountingValidationError
        super().__init__(code)


class AccountingEventKind(StrEnum):
    CHARGE = "charge"
    ROLLUP = "rollup"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingReservation:
    """Opaque process-local ownership handle for one admitted request."""

    reservation_id: str
    request_id: str
    api_key_id: int | None
    reserved_usd: float

    def __post_init__(self) -> None:
        object.__setattr__(self, "reservation_id", _normalize_event_id(self.reservation_id))
        object.__setattr__(self, "request_id", _normalize_event_id(self.request_id))
        if self.api_key_id is not None:
            api_key_id = _normalize_int(self.api_key_id)
            if api_key_id == 0:
                _invalid_contract()
            object.__setattr__(self, "api_key_id", api_key_id)
        object.__setattr__(
            self,
            "reserved_usd",
            _normalize_float(self.reserved_usd, non_negative=True),
        )

    def __repr__(self) -> str:
        return "<AccountingReservation>"


class BillingUnit(StrEnum):
    TOKEN = "token"
    OPERATION = "operation"


class BillingComponentKind(StrEnum):
    MODEL = "model"
    OPERATION = "operation"


class SourceStatus(StrEnum):
    ACCEPTED = "accepted"
    RECOVERED = "recovered"
    DUPLICATE = "duplicate"


class ProjectionStatus(StrEnum):
    APPLIED = "applied"
    ALREADY_APPLIED = "already_applied"
    NO_SINK = "no_sink"
    PENDING = "pending"


class AccountingHealthState(StrEnum):
    NEW = "new"
    STARTING = "starting"
    RUNNING = "running"
    BLOCKED = "blocked"
    STOPPING = "stopping"
    STOPPED = "stopped"
    FAILED = "failed"


class ReconciliationMode(StrEnum):
    RUNTIME = "runtime"
    FULL = "full"


class AccountingSourceAuditKind(StrEnum):
    OUTBOX = "outbox"
    USAGE_WITHOUT_OUTBOX = "usage_without_outbox"


class AccountingParentLinkState(StrEnum):
    NOT_APPLICABLE = "not_applicable"
    LINKED = "linked"
    UNROLLED = "unrolled"


class AccountingOwnerState(StrEnum):
    ACTIVE = "active"
    TOMBSTONE = "tombstone"
    MISSING = "missing"
    AMBIGUOUS = "ambiguous"


def _invalid_contract() -> NoReturn:
    raise AccountingValidationError from None


def _normalize_float(value: object, *, non_negative: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _invalid_contract()
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        _invalid_contract()
    if not math.isfinite(normalized) or (non_negative and normalized < 0):
        _invalid_contract()
    return 0.0 if normalized == 0.0 else normalized


def _normalize_int(value: object, *, non_negative: bool = True) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        _invalid_contract()
    minimum = 0 if non_negative else _SQLITE_INT64_MIN
    if value < minimum or value > _SQLITE_INT64_MAX:
        _invalid_contract()
    return value


def _normalize_text(
    value: object,
    *,
    max_bytes: int = _MAX_TEXT_BYTES,
    ascii_only: bool = False,
) -> str:
    if not isinstance(value, str):
        _invalid_contract()
    normalized = value.strip()
    if not normalized:
        _invalid_contract()
    encoding = "ascii" if ascii_only else "utf-8"
    try:
        encoded = normalized.encode(encoding)
    except UnicodeEncodeError:
        _invalid_contract()
    if len(encoded) > max_bytes:
        _invalid_contract()
    return normalized


def _normalize_optional_text(
    value: object,
    *,
    max_bytes: int = _MAX_TEXT_BYTES,
    ascii_only: bool = False,
) -> str | None:
    if value is None:
        return None
    return _normalize_text(value, max_bytes=max_bytes, ascii_only=ascii_only)


def _normalize_gateway_model(value: object) -> str:
    normalized = _normalize_text(value, max_bytes=_MAX_ID_BYTES)
    if normalized != value or any(character.isspace() for character in normalized):
        _invalid_contract()
    return normalized


def _normalize_component_identity(value: object) -> str:
    normalized = _normalize_text(value)
    if "\x00" in normalized:
        _invalid_contract()
    return normalized


def _normalize_event_id(value: object) -> str:
    normalized = _normalize_text(
        value,
        max_bytes=_MAX_ID_BYTES,
        ascii_only=True,
    )
    if any(not 0x20 <= ord(character) <= 0x7E for character in normalized):
        _invalid_contract()
    return normalized


def _normalize_optional_event_id(value: object) -> str | None:
    if value is None:
        return None
    return _normalize_event_id(value)


def _normalize_http_method(value: object) -> str:
    normalized = _normalize_text(value, max_bytes=16, ascii_only=True).upper()
    if _HTTP_METHOD_RE.fullmatch(normalized) is None:
        _invalid_contract()
    return normalized


def _normalize_route_template(value: object) -> str:
    normalized = _normalize_text(value, ascii_only=True)
    if (
        not normalized.startswith("/")
        or "?" in normalized
        or "#" in normalized
        or any(not 0x20 <= ord(character) <= 0x7E for character in normalized)
    ):
        _invalid_contract()
    return normalized


def _normalize_utc_datetime(value: object) -> datetime:
    if not isinstance(value, datetime) or value.tzinfo is None or value.utcoffset() is None:
        _invalid_contract()
    return value.astimezone(timezone.utc)


def _normalize_fingerprint(value: object) -> str:
    if not isinstance(value, str) or _FINGERPRINT_RE.fullmatch(value) is None:
        _invalid_contract()
    return value


def _coerce_enum[T: StrEnum](value: object, enum_type: type[T]) -> T:
    if isinstance(value, enum_type):
        return value
    if not isinstance(value, str):
        _invalid_contract()
    try:
        return enum_type(value)
    except ValueError:
        _invalid_contract()


def _float_hex(value: float) -> str:
    return (0.0 if value == 0.0 else value).hex()


def _sha256_json(payload: object) -> str:
    encoded = json.dumps(
        payload,
        ensure_ascii=True,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


@dataclass(frozen=True, slots=True)
class OperationCostCalculator:
    unit: Literal["operation"]
    rate_usd: float

    def __post_init__(self) -> None:
        if self.unit != BillingUnit.OPERATION.value:
            _invalid_contract()
        object.__setattr__(
            self,
            "rate_usd",
            _normalize_float(self.rate_usd, non_negative=True),
        )


@dataclass(frozen=True, slots=True)
class ResolvedCost:
    cost_usd: float
    source: CostSource
    unit: Literal["operation"] = "operation"
    quantity: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "cost_usd",
            _normalize_float(self.cost_usd, non_negative=True),
        )
        object.__setattr__(self, "source", _coerce_enum(self.source, CostSource))
        if self.unit != BillingUnit.OPERATION.value or type(self.quantity) is not int or self.quantity != 1:
            _invalid_contract()


def resolve_operation_cost(
    *,
    upstream_cost_present: bool,
    upstream_cost: object,
    calculator: OperationCostCalculator | None,
) -> ResolvedCost:
    """Resolve one successful operation without masking malformed upstream cost."""
    if not isinstance(upstream_cost_present, bool):
        _invalid_contract()
    if upstream_cost_present:
        try:
            cost_usd = _normalize_float(upstream_cost, non_negative=True)
        except AccountingValidationError:
            raise AccountingCostError from None
        return ResolvedCost(cost_usd=cost_usd, source=CostSource.UPSTREAM)

    if calculator is not None:
        if not isinstance(calculator, OperationCostCalculator):
            _invalid_contract()
        return ResolvedCost(
            cost_usd=calculator.rate_usd,
            source=CostSource.OPERATION_CONFIGURED,
        )

    return ResolvedCost(
        cost_usd=DEFAULT_OPERATION_COST_USD,
        source=CostSource.OPERATION_DEFAULT,
    )


_OPERATION_RULE_SECTIONS = MappingProxyType(
    {
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
)


def _calculator_from_config(value: object) -> OperationCostCalculator:
    if isinstance(value, OperationCostCalculator):
        return value
    if not isinstance(value, Mapping) or set(value) != {"unit", "rate_usd"}:
        _invalid_contract()
    return OperationCostCalculator(
        unit=value["unit"],
        rate_usd=value["rate_usd"],
    )


def build_operation_cost_calculator_registry(
    operation_rules_config: Mapping[str, Mapping[str, Any]] | None,
) -> Mapping[tuple[str, str], OperationCostCalculator]:
    """Build an immutable registry without materializing the default rate."""
    if operation_rules_config is None:
        return MappingProxyType({})
    if not isinstance(operation_rules_config, Mapping):
        _invalid_contract()

    registry: dict[tuple[str, str], OperationCostCalculator] = {}
    for section_name, operation in _OPERATION_RULE_SECTIONS.items():
        if section_name not in operation_rules_config:
            continue
        model_configs = operation_rules_config[section_name]
        if not isinstance(model_configs, Mapping):
            _invalid_contract()
        for gateway_model, model_config in model_configs.items():
            normalized_model = _normalize_gateway_model(gateway_model)
            if not isinstance(model_config, Mapping):
                _invalid_contract()
            if "cost_calculator" not in model_config:
                continue
            registry_key = (operation, normalized_model)
            if registry_key in registry:
                _invalid_contract()
            registry[registry_key] = _calculator_from_config(model_config["cost_calculator"])
    return MappingProxyType(registry)


@dataclass(frozen=True, slots=True)
class AccountingUsage:
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    reasoning_tokens: int = 0
    cached_tokens: int = 0
    cost: float = 0.0
    cost_saved: float = 0.0
    duration_ms: int | None = None
    is_estimated: bool = False
    cost_unavailable: bool = False

    def __post_init__(self) -> None:
        for field_name in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_int(getattr(self, field_name)),
            )
        if self.total_tokens != self.prompt_tokens + self.completion_tokens:
            _invalid_contract()
        object.__setattr__(
            self,
            "cost",
            _normalize_float(self.cost, non_negative=True),
        )
        object.__setattr__(
            self,
            "cost_saved",
            _normalize_float(self.cost_saved, non_negative=False),
        )
        if self.duration_ms is not None:
            object.__setattr__(self, "duration_ms", _normalize_int(self.duration_ms))
        if not isinstance(self.is_estimated, bool):
            _invalid_contract()
        if not isinstance(self.cost_unavailable, bool):
            _invalid_contract()

    def _billing_payload(self) -> dict[str, int | str]:
        return {
            "cached_tokens": self.cached_tokens,
            "completion_tokens": self.completion_tokens,
            "cost": _float_hex(self.cost),
            "prompt_tokens": self.prompt_tokens,
            "reasoning_tokens": self.reasoning_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass(frozen=True, slots=True)
class BillingComponent:
    provider: str | None
    model: str | None
    usage: AccountingUsage
    cost_source: CostSource
    component_kind: BillingComponentKind = BillingComponentKind.MODEL
    operation: str | None = None
    gateway_model: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.usage, AccountingUsage):
            _invalid_contract()
        object.__setattr__(
            self,
            "component_kind",
            _coerce_enum(self.component_kind, BillingComponentKind),
        )
        object.__setattr__(
            self,
            "cost_source",
            _coerce_enum(self.cost_source, CostSource),
        )
        if self.component_kind is BillingComponentKind.MODEL:
            self._normalize_model_component()
        else:
            self._normalize_operation_component()

    def _normalize_model_component(self) -> None:
        if self.operation is not None or self.gateway_model is not None:
            _invalid_contract()
        object.__setattr__(
            self,
            "provider",
            _normalize_component_identity(self.provider),
        )
        object.__setattr__(
            self,
            "model",
            _normalize_component_identity(self.model),
        )
        if self.cost_source in {CostSource.COMPONENT_SUM, CostSource.RECEIPT_ROLLUP}:
            _invalid_contract()

    def _normalize_operation_component(self) -> None:
        if self.provider is not None or self.model is not None:
            _invalid_contract()
        object.__setattr__(
            self,
            "operation",
            _normalize_component_identity(self.operation),
        )
        gateway_model = _normalize_gateway_model(self.gateway_model)
        if "\x00" in gateway_model:
            _invalid_contract()
        object.__setattr__(self, "gateway_model", gateway_model)
        if self.cost_source not in {
            CostSource.UPSTREAM,
            CostSource.OPERATION_CONFIGURED,
            CostSource.OPERATION_DEFAULT,
        }:
            _invalid_contract()
        if any(
            (
                self.usage.prompt_tokens,
                self.usage.completion_tokens,
                self.usage.total_tokens,
                self.usage.reasoning_tokens,
                self.usage.cached_tokens,
            )
        ):
            _invalid_contract()

    @property
    def billing_fingerprint(self) -> str:
        if self.component_kind is BillingComponentKind.OPERATION:
            return _sha256_json(
                {
                    "component_kind": self.component_kind.value,
                    "cost_source": self.cost_source.value,
                    "gateway_model": self.gateway_model,
                    "operation": self.operation,
                    "usage": self.usage._billing_payload(),
                }
            )
        return _sha256_json(
            {
                "cost_source": self.cost_source.value,
                "model": self.model,
                "provider": self.provider,
                "usage": self.usage._billing_payload(),
            }
        )


def build_component_sum_usage(
    components: Iterable[BillingComponent],
    *,
    duration_ms: int | None = None,
) -> AccountingUsage:
    """Build the canonical aggregate usage for an ordered component collection."""
    try:
        normalized_components = tuple(components)
    except TypeError:
        _invalid_contract()
    if not normalized_components or any(
        not isinstance(component, BillingComponent) for component in normalized_components
    ):
        _invalid_contract()

    try:
        cost = math.fsum(component.usage.cost for component in normalized_components)
        cost_saved = math.fsum(component.usage.cost_saved for component in normalized_components)
    except (OverflowError, ValueError):
        _invalid_contract()

    return AccountingUsage(
        prompt_tokens=sum(component.usage.prompt_tokens for component in normalized_components),
        completion_tokens=sum(component.usage.completion_tokens for component in normalized_components),
        total_tokens=sum(component.usage.total_tokens for component in normalized_components),
        reasoning_tokens=sum(component.usage.reasoning_tokens for component in normalized_components),
        cached_tokens=sum(component.usage.cached_tokens for component in normalized_components),
        cost=cost,
        cost_saved=cost_saved,
        duration_ms=duration_ms,
        is_estimated=any(component.usage.is_estimated for component in normalized_components),
        cost_unavailable=any(component.usage.cost_unavailable for component in normalized_components),
    )


@dataclass(frozen=True, slots=True)
class BillingPolicy:
    operation: str
    unit: BillingUnit

    def __post_init__(self) -> None:
        object.__setattr__(self, "operation", _normalize_text(self.operation))
        object.__setattr__(self, "unit", _coerce_enum(self.unit, BillingUnit))


@dataclass(frozen=True, slots=True, repr=False)
class AccountingEvent:
    version: int
    event_id: str
    kind: AccountingEventKind
    api_key_id: int | None
    method: str
    route_template: str
    operation: str
    gateway_model: str
    provider: str | None
    model: str | None
    usage: AccountingUsage
    cost_source: CostSource
    occurred_at: datetime
    request_id: str | None = None
    parent_event_id: str | None = None
    components: tuple[BillingComponent, ...] = ()
    child_event_ids: tuple[str, ...] = ()
    child_fingerprints: tuple[str, ...] = ()
    upstream_key_fingerprint: str | None = None
    x_title: str | None = None

    def __post_init__(self) -> None:
        if _normalize_int(self.version) != ACCOUNTING_EVENT_VERSION:
            _invalid_contract()
        object.__setattr__(
            self,
            "event_id",
            _normalize_event_id(self.event_id),
        )
        object.__setattr__(self, "kind", _coerce_enum(self.kind, AccountingEventKind))
        if self.api_key_id is not None:
            api_key_id = _normalize_int(self.api_key_id)
            if api_key_id == 0:
                _invalid_contract()
            object.__setattr__(self, "api_key_id", api_key_id)

        object.__setattr__(self, "method", _normalize_http_method(self.method))
        object.__setattr__(
            self,
            "route_template",
            _normalize_route_template(self.route_template),
        )
        object.__setattr__(self, "operation", _normalize_text(self.operation))
        object.__setattr__(self, "gateway_model", _normalize_gateway_model(self.gateway_model))
        object.__setattr__(self, "provider", _normalize_optional_text(self.provider))
        object.__setattr__(self, "model", _normalize_optional_text(self.model))
        if (self.provider is None) != (self.model is None):
            _invalid_contract()
        if not isinstance(self.usage, AccountingUsage):
            _invalid_contract()
        object.__setattr__(
            self,
            "cost_source",
            _coerce_enum(self.cost_source, CostSource),
        )
        object.__setattr__(self, "occurred_at", _normalize_utc_datetime(self.occurred_at))
        object.__setattr__(
            self,
            "request_id",
            _normalize_optional_text(self.request_id, max_bytes=_MAX_ID_BYTES),
        )
        object.__setattr__(
            self,
            "parent_event_id",
            _normalize_optional_event_id(self.parent_event_id),
        )
        object.__setattr__(
            self,
            "upstream_key_fingerprint",
            _normalize_optional_text(self.upstream_key_fingerprint, max_bytes=_MAX_ID_BYTES),
        )
        object.__setattr__(self, "x_title", _normalize_optional_text(self.x_title))

        try:
            components = tuple(self.components)
        except TypeError:
            _invalid_contract()
        if any(not isinstance(component, BillingComponent) for component in components):
            _invalid_contract()
        object.__setattr__(self, "components", components)
        try:
            child_event_ids = tuple(_normalize_event_id(value) for value in self.child_event_ids)
            child_fingerprints = tuple(_normalize_fingerprint(value) for value in self.child_fingerprints)
        except TypeError:
            _invalid_contract()
        if len(child_event_ids) != len(child_fingerprints):
            _invalid_contract()
        if len(set(child_event_ids)) != len(child_event_ids):
            _invalid_contract()
        if self.event_id in child_event_ids:
            _invalid_contract()
        object.__setattr__(self, "child_event_ids", child_event_ids)
        object.__setattr__(self, "child_fingerprints", child_fingerprints)

        if self.kind is AccountingEventKind.ROLLUP:
            self._validate_rollup()
        else:
            self._validate_charge()

    def _validate_rollup(self) -> None:
        if (
            self.cost_source is not CostSource.RECEIPT_ROLLUP
            or self.components
            or not self.child_event_ids
            or self.usage.cost != 0.0
            or self.usage.total_tokens != 0
            or self.usage.reasoning_tokens != 0
            or self.usage.cached_tokens != 0
        ):
            _invalid_contract()

    def _validate_charge(self) -> None:
        if self.child_event_ids or self.cost_source is CostSource.RECEIPT_ROLLUP:
            _invalid_contract()
        if bool(self.components) != (self.cost_source is CostSource.COMPONENT_SUM):
            _invalid_contract()
        if not self.components:
            return
        canonical_usage = build_component_sum_usage(
            self.components,
            duration_ms=self.usage.duration_ms,
        )
        if canonical_usage._billing_payload() != self.usage._billing_payload():
            _invalid_contract()

    @property
    def billing_fingerprint(self) -> str:
        return _sha256_json(
            {
                "api_key_id": self.api_key_id,
                "child_fingerprints": list(self.child_fingerprints),
                "component_fingerprints": [component.billing_fingerprint for component in self.components],
                "cost_source": self.cost_source.value,
                "gateway_model": self.gateway_model,
                "kind": self.kind.value,
                "method": self.method,
                "model": self.model,
                "operation": self.operation,
                "parent_event_id": self.parent_event_id,
                "provider": self.provider,
                "route_template": self.route_template,
                "usage": self.usage._billing_payload(),
                "version": self.version,
            }
        )

    def __repr__(self) -> str:
        return "<AccountingEvent>"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingReceipt:
    source_status: SourceStatus
    projection_status: ProjectionStatus
    event_id: str
    billing_fingerprint: str
    usage_row_id: int | None = None
    child_event_ids: tuple[str, ...] = ()
    child_fingerprints: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "source_status",
            _coerce_enum(self.source_status, SourceStatus),
        )
        object.__setattr__(
            self,
            "projection_status",
            _coerce_enum(self.projection_status, ProjectionStatus),
        )
        object.__setattr__(
            self,
            "event_id",
            _normalize_event_id(self.event_id),
        )
        object.__setattr__(
            self,
            "billing_fingerprint",
            _normalize_fingerprint(self.billing_fingerprint),
        )
        if self.usage_row_id is not None:
            usage_row_id = _normalize_int(self.usage_row_id)
            if usage_row_id == 0:
                _invalid_contract()
            object.__setattr__(self, "usage_row_id", usage_row_id)
        try:
            child_event_ids = tuple(_normalize_event_id(value) for value in self.child_event_ids)
            child_fingerprints = tuple(_normalize_fingerprint(value) for value in self.child_fingerprints)
        except TypeError:
            _invalid_contract()
        if len(child_event_ids) != len(child_fingerprints):
            _invalid_contract()
        object.__setattr__(self, "child_event_ids", child_event_ids)
        object.__setattr__(self, "child_fingerprints", child_fingerprints)

    def __repr__(self) -> str:
        return "<AccountingReceipt>"


@dataclass(frozen=True, slots=True, repr=False)
class StoredAccountingEvent:
    event: AccountingEvent
    usage_row_id: int | None
    created_at: datetime
    projected_at: datetime | None = None
    projection_attempts: int = 0
    last_attempt_at: datetime | None = None
    last_error_code: AccountingErrorCode | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.event, AccountingEvent):
            _invalid_contract()
        if self.usage_row_id is None:
            if self.projected_at is None:
                _invalid_contract()
        else:
            usage_row_id = _normalize_int(self.usage_row_id)
            if usage_row_id == 0:
                _invalid_contract()
            object.__setattr__(self, "usage_row_id", usage_row_id)
        object.__setattr__(
            self,
            "created_at",
            _normalize_utc_datetime(self.created_at),
        )
        if self.projected_at is not None:
            object.__setattr__(
                self,
                "projected_at",
                _normalize_utc_datetime(self.projected_at),
            )
        object.__setattr__(
            self,
            "projection_attempts",
            _normalize_int(self.projection_attempts),
        )
        if self.last_attempt_at is not None:
            object.__setattr__(
                self,
                "last_attempt_at",
                _normalize_utc_datetime(self.last_attempt_at),
            )
        if self.last_error_code is not None:
            object.__setattr__(
                self,
                "last_error_code",
                _coerce_enum(self.last_error_code, AccountingErrorCode),
            )

    @property
    def billing_fingerprint(self) -> str:
        return self.event.billing_fingerprint

    def __repr__(self) -> str:
        return "<StoredAccountingEvent>"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingProjectionMark:
    event_id: str
    billing_fingerprint: str
    projected_at: datetime | None
    projection_attempts: int
    last_attempt_at: datetime | None
    last_error_code: AccountingErrorCode | None

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _normalize_event_id(self.event_id))
        object.__setattr__(
            self,
            "billing_fingerprint",
            _normalize_fingerprint(self.billing_fingerprint),
        )
        if self.projected_at is not None:
            object.__setattr__(
                self,
                "projected_at",
                _normalize_utc_datetime(self.projected_at),
            )
        object.__setattr__(
            self,
            "projection_attempts",
            _normalize_int(self.projection_attempts),
        )
        if self.last_attempt_at is not None:
            object.__setattr__(
                self,
                "last_attempt_at",
                _normalize_utc_datetime(self.last_attempt_at),
            )
        if self.last_error_code is not None:
            object.__setattr__(
                self,
                "last_error_code",
                _coerce_enum(self.last_error_code, AccountingErrorCode),
            )
        if self.projection_attempts == 0:
            if (
                self.projected_at is not None
                or self.last_attempt_at is not None
                or self.last_error_code is not None
            ):
                _invalid_contract()
        elif self.last_attempt_at is None or (
            (self.projected_at is None) != (self.last_error_code is not None)
        ):
            _invalid_contract()

    def __repr__(self) -> str:
        return "<AccountingProjectionMark>"


@dataclass(frozen=True, slots=True, repr=False)
class SourceAcceptance:
    status: SourceStatus
    stored_event: StoredAccountingEvent

    def __post_init__(self) -> None:
        object.__setattr__(self, "status", _coerce_enum(self.status, SourceStatus))
        if not isinstance(self.stored_event, StoredAccountingEvent):
            _invalid_contract()

    def __repr__(self) -> str:
        return "<SourceAcceptance>"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingSourceAuditRow:
    event_id: str
    row_kind: AccountingSourceAuditKind
    event_kind: AccountingEventKind
    billing_fingerprint: str | None
    api_key_id: int | None
    spend_usd: float
    projected_at: datetime | None
    usage_present: bool
    parent_link_state: AccountingParentLinkState

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _normalize_event_id(self.event_id))
        object.__setattr__(
            self,
            "row_kind",
            _coerce_enum(self.row_kind, AccountingSourceAuditKind),
        )
        object.__setattr__(
            self,
            "event_kind",
            _coerce_enum(self.event_kind, AccountingEventKind),
        )
        if self.billing_fingerprint is not None:
            object.__setattr__(
                self,
                "billing_fingerprint",
                _normalize_fingerprint(self.billing_fingerprint),
            )
        if self.api_key_id is not None:
            api_key_id = _normalize_int(self.api_key_id)
            if api_key_id == 0:
                _invalid_contract()
            object.__setattr__(self, "api_key_id", api_key_id)
        object.__setattr__(
            self,
            "spend_usd",
            _normalize_float(self.spend_usd, non_negative=True),
        )
        if self.projected_at is not None:
            object.__setattr__(
                self,
                "projected_at",
                _normalize_utc_datetime(self.projected_at),
            )
        if not isinstance(self.usage_present, bool):
            _invalid_contract()
        object.__setattr__(
            self,
            "parent_link_state",
            _coerce_enum(self.parent_link_state, AccountingParentLinkState),
        )
        if self.event_kind is AccountingEventKind.ROLLUP and self.spend_usd != 0.0:
            _invalid_contract()
        if self.row_kind is AccountingSourceAuditKind.OUTBOX:
            if self.billing_fingerprint is None:
                _invalid_contract()
        elif (
            self.billing_fingerprint is not None
            or self.projected_at is not None
            or not self.usage_present
            or self.parent_link_state is not AccountingParentLinkState.NOT_APPLICABLE
        ):
            _invalid_contract()

    def __repr__(self) -> str:
        return "<AccountingSourceAuditRow>"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingSinkAuditRow:
    event_id: str
    billing_fingerprint: str
    api_key_id: int
    spend_usd: float
    applied_at: datetime
    sink_kind: Literal["active", "tombstone"]
    owner_state: AccountingOwnerState

    def __post_init__(self) -> None:
        object.__setattr__(self, "event_id", _normalize_event_id(self.event_id))
        object.__setattr__(
            self,
            "billing_fingerprint",
            _normalize_fingerprint(self.billing_fingerprint),
        )
        api_key_id = _normalize_int(self.api_key_id)
        if api_key_id == 0:
            _invalid_contract()
        object.__setattr__(self, "api_key_id", api_key_id)
        object.__setattr__(
            self,
            "spend_usd",
            _normalize_float(self.spend_usd, non_negative=True),
        )
        object.__setattr__(
            self,
            "applied_at",
            _normalize_utc_datetime(self.applied_at),
        )
        if type(self.sink_kind) is not str or self.sink_kind not in {
            "active",
            "tombstone",
        }:
            _invalid_contract()
        object.__setattr__(
            self,
            "owner_state",
            _coerce_enum(self.owner_state, AccountingOwnerState),
        )

    def __repr__(self) -> str:
        return "<AccountingSinkAuditRow>"


@dataclass(frozen=True, slots=True, repr=False)
class AccountingOwnerAuditRow:
    api_key_id: int
    owner_state: AccountingOwnerState

    def __post_init__(self) -> None:
        api_key_id = _normalize_int(self.api_key_id)
        if api_key_id == 0:
            _invalid_contract()
        object.__setattr__(self, "api_key_id", api_key_id)
        object.__setattr__(
            self,
            "owner_state",
            _coerce_enum(self.owner_state, AccountingOwnerState),
        )

    def __repr__(self) -> str:
        return "<AccountingOwnerAuditRow>"


@dataclass(frozen=True, slots=True)
class ReconciliationReport:
    mode: ReconciliationMode
    source_rows_scanned: int = 0
    sink_rows_scanned: int = 0
    retained_events: int = 0
    pending_source_events: int = 0
    usage_without_outbox: int = 0
    pending_without_usage: int = 0
    projected_without_receipt: int = 0
    receipt_without_source: int = 0
    unexpected_receipts: int = 0
    fingerprint_mismatches: int = 0
    api_key_mismatches: int = 0
    cost_mismatches: int = 0
    owner_orphans: int = 0
    unrolled_children: int = 0

    def __post_init__(self) -> None:
        object.__setattr__(self, "mode", _coerce_enum(self.mode, ReconciliationMode))
        for field_name in (
            "source_rows_scanned",
            "sink_rows_scanned",
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
            "unrolled_children",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_int(getattr(self, field_name)),
            )

    @property
    def full(self) -> bool:
        return self.mode is ReconciliationMode.FULL

    @property
    def clean(self) -> bool:
        return all(
            getattr(self, field_name) == 0
            for field_name in (
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
            )
        )


@dataclass(frozen=True, slots=True)
class AccountingHealthSnapshot:
    state: AccountingHealthState = AccountingHealthState.NEW
    initialized: bool = False
    accepting: bool = False
    active_sessions: int = 0
    active_session_limit: int = 0
    reset_pending_keys: int = 0
    pending_source_events: int = 0
    projection_attempts: int = 0
    fingerprint_conflicts: int = 0
    source_orphans: int = 0
    sink_orphans: int = 0
    unrolled_children: int = 0
    oldest_pending_at: datetime | None = None
    last_full_reconciliation: ReconciliationReport | None = None
    last_error_code: AccountingErrorCode | None = None
    last_error_at: datetime | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "state", _coerce_enum(self.state, AccountingHealthState))
        if not isinstance(self.initialized, bool) or not isinstance(self.accepting, bool):
            _invalid_contract()
        if self.accepting and not self.initialized:
            _invalid_contract()
        for field_name in (
            "active_sessions",
            "active_session_limit",
            "reset_pending_keys",
            "pending_source_events",
            "projection_attempts",
            "fingerprint_conflicts",
            "source_orphans",
            "sink_orphans",
            "unrolled_children",
        ):
            object.__setattr__(
                self,
                field_name,
                _normalize_int(getattr(self, field_name)),
            )
        if self.oldest_pending_at is not None:
            object.__setattr__(
                self,
                "oldest_pending_at",
                _normalize_utc_datetime(self.oldest_pending_at),
            )
        if self.last_full_reconciliation is not None:
            if (
                not isinstance(self.last_full_reconciliation, ReconciliationReport)
                or not self.last_full_reconciliation.full
                or self.unrolled_children
                != self.last_full_reconciliation.unrolled_children
            ):
                _invalid_contract()
        if self.last_error_code is not None:
            object.__setattr__(
                self,
                "last_error_code",
                _coerce_enum(self.last_error_code, AccountingErrorCode),
            )
        if self.last_error_at is not None:
            object.__setattr__(
                self,
                "last_error_at",
                _normalize_utc_datetime(self.last_error_at),
            )

    @property
    def healthy(self) -> bool:
        return (
            self.state is AccountingHealthState.RUNNING
            and self.accepting
            and self.pending_source_events == 0
            and self.fingerprint_conflicts == 0
            and self.source_orphans == 0
            and self.sink_orphans == 0
            and (
                self.last_full_reconciliation is None
                or self.last_full_reconciliation.clean
            )
        )


_BILLING_POLICIES = MappingProxyType(
    {
        ("POST", "/v1/chat/completions"): BillingPolicy("chat", BillingUnit.TOKEN),
        ("POST", "/v1/v1/chat/completions"): BillingPolicy("chat", BillingUnit.TOKEN),
        ("POST", "/v1/responses"): BillingPolicy("chat", BillingUnit.TOKEN),
        ("POST", "/v1/messages"): BillingPolicy("chat", BillingUnit.TOKEN),
        ("POST", "/v1/v1/messages"): BillingPolicy("chat", BillingUnit.TOKEN),
        ("POST", "/v1/embeddings"): BillingPolicy("embeddings", BillingUnit.TOKEN),
        ("POST", "/v1/rerank"): BillingPolicy("rerank", BillingUnit.TOKEN),
        ("POST", "/v1/images"): BillingPolicy("images_generation", BillingUnit.OPERATION),
        ("POST", "/v1/images/generations"): BillingPolicy(
            "images_generation",
            BillingUnit.OPERATION,
        ),
        ("POST", "/v1/images/edits"): BillingPolicy("images_edit", BillingUnit.OPERATION),
        ("POST", "/v1/audio/speech"): BillingPolicy("audio_speech", BillingUnit.OPERATION),
        ("POST", "/v1/audio/transcriptions"): BillingPolicy(
            "audio_transcription",
            BillingUnit.OPERATION,
        ),
        ("POST", "/v1/pdf/convert"): BillingPolicy("pdf_conversion", BillingUnit.OPERATION),
        ("POST", "/v1/pdf/jobs"): BillingPolicy(
            "pdf_conversion",
            BillingUnit.OPERATION,
        ),
        ("GET", "/v1/pdf/jobs/{job_id}"): BillingPolicy(
            "pdf_conversion",
            BillingUnit.OPERATION,
        ),
        ("POST", "/v1/web/search"): BillingPolicy("web_search", BillingUnit.OPERATION),
        ("POST", "/v1/tavily/search"): BillingPolicy("web_search", BillingUnit.OPERATION),
        ("POST", "/v1/web/read"): BillingPolicy("web_read", BillingUnit.OPERATION),
        ("POST", "/v1/tavily/extract"): BillingPolicy("web_read", BillingUnit.OPERATION),
        ("POST", "/v1/web/research"): BillingPolicy("web_research", BillingUnit.OPERATION),
        ("POST", "/v1/web/deep-research"): BillingPolicy(
            "web_deep_research",
            BillingUnit.OPERATION,
        ),
    }
)


def classify_billing_policy(method: str, route_template: str) -> BillingPolicy | None:
    """Return policy only for an exact matched route template and HTTP method."""
    if not isinstance(method, str) or not isinstance(route_template, str):
        return None
    return _BILLING_POLICIES.get((method.upper(), route_template))
