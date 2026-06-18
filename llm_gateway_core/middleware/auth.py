import asyncio
import logging
import secrets
import time
import hmac
import hashlib
from uuid import uuid4
from urllib.parse import quote

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.security import APIKeyHeader
from starlette.routing import Match

from ..config.settings import settings
from ..db.api_keys_db import ApiKeyRecord, ApiKeysDB
from ..db.rejections_db import record_rejection
from ..services.access_control import UsdBudgetLedger
from ..services.active_requests import get_active_requests_registry

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
USD_BUDGET_RESERVATION_SUFFIXES = (
    "/chat/completions",
    "/responses",
    "/messages",
    "/embeddings",
    "/rerank",
    "/images",
    "/images/generations",
    "/images/edits",
    "/audio/speech",
    "/audio/transcriptions",
    "/pdf/convert",
    "/pdf/jobs",
    "/web/search",
    "/web/read",
    "/web/research",
    "/web/deep-research",
    "/tavily/search",
    "/tavily/extract",
)
CHAT_USAGE_RESERVATION_SUFFIXES = (
    "/chat/completions",
    "/responses",
    "/messages",
)

PUBLIC_EXACT_PATHS = {"/health", "/healthz", LOGIN_PATH}
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
    "/v1/config/",
    "/v1/openrouter/free-models",
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
    secret = settings.gateway_api_key
    if not secret or not secret.strip():
        logging.error(SESSION_HMAC_CONFIGURATION_ERROR)
        return None
    return secret.encode("utf-8")


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
        samesite="lax",
        path="/",
    )


def clear_authenticated_session_cookie(response: Response) -> None:
    response.delete_cookie(key=SESSION_COOKIE_NAME, path="/")


def normalize_next_path(next_path: str | None) -> str:
    if not next_path or not next_path.startswith("/") or next_path.startswith("//"):
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


