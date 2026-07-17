from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import socket
import urllib.parse
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

import httpcore
import httpx
from fastapi import HTTPException
from httpcore._backends.auto import AutoBackend

WEB_FETCH_MAX_REDIRECTS = 5
WEB_FETCH_MAX_RESPONSE_BYTES = 80 * 1024 * 1024
WEB_FETCH_BLOCKED_HOST_DETAIL = "'url' must resolve to a public http(s) address"
WEB_FETCH_OVERALL_TIMEOUT_SECONDS = 45.0
_WEB_FETCH_REDIRECT_STATUS_CODES = frozenset({301, 302, 303, 307, 308})

_DIRECT_FETCH_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
}


@dataclass(frozen=True)
class _ValidatedFetchUrl:
    url: str
    host: str
    port: int
    addresses: tuple[str, ...]
    connect_ip: str


class _PinnedHostNetworkBackend(httpcore.AsyncNetworkBackend):
    def __init__(self, *, pinned_host: str, pinned_port: int, connect_ip: str):
        self._pinned_host = pinned_host.lower()
        self._pinned_port = pinned_port
        self._connect_ip = connect_ip
        self._backend = AutoBackend()

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        if host.lower() != self._pinned_host or port != self._pinned_port:
            raise httpcore.ConnectError("Unexpected outbound host for pinned public fetch.")
        return await self._backend.connect_tcp(
            self._connect_ip,
            port,
            timeout=timeout,
            local_address=local_address,
            socket_options=socket_options,
        )

    async def connect_unix_socket(
        self,
        path: str,
        timeout: float | None = None,
        socket_options: Iterable[Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        raise httpcore.ConnectError("Unix sockets are not allowed for public fetch.")

    async def sleep(self, seconds: float) -> None:
        await self._backend.sleep(seconds)


class _PinnedHostAsyncHTTPTransport(httpx.AsyncHTTPTransport):
    def __init__(self, *, pinned_host: str, pinned_port: int, connect_ip: str):
        super().__init__(trust_env=False, http2=False)
        self._pool._network_backend = _PinnedHostNetworkBackend(
            pinned_host=pinned_host,
            pinned_port=pinned_port,
            connect_ip=connect_ip,
        )


class _PinnedForwardProxy:
    """Local CONNECT/HTTP forward proxy for CloakBrowser's real Chromium process.

    ``_abort_blocked_cloakbrowser_request`` only validates the *string* form of
    each navigated URL; the browser then resolves DNS on its own before
    connecting, which leaves a DNS-rebinding window between validation and
    connection (an attacker-controlled name server can answer differently the
    second time). Routing the whole browser session through this proxy closes
    that gap: every CONNECT/HTTP request the browser makes is revalidated
    against the SSRF blocklist and dialed using the IP resolved *at connect
    time*, never trusting a second DNS lookup performed by the browser.
    """

    def __init__(self) -> None:
        self._server: asyncio.base_events.Server | None = None
        self.port: int = 0

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_client, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]

    async def stop(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()

    async def _handle_client(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        try:
            request_line = await reader.readline()
            if not request_line:
                return
            parts = request_line.decode("latin-1").strip().split(" ")
            if len(parts) != 3:
                return
            method, target, _version = parts

            header_lines: list[bytes] = []
            while True:
                line = await reader.readline()
                if not line or line == b"\r\n":
                    break
                header_lines.append(line)

            if method == "CONNECT":
                await self._proxy_connect(target, reader, writer)
            else:
                await self._proxy_plain_http(method, target, header_lines, reader, writer)
        except (OSError, asyncio.IncompleteReadError):
            pass
        finally:
            with contextlib.suppress(OSError):
                writer.close()

    async def _dial_pinned_upstream(
        self, host: str, port: int
    ) -> tuple[asyncio.StreamReader, asyncio.StreamWriter] | None:
        try:
            addresses = await _validate_public_fetch_host(host, port)
        except HTTPException:
            return None
        try:
            return await asyncio.open_connection(addresses[0], port)
        except OSError:
            return None

    async def _proxy_connect(
        self, target: str, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        host, _, port_text = target.rpartition(":")
        try:
            port = int(port_text)
        except ValueError:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        upstream = await self._dial_pinned_upstream(host.strip("[]"), port)
        if upstream is None:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            return
        upstream_reader, upstream_writer = upstream
        writer.write(b"HTTP/1.1 200 Connection Established\r\n\r\n")
        await writer.drain()
        await self._splice(reader, writer, upstream_reader, upstream_writer)

    async def _proxy_plain_http(
        self,
        method: str,
        target: str,
        header_lines: list[bytes],
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        parsed = urlsplit(target)
        if not parsed.hostname:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()
            return
        port = parsed.port or 80
        upstream = await self._dial_pinned_upstream(parsed.hostname, port)
        if upstream is None:
            writer.write(b"HTTP/1.1 403 Forbidden\r\n\r\n")
            await writer.drain()
            return
        upstream_reader, upstream_writer = upstream
        origin_form = parsed.path or "/"
        if parsed.query:
            origin_form += f"?{parsed.query}"
        upstream_writer.write(f"{method} {origin_form} HTTP/1.1\r\n".encode("latin-1"))
        for line in header_lines:
            if line.lower().startswith((b"proxy-connection:", b"proxy-authorization:")):
                continue
            upstream_writer.write(line)
        upstream_writer.write(b"\r\n")
        await upstream_writer.drain()
        await self._splice(reader, writer, upstream_reader, upstream_writer)

    @staticmethod
    async def _splice(
        client_reader: asyncio.StreamReader,
        client_writer: asyncio.StreamWriter,
        upstream_reader: asyncio.StreamReader,
        upstream_writer: asyncio.StreamWriter,
    ) -> None:
        async def pipe(
            src: asyncio.StreamReader, dst: asyncio.StreamWriter
        ) -> None:
            try:
                while True:
                    chunk = await src.read(65536)
                    if not chunk:
                        break
                    dst.write(chunk)
                    await dst.drain()
            except (OSError, ConnectionError):
                pass

        try:
            await asyncio.gather(
                pipe(client_reader, upstream_writer),
                pipe(upstream_reader, client_writer),
            )
        finally:
            with contextlib.suppress(OSError):
                upstream_writer.close()


def _validate_http_url(raw_url: str) -> str:
    url, hostname, _port = _parse_fetch_url(raw_url)
    _validate_fetch_hostname(hostname)
    return url


def _parse_fetch_url(raw_url: str) -> tuple[str, str, int]:
    url = raw_url.strip()
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'url' must be an absolute http(s) URL") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.netloc or not hostname:
        raise HTTPException(status_code=400, detail="'url' must be an absolute http(s) URL")
    if parsed.username or parsed.password:
        raise HTTPException(status_code=400, detail="'url' must not contain credentials")
    try:
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail="'url' contains an invalid port") from exc
    return url, hostname, port


async def _validated_fetch_url(raw_url: str) -> _ValidatedFetchUrl:
    url, hostname, port = _parse_fetch_url(raw_url)
    addresses = await _validate_public_fetch_host(hostname, port)
    return _ValidatedFetchUrl(
        url=url,
        host=hostname,
        port=port,
        addresses=addresses,
        connect_ip=addresses[0],
    )


def _is_blocked_fetch_ip(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return True
    return any(
        (
            ip.is_private,
            ip.is_loopback,
            ip.is_link_local,
            ip.is_multicast,
            ip.is_reserved,
            ip.is_unspecified,
            not ip.is_global,
        )
    )


async def _resolve_fetch_host(hostname: str, port: int) -> tuple[str, ...]:
    try:
        loop = asyncio.get_running_loop()
        return tuple(
            dict.fromkeys(
                item[4][0]
                for item in await loop.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
                if item and item[4]
            )
        )
    except OSError as exc:
        raise HTTPException(status_code=400, detail=f"'url' host could not be resolved: {hostname}") from exc


def _validate_fetch_hostname(hostname: str) -> str:
    normalized_hostname = hostname.strip("[]").lower()
    if normalized_hostname == "localhost" or normalized_hostname.endswith(".localhost"):
        raise HTTPException(status_code=400, detail=WEB_FETCH_BLOCKED_HOST_DETAIL)

    try:
        ipaddress.ip_address(normalized_hostname)
    except ValueError:
        return normalized_hostname
    if _is_blocked_fetch_ip(normalized_hostname):
        raise HTTPException(status_code=400, detail=WEB_FETCH_BLOCKED_HOST_DETAIL)
    return normalized_hostname


async def _validate_public_fetch_host(hostname: str, port: int) -> tuple[str, ...]:
    normalized_hostname = _validate_fetch_hostname(hostname)
    try:
        ipaddress.ip_address(normalized_hostname)
    except ValueError:
        addresses = await _resolve_fetch_host(normalized_hostname, port)
    else:
        addresses = (normalized_hostname,)

    if not addresses or any(_is_blocked_fetch_ip(address) for address in addresses):
        raise HTTPException(status_code=400, detail=WEB_FETCH_BLOCKED_HOST_DETAIL)
    return addresses


async def _get_with_public_redirects(url: str) -> tuple[httpx.Response, str]:
    try:
        async with asyncio.timeout(WEB_FETCH_OVERALL_TIMEOUT_SECONDS):
            return await _get_with_public_redirects_unbounded(url)
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Timed out while reading URL") from exc


async def _get_with_public_redirects_unbounded(url: str) -> tuple[httpx.Response, str]:
    current_fetch_url = await _validated_fetch_url(url)
    for _ in range(WEB_FETCH_MAX_REDIRECTS + 1):
        response = await _get_pinned_public_url(current_fetch_url)
        if response.status_code not in _WEB_FETCH_REDIRECT_STATUS_CODES:
            return response, current_fetch_url.url

        location = response.headers.get("location")
        if not location:
            return response, current_fetch_url.url
        current_fetch_url = await _validated_fetch_url(urllib.parse.urljoin(str(response.url), location))

    raise HTTPException(status_code=400, detail="Too many redirects while reading URL")


async def _get_pinned_public_url(fetch_url: _ValidatedFetchUrl) -> httpx.Response:
    transport = _PinnedHostAsyncHTTPTransport(
        pinned_host=fetch_url.host,
        pinned_port=fetch_url.port,
        connect_ip=fetch_url.connect_ip,
    )
    async with httpx.AsyncClient(transport=transport) as pinned_client:
        request = pinned_client.build_request(
            "GET",
            fetch_url.url,
            headers=_DIRECT_FETCH_HEADERS,
            timeout=20.0,
        )
        response = await pinned_client.send(request, stream=True, follow_redirects=False)
        return await _read_bounded_response(response, request_url=fetch_url.url)


async def _read_bounded_response(response: Any, *, request_url: str) -> httpx.Response:
    try:
        raw_content_length = response.headers.get("Content-Length")
        if raw_content_length is not None:
            try:
                declared_length = int(raw_content_length)
            except (TypeError, ValueError):
                declared_length = None
            if declared_length is not None and declared_length > WEB_FETCH_MAX_RESPONSE_BYTES:
                raise HTTPException(status_code=413, detail="Remote response body is too large")

        body = bytearray()
        if hasattr(response, "aiter_bytes"):
            async for chunk in response.aiter_bytes():
                if len(body) + len(chunk) > WEB_FETCH_MAX_RESPONSE_BYTES:
                    raise HTTPException(status_code=413, detail="Remote response body is too large")
                body.extend(chunk)
        else:
            content = bytes(response.content)
            if len(content) > WEB_FETCH_MAX_RESPONSE_BYTES:
                raise HTTPException(status_code=413, detail="Remote response body is too large")
            body.extend(content)
    finally:
        close = getattr(response, "aclose", None)
        if close is not None:
            await close()

    headers = {
        key: value
        for key, value in response.headers.items()
        if key.lower() not in {"content-encoding", "content-length", "transfer-encoding"}
    }
    headers["Content-Length"] = str(len(body))
    request = getattr(response, "request", None) or httpx.Request("GET", request_url)
    return httpx.Response(
        status_code=response.status_code,
        headers=headers,
        content=bytes(body),
        request=request,
    )
