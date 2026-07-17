from __future__ import annotations

import asyncio
from collections.abc import Callable
from unittest.mock import patch

import httpx
import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import web_extraction, web_safe_fetch
from tests._async_compat import run_async


class _RecordingAsyncByteStream(httpx.AsyncByteStream):
    """Byte stream that records whether it was iterated/closed.

    ``_get_pinned_public_url`` always builds its own pinned ``httpx.AsyncClient``
    (never one supplied by a caller), so these tests swap only the transport
    it uses (via a patched ``_PinnedHostAsyncHTTPTransport``) rather than
    injecting a duck-typed fake client. That way the real pinned-fetch code
    (``build_request``, ``send(stream=True, follow_redirects=False)`` and
    ``_read_bounded_response``) still executes end to end.
    """

    def __init__(self, chunks: tuple[bytes, ...]) -> None:
        self._chunks = chunks
        self.iterated = False
        self.closed = False

    async def __aiter__(self):
        self.iterated = True
        for chunk in self._chunks:
            yield chunk

    async def aclose(self) -> None:
        self.closed = True


def _patch_pinned_transport(
    monkeypatch: pytest.MonkeyPatch, handler: Callable[[httpx.Request], httpx.Response]
) -> None:
    def _factory(*, pinned_host: str, pinned_port: int, connect_ip: str) -> httpx.MockTransport:
        return httpx.MockTransport(handler)

    monkeypatch.setattr(web_safe_fetch, "_PinnedHostAsyncHTTPTransport", _factory)


@pytest.mark.parametrize(
    "url",
    (
        "https://example.com:not-a-port/article",
        "https://example.com:70000/article",
    ),
)
def test_invalid_url_port_is_reported_as_http_400(url: str) -> None:
    with pytest.raises(HTTPException) as raised:
        web_safe_fetch._validate_http_url(url)

    assert raised.value.status_code == 400


def test_dns_resolution_uses_running_loop_without_blocking_other_tasks() -> None:
    async def scenario() -> None:
        started = asyncio.Event()
        release = asyncio.Event()
        progressed = False

        class _Loop:
            async def getaddrinfo(self, hostname, port, *, type):  # noqa: A002
                assert (hostname, port, type) == ("example.com", 443, web_safe_fetch.socket.SOCK_STREAM)
                started.set()
                await release.wait()
                return [(None, None, None, None, ("93.184.216.34", port))]

        async def make_progress() -> None:
            nonlocal progressed
            await started.wait()
            progressed = True
            release.set()

        with patch.object(web_safe_fetch.asyncio, "get_running_loop", return_value=_Loop()):
            addresses, _ = await asyncio.gather(
                web_safe_fetch._resolve_fetch_host("example.com", 443),
                make_progress(),
            )

        assert progressed is True
        assert addresses == ("93.184.216.34",)

    run_async(scenario())


@pytest.mark.parametrize(
    ("content_type", "content_length", "chunks", "must_iterate"),
    (
        ("text/html", 6, (b"unused",), False),
        ("application/pdf", None, (b"123", b"456"), True),
    ),
)
def test_direct_html_and_pdf_fetches_enforce_declared_and_actual_byte_limits(
    monkeypatch: pytest.MonkeyPatch,
    content_type: str,
    content_length: int | None,
    chunks: tuple[bytes, ...],
    must_iterate: bool,
) -> None:
    async def public_host(_hostname: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    headers = {"Content-Type": content_type}
    if content_length is not None:
        headers["Content-Length"] = str(content_length)
    stream = _RecordingAsyncByteStream(chunks)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, headers=headers, stream=stream, request=request)

    monkeypatch.setattr(web_safe_fetch, "WEB_FETCH_MAX_RESPONSE_BYTES", 5)
    monkeypatch.setattr(web_safe_fetch, "_validate_public_fetch_host", public_host)
    _patch_pinned_transport(monkeypatch, handler)

    with pytest.raises(HTTPException) as raised:
        run_async(web_extraction._direct_http_fetch("https://example.com/document"))

    assert raised.value.status_code == 413
    assert stream.iterated is must_iterate
    assert stream.closed is True


def test_redirects_are_async_revalidated_and_every_response_is_closed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        resolved_hosts: list[tuple[str, int]] = []

        async def resolve(hostname: str, port: int) -> tuple[str, ...]:
            resolved_hosts.append((hostname, port))
            return ("93.184.216.34",)

        redirect_stream = _RecordingAsyncByteStream((b"redirect",))
        final_stream = _RecordingAsyncByteStream((b"<html>ok</html>",))

        def handler(request: httpx.Request) -> httpx.Response:
            if str(request.url) == "https://example.com/start":
                return httpx.Response(
                    302,
                    headers={"Location": "https://cdn.example.com/article"},
                    stream=redirect_stream,
                    request=request,
                )
            assert str(request.url) == "https://cdn.example.com/article"
            return httpx.Response(200, stream=final_stream, request=request)

        monkeypatch.setattr(web_safe_fetch, "_resolve_fetch_host", resolve)
        _patch_pinned_transport(monkeypatch, handler)

        response, final_url = await web_safe_fetch._get_with_public_redirects(
            "https://example.com/start",
        )

        assert final_url == "https://cdn.example.com/article"
        assert response.text == "<html>ok</html>"
        assert resolved_hosts == [("example.com", 443), ("cdn.example.com", 443)]
        assert redirect_stream.closed is True
        assert final_stream.closed is True

    run_async(scenario())
