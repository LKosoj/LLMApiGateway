"""Access-control helpers applied to LLM requests after auth middleware."""

from __future__ import annotations

import math
import threading
from dataclasses import dataclass, field

from fastapi import HTTPException, Request

from ..db.rejections_db import record_rejection
from .accounting import AccountingValidationError

DEFAULT_USD_BUDGET_RESERVATION_ESTIMATE = 5.0
_USD_EPSILON = 1e-9


def _normalize_request_usd(value: object) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise AccountingValidationError from None
    try:
        normalized = float(value)
    except (OverflowError, TypeError, ValueError):
        raise AccountingValidationError from None
    if not math.isfinite(normalized) or normalized < 0:
        raise AccountingValidationError from None
    return 0.0 if normalized == 0.0 else normalized


def _sum_request_usd(*values: float) -> float:
    try:
        total = math.fsum(values)
    except (OverflowError, ValueError):
        raise AccountingValidationError from None
    if not math.isfinite(total) or total < 0:
        raise AccountingValidationError from None
    return 0.0 if total == 0.0 else total


@dataclass
class _UsdBudgetLedgerEntry:
    budget_usd: float | None
    spent_usd: float
    reserved_usd: float = 0.0
    reservations: list[float] = field(default_factory=list)
    request_reservations: dict[str, float] = field(default_factory=dict)

    def budget_enforced(self) -> bool:
        return self.budget_usd is not None and self.budget_usd >= 0


def _request_reserved_total(
    entry: _UsdBudgetLedgerEntry,
    *,
    exclude_request_id: str | None = None,
    additional: tuple[float, ...] = (),
) -> float:
    values = list(entry.reservations)
    values.extend(
        value
        for request_id, value in entry.request_reservations.items()
        if request_id != exclude_request_id
    )
    values.extend(additional)
    return _sum_request_usd(*values)


