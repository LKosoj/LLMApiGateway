from __future__ import annotations

from unittest.mock import create_autospec, patch

import pytest
from fastapi import FastAPI, Request
from fastapi.testclient import TestClient

from llm_gateway_core.db.api_keys_db import ApiKeyRecord, ApiKeysDB
from llm_gateway_core.middleware.accounting_admission import (
    AccountingRequestContext,
    publish_accounting_request_context,
    take_accounting_request_context,
)
from llm_gateway_core.middleware.auth import ApiKeyAuthMiddleware
from llm_gateway_core.middleware.request_logging import RequestLoggingASGIMiddleware
from llm_gateway_core.services.active_requests import ActiveRequestsRegistry
from llm_gateway_core.services.deep_research_accounting import (
    DeepResearchAuthIdentity,
    DeepResearchChildAdmission,
    DeepResearchContextError,
    DeepResearchContextErrorCode,
    DeepResearchResolvedContext,
)
from llm_gateway_core.services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReservation,
    classify_billing_policy,
)
from tests.runtime_test_support import bind_app_services


MASTER_HEADERS = {"Authorization": "Bearer master-key"}


def _app(
    *,
    api_keys_db: ApiKeysDB | None = None,
    accounting_service: object | None = None,
    request_logging: bool = True,
) -> tuple[FastAPI, object]:
    app = FastAPI()
    overrides = {} if api_keys_db is None else {"api_keys_db": api_keys_db}
    if accounting_service is not None:
        overrides["accounting_service"] = accounting_service
    services = bind_app_services(app, **overrides)
    app.add_middleware(ApiKeyAuthMiddleware)
    if request_logging:
        app.add_middleware(RequestLoggingASGIMiddleware)

    @app.post("/v1/embeddings")
    async def embeddings() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/shadow/v1/embeddings")
    async def shadow_embeddings() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/embeddings")
    async def get_embeddings() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/v1/audio/voices")
    async def get_audio_voices() -> dict[str, bool]:
        return {"ok": True}

    @app.post("/v1/v1/messages/count_tokens")
    async def post_count_tokens_double_prefix() -> dict[str, bool]:
        return {"ok": True}

    return app, services


def _virtual_record() -> ApiKeyRecord:
    return ApiKeyRecord(
        id=17,
        name="virtual",
        api_key="virtual-key",
        budget_usd=12.5,
        spent_usd=1.25,
        rpm=3,
        tpm=400,
    )


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_exact_master_route_reserves_and_auth_releases_unclaimed_context() -> None:
    app, services = _app()

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 200
    request_id = response.headers["X-Request-ID"]
    services.accounting_service.reserve.assert_awaited_once_with(
        request_id=request_id,
        api_key_id=None,
        budget_usd=None,
        spent_usd=0.0,
        rpm_limit=None,
        tpm_limit=None,
        estimate_usd=services.usd_budget_ledger.default_estimate_usd,
    )
    services.accounting_service.release.assert_awaited_once()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_virtual_key_is_authoritative_for_accounting_admission() -> None:
    record = _virtual_record()
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert response.status_code == 200
    assert services.accounting_service.reserve.await_args.kwargs == {
        "request_id": response.headers["X-Request-ID"],
        "api_key_id": record.id,
        "budget_usd": record.budget_usd,
        "spent_usd": record.spent_usd,
        "rpm_limit": record.rpm,
        "tpm_limit": record.tpm,
        "estimate_usd": services.usd_budget_ledger.default_estimate_usd,
    }


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_negative_virtual_key_budget_is_admitted_as_unlimited() -> None:
    record = _virtual_record()
    record.budget_usd = -1.0
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert response.status_code == 200
    assert services.accounting_service.reserve.await_args.kwargs["budget_usd"] is None


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_non_billing_route_and_method_do_not_touch_accounting() -> None:
    app, services = _app()

    with TestClient(app) as client:
        lookalike = client.post("/shadow/v1/embeddings", headers=MASTER_HEADERS)
        method_mismatch = client.get("/v1/embeddings", headers=MASTER_HEADERS)

    assert lookalike.status_code == 200
    assert method_mismatch.status_code == 200
    services.accounting_service.reserve.assert_not_awaited()
    services.accounting_service.release.assert_not_awaited()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_billable_route_requires_outer_request_id_without_fallback() -> None:
    app, services = _app(request_logging=False)

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "accounting_unavailable"
    services.accounting_service.reserve.assert_not_awaited()


