from __future__ import annotations

import ast
import json
from dataclasses import FrozenInstanceError
from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter, FastAPI
from starlette.requests import Request
from starlette.routing import Match

from llm_gateway_core.api.accounting_http import (
    AccountingHttpUse,
    accounting_error_response,
    map_accounting_error,
)
from llm_gateway_core.middleware.accounting_admission import (
    AccountingRequestContext,
    resolve_effective_http_route,
    get_accounting_request_context,
    publish_accounting_request_context,
    take_accounting_request_context,
)
from llm_gateway_core.services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReservation,
    AccountingValidationError,
    classify_billing_policy,
)


PROJECT_ROOT = Path(__file__).resolve().parents[1]


def _scope(path: str, *, method: str = "GET") -> dict[str, Any]:
    return {
        "type": "http",
        "asgi": {"version": "3.0"},
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": path,
        "raw_path": path.encode("ascii"),
        "root_path": "",
        "query_string": b"",
        "headers": [],
        "client": ("127.0.0.1", 12345),
        "server": ("testserver", 80),
        "state": {},
    }


def _context() -> AccountingRequestContext:
    policy = classify_billing_policy("POST", "/v1/embeddings")
    assert policy is not None
    return AccountingRequestContext(
        method="POST",
        route_template="/v1/embeddings",
        policy=policy,
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=0.1,
        ),
    )


def test_resolver_flattens_nested_included_routers_without_private_fastapi_types() -> None:
    inner = APIRouter()

    @inner.get("/jobs/{job_id}")
    async def get_job(job_id: str) -> dict[str, str]:
        return {"job_id": job_id}

    middle = APIRouter(prefix="/pdf")
    middle.include_router(inner)
    app = FastAPI()
    app.include_router(middle, prefix="/v1")

    route = resolve_effective_http_route(
        _scope("/v1/pdf/jobs/job-42"),
        app.router.routes,
    )

    assert route is not None
    assert route.method == "GET"
    assert route.route_template == "/v1/pdf/jobs/{job_id}"
    assert classify_billing_policy(route.method, route.route_template) is not None


def test_resolver_rejects_partial_method_suffix_and_lookalike_billing() -> None:
    router = APIRouter()

    @router.post("/v1/embeddings")
    async def embeddings() -> dict[str, bool]:
        return {"ok": True}

    @router.post("/v1/embeddings-extra")
    async def embeddings_extra() -> dict[str, bool]:
        return {"ok": True}

    assert resolve_effective_http_route(
        _scope("/v1/embeddings", method="GET"),
        router.routes,
    ) is None
    assert resolve_effective_http_route(
        _scope("/v1/embeddings/suffix", method="POST"),
        router.routes,
    ) is None

    lookalike = resolve_effective_http_route(
        _scope("/v1/embeddings-extra", method="POST"),
        router.routes,
    )
    assert lookalike is not None
    assert classify_billing_policy(
        lookalike.method,
        lookalike.route_template,
    ) is None


def test_resolver_preserves_first_full_route_order() -> None:
    router = APIRouter()

    @router.get("/v1/{rest:path}")
    async def catch_all(rest: str) -> dict[str, str]:
        return {"rest": rest}

    @router.get("/v1/pdf/jobs/{job_id}")
    async def exact(job_id: str) -> dict[str, str]:
        return {"job_id": job_id}

    route = resolve_effective_http_route(
        _scope("/v1/pdf/jobs/job-42"),
        router.routes,
    )

    assert route is not None
    assert route.route_template == "/v1/{rest}"


def test_resolver_fails_closed_for_malformed_full_candidate_and_match_error() -> None:
    class MalformedFull:
        methods = {"GET"}
        path_format = None

        @staticmethod
        def matches(_scope: object) -> tuple[Match, dict[str, object]]:
            return Match.FULL, {}

    class BrokenMatch:
        @staticmethod
        def matches(_scope: object) -> tuple[Match, dict[str, object]]:
            raise RuntimeError("unsafe route detail")

    class MalformedMethods:
        methods = {1}
        path_format = "/v1/test"

        @staticmethod
        def matches(_scope: object) -> tuple[Match, dict[str, object]]:
            return Match.FULL, {}

    with pytest.raises(AccountingError) as malformed:
        resolve_effective_http_route(_scope("/v1/test"), [MalformedFull()])
    assert malformed.value.code is AccountingErrorCode.INVALID_CONTRACT

    with pytest.raises(AccountingError) as malformed_methods:
        resolve_effective_http_route(_scope("/v1/test"), [MalformedMethods()])
    assert malformed_methods.value.code is AccountingErrorCode.INVALID_CONTRACT

    with pytest.raises(AccountingError) as broken:
        resolve_effective_http_route(_scope("/v1/test"), [BrokenMatch()])
    assert broken.value.code is AccountingErrorCode.ACCOUNTING_FAILED
    assert "unsafe route detail" not in str(broken.value)