class UsdBudgetLedger:
    """In-memory per-key USD reservation ledger.

    The durable source of truth remains ``ApiKeysDB.spent_usd``. This ledger
    only closes the mid-request concurrency gap by accounting for in-flight
    requests until ``AccountingService`` commits their actual cost.
    """

    def __init__(
        self,
        *,
        default_estimate_usd: float = DEFAULT_USD_BUDGET_RESERVATION_ESTIMATE,
    ) -> None:
        self.default_estimate_usd = max(0.0, float(default_estimate_usd))
        self._lock = threading.Lock()
        self._entries: dict[int, _UsdBudgetLedgerEntry] = {}

    def sync_record(
        self,
        key_id: int,
        *,
        budget_usd: float | None,
        spent_usd: float,
    ) -> None:
        budget_value = float(budget_usd) if budget_usd is not None else None
        spent_value = max(0.0, float(spent_usd or 0.0))
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                self._entries[key_id] = _UsdBudgetLedgerEntry(
                    budget_usd=budget_value,
                    spent_usd=spent_value,
                )
                return

            entry.budget_usd = budget_value
            if spent_value > entry.spent_usd:
                entry.spent_usd = spent_value

    def reset_record(
        self,
        key_id: int,
        *,
        budget_usd: float | None,
        spent_usd: float,
    ) -> None:
        budget_value = float(budget_usd) if budget_usd is not None else None
        spent_value = max(0.0, float(spent_usd or 0.0))
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                self._entries[key_id] = _UsdBudgetLedgerEntry(
                    budget_usd=budget_value,
                    spent_usd=spent_value,
                )
                return
            entry.budget_usd = budget_value
            entry.spent_usd = spent_value

    def reserve(self, key_id: int, estimate: float) -> bool:
        estimate_value = max(0.0, float(estimate or 0.0))
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None or not entry.budget_enforced():
                return True

            next_reserved = entry.reserved_usd + estimate_value
            if entry.spent_usd + next_reserved > float(entry.budget_usd) + _USD_EPSILON:
                return False

            entry.reserved_usd = next_reserved
            entry.reservations.append(estimate_value)
            return True

    def reserve_request(self, request_id: str, key_id: int, estimate: float) -> bool:
        """Reserve *estimate* once for a server-owned request identifier."""
        estimate_value = _normalize_request_usd(estimate)
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                entry = _UsdBudgetLedgerEntry(budget_usd=None, spent_usd=0.0)
            existing = entry.request_reservations.get(request_id)
            if existing is not None:
                if existing != estimate_value:
                    raise AccountingValidationError from None
                return True

            next_reserved = _request_reserved_total(entry, additional=(estimate_value,))
            liability = _sum_request_usd(entry.spent_usd, next_reserved)
            if entry.budget_enforced() and liability > float(entry.budget_usd) + _USD_EPSILON:
                return False

            self._entries[key_id] = entry
            entry.reserved_usd = next_reserved
            entry.request_reservations[request_id] = estimate_value
            return True

    def commit_reserved(self, key_id: int, actual: float, *, reserved: float | None) -> None:
        actual_value = max(0.0, float(actual or 0.0))
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return

            reserved_value = self._pop_reserved_value(entry, reserved)
            entry.reserved_usd = max(0.0, entry.reserved_usd - reserved_value)
            entry.spent_usd += actual_value

    def commit_request(self, request_id: str, key_id: int, actual: float) -> bool:
        """Commit actual spend for a live request reservation exactly once."""
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return False
            reserved_value = entry.request_reservations.get(request_id)
            if reserved_value is None:
                return False
            actual_value = _normalize_request_usd(actual)
            next_reserved = _request_reserved_total(
                entry,
                exclude_request_id=request_id,
            )
            next_spent = _sum_request_usd(entry.spent_usd, actual_value)

            entry.request_reservations.pop(request_id)
            entry.reserved_usd = next_reserved
            entry.spent_usd = next_spent
            return True

    def release(self, key_id: int, reserved: float | None = None) -> None:
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return

            reserved_value = self._pop_reserved_value(entry, reserved)
            entry.reserved_usd = max(0.0, entry.reserved_usd - reserved_value)

    def release_request(self, request_id: str, key_id: int) -> bool:
        """Release a live request reservation exactly once."""
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return False
            reserved_value = entry.request_reservations.get(request_id)
            if reserved_value is None:
                return False
            next_reserved = _request_reserved_total(
                entry,
                exclude_request_id=request_id,
            )
            entry.request_reservations.pop(request_id)
            entry.reserved_usd = next_reserved
            return True

    def discard_record(self, key_id: int) -> None:
        with self._lock:
            self._entries.pop(key_id, None)

    def reserved_for(self, key_id: int) -> float:
        with self._lock:
            entry = self._entries.get(key_id)
            return 0.0 if entry is None else entry.reserved_usd

    @staticmethod
    def _pop_reserved_value(entry: _UsdBudgetLedgerEntry, reserved: float | None) -> float:
        if not entry.reservations:
            return 0.0
        if reserved is None:
            return entry.reservations.pop(0)

        reserved_value = max(0.0, float(reserved or 0.0))
        for index, value in enumerate(entry.reservations):
            if abs(value - reserved_value) <= _USD_EPSILON:
                return entry.reservations.pop(index)
        return 0.0


def enforce_virtual_key_access(request: Request, requested_model: str | None) -> None:
    """Reject the request when the caller's virtual key is out of bounds.

    Admission owns budget and rate limits. This endpoint boundary keeps only
    disabled-key and allowed-model checks.
    """
    record = getattr(request.state, "api_key_record", None)
    if record is None:
        return

    def _reject(status_code: int, reason: str, category: str) -> None:
        record_rejection(request, status_code=status_code, reason=reason, category=category)
        raise HTTPException(status_code=status_code, detail=reason)

    if record.disabled:
        _reject(403, "API key is disabled", "key_disabled")

    if not record.model_allowed(requested_model):
        _reject(
            403,
            f"Model '{requested_model}' is not allowed for this API key",
            "model_not_allowed",
        )
