from __future__ import annotations

import asyncio
import logging
from typing import Any
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException

from ...agents.web_research import _extract_text_with_selectolax
from ...config.settings import settings
from .web_content import (
    clean_read_url as _clean_read_url,
    content_with_images as _content_with_images,
    extract_article_images_from_html as _extract_article_images_from_html,
)
from .web_safe_fetch import (
    _PinnedForwardProxy,
    _get_with_public_redirects,
    _validated_fetch_url,
)

logger = logging.getLogger(__name__)

FREEDIUM_MIRROR_PREFIX = "https://freedium-mirror.cfd/"


def _is_medium_url(url: str) -> bool:
    parsed = urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    return hostname == "medium.com" or hostname.endswith(".medium.com")


def _is_freedium_url(url: str) -> bool:
    parsed = urlsplit(url)
    return (parsed.hostname or "").lower() == "freedium-mirror.cfd"


def _direct_fetch_url_candidates(url: str) -> list[tuple[str, str | None]]:
    if _is_medium_url(url) and not _is_freedium_url(url):
        return [(f"{FREEDIUM_MIRROR_PREFIX}{url}", url), (url, None)]
    return [(url, None)]


async def _direct_http_fetch(url: str) -> dict[str, Any] | None:
    from ...agents.web_research import (
        _HTMLTextExtractor,
        _extract_youtube_video_id,
        _HTML_TITLE_RE,
        _HTML_H1_RE,
        _HTML_TAG_RE,
    )
    from html import unescape

    cleaned = _clean_read_url(url)
    if not cleaned.startswith(("http://", "https://")):
        return None

    video_id = _extract_youtube_video_id(cleaned)
    if video_id:
        try:
            from youtube_transcript_api import YouTubeTranscriptApi

            def _fetch() -> tuple[str, str]:
                api = YouTubeTranscriptApi()
                transcripts = api.list(video_id)
                try:
                    transcript = transcripts.find_transcript(["ru", "en", "zh-Hans", "zh-Hant"])
                except Exception:
                    transcript = next(iter(transcripts))
                segments = list(transcript.fetch())
                parts = [(seg.get("text") if isinstance(seg, dict) else getattr(seg, "text", "")) for seg in segments]
                text = " ".join(p.strip() for p in parts if p)
                return text, f"YouTube: {video_id} ({getattr(transcript, 'language', '')})"

            content, title = await asyncio.to_thread(_fetch)
            if content.strip():
                return {"url": cleaned, "title": title, "content": content}
        except ImportError:
            logger.debug("youtube_transcript_api not installed; skipping YouTube transcript for %s", cleaned)
        except Exception as exc:
            logger.warning("YouTube transcript failed for %s: %s", cleaned, exc)

    for fetch_url, response_url_override in _direct_fetch_url_candidates(cleaned):
        if fetch_url != cleaned:
            logger.info("Routing Medium URL through Freedium mirror: %s", cleaned)
        try:
            response, final_url = await _get_with_public_redirects(fetch_url)
            response.raise_for_status()
            result_url = response_url_override or final_url
            image_base_url = final_url
            content_type = response.headers.get("Content-Type", "").lower()
            if fetch_url.lower().endswith(".pdf") or "application/pdf" in content_type:
                try:
                    from io import BytesIO

                    from pdfminer.high_level import extract_text

                    pdf_text = await asyncio.to_thread(extract_text, BytesIO(response.content))
                    if pdf_text and pdf_text.strip():
                        return {
                            "url": result_url,
                            "title": cleaned.rsplit("/", 1)[-1] or "PDF",
                            "content": pdf_text.strip(),
                        }
                except ImportError:
                    logger.debug("pdfminer not installed; skipping direct PDF extraction for %s", fetch_url)
                except Exception as exc:
                    logger.warning("Direct PDF extraction failed for %s: %s", fetch_url, exc)
                return None

            html_text = response.text
            html_images = _extract_article_images_from_html(html_text, image_base_url)
            title = ""
            title_match = _HTML_TITLE_RE.search(html_text) or _HTML_H1_RE.search(html_text)
            if title_match:
                title = unescape(_HTML_TAG_RE.sub("", title_match.group(1))).strip()

            extracted = _trafilatura_markdown(html_text, image_base_url)
            if extracted:
                content, images = _content_with_images(extracted, html_images, image_base_url)
                return {"url": result_url, "title": title, "content": content, "images": images}

            selectolax_text = await asyncio.to_thread(_extract_text_with_selectolax, html_text)
            if selectolax_text:
                content, images = _content_with_images(selectolax_text, html_images, image_base_url)
                return {"url": result_url, "title": title, "content": content, "images": images}

            def _parse_html_to_text(raw_html: str) -> str:
                parser = _HTMLTextExtractor()
                parser.feed(raw_html)
                return "\n".join(line.strip() for line in "\n".join(parser.parts).splitlines() if line.strip())

            text = await asyncio.to_thread(_parse_html_to_text, html_text)
            if text:
                content, images = _content_with_images(text, html_images, image_base_url)
                return {"url": result_url, "title": title, "content": content, "images": images}
            if html_images:
                content, images = _content_with_images("", html_images, image_base_url)
                return {"url": result_url, "title": title, "content": content, "images": images}
        except HTTPException:
            raise
        except httpx.TimeoutException:
            logger.warning("Direct HTTP fetch timed out for %s", fetch_url)
        except httpx.HTTPError as exc:
            logger.warning("Direct HTTP fetch HTTP error for %s: %s", fetch_url, exc)
        except Exception as exc:
            logger.warning("Direct HTTP fetch unexpected error for %s: %s", fetch_url, exc)
    return None


