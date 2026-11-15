import asyncio
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import neuraldeep_music as music


TARGET = "https://neuraldeep.example/v1/music/generate"
HEADERS = {"Authorization": "Bearer provider-key"}
PAYLOAD = {"model": "lyria-3-pro", "input": "Jazz", "lyrics": "[instrumental]"}


def test_music_waits_for_same_task_and_downloads_audio():
    async def run():
        requests = []
        statuses = iter(["queued", "running", "succeeded"])

        def respond(request):
            requests.append(request)
            if request.method == "POST":
                import json
                body = json.loads(request.content)
                assert body == {
                    "model": "lyria-3-pro", "prompt": "Jazz", "lyrics": "[instrumental]",
                    "audio_duration": 60, "vocal_language": "ru", "bpm": 90,
                    "key_scale": "C major", "time_signature": "4/4", "seed": 42, "batch_size": 1,
                }
                return httpx.Response(200, json={"task_id": "task-1"})
            if request.url.path.endswith("/result"):
                assert dict(request.url.params) == {"index": "0", "audio_format": "flac"}
                return httpx.Response(200, content=b"music", headers={"content-type": "audio/flac"})
            assert request.url.path == "/v1/music/tasks/task-1"
            return httpx.Response(200, json={"status": next(statuses)})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music.asyncio, "sleep", new_callable=AsyncMock) as sleep:
                result = await music.generate_neuraldeep_music(
                    TARGET, HEADERS,
                    dict(PAYLOAD, response_format="flac", audio_duration=60, vocal_language="ru",
                         bpm=90, key_scale="C major", time_signature="4/4", seed=42, batch_size=1),
                    client,
                )
        assert result == (b"music", 200, "audio/flac")
        assert sleep.await_count == 2
        assert len([r for r in requests if r.method == "POST"]) == 1
        assert all(r.headers["Authorization"] == HEADERS["Authorization"] for r in requests)

    asyncio.run(run())


@pytest.mark.parametrize("failure,expected", [
    ("failed", 502), ("unknown", 502), ("bad-json", 502), ("missing-id", 502),
    ("create-error", 402), ("poll-error", 503), ("download-error", 404), ("network-error", 503),
])
def test_music_errors_do_not_resubmit_task(failure, expected):
    async def run():
        posts = []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
                if failure == "create-error":
                    return httpx.Response(402, json={"error": "balance"})
                return httpx.Response(200, json={} if failure == "missing-id" else {"task_id": "t"})
            if failure == "network-error":
                raise httpx.ConnectError("offline", request=request)
            if failure == "bad-json":
                return httpx.Response(200, content=b"bad-json")
            if failure == "poll-error":
                return httpx.Response(429)
            if request.url.path.endswith("/result"):
                return httpx.Response(404)
            return httpx.Response(200, json={"status": "succeeded" if failure == "download-error" else failure})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException) as error:
                await music.generate_neuraldeep_music(TARGET, HEADERS, PAYLOAD, client)
        assert error.value.status_code == expected
        assert len(posts) == 1

    asyncio.run(run())


def test_music_total_timeout_does_not_resubmit_task():
    async def run():
        posts = []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
                return httpx.Response(200, json={"task_id": "t"})
            return httpx.Response(200, json={"status": "running"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music, "MUSIC_TIMEOUT_SECONDS", 0.01), pytest.raises(HTTPException) as error:
                await music.generate_neuraldeep_music(TARGET, HEADERS, PAYLOAD, client)
        assert error.value.status_code == 504
        assert len(posts) == 1

    asyncio.run(run())


@pytest.mark.parametrize("overrides", [{"input": [{"text": "hello"}]}, {"response_format": "pcm"}])
def test_music_rejects_invalid_speech_parameters_before_creation(overrides):
    client = AsyncMock()
    with pytest.raises(HTTPException) as error:
        asyncio.run(music.generate_neuraldeep_music(TARGET, HEADERS, dict(PAYLOAD, **overrides), client))
    assert error.value.status_code == 400
    client.post.assert_not_awaited()
