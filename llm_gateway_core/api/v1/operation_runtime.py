from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import TYPE_CHECKING, BinaryIO, Literal, TypeAlias, cast

from fastapi import HTTPException, Request, UploadFile
from starlette.datastructures import UploadFile as StarletteUploadFile

from ...services.upload_admission import (
    UploadAdmissionClosed,
    UploadAdmissionTimeout,
    UploadAdmissionTooLarge,
)

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices

UPLOAD_READ_CHUNK_SIZE = 1024 * 1024
UPLOAD_HEADER_MAX_BYTES = 16
UploadKind = Literal["audio", "image", "pdf"]
MultipartFileContent: TypeAlias = bytes | BinaryIO
MultipartFilePayload: TypeAlias = tuple[str, MultipartFileContent, str | None]


@dataclass(frozen=True, slots=True)
class ValidatedUpload:
    """Validated metadata and the original Starlette-owned seekable spool."""

    filename: str
    content_type: str | None
    size: int
    file: BinaryIO

    def as_multipart_file(self) -> MultipartFilePayload:
        return self.filename, self.file, self.content_type

    def __deepcopy__(self, _memo: dict[int, object]) -> ValidatedUpload:
        return self


@asynccontextmanager
async def admit_upload_processing(
    request: Request,
    weight: int,
) -> AsyncIterator[None]:
    """Hold one process upload lease for the complete selected-route attempt."""
    services = cast("AppServices", request.app.state.services)
    try:
        lease = await services.upload_admission.acquire(
            weight,
            timeout_seconds=services.upload_admission_timeout_seconds,
        )
    except UploadAdmissionTooLarge as exc:
        raise HTTPException(
            status_code=413,
            detail="Upload requires too much processing memory.",
        ) from exc
    except (UploadAdmissionTimeout, UploadAdmissionClosed) as exc:
        raise HTTPException(
            status_code=503,
            detail="Upload processing capacity is unavailable.",
        ) from exc

    try:
        yield
    finally:
        lease.release()


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


def _validate_limit(value: int, *, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")
    return value


async def validate_upload_limited(
    value: UploadFile | StarletteUploadFile,
    *,
    default_filename: str,
    kind: UploadKind,
    max_bytes: int,
) -> ValidatedUpload:
    """Validate size and magic header without retaining a second full copy."""
    limit = _validate_limit(max_bytes, name="max_bytes")
    await value.seek(0)
    declared_size = value.size
    if declared_size is not None:
        if (
            isinstance(declared_size, bool)
            or not isinstance(declared_size, int)
            or declared_size < 0
        ):
            raise HTTPException(status_code=400, detail="Invalid uploaded file size.")
        if declared_size > limit:
            raise HTTPException(status_code=413, detail="Uploaded file too large.")
        try:
            header = await value.read(UPLOAD_HEADER_MAX_BYTES)
        finally:
            await value.seek(0)
        size = declared_size
    else:
        size = 0
        header_buffer = bytearray()
        try:
            while True:
                chunk = await value.read(UPLOAD_READ_CHUNK_SIZE)
                if not chunk:
                    break
                size += len(chunk)
                if size > limit:
                    raise HTTPException(
                        status_code=413,
                        detail="Uploaded file too large.",
                    )
                if len(header_buffer) < UPLOAD_HEADER_MAX_BYTES:
                    remaining = UPLOAD_HEADER_MAX_BYTES - len(header_buffer)
                    header_buffer.extend(chunk[:remaining])
        finally:
            await value.seek(0)
        header = bytes(header_buffer)

    _validate_upload_content(kind, header, value.content_type)
    return ValidatedUpload(
        filename=value.filename or default_filename,
        content_type=value.content_type,
        size=size,
        file=value.file,
    )


def validate_upload_total(
    uploads: list[ValidatedUpload] | tuple[ValidatedUpload, ...],
    *,
    max_bytes: int,
) -> int:
    """Return the exact aggregate size or reject an oversized upload set."""
    limit = _validate_limit(max_bytes, name="max_bytes")
    total = 0
    for upload in uploads:
        total += upload.size
        if total > limit:
            raise HTTPException(
                status_code=413,
                detail="Uploaded files total too large.",
            )
    return total