def _trafilatura_markdown(html_text: str, base_url: str) -> str | None:
    """Extract clean main-content Markdown with inline ``![](url)`` images via full trafilatura.

    Only the full ``trafilatura`` package keeps image links inside the markdown body;
    ``rs_trafilatura`` surfaces images as a separate list, so the read pipeline relies on this
    engine to return images on their place in the text. Returns ``None`` when trafilatura is
    unavailable or yields no main content.
    """
    try:
        import trafilatura
    except ImportError:
        logger.debug("trafilatura not installed; cannot extract inline-image markdown for %s", base_url)
        return None
    try:
        extracted = trafilatura.extract(
            html_text,
            url=base_url,
            include_formatting=True,
            include_links=True,
            include_tables=True,
            include_images=True,
            include_comments=False,
            output_format="markdown",
        )
    except Exception as exc:
        logger.warning("trafilatura extraction failed for %s: %s", base_url, exc)
        return None
    if extracted and extracted.strip():
        return extracted
    return None


def _title_from_html(html_text: str) -> str:
    from html import unescape

    from ...agents.web_research import _HTML_H1_RE, _HTML_TAG_RE, _HTML_TITLE_RE

    match = _HTML_TITLE_RE.search(html_text) or _HTML_H1_RE.search(html_text)
    if not match:
        return ""
    return unescape(_HTML_TAG_RE.sub("", match.group(1))).strip()


def _extract_cloakbrowser_markdown(html_text: str, final_url: str, page_title: str) -> dict[str, Any] | None:
    # Full trafilatura keeps image links inline in the markdown body; fall back to plain
    # selectolax text (without inline images) only when trafilatura is unavailable or the
    # rendered page is not article-like, so the rendered content is not dropped entirely.
    markdown = _trafilatura_markdown(html_text, final_url)
    if not markdown:
        markdown = _extract_text_with_selectolax(html_text)
    markdown = (markdown or "").strip()
    if not markdown:
        return None
    # Prefer Playwright's page.title() (reads document.title) over the heuristic HTML <title>
    # title — the latter sometimes returns the article's lead sentence instead of the real
    # <title> (e.g. en.wikipedia.org/wiki/Type_system).
    title = (page_title or "").strip() or _title_from_html(html_text)
    content, images = _content_with_images(markdown, _extract_article_images_from_html(html_text, final_url), final_url)
    return {"url": final_url, "title": title, "content": content, "images": images}


def _abort_blocked_cloakbrowser_request(route: Any) -> None:
    request_url = getattr(getattr(route, "request", None), "url", "")
    try:
        asyncio.run(_validated_fetch_url(str(request_url)))
    except HTTPException:
        route.abort()
        return
    route.continue_()


def _cloakbrowser_launch_args() -> list[str]:
    browser_args = [
        "--disable-dev-shm-usage",
        # --proxy-server (set below in launch()) does not cover WebRTC
        # ICE/STUN/TURN UDP traffic, which would otherwise let a page use
        # RTCPeerConnection to leak internal IPs / probe internal UDP ports,
        # bypassing _PinnedForwardProxy's SSRF protection.
        "--force-webrtc-ip-handling-policy=disable_non_proxied_udp",
    ]
    if settings.web_read_cloakbrowser_no_sandbox:
        browser_args.append("--no-sandbox")
    return browser_args


def _cloakbrowser_render_sync(url: str, proxy_port: int) -> tuple[str, str, str]:
    from contextlib import redirect_stderr, redirect_stdout
    from io import StringIO

    from cloakbrowser import launch

    with redirect_stdout(StringIO()), redirect_stderr(StringIO()):
        browser = launch(
            headless=True,
            locale="ru-RU",
            humanize=False,
            args=_cloakbrowser_launch_args(),
            # Force every outbound connection through our pinned-IP forward proxy
            # so the browser's own DNS resolution can never diverge from the
            # host we already validated (see _PinnedForwardProxy).
            proxy={"server": f"http://127.0.0.1:{proxy_port}"},
        )
        try:
            page = browser.new_page()
            page.route("**/*", _abort_blocked_cloakbrowser_request)
            page.goto(url, wait_until="domcontentloaded", timeout=20_000)
            try:
                page.wait_for_load_state("networkidle", timeout=5_000)
            except Exception:
                logger.debug("CloakBrowser networkidle wait timed out for %s; extracting current DOM.", url)
            return page.content(), page.title(), page.url
        finally:
            browser.close()


async def _cloakbrowser_fetch(url: str) -> dict[str, Any] | None:
    cleaned = _clean_read_url(url)
    if not cleaned.startswith(("http://", "https://")):
        return None
    if not settings.web_read_cloakbrowser_enabled:
        logger.info("CloakBrowser rendered fetch is disabled; skipping local browser render for %s", cleaned)
        return None

    proxy = _PinnedForwardProxy()
    try:
        await _validated_fetch_url(cleaned)
        await proxy.start()
        html_text, page_title, final_url = await asyncio.to_thread(
            _cloakbrowser_render_sync, cleaned, proxy.port
        )
    except ImportError:
        logger.warning("cloakbrowser is not installed; skipping rendered fetch for %s", cleaned)
        return None
    except Exception as exc:
        logger.warning("CloakBrowser rendered fetch failed for %s: %s", cleaned, exc)
        return None
    finally:
        await proxy.stop()
    await _validated_fetch_url(final_url)
    return _extract_cloakbrowser_markdown(html_text, final_url, page_title)
