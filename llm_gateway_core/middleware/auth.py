import asyncio
import logging
import secrets
import time
import hmac
import hashlib
from typing import TYPE_CHECKING, NamedTuple, cast
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response
from fastapi.security import APIKeyHeader
from starlette.routing import Match
from starlette.types import ASGIApp, Receive, Scope, Send

from ..api.accounting_http import (
    AccountingHttpUse,
    accounting_error_response,
    map_accounting_error,
)
from ..api.error_envelope import error_response
from ..config.settings import settings
from ..db.api_keys_db import ApiKeyRecord, ApiKeysDB
from ..db.rejections_db import record_rejection
from ..middleware.content_size import (
    RUNTIME_INDEPENDENT_PATHS,
    UNMATCHED_ROUTE_STATE_KEY,
)
from ..middleware.request_logging import is_valid_request_id
from ..middleware.response_observation import response_observation_published
from ..middleware.accounting_admission import (
    AccountingRequestContext,
    get_accounting_request_context,
    publish_accounting_request_context,
    resolve_effective_http_route,
    take_accounting_request_context,
)
from ..services import session_secret
from ..services.accounting import (
    AccountingError,
    AccountingErrorCode,
    AccountingReservation,
    AccountingValidationError,
    classify_billing_policy,
)
from ..services.accounting_service import AccountingService
from ..services.active_requests import ActiveRequestsRegistry
from ..services.deep_research_accounting import (
    DEEP_RESEARCH_CONTEXT_TOKEN_PREFIX,
    DeepResearchContextError,
)
from ..services.ip_blocklist import IpBlockGuard
from ..utils.client_ip import get_client_ip

if TYPE_CHECKING:
    from ..services.runtime_config import AppServices

api_key_header = APIKeyHeader(name="Authorization", auto_error=False)
ANTHROPIC_API_KEY_HEADER_NAME = "x-api-key"

DEFAULT_UI_PATH = "/v1/ui/usage-stats"
LOGIN_PATH = "/auth/login"
SESSION_COOKIE_NAME = "llmgateway_session"
SESSION_TTL_SECONDS = 365 * 24 * 60 * 60
SESSION_HMAC_CONFIGURATION_ERROR = "GATEWAY_API_KEY must be configured for session HMAC signing."
API_KEY_DISABLED_DETAIL = "api_key_disabled"
API_KEY_DISABLED_HTML = "API key disabled"
API_KEY_INVALID_DETAIL = "Invalid API Key"

ROLE_MASTER = "master"
ROLE_USER = "user"

_DEEP_RESEARCH_CHILD_ROUTES = frozenset(
    {
        ("POST", "/v1/chat/completions"),
        ("POST", "/v1/embeddings"),
        ("POST", "/v1/images/generations"),
    }
)

# Routes that have no billing policy but were rate-limited pre-refactor and
# must keep going through _admit_rate_limit_only. This is deliberately an
# allowlist, not "any policy-less route": dashboard routes such as
# GET /v1/api/quota/keys or GET /v1/ui/usage-stats also have no billing
# policy, are polled every few seconds by the UI, and share the same
# RateLimiter partitioning (by key_id only, not by route) as billed routes --
# forcing them through the rate limiter would let an open dashboard tab
# exhaust the caller's real LLM request budget.
_RATE_LIMIT_ONLY_ROUTES = frozenset(
    {
        ("GET", "/v1/audio/voices"),
        ("POST", "/v1/messages/count_tokens"),
        # anthropic_router is mounted twice (api/v1/__init__.py): once bare
        # and once under prefix="/v1" for SDKs that already add their own
        # /v1. Both registered route templates must be allowlisted, or the
        # double-prefix path bypasses RPM/TPM entirely.
        ("POST", "/v1/v1/messages/count_tokens"),
    }
)

PUBLIC_EXACT_PATHS = {*RUNTIME_INDEPENDENT_PATHS, LOGIN_PATH}
PUBLIC_PREFIXES = ("/static/",)
OPTIONAL_AUTH_PATHS = {"/"}
# Paths that accept X-Api-Key header (Anthropic SDK style authentication)
ANTHROPIC_API_PREFIXES = (
    "/v1/messages",
    "/v1/models",
    "/v1/v1/messages",
    "/v1/v1/models"
)

