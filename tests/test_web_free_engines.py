"""Tests for the keyless web search engines (httpx.MockTransport only, no network)."""
from __future__ import annotations

import base64
import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

import httpx
import pytest

from llm_gateway_core.api.v1 import web_free_engines as engines
from llm_gateway_core.api.v1.web_engine_errors import (
    AuthError,
    BotWallError,
    InvalidResponseError,
    QuotaError,
    TransientError,
)
from tests._async_compat import run_async

FIXTURES = Path(__file__).parent / "fixtures"

Handler = Callable[[httpx.Request], httpx.Response]


def _run(engine: str, handler: Handler, query: str = "python httpx tutorial", max_results: int = 10, **kwargs: Any):
    async def scenario() -> list[dict[str, Any]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await engines.FREE_ENGINES[engine](client, query, max_results, **kwargs)

    return run_async(scenario())


def _pad(html: str) -> str:
    """Keep the body above the 500-byte retry threshold."""
    return html + "<!--" + "p" * 600 + "-->"


@pytest.fixture
def retry_delays(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    delays: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(engines.asyncio, "sleep", fake_sleep)
    return delays


# --- bing ---------------------------------------------------------------------


def test_bing_parses_real_serp_fixture() -> None:
    html = (FIXTURES / "bing_serp.html").read_text(encoding="utf-8")
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=html)

    results = _run("bing", handler)
    assert [item["url"] for item in results] == [
        "https://www.python.org/",
        "https://www.w3schools.com/python/python_intro.asp",
        "https://www.python.org/downloads/",
        "https://www.learnpython.org/",
        "https://en.wikipedia.org/wiki/Python",
        "https://pypi.org/",
        "https://code-basics.com/ru/languages/python",
        "https://docs.python.org/",
        "https://en.wikipedia.org/wiki/Python_(programming_language)",
        "https://ru.wikipedia.org/wiki/Python",
    ]
    assert all(item["title"] for item in results)
    assert all(item["snippet"] and len(item["snippet"]) <= 300 for item in results)
    assert "Python" in results[0]["title"]
    assert len(requests) == 1
    assert requests[0].url.params["q"] == "python httpx tutorial"
    assert "mkt" not in requests[0].url.params
    assert requests[0].headers["accept-language"] == "en-US,en;q=0.9"
    assert "Chrome" in requests[0].headers["user-agent"]


def test_bing_accept_language_by_language() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["accept-language"])
        return httpx.Response(200, text=_pad("<html><body></body></html>"))

    _run("bing", handler, language="ru")
    _run("bing", handler, language="zh")
    _run("bing", handler, language="fr")
    assert seen == ["ru-RU,ru;q=0.9,en-US;q=0.8", "zh-CN,zh;q=0.9", "en-US,en;q=0.9"]


def test_bing_unwraps_ck_a_redirect() -> None:
    target = "https://example.com/article?x=1&y=2"
    encoded = base64.urlsafe_b64encode(target.encode()).decode().rstrip("=")
    href = f"https://www.bing.com/ck/a?!&amp;&amp;p=deadbeef&amp;u=a1{encoded}&amp;nt=1"
    html = _pad(
        '<html><body><ol id="b_results">'
        f'<li class="b_algo"><h2><a href="{href}">Example <strong>Title</strong></a></h2>'
        "<p>Some snippet text.</p></li>"
        '<li class="b_algo"><h2><a href="https://plain.example.org/page">Plain</a></h2><p>Plain snippet.</p></li>'
        "</ol></body></html>"
    )

    results = _run("bing", lambda request: httpx.Response(200, text=html))
    assert results[0]["url"] == target
    assert results[0]["title"] == "Example Title"
    assert results[0]["snippet"] == "Some snippet text."
    assert results[1]["url"] == "https://plain.example.org/page"


def test_bing_keeps_href_when_u_is_broken() -> None:
    href = "https://www.bing.com/ck/a?!&amp;u=a1%%%not-base64%%%"
    html = _pad(f'<li class="b_algo"><h2><a href="{href}">Broken</a></h2><p>s</p></li>')
    results = _run("bing", lambda request: httpx.Response(200, text=html))
    assert results[0]["url"] == href.replace("&amp;", "&")


