from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, Field, field_validator

from ...config.loader import ProviderAuthConfig, provider_auth_is_managed_oauth
from ...services.managed_oauth import ManagedOAuthError, ManagedOAuthService

router = APIRouter()


class OAuthImportRequest(BaseModel):
    access_token: str = Field(min_length=1)
    refresh_token: str | None = None
    expires_at: int | None = None
    expires_in: int | None = None
    scopes: list[str] | None = None
    account_label: str | None = None

    @field_validator("refresh_token", "account_label", mode="before")
    @classmethod
    def normalize_optional_strings(cls, value: Any) -> Any:
        if isinstance(value, str):
            value = value.strip()
            return value or None
        return value

    @field_validator("scopes", mode="before")
    @classmethod
    def normalize_scopes(cls, value: Any) -> Any:
        if value is None or isinstance(value, list):
            return value
        if isinstance(value, str):
            return [scope for scope in value.split() if scope]
        return value

    def resolved_expires_at(self) -> int | None:
        if self.expires_at is not None:
            return self.expires_at
        if self.expires_in is None:
            return None
        return int(datetime.now(timezone.utc).timestamp()) + int(self.expires_in)


@router.get("/admin/oauth/providers")
async def list_oauth_providers(request: Request) -> JSONResponse:
    service = _get_managed_oauth_service(request)
    config_loader = _get_config_loader(request)
    statuses = await service.list_provider_statuses(config_loader.providers_config)
    return JSONResponse(content={"providers": jsonable_encoder(statuses)})


@router.post("/admin/oauth/{provider_name}/login/authorization")
async def start_authorization_login(provider_name: str, request: Request) -> JSONResponse:
    service, auth_config = _managed_provider(request, provider_name)
    try:
        result = await service.start_authorization_login(
            provider_name=provider_name,
            auth_config=auth_config,
        )
    except ManagedOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(content=jsonable_encoder(result))


@router.post("/admin/oauth/{provider_name}/login/device")
async def start_device_login(provider_name: str, request: Request) -> JSONResponse:
    service, auth_config = _managed_provider(request, provider_name)
    try:
        result = await service.start_device_login(
            provider_name=provider_name,
            auth_config=auth_config,
        )
    except ManagedOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(content=jsonable_encoder(result))


@router.post("/admin/oauth/{provider_name}/login/device/{state}/poll")
async def poll_device_login(provider_name: str, state: str, request: Request) -> JSONResponse:
    service = _get_managed_oauth_service(request)
    try:
        result = await service.poll_device_login(provider_name=provider_name, state=state)
    except ManagedOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(content=jsonable_encoder(result))


@router.get("/auth/oauth/callback/{provider_name}")
async def finish_authorization_login(
    provider_name: str,
    state: str,
    request: Request,
    code: str | None = None,
    error: str | None = None,
    error_description: str | None = None,
) -> HTMLResponse:
    if error:
        detail = error_description or error
        return HTMLResponse(
            content=_callback_html("OAuth login failed", detail),
            status_code=400,
        )
    if not code:
        return HTMLResponse(
            content=_callback_html("OAuth login failed", "Missing authorization code."),
            status_code=400,
        )
    service = _get_managed_oauth_service(request)
    try:
        await service.finish_authorization_login(
            provider_name=provider_name,
            state=state,
            code=code,
        )
    except ManagedOAuthError as exc:
        return HTMLResponse(
            content=_callback_html("OAuth login failed", exc.detail),
            status_code=exc.status_code,
        )
    return HTMLResponse(content=_callback_html("OAuth login complete", "You can close this tab."))


@router.post("/admin/oauth/{provider_name}/import")
async def import_oauth_tokens(
    provider_name: str,
    payload: OAuthImportRequest,
    request: Request,
) -> JSONResponse:
    service, auth_config = _managed_provider(request, provider_name)
    try:
        status = await service.import_tokens(
            provider_name=provider_name,
            auth_config=auth_config,
            access_token=payload.access_token,
            refresh_token=payload.refresh_token,
            expires_at=payload.resolved_expires_at(),
            scopes=payload.scopes,
            account_label=payload.account_label,
        )
    except ManagedOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(content={"credential": jsonable_encoder(status)})


@router.post("/admin/oauth/{provider_name}/refresh")
async def refresh_oauth_tokens(provider_name: str, request: Request) -> JSONResponse:
    service, auth_config = _managed_provider(request, provider_name)
    try:
        token = await service.force_refresh(provider_name=provider_name, auth_config=auth_config)
    except ManagedOAuthError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    return JSONResponse(
        content={
            "credential_id": token.credential_id,
            "status": token.status,
            "expires_at": token.expires_at,
        }
    )


@router.delete("/admin/oauth/credentials/{credential_id}")
async def delete_oauth_credential(credential_id: str, request: Request) -> JSONResponse:
    service = _get_managed_oauth_service(request)
    deleted = await service.delete_credential(credential_id)
    return JSONResponse(content={"deleted": deleted})


def _managed_provider(request: Request, provider_name: str) -> tuple[ManagedOAuthService, ProviderAuthConfig]:
    config_loader = _get_config_loader(request)
    provider_config = config_loader.providers_config.get(provider_name)
    if provider_config is None:
        raise HTTPException(status_code=404, detail=f"Provider '{provider_name}' is not configured.")
    auth_config = getattr(provider_config, "auth", None)
    if not provider_auth_is_managed_oauth(auth_config):
        raise HTTPException(status_code=400, detail=f"Provider '{provider_name}' does not use managed OAuth.")
    return _get_managed_oauth_service(request), auth_config


def _get_config_loader(request: Request) -> Any:
    config_loader = getattr(request.app.state, "config_loader", None)
    if config_loader is None:
        raise HTTPException(status_code=500, detail="ConfigLoader is not available.")
    return config_loader


def _get_managed_oauth_service(request: Request) -> ManagedOAuthService:
    service = getattr(request.app.state, "managed_oauth_service", None)
    if service is None:
        raise HTTPException(status_code=500, detail="Managed OAuth service is not available.")
    return service


def _callback_html(title: str, message: str) -> str:
    escaped_title = _escape_html(title)
    escaped_message = _escape_html(message)
    return (
        "<!doctype html><html><head><meta charset=\"utf-8\">"
        f"<title>{escaped_title}</title></head><body>"
        f"<h1>{escaped_title}</h1><p>{escaped_message}</p>"
        "</body></html>"
    )


def _escape_html(value: str) -> str:
    return (
        value.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#x27;")
    )