# Routes only the master role (GATEWAY_API_KEY) may access. Virtual-key
# callers get a 403 regardless of their individual permissions.
MASTER_ONLY_PREFIXES = (
    "/v1/ui/rules-editor",
    "/v1/ui/api-keys",
    "/v1/ui/playground",
    "/v1/ui/web-playground",
    "/v1/ui/translator-debug",
    "/v1/ui/pricing",
    "/v1/ui/rejections",
    "/v1/ui/providers-config",
    "/v1/config/",
    "/v1/openrouter/free-models",
    "/v1/fallback-model-evals",
    "/v1/admin/",
)

def _matches_path_prefix(path: str, prefix: str) -> bool:
    return path == prefix.rstrip("/") or path.startswith(prefix)


def is_public_path(path: str) -> bool:
    if path in PUBLIC_EXACT_PATHS:
        return True

    return any(_matches_path_prefix(path, prefix) for prefix in PUBLIC_PREFIXES)


def requires_api_key(path: str) -> bool:
    return path not in OPTIONAL_AUTH_PATHS and not is_public_path(path)


def _accepts_anthropic_api_key(path: str) -> bool:
    return any(_matches_path_prefix(path, prefix) for prefix in ANTHROPIC_API_PREFIXES)


def _is_master_only_path(path: str) -> bool:
    return any(_matches_path_prefix(path, prefix) for prefix in MASTER_ONLY_PREFIXES)


def _matches_registered_route(request: Request) -> bool:
    for route in request.app.router.routes:
        match, _ = route.matches(request.scope)
        if match is not Match.NONE:
            return True

    return False


def _tokens_match(provided: str | None, expected: str | None) -> bool:
    """Constant-time comparison of API keys to prevent timing attacks."""
    if not provided or not expected:
        return False
    return hmac.compare_digest(provided.encode("utf-8"), expected.encode("utf-8"))


def _extract_bearer_token(auth_header: str) -> str:
    parts = auth_header.split()

    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid Authorization header format",
        )
    return parts[1]


def _get_session_hmac_secret() -> bytes | None:
    secret = session_secret.get_session_secret(settings.gateway_api_key)
    if secret is None:
        logging.error(SESSION_HMAC_CONFIGURATION_ERROR)
    return secret


def is_session_hmac_configured() -> bool:
    return _get_session_hmac_secret() is not None


def _build_session_signature_payload(
    issued_at: int,
    expires_at: int,
    nonce: str,
    role: str = ROLE_MASTER,
    key_id: int | None = None,
) -> bytes:
    key_id_token = str(key_id) if key_id is not None else ""
    return f"{issued_at}.{expires_at}.{nonce}.{role}.{key_id_token}".encode("utf-8")


def _build_session_signature(
    issued_at: int,
    expires_at: int,
    nonce: str,
    role: str = ROLE_MASTER,
    key_id: int | None = None,
) -> str:
    secret = _get_session_hmac_secret()
    if secret is None:
        raise RuntimeError(SESSION_HMAC_CONFIGURATION_ERROR)
    return hmac.new(
        secret,
        _build_session_signature_payload(issued_at, expires_at, nonce, role, key_id),
        hashlib.sha256,
    ).hexdigest()


def verify_session_hmac(
    signature: str,
    issued_at: int,
    expires_at: int,
    nonce: str,
    role: str = ROLE_MASTER,
    key_id: int | None = None,
) -> bool:
    secret = _get_session_hmac_secret()
    if secret is None:
        return False
    expected_signature = hmac.new(
        secret,
        _build_session_signature_payload(issued_at, expires_at, nonce, role, key_id),
        hashlib.sha256,
    ).hexdigest()
    return hmac.compare_digest(signature, expected_signature)


