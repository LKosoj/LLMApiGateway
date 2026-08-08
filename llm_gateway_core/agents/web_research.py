from __future__ import annotations

import asyncio
import logging
import re
import ssl
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from html import unescape
from html.parser import HTMLParser
from io import BytesIO
import urllib.parse
from typing import Any, cast

from fastapi import HTTPException
import httpx

from ..utils.api_keys import has_api_key, select_next_api_key
from ..utils.zai_mcp import detect_zai_search_location, zai_mcp_tool_call

logger = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/91.0.4472.124 Safari/537.36"
    )
}

LANG_QUERIES_PROMPTS = {
    "ru": {
        "prompt": (
            "Сформулируй {n} коротких поисковых запроса (ключевые фразы, не длиннее 5-6 слов) "
            "на русском языке по теме: {query}. Не используй номера, не добавляй лишних слов, "
            "только сами поисковые фразы."
        ),
        "system": "Ты — эксперт по поисковым системам. Отвечай только списком поисковых фраз на русском языке.",
    },
    "en": {
        "prompt": (
            "Generate {n} short search queries (keywords, no more than 5-6 words each) "
            "in English for the topic: {query}. No numbering, just the queries."
        ),
        "system": "You are a search engine expert. Reply with a list of short search queries in English only.",
    },
    "zh": {
        "prompt": "请用中文为主题\"{query}\"生成{n}个简短的搜索引擎关键词（每个不超过6个字），不要编号，只列出关键词。",
        "system": "你是一名搜索引擎专家。只用中文列出搜索关键词，每行一个。",
    },
}


_YOUTUBE_URL_RE = re.compile(
    r"(?:https?://)?(?:www\.|m\.)?(?:youtube\.com/watch\?.*?v=|youtu\.be/)([\w-]{11})"
)
_HTML_TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.IGNORECASE | re.DOTALL)
_HTML_H1_RE = re.compile(r"<h1[^>]*>(.*?)</h1>", re.IGNORECASE | re.DOTALL)
_HTML_TAG_RE = re.compile(r"<[^>]+>")


def _extract_youtube_video_id(url: str) -> str | None:
    m = _YOUTUBE_URL_RE.search(url)
    return m.group(1) if m else None


_SELECTOLAX_NOISE_TAGS = (
    "script",
    "style",
    "iframe",
    "noscript",
    "nav",
    "footer",
    "header",
    "aside",
    "form",
    "button",
)


def _extract_text_with_selectolax(html_text: str) -> str:
    """Strip noise tags and pull main text via selectolax. "" on failure."""
    try:
        from selectolax.parser import HTMLParser as SelectolaxParser
    except ImportError:
        return ""
    try:
        tree = SelectolaxParser(html_text)
        for tag in _SELECTOLAX_NOISE_TAGS:
            for node in tree.css(tag):
                node.decompose()
        root = tree.body if tree.body is not None else tree.root
        if root is None:
            return ""
        raw_text = root.text(separator="\n", strip=False)
    except Exception as exc:
        logger.warning("selectolax extraction failed: %s", exc)
        return ""
    return "\n".join(line.strip() for line in raw_text.splitlines() if line.strip())


