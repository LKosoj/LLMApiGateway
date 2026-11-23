"""Wait for a NeuralDeep music task and return its audio through Audio Speech."""

import asyncio
import logging
import math
import time
from datetime import datetime
from urllib.parse import quote

import httpx
from fastapi import HTTPException

from ...services.request_handler import parse_retry_after_header
from ...services.upstream_routing_state import UpstreamRoutingState
from .operation_proxy import (
    DownstreamHTTPException,
    downstream_error_status_code,
    extract_downstream_error_detail,
    proxy_json_to_downstream,
    sanitize_target_url_for_log,
    should_retry_operation_status,
    sleep_before_retry,
)

MUSIC_TIMEOUT_SECONDS = 360
MUSIC_POLL_INTERVAL_SECONDS = 3
MUSIC_AUDIO_FORMATS = frozenset({"mp3", "wav", "wav32", "flac", "opus", "aac"})
logger = logging.getLogger(__name__)


def _quota_reset_after(response: httpx.Response) -> float | None:
    delays = []
    retry_after = parse_retry_after_header(response.headers.get("retry-after"))
    if retry_after is not None and math.isfinite(retry_after) and retry_after > 0:
        delays.append(retry_after)
    reset_values = [response.headers.get("x-quota-reset")]
    try:
        body = response.json()
    except ValueError:
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), dict):
        reset_values.append(body["error"].get("reset_at"))
    for value in reset_values:
        if not isinstance(value, str):
            continue
        try:
            reset_at = datetime.fromisoformat(value)
            if reset_at.tzinfo is None:
                continue
            delay = reset_at.timestamp() - time.time()
        except (ValueError, OverflowError):
            continue
        if math.isfinite(delay) and delay > 0:
            delays.append(delay)
    return max(delays) if delays else None


def _check_response(response: httpx.Response) -> None:
    if response.status_code >= 400:
        error = DownstreamHTTPException(
            status_code=downstream_error_status_code(response.status_code),
            detail=extract_downstream_error_detail(response),
        )
        error.upstream_response = response
        raise error


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
    routing_state: UpstreamRoutingState | None = None,
    key_fingerprint: str | None = None,
    completed_usage: dict | None = None,
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

    if routing_state is not None and key_fingerprint:
        remaining = routing_state.cooldown_remaining_seconds("neuraldeep", str(payload["model"]), key_fingerprint)
        if remaining is not None:
            raise DownstreamHTTPException(
                status_code=503,
                detail=f"NeuralDeep music is cooling down for another {math.ceil(remaining)} seconds.",
            )

    try:
        async with asyncio.timeout(MUSIC_TIMEOUT_SECONDS):
            for attempt in range(retry_count + 1):
                try:
                    task, _ = await proxy_json_to_downstream(target_url, headers, music_payload, http_client)
                    break
                except HTTPException as exc:
                    response = exc.upstream_response if isinstance(exc, DownstreamHTTPException) else None
                    if response is not None and response.status_code == 429 and _quota_reset_after(response) is not None:
                        raise
                    retryable_failure = isinstance(exc, DownstreamHTTPException) or isinstance(
                        exc.__cause__, httpx.RequestError,
                    )
                    if attempt == retry_count or not retryable_failure or not should_retry_operation_status(exc.status_code):
                        raise
                    await sleep_before_retry(
                        sanitize_target_url_for_log(target_url), retry_delay, attempt + 1, retry_count + 1,
                        f"upstream HTTP {exc.status_code}",
                    )
            task_id = task.get("task_id") if isinstance(task, dict) else None
            if not isinstance(task_id, str) or not task_id:
                raise HTTPException(status_code=502, detail="NeuralDeep music returned no task_id.")
            task_url = target_url.rsplit("/generate", 1)[0] + "/tasks/" + quote(task_id, safe="")
            while True:
                task = _task_json(await http_client.get(task_url, headers=headers))
                status = task.get("status")
                if status == "succeeded":
                    if completed_usage is not None:
                        completed_usage["usage"] = task.get("usage")
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
            if routing_state is not None and key_fingerprint:
                routing_state.record_success("neuraldeep", str(payload["model"]), key_fingerprint)
            return response.content, response.status_code, response.headers.get("content-type")
    except DownstreamHTTPException as exc:
        response = exc.upstream_response
        if routing_state is not None and key_fingerprint and response is not None and response.status_code == 429:
            delay = _quota_reset_after(response)
            if delay is not None:
                routing_state.record_failure(
                    "neuraldeep", str(payload["model"]), key_fingerprint, exc,
                    temporary=True, apply_penalty=False, retry_after=delay,
                )
                logger.warning("NeuralDeep music quota cooldown set for %.0f seconds.", delay)
        raise
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="NeuralDeep music generation timed out.") from exc
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="NeuralDeep music request failed.") from exc
