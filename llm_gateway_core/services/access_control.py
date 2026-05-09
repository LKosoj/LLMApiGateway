"""Access-control helpers applied to LLM requests after auth middleware."""

from __future__ import annotations

import threading
from dataclasses import dataclass, field

from fastapi import HTTPException, Request


DEFAULT_USD_BUDGET_RESERVATION_ESTIMATE = 5.0
_USD_EPSILON = 1e-9


@dataclass
class _UsdBudgetLedgerEntry:
    budget_usd: float | None
    spent_usd: float
    reserved_usd: float = 0.0
    reservations: list[float] = field(default_factory=list)

    def budget_enforced(self) -> bool:
        return self.budget_usd is not None and self.budget_usd >= 0


class UsdBudgetLedger:
    """In-memory per-key USD reservation ledger.

    The durable source of truth remains ``ApiKeysDB.spent_usd``. This ledger
    only closes the mid-request concurrency gap by accounting for in-flight
    requests until ``chat_logging`` commits their actual cost.
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

    def commit(self, key_id: int, actual: float) -> None:
        actual_value = max(0.0, float(actual or 0.0))
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return

            reserved_value = entry.reservations.pop(0) if entry.reservations else 0.0
            entry.reserved_usd = max(0.0, entry.reserved_usd - reserved_value)
            entry.spent_usd += actual_value

    def release(self, key_id: int) -> None:
        with self._lock:
            entry = self._entries.get(key_id)
            if entry is None:
                return

            reserved_value = entry.reservations.pop(0) if entry.reservations else 0.0
            entry.reserved_usd = max(0.0, entry.reserved_usd - reserved_value)

    def reserved_for(self, key_id: int) -> float:
        with self._lock:
            entry = self._entries.get(key_id)
            return 0.0 if entry is None else entry.reserved_usd


def enforce_virtual_key_access(request: Request, requested_model: str | None) -> None:
    """Reject the request when the caller's virtual key is out of bounds.

    Checks, in order: disabled, allowed-model list, budget, and RPM/TPM rate
    limit. Master-key / legacy callers (no ``api_key_record`` on state) pass
    through without checks.
    """
    record = getattr(request.state, "api_key_record", None)
    if record is None:
        return

    if record.disabled:
        raise HTTPException(status_code=403, detail="API key is disabled")

    if not record.model_allowed(requested_model):
        raise HTTPException(
            status_code=403,
            detail=f"Model '{requested_model}' is not allowed for this API key",
        )

    if record.budget_exhausted():
        raise HTTPException(
            status_code=429,
            detail=(
                f"API key budget of ${record.budget_usd:.4f} exhausted "
                f"(spent ${record.spent_usd:.4f})"
            ),
        )

    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    if rate_limiter is not None:
        error = rate_limiter.try_acquire(
            record.id, rpm_limit=record.rpm, tpm_limit=record.tpm
        )
        if error is not None:
            raise HTTPException(status_code=429, detail=error)
