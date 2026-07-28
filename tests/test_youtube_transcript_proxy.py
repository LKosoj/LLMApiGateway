import unittest
from unittest.mock import patch

from fastapi import HTTPException

from llm_gateway_core.agents import web_research as web_research_agent
from llm_gateway_core.api.v1 import web_extraction as web_extraction_owner
from llm_gateway_core.config.settings import settings
from llm_gateway_core.utils import youtube_transcript as youtube_transcript_owner
from tests._async_compat import run_async


VIDEO_URL = "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


class _RecordingYouTubeTranscriptApi:
    """Captures the kwargs the gateway passes to youtube_transcript_api."""

    instances: list["_RecordingYouTubeTranscriptApi"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        type(self).instances.append(self)


class _BlockedYouTubeTranscriptApi:
    def __init__(self, **kwargs):
        self.kwargs = kwargs

    def list(self, video_id):
        raise RuntimeError(f"YouTube is blocking requests from your IP ({video_id})")


class BuildYouTubeTranscriptApiTests(unittest.TestCase):
    def setUp(self):
        _RecordingYouTubeTranscriptApi.instances = []

    def test_transcript_api_is_built_without_proxy_when_variable_is_unset(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _RecordingYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
        ):
            api = youtube_transcript_owner.build_youtube_transcript_api()

        self.assertEqual(api.kwargs, {})

    def test_transcript_api_uses_configured_proxy(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _RecordingYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", "socks5://user:pass@proxy.example:1080"),
        ):
            api = youtube_transcript_owner.build_youtube_transcript_api()

        proxy_config = api.kwargs["proxy_config"]
        self.assertEqual(
            proxy_config.to_requests_dict(),
            {
                "http": "socks5://user:pass@proxy.example:1080",
                "https": "socks5://user:pass@proxy.example:1080",
            },
        )

    def test_blank_proxy_variable_is_treated_as_unset(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _RecordingYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", "   "),
        ):
            api = youtube_transcript_owner.build_youtube_transcript_api()

        self.assertEqual(api.kwargs, {})


class DirectFetchTranscriptErrorTests(unittest.TestCase):
    def test_direct_fetch_reports_error_instead_of_falling_back_to_video_page(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _BlockedYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
            patch.object(web_extraction_owner, "_get_with_public_redirects") as get_with_redirects,
        ):
            with self.assertRaises(HTTPException) as ctx:
                run_async(web_extraction_owner._direct_http_fetch(VIDEO_URL))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("YouTube transcript is unavailable", ctx.exception.detail)
        get_with_redirects.assert_not_called()

    def test_direct_fetch_reports_error_for_empty_transcript(self):
        class _EmptyTranscript:
            language = "en"

            def fetch(self):
                return []

        class _EmptyTranscriptList:
            def find_transcript(self, languages):
                return _EmptyTranscript()

        class _EmptyTranscriptApi:
            def __init__(self, **kwargs):
                pass

            def list(self, video_id):
                return _EmptyTranscriptList()

        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _EmptyTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
            patch.object(web_extraction_owner, "_get_with_public_redirects") as get_with_redirects,
        ):
            with self.assertRaises(HTTPException) as ctx:
                run_async(web_extraction_owner._direct_http_fetch(VIDEO_URL))

        self.assertEqual(ctx.exception.status_code, 503)
        self.assertIn("YouTube transcript is empty", ctx.exception.detail)
        get_with_redirects.assert_not_called()


class ResearchAgentTranscriptErrorTests(unittest.TestCase):
    def test_download_content_propagates_transcript_failure(self):
        agent = web_research_agent.WebResearchClient()

        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _BlockedYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
        ):
            with self.assertRaises(RuntimeError):
                run_async(agent._download_content(VIDEO_URL))


if __name__ == "__main__":
    unittest.main()
