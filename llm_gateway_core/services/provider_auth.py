from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastapi import HTTPException, Request

from ..config.loader import (
    ProviderDetails,
    provider_auth_is_managed_oauth,
    resolve_provider_config_api_key,
    resolve_provider_config_auth_headers,
)
from .managed_oauth import ManagedOAuthError, ManagedOAuthService
from .upstream_routing_state import (
    fingerprint_api_key,
    managed_oauth_fingerprint as upstream_managed_oauth_fingerprint,
)


@dataclass(frozen=True)
class ProviderAuthMaterial:
    headers: dict[str, str]
    api_key: str | None
    upstream_key_fingerprint: str | None
    managed_credential_id: str | None = None


def provider_uses_managed_oauth(provider_config: object) -> bool:
    return provider_auth_is_managed_oauth(getattr(provider_config, "auth", None))


def managed_oauth_fingerprint(provider_name: str, provider_config: object) -> str | None:
    return upstream_managed_oauth_fingerprint(provider_name, provider_config)


async def resolve_provider_auth_material(
    request: Request,
    *,
    provider_name: str,
    provider_config: ProviderDetails,
    api_key: str | None = None,
    force_oauth_refresh: bool = False,
) -> ProviderAuthMaterial:
    auth_config = getattr(provider_config, "auth", None)
    if provider_auth_is_managed_oauth(auth_config):
        service = _get_managed_oauth_service(request)
        try:
            token = await service.get_access_token(
                provider_name=provider_name,
                auth_config=auth_config,
                force_refresh=force_oauth_refresh,
            )
        except ManagedOAuthError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
        return ProviderAuthMaterial(
            headers={"Authorization": f"Bearer {token.access_token}"},
            api_key=None,
            upstream_key_fingerprint=managed_oauth_fingerprint(provider_name, provider_config),
            managed_credential_id=token.credential_id,
        )

    resolved_api_key = api_key if api_key is not None else resolve_provider_config_api_key(provider_config)
    return ProviderAuthMaterial(
        headers=resolve_provider_config_auth_headers(provider_config, resolved_api_key),
        api_key=resolved_api_key,
        upstream_key_fingerprint=fingerprint_api_key(resolved_api_key),
    )


async def resolve_provider_auth_headers(
    request: Request,
    *,
    provider_name: str,
    provider_config: ProviderDetails,
    api_key: str | None = None,
    force_oauth_refresh: bool = False,
) -> dict[str, str]:
    material = await resolve_provider_auth_material(
        request,
        provider_name=provider_name,
        provider_config=provider_config,
        api_key=api_key,
        force_oauth_refresh=force_oauth_refresh,
    )
    return material.headers


def _get_managed_oauth_service(request: Request) -> ManagedOAuthService:
    service: Any = getattr(request.app.state, "managed_oauth_service", None)
    if not isinstance(service, ManagedOAuthService):
        raise HTTPException(status_code=500, detail="Managed OAuth service is not available.")
    return service