def create_authenticated_session(
    *, role: str = ROLE_MASTER, key_id: int | None = None
) -> str:
    issued_at = int(time.time())
    expires_at = issued_at + SESSION_TTL_SECONDS
    nonce = secrets.token_urlsafe(24)
    key_id_token = str(key_id) if key_id is not None else ""
    signature = _build_session_signature(issued_at, expires_at, nonce, role, key_id)
    return f"{issued_at}.{expires_at}.{nonce}.{role}.{key_id_token}.{signature}"


def invalidate_authenticated_session(session_id: str | None) -> None:
    return None


def set_authenticated_session_cookie(response: Response, session_id: str) -> None:
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        max_age=SESSION_TTL_SECONDS,
        httponly=True,
        secure=settings.session_cookie_secure,
        samesite="lax",
        path="/",
    )


def clear_authenticated_session_cookie(response: Response) -> None:
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        path="/",
        secure=settings.session_cookie_secure,
        httponly=True,
        samesite="lax",
    )


def normalize_next_path(next_path: str | None) -> str:
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
        return DEFAULT_UI_PATH

    # Browsers normalize backslashes to forward slashes before resolving a
    # Location header, so "/\evil.com" becomes a protocol-relative
    # "//evil.com" redirect even though it passed the "//" check above.
    # Control characters have no legitimate use in a same-site path either.
    if any(char == "\\" or ord(char) < 0x20 or ord(char) == 0x7F for char in next_path):
        return DEFAULT_UI_PATH

    if next_path == LOGIN_PATH or next_path.startswith(f"{LOGIN_PATH}?"):
        return DEFAULT_UI_PATH

    return next_path


def build_login_redirect_path(request: Request, next_path: str | None = None) -> str:
    target_path = normalize_next_path(next_path or f"{request.url.path}{f'?{request.url.query}' if request.url.query else ''}")
    return f"{LOGIN_PATH}?next={quote(target_path, safe='/?:=&')}"


def is_request_authenticated(request: Request) -> bool:
    return bool(getattr(request.state, "gateway_authenticated", False))


def _is_html_navigation(request: Request) -> bool:
    if request.method not in {"GET", "HEAD"}:
        return False

    accept_header = request.headers.get("accept", "")
    return "text/html" in accept_header.lower()


class _ParsedSession(NamedTuple):
    role: str
    key_id: int | None
    nonce: str
    expires_at: int
    issued_at: int


def _parse_session_claims(session_id: str | None) -> _ParsedSession | None:
    """Return the validated claims for a session cookie, else ``None``.

    Accepts both the legacy 4-part format (master-only, no key id) and the
    extended 6-part format with role + key_id.
    """
    if not session_id:
        return None

    parts = session_id.split(".")
    if len(parts) == 4:
        issued_at_str, expires_at_str, nonce, signature = parts
        role = ROLE_MASTER
        key_id: int | None = None
    elif len(parts) == 6:
        issued_at_str, expires_at_str, nonce, role, key_id_token, signature = parts
        if role not in (ROLE_MASTER, ROLE_USER):
            return None
        if key_id_token:
            try:
                key_id = int(key_id_token)
            except ValueError:
                return None
        else:
            # ROLE_USER sessions must carry a key_id — a blank key_id_token on
            # a user-role cookie means either corruption or a forged attempt
            # to pass access_control checks without binding to a real key.
            if role == ROLE_USER:
                return None
            key_id = None
    else:
        return None

    try:
        issued_at = int(issued_at_str)
        expires_at = int(expires_at_str)
    except ValueError:
        return None

    if expires_at <= issued_at or expires_at <= int(time.time()):
        return None

    if not verify_session_hmac(signature, issued_at, expires_at, nonce, role, key_id):
        return None
    return _ParsedSession(
        role=role, key_id=key_id, nonce=nonce, expires_at=expires_at, issued_at=issued_at
    )


def _parse_session(session_id: str | None) -> tuple[str, int | None] | None:
    """Compatibility wrapper returning ``(role, key_id)`` for callers that
    don't need the nonce/expiry claims."""
    parsed = _parse_session_claims(session_id)
    if parsed is None:
        return None
    return parsed.role, parsed.key_id


def _has_valid_session(session_id: str | None) -> bool:
    return _parse_session(session_id) is not None


