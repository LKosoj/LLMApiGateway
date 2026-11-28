from __future__ import annotations

import asyncio
import base64
import io
import json
import socket
import ssl
import threading
import wave
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image
from starlette.requests import Request

from llm_gateway_core.api.v1 import (
    audio_adapters,
    image_dimensions,
    images,
    web,
    web_adapters,
    web_content,
    web_extraction,
    web_research_orchestration,
    web_safe_fetch,
)
from llm_gateway_core.agents import web_research as web_research_agent
from llm_gateway_core.config.loader import OperationRoute
from llm_gateway_core.services.deep_research_protocol import (
    DeepResearchCallbackOperation,
    DeepResearchCallbackRequest,
)
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState


def _make_png_bytes(width: int = 64, height: int = 64) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", (width, height), color=(10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


def _make_wav_bytes() -> bytes:
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(16000)
        wf.writeframes(b"\x00\x00" * 160)
    return buf.getvalue()


def _make_request(*, services: Any = None, runtime_snapshot: Any = None) -> Request:
    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/web/research",
        "headers": [],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "app": SimpleNamespace(state=SimpleNamespace(services=services)),
    }
    request = Request(scope, _receive)
    if runtime_snapshot is not None:
        request.state.runtime_snapshot = runtime_snapshot
    return request


# ---------------------------------------------------------------------------
# 1. web_content.py + web_extraction.py + web_safe_fetch.py
# ---------------------------------------------------------------------------


def test_extract_article_images_parses_html_once_for_dom_and_jsonld(monkeypatch: pytest.MonkeyPatch) -> None:
    import bs4

    real_bs = bs4.BeautifulSoup
    parse_calls = 0

    class _CountingBS(real_bs):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            nonlocal parse_calls
            parse_calls += 1
            super().__init__(*args, **kwargs)

    monkeypatch.setattr(bs4, "BeautifulSoup", _CountingBS)

    html = """
    <html>
      <head>
        <title>Sample Article</title>
        <script type="application/ld+json">
          {"@type": "Article", "headline": "JSONLD Hero", "image": "https://cdn.example.com/jsonld.jpg"}
        </script>
      </head>
      <body>
        <article>
          <img src="/images/body.jpg" alt="Body image" />
        </article>
      </body>
    </html>
    """
    images_found = web_content.extract_article_images_from_html(html, "https://example.com/post")

    assert parse_calls == 1
    urls = [item["url"] for item in images_found]
    assert "https://example.com/images/body.jpg" in urls
    assert "https://cdn.example.com/jsonld.jpg" in urls

    # Passing an existing soup to extract_jsonld_images must not re-parse HTML.
    pre_parsed_soup = real_bs(html, "lxml")
    parse_calls = 0
    jsonld_only = web_content.extract_jsonld_images(html, "https://example.com/post", soup=pre_parsed_soup)
    assert parse_calls == 0
    assert [item["url"] for item in jsonld_only] == ["https://cdn.example.com/jsonld.jpg"]


def test_direct_http_fetch_offloads_html_images_and_title_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    main_thread_id = threading.get_ident()
    observed_thread_ids: list[int] = []
    real_helper = web_extraction._extract_html_images_and_title

    def _recording_helper(html_text: str, base_url: str) -> tuple[list[dict[str, str]], str]:
        observed_thread_ids.append(threading.get_ident())
        return real_helper(html_text, base_url)

    async def _fake_get(url: str) -> tuple[httpx.Response, str]:
        html = (
            "<html><head><title>Threaded &amp; Title</title></head>"
            "<body><article><img src='https://example.com/pic.png' alt='Pic'/>"
            "<p>Hello world</p></article></body></html>"
        )
        return (
            httpx.Response(
                200,
                text=html,
                headers={"Content-Type": "text/html"},
                request=httpx.Request("GET", url),
            ),
            url,
        )

    monkeypatch.setattr(web_extraction, "_extract_html_images_and_title", _recording_helper)
    monkeypatch.setattr(web_extraction, "_get_with_public_redirects", _fake_get)
    monkeypatch.setattr(web_extraction, "_trafilatura_markdown", lambda html, base: "Hello world")

    result = asyncio.run(web_extraction._direct_http_fetch("https://example.com/page"))

    assert result is not None
    assert result["title"] == "Threaded & Title"
    assert [img["url"] for img in result["images"]] == ["https://example.com/pic.png"]
    assert len(observed_thread_ids) == 1
    assert observed_thread_ids[0] != main_thread_id