@pytest.mark.parametrize(
    "request_id",
    ("", "   ", "bad id", "bad\nid", "汉", "x" * 256),
)
@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_malformed_outer_request_id_fails_before_reserve(request_id: str) -> None:
    app, services = _app()

    with patch(
        "llm_gateway_core.middleware.request_logging.uuid.uuid4",
        return_value=request_id,
    ):
        with TestClient(app) as client:
            response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "accounting_unavailable"
    assert "request_id" not in response.json()
    assert "request_id" not in response.json()["error"]
    assert "X-Request-ID" not in response.headers
    services.accounting_service.reserve.assert_not_awaited()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_invalid_accounting_dependency_fails_before_reserve() -> None:
    app, _services = _app(accounting_service=object())

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "accounting_unavailable"


@pytest.mark.parametrize(
    ("code", "status_code", "public_code"),
    (
        (AccountingErrorCode.BUDGET_EXHAUSTED, 429, "budget_exhausted"),
        (AccountingErrorCode.RATE_LIMITED, 429, "rate_limited"),
        (AccountingErrorCode.ACCOUNTING_IN_FLIGHT, 409, "accounting_in_flight"),
        (AccountingErrorCode.ADMISSION_CLOSED, 503, "accounting_unavailable"),
    ),
)
@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_admission_failure_uses_credential_safe_accounting_envelope(
    code: AccountingErrorCode,
    status_code: int,
    public_code: str,
) -> None:
    app, services = _app()
    services.accounting_service.reserve.side_effect = AccountingError(code)

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == status_code
    assert response.json()["error"]["code"] == public_code
    assert "AccountingError" not in response.text