def test_bing_empty_serp_is_not_retried(retry_delays: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=_pad('<html><body><ol id="b_results"></ol></body></html>'))

    assert _run("bing", handler) == []
    assert len(calls) == 1
    assert retry_delays == []


def test_bing_challenge_markers_raise_bot_wall() -> None:
    with pytest.raises(BotWallError):
        _run("bing", lambda request: httpx.Response(200, text="<html><title>captcha challenge</title></html>"))


# --- tavily keyless -------------------------------------------------------------

def test_tavily_keyless_sends_header_without_api_key_and_clips_snippet() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"results": [{"url": "https://example.com/", "title": "Example", "content": "s" * 400}]},
        )

    results = _run("tavily_keyless", handler, max_results=5)
    assert results == [{"url": "https://example.com/", "title": "Example", "snippet": "s" * 300}]
    assert str(requests[0].url) == "https://api.tavily.com/search"
    assert requests[0].headers["x-tavily-access-mode"] == "keyless"
    assert json.loads(requests[0].content) == {
        "query": "python httpx tutorial",
        "max_results": 5,
        "search_depth": "basic",
    }

def test_tavily_keyless_clamps_max_results_to_20() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"results": []})

    _run("tavily_keyless", handler, max_results=50)
    assert json.loads(requests[0].content)["max_results"] == 20

def test_tavily_keyless_403_raises_auth() -> None:
    with pytest.raises(AuthError):
        _run("tavily_keyless", lambda request: httpx.Response(403, text="forbidden"))

def test_tavily_keyless_429_raises_quota() -> None:
    with pytest.raises(QuotaError):
        _run("tavily_keyless", lambda request: httpx.Response(429, text="rate limited"))

def test_tavily_keyless_status_mapping() -> None:
    with pytest.raises(QuotaError):
        _run("tavily_keyless", lambda request: httpx.Response(402, text="payment required"))
    with pytest.raises(InvalidResponseError):
        _run("tavily_keyless", lambda request: httpx.Response(404, text="not found"))
    with pytest.raises(TransientError):
        _run("tavily_keyless", lambda request: httpx.Response(503, text="unavailable"))

# --- ddg html -----------------------------------------------------------------

DDG_HTML = _pad(
    '<html><body><div class="result results_links results_links_deep web-result">'
    '<h2><a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fpage&amp;rut=abc">'
    "Example <b>Page</b></a></h2>"
    '<a class="result__snippet" href="#">An example snippet.</a>'
    "<span>2026-11-01T00:00:00+00:00</span>"
    "</div></body></html>"
)


def test_ddg_html_202_raises_bot_wall_without_retry(retry_delays: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(202, text="anomaly detected")

    with pytest.raises(BotWallError):
        _run("ddg_html", handler)
    assert len(calls) == 1
    assert retry_delays == []


def test_ddg_html_bot_markers_raise_bot_wall() -> None:
    with pytest.raises(BotWallError):
        _run("ddg_html", lambda request: httpx.Response(200, text="<html>Unusual traffic detected</html>"))


def test_ddg_html_parses_results_and_unwraps_uddg() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=DDG_HTML)

    results = _run("ddg_html", handler)
    assert results == [
        {
            "url": "https://example.com/page",
            "title": "Example Page",
            "snippet": "An example snippet.",
            "published_at": "2026-11-01T00:00:00+00:00",
        }
    ]
    assert requests[0].url.params["adlt"] == "-1"
    assert requests[0].url.host == "html.duckduckgo.com"


def test_ddg_html_retries_short_body(retry_delays: list[float]) -> None:
    bodies = ["tiny", DDG_HTML]

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=bodies.pop(0))

    results = _run("ddg_html", handler)
    assert results[0]["url"] == "https://example.com/page"
    assert bodies == []
    assert retry_delays == [1.5]