def test_abort_blocked_cloakbrowser_request_uses_sync_validation_without_dns_cache(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls: list[tuple[str, int]] = []
    resolved_ips = iter(["93.184.216.34", "127.0.0.1"])

    def _fake_getaddrinfo(host: str, port: int, *args: Any, **kwargs: Any) -> list[Any]:
        dns_calls.append((host, port))
        ip = next(resolved_ips)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, port))]

    monkeypatch.setattr(web_safe_fetch.socket, "getaddrinfo", _fake_getaddrinfo)

    class _FakeRoute:
        def __init__(self, url: str) -> None:
            self.request = SimpleNamespace(url=url)
            self.aborted = False
            self.continued = False

        def abort(self) -> None:
            self.aborted = True

        def continue_(self) -> None:
            self.continued = True

    async def _run_inside_active_loop() -> None:
        # Calling inside a running event loop proves asyncio.run() is no longer used.
        public_route = _FakeRoute("https://rebind.example.com/asset.js")
        web_extraction._abort_blocked_cloakbrowser_request(public_route)
        assert public_route.continued is True
        assert public_route.aborted is False

        # Second call for the same host re-resolves DNS (no caching) and blocks private IP.
        rebound_route = _FakeRoute("https://rebind.example.com/asset.js")
        web_extraction._abort_blocked_cloakbrowser_request(rebound_route)
        assert rebound_route.aborted is True
        assert rebound_route.continued is False

        localhost_route = _FakeRoute("http://localhost:8000/secret")
        web_extraction._abort_blocked_cloakbrowser_request(localhost_route)
        assert localhost_route.aborted is True
        assert localhost_route.continued is False

    asyncio.run(_run_inside_active_loop())
    assert dns_calls == [("rebind.example.com", 443), ("rebind.example.com", 443)]


# ---------------------------------------------------------------------------
# 2. web_adapters.py — bounded parallel queries + deterministic order
# ---------------------------------------------------------------------------


def test_search_with_model_runs_queries_concurrently_and_preserves_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    queries = [f"query-{i}" for i in range(6)]
    in_flight = 0
    max_in_flight = 0

    async def _fake_generate_queries(*args: Any, **kwargs: Any) -> list[str]:
        return list(queries)

    async def _fake_adapter(
        client: httpx.AsyncClient,
        query: str,
        max_results: int,
        *,
        include_images: bool = False,
    ) -> list[dict[str, Any]]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            idx = int(query.split("-")[1])
            # Earlier queries finish slower to verify deterministic ordering.
            await asyncio.sleep(0.04 - idx * 0.005)
            return [
                {"url": f"https://example.com/item-{idx}", "title": f"T{idx}", "snippet": ""},
                {"url": "https://example.com/shared", "title": f"Shared-{idx}", "snippet": ""},
            ]
        finally:
            in_flight -= 1

    config_loader = SimpleNamespace(operation_rules={web_adapters.WEB_SEARCH_SECTION: {"search-m": {}}})
    runtime_snapshot = SimpleNamespace(
        operation_dispatcher=None,
        config_loader=config_loader,
        proxy_http_clients={},
    )
    services = SimpleNamespace(http_client=MagicMock(), upstream_routing_state=UpstreamRoutingState())
    request = _make_request(services=services, runtime_snapshot=runtime_snapshot)

    monkeypatch.setattr(web_adapters, "_generate_queries", _fake_generate_queries)
    monkeypatch.setattr(web_adapters, "_search_adapter_enabled", lambda name: name == "tavily")
    monkeypatch.setitem(web_adapters._SEARCH_ADAPTERS, "tavily", _fake_adapter)
    monkeypatch.setattr(web_adapters, "update_active_request_from_state", lambda req: None)

    results = asyncio.run(
        web_adapters._search_with_model(
            request,
            search_model="search-m",
            query="root",
            max_results=5,
            num_queries=6,
            language="en",
            usage_accumulator=web_adapters._UsageAccumulator(),
        )
    )

    assert 1 < max_in_flight <= web_adapters.WEB_SEARCH_QUERY_CONCURRENCY
    assert [item["url"] for item in results] == [
        "https://example.com/item-0",
        "https://example.com/shared",
        "https://example.com/item-1",
        "https://example.com/item-2",
        "https://example.com/item-3",
    ]
    # Shared URL keeps the first query's entry deterministically.
    assert results[1]["title"] == "Shared-0"


