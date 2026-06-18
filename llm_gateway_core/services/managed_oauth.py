from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import secrets
import time
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlencode

import httpx

from ..config.loader import ConfigError, ProviderAuthConfig
from ..db.oauth_tokens_db import (
    OAuthCredentialRecord,
    OAuthCredentialStatus,
    OAuthTokensDB,
    STATUS_ACTIVE,
)

DEVICE_CODE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
SAFE_OAUTH_ERROR_CODES = frozenset(
    {
        "access_denied",
        "authorization_pending",
        "expired_token",
        "invalid_client",
        "invalid_grant",
        "invalid_request",
        "invalid_scope",
        "slow_down",
        "temporarily_unavailable",
        "unsupported_grant_type",
    }
)
logger = logging.getLogger(__name__)


class ManagedOAuthError(RuntimeError):
    def __init__(self, detail: str, *, status_code: int = 503) -> None:
        super().__init__(detail)
        self.detail = detail
        self.status_code = status_code


class OAuthLoginRequired(ManagedOAuthError):
    def __init__(self, credential_id: str) -> None:
        super().__init__(
            f"OAuth credential '{credential_id}' is not logged in.",
            status_code=503,
        )


class OAuthRefreshFailed(ManagedOAuthError):
    pass


@dataclass(frozen=True)
class OAuthAccessToken:
    credential_id: str
    access_token: str
    expires_at: int | None
    status: str


@dataclass(frozen=True)
class OAuthProviderStatus:
    provider_name: str
    auth_type: str
    credential_id: str
    login_methods: list[str]
    token_status: OAuthCredentialStatus | None


@dataclass(frozen=True)
class OAuthLoginSession:
    state: str
    provider_name: str
    credential_id: str
    auth_type: str
    client_id: str
    client_secret: str | None
    token_endpoint: str
    scopes: list[str]
    flow_type: str
    expires_at: int
    redirect_uri: str | None = None
    code_verifier: str | None = None
    device_code: str | None = None
    interval_seconds: int = 5


@dataclass(frozen=True)
class AuthorizationLoginStart:
    state: str
    authorization_url: str
    expires_at: int


@dataclass(frozen=True)
class DeviceLoginStart:
    state: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str | None
    expires_at: int
    interval_seconds: int


@dataclass(frozen=True)
class DevicePollResult:
    status: str
    credential: OAuthCredentialStatus | None = None
    interval_seconds: int | None = None
    detail: str | None = None


@dataclass(frozen=True)
class OAuthClientRuntimeConfig:
    client_id: str
    client_secret: str | None
    token_endpoint: str
    authorization_endpoint: str | None
    device_authorization_endpoint: str | None
    redirect_uri: str | None
    scopes: list[str] = field(default_factory=list)