def _parse_session(session_id: str | None) -> tuple[str, int | None] | None:
    """Return ``(role, key_id)`` for a valid session cookie, else ``None``.

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
    return role, key_id


def _has_valid_session(session_id: str | None) -> bool:
    return _parse_session(session_id) is not None


async def _lookup_virtual_key(request: Request, token: str) -> ApiKeyRecord | None:
    db: ApiKeysDB | None = getattr(request.app.state, "api_keys_db", None)
    if db is None:
        return None
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


def _path_uses_usd_budget_reservation(path: str) -> bool:
    normalized_path = path.rstrip("/")
    return any(
        normalized_path.endswith(suffix)
        for suffix in USD_BUDGET_RESERVATION_SUFFIXES
    )


def _path_uses_chat_usage_reservation(path: str) -> bool:
    normalized_path = path.rstrip("/")
    return any(
        normalized_path.endswith(suffix)
        for suffix in CHAT_USAGE_RESERVATION_SUFFIXES
    )


def _get_usd_budget_ledger(request: Request) -> UsdBudgetLedger | None:
    ledger = getattr(request.app.state, "usd_budget_ledger", None)
    return ledger if isinstance(ledger, UsdBudgetLedger) else None


def _reserve_usd_budget_if_needed(request: Request, path: str) -> None:
    if not _path_uses_usd_budget_reservation(path):
        return

    record = getattr(request.state, "api_key_record", None)
    if record is None or not record.budget_enforced():
        return

    ledger = _get_usd_budget_ledger(request)
    if ledger is None:
        return

    ledger.sync_record(
        record.id,
        budget_usd=record.budget_usd,
        spent_usd=record.spent_usd,
    )
    estimate = ledger.default_estimate_usd
    if not ledger.reserve(record.id, estimate):
        reserved_usd = ledger.reserved_for(record.id)
        raise HTTPException(
            status_code=status.HTTP_429_TOO_MANY_REQUESTS,
            detail=(
                f"API key budget of ${record.budget_usd:.4f} exhausted "
                f"(spent ${record.spent_usd:.4f}, reserved ${reserved_usd:.4f})"
            ),
        )

    request.state.usd_budget_reserved = True
    request.state.usd_budget_finalized = False
    request.state.usd_budget_reserved_key_id = record.id
    request.state.usd_budget_reserved_estimate = estimate


def _release_usd_budget_reservation(request: Request) -> None:
    if not getattr(request.state, "usd_budget_reserved", False):
        return
    if getattr(request.state, "usd_budget_finalized", False):
        return

    ledger = _get_usd_budget_ledger(request)
    key_id = getattr(request.state, "usd_budget_reserved_key_id", None)
    if ledger is not None and key_id is not None:
        ledger.release(int(key_id))
    request.state.usd_budget_finalized = True


def _start_active_request_if_needed(request: Request, path: str) -> str | None:
    if not _path_uses_usd_budget_reservation(path):
        return None

    request_id = getattr(request.state, "llmgateway_active_request_id", None)
    if not isinstance(request_id, str) or not request_id:
        request_id = getattr(request.state, "llmgateway_request_id", None)
    if not isinstance(request_id, str) or not request_id:
        request_id = str(uuid4())
    request.state.llmgateway_active_request_id = request_id
    x_title = request.headers.get("x-title")
    if isinstance(x_title, str):
        x_title = x_title.strip() or None

    get_active_requests_registry(request.app).start(
        request_id=request_id,
        path=path,
        api_key_id=getattr(request.state, "api_key_id", None),
        gateway_model=getattr(request.state, "llmgateway_gateway_model", None),
        operation=getattr(request.state, "llmgateway_operation", None),
        x_title=x_title,
    )
    return request_id


def _finish_active_request(request: Request, request_id: str | None) -> None:
    if request_id:
        get_active_requests_registry(request.app).finish(request_id)


def _finish_active_request_with_response(
    request: Request,
    response: Response,
    request_id: str | None,
) -> Response:
    if not request_id:
        return response

    if isinstance(response, StreamingResponse):
        original_iterator = response.body_iterator

        async def finishing_iterator():
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                _finish_active_request(request, request_id)

        response.body_iterator = finishing_iterator()
        return response

    _finish_active_request(request, request_id)
    return response


async def _authenticate_request(request: Request) -> tuple[bool, str | None]:
    auth_header = await api_key_header(request)
    if auth_header:
        api_key = _extract_bearer_token(auth_header)
        if _tokens_match(api_key, settings.gateway_api_key):
            request.state.api_key_role = ROLE_MASTER
            request.state.api_key_id = None
            return True, "bearer"
        record = await _lookup_virtual_key(request, api_key)
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
            record = await _lookup_virtual_key(request, api_key)
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
            db: ApiKeysDB | None = getattr(request.app.state, "api_keys_db", None)
            if db is not None:
                try:
                    record = await asyncio.to_thread(db.get_by_id, key_id)
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


async def _try_authenticate_request(request: Request) -> tuple[bool, str | None]:
    try:
        return await _authenticate_request(request)
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
    itself — invalid/disabled keys during authentication and the USD-budget
    *reservation* (the 429 from ``_reserve_usd_budget_if_needed``). Rejections
    decided later in route handlers (``enforce_virtual_key_access``:
    model-not-allowed, record-level budget, RPM/TPM rate limit) record their
    own precise category via ``record_rejection`` and never reach here, so the
    429 below maps to ``budget_exhausted`` rather than ``rate_limited``.
    """
    if exc.status_code == status.HTTP_401_UNAUTHORIZED:
        return "auth_invalid"
    if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_DISABLED_DETAIL:
        return "key_disabled"
    if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_INVALID_DETAIL:
        return "auth_invalid"
    if exc.status_code == status.HTTP_403_FORBIDDEN:
        return "unauthorized"
    if exc.status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        return "budget_exhausted"
    return "unauthorized"


def _client_ip(request: Request) -> str | None:
    client = getattr(request, "client", None)
    return client.host if client is not None else None


def _ip_blocked_response(retry_after: int) -> JSONResponse:
    return JSONResponse(
        status_code=status.HTTP_429_TOO_MANY_REQUESTS,
        content={"detail": "Too many failed authentication attempts. Try again later."},
        headers={"Retry-After": str(retry_after)},
    )


def _note_auth_failure(request: Request) -> None:
    """Count a failed authentication for the client IP and, when it crosses the
    threshold, audit the resulting block once (subsequent blocked requests are
    rejected silently to avoid flooding the rejection log)."""
    guard = getattr(request.app.state, "ip_block_guard", None)
    if guard is None:
        return
    ip = _client_ip(request)
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