@pytest.mark.parametrize(
    ("code", "category"),
    (
        (AccountingErrorCode.BUDGET_EXHAUSTED, "budget_exhausted"),
        (AccountingErrorCode.RATE_LIMITED, "rate_limited"),
    ),
)
@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_admission_failure_records_rejection_for_budget_and_rate_limit(
    code: AccountingErrorCode,
    category: str,
) -> None:
    record = _virtual_record()
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)
    services.accounting_service.reserve.side_effect = AccountingError(code)

    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert response.status_code == 429
    services.rejections_db.insert_rejection.assert_called_once()
    kwargs = services.rejections_db.insert_rejection.call_args.kwargs
    assert kwargs["category"] == category
    assert kwargs["status_code"] == 429
    assert kwargs["api_key_id"] == record.id


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_admission_failure_does_not_record_rejection_for_transient_codes() -> None:
    app, services = _app()
    services.accounting_service.reserve.side_effect = AccountingError(
        AccountingErrorCode.ACCOUNTING_IN_FLIGHT
    )

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 409
    services.rejections_db.insert_rejection.assert_not_called()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_failure_after_reserve_releases_once() -> None:
    app, services = _app()

    with patch(
        "llm_gateway_core.middleware.auth.publish_accounting_request_context",
        side_effect=RuntimeError("publish failed"),
    ):
        with TestClient(app) as client:
            response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    services.accounting_service.reserve.assert_awaited_once()
    services.accounting_service.release.assert_awaited_once()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_partial_active_request_start_is_finished_and_released_once() -> None:
    registry = ActiveRequestsRegistry()
    app = FastAPI()
    services = bind_app_services(app, active_requests_registry=registry)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    @app.post("/v1/embeddings")
    async def embeddings() -> dict[str, bool]:
        return {"ok": True}

    original_start = registry.start

    def partial_start(**kwargs: object) -> None:
        original_start(**kwargs)  # type: ignore[arg-type]
        raise RuntimeError("start failed after insert")

    with patch.object(registry, "start", side_effect=partial_start):
        with TestClient(app) as client:
            response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    assert registry.list_records() == []
    services.accounting_service.reserve.assert_awaited_once()
    services.accounting_service.release.assert_awaited_once()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_consumed_context_is_not_released_by_auth() -> None:
    app = FastAPI()
    services = bind_app_services(app)
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, bool]:
        assert take_accounting_request_context(request.scope) is not None
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 200
    services.accounting_service.release.assert_not_awaited()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_duplicate_context_is_rejected_before_reserve() -> None:
    app = FastAPI()
    services = bind_app_services(app)
    app.add_middleware(ApiKeyAuthMiddleware)

    class DuplicateContextMiddleware:
        def __init__(self, wrapped):
            self.wrapped = wrapped

        async def __call__(self, scope, receive, send):
            if scope["type"] == "http":
                request_id = scope["state"]["llmgateway_request_id"]
                policy = classify_billing_policy("POST", "/v1/embeddings")
                assert policy is not None
                publish_accounting_request_context(
                    scope,
                    AccountingRequestContext(
                        method="POST",
                        route_template="/v1/embeddings",
                        policy=policy,
                        request_id=request_id,
                        reservation=AccountingReservation(
                            reservation_id="existing-reservation",
                            request_id=request_id,
                            api_key_id=None,
                            reserved_usd=0.0,
                        ),
                    ),
                )
            await self.wrapped(scope, receive, send)

    app.add_middleware(DuplicateContextMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    @app.post("/v1/embeddings")
    async def embeddings() -> dict[str, bool]:
        return {"ok": True}

    with TestClient(app) as client:
        response = client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert response.status_code == 503
    services.accounting_service.reserve.assert_not_awaited()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_downstream_exception_identity_survives_cleanup_failure() -> None:
    app = FastAPI()
    services = bind_app_services(app)
    services.accounting_service.release.side_effect = RuntimeError("cleanup failed")
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)
    sentinel = RuntimeError("downstream failed")

    @app.post("/v1/embeddings")
    async def embeddings() -> dict[str, bool]:
        raise sentinel

    with TestClient(app) as client:
        with pytest.raises(RuntimeError) as caught:
            client.post("/v1/embeddings", headers=MASTER_HEADERS)

    assert caught.value is sentinel
    services.accounting_service.release.assert_awaited_once()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_authentication_failures_precede_accounting() -> None:
    app, services = _app()

    with TestClient(app) as client:
        missing = client.post("/v1/embeddings")
        invalid = client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer wrong"},
        )

    assert missing.status_code == 401
    assert invalid.status_code == 403
    services.accounting_service.reserve.assert_not_awaited()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_signed_deep_research_child_uses_sanitized_identity_and_child_reserve() -> None:
    app = FastAPI()
    services = bind_app_services(app)
    token = "dr1.signed-context"
    services.accounting_service.resolve_deep_research_context.return_value = (
        DeepResearchResolvedContext(
            context_id="a" * 32,
            parent_event_id="deep-parent-request",
            auth_identity=DeepResearchAuthIdentity(
                api_key_id=17,
                allowed_models=("gateway-embedding",),
            ),
        )
    )

    async def reserve_child(_token, *, request_id, estimate_usd):
        return DeepResearchChildAdmission(
            reservation=AccountingReservation(
                reservation_id="deep-child-reservation",
                request_id=request_id,
                api_key_id=17,
                reserved_usd=estimate_usd,
            ),
            parent_event_id="deep-parent-request",
            ordinal=0,
        )

    services.accounting_service.reserve_deep_research_child.side_effect = reserve_child
    app.add_middleware(ApiKeyAuthMiddleware)
    app.add_middleware(RequestLoggingASGIMiddleware)

    @app.post("/v1/embeddings")
    async def embeddings(request: Request) -> dict[str, object]:
        context = take_accounting_request_context(request.scope)
        assert context is not None
        record = request.state.api_key_record
        await services.accounting_service.release(context.reservation)
        return {
            "api_key_id": record.id,
            "api_key": record.api_key,
            "allowed_models": record.allowed_models,
            "parent_event_id": context.parent_event_id,
        }

    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": f"Bearer {token}"},
        )

    assert response.status_code == 200
    assert response.json() == {
        "api_key_id": 17,
        "api_key": "",
        "allowed_models": ["gateway-embedding"],
        "parent_event_id": "deep-parent-request",
    }
    services.api_keys_db.get_by_key.assert_not_called()
    services.accounting_service.reserve.assert_not_awaited()
    services.accounting_service.reserve_deep_research_child.assert_awaited_once()


