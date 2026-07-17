"""Canonical construction and cleanup for gateway-owned HTTP clients."""

from __future__ import annotations

import logging
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any

import httpx

from ..config.loader import resolve_provider_proxy
from ..utils.log_redaction import safe_exception_type_name

logger = logging.getLogger(__name__)

HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS = 30.0
HTTP_CLIENT_READ_TIMEOUT_SECONDS = 500.0
HTTP_CLIENT_WRITE_TIMEOUT_SECONDS = 60.0
HTTP_CLIENT_POOL_TIMEOUT_SECONDS = 30.0
HTTP_CLIENT_MAX_CONNECTIONS = 200
HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS = 40

_SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5"})


class ProxyConfigurationError(ValueError):
    """A provider proxy reference cannot be used safely by HTTPX."""


class ProxyClientConstructionError(RuntimeError):
    """A provider proxy client could not be constructed."""


@dataclass(frozen=True, slots=True)
class HttpClientCloseFailure:
    """Credential-safe diagnostic for a regular client close failure."""

    client_name: str
    exception_type: str


def _default_timeout() -> httpx.Timeout:
    return httpx.Timeout(
        connect=HTTP_CLIENT_CONNECT_TIMEOUT_SECONDS,
        read=HTTP_CLIENT_READ_TIMEOUT_SECONDS,
        write=HTTP_CLIENT_WRITE_TIMEOUT_SECONDS,
        pool=HTTP_CLIENT_POOL_TIMEOUT_SECONDS,
    )


def _default_pool_limits() -> httpx.Limits:
    return httpx.Limits(
        max_connections=HTTP_CLIENT_MAX_CONNECTIONS,
        max_keepalive_connections=HTTP_CLIENT_MAX_KEEPALIVE_CONNECTIONS,
    )


def create_shared_http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=_default_timeout(),
        limits=_default_pool_limits(),
        http2=True,
    )


def _resolve_proxy(provider_name: str, details: object) -> httpx.Proxy | None:
    try:
        proxy_url = resolve_provider_proxy(getattr(details, "proxy", None))
        if not proxy_url:
            return None
        proxy = httpx.Proxy(proxy_url)
        if proxy.url.scheme not in _SUPPORTED_PROXY_SCHEMES or not proxy.url.host:
            raise ValueError("unsupported or incomplete proxy URL")
        return proxy
    except Exception:
        raise ProxyConfigurationError(
            f"Invalid proxy configuration for provider '{provider_name}'."
        ) from None


async def populate_proxy_http_clients(
    providers_config: Mapping[str, Any] | object,
    *,
    register_client: Callable[[str, httpx.AsyncClient], None],
) -> dict[str, httpx.AsyncClient]:
    """Construct proxy clients and transfer ownership as each client is created."""
    if not isinstance(providers_config, Mapping):
        return {}

    resolved_proxies = [
        (provider_name, proxy)
        for provider_name, details in providers_config.items()
        if (proxy := _resolve_proxy(provider_name, details)) is not None
    ]
    clients: dict[str, httpx.AsyncClient] = {}
    for provider_name, proxy in resolved_proxies:
        try:
            client = httpx.AsyncClient(
                proxy=proxy,
                timeout=_default_timeout(),
                limits=_default_pool_limits(),
                http2=True,
            )
        except BaseException as exc:
            if not isinstance(exc, Exception):
                raise
            raise ProxyClientConstructionError(
                f"Could not create proxy HTTP client for provider '{provider_name}'."
            ) from None
        register_client(provider_name, client)
        clients[provider_name] = client
        logger.info("Created proxy HTTP client for provider '%s'.", provider_name)
    return clients


async def create_proxy_http_clients(
    providers_config: Mapping[str, Any] | object,
) -> dict[str, httpx.AsyncClient]:
    """Validate every proxy first, then construct a complete client mapping."""
    clients: dict[str, httpx.AsyncClient] = {}
    try:
        return await populate_proxy_http_clients(
            providers_config,
            register_client=clients.__setitem__,
        )
    except BaseException:
        await close_http_clients(clients)
        raise


async def close_http_clients(
    clients: Mapping[str, httpx.AsyncClient],
) -> tuple[HttpClientCloseFailure, ...]:
    """Close every client even when an earlier close fails."""
    terminal_error: BaseException | None = None
    failures: list[HttpClientCloseFailure] = []
    for client_name, client in tuple(clients.items()):
        try:
            await client.aclose()
        except BaseException as exc:
            exception_type = safe_exception_type_name(exc)
            logger.error(
                "Failed to close HTTP client '%s' (%s).",
                client_name,
                exception_type,
            )
            if isinstance(exc, Exception):
                failures.append(
                    HttpClientCloseFailure(
                        client_name=client_name,
                        exception_type=exception_type,
                    )
                )
            elif terminal_error is None:
                terminal_error = exc
        else:
            logger.info("HTTP client '%s' closed.", client_name)
    if terminal_error is not None:
        raise terminal_error
    return tuple(failures)
