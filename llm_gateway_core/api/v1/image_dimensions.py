import asyncio
import base64
import io
import urllib.parse

import httpx
from fastapi import HTTPException
from PIL import Image

from .image_adapters import SIZE_DIMENSIONS_RE
from .web_safe_fetch import (
    WEB_FETCH_MAX_REDIRECTS,
    WEB_FETCH_OVERALL_TIMEOUT_SECONDS,
    _WEB_FETCH_REDIRECT_STATUS_CODES,
    _get_with_public_redirects,
    _read_bounded_response,
    _validated_fetch_url,
)

# Провайдеры округляют размер по-своему (1093x1920 вместо 1088x1920): допускаем отклонение каждой стороны.
SIZE_TOLERANCE = 0.10
IMAGE_DIMENSIONS_CHECK_CONCURRENCY = 4


def requested_image_dimensions(payload: dict[str, object]) -> tuple[int, int] | None:
    size = payload.get("size")
    if size is None or size == "auto":
        return None
    match = SIZE_DIMENSIONS_RE.fullmatch(size) if isinstance(size, str) else None
    if match is None or any(int(value) <= 0 for value in match.groups()):
        raise HTTPException(status_code=400, detail="Field 'size' must use positive WIDTHxHEIGHT dimensions or 'auto'.")
    return int(match.group(1)), int(match.group(2))


def _inspect_image_dimensions(content: bytes) -> tuple[str | None, tuple[int, int]]:
    with Image.open(io.BytesIO(content)) as image:
        return image.format, image.size


def _decode_and_inspect_b64_image(raw_b64: str) -> tuple[str | None, tuple[int, int]]:
    content = base64.b64decode(raw_b64, validate=True)
    return _inspect_image_dimensions(content)


async def _drain_tasks(
    *tasks: asyncio.Future[object],
    propagate_cancellation: bool,
) -> None:
    cancelled = False
    pending = {task for task in tasks if not task.done()}
    while pending:
        try:
            await asyncio.shield(asyncio.gather(*pending, return_exceptions=True))
        except asyncio.CancelledError:
            cancelled = True
        pending = {task for task in pending if not task.done()}
    if cancelled and propagate_cancellation:
        raise asyncio.CancelledError


async def _fetch_image_via_proxy_client(
    url: str,
    http_client: httpx.AsyncClient,
) -> httpx.Response:
    try:
        async with asyncio.timeout(WEB_FETCH_OVERALL_TIMEOUT_SECONDS):
            current_fetch_url = await _validated_fetch_url(url)
            for _ in range(WEB_FETCH_MAX_REDIRECTS + 1):
                request = http_client.build_request("GET", current_fetch_url.url)
                raw_response = await http_client.send(
                    request,
                    stream=True,
                    follow_redirects=False,
                )
                response = await _read_bounded_response(
                    raw_response,
                    request_url=current_fetch_url.url,
                )
                if response.status_code not in _WEB_FETCH_REDIRECT_STATUS_CODES:
                    return response
                location = response.headers.get("location")
                if not location:
                    return response
                current_fetch_url = await _validated_fetch_url(
                    urllib.parse.urljoin(str(response.url), location)
                )
            raise HTTPException(
                status_code=400,
                detail="Too many redirects while reading URL",
            )
    except TimeoutError as exc:
        raise HTTPException(status_code=504, detail="Timed out while reading URL") from exc


async def image_dimensions_match(
    payload: dict,
    expected: tuple[int, int],
    http_client: httpx.AsyncClient,
    *,
    gif_requested: bool = False,
    use_proxy_client: bool = False,
) -> bool:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=502, detail="Cannot check image size: response contains no images.")

    async def _check_item(item: object) -> bool:
        try:
            if not isinstance(item, dict):
                raise ValueError("Invalid image item")
            if item.get("b64_json"):
                image_format, image_size = await asyncio.to_thread(
                    _decode_and_inspect_b64_image,
                    item["b64_json"],
                )
            elif item.get("url"):
                url = item["url"]
                if not isinstance(url, str):
                    raise ValueError("Invalid image URL")
                if use_proxy_client:
                    response = await _fetch_image_via_proxy_client(url, http_client)
                else:
                    response, _final_url = await _get_with_public_redirects(url)
                response.raise_for_status()
                content = response.content
                del response
                image_format, image_size = await asyncio.to_thread(_inspect_image_dimensions, content)
                del content
            else:
                raise ValueError("Missing image content")
            if gif_requested and image_format == "GIF":
                # Анимацию провайдер режет из одного листа кадров: размер кадра меньше заказанного
                return True
            return all(
                abs(actual - wanted) <= wanted * SIZE_TOLERANCE
                for actual, wanted in zip(image_size, expected)
            )
        except HTTPException as exc:
            if exc.status_code in {400, 413, 504}:
                raise HTTPException(
                    status_code=502,
                    detail="Cannot read returned image to check its size.",
                ) from exc
            raise
        except (ValueError, TypeError, OSError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail="Cannot read returned image to check its size.") from exc

    if len(items) == 1:
        return await _check_item(items[0])

    semaphore = asyncio.Semaphore(IMAGE_DIMENSIONS_CHECK_CONCURRENCY)

    async def _bounded_check(item: object) -> bool:
        async with semaphore:
            return await _check_item(item)

    tasks = [asyncio.create_task(_bounded_check(item)) for item in items]
    try:
        pending: set[asyncio.Task[bool]] = set(tasks)
        while pending:
            done, pending = await asyncio.wait(
                pending,
                return_when=asyncio.FIRST_EXCEPTION,
            )
            for finished in done:
                finished.result()
    except BaseException as exc:
        for task in tasks:
            if not task.done() and not task.cancelling():
                task.cancel()
        await _drain_tasks(
            *tasks,
            propagate_cancellation=not isinstance(exc, asyncio.CancelledError),
        )
        raise
    return all(task.result() for task in tasks)
