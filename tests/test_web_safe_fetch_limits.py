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


def test_pinned_forward_proxy_rejects_oversized_headers_with_431() -> None:
    async def scenario() -> None:
        proxy = web_safe_fetch._PinnedForwardProxy()
        await proxy.start()
        try:
            reader, writer = await asyncio.open_connection("127.0.0.1", proxy.port)
            try:
                oversized_headers = b"GET http://example.com/ HTTP/1.1\r\n" + (
                    b"X-Pad: " + b"a" * 1000 + b"\r\n"
                ) * 70 + b"\r\n"
                writer.write(oversized_headers)
                await writer.drain()
                status_line = await asyncio.wait_for(reader.readline(), timeout=2.0)
                assert status_line.startswith(b"HTTP/1.1 431 ")
            finally:
                writer.close()
                await writer.wait_closed()
        finally:
            await proxy.stop()

    run_async(scenario())


def test_cloakbrowser_fetch_propagates_http_exception_and_bounds_concurrency(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def scenario() -> None:
        monkeypatch.setattr(web_extraction.settings, "web_read_cloakbrowser_enabled", True)
        with pytest.raises(HTTPException) as exc_info:
            await web_extraction._cloakbrowser_fetch("http://127.0.0.1/secret")
        assert exc_info.value.status_code == 400

        in_flight = 0
        max_in_flight = 0

        def fake_render(url: str, _port: int) -> tuple[str, str, str]:
            nonlocal in_flight, max_in_flight
            import time

            in_flight += 1
            max_in_flight = max(max_in_flight, in_flight)
            time.sleep(0.05)
            in_flight -= 1
            return "<html><title>T</title><body><p>Body</p></body></html>", "T", url

        async def fake_validate(url: str):
            return web_safe_fetch._ValidatedFetchUrl(
                url=url, host="example.com", port=443, addresses=("93.184.216.34",), connect_ip="93.184.216.34"
            )

        monkeypatch.setattr(web_extraction, "_validated_fetch_url", fake_validate)
        monkeypatch.setattr(web_extraction, "_cloakbrowser_render_sync", fake_render)
        monkeypatch.setattr(web_extraction, "_extract_cloakbrowser_markdown", lambda html, url, title: {"url": url, "title": title, "content": "Body", "images": []})

        results = await asyncio.gather(
            *(web_extraction._cloakbrowser_fetch(f"https://example.com/{i}") for i in range(4))
        )
        assert len(results) == 4
        assert max_in_flight <= web_extraction.CLOAKBROWSER_MAX_CONCURRENT

    run_async(scenario())


def test_web_research_download_content_returns_empty_on_ssrf_400_without_fallback() -> None:
    from unittest.mock import AsyncMock

    from llm_gateway_core.agents import web_research as web_research_module

    async def scenario() -> None:
        client = web_research_module.WebResearchClient(
            tavily_api_key="tavily-key",
            jina_api_key="jina-key",
            proxy_url="http://proxy.internal",
        )
        client._tavily_extract = AsyncMock(side_effect=AssertionError("must not fall back to tavily"))
        client._get_clean_text_jina = AsyncMock(side_effect=AssertionError("must not fall back to jina"))

        with patch.object(
            web_research_module,
            "_get_with_public_redirects",
            new=AsyncMock(side_effect=HTTPException(status_code=400, detail="blocked")),
        ):
            result = await client._download_content("http://127.0.0.1/admin")

        assert result == {"url": "http://127.0.0.1/admin", "title": "", "content": ""}
        client._tavily_extract.assert_not_awaited()
        client._get_clean_text_jina.assert_not_awaited()

    run_async(scenario())
