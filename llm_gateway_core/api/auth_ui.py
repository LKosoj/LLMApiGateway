import asyncio
import html
import logging

from fastapi import APIRouter, HTTPException, Request, status
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse

from ..config.paths import STATIC_DIR
from ..config.settings import settings
from ..utils.html_cache import get_template
from ..middleware.auth import (
    DEFAULT_UI_PATH,
    ROLE_MASTER,
    ROLE_USER,
    SESSION_COOKIE_NAME,
    SESSION_HMAC_CONFIGURATION_ERROR,
    _tokens_match,
    build_login_redirect_path,
    clear_authenticated_session_cookie,
    create_authenticated_session,
    invalidate_authenticated_session,
    is_session_hmac_configured,
    is_request_authenticated,
    normalize_next_path,
    set_authenticated_session_cookie,
)

auth_router = APIRouter()

LOGIN_TEMPLATE_PATH = STATIC_DIR / "login.html"


async def _render_login_page(next_path: str, error_message: str | None = None) -> HTMLResponse:
    if not LOGIN_TEMPLATE_PATH.exists():
        logging.error(f"Login HTML file not found at {LOGIN_TEMPLATE_PATH}")
        raise HTTPException(status_code=404, detail="Login page not found.")

    try:
        template = await get_template(LOGIN_TEMPLATE_PATH)
    except HTTPException:
        raise
    except Exception as exc:
        logging.error(f"Error reading login HTML file: {exc}", exc_info=True)
        raise HTTPException(status_code=500, detail="Could not load login page.")

    safe_next = html.escape(next_path, quote=True)
    safe_error = html.escape(error_message or "", quote=False)
    html_content = (
        template.replace("__NEXT__", safe_next)
        .replace("__ERROR_DISPLAY__", "block" if error_message else "none")
        .replace("__ERROR_MESSAGE__", safe_error)
    )
    return HTMLResponse(content=html_content)


@auth_router.get("/", include_in_schema=False)
async def root_redirect(request: Request):
    if is_request_authenticated(request):
        return RedirectResponse(url=DEFAULT_UI_PATH, status_code=status.HTTP_303_SEE_OTHER)

    return RedirectResponse(
        url=build_login_redirect_path(request, DEFAULT_UI_PATH),
        status_code=status.HTTP_303_SEE_OTHER,
    )


@auth_router.get("/auth/login", response_class=HTMLResponse, include_in_schema=False)
async def login_page(request: Request, next: str | None = None):
    next_path = normalize_next_path(next)
    if is_request_authenticated(request):
        return RedirectResponse(url=next_path, status_code=status.HTTP_303_SEE_OTHER)

    return await _render_login_page(next_path)


@auth_router.post("/auth/login", include_in_schema=False)
async def login_submit(request: Request):
    next_path = DEFAULT_UI_PATH
    try:
        payload = await request.json()
    except Exception:
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Request body must be a JSON object"},
        )

    if not isinstance(payload, dict):
        return JSONResponse(
            status_code=status.HTTP_400_BAD_REQUEST,
            content={"detail": "Request body must be a JSON object"},
        )

    api_key = payload.get("api_key", "")
    next_path = normalize_next_path(payload.get("next"))

    if not is_session_hmac_configured():
        return JSONResponse(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            content={"detail": SESSION_HMAC_CONFIGURATION_ERROR},
        )

    if not isinstance(api_key, str) or not api_key:
        response = JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Invalid API Key"},
        )
        clear_authenticated_session_cookie(response)
        return response

    role = ROLE_MASTER
    key_id: int | None = None
    if _tokens_match(api_key, settings.gateway_api_key):
        role = ROLE_MASTER
    else:
        api_keys_db = getattr(request.app.state, "api_keys_db", None)
        record = (
            await asyncio.to_thread(api_keys_db.get_by_key, api_key)
            if api_keys_db is not None
            else None
        )
        if record is None or record.disabled:
            response = JSONResponse(
                status_code=status.HTTP_401_UNAUTHORIZED,
                content={"detail": "Invalid API Key"},
            )
            clear_authenticated_session_cookie(response)
            return response
        role = ROLE_USER
        key_id = record.id

    session_id = create_authenticated_session(role=role, key_id=key_id)
    response = JSONResponse(content={"redirect_to": next_path, "role": role})
    set_authenticated_session_cookie(response, session_id)
    return response


@auth_router.post("/auth/logout", include_in_schema=False)
async def logout(request: Request):
    invalidate_authenticated_session(request.cookies.get(SESSION_COOKIE_NAME))
    response = JSONResponse(content={"redirect_to": build_login_redirect_path(request, DEFAULT_UI_PATH)})
    clear_authenticated_session_cookie(response)
    return response


@auth_router.get("/auth/me", include_in_schema=False)
async def auth_me(request: Request):
    if not is_request_authenticated(request):
        return JSONResponse(
            status_code=status.HTTP_401_UNAUTHORIZED,
            content={"detail": "Not authenticated"},
        )
    role = getattr(request.state, "api_key_role", ROLE_MASTER)
    key_id = getattr(request.state, "api_key_id", None)
    record = getattr(request.state, "api_key_record", None)
    payload = {
        "role": role,
        "key_id": key_id,
        "name": record.name if record is not None else None,
    }
    return JSONResponse(content=payload)