def test_adapter_does_not_import_fastapi_private_route_types() -> None:
    path = (
        PROJECT_ROOT
        / "llm_gateway_core"
        / "middleware"
        / "accounting_admission.py"
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    imported_names = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }

    assert {
        "_IncludedRouter",
        "_EffectiveRouteContext",
        "_RouterIncludeContext",
    }.isdisjoint(imported_names)


def test_accounting_context_is_frozen_slotted_repr_safe_and_taken_once() -> None:
    context = _context()
    scope = _scope("/v1/embeddings", method="POST")
    publish_accounting_request_context(scope, context)

    assert not hasattr(context, "__dict__")
    assert repr(context) == "<AccountingRequestContext>"
    assert "request-1" not in repr(context)
    with pytest.raises(FrozenInstanceError):
        context.request_id = "changed"  # type: ignore[misc]
    with pytest.raises(AccountingError):
        publish_accounting_request_context(scope, context)

    assert get_accounting_request_context(scope) is context
    assert take_accounting_request_context(scope) is context
    assert get_accounting_request_context(scope) is None
    assert take_accounting_request_context(scope) is None


def test_accounting_context_appends_normalized_optional_parent_event_id() -> None:
    base = _context()

    context = AccountingRequestContext(
        base.method,
        base.route_template,
        base.policy,
        base.request_id,
        base.reservation,
        "  usage:v1:http:parent  ",
    )

    assert context.parent_event_id == "usage:v1:http:parent"


@pytest.mark.parametrize(
    "parent_event_id",
    ["", "event\nchild", "event\x00child", "event\x7fchild", "рarent", "x" * 256, 1],
)
def test_accounting_context_parent_event_id_uses_event_validation_semantics(
    parent_event_id: object,
) -> None:
    base = _context()

    with pytest.raises(AccountingValidationError):
        AccountingRequestContext(
            base.method,
            base.route_template,
            base.policy,
            base.request_id,
            base.reservation,
            parent_event_id,  # type: ignore[arg-type]
        )


@pytest.mark.parametrize("code", tuple(AccountingErrorCode))
def test_accounting_error_mapping_is_complete_and_safe(code: AccountingErrorCode) -> None:
    admission_expected = {
        AccountingErrorCode.BUDGET_EXHAUSTED: (429, "budget_exhausted"),
        AccountingErrorCode.RATE_LIMITED: (429, "rate_limited"),
        AccountingErrorCode.ACCOUNTING_IN_FLIGHT: (409, "accounting_in_flight"),
        AccountingErrorCode.BUDGET_RESET_IN_PROGRESS: (
            503,
            "budget_reset_in_progress",
        ),
        AccountingErrorCode.ACTIVE_SESSION_LIMIT: (503, "active_session_limit"),
    }
    admin_expected = {
        AccountingErrorCode.ACCOUNTING_IN_FLIGHT: (409, "accounting_in_flight"),
        AccountingErrorCode.BUDGET_RESET_IN_PROGRESS: (
            409,
            "budget_reset_in_progress",
        ),
    }

    admission = map_accounting_error(
        AccountingError(code),
        use=AccountingHttpUse.ADMISSION,
    )
    admin = map_accounting_error(
        AccountingError(code),
        use=AccountingHttpUse.ADMIN_MUTATION,
    )

    assert (admission.status_code, admission.public_code) == admission_expected.get(
        code,
        (503, "accounting_unavailable"),
    )
    assert (admin.status_code, admin.public_code) == admin_expected.get(
        code,
        (503, "accounting_unavailable"),
    )
    assert code.value not in admission.detail or code in admission_expected
    assert code.value not in admin.detail or code in admin_expected


def test_accounting_error_response_preserves_request_id_and_public_detail_only() -> None:
    scope = _scope("/v1/embeddings", method="POST")
    scope["state"]["llmgateway_request_id"] = "request-42"
    request = Request(scope)

    response = accounting_error_response(
        request,
        AccountingError(AccountingErrorCode.SOURCE_WRITE_FAILED),
        use=AccountingHttpUse.ADMISSION,
    )
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert response.headers["X-Request-ID"] == "request-42"
    assert payload["request_id"] == "request-42"
    assert payload["error"]["request_id"] == "request-42"
    assert payload["error"]["code"] == "accounting_unavailable"
    assert "source_write_failed" not in response.body.decode("utf-8")