class ManagedOAuthService:
    def __init__(
        self,
        *,
        tokens_db: OAuthTokensDB,
        http_client: httpx.AsyncClient,
        refresh_skew_seconds: int = 300,
        time_func: Any = time.time,
    ) -> None:
        self._tokens_db = tokens_db
        self._http_client = http_client
        self._refresh_skew_seconds = refresh_skew_seconds
        self._time_func = time_func
        self._refresh_locks: dict[str, asyncio.Lock] = {}
        self._login_sessions: dict[str, OAuthLoginSession] = {}

    async def get_access_token(
        self,
        *,
        provider_name: str,
        auth_config: ProviderAuthConfig,
        force_refresh: bool = False,
    ) -> OAuthAccessToken:
        credential_id = _managed_credential_id(auth_config)
        record = await asyncio.to_thread(self._tokens_db.get, credential_id)
        if record is None:
            raise OAuthLoginRequired(credential_id)
        if record.status != STATUS_ACTIVE:
            raise OAuthRefreshFailed(
                f"OAuth credential '{credential_id}' is not active: {record.reauth_reason or record.status}",
                status_code=503,
            )
        if record.provider_name != provider_name or record.auth_type != auth_config.type:
            raise OAuthRefreshFailed(
                f"OAuth credential '{credential_id}' is bound to another provider or auth type.",
                status_code=503,
            )

        now = int(self._time_func())
        if not force_refresh and not record.is_expired_or_near_expiry(now, self._refresh_skew_seconds):
            return OAuthAccessToken(
                credential_id=credential_id,
                access_token=record.access_token,
                expires_at=record.expires_at,
                status=record.status,
            )

        lock = self._refresh_locks.setdefault(credential_id, asyncio.Lock())
        async with lock:
            fresh_record = await asyncio.to_thread(self._tokens_db.get, credential_id)
            if fresh_record is None:
                raise OAuthLoginRequired(credential_id)
            if (
                not force_refresh
                and not fresh_record.is_expired_or_near_expiry(int(self._time_func()), self._refresh_skew_seconds)
            ):
                return OAuthAccessToken(
                    credential_id=credential_id,
                    access_token=fresh_record.access_token,
                    expires_at=fresh_record.expires_at,
                    status=fresh_record.status,
                )
            refreshed = await self._refresh(provider_name, auth_config, fresh_record)
            return OAuthAccessToken(
                credential_id=credential_id,
                access_token=refreshed.access_token,
                expires_at=refreshed.expires_at,
                status=refreshed.status,
            )

    async def start_authorization_login(
        self,
        *,
        provider_name: str,
        auth_config: ProviderAuthConfig,
    ) -> AuthorizationLoginStart:
        runtime = _runtime_config(auth_config)
        if not runtime.authorization_endpoint:
            raise ManagedOAuthError("OAuth authorization endpoint is not configured for this provider.", status_code=400)
        if not runtime.redirect_uri:
            raise ManagedOAuthError("OAuth redirect_uri is required for authorization-code login.", status_code=400)

        state = secrets.token_urlsafe(32)
        code_verifier = _new_code_verifier()
        code_challenge = _code_challenge_s256(code_verifier)
        expires_at = int(self._time_func()) + 600
        self._login_sessions[state] = OAuthLoginSession(
            state=state,
            provider_name=provider_name,
            credential_id=_managed_credential_id(auth_config),
            auth_type=auth_config.type,
            client_id=runtime.client_id,
            client_secret=runtime.client_secret,
            token_endpoint=runtime.token_endpoint,
            scopes=runtime.scopes,
            flow_type="authorization_code",
            expires_at=expires_at,
            redirect_uri=runtime.redirect_uri,
            code_verifier=code_verifier,
        )
        query = {
            "response_type": "code",
            "client_id": runtime.client_id,
            "redirect_uri": runtime.redirect_uri,
            "scope": " ".join(runtime.scopes),
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
        }
        return AuthorizationLoginStart(
            state=state,
            authorization_url=f"{runtime.authorization_endpoint}?{urlencode(query)}",
            expires_at=expires_at,
        )

    async def finish_authorization_login(
        self,
        *,
        provider_name: str,
        state: str,
        code: str,
    ) -> OAuthCredentialStatus:
        session = self._pop_session(state)
        if session.provider_name != provider_name or session.flow_type != "authorization_code":
            raise ManagedOAuthError("OAuth callback state does not match this provider.", status_code=400)
        form = {
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": session.redirect_uri,
            "client_id": session.client_id,
            "code_verifier": session.code_verifier,
        }
        _add_client_secret(form, session.client_secret)
        payload = await self._post_token_endpoint(session.token_endpoint, form)
        return await self._store_token_payload(session, payload)

    async def start_device_login(
        self,
        *,
        provider_name: str,
        auth_config: ProviderAuthConfig,
    ) -> DeviceLoginStart:
        runtime = _runtime_config(auth_config)
        if not runtime.device_authorization_endpoint:
            raise ManagedOAuthError("OAuth device authorization endpoint is not configured.", status_code=400)
        state = secrets.token_urlsafe(32)
        form = {
            "client_id": runtime.client_id,
            "scope": " ".join(runtime.scopes),
        }
        _add_client_secret(form, runtime.client_secret)
        payload = await self._post_form(runtime.device_authorization_endpoint, form)
        device_code = _required_str(payload, "device_code")
        user_code = _required_str(payload, "user_code")
        verification_uri = _required_str(payload, "verification_uri")
        expires_in = _optional_int(payload.get("expires_in")) or 600
        interval_seconds = _optional_int(payload.get("interval")) or 5
        expires_at = int(self._time_func()) + expires_in
        self._login_sessions[state] = OAuthLoginSession(
            state=state,
            provider_name=provider_name,
            credential_id=_managed_credential_id(auth_config),
            auth_type=auth_config.type,
            client_id=runtime.client_id,
            client_secret=runtime.client_secret,
            token_endpoint=runtime.token_endpoint,
            scopes=runtime.scopes,
            flow_type="device_code",
            expires_at=expires_at,
            device_code=device_code,
            interval_seconds=interval_seconds,
        )
        return DeviceLoginStart(
            state=state,
            user_code=user_code,
            verification_uri=verification_uri,
            verification_uri_complete=_optional_str(payload.get("verification_uri_complete")),
            expires_at=expires_at,
            interval_seconds=interval_seconds,
        )

    async def poll_device_login(
        self,
        *,
        provider_name: str,
        state: str,
    ) -> DevicePollResult:
        session = self._get_session(state)
        if session.provider_name != provider_name or session.flow_type != "device_code":
            raise ManagedOAuthError("OAuth device state does not match this provider.", status_code=400)
        form = {
            "grant_type": DEVICE_CODE_GRANT_TYPE,
            "device_code": session.device_code,
            "client_id": session.client_id,
        }
        _add_client_secret(form, session.client_secret)
        try:
            payload = await self._post_token_endpoint(session.token_endpoint, form)
        except ManagedOAuthError as exc:
            error = _oauth_error_code(exc.detail)
            if error == "authorization_pending":
                return DevicePollResult(status="pending", interval_seconds=session.interval_seconds)
            if error == "slow_down":
                slowed_interval = session.interval_seconds + 5
                self._login_sessions[state] = OAuthLoginSession(
                    **{**session.__dict__, "interval_seconds": slowed_interval}
                )
                return DevicePollResult(status="pending", interval_seconds=slowed_interval)
            if error in {"expired_token", "access_denied"}:
                self._login_sessions.pop(state, None)
                return DevicePollResult(status=error, detail=exc.detail)
            raise
        self._login_sessions.pop(state, None)
        credential = await self._store_token_payload(session, payload)
        return DevicePollResult(status="complete", credential=credential)

    async def force_refresh(self, *, provider_name: str, auth_config: ProviderAuthConfig) -> OAuthAccessToken:
        return await self.get_access_token(
            provider_name=provider_name,
            auth_config=auth_config,
            force_refresh=True,
        )

    async def delete_credential(self, credential_id: str) -> bool:
        return await asyncio.to_thread(self._tokens_db.delete, credential_id)

    async def list_provider_statuses(self, providers_config: dict[str, Any]) -> list[OAuthProviderStatus]:
        stored = {
            status.credential_id: status
            for status in await asyncio.to_thread(self._tokens_db.list_statuses)
        }
        result: list[OAuthProviderStatus] = []
        for provider_name, provider_config in providers_config.items():
            auth_config = getattr(provider_config, "auth", None)
            if not _is_managed_auth(auth_config):
                continue
            runtime = _runtime_config(auth_config)
            login_methods: list[str] = []
            if runtime.authorization_endpoint and runtime.redirect_uri:
                login_methods.append("authorization_code")
            if runtime.device_authorization_endpoint:
                login_methods.append("device_code")
            credential_id = _managed_credential_id(auth_config)
            result.append(
                OAuthProviderStatus(
                    provider_name=provider_name,
                    auth_type=auth_config.type,
                    credential_id=credential_id,
                    login_methods=login_methods,
                    token_status=stored.get(credential_id),
                )
            )
        return result

    async def import_tokens(
        self,
        *,
        provider_name: str,
        auth_config: ProviderAuthConfig,
        access_token: str,
        refresh_token: str | None,
        expires_at: int | None,
        scopes: list[str] | None = None,
        account_label: str | None = None,
    ) -> OAuthCredentialStatus:
        credential_id = _managed_credential_id(auth_config)
        return await asyncio.to_thread(
            self._tokens_db.upsert_tokens,
            credential_id=credential_id,
            provider_name=provider_name,
            auth_type=auth_config.type,
            access_token=access_token,
            refresh_token=refresh_token,
            expires_at=expires_at,
            scopes=scopes or _runtime_config(auth_config).scopes,
            account_label=account_label,
            metadata={"source": "manual_import"},
            preserve_existing_refresh_token=False,
        )

    async def _refresh(
        self,
        provider_name: str,
        auth_config: ProviderAuthConfig,
        record: OAuthCredentialRecord,
    ) -> OAuthCredentialRecord:
        if not record.refresh_token:
            await asyncio.to_thread(
                self._tokens_db.mark_reauth_required,
                record.credential_id,
                "missing_refresh_token",
            )
            raise OAuthRefreshFailed(
                f"OAuth credential '{record.credential_id}' has no refresh token.",
                status_code=503,
            )
        runtime = _runtime_config(auth_config)
        form = {
            "grant_type": "refresh_token",
            "refresh_token": record.refresh_token,
            "client_id": runtime.client_id,
        }
        _add_client_secret(form, runtime.client_secret)
        try:
            payload = await self._post_token_endpoint(runtime.token_endpoint, form)
        except ManagedOAuthError as exc:
            if "invalid_grant" in exc.detail:
                await asyncio.to_thread(
                    self._tokens_db.mark_reauth_required,
                    record.credential_id,
                    "invalid_grant",
                )
            raise OAuthRefreshFailed(exc.detail, status_code=exc.status_code) from exc
        status = await self._store_token_payload(
            OAuthLoginSession(
                state="refresh",
                provider_name=provider_name,
                credential_id=record.credential_id,
                auth_type=auth_config.type,
                client_id=runtime.client_id,
                client_secret=runtime.client_secret,
                token_endpoint=runtime.token_endpoint,
                scopes=runtime.scopes or record.scopes,
                flow_type="refresh_token",
                expires_at=int(self._time_func()) + 60,
            ),
            payload,
        )
        refreshed = await asyncio.to_thread(self._tokens_db.get, status.credential_id)
        if refreshed is None:
            raise OAuthRefreshFailed("OAuth refresh did not persist credentials.", status_code=503)
        return refreshed

    async def _store_token_payload(
        self,
        session: OAuthLoginSession,
        payload: dict[str, Any],
    ) -> OAuthCredentialStatus:
        access_token = _required_str(payload, "access_token")
        expires_in = _optional_int(payload.get("expires_in"))
        expires_at = int(self._time_func()) + expires_in if expires_in is not None else None
        refresh_token = _optional_str(payload.get("refresh_token"))
        id_token = _optional_str(payload.get("id_token"))
        scopes = _scope_list(payload.get("scope"), default=session.scopes)
        account_label = _account_label_from_id_token(id_token)
        return await asyncio.to_thread(
            self._tokens_db.upsert_tokens,
            credential_id=session.credential_id,
            provider_name=session.provider_name,
            auth_type=session.auth_type,
            access_token=access_token,
            refresh_token=refresh_token,
            id_token=id_token,
            expires_at=expires_at,
            scopes=scopes,
            account_label=account_label,
            metadata={"token_type": payload.get("token_type")},
        )

    def _get_session(self, state: str) -> OAuthLoginSession:
        session = self._login_sessions.get(state)
        if session is None:
            raise ManagedOAuthError("Unknown OAuth login state.", status_code=404)
        if session.expires_at <= int(self._time_func()):
            self._login_sessions.pop(state, None)
            raise ManagedOAuthError("OAuth login state has expired.", status_code=410)
        return session

    def _pop_session(self, state: str) -> OAuthLoginSession:
        session = self._get_session(state)
        self._login_sessions.pop(state, None)
        return session

    async def _post_token_endpoint(self, endpoint: str, form: dict[str, Any]) -> dict[str, Any]:
        return await self._post_form(endpoint, form)

    async def _post_form(self, endpoint: str, form: dict[str, Any]) -> dict[str, Any]:
        clean_form = {key: value for key, value in form.items() if value is not None}
        try:
            response = await self._http_client.post(
                endpoint,
                data=clean_form,
                headers={"Accept": "application/json"},
            )
        except httpx.RequestError as exc:
            raise ManagedOAuthError(f"OAuth endpoint request failed: {exc}", status_code=503) from exc
        try:
            payload = response.json()
        except ValueError as exc:
            logger.warning("OAuth endpoint returned a non-JSON response with status %s.", response.status_code)
            raise ManagedOAuthError(
                "OAuth endpoint returned a non-JSON response.",
                status_code=502,
            ) from exc
        if response.status_code >= 400:
            detail = _oauth_error_detail(payload)
            raise ManagedOAuthError(detail, status_code=502)
        if not isinstance(payload, dict):
            raise ManagedOAuthError("OAuth endpoint returned an invalid JSON shape.", status_code=502)
        return payload


