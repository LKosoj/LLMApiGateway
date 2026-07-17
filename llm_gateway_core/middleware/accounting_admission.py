"""HTTP route and request-context adapters for accounting admission."""

from __future__ import annotations

from collections.abc import Iterable, MutableMapping
from dataclasses import dataclass
from typing import Any

from starlette.routing import Match
from starlette.types import Scope

from ..services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReservation,
    AccountingValidationError,
    BillingPolicy,
    _normalize_optional_event_id,
    classify_billing_policy,
)


ACCOUNTING_REQUEST_CONTEXT_STATE_KEY = "llmgateway_accounting_context"


@dataclass(frozen=True, slots=True)
class EffectiveHttpRoute:
    method: str
    route_template: str

    def __post_init__(self) -> None:
        if (
            not isinstance(self.method, str)
            or not self.method
            or self.method != self.method.upper()
            or not isinstance(self.route_template, str)
            or not self.route_template.startswith("/")
        ):
            raise AccountingValidationError


@dataclass(frozen=True, slots=True, repr=False)
class AccountingRequestContext:
    method: str
    route_template: str
    policy: BillingPolicy
    request_id: str
    reservation: AccountingReservation
    parent_event_id: str | None = None

    def __post_init__(self) -> None:
        if (
            not isinstance(self.policy, BillingPolicy)
            or not isinstance(self.reservation, AccountingReservation)
            or not isinstance(self.request_id, str)
            or not self.request_id
            or self.reservation.request_id != self.request_id
            or classify_billing_policy(self.method, self.route_template) != self.policy
        ):
            raise AccountingValidationError
        object.__setattr__(
            self,
            "parent_event_id",
            _normalize_optional_event_id(self.parent_event_id),
        )

    def __repr__(self) -> str:
        return "<AccountingRequestContext>"


def _iter_effective_candidates(routes: Iterable[object]) -> Iterable[object]:
    for route in routes:
        contexts = getattr(route, "effective_route_contexts", None)
        if contexts is None:
            yield route
            continue
        if not callable(contexts):
            raise AccountingValidationError
        yield from contexts()


def resolve_effective_http_route(
    scope: Scope,
    routes: Iterable[object],
) -> EffectiveHttpRoute | None:
    """Resolve the first exact FULL HTTP route using public match capabilities."""
    if scope.get("type") != "http":
        return None
    method = scope.get("method")
    if not isinstance(method, str) or not method or method != method.upper():
        raise AccountingValidationError

    try:
        candidates = _iter_effective_candidates(routes)
        for candidate in candidates:
            matches = getattr(candidate, "matches", None)
            if not callable(matches):
                raise AccountingValidationError
            match_result = matches(scope)
            if not isinstance(match_result, tuple) or len(match_result) != 2:
                raise AccountingValidationError
            match = match_result[0]
            if not isinstance(match, Match):
                raise AccountingValidationError
            if match is not Match.FULL:
                continue

            methods = getattr(candidate, "methods", None)
            if methods is None:
                continue
            if isinstance(methods, (str, bytes)):
                raise AccountingValidationError
            try:
                normalized_methods = tuple(methods)
            except (TypeError, ValueError):
                raise AccountingValidationError from None
            if not normalized_methods or any(
                not isinstance(candidate_method, str)
                for candidate_method in normalized_methods
            ):
                raise AccountingValidationError
            if method not in normalized_methods:
                continue

            route_template = getattr(candidate, "path_format", None)
            if not isinstance(route_template, str) or not route_template:
                route_template = getattr(candidate, "path", None)
            if not isinstance(route_template, str) or not route_template.startswith("/"):
                raise AccountingValidationError
            return EffectiveHttpRoute(method=method, route_template=route_template)
    except AccountingError:
        raise
    except Exception:
        raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
    return None


def _request_state(scope: Scope) -> MutableMapping[str, Any] | None:
    state = scope.get("state")
    if state is None:
        return None
    if not isinstance(state, MutableMapping):
        raise AccountingValidationError
    return state


def get_accounting_request_context(scope: Scope) -> AccountingRequestContext | None:
    state = _request_state(scope)
    if state is None:
        return None
    context = state.get(ACCOUNTING_REQUEST_CONTEXT_STATE_KEY)
    if context is None:
        return None
    if not isinstance(context, AccountingRequestContext):
        raise AccountingValidationError
    return context


def publish_accounting_request_context(
    scope: Scope,
    context: AccountingRequestContext,
) -> None:
    if not isinstance(context, AccountingRequestContext):
        raise AccountingValidationError
    state = _request_state(scope)
    if state is None or ACCOUNTING_REQUEST_CONTEXT_STATE_KEY in state:
        raise AccountingValidationError
    state[ACCOUNTING_REQUEST_CONTEXT_STATE_KEY] = context


def take_accounting_request_context(scope: Scope) -> AccountingRequestContext | None:
    state = _request_state(scope)
    if state is None:
        return None
    context = state.pop(ACCOUNTING_REQUEST_CONTEXT_STATE_KEY, None)
    if context is None:
        return None
    if not isinstance(context, AccountingRequestContext):
        raise AccountingValidationError
    return context
