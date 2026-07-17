"""Credential-safe HTTP mapping for accounting failures."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from fastapi import Request, status
from fastapi.responses import JSONResponse

from ..services.accounting import AccountingError, AccountingErrorCode
from .error_envelope import error_response


class AccountingHttpUse(StrEnum):
    ADMISSION = "admission"
    ADMIN_MUTATION = "admin_mutation"


@dataclass(frozen=True, slots=True)
class AccountingHttpFailure:
    status_code: int
    public_code: str
    detail: str


_UNAVAILABLE = AccountingHttpFailure(
    status.HTTP_503_SERVICE_UNAVAILABLE,
    "accounting_unavailable",
    "Accounting service is unavailable.",
)
_ADMISSION_FAILURES = {
    AccountingErrorCode.BUDGET_EXHAUSTED: AccountingHttpFailure(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "budget_exhausted",
        "Request budget is exhausted.",
    ),
    AccountingErrorCode.RATE_LIMITED: AccountingHttpFailure(
        status.HTTP_429_TOO_MANY_REQUESTS,
        "rate_limited",
        "Request rate limit exceeded.",
    ),
    AccountingErrorCode.ACCOUNTING_IN_FLIGHT: AccountingHttpFailure(
        status.HTTP_409_CONFLICT,
        "accounting_in_flight",
        "Accounting state update is in progress.",
    ),
    AccountingErrorCode.BUDGET_RESET_IN_PROGRESS: AccountingHttpFailure(
        status.HTTP_503_SERVICE_UNAVAILABLE,
        "budget_reset_in_progress",
        "Accounting budget reset is in progress.",
    ),
}
_ADMIN_FAILURES = {
    AccountingErrorCode.ACCOUNTING_IN_FLIGHT: AccountingHttpFailure(
        status.HTTP_409_CONFLICT,
        "accounting_in_flight",
        "Accounting state update is in progress.",
    ),
    AccountingErrorCode.BUDGET_RESET_IN_PROGRESS: AccountingHttpFailure(
        status.HTTP_409_CONFLICT,
        "budget_reset_in_progress",
        "Accounting budget reset is in progress.",
    ),
}


def map_accounting_error(
    error: AccountingError,
    *,
    use: AccountingHttpUse,
) -> AccountingHttpFailure:
    if not isinstance(error, AccountingError):
        raise TypeError("error must be an AccountingError")
    if use is AccountingHttpUse.ADMISSION:
        return _ADMISSION_FAILURES.get(error.code, _UNAVAILABLE)
    if use is AccountingHttpUse.ADMIN_MUTATION:
        return _ADMIN_FAILURES.get(error.code, _UNAVAILABLE)
    raise TypeError("use must be an AccountingHttpUse")


def accounting_error_response(
    request: Request,
    error: AccountingError,
    *,
    use: AccountingHttpUse,
) -> JSONResponse:
    failure = map_accounting_error(error, use=use)
    return error_response(
        request,
        status_code=failure.status_code,
        detail=failure.detail,
        code=failure.public_code,
    )
