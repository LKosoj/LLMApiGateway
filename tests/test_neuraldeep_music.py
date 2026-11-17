import asyncio
from datetime import datetime, timezone
from email.utils import format_datetime
from unittest.mock import AsyncMock, patch

import httpx
import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import neuraldeep_music as music
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState


TARGET = "https://neuraldeep.example/v1/music/generate"
HEADERS = {"Authorization": "Bearer provider-key"}
PAYLOAD = {"model": "lyria-3-pro", "input": "Jazz", "lyrics": "[instrumental]"}


def _routing_state(epoch: list[float], monotonic: list[float]) -> UpstreamRoutingState:
    return UpstreamRoutingState(
        time_func=lambda: epoch[0],
        monotonic_func=lambda: monotonic[0],
    )


def _successful_music_responder(posts: list[httpx.Request], gets: list[httpx.Request]):
    def respond(request):
        if request.method == "POST":
            posts.append(request)
            return httpx.Response(200, json={"task_id": "task-1"})
        gets.append(request)
        if request.url.path.endswith("/result"):
            return httpx.Response(200, content=b"music", headers={"content-type": "audio/mpeg"})
        return httpx.Response(200, json={"status": "succeeded"})

    return respond


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


@pytest.mark.parametrize("source", ["retry_after", "retry_after_date", "quota_header", "body"])
def test_music_quota_cooldown_uses_reset_source(source):
    async def run():
        epoch = [datetime.now(timezone.utc).replace(microsecond=0).timestamp()]
        monotonic = [100.0]
        state = _routing_state(epoch, monotonic)
        reset_at = datetime.fromtimestamp(epoch[0] + 7200, tz=timezone.utc).isoformat()
        headers = {"content-type": "application/json"}
        body = {"error": "monthly quota exhausted"}
        if source == "retry_after":
            headers["retry-after"] = "7200"
        elif source == "retry_after_date":
            headers["retry-after"] = format_datetime(
                datetime.fromtimestamp(epoch[0] + 7200, tz=timezone.utc), usegmt=True,
            )
        elif source == "quota_header":
            headers["x-quota-reset"] = reset_at
        else:
            body = {"error": {"message": "monthly quota exhausted", "reset_at": reset_at}}

        def respond(request):
            assert request.method == "POST"
            return httpx.Response(429, json=body, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music.time, "time", return_value=epoch[0]), pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") == pytest.approx(7200, abs=2)

    asyncio.run(run())


def test_music_quota_cooldown_uses_latest_reset_and_http_date_retry_after():
    async def run():
        epoch = [datetime.now(timezone.utc).replace(microsecond=0).timestamp()]
        monotonic = [100.0]
        state = _routing_state(epoch, monotonic)
        reset_at = datetime.fromtimestamp(epoch[0] + 7200, tz=timezone.utc).isoformat()
        retry_after = format_datetime(
            datetime.fromtimestamp(epoch[0] + 3600, tz=timezone.utc), usegmt=True,
        )

        def respond(request):
            return httpx.Response(
                429,
                json={"error": {"reset_at": reset_at}},
                headers={
                    "retry-after": retry_after,
                    "x-quota-reset": datetime.fromtimestamp(epoch[0] + 10800, tz=timezone.utc).isoformat(),
                },
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music.time, "time", return_value=epoch[0]), pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") == pytest.approx(10800)

    asyncio.run(run())


@pytest.mark.parametrize(
    "headers,body",
    [
        ({}, {}),
        ({"retry-after": "0"}, {}),
        ({"retry-after": "-1"}, {}),
        ({"retry-after": "NaN"}, {}),
        ({"retry-after": "Infinity"}, {}),
        ({"retry-after": "not-a-date"}, {}),
        ({"x-quota-reset": "not-a-date"}, {}),
        ({}, {"error": {"reset_at": "1970-01-01T00:00:00+00:00"}}),
    ],
)
def test_music_invalid_or_missing_quota_reset_does_not_set_cooldown(headers, body):
    async def run():
        epoch = [1_700_000_000.0]
        state = _routing_state(epoch, [100.0])

        def respond(request):
            return httpx.Response(429, json=body, headers=headers)

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") is None

    asyncio.run(run())


