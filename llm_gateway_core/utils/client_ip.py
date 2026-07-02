"""Client IP resolution with trusted-proxy support."""

from __future__ import annotations

import logging
from ipaddress import ip_address, ip_network

from ..config.settings import settings

logger = logging.getLogger(__name__)


def _parse_trusted_proxy_networks(raw_value: str) -> tuple:
    networks = []
    for raw_part in raw_value.split(","):
        part = raw_part.strip()
        if not part:
            continue
        try:
            network = ip_network(part, strict=False)
        except ValueError:
            try:
                address = ip_address(part)
            except ValueError as exc:
                raise ValueError(
                    "TRUSTED_PROXIES must contain comma-separated IP addresses or CIDR ranges"
                ) from exc
            network = ip_network(f"{address}/{address.max_prefixlen}", strict=False)
        networks.append(network)
    return tuple(networks)


def _is_trusted_peer(peer_host: str, trusted_proxies: str) -> bool:
    if not trusted_proxies:
        return False

    try:
        peer_ip = ip_address(peer_host)
    except ValueError:
        return False

    networks = _parse_trusted_proxy_networks(trusted_proxies)
    return any(peer_ip in network for network in networks)


def _get_header(headers, name: str) -> str | None:
    if headers is None:
        return None
    return (
        headers.get(name)
        or headers.get(name.lower())
        or headers.get("-".join(part.capitalize() for part in name.split("-")))
    )


def _forwarded_for_chain(header_value: str | None) -> list[str]:
    if not header_value:
        return []

    chain: list[str] = []
    for raw_part in header_value.split(","):
        part = raw_part.strip().strip('"')
        if not part:
            continue
        try:
            chain.append(str(ip_address(part)))
        except ValueError:
            continue
    return chain


def get_client_ip(request) -> str | None:
    """Return the effective client IP for auth, blocking and audit logic.

    X-Forwarded-For is trusted only when the immediate peer is configured in
    TRUSTED_PROXIES. Spoofed headers from direct clients are ignored.
    """
    client = getattr(request, "client", None)
    peer_host = getattr(client, "host", None)
    if not peer_host:
        return None

    trusted_proxies = getattr(settings, "trusted_proxies", "")
    try:
        if not _is_trusted_peer(peer_host, trusted_proxies):
            return peer_host
    except ValueError:
        logger.exception("Invalid TRUSTED_PROXIES value; ignoring X-Forwarded-For")
        return peer_host

    forwarded_chain = _forwarded_for_chain(
        _get_header(getattr(request, "headers", None), "x-forwarded-for")
    )
    for candidate in reversed([*forwarded_chain, peer_host]):
        if not _is_trusted_peer(candidate, trusted_proxies):
            return candidate
    return forwarded_chain[0] if forwarded_chain else peer_host
