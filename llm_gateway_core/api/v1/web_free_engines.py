"""Keyless web-search engines for the free-first search pool.

Each engine performs the HTTP exchange (plus engine-local retries/failover),
parses the payload and returns ``[{"url", "title", "snippet"}]`` items with an
optional ``published_at``; snippets are clipped to 300 characters. Relevance
gating, routing and cooldowns belong to the orchestrator, not here. Failures
are raised as the typed classes from ``web_engine_errors`` (raw httpx network
exceptions may propagate; the routing layer classifies them).

Semantics ported from dsh-free-search ``lib/engines/*.js``.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import re
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from selectolax.lexbor import LexborHTMLParser, LexborNode

from .web_engine_errors import (
    AuthError,
    BotWallError,
    InvalidResponseError,
    QuotaError,
    TransientError,
)

logger = logging.getLogger(__name__)

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)

BING_URL = "https://www.bing.com/search"
TAVILY_URL = "https://api.tavily.com/search"
DDG_HTML_URL = "https://html.duckduckgo.com/html/"
DDG_LITE_URL = "https://lite.duckduckgo.com/lite/"
ANYSEARCH_URL = "https://api.anysearch.com/v1/search"
YOUCOM_URL = "https://api.you.com/v1/agents/search"
FIRECRAWL_URL = "https://api.firecrawl.dev/v2/search"
KEENABLE_MCP_URL = "https://api.keenable.ai/mcp"

DEFAULT_SEARXNG_INSTANCES: tuple[str, ...] = (
    "https://opnxng.com",
    "https://priv.au",
    "https://searx.be",
    "https://searx.tiekoetter.com",
    "https://search.inetol.net",
    "https://paulgo.io",
)

HTML_TIMEOUT_SECONDS = 12.0
SEARXNG_TIMEOUT_SECONDS = 8.0
HTML_RETRY_ATTEMPTS = 3
HTML_RETRY_DELAY_SECONDS = 1.5
MIN_HTML_BYTES = 500
SNIPPET_LIMIT = 300

_ACCEPT_LANGUAGES = {
    "ru": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "en": "en-US,en;q=0.9",
    "zh": "zh-CN,zh;q=0.9",
}
_BOT_WALL_RE = re.compile(r"anomaly|captcha|unusual traffic|robot check", re.IGNORECASE)
_BING_CK_RE = re.compile(r"^https?://(?:www\.)?bing\.com/ck/a", re.IGNORECASE)
_UDDG_RE = re.compile(r"uddg=([^&]+)")
_DDG_DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}[T\d:.+-]*")
_FIRECRAWL_KEYLESS_REFUSAL_RE = re.compile(r"vpn|proxy|without an api key|api key from here", re.IGNORECASE)


def _accept_language(language: str) -> str:
    return _ACCEPT_LANGUAGES.get(str(language or "").strip().lower(), _ACCEPT_LANGUAGES["en"])


def _clean_text(text: str) -> str:
    return " ".join(text.split())


def _node_text(node: LexborNode | None) -> str:
    return _clean_text(node.text(separator=" ", strip=True)) if node is not None else ""


def _as_dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _result(url: str, title: str, snippet: str, published_at: str | None = None) -> dict[str, Any]:
    item: dict[str, Any] = {"url": url, "title": _clean_text(title), "snippet": _clean_text(snippet)[:SNIPPET_LIMIT]}
    if published_at:
        item["published_at"] = published_at
    return item


def _unique(items: list[dict[str, Any]], limit: int) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        url = item["url"]
        if not url or url in seen:
            continue
        seen.add(url)
        out.append(item)
        if len(out) >= limit:
            break
    return out


def _raise_for_http_status(response: httpx.Response, engine: str) -> None:
    status = response.status_code
    if status < 400:
        return
    if status in (401, 403):
        raise AuthError(f"{engine}: HTTP {status}")
    if status in (402, 429):
        raise QuotaError(f"{engine}: HTTP {status}")
    if status >= 500:
        raise TransientError(f"{engine}: HTTP {status}")
    raise InvalidResponseError(f"{engine}: HTTP {status}")


def _json_object(response: httpx.Response, engine: str) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError as exc:
        raise InvalidResponseError(f"{engine}: invalid JSON") from exc
    if not isinstance(data, dict):
        raise InvalidResponseError(f"{engine}: unexpected payload")
    return data


async def _fetch_html(
    client: httpx.AsyncClient,
    url: str,
    *,
    engine: str,
    accept_language: str,
    params: dict[str, str] | None = None,
) -> str:
    response = await client.get(
        url,
        params=params,
        headers={"User-Agent": USER_AGENT, "Accept-Language": accept_language},
        timeout=HTML_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    body = response.text
    if response.status_code == 202 or _BOT_WALL_RE.search(body[:4000]):
        raise BotWallError(f"{engine}: anti-bot challenge (HTTP {response.status_code})")
    _raise_for_http_status(response, engine)
    return body


async def _fetch_html_with_retry(
    client: httpx.AsyncClient,
    url: str,
    *,
    engine: str,
    accept_language: str,
    params: dict[str, str] | None = None,
) -> str:
    """Retry only network faults and sub-500-byte bodies; bot walls propagate at once."""
    last_error: Exception = TransientError(f"{engine}: fetch failed")
    for attempt in range(HTML_RETRY_ATTEMPTS):
        try:
            body = await _fetch_html(client, url, engine=engine, accept_language=accept_language, params=params)
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            last_error = exc
            logger.debug("%s: network error on attempt %d: %s", engine, attempt + 1, exc)
        else:
            if len(body) > MIN_HTML_BYTES:
                return body
            last_error = TransientError(f"{engine}: empty response ({len(body)} bytes)")
        if attempt < HTML_RETRY_ATTEMPTS - 1:
            await asyncio.sleep(HTML_RETRY_DELAY_SECONDS)
    raise last_error


def _extract_bing_url(href: str) -> str:
    """Unwrap bing.com/ck/a redirects: the real URL is base64url in the ``u`` param (``a1`` prefix)."""
    if not _BING_CK_RE.match(href):
        return href
    try:
        values = parse_qs(urlsplit(href).query).get("u") or []
        encoded = values[0] if values else ""
        if not encoded:
            return href
        payload = encoded[2:] if encoded.startswith("a1") else encoded
        payload += "=" * (-len(payload) % 4)
        decoded = base64.urlsafe_b64decode(payload).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return href
    return decoded if decoded.lower().startswith(("http://", "https://")) else href


def _extract_ddg_url(href: str | None) -> str | None:
    if not href:
        return None
    match = _UDDG_RE.search(href)
    if match:
        return unquote(match.group(1))
    if href.startswith("//"):
        return f"https:{href}"
    return href


async def search_bing(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    # No ``mkt`` param: some egress IPs get an empty SERP with it; Bing localizes via Accept-Language.
    body = await _fetch_html_with_retry(
        client, BING_URL, engine="bing", accept_language=_accept_language(language), params={"q": query}
    )
    sources: list[dict[str, Any]] = []
    for block in LexborHTMLParser(body).css("li.b_algo"):
        link = block.css_first('a[href^="http"]')
        href = (link.attributes.get("href") or "") if link is not None else ""
        if not href:
            continue
        heading = block.css_first("h2")
        title_node = heading.css_first("a") if heading is not None else None
        sources.append(_result(_extract_bing_url(href), _node_text(title_node), _node_text(block.css_first("p"))))
    # An empty SERP with valid markup is a real "0 results"; never retried here.
    return _unique(sources, max_results)


async def search_tavily_keyless(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    # No follow_redirects: the tavily.js reference uses redirect:"error".
    response = await client.post(
        TAVILY_URL,
        json={"query": query, "max_results": min(max_results, 20), "search_depth": "basic"},
        headers={"Accept": "application/json", "x-tavily-access-mode": "keyless"},
        timeout=20.0,
    )
    _raise_for_http_status(response, "tavily_keyless")
    data = _json_object(response, "tavily_keyless")
    results = data.get("results")
    if not isinstance(results, list):
        results = []
    sources = [
        _result(str(hit["url"]), str(hit.get("title") or ""), str(hit.get("content") or ""))
        for hit in results
        if isinstance(hit, dict) and hit.get("url")
    ]
    return _unique(sources, max_results)


async def search_ddg_html(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    body = await _fetch_html_with_retry(
        client,
        DDG_HTML_URL,
        engine="ddg_html",
        accept_language=_accept_language(language),
        params={"q": query, "adlt": "-1"},
    )
    sources: list[dict[str, Any]] = []
    for block in LexborHTMLParser(body).css("div.results_links"):
        link = block.css_first("a.result__a")
        if link is None:
            continue
        url = _extract_ddg_url(link.attributes.get("href"))
        if not url:
            continue
        published_at = None
        for span in block.css("span"):
            text = span.text(strip=True)
            if _DDG_DATE_RE.fullmatch(text):
                published_at = text
                break
        sources.append(_result(url, _node_text(link), _node_text(block.css_first(".result__snippet")), published_at))
    return _unique(sources, max_results)


async def search_ddg_lite(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    body = await _fetch_html_with_retry(
        client,
        DDG_LITE_URL,
        engine="ddg_lite",
        accept_language=_accept_language(language),
        params={"q": query, "adlt": "-1"},
    )
    tree = LexborHTMLParser(body)
    links = tree.css("a.result-link")
    snippets = tree.css(".result-snippet")
    sources: list[dict[str, Any]] = []
    for index, link in enumerate(links):
        url = _extract_ddg_url(link.attributes.get("href"))
        if not url:
            continue
        snippet = _node_text(snippets[index]) if index < len(snippets) else ""
        sources.append(_result(url, _node_text(link), snippet))
    return _unique(sources, max_results)


async def search_searxng(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    language: str = "en",
    instances: list[str] | None = None,
    bearer_token: str | None = None,
) -> list[dict[str, Any]]:
    """Fail over SearXNG instances until one returns results.

    ``instances=None`` selects the built-in public list; an explicit empty list means
    SearXNG is switched off and fails fast with ``TransientError`` before any HTTP call.
    """
    if instances is None:
        bases = list(DEFAULT_SEARXNG_INSTANCES)
    elif not instances:
        raise TransientError("no instances configured")
    else:
        bases = instances
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json",
        "Accept-Language": _accept_language(language),
    }
    if bearer_token:
        headers["Authorization"] = f"Bearer {bearer_token}"
    failures: list[tuple[str, bool]] = []  # (reason, is_bot_check)
    for base in bases:
        label = base.rstrip("/")
        try:
            response = await client.get(
                f"{label}/search",
                params={"q": query, "format": "json"},
                headers=headers,
                timeout=SEARXNG_TIMEOUT_SECONDS,
                follow_redirects=True,
            )
        except httpx.HTTPError as exc:
            failures.append((f"{label}: {exc}", False))
            continue
        status = response.status_code
        if status in (202, 403, 429):
            failures.append((f"{label}: HTTP {status}", True))
            continue
        if status >= 400:
            failures.append((f"{label}: HTTP {status}", False))
            continue
        try:
            data = response.json()
        except ValueError:
            is_bot = bool(_BOT_WALL_RE.search(response.text[:4000]))
            failures.append((f"{label}: invalid JSON", is_bot))
            continue
        results = data.get("results") if isinstance(data, dict) else None
        if not isinstance(results, list):
            failures.append((f"{label}: invalid JSON", False))
            continue
        sources = [
            _result(str(hit["url"]), str(hit.get("title") or ""), str(hit.get("content") or ""))
            for hit in results
            if isinstance(hit, dict) and hit.get("url")
        ]
        if sources:
            return _unique(sources, max_results)
        failures.append((f"{label}: 0 results", False))
    detail = ", ".join(reason for reason, _ in failures)[:SNIPPET_LIMIT]
    message = f"all SearXNG instances failed: {detail}"
    if failures and all(is_bot for _, is_bot in failures):
        raise BotWallError(message)
    raise TransientError(message)


async def search_anysearch(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    response = await client.post(
        ANYSEARCH_URL,
        json={"query": query, "max_results": max_results},
        timeout=HTML_TIMEOUT_SECONDS,
        follow_redirects=True,
    )
    _raise_for_http_status(response, "anysearch")
    data = _json_object(response, "anysearch")
    if data.get("code") != 0:
        detail = str(data.get("message") or data.get("code") or "unknown")
        raise InvalidResponseError(f"anysearch: code={data.get('code')} {detail}")
    results = _as_dict(data.get("data")).get("results")
    if not isinstance(results, list):
        results = []
    sources = [
        _result(str(hit["url"]), str(hit.get("title") or ""), str(hit.get("snippet") or ""))
        for hit in results
        if isinstance(hit, dict) and hit.get("url")
    ]
    return _unique(sources, max_results)


async def search_youcom(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    response = await client.get(
        YOUCOM_URL,
        params={"query": query, "count": str(min(max_results, 20))},
        headers={"Accept": "application/json", "User-Agent": USER_AGENT},
        timeout=15.0,
    )
    _raise_for_http_status(response, "youcom")
    data = _json_object(response, "youcom")
    hits = _as_dict(data.get("results")).get("web")
    if not isinstance(hits, list):
        hits = []
    sources: list[dict[str, Any]] = []
    for hit in hits:
        if not isinstance(hit, dict) or not hit.get("url"):
            continue
        snippets = hit.get("snippets")
        if isinstance(snippets, list):
            snippet = " ".join(part for part in snippets if isinstance(part, str) and part)
        else:
            snippet = str(hit.get("description") or "")
        sources.append(_result(str(hit["url"]), str(hit.get("title") or ""), snippet))
    return _unique(sources, max_results)


async def search_firecrawl(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    response = await client.post(
        FIRECRAWL_URL,
        json={"query": query, "limit": min(max(max_results, 1), 10)},
        headers={"Accept": "application/json"},
        timeout=20.0,
    )
    if response.status_code == 403 and _FIRECRAWL_KEYLESS_REFUSAL_RE.search(response.text):
        raise BotWallError("firecrawl: keyless tier refused from this egress (VPN/proxy)")
    _raise_for_http_status(response, "firecrawl")
    data = _json_object(response, "firecrawl")
    if data.get("success") is False:
        raise InvalidResponseError(f"firecrawl: {str(data.get('error') or 'request failed')[:200]}")
    hits = _as_dict(data.get("data")).get("web")
    if not isinstance(hits, list):
        hits = []
    sources = [
        _result(str(hit["url"]), str(hit.get("title") or ""), str(hit.get("description") or ""))
        for hit in hits
        if isinstance(hit, dict) and hit.get("url")
    ]
    return _unique(sources, max_results)


def _try_json_object(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _parse_mcp_response_text(raw: str) -> dict[str, Any] | None:
    """Parse an MCP body as plain JSON or as SSE ``data:`` frames (port of mcp_adapter.js)."""
    if not raw:
        return None
    direct = _try_json_object(raw)
    if direct is not None:
        return direct
    payloads: list[str] = []
    for event in re.split(r"\r?\n\r?\n", raw):
        data_lines = [line[5:].removeprefix(" ") for line in re.split(r"\r?\n", event) if line.startswith("data:")]
        if data_lines:
            payloads.append("\n".join(data_lines))
    if not payloads:
        payloads = [line[5:].removeprefix(" ") for line in re.split(r"\r?\n", raw) if line.startswith("data:")]
    fallback: dict[str, Any] | None = None
    for payload in payloads:
        parsed = _try_json_object(payload)
        if parsed is None:
            continue
        if "result" in parsed or "error" in parsed:
            return parsed
        if fallback is None:
            fallback = parsed
    return fallback


def _mcp_content_text(result: dict[str, Any]) -> str:
    content = result.get("content")
    if not isinstance(content, list):
        return ""
    for block in content:
        if isinstance(block, dict) and block.get("type") == "text" and isinstance(block.get("text"), str):
            return block["text"]
    return ""


async def search_keenable(
    client: httpx.AsyncClient, query: str, max_results: int, *, language: str = "en"
) -> list[dict[str, Any]]:
    response = await client.post(
        KEENABLE_MCP_URL,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "search", "arguments": {"query": query}},
        },
        headers={"Accept": "application/json, text/event-stream"},
        timeout=25.0,
    )
    _raise_for_http_status(response, "keenable")
    envelope = _parse_mcp_response_text(response.text)
    if envelope is None:
        raise InvalidResponseError("keenable: response is not valid JSON or SSE")
    error = envelope.get("error")
    if error:
        raise InvalidResponseError(f"keenable MCP error: {_as_dict(error).get('message') or 'unknown'}")
    result = envelope.get("result")
    if not isinstance(result, dict):
        raise InvalidResponseError("keenable: MCP result missing or malformed")
    text = _mcp_content_text(result)
    if result.get("isError"):
        raise InvalidResponseError(f"keenable MCP tool error: {text[:200]}")
    payload = _try_json_object(text)
    if payload is None:
        raise InvalidResponseError("keenable: tool text is not JSON")
    results = payload.get("results")
    if not isinstance(results, list):
        raise InvalidResponseError("keenable: tool JSON has no results list")
    sources: list[dict[str, Any]] = []
    for hit in results:
        if not isinstance(hit, dict) or not hit.get("url"):
            continue
        snippet = str(hit.get("snippet") or hit.get("description") or "")
        published = hit.get("published_at")
        sources.append(
            _result(str(hit["url"]), str(hit.get("title") or ""), snippet, str(published) if published else None)
        )
    return _unique(sources, max_results)


FreeEngine = Callable[..., Awaitable[list[dict[str, Any]]]]

FREE_ENGINE_NAMES: tuple[str, ...] = (
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

FREE_ENGINES: dict[str, FreeEngine] = {
    "bing": search_bing,
    "tavily_keyless": search_tavily_keyless,
    "anysearch": search_anysearch,
    "youcom": search_youcom,
    "firecrawl": search_firecrawl,
    "keenable": search_keenable,
    "ddg_html": search_ddg_html,
    "searxng": search_searxng,
    "ddg_lite": search_ddg_lite,
}