def test_music_non_quota_status_does_not_use_retry_after_for_quota_cooldown():
    async def run():
        state = _routing_state([1_700_000_000.0], [100.0])

        def respond(request):
            return httpx.Response(503, headers={"retry-after": "7200"}, json={"error": "overloaded"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") is None

    asyncio.run(run())


def test_music_quota_cooldown_isolated_by_key_and_model():
    async def run():
        epoch = [1_700_000_000.0]
        state = _routing_state(epoch, [100.0])
        posts = []
        gets = []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
            else:
                gets.append(request)
            if request.method == "POST" and len(posts) == 1:
                return httpx.Response(
                    429,
                    headers={"retry-after": "7200"},
                    json={"error": "quota"},
                )
            if request.url.path.endswith("/result"):
                return httpx.Response(200, content=b"music", headers={"content-type": "audio/mpeg"})
            if request.method == "GET":
                return httpx.Response(200, json={"status": "succeeded"})
            return httpx.Response(200, json={"task_id": "task-1"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
            with patch.object(music.asyncio, "sleep", new_callable=AsyncMock):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, dict(PAYLOAD, model="lyria-3-basic"), client,
                    routing_state=state, key_fingerprint="key-a",
                )
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-b",
                )
        assert len(posts) == 3

    asyncio.run(run())


def test_music_quota_cooldown_skips_requests_until_expiry_then_retries():
    async def run():
        epoch = [1_700_000_000.0]
        monotonic = [100.0]
        state = _routing_state(epoch, monotonic)
        posts = []
        gets = []
        responder = _successful_music_responder(posts, gets)

        def quota_responder(request):
            posts.append(request)
            return httpx.Response(429, headers={"retry-after": "60"}, json={"error": "quota"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(quota_responder)) as client:
            with pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
            with pytest.raises(HTTPException) as blocked:
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
            assert blocked.value.status_code == 503
            assert len(posts) == 1
            assert not gets

        monotonic[0] += 61
        epoch[0] += 61
        async with httpx.AsyncClient(transport=httpx.MockTransport(responder)) as client:
            with patch.object(music.asyncio, "sleep", new_callable=AsyncMock):
                result = await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        assert result[0] == b"music"
        assert len(posts) == 2
        assert len(gets) == 2
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") is None
        assert state.get_status_rows()[0]["health_status"] == "healthy"

    asyncio.run(run())


def test_music_monthly_quota_reset_preserves_more_than_one_day():
    async def run():
        epoch = [1_700_000_000.0]
        state = _routing_state(epoch, [100.0])
        reset_at = datetime.fromtimestamp(epoch[0] + 30 * 86400, tz=timezone.utc).isoformat()

        def respond(request):
            return httpx.Response(
                429,
                json={"error": {"message": "monthly quota exhausted", "reset_at": reset_at}},
            )

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music.time, "time", return_value=epoch[0]), pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    routing_state=state, key_fingerprint="key-a",
                )
        remaining = state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a")
        assert remaining is not None and remaining > 86400

    asyncio.run(run())


def test_music_quota_reset_stops_retries_even_when_retry_count_is_two():
    async def run():
        state = _routing_state([1_700_000_000.0], [100.0])
        posts = []

        def respond(request):
            assert request.method == "POST"
            posts.append(request)
            return httpx.Response(429, headers={"retry-after": "60"}, json={"error": "quota"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with pytest.raises(HTTPException):
                await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client,
                    retry_count=2, routing_state=state, key_fingerprint="key-a",
                )
        assert len(posts) == 1
        assert state.cooldown_remaining_seconds("neuraldeep", PAYLOAD["model"], "key-a") == pytest.approx(60)

    asyncio.run(run())


@pytest.mark.parametrize("failure", ["http503", "network"])
def test_music_retries_non_quota_failures_and_succeeds_on_third_post(failure):
    async def run():
        posts = []
        gets = []

        def respond(request):
            if request.method == "POST":
                posts.append(request)
                if len(posts) < 3:
                    if failure == "network":
                        raise httpx.ConnectError("offline", request=request)
                    return httpx.Response(503, json={"error": "overloaded"})
                return httpx.Response(200, json={"task_id": "task-1"})
            gets.append(request)
            if request.url.path.endswith("/result"):
                return httpx.Response(200, content=b"music", headers={"content-type": "audio/mpeg"})
            return httpx.Response(200, json={"status": "succeeded"})

        async with httpx.AsyncClient(transport=httpx.MockTransport(respond)) as client:
            with patch.object(music.asyncio, "sleep", new_callable=AsyncMock):
                result = await music.generate_neuraldeep_music(
                    TARGET, HEADERS, PAYLOAD, client, retry_count=2,
                )
        assert result[0] == b"music"
        assert len(posts) == 3
        assert len(gets) == 2

    asyncio.run(run())