async def _lookup_virtual_key(db: ApiKeysDB, token: str) -> ApiKeyRecord | None:
    try:
        return await asyncio.to_thread(db.get_by_key, token)
    except Exception:
        logging.exception("Virtual key lookup failed")
        return None


def _apply_virtual_key_auth(request: Request, record: ApiKeyRecord) -> None:
    if record.disabled:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=API_KEY_DISABLED_DETAIL,
        )
    request.state.api_key_id = record.id
    request.state.api_key_role = ROLE_USER
    request.state.api_key_record = record


def _apply_deep_research_context_auth(
    request: Request,
    accounting_service: AccountingService,
    token: str,
) -> None:
    route = resolve_effective_http_route(request.scope, request.app.router.routes)
    if route is None or (route.method, route.route_template) not in _DEEP_RESEARCH_CHILD_ROUTES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=API_KEY_INVALID_DETAIL,
        )
    try:
        resolved = accounting_service.resolve_deep_research_context(token)
    except DeepResearchContextError:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=API_KEY_INVALID_DETAIL,
        ) from None

    identity = resolved.auth_identity
    request.state.llmgateway_deep_research_context_token = token
    request.state.api_key_id = identity.api_key_id
    if identity.api_key_id is None:
        request.state.api_key_role = ROLE_MASTER
        request.state.api_key_record = None
        return
    request.state.api_key_role = ROLE_USER
    request.state.api_key_record = ApiKeyRecord(
        id=identity.api_key_id,
        name="deep-research-child",
        api_key="",
        budget_usd=None,
        spent_usd=0.0,
        rpm=None,
        tpm=None,
        allowed_models=list(identity.allowed_models),
    )


def _accounting_identity(
    request: Request,
) -> tuple[int | None, float | None, float, int | None, int | None]:
    record = getattr(request.state, "api_key_record", None)
    if record is None:
        return None, None, 0.0, None, None
    if not isinstance(record, ApiKeyRecord):
        raise AccountingValidationError
    budget_usd = record.budget_usd
    if budget_usd is not None and budget_usd < 0:
        budget_usd = None
    return record.id, budget_usd, record.spent_usd, record.rpm, record.tpm


async def _release_failed_admission(
    accounting_service: AccountingService,
    reservation: AccountingReservation,
) -> None:
    try:
        await accounting_service.release(reservation)
    except BaseException:
        logging.exception("Accounting admission cleanup failed")


def _admit_rate_limit_only(
    request: Request,
    services: "AppServices",
) -> None:
    """Enforce RPM/TPM for the _RATE_LIMIT_ONLY_ROUTES allowlist.

    AccountingService.reserve() owns RPM/TPM together with budget for every
    billed route. Routes without a policy skip reserve() entirely and
    therefore never touch the rate limiter -- this restores that enforcement
    for exactly the allowlisted routes (see the caller below), using the same
    RateLimiter instance and admit/attribute pairing as reserve()/release()
    so the sliding window stays consistent with billed routes, without
    reserving budget or an accounting session slot.
    """
    record = getattr(request.state, "api_key_record", None)
    if not isinstance(record, ApiKeyRecord):
        return
    if record.rpm is None and record.tpm is None:
        return
    request_id = getattr(request.state, "llmgateway_request_id", None)
    if not is_valid_request_id(request_id):
        raise AccountingValidationError
    error = services.rate_limiter.admit_request(
        request_id,
        record.id,
        rpm_limit=record.rpm,
        tpm_limit=record.tpm,
    )
    if error is not None:
        raise AccountingError(AccountingErrorCode.RATE_LIMITED)
    request.state.llmgateway_rate_limit_admission_key_id = record.id