# ---------------------------------------------------------------------------
# 3. web.py — concurrent _plan_evidence_matrix and _search_languages
# ---------------------------------------------------------------------------


def _build_web_research_request(payload: dict[str, Any]) -> Request:
    body_bytes = json.dumps(payload).encode("utf-8")

    async def _receive() -> dict[str, Any]:
        return {"type": "http.request", "body": body_bytes, "more_body": False}

    config_loader = SimpleNamespace(
        operation_rules={
            web.WEB_RESEARCH_SECTION: {
                "res-model": {
                    "search_model": "search-m",
                    "read_model": "read-m",
                    "rerank_model": "rerank-m",
                    "analysis_model": "analysis-m",
                }
            }
        }
    )
    runtime_snapshot = SimpleNamespace(
        operation_dispatcher=None,
        config_loader=config_loader,
        proxy_http_clients={},
    )
    services = SimpleNamespace(http_client=MagicMock())
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/v1/web/research",
        "headers": [(b"content-type", b"application/json")],
        "query_string": b"",
        "client": ("127.0.0.1", 12345),
        "app": SimpleNamespace(state=SimpleNamespace(services=services)),
    }
    request = Request(scope, _receive)
    request.state.runtime_snapshot = runtime_snapshot
    return request


def test_web_research_overlaps_evidence_plan_and_language_search(monkeypatch: pytest.MonkeyPatch) -> None:
    plan_started = asyncio.Event()
    search_started = asyncio.Event()

    async def _fake_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        plan_started.set()
        await asyncio.wait_for(search_started.wait(), timeout=2.0)
        return {"mode": "not_applicable", "reason": "test"}

    async def _fake_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        search_started.set()
        await asyncio.wait_for(plan_started.wait(), timeout=2.0)
        return []

    monkeypatch.setattr(web, "enforce_virtual_key_access", lambda req, model: None)
    monkeypatch.setattr(web, "update_active_request_from_state", lambda req: None)
    monkeypatch.setattr(web, "_plan_evidence_matrix", _fake_plan)
    monkeypatch.setattr(web, "_search_with_model", _fake_search)

    endpoint = web.web_research.__wrapped__
    request = _build_web_research_request({"model": "res-model", "query": "overlap test", "language": "en"})
    response = asyncio.run(endpoint(request))

    assert response.status_code == 200
    assert plan_started.is_set() and search_started.is_set()


def test_web_research_plan_failure_cancels_and_drains_search(monkeypatch: pytest.MonkeyPatch) -> None:
    search_started = asyncio.Event()
    search_cancelled = False

    async def _failing_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
        await asyncio.wait_for(search_started.wait(), timeout=2.0)
        raise HTTPException(status_code=502, detail="plan failed")

    async def _slow_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
        nonlocal search_cancelled
        search_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            search_cancelled = True
            raise
        return []

    monkeypatch.setattr(web, "enforce_virtual_key_access", lambda req, model: None)
    monkeypatch.setattr(web, "update_active_request_from_state", lambda req: None)
    monkeypatch.setattr(web, "_plan_evidence_matrix", _failing_plan)
    monkeypatch.setattr(web, "_search_with_model", _slow_search)

    endpoint = web.web_research.__wrapped__
    request = _build_web_research_request({"model": "res-model", "query": "cancel test", "language": "en"})

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(endpoint(request))

    assert exc_info.value.status_code == 502
    assert search_cancelled is True


# ---------------------------------------------------------------------------
# 4. web_research_orchestration.py + agents/web_research.py
# ---------------------------------------------------------------------------


