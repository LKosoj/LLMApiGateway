import asyncio
import threading
import time
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

    def test_calls_alternate_between_the_proxy_and_the_gateway_address(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _RecordingYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", "socks5h://user:pass@proxy.example:1080"),
        ):
            first = youtube_transcript_owner.build_youtube_transcript_api()
            second = youtube_transcript_owner.build_youtube_transcript_api()
            third = youtube_transcript_owner.build_youtube_transcript_api()

        self.assertIn("proxy_config", first.kwargs)
        self.assertEqual(second.kwargs, {})
        self.assertIn("proxy_config", third.kwargs)

    def test_every_call_goes_out_directly_when_no_proxy_is_configured(self):
        with (
            patch("youtube_transcript_api.YouTubeTranscriptApi", _RecordingYouTubeTranscriptApi),
            patch.object(settings, "youtube_proxy_url", None),
        ):
            first = youtube_transcript_owner.build_youtube_transcript_api()
            second = youtube_transcript_owner.build_youtube_transcript_api()

        self.assertEqual(first.kwargs, {})
        self.assertEqual(second.kwargs, {})


class TranscriptQueueTests(unittest.TestCase):
    def test_queued_calls_never_overlap(self):
        active = 0
        peak_active = 0
        counter_guard = threading.Lock()

        def _fetch() -> str:
            nonlocal active, peak_active
            with counter_guard:
                active += 1
                peak_active = max(peak_active, active)
            time.sleep(0.05)
            with counter_guard:
                active -= 1
            return "transcript"

        async def _fetch_four_at_once():
            return await asyncio.gather(
                *(youtube_transcript_owner.run_queued_transcript_call(_fetch) for _ in range(4))
            )

        with patch.object(settings, "youtube_fetch_interval_seconds", 0.0):
            results = run_async(_fetch_four_at_once())

        self.assertEqual(results, ["transcript"] * 4)
        self.assertEqual(peak_active, 1)

    def test_queue_keeps_the_configured_pause_between_calls(self):
        started_at: list[float] = []

        def _fetch() -> str:
            started_at.append(time.monotonic())
            return "transcript"

        async def _fetch_twice():
            await youtube_transcript_owner.run_queued_transcript_call(_fetch)
            await youtube_transcript_owner.run_queued_transcript_call(_fetch)

        with patch.object(settings, "youtube_fetch_interval_seconds", 0.2):
            run_async(_fetch_twice())

        self.assertGreaterEqual(started_at[1] - started_at[0], 0.2)

    def test_a_failed_call_still_paces_the_next_one(self):
        started_at: list[float] = []

        def _failing_fetch() -> str:
            started_at.append(time.monotonic())
            raise RuntimeError("YouTube is blocking requests from your IP")

        async def _fetch_twice():
            for _ in range(2):
                with self.assertRaises(RuntimeError):
                    await youtube_transcript_owner.run_queued_transcript_call(_failing_fetch)

        with patch.object(settings, "youtube_fetch_interval_seconds", 0.2):
            run_async(_fetch_twice())

        self.assertGreaterEqual(started_at[1] - started_at[0], 0.2)


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
