"""HTTP-boundary primitives shared by transactional config endpoints."""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from fastapi import Request
from fastapi.responses import JSONResponse

from ...config.config_store import ConfigFile, ConfigSourceBundle
from ...services.config_updates import (
    ConfigRevision,
    ConfigUpdateCoordinator,
    ConfigUpdateError,
    ConfigUpdateErrorCode,
)
from ...services.runtime_config import AppServices, RuntimeSnapshot


__all__ = (
    "ConfigRequestInvalid",
    "RawConfigBody",
    "capture_config_update_runtime",
    "config_error_response",
    "config_response_headers",
    "parse_config_if_match",
    "read_raw_config_body",
)


_UTF8_BOM = b"\xef\xbb\xbf"
_REQUEST_INVALID_CODE = "config_request_invalid"
_REQUEST_INVALID_MESSAGE = "The configuration request is invalid."
_ERROR_HTTP_STATUS = {
    ConfigUpdateErrorCode.VALIDATION_FAILED: 400,
    ConfigUpdateErrorCode.GENERATION_STALE: 409,
    ConfigUpdateErrorCode.REVISION_CONFLICT: 409,
    ConfigUpdateErrorCode.GENERATION_BUSY: 409,
    ConfigUpdateErrorCode.COMMIT_FAILED: 500,
    ConfigUpdateErrorCode.UPDATE_UNAVAILABLE: 503,
    ConfigUpdateErrorCode.UPDATE_BROKEN: 503,
}
_ERROR_MESSAGES = {
    ConfigUpdateErrorCode.VALIDATION_FAILED: "Configuration validation failed.",
    ConfigUpdateErrorCode.GENERATION_STALE: (
        "The configuration generation is stale."
    ),
    ConfigUpdateErrorCode.REVISION_CONFLICT: (
        "The configuration revision changed."
    ),
    ConfigUpdateErrorCode.GENERATION_BUSY: (
        "The previous runtime generation is still retiring."
    ),
    ConfigUpdateErrorCode.COMMIT_FAILED: (
        "The configuration update could not be committed."
    ),
    ConfigUpdateErrorCode.UPDATE_UNAVAILABLE: (
        "Configuration updates are unavailable."
    ),
    ConfigUpdateErrorCode.UPDATE_BROKEN: (
        "Configuration update integrity is not proven."
    ),
}


class ConfigRequestInvalid(ValueError):
    """Credential-safe request grammar failure without raw input details."""

    def __init__(self) -> None:
        super().__init__(_REQUEST_INVALID_MESSAGE)


@dataclass(frozen=True, slots=True)
class RawConfigBody:
    """Exact candidate bytes and BOM-free text used only for validation."""

    original_bytes: bytes = field(repr=False)
    validation_text: str = field(repr=False)


def capture_config_update_runtime(
    request: Request,
) -> tuple[AppServices, RuntimeSnapshot]:
    """Capture the typed process services and request-owned snapshot."""
    if not isinstance(request, Request):
        raise TypeError("request must be a Request")
    try:
        services = request.app.state.services
    except (AttributeError, KeyError, TypeError):
        services = None
    runtime_snapshot = getattr(request.state, "runtime_snapshot", None)
    if not isinstance(services, AppServices) or not isinstance(
        runtime_snapshot,
        RuntimeSnapshot,
    ):
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    coordinator = services.config_update_coordinator
    if not isinstance(coordinator, ConfigUpdateCoordinator):
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    return services, runtime_snapshot


def parse_config_if_match(
    request: Request,
    config_file: ConfigFile,
) -> ConfigRevision | None:
    """Parse one optional strong config revision from raw ASGI headers."""
    if not isinstance(request, Request):
        raise TypeError("request must be a Request")
    if not isinstance(config_file, ConfigFile):
        raise TypeError("config_file must be a ConfigFile")

    values: list[bytes] = []
    for header in request.scope.get("headers") or ():
        try:
            name, value = header
        except (TypeError, ValueError):
            raise ConfigRequestInvalid from None
        if not isinstance(name, bytes) or not isinstance(value, bytes):
            raise ConfigRequestInvalid
        if name.lower() != b"if-match":
            continue
        values.append(value)

    if not values:
        return None
    if len(values) != 1:
        raise ConfigRequestInvalid

    config_id = re.escape(config_file.value.encode("ascii"))
    match = re.fullmatch(
        rb'[ \t]*"'
        + config_id
        + rb':(?:(missing)|sha256:([0-9a-f]{64}))"[ \t]*',
        values[0],
    )
    if match is None:
        raise ConfigRequestInvalid
    digest_bytes = match.group(2)
    digest = digest_bytes.decode("ascii") if digest_bytes is not None else None
    return ConfigRevision(config_file=config_file, digest=digest)


async def read_raw_config_body(request: Request) -> RawConfigBody:
    """Read exact bytes and apply only strict UTF-8 and BOM grammar."""
    if not isinstance(request, Request):
        raise TypeError("request must be a Request")
    original_bytes = await request.body()
    validation_bytes = original_bytes
    if validation_bytes.startswith(_UTF8_BOM):
        validation_bytes = validation_bytes[len(_UTF8_BOM) :]
    if _UTF8_BOM in validation_bytes:
        raise ConfigRequestInvalid
    try:
        validation_text = validation_bytes.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        raise ConfigRequestInvalid from None
    return RawConfigBody(
        original_bytes=original_bytes,
        validation_text=validation_text,
    )


def config_response_headers(
    snapshot: RuntimeSnapshot,
    config_file: ConfigFile,
) -> dict[str, str]:
    """Build exact revision headers from one captured or published snapshot."""
    if not isinstance(snapshot, RuntimeSnapshot):
        raise TypeError("snapshot must be a RuntimeSnapshot")
    if not isinstance(config_file, ConfigFile):
        raise TypeError("config_file must be a ConfigFile")
    bundle = snapshot.config_loader.source_bundle
    if not isinstance(bundle, ConfigSourceBundle):
        raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
    document = bundle[config_file]
    if document.exists:
        digest = document.digest
        if (
            not isinstance(digest, str)
            or len(digest) != 64
            or any(character not in "0123456789abcdef" for character in digest)
        ):
            raise ConfigUpdateError(ConfigUpdateErrorCode.UPDATE_UNAVAILABLE)
        revision = f"sha256:{digest}"
    else:
        revision = "missing"
    return {
        "ETag": f'"{config_file.value}:{revision}"',
        "X-Config-Generation": str(snapshot.generation),
        "Cache-Control": "no-store",
    }


def config_error_response(
    error: ConfigRequestInvalid | ConfigUpdateError,
) -> JSONResponse:
    """Map only known config failures to the frozen credential-safe body."""
    if isinstance(error, ConfigRequestInvalid):
        status_code = 400
        code = _REQUEST_INVALID_CODE
        message = _REQUEST_INVALID_MESSAGE
        errors = None
    elif isinstance(error, ConfigUpdateError):
        status_code = _ERROR_HTTP_STATUS[error.code]
        code = error.code.value
        message = _ERROR_MESSAGES[error.code]
        errors = error.errors
    else:
        raise TypeError(
            "error must be a ConfigRequestInvalid or ConfigUpdateError"
        )
    detail: dict[str, object] = {"code": code, "message": message}
    if errors:
        detail["errors"] = errors
    return JSONResponse(
        status_code=status_code,
        content={"detail": detail},
    )