def test_ddg_html_retries_network_error(retry_delays: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        if len(calls) == 1:
            raise httpx.ConnectError("connection reset", request=request)
        return httpx.Response(200, text=DDG_HTML)

    results = _run("ddg_html", handler)
    assert len(results) == 1
    assert len(calls) == 2
    assert retry_delays == [1.5]


def test_ddg_html_exhausts_three_attempts(retry_delays: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text="tiny")

    with pytest.raises(TransientError, match="empty response"):
        _run("ddg_html", handler)
    assert len(calls) == 3
    assert retry_delays == [1.5, 1.5]


def test_ddg_html_valid_empty_serp_is_not_retried(retry_delays: list[float]) -> None:
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(1)
        return httpx.Response(200, text=_pad("<html><body>No results.</body></html>"))

    assert _run("ddg_html", handler) == []
    assert len(calls) == 1
    assert retry_delays == []


# --- ddg lite -----------------------------------------------------------------


def test_ddg_lite_parses_result_links() -> None:
    html = _pad(
        "<html><body><table>"
        '<tr><td><a class="result-link" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fone.example%2F">One</a></td></tr>'
        '<tr><td class="result-snippet">First snippet.</td></tr>'
        '<tr><td><a class="result-link" href="https://two.example/">Two</a></td></tr>'
        '<tr><td class="result-snippet">Second snippet.</td></tr>'
        "</table></body></html>"
    )
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, text=html)

    results = _run("ddg_lite", handler)
    assert results == [
        {"url": "https://one.example/", "title": "One", "snippet": "First snippet."},
        {"url": "https://two.example/", "title": "Two", "snippet": "Second snippet."},
    ]
    assert requests[0].url.host == "lite.duckduckgo.com"
    assert requests[0].url.params["adlt"] == "-1"


# --- searxng ------------------------------------------------------------------

SEARXNG_JSON = {
    "results": [
        {"url": "https://example.com/a", "title": "A", "content": "Snippet A"},
        {"url": "https://example.com/a", "title": "dup", "content": "dup"},
        {"url": "https://example.com/b", "title": "B", "content": "Snippet B"},
    ]
}


def test_searxng_failover_across_instances() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        host = request.url.host
        if host == "a.example":
            return httpx.Response(500, text="boom")
        if host == "b.example":
            return httpx.Response(200, text="<html>not json</html>")
        return httpx.Response(200, json=SEARXNG_JSON)

    results = _run(
        "searxng",
        handler,
        instances=["https://a.example", "https://b.example", "https://c.example"],
        bearer_token="tok",
    )
    assert [item["url"] for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert results[0]["snippet"] == "Snippet A"
    assert [request.url.host for request in requests] == ["a.example", "b.example", "c.example"]
    assert all(request.url.params["format"] == "json" for request in requests)
    assert all(request.headers["authorization"] == "Bearer tok" for request in requests)


def test_searxng_none_instances_use_default_public_list() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=SEARXNG_JSON)

    _run("searxng", handler)
    _run("searxng", handler, instances=None)
    assert [request.url.host for request in requests] == ["opnxng.com", "opnxng.com"]
    assert "authorization" not in requests[0].headers


def test_searxng_empty_instances_fail_fast_without_requests() -> None:
    # An operator clearing SEARXNG_INSTANCES must not silently get the public instances.
    calls: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        return httpx.Response(200, json=SEARXNG_JSON)

    with pytest.raises(TransientError, match="^no instances configured$"):
        _run("searxng", handler, instances=[])
    assert calls == []


def test_searxng_follows_redirect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://a.example/search/"})
        return httpx.Response(200, json=SEARXNG_JSON)

    results = _run("searxng", handler, instances=["https://a.example"])
    assert [item["url"] for item in results] == ["https://example.com/a", "https://example.com/b"]
    assert len(requests) == 2
    assert requests[1].url.path == "/search/"


def test_searxng_all_failed_aggregates_transient_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(500, text="boom")
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(TransientError) as excinfo:
        _run("searxng", handler, instances=["https://a.example", "https://b.example"])
    message = str(excinfo.value)
    assert message.startswith("all SearXNG instances failed: ")
    assert "a.example: HTTP 500" in message
    assert "b.example" in message
    assert len(message.removeprefix("all SearXNG instances failed: ")) <= 300


def test_searxng_all_bot_checks_raise_bot_wall() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(429, text="slow down")
        return httpx.Response(200, text="<html>captcha check</html>")

    with pytest.raises(BotWallError):
        _run("searxng", handler, instances=["https://a.example", "https://b.example"])