class _HTMLTextExtractor(HTMLParser):
    BLOCK_TAGS = {
        "br",
        "p",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "li",
        "tr",
        "td",
        "th",
        "div",
        "section",
        "article",
    }
    SKIP_TAGS = {"script", "style", "iframe", "noscript", "nav", "footer", "header", "aside", "form", "button"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self.parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP_TAGS:
            self._skip_depth += 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1
            return
        if tag in self.BLOCK_TAGS:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if self._skip_depth > 0:
            return
        text = data.strip()
        if text:
            self.parts.append(text)


class WebResearchClient:
    JINA_SEARCH_URL = "https://s.jina.ai"
    JINA_READER_URL = "https://r.jina.ai"

    def __init__(
        self,
        *,
        research_model: str | None = None,
        text_completion: Callable[[list[dict[str, str]], float, int], Awaitable[str]] | None = None,
        tavily_api_key: str | None = None,
        jina_api_key: str | None = None,
        zai_api_key: str | None = None,
        proxy_url: str | None = None,
        http_client: httpx.AsyncClient | None = None,
        temperature: float = 0.2,
    ) -> None:
        self._research_model = research_model
        self._text_completion = text_completion
        self._http_client = http_client
        self._temperature = temperature
        self._tavily_api_key = tavily_api_key
        self._jina_api_key = jina_api_key
        self._zai_api_key = zai_api_key
        self._proxy_url = proxy_url

    @asynccontextmanager
    async def _httpx_client(
        self, **client_kwargs: Any
    ) -> AsyncIterator[httpx.AsyncClient]:
        if self._http_client is not None and not client_kwargs:
            yield self._http_client
            return
        async with httpx.AsyncClient(**client_kwargs) as client:
            yield client

    def _get_jina_api_key(self) -> str | None:
        return select_next_api_key(self._jina_api_key)

    def _get_tavily_api_key(self) -> str | None:
        return select_next_api_key(self._tavily_api_key)

    def _get_zai_api_key(self) -> str | None:
        return select_next_api_key(self._zai_api_key)

    def _get_proxy_url(self) -> str | None:
        return self._proxy_url

    def _get_model(self, big: bool = False) -> str:
        return self._research_model or ""

    async def _call_openai_for_queries(self, user_prompt: str, system_prompt: str | None = None) -> list[str]:
        try:
            if self._text_completion is None:
                raise RuntimeError("LLM completion callback is not configured")

            messages: list[dict[str, str]] = []
            if system_prompt:
                messages.append({"role": "system", "content": system_prompt})
            messages.append({"role": "user", "content": user_prompt})

            response_text = (await self._text_completion(messages, 0.7, 300)).strip()
            if not response_text:
                return []

            queries = [
                line.strip().lstrip("0123456789.- ").strip()
                for line in response_text.split("\n")
                if line.strip()
            ]
            return queries[:5]
        except HTTPException:
            raise
        except Exception as exc:
            logger.error("Ошибка генерации поисковых запросов: %s", exc)
            return []

    async def _jina_search(self, query: str, max_results: int = 5) -> list[str]:
        jina_api_key = self._get_jina_api_key()
        if not jina_api_key:
            logger.error("Jina AI API ключ не настроен")
            return []

        try:
            url = f"{self.JINA_SEARCH_URL}/?q={urllib.parse.quote(query)}"
            headers = {
                "Accept": "application/json",
                "Authorization": f"Bearer {jina_api_key}",
                "X-Respond-With": "no-content",
            }

            async with self._httpx_client() as client:
                response = await client.get(url, headers=headers, timeout=30.0)
                response.raise_for_status()
                data = response.json()

                if data.get("code") != 200:
                    logger.error("Jina AI API вернул ошибку: %s", data.get("status"))
                    return []

                items = data.get("data", [])[:max_results]
                links = [item.get("url", "") for item in items if item.get("url")]

                logger.info("Jina AI поиск '%s': найдено %s ссылок", query, len(links))
                return links
        except Exception as exc:
            logger.error("Ошибка поиска через Jina AI для запроса '%s': %s", query, exc)
            return []

    async def _tavily_search(self, query: str, max_results: int = 5) -> list[str]:
        tavily_key = self._get_tavily_api_key()
        if not tavily_key:
            return []

        try:
            async with self._httpx_client() as client:
                response = await client.post(
                    "https://api.tavily.com/search",
                    json={"api_key": tavily_key, "query": query, "max_results": max_results},
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json() or {}
                items = data.get("results", [])[:max_results]
                links = [
                    item.get("url") or item.get("link", "")
                    for item in items
                    if item.get("url") or item.get("link")
                ]
                logger.info("Tavily поиск '%s': найдено %s ссылок", query, len(links))
                return links
        except Exception as exc:
            logger.warning("Ошибка поиска через Tavily для запроса '%s': %s", query, exc)
            return []

    async def _zai_search(self, query: str, max_results: int = 5) -> list[str]:
        zai_key = self._get_zai_api_key()
        if not zai_key:
            return []

        try:
            async with self._httpx_client() as client:
                payload = await zai_mcp_tool_call(
                    client,
                    api_key=zai_key,
                    server_path="web_search_prime",
                    tool_name="web_search_prime",
                    arguments={
                        "search_query": query,
                        "location": detect_zai_search_location(query),
                    },
                    timeout=60.0,
                )
            items = payload if isinstance(payload, list) else []
            links: list[str] = []
            for item in items[:max_results]:
                if not isinstance(item, dict):
                    continue
                link = item.get("link") or item.get("url")
                if link:
                    links.append(link)
            logger.info("Z.AI поиск '%s': найдено %s ссылок", query, len(links))
            return links
        except Exception as exc:
            logger.warning("Ошибка поиска через Z.AI для запроса '%s': %s", query, exc)
            return []

    async def _proxy_search(self, query: str, max_results: int = 5) -> list[str]:
        proxy_url = self._get_proxy_url()
        if not proxy_url:
            return []

        try:
            async with self._httpx_client() as client:
                response = await client.get(
                    f"{proxy_url}/zai/search",
                    params={"q": query},
                    timeout=30.0,
                )
                response.raise_for_status()
                data = response.json() or {}
                items = data.get("search_result", [])[:max_results]
                links = [
                    item.get("url") or item.get("link", "")
                    for item in items
                    if item.get("url") or item.get("link")
                ]
                logger.info("Proxy поиск '%s': найдено %s ссылок", query, len(links))
                return links
        except Exception as exc:
            logger.warning("Ошибка поиска через Proxy для запроса '%s': %s", query, exc)
            return []

    async def _search(self, query: str, max_results: int = 5) -> list[str]:
        search_providers: list[tuple[str, Callable[[str, int], Awaitable[list[str]]]]] = []
        if self._get_proxy_url():
            search_providers.append(("Proxy", self._proxy_search))
        if self._get_tavily_api_key():
            search_providers.append(("Tavily", self._tavily_search))
        if self._get_jina_api_key():
            search_providers.append(("Jina", self._jina_search))
        if self._get_zai_api_key():
            search_providers.append(("Z.AI", self._zai_search))

        for name, func in search_providers:
            try:
                results = await func(query, max_results)
                if results:
                    return cast(list[str], results)
                logger.warning("Поиск через %s вернул пустые результаты для '%s'", name, query)
            except Exception as exc:
                logger.warning("Поиск через %s завершился ошибкой для '%s': %s", name, query, exc)

        logger.error("Все поисковые провайдеры не дали результатов для '%s'", query)
        return []

    async def _download_content(self, url: str) -> dict[str, str]:
        if "(" in url:
            url = url.split("(")[1]
        url = url.strip(")").strip("(").strip('"').strip("'").strip()

        if not url.startswith("http"):
            logger.warning("Некорректный URL: %s", url)
            return {"url": url, "title": "", "content": ""}

        video_id = _extract_youtube_video_id(url)
        if video_id:
            # Без субтитров у YouTube-ссылки нет пригодного текста: возвращать
            # текст-заглушку значило бы отдать её агенту как настоящий контент.
            content, yt_title = await self._extract_youtube_transcript(video_id)
            return {"url": url, "title": yt_title, "content": content}

        title = ""

        try:
            enhanced_headers = {
                **HEADERS,
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
                "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8,en;q=0.7",
                "Accept-Encoding": "gzip, deflate, br",
                "DNT": "1",
                "Connection": "keep-alive",
                "Upgrade-Insecure-Requests": "1",
            }

            async with self._httpx_client(follow_redirects=True) as client:
                response = await client.get(url, headers=enhanced_headers, timeout=20.0)
                response.raise_for_status()

                content_type = response.headers.get("Content-Type", "").lower()
                if url.lower().endswith(".pdf") or "application/pdf" in content_type:
                    logger.info("Обнаружен PDF: %s", url)
                    try:
                        pdf_content, pdf_title = await self._extract_pdf_content(response.content, url)
                        return {"url": url, "title": pdf_title, "content": pdf_content}
                    except Exception as exc:
                        logger.warning("Ошибка извлечения PDF %s: %s", url, exc)
                        return {
                            "url": url,
                            "title": url.split("/")[-1] or "PDF-документ",
                            "content": "PDF-документ (ошибка извлечения содержимого)",
                        }

                html_content = response.text
                title = self._extract_title(html_content)

                try:
                    import trafilatura  # type: ignore[import-untyped]

                    clean_content = trafilatura.extract(
                        html_content,
                        include_formatting=True,
                        include_links=True,
                        include_tables=True,
                        include_images=True,
                        include_comments=False,
                        output_format="markdown",
                    )
                    if clean_content and clean_content.strip():
                        clean_content = self._clean_extra_spaces(clean_content)
                        logger.info("Успешно загружен через trafilatura: %s", url)
                        return {"url": url, "title": title, "content": clean_content}
                except ImportError:
                    logger.debug("trafilatura не установлен, используем BeautifulSoup")
                except Exception as exc:
                    logger.warning("Ошибка trafilatura для %s: %s", url, exc)

                selectolax_text = await asyncio.to_thread(_extract_text_with_selectolax, html_content)
                if selectolax_text:
                    logger.info("Успешно загружен через selectolax: %s", url)
                    return {"url": url, "title": title, "content": selectolax_text}

                clean_content = await asyncio.to_thread(self._clean_html_content, html_content)
                if clean_content and clean_content.strip():
                    logger.info("Успешно загружен через html.parser: %s", url)
                    return {"url": url, "title": title, "content": clean_content}

                logger.warning("Пустой контент после extraction для %s, пробуем API readers", url)

        except httpx.TimeoutException:
            logger.warning("Тайм-аут при загрузке URL: %s", url)
        except httpx.HTTPError as exc:
            logger.warning("HTTP ошибка при загрузке URL: %s, ошибка: %s", url, exc)
        except Exception as exc:
            logger.warning("Непредвиденная ошибка при загрузке URL: %s, ошибка: %s", url, exc)

        proxy_url = self._get_proxy_url()
        if proxy_url:
            try:
                async with self._httpx_client() as client:
                    resp = await client.get(
                        f"{proxy_url}/zai/read",
                        params={"url": url},
                        timeout=30.0,
                    )
                    resp.raise_for_status()
                    data = (resp.json() or {}).get("reader_result") or {}
                    api_content = data.get("content")
                    if api_content and api_content.strip():
                        api_title = data.get("title") or title
                        logger.info("Успешно загружен через Proxy Reader (fallback): %s", url)
                        return {"url": url, "title": api_title, "content": api_content}
                    logger.warning("Proxy Reader вернул пустой контент для %s", url)
            except Exception as exc:
                logger.warning("Ошибка Proxy Reader fallback для %s: %s", url, exc)

        tavily_key = self._get_tavily_api_key()
        if tavily_key:
            try:
                tavily_result = await self._tavily_extract(tavily_key, url)
                if tavily_result:
                    api_content = tavily_result.get("content") or ""
                    if api_content.strip():
                        api_title = tavily_result.get("title") or title
                        logger.info("Успешно загружен через Tavily Extract (fallback): %s", url)
                        return {"url": url, "title": api_title, "content": api_content}
                    logger.warning("Tavily Extract вернул пустой контент для %s", url)
            except Exception as exc:
                logger.warning("Ошибка Tavily Extract fallback для %s: %s", url, exc)

        jina_key = self._get_jina_api_key()
        if jina_key:
            try:
                content, jina_title = await self._get_clean_text_jina(url)
                if content and content.strip():
                    logger.info("Успешно загружен через Jina Reader (fallback): %s", url)
                    return {"url": url, "title": jina_title or title, "content": content}
                logger.warning("Jina Reader вернул пустой контент для %s", url)
            except Exception as exc:
                logger.warning("Ошибка Jina Reader fallback для %s: %s", url, exc)

        zai_key = self._get_zai_api_key()
        if zai_key:
            try:
                async with self._httpx_client() as client:
                    payload = await zai_mcp_tool_call(
                        client,
                        api_key=zai_key,
                        server_path="web_reader",
                        tool_name="webReader",
                        arguments={
                            "url": url,
                            "return_format": "markdown",
                            "retain_images": False,
                            "timeout": 20,
                        },
                        timeout=60.0,
                    )
                data = payload if isinstance(payload, dict) else {}
                api_content = data.get("content")
                if api_content and str(api_content).strip():
                    api_title = data.get("title") or title
                    logger.info("Успешно загружен через Z.AI Reader (fallback): %s", url)
                    return {"url": url, "title": api_title, "content": api_content}
                logger.warning("Z.AI Reader вернул пустой контент для %s", url)
            except Exception as exc:
                logger.warning("Ошибка Z.AI Reader fallback для %s: %s", url, exc)

        return {"url": url, "title": title, "content": ""}

    async def _extract_youtube_transcript(self, video_id: str) -> tuple[str, str]:
        from youtube_transcript_api._errors import (
            NoTranscriptFound,
            TranscriptsDisabled,
        )

        from ..utils.youtube_transcript import (
            build_youtube_transcript_api,
            run_queued_transcript_call,
        )

        def _fetch() -> tuple[str, str]:
            # Built inside the queued call so the proxy/direct alternation advances
            # once per actual download rather than once per queued caller.
            api = build_youtube_transcript_api()
            transcript_list = api.list(video_id)

            lang_priority = ["ru", "en", "zh-Hans", "zh-Hant"]
            try:
                transcript = transcript_list.find_transcript(lang_priority)
            except NoTranscriptFound:
                transcript = next(iter(transcript_list))

            fetched = transcript.fetch()
            segments = list(fetched)
            text_parts: list[str] = []
            for seg in segments:
                snippet = seg.get("text") if isinstance(seg, dict) else getattr(seg, "text", str(seg))
                if snippet:
                    text_parts.append(snippet.strip())

            content = " ".join(text_parts)
            video_title = f"YouTube: {video_id} ({transcript.language})"
            return content, video_title

        try:
            content, video_title = await run_queued_transcript_call(_fetch)
            if content.strip():
                logger.info("Успешно извлечён транскрипт YouTube %s (%s символов)", video_id, len(content))
                return content, video_title
            raise RuntimeError("Пустой транскрипт")
        except (TranscriptsDisabled, NoTranscriptFound) as exc:
            logger.warning("Субтитры недоступны для YouTube %s: %s", video_id, exc)
            raise
        except Exception as exc:
            logger.warning("Ошибка извлечения транскрипта YouTube %s: %s", video_id, exc)
            raise

    async def _extract_pdf_content(self, pdf_bytes: bytes, url: str) -> tuple[str, str]:
        try:
            from pdfminer.high_level import extract_text

            pdf_stream = BytesIO(pdf_bytes)
            text = await asyncio.to_thread(extract_text, pdf_stream)

            if text and text.strip():
                clean_text = self._clean_extra_spaces(text)
                title = url.split("/")[-1] or "PDF-документ"
                logger.info("Успешно извлечен текст из PDF: %s (%s символов)", url, len(clean_text))
                return clean_text, title

            logger.warning("PDF пустой или не содержит текста: %s", url)
            return "PDF-документ не содержит извлекаемого текста", url.split("/")[-1] or "PDF-документ"
        except ImportError:
            logger.error("pdfminer не установлен. Установите: pip install pdfminer.six")
            return "PDF-документ (pdfminer не установлен)", url.split("/")[-1] or "PDF-документ"
        except Exception as exc:
            logger.error("Ошибка извлечения текста из PDF %s: %s", url, exc)
            raise

    @staticmethod
    def _is_ssl_error(exc: Exception) -> bool:
        if isinstance(exc, ssl.SSLError):
            return True
        if isinstance(exc, httpx.ConnectError) and isinstance(exc.__cause__, ssl.SSLError):
            return True
        return False

    async def _tavily_extract(self, tavily_key: str, url: str) -> dict[str, str] | None:
        """Call Tavily Extract API.

        TLS verification is enforced. SSL errors are NOT retried with verify=False:
        отключение проверки сертификата открыло бы MITM (потенциальная утечка
        Authorization-заголовка с tavily_key).
        """
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {tavily_key}",
        }
        payload = {"urls": [url]}

        try:
            async with self._httpx_client() as client:
                resp = await client.post(
                    "https://api.tavily.com/extract",
                    headers=headers,
                    json=payload,
                    timeout=30.0,
                )
                resp.raise_for_status()
                data = resp.json() or {}
                results = data.get("results") or data.get("data") or []
                if isinstance(results, dict):
                    results = [results]
                item = results[0] if results else {}
                return {
                    "content": item.get("content") or item.get("raw_content") or "",
                    "title": item.get("title") or "",
                }
        except (ssl.SSLError, httpx.ConnectError) as exc:
            if self._is_ssl_error(exc):
                logger.error(
                    "SSL verification failed for Tavily Extract %s: %s; aborting (no insecure retry)",
                    url,
                    exc,
                )
            raise

    async def _get_clean_text_jina(self, url: str) -> tuple[str, str]:
        jina_api_key = self._get_jina_api_key()
        if not jina_api_key:
            raise RuntimeError("Jina API ключ не настроен")

        headers = {
            "Authorization": f"Bearer {jina_api_key}",
            "Content-Type": "application/json",
            "X-Base": "final",
            "X-Engine": "browser",
            "X-Timeout": "20000",
            "X-No-Gfm": "true",
        }
        data = {"url": url}

        async with self._httpx_client() as client:
            response = await client.post(self.JINA_READER_URL, headers=headers, json=data, timeout=30.0)
            response.raise_for_status()

            text_response = response.text
            lines = text_response.splitlines()

            extracted_title = ""
            markdown_content_lines: list[str] = []
            markdown_section_started = False

            for line in lines:
                if line.startswith("Title:"):
                    if not markdown_section_started:
                        extracted_title = line.replace("Title:", "").strip()
                elif line.startswith("URL Source:"):
                    continue
                elif line.startswith("Markdown Content:"):
                    markdown_section_started = True
                    content_on_label_line = line.replace("Markdown Content:", "").strip()
                    if content_on_label_line:
                        markdown_content_lines.append(content_on_label_line)
                elif markdown_section_started:
                    markdown_content_lines.append(line)

            markdown_content = "\n".join(markdown_content_lines).strip() if markdown_content_lines else ""

            if extracted_title and markdown_content:
                return markdown_content, extracted_title
            raise RuntimeError("Не удалось извлечь контент через Jina API")

    def _extract_title(self, html_content: str) -> str:
        try:
            title_match = _HTML_TITLE_RE.search(html_content)
            if title_match:
                title = _HTML_TAG_RE.sub("", title_match.group(1))
                title = unescape(title).strip()
                if title:
                    return title

            h1_match = _HTML_H1_RE.search(html_content)
            if h1_match:
                title = _HTML_TAG_RE.sub("", h1_match.group(1))
                title = unescape(title).strip()
                if title:
                    return title
        except Exception as exc:
            logger.warning("Ошибка извлечения заголовка: %s", exc)

        return "Без заголовка"

    def _clean_extra_spaces(self, text: str) -> str:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        return "\n".join(lines)

    def _clean_html_content(self, html_content: str) -> str:
        try:
            parser = _HTMLTextExtractor()
            parser.feed(html_content)
            return self._clean_extra_spaces("\n".join(parser.parts))
        except Exception as exc:
            logger.error("Ошибка при очистке HTML: %s", exc)
            return html_content

    async def _generate_search_queries_lang(self, user_query: str, lang: str, n: int) -> list[str]:
        if lang not in LANG_QUERIES_PROMPTS:
            lang = "en"

        prompt_data = LANG_QUERIES_PROMPTS[lang]
        prompt = prompt_data["prompt"].format(query=user_query, n=n)
        system_prompt = prompt_data["system"]

        queries = await self._call_openai_for_queries(prompt, system_prompt)
        return queries[:n]

    async def generate_search_queries(self, user_query: str, lang: str, n: int) -> list[str]:
        return await self._generate_search_queries_lang(user_query, lang, n)

    async def _find_articles_for_language(
        self,
        user_query: str,
        lang: str,
        num_queries: int,
        max_results: int,
    ) -> list[str]:
        queries = await self._generate_search_queries_lang(user_query, lang, num_queries)
        logger.info("Поисковые запросы %s: %s", lang.upper(), queries)

        search_tasks = []
        for query in queries:
            task = self._search(query, max_results=max_results // num_queries + 1)
            search_tasks.append(task)

        search_results = await asyncio.gather(*search_tasks, return_exceptions=True)

        all_results: list[list[str]] = []
        for result in search_results:
            if isinstance(result, list):
                all_results.append(result)
            else:
                logger.warning("Ошибка в поиске: %s", result)
                all_results.append([])

        return self._round_robin_merge(all_results)

    def _round_robin_merge(self, lists: list[list[str]]) -> list[str]:
        merged: list[str] = []
        seen: set[str] = set()
        maxlen = max(len(lst) for lst in lists) if lists else 0

        for i in range(maxlen):
            for lst in lists:
                if i < len(lst):
                    link = lst[i]
                    if link and link not in seen:
                        merged.append(link)
                        seen.add(link)

        return merged

    def _detect_target_language(self, text: str) -> str:
        if any("\u4e00" <= ch <= "\u9fff" for ch in text):
            return "zh"
        if any("а" <= ch.lower() <= "я" for ch in text):
            return "ru"
        return "en"

    async def _extract_relevant_from_article_with_llm(self, user_query: str, article: dict[str, str]) -> str:
        content = (article.get("content") or "").strip()
        if not content:
            return ""

        system_message = (
            "Ты эксперт-редактор для подготовки seed-контента книги. "
            "Извлекай только релевантные факты по запросу пользователя."
        )

        prompt = (
            "Проанализируй ОДНУ статью и верни только релевантные запросу факты.\n"
            f"Запрос пользователя: {user_query}\n"
            f"URL: {article.get('url', '')}\n"
            f"Заголовок: {article.get('title', '')}\n\n"
            "Текст статьи:\n"
            f"{content}\n\n"
            "Формат ответа:\n"
            "- Если релевантного нет, верни только: IRRELEVANT\n"
            "- Иначе 3-10 коротких bullet-пунктов с фактами и идеями, каждый пункт со ссылкой на URL.\n"
            f"- Язык ответа: {self._detect_target_language(user_query)}"
        )

        if self._text_completion is None:
            raise RuntimeError("LLM completion callback is not configured")

        response = (
            await self._text_completion(
                [
                    {"role": "system", "content": system_message},
                    {"role": "user", "content": prompt},
                ],
                0.2,
                1000,
            )
        ).strip()
        if not response or response.upper() == "IRRELEVANT":
            return ""
        return response

    async def _analyze_content_with_llm(self, user_query: str, articles: list[dict[str, str]]) -> str:
        try:
            valid_articles = [article for article in articles if article.get("content", "").strip()]
            if not valid_articles:
                return "Не удалось скачать содержимое статей для анализа."

            tasks = [
                self._extract_relevant_from_article_with_llm(user_query, article)
                for article in valid_articles
            ]
            chunks = await asyncio.gather(*tasks, return_exceptions=True)

            collected: list[str] = []
            for article, chunk in zip(valid_articles, chunks):
                if isinstance(chunk, BaseException):
                    logger.warning("Ошибка анализа статьи %s: %s", article.get("url", ""), chunk)
                    continue
                text = chunk.strip()
                if not text:
                    continue
                collected.append(
                    "\n".join(
                        [
                            f"### Источник: {article.get('title', 'Без заголовка')}",
                            f"URL: {article.get('url', '')}",
                            text,
                        ]
                    )
                )

            if not collected:
                return "Релевантный контент по найденным статьям не обнаружен."

            return "\n\n".join(collected)
        except Exception as exc:
            logger.error("Ошибка анализа содержимого: %s", exc)
            return f"Ошибка при анализе содержимого: {exc}"

    async def execute(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            has_any_search = any(
                [
                    self._get_jina_api_key(),
                    self._get_tavily_api_key(),
                    self._get_zai_api_key(),
                    self._get_proxy_url(),
                ]
            )
            if not has_any_search:
                return {
                    "success": False,
                    "error": (
                        "Не настроен ни один поисковый API. Проверьте переменные окружения: "
                        "JINA_API_KEY, TAVILY_API_KEY, ZAI_API_KEY или PROXY_URL"
                    ),
                }

            query = (args.get("query") or "").strip()
            if not query:
                return {"success": False, "error": "Запрос не может быть пустым"}

            max_results_per_lang = int(args.get("max_results_per_lang") or 10)
            max_results_per_lang = max(1, min(max_results_per_lang, 20))
            analyze_content = args.get("analyze_content", True)
            if analyze_content is None:
                analyze_content = True

            logger.info("Начинаем веб-исследование для запроса: %s", query)

            tasks = [
                self._find_articles_for_language(query, "ru", 2, max_results_per_lang),
                self._find_articles_for_language(query, "en", 3, max_results_per_lang),
                self._find_articles_for_language(query, "zh", 2, max_results_per_lang),
            ]

            results = await asyncio.gather(*tasks, return_exceptions=True)

            links_ru = results[0] if isinstance(results[0], list) else []
            links_en = results[1] if isinstance(results[1], list) else []
            links_zh = results[2] if isinstance(results[2], list) else []

            logger.info("Найдено ссылок - RU: %s, EN: %s, ZH: %s", len(links_ru), len(links_en), len(links_zh))

            all_links = links_ru[:8] + links_en[:8] + links_zh[:4]

            if not all_links:
                return {"success": True, "output": "Не найдено релевантных статей по запросу."}

            logger.info("Скачиваем содержимое %s статей...", len(all_links))

            content_tasks = [self._download_content(link) for link in all_links]
            articles_data = await asyncio.gather(*content_tasks, return_exceptions=True)

            valid_articles = []
            for article in articles_data:
                if isinstance(article, dict) and article.get("content", "").strip():
                    valid_articles.append(article)

            logger.info("Успешно скачано содержимое %s статей", len(valid_articles))

            output_parts: list[str] = []
            output_parts.append(f"Найдено ссылок: RU={len(links_ru)}, EN={len(links_en)}, ZH={len(links_zh)}")
            output_parts.append(f"Скачано статей: {len(valid_articles)}")
            output_parts.append("")

            if links_ru:
                output_parts.append("=== Русские источники ===")
                for link in links_ru[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            if links_en:
                output_parts.append("=== English sources ===")
                for link in links_en[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            if links_zh:
                output_parts.append("=== 中文来源 ===")
                for link in links_zh[:max_results_per_lang]:
                    output_parts.append(f"• {link}")
                output_parts.append("")

            if analyze_content and valid_articles:
                logger.info("Анализируем статьи по отдельности с помощью RESEARCH_MODEL...")
                analysis = await self._analyze_content_with_llm(query, valid_articles)
                output_parts.append("=== АНАЛИЗ ===")
                output_parts.append(analysis)
            elif not valid_articles:
                output_parts.append("Не удалось скачать содержимое статей для анализа.")

            return {"success": True, "output": "\n".join(output_parts)}
        except Exception as exc:
            error_msg = f"Ошибка выполнения веб-исследования: {exc}"
            logger.error(error_msg)
            return {"success": False, "error": error_msg}

    async def build_seed(
        self,
        *,
        topic: str,
        additional_instructions: str,
        max_results_per_lang: int = 10,
    ) -> str:
        query = topic.strip()
        if additional_instructions.strip():
            query = f"{query}. {additional_instructions.strip()}"

        result = await self.execute(
            {
                "query": query,
                "max_results_per_lang": max_results_per_lang,
                "analyze_content": True,
            }
        )
        if not result.get("success"):
            return ""

        output = str(result.get("output") or "")
        marker = "=== АНАЛИЗ ==="
        if marker in output:
            return output.split(marker, 1)[1].strip()
        return output.strip()


def has_any_search_provider_config(
    *,
    tavily_api_key: str | None = None,
    jina_api_key: str | None = None,
    zai_api_key: str | None = None,
    proxy_url: str | None = None,
) -> bool:
    return any(
        [
            has_api_key(tavily_api_key),
            has_api_key(jina_api_key),
            has_api_key(zai_api_key),
            bool(proxy_url and str(proxy_url).strip()),
        ]
    )


def build_research_seed_sync(
    *,
    openai_api_key: str | None = None,
    research_model: str,
    text_completion: Callable[[list[dict[str, str]], float, int], Awaitable[str]] | None = None,
    tavily_api_key: str | None = None,
    jina_api_key: str | None = None,
    zai_api_key: str | None = None,
    proxy_url: str | None = None,
    topic: str,
    additional_instructions: str,
    temperature: float = 0.2,
    max_results_per_lang: int = 10,
    http_client: httpx.AsyncClient | None = None,
) -> str:
    client = WebResearchClient(
        research_model=research_model,
        text_completion=text_completion,
        tavily_api_key=tavily_api_key,
        jina_api_key=jina_api_key,
        zai_api_key=zai_api_key,
        proxy_url=proxy_url,
        temperature=temperature,
        http_client=http_client,
    )

    async def _run() -> str:
        return await client.build_seed(
            topic=topic,
            additional_instructions=additional_instructions,
            max_results_per_lang=max_results_per_lang,
        )

    try:
        return asyncio.run(_run())
    except RuntimeError:
        loop = asyncio.new_event_loop()
        try:
            return loop.run_until_complete(_run())
        finally:
            loop.close()
