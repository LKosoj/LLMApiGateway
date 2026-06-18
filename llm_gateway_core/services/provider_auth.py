from __future__ import annotations

from dataclasses import dataclass

from fastapi import Request

from ..config.loader import (
    ProviderDetails,
    resolve_provider_config_api_key,
    resolve_provider_config_auth_headers,
)
from .upstream_routing_state import (
    fingerprint_api_key,
)


@dataclass(frozen=True)
class ProviderAuthMaterial:
    headers: dict[str, str]
    api_key: str | None
    upstream_key_fingerprint: str | None


async def resolve_provider_auth_material(
    request: Request,
    *,
    provider_name: str,
    provider_config: ProviderDetails,
    api_key: str | None = None,
) -> ProviderAuthMaterial:
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
) -> dict[str, str]:
    material = await resolve_provider_auth_material(
        request,
        provider_name=provider_name,
        provider_config=provider_config,
        api_key=api_key,
    )
    return material.headers
