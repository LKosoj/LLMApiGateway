"""Shared YouTube transcript client for the web read API and the research agent.

YouTube blocks its caption endpoint for datacenter IPs, so the transcript is fetched
through ``YOUTUBE_PROXY_URL`` when that variable is set. Without it the transcript is
requested directly from the gateway's own IP.
"""

from __future__ import annotations

from typing import Any

from ..config.settings import settings


def build_youtube_transcript_api() -> Any:
    """Return a ``YouTubeTranscriptApi`` bound to ``YOUTUBE_PROXY_URL`` when configured."""
    from youtube_transcript_api import YouTubeTranscriptApi

    proxy_url = (settings.youtube_proxy_url or "").strip()
    if not proxy_url:
        return YouTubeTranscriptApi()

    from youtube_transcript_api.proxies import GenericProxyConfig

    return YouTubeTranscriptApi(
        proxy_config=GenericProxyConfig(http_url=proxy_url, https_url=proxy_url)
    )