async def _admit_accounting_if_needed(
    request: Request,
    services: "AppServices",
) -> str | None:
    route = resolve_effective_http_route(request.scope, request.app.router.routes)
    if route is None:
        return None
    policy = classify_billing_policy(route.method, route.route_template)
    if policy is None:
        if (route.method, route.route_template) in _RATE_LIMIT_ONLY_ROUTES:
            _admit_rate_limit_only(request, services)
        return None
    if get_accounting_request_context(request.scope) is not None:
        raise AccountingValidationError

    request_id = getattr(request.state, "llmgateway_request_id", None)
    if not is_valid_request_id(request_id):
        request.state.llmgateway_request_id = None
        raise AccountingValidationError
    accounting_service = services.accounting_service
    if not isinstance(accounting_service, AccountingService):
        raise AccountingValidationError

    child_token = getattr(
        request.state,
        "llmgateway_deep_research_context_token",
        None,
    )
    parent_event_id: str | None = None
    if child_token is None:
        api_key_id, budget_usd, spent_usd, rpm_limit, tpm_limit = (
            _accounting_identity(request)
        )
        reservation = await accounting_service.reserve(
            request_id=request_id,
            api_key_id=api_key_id,
            budget_usd=budget_usd,
            spent_usd=spent_usd,
            rpm_limit=rpm_limit,
            tpm_limit=tpm_limit,
            estimate_usd=services.usd_budget_ledger.default_estimate_usd,
        )
    else:
        if not isinstance(child_token, str):
            raise AccountingValidationError
        child_admission = await accounting_service.reserve_deep_research_child(
            child_token,
            request_id=request_id,
            estimate_usd=services.usd_budget_ledger.default_estimate_usd,
        )
        reservation = child_admission.reservation
        api_key_id = reservation.api_key_id
        parent_event_id = child_admission.parent_event_id

    try:
        context = AccountingRequestContext(
            method=route.method,
            route_template=route.route_template,
            policy=policy,
            request_id=request_id,
            reservation=reservation,
            parent_event_id=parent_event_id,
        )
        publish_accounting_request_context(request.scope, context)
        x_title = request.headers.get("x-title")
        if isinstance(x_title, str):
            x_title = x_title.strip() or None
        services.active_requests_registry.start(
            request_id=request_id,
            path=request.url.path,
            api_key_id=api_key_id,
            gateway_model=getattr(
                request.state,
                "llmgateway_gateway_model",
                None,
            ),
            operation=policy.operation,
            x_title=x_title,
        )
        request.state.llmgateway_active_request_id = request_id
        return request_id
    except BaseException as exc:
        try:
            take_accounting_request_context(request.scope)
        except AccountingError:
            logging.exception("Accounting context cleanup failed")
        try:
            services.active_requests_registry.finish(request_id)
        except BaseException:
            logging.exception("Active request admission cleanup failed")
        await _release_failed_admission(accounting_service, reservation)
        if isinstance(exc, AccountingError):
            raise
        if isinstance(exc, Exception):
            raise AccountingError(AccountingErrorCode.ACCOUNTING_FAILED) from None
        raise


def _finish_active_request(
    registry: ActiveRequestsRegistry,
    request_id: str | None,
) -> None:
    if request_id:
        registry.finish(request_id)


async def _authenticate_request(
    request: Request,
    services: "AppServices",
) -> tuple[bool, str | None]:
    api_keys_db = services.api_keys_db
    auth_header = await api_key_header(request)
    if auth_header:
        api_key = _extract_bearer_token(auth_header)
        if api_key.startswith(DEEP_RESEARCH_CONTEXT_TOKEN_PREFIX):
            accounting_service = services.accounting_service
            if not isinstance(accounting_service, AccountingService):
                raise HTTPException(
                    status_code=status.HTTP_403_FORBIDDEN,
                    detail=API_KEY_INVALID_DETAIL,
                )
            _apply_deep_research_context_auth(
                request,
                accounting_service,
                api_key,
            )
            return True, "deep-research-context"
        if _tokens_match(api_key, settings.gateway_api_key):
            request.state.api_key_role = ROLE_MASTER
            request.state.api_key_id = None
            return True, "bearer"
        record = await _lookup_virtual_key(api_keys_db, api_key)
        if record is not None:
            _apply_virtual_key_auth(request, record)
            return True, "bearer-virtual"
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail=API_KEY_INVALID_DETAIL,
        )

    if _accepts_anthropic_api_key(request.url.path):
        api_key = request.headers.get(ANTHROPIC_API_KEY_HEADER_NAME)
        if api_key:
            if _tokens_match(api_key, settings.gateway_api_key):
                request.state.api_key_role = ROLE_MASTER
                request.state.api_key_id = None
                return True, "x-api-key"
            record = await _lookup_virtual_key(api_keys_db, api_key)
            if record is not None:
                _apply_virtual_key_auth(request, record)
                return True, "x-api-key-virtual"
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail=API_KEY_INVALID_DETAIL,
            )

    parsed_session = _parse_session(request.cookies.get(SESSION_COOKIE_NAME))
    if parsed_session is not None:
        role, key_id = parsed_session
        request.state.api_key_role = role
        request.state.api_key_id = key_id
        if role == ROLE_USER and key_id is not None:
            try:
                record = await asyncio.to_thread(api_keys_db.get_by_id, key_id)
            except Exception:
                logging.exception("Session key lookup failed")
                record = None
            if record is None or record.disabled:
                if record is not None and record.disabled:
                    raise HTTPException(
                        status_code=status.HTTP_403_FORBIDDEN,
                        detail=API_KEY_DISABLED_DETAIL,
                    )
                return False, None
            request.state.api_key_record = record
        return True, "session"

    return False, None


