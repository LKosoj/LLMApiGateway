import base64
import io

import httpx
from fastapi import HTTPException
from PIL import Image

from .image_adapters import SIZE_DIMENSIONS_RE

# Провайдеры округляют размер по-своему (1093x1920 вместо 1088x1920): допускаем отклонение каждой стороны.
SIZE_TOLERANCE = 0.10


def requested_image_dimensions(payload: dict[str, object]) -> tuple[int, int] | None:
    size = payload.get("size")
    if size is None or size == "auto":
        return None
    match = SIZE_DIMENSIONS_RE.fullmatch(size) if isinstance(size, str) else None
    if match is None or any(int(value) <= 0 for value in match.groups()):
        raise HTTPException(status_code=400, detail="Field 'size' must use positive WIDTHxHEIGHT dimensions or 'auto'.")
    return int(match.group(1)), int(match.group(2))


async def image_dimensions_match(
    payload: dict,
    expected: tuple[int, int],
    http_client: httpx.AsyncClient,
) -> bool:
    items = payload.get("data")
    if not isinstance(items, list) or not items:
        raise HTTPException(status_code=502, detail="Cannot check image size: response contains no images.")
    matches = True
    for item in items:
        try:
            if not isinstance(item, dict):
                raise ValueError("Invalid image item")
            if item.get("b64_json"):
                content = base64.b64decode(item["b64_json"], validate=True)
            elif item.get("url"):
                response = await http_client.get(item["url"], follow_redirects=True)
                response.raise_for_status()
                content = response.content
                del response
            else:
                raise ValueError("Missing image content")
            with Image.open(io.BytesIO(content)) as image:
                matches = all(abs(actual - wanted) <= wanted * SIZE_TOLERANCE
                              for actual, wanted in zip(image.size, expected)) and matches
            del content
        except (ValueError, TypeError, OSError, httpx.HTTPError) as exc:
            raise HTTPException(status_code=502, detail="Cannot read returned image to check its size.") from exc
    return matches