def test_web_research_orchestration_bounds_llm_concurrency(monkeypatch: pytest.MonkeyPatch) -> None:
    request = _make_request()
    config_loader = SimpleNamespace()
    http_client = MagicMock()
    usage_acc = web_adapters._UsageAccumulator()

    # 4a. _prepare_relevant_articles
    in_flight = 0
    max_in_flight = 0

    async def _fake_llm_relevance(*args: Any, **kwargs: Any) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return "relevant excerpt"
        finally:
            in_flight -= 1

    monkeypatch.setattr(web_research_orchestration, "_call_internal_text_model", _fake_llm_relevance)
    long_content = "x" * (web_research_orchestration.ARTICLE_RELEVANCE_THRESHOLD_CHARS + 100)
    articles = [{"url": f"https://example.com/{i}", "title": f"A{i}", "content": long_content} for i in range(8)]
    prepared = asyncio.run(
        web_research_orchestration._prepare_relevant_articles(
            request,
            config_loader,
            http_client,
            relevance_model="m",
            query="q",
            articles=articles,
            usage_accumulator=usage_acc,
        )
    )
    assert len(prepared) == 8
    assert 1 < max_in_flight <= web_research_orchestration.WEB_RESEARCH_LLM_CONCURRENCY

    # 4b. _analyze_articles
    in_flight = 0
    max_in_flight = 0

    async def _fake_llm_analyze(*args: Any, **kwargs: Any) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return "- fact"
        finally:
            in_flight -= 1

    monkeypatch.setattr(web_research_orchestration, "_call_internal_text_model", _fake_llm_analyze)
    short_articles = [{"url": f"https://example.com/{i}", "title": f"A{i}", "content": "body"} for i in range(8)]
    analysis = asyncio.run(
        web_research_orchestration._analyze_articles(
            request,
            config_loader,
            http_client,
            analysis_model="m",
            query="q",
            output_language="English",
            articles=short_articles,
            usage_accumulator=usage_acc,
        )
    )
    assert analysis == "- fact"
    assert 1 < max_in_flight <= web_research_orchestration.WEB_RESEARCH_LLM_CONCURRENCY

    # 4c. _build_evidence_matrix_from_articles
    in_flight = 0
    max_in_flight = 0

    async def _fake_llm_evidence(*args: Any, **kwargs: Any) -> str:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return '{"candidates": []}'
        finally:
            in_flight -= 1

    monkeypatch.setattr(web_research_orchestration, "_call_internal_text_model", _fake_llm_evidence)
    evidence_plan = {
        "mode": "applied",
        "entity_type": "tool",
        "requirements": [{"id": "req_1", "label": "R1", "criterion": "C1"}],
    }
    matrix = asyncio.run(
        web_research_orchestration._build_evidence_matrix_from_articles(
            request,
            config_loader,
            http_client,
            analysis_model="m",
            query="q",
            evidence_plan=evidence_plan,
            articles=short_articles,
            usage_accumulator=usage_acc,
        )
    )
    assert matrix["mode"] == "applied"
    assert 1 < max_in_flight <= web_research_orchestration.WEB_RESEARCH_LLM_CONCURRENCY


def test_deep_research_callback_image_offloads_decode_and_publish() -> None:
    main_thread_id = threading.get_ident()
    publish_calls: list[tuple[int, str, bytes]] = []

    class _FakeStorage:
        def publish_png(self, data: bytes, filename: str, job_id: str) -> SimpleNamespace:
            publish_calls.append((threading.get_ident(), filename, data))
            return SimpleNamespace(url=f"https://cdn.local/{job_id}/{filename}")

    img0 = _make_png_bytes(16, 16)
    img1 = _make_png_bytes(32, 32)
    b64_0 = base64.b64encode(img0).decode("ascii")
    b64_1 = base64.b64encode(img1).decode("ascii")

    class _FakeHttpClient:
        def __init__(self, data: list[dict[str, Any]]) -> None:
            self._data = data

        async def post(self, *args: Any, **kwargs: Any) -> httpx.Response:
            return httpx.Response(200, json={"data": self._data})

    def _make_ctx(data: list[dict[str, Any]]) -> web_research_orchestration._DeepResearchCallbackContext:
        return web_research_orchestration._DeepResearchCallbackContext(
            services=SimpleNamespace(http_client=_FakeHttpClient(data), image_storage=_FakeStorage()),
            runtime_snapshot=SimpleNamespace(),
            request_scope={"type": "http", "headers": ()},
            request_state={},
            virtual_key=None,
            accounting_owner=MagicMock(),
            job_id="job-42",
            child_context_token="tok",
            search_model="s",
            read_model="r",
            output_format="markdown",
            image_generation_model="img-m",
            image_generation_size="1024x1024",
        )

    callback = DeepResearchCallbackRequest(
        message_id="cb-1",
        job_id="job-42",
        operation=DeepResearchCallbackOperation.IMAGE,
        arguments={
            "prompt": "architecture diagram",
            "num_images": 2,
            "aspect_ratio": "1:1",
            "context": "",
            "research_id": "job-42",
            "style": "diagram",
        },
    )

    ctx = _make_ctx([{"b64_json": b64_0}, {"b64_json": b64_1}])
    result = asyncio.run(ctx._image(callback))

    assert len(result) == 2
    assert len(publish_calls) == 2
    assert all(tid != main_thread_id for tid, _, _ in publish_calls)
    assert publish_calls[0][1].endswith("_0.png")
    assert publish_calls[1][1].endswith("_1.png")
    assert publish_calls[0][2] == img0
    assert publish_calls[1][2] == img1
    assert result[0]["url"].endswith("_0.png")
    assert result[1]["url"].endswith("_1.png")

    # Invalid base64 preserves exact RuntimeError message.
    bad_ctx = _make_ctx([{"b64_json": "!!!not-base64!!!"}])
    with pytest.raises(RuntimeError, match=r"^Gateway image generation returned invalid image bytes\.$"):
        asyncio.run(bad_ctx._image(callback))