async def _try_authenticate_request(
    request: Request,
    services: "AppServices",
) -> tuple[bool, str | None]:
    try:
        return await _authenticate_request(request, services)
    except HTTPException:
        return False, None


def _unauthorized_api_response() -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_401_UNAUTHORIZED,
        content={"detail": "Missing or invalid Authorization header (Bearer token expected)"},
    )


def _api_key_disabled_response(request: Request) -> Response:
    if _is_html_navigation(request):
        return Response(
            content=f"<!doctype html><title>{API_KEY_DISABLED_HTML}</title><h1>{API_KEY_DISABLED_HTML}</h1>",
            status_code=status.HTTP_403_FORBIDDEN,
            media_type="text/html",
        )
    return JSONResponse(
        status_code=status.HTTP_403_FORBIDDEN,
        content={"detail": API_KEY_DISABLED_DETAIL},
    )


def _category_from_exc(exc: HTTPException) -> str:
    """Map an ``HTTPException`` raised inside the auth middleware body to an
    audit category.

    Scope: this only classifies rejections originating in ``api_key_auth``
    itself. Accounting admission failures use the credential-safe accounting
    HTTP envelope instead.
    """
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return "auth_invalid"
    if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_DISABLED_DETAIL:
        return "key_disabled"
    if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_INVALID_DETAIL:
        return "auth_invalid"
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return "unauthorized"
    return "unauthorized"


# Only these two admission codes were ever audited pre-refactor (budget and
# rate-limit exhaustion). Transient accounting states (ACCOUNTING_IN_FLIGHT,
# BUDGET_RESET_IN_PROGRESS, ...) are availability issues, not governance
# rejections attributable to the caller -- keep them out of the audit log.
_AUDITED_ADMISSION_REJECTION_CATEGORIES: dict[AccountingErrorCode, str] = {
    AccountingErrorCode.BUDGET_EXHAUSTED: "budget_exhausted",
    AccountingErrorCode.RATE_LIMITED: "rate_limited",
}


def _ip_blocked_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many failed authentication attempts. Try again later."},
        headers={"Retry-After": str(retry_after)},
    )


def _note_auth_failure(request: Request, guard: IpBlockGuard | None) -> None:
    """Count a failed authentication for the client IP and, when it crosses the
    threshold, audit the resulting block once (subsequent blocked requests are
    rejected silently to avoid flooding the rejection log)."""
    if guard is None:
        return
    ip = get_client_ip(request)
    if not ip:
        return
    blocked_seconds = guard.register_failure(ip)
    if blocked_seconds is not None:
        logging.warning(
            "IP %s blocked for %ss after %d consecutive failed auth attempts",
            ip,
            blocked_seconds,
            guard.max_failures,
        )
        record_rejection(
            request,
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            reason=(
                f"IP blocked for {blocked_seconds}s after "
                f"{guard.max_failures} consecutive failed auth attempts"
            ),
            category="ip_blocked",
        )