def _note_auth_success(request: Request) -> None:
    guard = getattr(request.app.state, "ip_block_guard", None)
    if guard is None:
        return
    ip = _client_ip(request)
    if ip:
        guard.register_success(ip)


async def api_key_auth(request: Request, call_next):
    """
    FastAPI middleware to authenticate requests using either a Bearer token or
    a server-side session referenced by an HttpOnly cookie.
    """
    if not _matches_registered_route(request):
        return await call_next(request)

    request.state.gateway_authenticated = False
    request.state.gateway_auth_source = None
    request.state.api_key_role = ROLE_MASTER
    request.state.api_key_id = None
    request.state.api_key_record = None
    request.state.usd_budget_reserved = False
    request.state.usd_budget_finalized = False
    request.state.usd_budget_reserved_key_id = None
    request.state.usd_budget_reserved_estimate = 0.0
    request.state.llmgateway_active_request_id = None

    path = request.url.path

    try:
        if not requires_api_key(path):
            is_authenticated, auth_source = await _try_authenticate_request(request)
            request.state.gateway_authenticated = is_authenticated
            request.state.gateway_auth_source = auth_source
            return await call_next(request)

        guard = getattr(request.app.state, "ip_block_guard", None)
        if guard is not None:
            client_ip = _client_ip(request)
            if client_ip:
                retry_after = guard.check_blocked(client_ip)
                if retry_after is not None:
                    return _ip_blocked_response(retry_after)

        is_authenticated, auth_source = await _authenticate_request(request)
        request.state.gateway_authenticated = is_authenticated
        request.state.gateway_auth_source = auth_source

        if is_authenticated:
            _note_auth_success(request)
            if (
                _is_master_only_path(path)
                and getattr(request.state, "api_key_role", ROLE_MASTER) != ROLE_MASTER
            ):
                if _is_html_navigation(request):
                    return RedirectResponse(
                        url=DEFAULT_UI_PATH,
                        status_code=status.HTTP_303_SEE_OTHER,
                    )
                record_rejection(
                    request,
                    status_code=403,
                    reason="This endpoint is reserved for the master API key",
                    category="master_only",
                )
                return JSONResponse(
                    status_code=status.HTTP_403_FORBIDDEN,
                    content={"detail": "This endpoint is reserved for the master API key"},
                )
            _reserve_usd_budget_if_needed(request, path)
            active_request_id = _start_active_request_if_needed(request, path)
            try:
                response = await call_next(request)
            except Exception:
                _finish_active_request(request, active_request_id)
                raise
            if not _path_uses_chat_usage_reservation(path):
                _release_usd_budget_reservation(request)
            return _finish_active_request_with_response(request, response, active_request_id)

        if _is_html_navigation(request):
            response = RedirectResponse(
                url=build_login_redirect_path(request),
                status_code=status.HTTP_303_SEE_OTHER,
            )
            clear_authenticated_session_cookie(response)
            return response

        record_rejection(
            request,
            status_code=401,
            reason="Missing or invalid Authorization header",
            category="auth_invalid",
        )
        _note_auth_failure(request)
        return _unauthorized_api_response()
    except HTTPException as exc:
        _finish_active_request(request, getattr(request.state, "llmgateway_active_request_id", None))
        _release_usd_budget_reservation(request)
        logging.warning(f"Error in authentication. {exc.detail} (Status: {exc.status_code})")
        category = _category_from_exc(exc)
        record_rejection(
            request,
            status_code=exc.status_code,
            reason=str(exc.detail),
            category=category,
        )
        if category == "auth_invalid":
            _note_auth_failure(request)
        if exc.status_code == status.HTTP_403_FORBIDDEN and exc.detail == API_KEY_DISABLED_DETAIL:
            return _api_key_disabled_response(request)
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
    except Exception as exc:
        _finish_active_request(request, getattr(request.state, "llmgateway_active_request_id", None))
        _release_usd_budget_reservation(request)
        logging.error(f"Internal server error: {exc}", exc_info=True)
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": f"Internal server error. Error: {exc}"},
        )