def test_searxng_mixed_failures_stay_transient() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.host == "a.example":
            return httpx.Response(429, text="slow down")
        return httpx.Response(503, text="unavailable")

    with pytest.raises(TransientError):
        _run("searxng", handler, instances=["https://a.example", "https://b.example"])


# --- anysearch ----------------------------------------------------------------


def test_anysearch_maps_results_and_clips_snippet() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"results": [{"url": "https://example.com/", "title": "Example", "snippet": "s" * 400}]},
            },
        )

    results = _run("anysearch", handler, max_results=5)
    assert results == [{"url": "https://example.com/", "title": "Example", "snippet": "s" * 300}]
    assert json.loads(requests[0].content) == {"query": "python httpx tutorial", "max_results": 5}
    assert str(requests[0].url) == "https://api.anysearch.com/v1/search"


def test_anysearch_nonzero_code_raises_invalid_response() -> None:
    response = httpx.Response(200, json={"code": 1001, "message": "bad request"})
    with pytest.raises(InvalidResponseError, match="bad request"):
        _run("anysearch", lambda request: response)


def test_anysearch_429_raises_quota() -> None:
    with pytest.raises(QuotaError):
        _run("anysearch", lambda request: httpx.Response(429, text="rate limited"))


def test_anysearch_403_raises_auth() -> None:
    with pytest.raises(AuthError):
        _run("anysearch", lambda request: httpx.Response(403, text="forbidden"))


def test_anysearch_follows_redirect() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if len(requests) == 1:
            return httpx.Response(302, headers={"location": "https://api.anysearch.com/v1/search/"})
        return httpx.Response(
            200,
            json={
                "code": 0,
                "data": {"results": [{"url": "https://example.com/", "title": "Example", "snippet": "s"}]},
            },
        )

    results = _run("anysearch", handler)
    assert results[0]["url"] == "https://example.com/"
    assert len(requests) == 2


def test_anysearch_status_mapping() -> None:
    with pytest.raises(QuotaError):
        _run("anysearch", lambda request: httpx.Response(402, text="payment required"))
    with pytest.raises(InvalidResponseError):
        _run("anysearch", lambda request: httpx.Response(404, text="not found"))
    with pytest.raises(TransientError):
        _run("anysearch", lambda request: httpx.Response(503, text="unavailable"))


def test_anysearch_network_error_propagates_raw() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused", request=request)

    with pytest.raises(httpx.ConnectError):
        _run("anysearch", handler)


# --- youcom -------------------------------------------------------------------


def test_youcom_joins_snippets_and_falls_back_to_description() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "results": {
                    "web": [
                        {"url": "https://a.example/", "title": "A", "snippets": ["first", "", "second"]},
                        {"url": "https://b.example/", "title": "B", "description": "fallback text"},
                    ]
                }
            },
        )

    results = _run("youcom", handler, max_results=50)
    assert results == [
        {"url": "https://a.example/", "title": "A", "snippet": "first second"},
        {"url": "https://b.example/", "title": "B", "snippet": "fallback text"},
    ]
    assert requests[0].url.params["count"] == "20"
    assert requests[0].url.params["query"] == "python httpx tutorial"
    assert requests[0].headers["accept"] == "application/json"
    assert "Chrome" in requests[0].headers["user-agent"]


def test_youcom_429_raises_quota() -> None:
    with pytest.raises(QuotaError):
        _run("youcom", lambda request: httpx.Response(429, text="rate limited"))


def test_youcom_does_not_follow_redirects() -> None:
    # JS parity: youcom/firecrawl use redirect:"error"; a 3xx must not be chased.
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "https://elsewhere.example/"})

    with pytest.raises(InvalidResponseError):
        _run("youcom", handler)
    assert len(requests) == 1


# --- firecrawl ----------------------------------------------------------------


def test_firecrawl_maps_web_results_and_clamps_limit() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "success": True,
                "data": {"web": [{"url": "https://example.com/", "title": "Example", "description": "About it."}]},
            },
        )

    results = _run("firecrawl", handler, max_results=50)
    assert results == [{"url": "https://example.com/", "title": "Example", "snippet": "About it."}]
    assert json.loads(requests[0].content) == {"query": "python httpx tutorial", "limit": 10}
    _run("firecrawl", handler, max_results=0)
    assert json.loads(requests[1].content)["limit"] == 1


