"""Bounded gzip decompression to guard against decompression bombs."""

from __future__ import annotations

import gzip
import io

_CHUNK_SIZE = 64 * 1024


class GzipPayloadTooLarge(ValueError):
    """Raised when a gzip stream expands beyond the configured byte limit."""


def decompress_gzip_bounded(data: bytes, max_bytes: int) -> bytes:
    """Decompress *data* while enforcing an upper bound of *max_bytes* bytes."""
    if max_bytes < 0:
        raise ValueError("max_bytes must be non-negative")

    chunks: list[bytes] = []
    total = 0
    with gzip.GzipFile(fileobj=io.BytesIO(data)) as gz:
        while True:
            to_read = min(_CHUNK_SIZE, max_bytes - total + 1)
            chunk = gz.read(to_read)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                raise GzipPayloadTooLarge(
                    f"Decompressed gzip payload exceeds limit of {max_bytes} bytes"
                )
            chunks.append(chunk)
    return b"".join(chunks)
