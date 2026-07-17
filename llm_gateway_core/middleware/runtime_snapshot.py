"""Bind one immutable runtime generation to each HTTP request."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from starlette.requests import Request
from starlette.types import ASGIApp, Receive, Scope, Send

from .content_size import (
    PRE_ROUTE_REJECTED_STATE_KEY,
    RUNTIME_INDEPENDENT_PATHS,
    UNMATCHED_ROUTE_STATE_KEY,
)
from ..services.runtime_config import (
    AppServices,
    RuntimeGenerationManager,
    RuntimeLease,
    RuntimeManagerStateError,
    RuntimeManagerStatus,
)

_UNAVAILABLE_DETAIL = "Gateway runtime is temporarily unavailable."
_REQUEST_RUNTIME_LEASE_STATE_KEY = "_llmgateway_originating_runtime_lease"
_REQUEST_RUNTIME_UNAVAILABLE_DETAIL = "Request runtime lease is unavailable."


class RuntimeAvailabilityMiddleware:
    """Reject protected HTTP traffic until the typed runtime is running."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or _runtime_independent(scope):
            await self.app(scope, receive, send)
            return

        manager = _runtime_manager(scope)
        if manager is None or manager.status is not RuntimeManagerStatus.RUNNING:
            await _send_runtime_unavailable(scope, send)
            return
        await self.app(scope, receive, send)


class RuntimeSnapshotMiddleware:
    """Hold a runtime lease until the downstream ASGI app has fully returned."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if _runtime_independent(scope):
            await self.app(scope, receive, send)
            return
        if _is_unmatched_route(scope):
            await self.app(scope, receive, send)
            return

        manager = _runtime_manager(scope)
        if manager is None:
            _mark_pre_route_rejected(scope)
            await _send_runtime_unavailable(scope, send)
            return

        try:
            lease = manager.acquire_current()
        except RuntimeManagerStateError:
            _mark_pre_route_rejected(scope)
            await _send_runtime_unavailable(scope, send)
            return

        state: dict[str, Any] | None = None
        try:
            state = scope.setdefault("state", {})
            state[_REQUEST_RUNTIME_LEASE_STATE_KEY] = lease
            state["runtime_snapshot"] = lease.snapshot
            await self.app(scope, receive, send)
        finally:
            try:
                if isinstance(state, dict):
                    state.pop(_REQUEST_RUNTIME_LEASE_STATE_KEY, None)
            finally:
                lease.release()


def retain_request_runtime(request: Request) -> RuntimeLease:
    """Retain the exact request generation; the caller owns the returned lease."""
    if not isinstance(request, Request):
        raise RuntimeManagerStateError(_REQUEST_RUNTIME_UNAVAILABLE_DETAIL)

    state = request.scope.get("state")
    if not isinstance(state, dict):
        raise RuntimeManagerStateError(_REQUEST_RUNTIME_UNAVAILABLE_DETAIL)
    lease = state.get(_REQUEST_RUNTIME_LEASE_STATE_KEY)
    if not isinstance(lease, RuntimeLease):
        raise RuntimeManagerStateError(_REQUEST_RUNTIME_UNAVAILABLE_DETAIL)

    try:
        return lease.retain()
    except RuntimeManagerStateError:
        raise RuntimeManagerStateError(
            _REQUEST_RUNTIME_UNAVAILABLE_DETAIL
        ) from None


def _runtime_manager(scope: Scope) -> RuntimeGenerationManager | None:
    try:
        services = scope["app"].state.services
    except (AttributeError, KeyError, TypeError):
        return None
    if not isinstance(services, AppServices):
        return None
    manager = services.runtime_manager
    if not isinstance(manager, RuntimeGenerationManager):
        return None
    return manager


def _runtime_independent(scope: Scope) -> bool:
    return scope.get("path") in RUNTIME_INDEPENDENT_PATHS


def _is_unmatched_route(scope: Scope) -> bool:
    state = scope.get("state")
    return isinstance(state, dict) and bool(state.get(UNMATCHED_ROUTE_STATE_KEY))


def _mark_pre_route_rejected(scope: Scope) -> None:
    scope.setdefault("state", {})[PRE_ROUTE_REJECTED_STATE_KEY] = True


def _request_id(scope: Scope) -> str | None:
    state = scope.get("state")
    if not isinstance(state, Mapping):
        return None
    request_id = state.get("llmgateway_request_id")
    if isinstance(request_id, str) and request_id:
        return request_id
    return None


async def _send_runtime_unavailable(scope: Scope, send: Send) -> None:
    payload: dict[str, Any] = {
        "detail": _UNAVAILABLE_DETAIL,
        "error": {
            "message": _UNAVAILABLE_DETAIL,
            "type": "internal_error",
            "code": "runtime_unavailable",
        },
    }
    request_id = _request_id(scope)
    if request_id is not None:
        payload["request_id"] = request_id
        payload["error"]["request_id"] = request_id

    body = json.dumps(payload, ensure_ascii=True, separators=(",", ":")).encode(
        "utf-8"
    )
    headers = [
        (b"content-type", b"application/json; charset=utf-8"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    if request_id is not None:
        headers.append((b"x-request-id", request_id.encode("latin-1")))

    await send({"type": "http.response.start", "status": 503, "headers": headers})
    await send({"type": "http.response.body", "body": body})