def _managed_credential_id(auth_config: ProviderAuthConfig) -> str:
    credential_id = getattr(auth_config, "credential_id", None)
    if not credential_id:
        raise ConfigError("Managed OAuth auth must define credential_id.")
    return str(credential_id)


def _is_managed_auth(auth_config: object) -> bool:
    return bool(getattr(auth_config, "credential_id", None))


def _runtime_config(auth_config: ProviderAuthConfig) -> OAuthClientRuntimeConfig:
    client_config = getattr(auth_config, "oauth_client", None)
    if client_config is None:
        raise ConfigError("Managed OAuth auth must define oauth_client.")
    client_id = client_config.client_id
    if client_config.client_id_env:
        client_id = os.getenv(client_config.client_id_env, "").strip()
        if not client_id:
            raise ConfigError(f"env var {client_config.client_id_env} referenced but missing or empty for OAuth client_id")
    client_secret = None
    if client_config.client_secret_env:
        client_secret = os.getenv(client_config.client_secret_env, "").strip()
        if not client_secret:
            raise ConfigError(
                f"env var {client_config.client_secret_env} referenced but missing or empty for OAuth client_secret"
            )
    if not client_id:
        raise ConfigError("Managed OAuth auth must resolve a non-empty client_id.")
    return OAuthClientRuntimeConfig(
        client_id=client_id,
        client_secret=client_secret,
        token_endpoint=client_config.token_endpoint,
        authorization_endpoint=client_config.authorization_endpoint,
        device_authorization_endpoint=client_config.device_authorization_endpoint,
        redirect_uri=client_config.redirect_uri,
        scopes=list(client_config.scopes),
    )