def test_web_research_client_bounds_download_and_llm_concurrency() -> None:
    download_in_flight = 0
    download_max = 0
    llm_in_flight = 0
    llm_max = 0

    async def _fake_completion(messages: list[dict[str, str]], temp: float, max_tokens: int) -> str:
        nonlocal llm_in_flight, llm_max
        llm_in_flight += 1
        llm_max = max(llm_max, llm_in_flight)
        try:
            await asyncio.sleep(0.02)
            return "- extracted fact"
        finally:
            llm_in_flight -= 1

    client = web_research_agent.WebResearchClient(
        research_model="m",
        text_completion=_fake_completion,
        tavily_api_key="key",
    )

    async def _fake_find(query: str, lang: str, num_q: int, max_res: int) -> list[str]:
        return [f"https://example.com/{lang}/{i}" for i in range(6)]

    async def _fake_download(url: str) -> dict[str, str]:
        nonlocal download_in_flight, download_max
        download_in_flight += 1
        download_max = max(download_max, download_in_flight)
        try:
            await asyncio.sleep(0.02)
            return {"url": url, "title": url, "content": f"content for {url}"}
        finally:
            download_in_flight -= 1

    client._find_articles_for_language = _fake_find  # type: ignore[method-assign]
    client._download_content = _fake_download  # type: ignore[method-assign]

    res = asyncio.run(client.execute({"query": "concurrency test", "analyze_content": True}))
    assert res["success"] is True
    assert 1 < download_max <= web_research_agent.WEB_RESEARCH_DOWNLOAD_CONCURRENCY
    assert 1 < llm_max <= web_research_agent.WEB_RESEARCH_LLM_CONCURRENCY


# ---------------------------------------------------------------------------
# 5. web_safe_fetch.py — cached SSLContext
# ---------------------------------------------------------------------------


def test_pinned_transport_reuses_cached_ssl_context(monkeypatch: pytest.MonkeyPatch) -> None:
    web_safe_fetch._get_pinned_ssl_context.cache_clear()
    real_create = httpx.create_ssl_context
    calls = 0

    def _counting_create(*args: Any, **kwargs: Any) -> ssl.SSLContext:
        nonlocal calls
        calls += 1
        return real_create(*args, **kwargs)

    monkeypatch.setattr(web_safe_fetch.httpx, "create_ssl_context", _counting_create)

    try:
        t1 = web_safe_fetch._PinnedHostAsyncHTTPTransport(
            pinned_host="example.com",
            pinned_port=443,
            connect_ip="93.184.216.34",
        )
        t2 = web_safe_fetch._PinnedHostAsyncHTTPTransport(
            pinned_host="example.org",
            pinned_port=443,
            connect_ip="93.184.216.34",
        )
        assert calls == 1
        assert web_safe_fetch._get_pinned_ssl_context() is web_safe_fetch._get_pinned_ssl_context()
        asyncio.run(t1.aclose())
        asyncio.run(t2.aclose())
    finally:
        web_safe_fetch._get_pinned_ssl_context.cache_clear()