@pytest.mark.parametrize(
    ("path", "resolve_error"),
    (
        (
            "/v1/embeddings",
            DeepResearchContextError(DeepResearchContextErrorCode.INVALID),
        ),
        ("/shadow/v1/embeddings", None),
    ),
)
@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_deep_research_token_fails_closed_without_virtual_key_fallback(
    path: str,
    resolve_error: BaseException | None,
) -> None:
    app, services = _app()
    if resolve_error is not None:
        services.accounting_service.resolve_deep_research_context.side_effect = (
            resolve_error
        )

    with TestClient(app) as client:
        response = client.post(
            path,
            headers={"Authorization": "Bearer dr1.invalid-context"},
        )

    assert response.status_code == 403
    services.api_keys_db.get_by_key.assert_not_called()
    services.accounting_service.reserve.assert_not_awaited()
    services.accounting_service.reserve_deep_research_child.assert_not_awaited()
    if resolve_error is None:
        services.accounting_service.resolve_deep_research_context.assert_not_called()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_rate_limited_virtual_key_is_blocked_on_route_without_billing_policy() -> None:
    """GET /v1/audio/voices has no billing policy and used to skip RPM/TPM
    enforcement entirely. It is carried over from the pre-refactor rate
    limiter via _RATE_LIMIT_ONLY_ROUTES, so the rate-limit-only admission
    must close that gap without ever touching AccountingService.reserve()."""
    record = _virtual_record()
    record.rpm = 1
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        first = client.get(
            "/v1/audio/voices",
            headers={"Authorization": "Bearer virtual-key"},
        )
        second = client.get(
            "/v1/audio/voices",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    services.accounting_service.reserve.assert_not_awaited()
    services.rejections_db.insert_rejection.assert_called_once()
    assert (
        services.rejections_db.insert_rejection.call_args.kwargs["category"]
        == "rate_limited"
    )


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_rate_limited_virtual_key_is_blocked_on_double_prefixed_count_tokens_route() -> None:
    """POST /v1/v1/messages/count_tokens is the "Double Prefix Compat" mount
    of POST /v1/messages/count_tokens (api/v1/__init__.py includes
    anthropic_router twice: once bare and once under prefix="/v1", so SDKs
    that add their own /v1 still resolve). Just like the single-prefix route,
    it has no billing policy and must be caught by the same
    _RATE_LIMIT_ONLY_ROUTES allowlist entry -- otherwise it is a second,
    unlimited path to the same handler that fully bypasses RPM/TPM."""
    record = _virtual_record()
    record.rpm = 1
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        first = client.post(
            "/v1/v1/messages/count_tokens",
            headers={"Authorization": "Bearer virtual-key"},
        )
        second = client.post(
            "/v1/v1/messages/count_tokens",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 429
    assert second.json()["error"]["code"] == "rate_limited"
    services.accounting_service.reserve.assert_not_awaited()
    services.rejections_db.insert_rejection.assert_called_once()
    assert (
        services.rejections_db.insert_rejection.call_args.kwargs["category"]
        == "rate_limited"
    )


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_dashboard_route_without_billing_policy_is_not_rate_limited() -> None:
    """GET /v1/embeddings has no billing policy and is not one of the two
    routes carried over from the pre-refactor rate limiter
    (_RATE_LIMIT_ONLY_ROUTES only lists GET /v1/audio/voices and POST
    /v1/messages/count_tokens). Dashboard-style routes without a billing
    policy (e.g. GET /v1/api/quota/keys, GET /v1/ui/usage-stats) must not be
    forced through the rate limiter: a `policy is None` catch-all would make
    an open dashboard tab polling every few seconds exhaust the same
    RateLimiter window the caller's real LLM requests depend on."""
    record = _virtual_record()
    record.rpm = 1
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        first = client.get(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )
        second = client.get(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    services.accounting_service.reserve.assert_not_awaited()
    services.rejections_db.insert_rejection.assert_not_called()


@patch("llm_gateway_core.middleware.auth.settings.gateway_api_key", "master-key")
def test_billing_policy_route_does_not_trigger_new_rate_limit_only_admission() -> None:
    """A route with a billing policy must keep enforcing RPM/TPM exclusively
    through AccountingService.reserve() (mocked here) -- the rate-limit-only
    admission is gated on `policy is None` and must never also run for this
    route, or the same request would be admitted against the sliding window
    twice."""
    record = _virtual_record()
    record.rpm = 1
    api_keys_db = create_autospec(ApiKeysDB, instance=True, spec_set=True)
    api_keys_db.get_by_key.return_value = record
    app, services = _app(api_keys_db=api_keys_db)

    with TestClient(app) as client:
        response = client.post(
            "/v1/embeddings",
            headers={"Authorization": "Bearer virtual-key"},
        )

    assert response.status_code == 200
    assert services.rate_limiter.get_window_snapshot(record.id) == (0, 0, None)