def _note_auth_success(request: Request, guard: IpBlockGuard | None) -> None:
    if guard is None:
        return
    ip = get_client_ip(request)
    if ip:
        guard.register_success(ip)


async def _prepare_api_key_auth(
    request: Request,
    *,
    path: str,
    services: "AppServices",
) -> tuple[Response | None, str | None]:
    """Authenticate one request without crossing the downstream ASGI boundary."""
    guard = services.ip_block_guard
    request.state.gateway_authenticated = False
    request.state.gateway_auth_source = None
    # Default to no role rather than ROLE_MASTER: every `role == ROLE_MASTER`
    # check downstream must observe an explicit grant, not this placeholder.
    # Authentication always overwrites this before any endpoint runs.
    request.state.api_key_role = None
    request.state.api_key_id = None
    request.state.api_key_record = None
    request.state.llmgateway_active_request_id = None
    request.state.llmgateway_deep_research_context_token = None
    request.state.llmgateway_rate_limit_admission_key_id = None

    try:
        if not requires_api_key(path):
            is_authenticated, auth_source = await _try_authenticate_request(
                request,
                services,
            )
            request.state.gateway_authenticated = is_authenticated
            request.state.gateway_auth_source = auth_source
            return None, None

        if guard is not None:
            client_ip = get_client_ip(request)
            if client_ip:
                retry_after = guard.check_blocked(client_ip)
                if retry_after is not None:
                    return _ip_blocked_response(retry_after), None

        is_authenticated, auth_source = await _authenticate_request(
            request,
            services,
        )
        request.state.gateway_authenticated = is_authenticated
        request.state.gateway_auth_source = auth_source

        if is_authenticated:
            _note_auth_success(request, guard)
            if (
                _is_master_only_path(path)
                and getattr(request.state, "api_key_role", ROLE_MASTER) != ROLE_MASTER
            ):
                if _is_html_navigation(request):
                    return (
                        RedirectResponse(
                            url=DEFAULT_UI_PATH,
                            status_code=status.HTTP_303_SEE_OTHER,
                        ),
                        None,
                    )
                record_rejection(
                    request,
                    status_code=403,
                    reason="This endpoint is reserved for the master API key",
                    category="master_only",
                )
                return (
                    JSONResponse(
                        status_code=status.HTTP_403_FORBIDDEN,
                        content={"detail": "This endpoint is reserved for the master API key"},
                    ),
                    None,
                )
            active_request_id = await _admit_accounting_if_needed(
                request,
                services,
            )
            return None, active_request_id

        if _is_html_navigation(request):
            response = RedirectResponse(
                url=build_login_redirect_path(request),
                status_code=status.HTTP_303_SEE_OTHER,
            )
            clear_authenticated_session_cookie(response)
            return response, None

        record_rejection(
            request,
            status_code=401,
            reason="Missing or invalid Authorization header",
            category="auth_invalid",
        )
        _note_auth_failure(request, guard)
        return _unauthorized_api_response(), None
    except HTTPException as exc:
        _finish_active_request(
            services.active_requests_registry,
            getattr(request.state, "llmgateway_active_request_id", None),
        )
        logging.warning(f"Error in authentication. {exc.detail} (Status: {exc.status_code})")
        category = _category_from_exc(exc)
        record_rejection(
            request,
            status_code=exc.status_code,
            reason=str(exc.detail),
            category=category,
        )
        if category == "auth_invalid":
            _note_auth_failure(request, guard)
        if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_DISABLED_DETAIL:
            return _api_key_disabled_response(request), None
        return (
            JSONResponse(status_code=exc.status_code, content={"detail": exc.detail}),
            None,
        )
    except AccountingError as exc:
        _finish_active_request(
            services.active_requests_registry,
            getattr(request.state, "llmgateway_active_request_id", None),
        )
        category = _AUDITED_ADMISSION_REJECTION_CATEGORIES.get(exc.code)
        if category is not None:
            failure = map_accounting_error(exc, use=AccountingHttpUse.ADMISSION)
            record_rejection(
                request,
                status_code=failure.status_code,
                reason=failure.detail,
                category=category,
            )
        return (
            accounting_error_response(
                request,
                exc,
                use=AccountingHttpUse.ADMISSION,
            ),
            None,
        )
    except Exception as exc:
        _finish_active_request(
            services.active_requests_registry,
            getattr(request.state, "llmgateway_active_request_id", None),
        )
        logging.error(f"Internal server error: {exc}", exc_info=True)
        return (
            error_response(
                request,
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Internal server error",
                error_type="internal_error",
            ),
            None,
        )