# ---------------------------------------------------------------------------
# 6. images.py + image_dimensions.py
# ---------------------------------------------------------------------------


def test_images_offloads_build_downstream_image_request_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    main_thread_id = threading.get_ident()
    build_thread_ids: list[int] = []

    def _fake_build(*args: Any, **kwargs: Any) -> SimpleNamespace:
        build_thread_ids.append(threading.get_ident())
        return SimpleNamespace(transport="json", json_payload={"prompt": "hi"})

    async def _fake_prepare(*args: Any, **kwargs: Any) -> tuple[str, dict[str, str], tuple[int, float]]:
        return "https://provider.test/images", {}, (0, 0.0)

    async def _fake_exec(*args: Any, **kwargs: Any) -> tuple[dict[str, Any], int]:
        return {"data": [{"b64_json": "AAAA"}]}, 200

    async def _fake_finalize(owner: Any, response: Any, observation: Any) -> Any:
        return response

    monkeypatch.setattr(images, "build_downstream_image_request", _fake_build)
    monkeypatch.setattr(images, "estimate_image_request_processing_weight", lambda *a, **k: None)
    monkeypatch.setattr(images, "_prepare_route_request", _fake_prepare)
    monkeypatch.setattr(images, "_execute_prepared_request", _fake_exec)
    monkeypatch.setattr(images, "normalize_downstream_image_response", lambda data, route, payload: data)
    monkeypatch.setattr(images, "_parse_image_terminal_observation", lambda *a, **k: SimpleNamespace(with_rejected_attempts=lambda x: SimpleNamespace()))
    monkeypatch.setattr(images, "record_operation_route_attempt", lambda *a, **k: None)
    monkeypatch.setattr(images, "clear_route_key_fingerprint", lambda req: None)
    monkeypatch.setattr(images, "finalize_buffered_operation", _fake_finalize)

    route = OperationRoute(provider="p1", model="m1", target_path="/images/generations")
    config_loader = SimpleNamespace(providers_config={"p1": {"baseUrl": "https://provider.test"}})
    request = _make_request()

    response = asyncio.run(
        images._proxy_image_with_fallback(
            request,
            owner=MagicMock(),
            dispatcher=MagicMock(),
            config_loader_instance=config_loader,
            proxy_http_clients={},
            http_client=MagicMock(),
            routes=[route],
            requested_model="img-m",
            request_payload={"model": "img-m", "prompt": "hi"},
            operation_section=images.IMAGE_GENERATIONS_SECTION,
            operation_name=images.IMAGE_GENERATION_OPERATION,
            operation_label="Image generation",
            source_transport="json",
        )
    )

    assert response.status_code == 200
    assert len(build_thread_ids) == 1
    assert build_thread_ids[0] != main_thread_id


def test_image_dimensions_offloads_b64_bounds_concurrency_and_cancels_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main_thread_id = threading.get_ident()
    decode_threads: list[int] = []
    real_decode = image_dimensions._decode_and_inspect_b64_image

    def _recording_decode(raw_b64: str) -> tuple[str | None, tuple[int, int]]:
        decode_threads.append(threading.get_ident())
        return real_decode(raw_b64)

    monkeypatch.setattr(image_dimensions, "_decode_and_inspect_b64_image", _recording_decode)

    png_bytes = _make_png_bytes(64, 64)
    b64 = base64.b64encode(png_bytes).decode("ascii")
    matched = asyncio.run(
        image_dimensions.image_dimensions_match(
            {"data": [{"b64_json": b64}]},
            (64, 64),
            MagicMock(),
        )
    )
    assert matched is True
    assert len(decode_threads) == 1
    assert decode_threads[0] != main_thread_id

    # Multi-item URL concurrency is bounded by IMAGE_DIMENSIONS_CHECK_CONCURRENCY.
    in_flight = 0
    max_in_flight = 0

    async def _fake_get_public(url: str) -> tuple[httpx.Response, str]:
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        try:
            await asyncio.sleep(0.02)
            return httpx.Response(200, content=png_bytes, request=httpx.Request("GET", url)), url
        finally:
            in_flight -= 1

    monkeypatch.setattr(image_dimensions, "_get_with_public_redirects", _fake_get_public)
    multi_matched = asyncio.run(
        image_dimensions.image_dimensions_match(
            {"data": [{"url": f"https://example.com/{i}.png"} for i in range(8)]},
            (64, 64),
            MagicMock(),
        )
    )
    assert multi_matched is True
    assert 1 < max_in_flight <= image_dimensions.IMAGE_DIMENSIONS_CHECK_CONCURRENCY

    # Error in one item cancels and drains remaining tasks.
    slow_started = asyncio.Event()
    slow_cancelled = False

    async def _mixed_get(url: str) -> tuple[httpx.Response, str]:
        nonlocal slow_cancelled
        if url.endswith("bad.png"):
            await asyncio.wait_for(slow_started.wait(), timeout=2.0)
            raise HTTPException(status_code=400, detail="blocked")
        slow_started.set()
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            slow_cancelled = True
            raise
        return httpx.Response(200, content=png_bytes, request=httpx.Request("GET", url)), url

    monkeypatch.setattr(image_dimensions, "_get_with_public_redirects", _mixed_get)
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            image_dimensions.image_dimensions_match(
                {"data": [{"url": "https://example.com/slow.png"}, {"url": "https://example.com/bad.png"}]},
                (64, 64),
                MagicMock(),
            )
        )
    assert exc_info.value.status_code == 502
    assert slow_cancelled is True