def _new_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def _code_challenge_s256(code_verifier: str) -> str:
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _add_client_secret(form: dict[str, Any], client_secret: str | None) -> None:
    if client_secret:
        form["client_secret"] = client_secret


def _required_str(payload: dict[str, Any], key: str) -> str:
    value = payload.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ManagedOAuthError(f"OAuth endpoint response is missing '{key}'.", status_code=502)
    return value.strip()


def _optional_str(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    value = value.strip()
    return value or None


def _optional_int(value: Any) -> int | None:
    if value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _scope_list(value: Any, *, default: list[str]) -> list[str]:
    if isinstance(value, str):
        scopes = [scope for scope in value.split() if scope]
        return scopes or default
    return default


def _oauth_error_detail(payload: Any) -> str:
    if not isinstance(payload, dict):
        return "OAuth endpoint returned an error."
    error = payload.get("error")
    if isinstance(error, str) and error in SAFE_OAUTH_ERROR_CODES:
        return error
    if isinstance(error, str):
        logger.warning("OAuth endpoint returned a non-allowlisted error code.")
    return "OAuth endpoint returned an error."


def _oauth_error_code(detail: str) -> str | None:
    if not detail:
        return None
    return detail.split(":", 1)[0].strip()


def _account_label_from_id_token(id_token: str | None) -> str | None:
    if not id_token:
        return None
    parts = id_token.split(".")
    if len(parts) < 2:
        return None
    try:
        padded = parts[1] + "=" * (-len(parts[1]) % 4)
        payload = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
    except (ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    for key in ("email", "preferred_username", "sub"):
        value = payload.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    return None
