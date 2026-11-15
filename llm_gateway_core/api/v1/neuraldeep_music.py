"""Wait for a NeuralDeep music task and return its audio through Audio Speech."""

import asyncio
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from .operation_proxy import (
    DownstreamHTTPException,
    downstream_error_status_code,
    extract_downstream_error_detail,
    proxy_json_to_downstream,
)

MUSIC_TIMEOUT_SECONDS = 360
MUSIC_POLL_INTERVAL_SECONDS = 3
MUSIC_AUDIO_FORMATS = frozenset({"mp3", "wav", "wav32", "flac", "opus", "aac"})


def _check_response(response: httpx.Response) -> None:
    if response.status_code >= 400:
        raise DownstreamHTTPException(
            status_code=downstream_error_status_code(response.status_code),
            detail=extract_downstream_error_detail(response),
        )


def _task_json(response: httpx.Response) -> dict:
    _check_response(response)
    try:
        body = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="NeuralDeep music returned invalid JSON.") from exc
    if not isinstance(body, dict):
        raise HTTPException(status_code=502, detail="NeuralDeep music returned an invalid task.")
    return body


async def generate_neuraldeep_music(
    target_url: str,
    headers: dict,
    payload: dict[str, object],
    http_client: httpx.AsyncClient,
    *,
    retry_count: int = 0,
    retry_delay: float = 0.0,
) -> tuple[bytes, int, str | None]:
    music_payload = dict(payload)
    description = music_payload.pop("input")
    if not isinstance(description, str):
        raise HTTPException(status_code=400, detail="Music 'input' must be a string describing the music.")
    music_payload["prompt"] = description
    audio_format = music_payload.pop("response_format", "mp3")
    if not isinstance(audio_format, str) or audio_format not in MUSIC_AUDIO_FORMATS:
        raise HTTPException(status_code=400, detail="Music response_format must be mp3, wav, wav32, flac, opus or aac.")
    music_payload.pop("stream", None)

    try:
        async with asyncio.timeout(MUSIC_TIMEOUT_SECONDS):
            task, _ = await proxy_json_to_downstream(
                target_url, headers, music_payload, http_client,
                retry_count=retry_count, retry_delay=retry_delay,
            )
            task_id = task.get("task_id") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise HTTPException(status_code=502, detail="NeuralDeep music returned no task_id.")
            task_url = target_url.rsplit("/generate", 1)[0] + "/tasks/" + quote(task_id, safe="")
            while True:
                task = _task_json(await http_client.get(task_url, headers=headers))
                status = task.get("status")
                if status == "succeeded":
                    break
                if status == "failed":
                    raise HTTPException(status_code=502, detail="NeuralDeep music generation failed.")
                if status not in {"queued", "running"}:
                    raise HTTPException(status_code=502, detail="NeuralDeep music returned an unknown task status.")
                await asyncio.sleep(MUSIC_POLL_INTERVAL_SECONDS)

            response = await http_client.get(
                task_url + "/result", headers=headers,
                params={"index": 0, "audio_format": audio_format},
            )
            _check_response(response)
            return response.content, response.status_code, response.headers.get("content-type")
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="NeuralDeep music generation timed out.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="NeuralDeep music request failed.") from exc