# ---------------------------------------------------------------------------
# 7. audio_adapters.py — _extract_audio_bytes offloaded via _run_owned_blocking
# ---------------------------------------------------------------------------


def test_nvidia_riva_offloads_extract_audio_bytes_to_thread(monkeypatch: pytest.MonkeyPatch) -> None:
    main_thread_id = threading.get_ident()
    read_thread_ids: list[int] = []
    wav_bytes = _make_wav_bytes()

    class _TrackingStream(io.BytesIO):
        def read(self, size: int | None = -1) -> bytes:
            read_thread_ids.append(threading.get_ident())
            return super().read(size)

    def _fake_grpc_call(**kwargs: Any) -> SimpleNamespace:
        alt = SimpleNamespace(transcript="hello world", language_code=["en"], words=[])
        return SimpleNamespace(results=[SimpleNamespace(alternatives=[alt], audio_processed=0.1)])

    monkeypatch.setattr(audio_adapters, "_call_nvidia_riva_grpc_sync", _fake_grpc_call)

    stream = _TrackingStream(wav_bytes)
    response = asyncio.run(
        audio_adapters.transcribe_with_nvidia_riva_grpc(
            request_payload={"model": "riva-model"},
            files_payload=[("file", ("sample.wav", stream, "audio/wav"))],
            provider_name="nvidia",
            provider_base_url="grpc://riva.local:50051",
            provider_api_key="key",
            route_custom_headers={},
            target_path="/audio/transcriptions",
        )
    )

    assert response.body == {"text": "hello world"}
    assert len(read_thread_ids) == 1
    assert read_thread_ids[0] != main_thread_id


# ---------------------------------------------------------------------------
# 8. External cancellation (task.cancel() -> CancelledError) & shielded drain
# ---------------------------------------------------------------------------


