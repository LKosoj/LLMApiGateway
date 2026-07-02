from __future__ import annotations

from typing import Literal

from fastapi import HTTPException, UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...config.settings import settings

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024
UploadKind = Literal["audio", "image", "pdf"]


def _content_type_base(content_type: str | None) -> str:
    return (content_type or "").split(";", 1)[0].strip().lower()


def _is_wav(data: bytes) -> bool:
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WAVE"


def _is_mp3(data: bytes) -> bool:
    return data.startswith(b"ID3") or (len(data) >= 2 and data[0] == 0xFF and (data[1] & 0xE0) == 0xE0)


def _is_mp4_family(data: bytes) -> bool:
    return len(data) >= 12 and data[4:8] == b"ftyp"


def _is_webp(data: bytes) -> bool:
    return len(data) >= 12 and data.startswith(b"RIFF") and data[8:12] == b"WEBP"


def _validate_upload_content(kind: UploadKind, content: bytes, content_type: str | None) -> None:
    content_type_base = _content_type_base(content_type)
    if kind == "pdf":
        if content_type_base not in {"application/pdf", "application/octet-stream", ""}:
            raise HTTPException(status_code=400, detail="Unsupported PDF file content type.")
        if not content.startswith(b"%PDF-"):
            raise HTTPException(status_code=400, detail="Invalid PDF file content.")
        return

    if kind == "image":
        if content_type_base and not (
            content_type_base.startswith("image/")
            or content_type_base == "application/octet-stream"
        ):
            raise HTTPException(status_code=400, detail="Unsupported image file content type.")
        if (
            content.startswith(b"\x89PNG\r\n\x1a\n")
            or content.startswith(b"\xff\xd8\xff")
            or _is_webp(content)
        ):
            return
        raise HTTPException(status_code=400, detail="Invalid image file content.")

    if content_type_base and not (
        content_type_base.startswith("audio/")
        or content_type_base in {
            "application/octet-stream",
            "video/webm",
            "video/mp4",
            "application/mp4",
            "application/ogg",
        }
    ):
        raise HTTPException(status_code=400, detail="Unsupported audio file content type.")
    if (
        _is_wav(content)
        or _is_mp3(content)
        or content.startswith(b"OggS")
        or content.startswith(b"fLaC")
        or content.startswith(b"\x1aE\xdf\xa3")
        or _is_mp4_family(content)
    ):
        return
    raise HTTPException(status_code=400, detail="Invalid audio file content.")


async def serialize_upload_limited(
    value: UploadFile | StarletteUploadFile,
    *,
    default_filename: str,
    kind: UploadKind,
    max_bytes: int | None = None,
) -> tuple[str, bytes, str | None]:
    limit = max_bytes if max_bytes is not None else settings.max_request_body_bytes
    chunks: list[bytes] = []
    total_size = 0

    while True:
        chunk = await value.read(UPLOAD_READ_CHUNK_SIZE)
        if not chunk:
            break
        total_size += len(chunk)
        if total_size > limit:
            raise HTTPException(status_code=413, detail="Uploaded file too large.")
        chunks.append(chunk)

    content = b"".join(chunks)
    content_type = value.content_type
    _validate_upload_content(kind, content, content_type)
    return value.filename or default_filename, content, content_type