def _release_rate_limit_admission(
    request: Request,
    services: "AppServices",
) -> None:
    """Close the RateLimiter admission opened by _admit_rate_limit_only.

    Mirrors AccountingService.release()'s internal
    attribute_request_tokens(request_id, key_id, 0) call so a policy-less
    route's admission never leaks a request_events entry.
    """
    key_id = getattr(request.state, "llmgateway_rate_limit_admission_key_id", None)
    if key_id is None:
        return
    request_id = getattr(request.state, "llmgateway_request_id", None)
    try:
        services.rate_limiter.attribute_request_tokens(request_id, key_id, 0)
    except BaseException:
        logging.exception("Rate limit admission cleanup failed")


async def _release_unclaimed_accounting_context(
    request: Request,
    services: "AppServices",
) -> None:
    try:
        context = take_accounting_request_context(request.scope)
    except BaseException:
        logging.exception("Accounting context cleanup failed")
        return
    if context is None:
        return
    accounting_service = services.accounting_service
    if not isinstance(accounting_service, AccountingService):
        logging.error("Accounting context owner is unavailable during cleanup")
        return
    try:
        released = await accounting_service.release(context.reservation)
        if not isinstance(released, bool):
            logging.error("Accounting context cleanup returned an invalid result")
    except BaseException:
        logging.exception("Accounting context cleanup failed")


async def _cleanup_auth_ownership(
    request: Request,
    active_request_id: str | None,
    services: "AppServices",
) -> None:
    _release_rate_limit_admission(request, services)
    if response_observation_published(request.scope):
        return
    await _release_unclaimed_accounting_context(request, services)
    try:
        _finish_active_request(services.active_requests_registry, active_request_id)
    except BaseException:
        logging.exception("Active request cleanup failed")


def _bypasses_runtime_services(request: Request) -> bool:
    if request.url.path in RUNTIME_INDEPENDENT_PATHS:
        return True
    if _matches_registered_route(request):
        return False
    request.scope.setdefault("state", {})[UNMATCHED_ROUTE_STATE_KEY] = True
    return True


def _capture_app_services(request: Request) -> "AppServices":
    return cast("AppServices", request.app.state.services)


class ApiKeyAuthMiddleware:
    """Pure-ASGI auth boundary that preserves downstream BaseException identity."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        request = Request(scope, receive=receive)
        if _bypasses_runtime_services(request):
            await self.app(scope, receive, send)
            return

        services = _capture_app_services(request)
        path = request.url.path
        response, active_request_id = await _prepare_api_key_auth(
            request,
            path=path,
            services=services,
        )
        if response is not None:
            await response(scope, receive, send)
            return

        try:
            await self.app(scope, receive, send)
        except BaseException:
            await _cleanup_auth_ownership(
                request,
                active_request_id,
                services,
            )
            raise

        await _cleanup_auth_ownership(
            request,
            active_request_id,
            services,
        )


async def api_key_auth(request: Request, call_next):
    """Compatibility adapter for direct functional-middleware callers."""
    if _bypasses_runtime_services(request):
        return await call_next(request)

    services = _capture_app_services(request)
    path = request.url.path
    response, active_request_id = await _prepare_api_key_auth(
        request,
        path=path,
        services=services,
    )
    if response is not None:
        return response

    try:
        response = await call_next(request)
    except BaseException:
        await _cleanup_auth_ownership(
            request,
            active_request_id,
            services,
        )
        raise

    _release_rate_limit_admission(request, services)
    await _release_unclaimed_accounting_context(request, services)
    _finish_active_request(services.active_requests_registry, active_request_id)
    return response