def test_web_research_external_cancellation_drains_plan_and_search(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        plan_started = asyncio.Event()
        search_started = asyncio.Event()
        plan_drained = False
        search_drained = False

        async def _slow_plan(*args: Any, **kwargs: Any) -> dict[str, Any]:
            nonlocal plan_drained
            plan_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                plan_drained = True
                raise
            return {"mode": "not_applicable", "reason": "test"}

        async def _slow_search(*args: Any, **kwargs: Any) -> list[dict[str, Any]]:
            nonlocal search_drained
            search_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                await asyncio.sleep(0.02)
                search_drained = True
                raise
            return []

        monkeypatch.setattr(web, "enforce_virtual_key_access", lambda req, model: None)
        monkeypatch.setattr(web, "update_active_request_from_state", lambda req: None)
        monkeypatch.setattr(web, "_plan_evidence_matrix", _slow_plan)
        monkeypatch.setattr(web, "_search_with_model", _slow_search)

        endpoint = web.web_research.__wrapped__
        request = _build_web_research_request({"model": "res-model", "query": "cancel drain", "language": "en"})
        task = asyncio.create_task(endpoint(request))

        await asyncio.wait_for(asyncio.gather(plan_started.wait(), search_started.wait()), timeout=2.0)
        task.cancel()
        await asyncio.sleep(0)
        # Second cancel during shielded drain must not abort draining.
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert plan_drained is True
        assert search_drained is True

    asyncio.run(_run())


def test_image_dimensions_external_cancellation_drains_all_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        started_count = 0
        all_started = asyncio.Event()
        cleanup_started = asyncio.Event()
        drained_count = 0
        png_bytes = _make_png_bytes(64, 64)

        async def _slow_get(url: str) -> tuple[httpx.Response, str]:
            nonlocal started_count, drained_count
            started_count += 1
            if started_count == 3:
                all_started.set()
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cleanup_started.set()
                await asyncio.sleep(0.02)
                drained_count += 1
                raise
            return httpx.Response(200, content=png_bytes, request=httpx.Request("GET", url)), url

        monkeypatch.setattr(image_dimensions, "_get_with_public_redirects", _slow_get)

        task = asyncio.create_task(
            image_dimensions.image_dimensions_match(
                {"data": [{"url": f"https://example.com/{i}.png"} for i in range(3)]},
                (64, 64),
                MagicMock(),
            )
        )
        await asyncio.wait_for(all_started.wait(), timeout=2.0)
        task.cancel()
        await asyncio.wait_for(cleanup_started.wait(), timeout=2.0)
        await asyncio.sleep(0.005)
        # Second cancel during shielded drain must not abort draining.
        task.cancel()

        with pytest.raises(asyncio.CancelledError):
            await task

        assert drained_count == 3

    asyncio.run(_run())


def test_images_build_downstream_cancellation_waits_for_worker_before_releasing_admission(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def _run() -> None:
        events: list[str] = []
        worker_started = threading.Event()
        worker_release = threading.Event()

        @asynccontextmanager
        async def _fake_admission(request: Request, weight: int) -> AsyncIterator[None]:
            events.append("admission_enter")
            try:
                yield
            finally:
                events.append("admission_exit")

        def _blocking_build(*args: Any, **kwargs: Any) -> SimpleNamespace:
            worker_started.set()
            assert worker_release.wait(timeout=2.0)
            events.append("worker_finished")
            return SimpleNamespace(transport="json", json_payload={"prompt": "hi"})

        async def _fake_prepare(*args: Any, **kwargs: Any) -> tuple[str, dict[str, str], tuple[int, float]]:
            return "https://provider.test/images", {}, (0, 0.0)

        monkeypatch.setattr(images, "admit_upload_processing", _fake_admission)
        monkeypatch.setattr(images, "estimate_image_request_processing_weight", lambda *a, **k: 1024)
        monkeypatch.setattr(images, "build_downstream_image_request", _blocking_build)
        monkeypatch.setattr(images, "_prepare_route_request", _fake_prepare)
        monkeypatch.setattr(images, "clear_route_key_fingerprint", lambda req: None)

        route = OperationRoute(provider="p1", model="m1", target_path="/images/generations")
        config_loader = SimpleNamespace(providers_config={"p1": {"baseUrl": "https://provider.test"}})
        request = _make_request()

        task = asyncio.create_task(
            images._proxy_image_with_fallback(
                request,
                owner=MagicMock(),
                dispatcher=MagicMock(),
                config_loader_instance=config_loader,
                proxy_http_clients={},
                http_client=MagicMock(),
                routes=[route],
                requested_model="img-m",
                request_payload={"model": "img-m", "prompt": "hi"},
                operation_section=images.IMAGE_GENERATIONS_SECTION,
                operation_name=images.IMAGE_GENERATION_OPERATION,
                operation_label="Image generation",
                source_transport="json",
            )
        )

        # Wait for the worker thread to enter build_downstream_image_request.
        while not worker_started.is_set():
            await asyncio.sleep(0.005)

        task.cancel()
        await asyncio.sleep(0.02)
        # Admission lease must stay held while the worker thread is still running.
        assert not task.done()
        assert events == ["admission_enter"]

        worker_release.set()
        with pytest.raises(asyncio.CancelledError):
            await task

        assert events == ["admission_enter", "worker_finished", "admission_exit"]

    asyncio.run(_run())