def test_firecrawl_keyless_refusal_raises_bot_wall() -> None:
    refusal = httpx.Response(403, text="Access from VPN or proxy egress is not allowed without an API key")
    with pytest.raises(BotWallError):
        _run("firecrawl", lambda request: refusal)


def test_firecrawl_plain_403_raises_auth() -> None:
    with pytest.raises(AuthError):
        _run("firecrawl", lambda request: httpx.Response(403, text="Forbidden"))


def test_firecrawl_429_raises_quota() -> None:
    with pytest.raises(QuotaError):
        _run("firecrawl", lambda request: httpx.Response(429, text="rate limited"))


# --- keenable -----------------------------------------------------------------

KEENABLE_RESULTS = {
    "results": [
        {"url": "https://a.example/", "title": "A", "snippet": "Snippet A", "published_at": "2026-11-01"},
        {"url": "https://b.example/", "title": "B"},
    ]
}


def _keenable_envelope() -> dict[str, Any]:
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"content": [{"type": "text", "text": json.dumps(KEENABLE_RESULTS)}]},
    }


def test_keenable_plain_json_response() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json=_keenable_envelope())

    results = _run("keenable", handler)
    assert results == [
        {"url": "https://a.example/", "title": "A", "snippet": "Snippet A", "published_at": "2026-11-01"},
        {"url": "https://b.example/", "title": "B", "snippet": ""},
    ]
    body = json.loads(requests[0].content)
    assert body["method"] == "tools/call"
    assert body["params"] == {"name": "search", "arguments": {"query": "python httpx tutorial"}}
    assert requests[0].headers["accept"] == "application/json, text/event-stream"
    assert str(requests[0].url) == "https://api.keenable.ai/mcp"


def test_keenable_sse_response() -> None:
    sse = f"event: message\ndata: {json.dumps(_keenable_envelope())}\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=sse, headers={"content-type": "text/event-stream"})

    results = _run("keenable", handler)
    assert [item["url"] for item in results] == ["https://a.example/", "https://b.example/"]
    assert results[1]["snippet"] == ""


def test_keenable_sse_multiline_data() -> None:
    payload = json.dumps(_keenable_envelope(), separators=(",", ":"))
    split = payload.index(',"id"') + 1  # between tokens, never inside a string literal
    sse = f"data: {payload[:split]}\ndata: {payload[split:]}\n\n"

    results = _run("keenable", lambda request: httpx.Response(200, text=sse))
    assert len(results) == 2


def test_keenable_envelope_error_raises_invalid_response() -> None:
    envelope = {"jsonrpc": "2.0", "id": 1, "error": {"code": -32601, "message": "method not found"}}
    with pytest.raises(InvalidResponseError, match="method not found"):
        _run("keenable", lambda request: httpx.Response(200, json=envelope))


def test_keenable_tool_is_error_raises_invalid_response() -> None:
    envelope = {
        "jsonrpc": "2.0",
        "id": 1,
        "result": {"isError": True, "content": [{"type": "text", "text": "quota gone"}]},
    }
    with pytest.raises(InvalidResponseError, match="quota gone"):
        _run("keenable", lambda request: httpx.Response(200, json=envelope))


def test_keenable_garbage_raises_invalid_response() -> None:
    with pytest.raises(InvalidResponseError):
        _run("keenable", lambda request: httpx.Response(200, text="<html>not a protocol response</html>"))


def test_keenable_429_raises_quota() -> None:
    with pytest.raises(QuotaError):
        _run("keenable", lambda request: httpx.Response(429, text="rate limited"))


# --- registry -----------------------------------------------------------------


def test_registry_matches_contract() -> None:
    assert engines.FREE_ENGINE_NAMES == (
        "bing",
        "tavily_keyless",
        "anysearch",
        "youcom",
        "firecrawl",
        "keenable",
        "ddg_html",
        "searxng",
        "ddg_lite",
    )
    assert set(engines.FREE_ENGINES) == set(engines.FREE_ENGINE_NAMES)
    assert all(callable(engine) for engine in engines.FREE_ENGINES.values())
