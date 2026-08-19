"""Strict JSON protocol shared by deep-research parent and child processes."""

from __future__ import annotations

import asyncio
import json
import math
import re
import socket
import struct
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Mapping, TypeAlias
from urllib.parse import urlsplit


DEEP_RESEARCH_PROTOCOL_VERSION = 1
# One result frame carries the full raw text of every scraped source, so a wide
# deep-research run outgrew the previous 8 MiB ceiling and died at the last step.
DEEP_RESEARCH_FRAME_MAX_BYTES = 16 * 1024 * 1024
_FRAME_HEADER = struct.Struct("!I")
_JOB_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_MESSAGE_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
_ERROR_TOKEN_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_]{0,63}$")
DEEP_RESEARCH_TERMINAL_MESSAGE_ID = "terminal"

JsonValue: TypeAlias = (
    None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]
)


class DeepResearchProtocolError(RuntimeError):
    """The peer sent data outside the exact protocol schema."""


class DeepResearchChildReportedError(DeepResearchProtocolError):
    def __init__(self, code: str, exception_type: str) -> None:
        self.code = code
        self.exception_type = exception_type
        super().__init__(f"deep-research child reported {code} ({exception_type})")


class DeepResearchCallbackReportedError(DeepResearchProtocolError):
    def __init__(self, code: str, exception_type: str) -> None:
        self.code = code
        self.exception_type = exception_type
        super().__init__(f"deep-research callback reported {code} ({exception_type})")


class DeepResearchCallbackOperation(StrEnum):
    SEARCH = "search"
    READ = "read"
    IMAGE = "image"


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchJob:
    """Serializable GPT Researcher job accepted by the spawn runner."""

    job_id: str
    query: str
    fast_model: str
    smart_model: str
    strategic_model: str
    embedding_model: str | None = None
    gateway_base_url: str | None = None
    gateway_api_key: str | None = None
    report_type: str = "deep"
    max_words: int = 2500
    breadth: int = 4
    depth: int = 2
    concurrency: int = 6
    language: str = "English"
    verbose: bool = False
    image_generation_enabled: bool = False
    image_generation_model: str | None = None
    image_generation_size: str | None = None

    def __post_init__(self) -> None:
        if not isinstance(self.job_id, str) or _JOB_ID_RE.fullmatch(self.job_id) is None:
            raise ValueError("job_id must use 1-128 safe ASCII characters")
        for name in (
            "query",
            "fast_model",
            "smart_model",
            "strategic_model",
            "report_type",
            "language",
        ):
            _require_non_empty_text(name, getattr(self, name))
        for name in (
            "embedding_model",
            "gateway_base_url",
            "gateway_api_key",
            "image_generation_model",
            "image_generation_size",
        ):
            value = getattr(self, name)
            if value is not None:
                _require_non_empty_text(name, value)
        for name in ("max_words", "breadth", "depth", "concurrency"):
            _require_positive_int(name, getattr(self, name))
        if not isinstance(self.verbose, bool):
            raise ValueError("verbose must be a boolean")
        if not isinstance(self.image_generation_enabled, bool):
            raise ValueError("image_generation_enabled must be a boolean")
        if self.image_generation_enabled and (
            self.image_generation_model is None
            or self.image_generation_size is None
        ):
            raise ValueError(
                "enabled image generation requires a model and size"
            )
        for model in (
            self.fast_model,
            self.smart_model,
            self.strategic_model,
            self.embedding_model,
            self.image_generation_model,
        ):
            if model is not None and ":" in model:
                raise ValueError(
                    "Deep research model names must be gateway model ids without provider prefixes."
                )

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "job_id": self.job_id,
            "query": self.query,
            "fast_model": self.fast_model,
            "smart_model": self.smart_model,
            "strategic_model": self.strategic_model,
            "embedding_model": self.embedding_model,
            "gateway_base_url": self.gateway_base_url,
            "gateway_api_key": self.gateway_api_key,
            "report_type": self.report_type,
            "max_words": self.max_words,
            "breadth": self.breadth,
            "depth": self.depth,
            "concurrency": self.concurrency,
            "language": self.language,
            "verbose": self.verbose,
            "image_generation_enabled": self.image_generation_enabled,
            "image_generation_model": self.image_generation_model,
            "image_generation_size": self.image_generation_size,
        }

    @classmethod
    def from_wire(cls, value: object) -> DeepResearchJob:
        mapping = _exact_object(value, set(cls.__dataclass_fields__))
        try:
            return cls(**mapping)  # type: ignore[arg-type]
        except (TypeError, ValueError) as exc:
            raise DeepResearchProtocolError("invalid deep-research job") from exc

    def environment(self) -> dict[str, str]:
        values = {
            "FAST_LLM": _as_gpt_researcher_llm(self.fast_model),
            "SMART_LLM": _as_gpt_researcher_llm(self.smart_model),
            "STRATEGIC_LLM": _as_gpt_researcher_llm(self.strategic_model),
            "TOTAL_WORDS": str(self.max_words),
            "DEEP_RESEARCH_BREADTH": str(self.breadth),
            "DEEP_RESEARCH_DEPTH": str(self.depth),
            "DEEP_RESEARCH_CONCURRENCY": str(self.concurrency),
            "LANGUAGE": self.language.strip(),
            "IMAGE_GENERATION_ENABLED": (
                "true" if self.image_generation_enabled else "false"
            ),
        }
        if self.embedding_model is not None:
            values["EMBEDDING"] = f"custom:{self.embedding_model.strip()}"
        if self.gateway_base_url is not None:
            values["OPENAI_BASE_URL"] = self.gateway_base_url.rstrip("/")
        if self.gateway_api_key is not None:
            values["OPENAI_API_KEY"] = self.gateway_api_key
        if self.image_generation_model is not None:
            values["IMAGE_GENERATION_MODEL"] = self.image_generation_model.strip()
        if self.image_generation_size is not None:
            values["IMAGE_GENERATION_SIZE"] = self.image_generation_size.strip()
        if self.image_generation_enabled:
            values["IMAGE_GENERATION_PROVIDER"] = "gateway"
        return values


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchResult:
    query: str
    report: str
    research_result: JsonValue
    sources: tuple[dict[str, JsonValue], ...]
    source_urls: tuple[str, ...]
    context: tuple[JsonValue, ...]
    costs: float | None
    generated_images: tuple[dict[str, str], ...] = ()

    def __post_init__(self) -> None:
        _require_non_empty_text("query", self.query)
        if not isinstance(self.report, str):
            raise ValueError("report must be a string")
        _validate_json_value(self.research_result)
        if not isinstance(self.sources, tuple):
            raise ValueError("sources must be a tuple")
        for source in self.sources:
            if not isinstance(source, dict):
                raise ValueError("each source must be an object")
            _validate_json_value(source)
        if not isinstance(self.source_urls, tuple) or not all(
            isinstance(url, str) for url in self.source_urls
        ):
            raise ValueError("source_urls must be a tuple of strings")
        if not isinstance(self.context, tuple):
            raise ValueError("context must be a tuple")
        for item in self.context:
            _validate_json_value(item)
        if self.costs is not None:
            if isinstance(self.costs, bool) or not isinstance(self.costs, (int, float)):
                raise ValueError("costs must be a non-negative finite number or null")
            if not math.isfinite(float(self.costs)) or self.costs < 0:
                raise ValueError("costs must be a non-negative finite number or null")
            object.__setattr__(self, "costs", float(self.costs))
        if not isinstance(self.generated_images, tuple):
            raise ValueError("generated_images must be a tuple")
        for image in self.generated_images:
            _validate_safe_image_metadata(image)

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "query": self.query,
            "report": self.report,
            "research_result": self.research_result,
            "sources": list(self.sources),
            "source_urls": list(self.source_urls),
            "context": list(self.context),
            "costs": self.costs,
            "generated_images": list(self.generated_images),
        }

    def to_public_dict(self) -> dict[str, object]:
        return {
            "success": True,
            **self.to_wire(),
        }

    @classmethod
    def from_wire(cls, value: object) -> DeepResearchResult:
        fields = {
            "query",
            "report",
            "research_result",
            "sources",
            "source_urls",
            "context",
            "costs",
            "generated_images",
        }
        mapping = _exact_object(value, fields)
        sources = mapping["sources"]
        source_urls = mapping["source_urls"]
        context = mapping["context"]
        generated_images = mapping["generated_images"]
        if (
            not isinstance(sources, list)
            or not isinstance(source_urls, list)
            or not isinstance(context, list)
            or not isinstance(generated_images, list)
        ):
            raise DeepResearchProtocolError("invalid deep-research result collections")
        try:
            return cls(
                query=mapping["query"],  # type: ignore[arg-type]
                report=mapping["report"],  # type: ignore[arg-type]
                research_result=mapping["research_result"],  # type: ignore[arg-type]
                sources=tuple(sources),  # type: ignore[arg-type]
                source_urls=tuple(source_urls),  # type: ignore[arg-type]
                context=tuple(context),  # type: ignore[arg-type]
                costs=mapping["costs"],  # type: ignore[arg-type]
                generated_images=tuple(generated_images),  # type: ignore[arg-type]
            )
        except (TypeError, ValueError) as exc:
            raise DeepResearchProtocolError("invalid deep-research result") from exc


@dataclass(frozen=True, slots=True, repr=False)
class DeepResearchCallbackRequest:
    job_id: str
    message_id: str
    operation: DeepResearchCallbackOperation
    arguments: Mapping[str, JsonValue]

    def __post_init__(self) -> None:
        _validate_identity(self.job_id, self.message_id)
        if self.message_id == DEEP_RESEARCH_TERMINAL_MESSAGE_ID:
            raise DeepResearchProtocolError(
                "callback message id is reserved for terminal messages"
            )
        try:
            operation = DeepResearchCallbackOperation(self.operation)
        except (TypeError, ValueError):
            raise DeepResearchProtocolError("invalid callback operation") from None
        arguments = dict(self.arguments)
        _validate_callback_arguments(operation, arguments)
        object.__setattr__(self, "operation", operation)
        object.__setattr__(self, "arguments", MappingProxyType(arguments))

    def to_wire(self) -> dict[str, JsonValue]:
        return {
            "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
            "type": "callback_request",
            "job_id": self.job_id,
            "message_id": self.message_id,
            "operation": self.operation.value,
            "arguments": dict(self.arguments),
        }


def encode_frame(message: object) -> bytes:
    _validate_json_value(message)
    try:
        payload = json.dumps(
            message,
            ensure_ascii=False,
            allow_nan=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
        raise DeepResearchProtocolError("message is not strict UTF-8 JSON") from exc
    if not payload or len(payload) > DEEP_RESEARCH_FRAME_MAX_BYTES:
        raise DeepResearchProtocolError("deep-research frame size is invalid")
    return _FRAME_HEADER.pack(len(payload)) + payload


def recv_frame(sock: socket.socket) -> JsonValue:
    header = _recv_exact(sock, _FRAME_HEADER.size)
    size = _FRAME_HEADER.unpack(header)[0]
    if size <= 0 or size > DEEP_RESEARCH_FRAME_MAX_BYTES:
        raise DeepResearchProtocolError("deep-research frame size is invalid")
    return decode_payload(_recv_exact(sock, size))


async def send_async_frame(sock: socket.socket, message: object) -> None:
    await asyncio.get_running_loop().sock_sendall(sock, encode_frame(message))


async def recv_async_frame(sock: socket.socket) -> JsonValue:
    header = await _recv_async_exact(sock, _FRAME_HEADER.size)
    size = _FRAME_HEADER.unpack(header)[0]
    if size <= 0 or size > DEEP_RESEARCH_FRAME_MAX_BYTES:
        raise DeepResearchProtocolError("deep-research frame size is invalid")
    return decode_payload(await _recv_async_exact(sock, size))


def decode_payload(payload: bytes) -> JsonValue:
    def reject_constant(_value: str) -> None:
        raise ValueError("non-finite number")

    def exact_object(pairs: list[tuple[str, JsonValue]]) -> dict[str, JsonValue]:
        result: dict[str, JsonValue] = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("duplicate object key")
            result[key] = value
        return result

    try:
        value = json.loads(
            payload.decode("utf-8"),
            parse_constant=reject_constant,
            object_pairs_hook=exact_object,
        )
        _validate_json_value(value)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise DeepResearchProtocolError("invalid strict JSON payload") from exc
    return value


def ready_message(pid: int) -> dict[str, JsonValue]:
    return {"protocol": DEEP_RESEARCH_PROTOCOL_VERSION, "type": "ready", "pid": pid}


def parse_ready_message(value: object, *, expected_pid: int) -> None:
    mapping = _exact_object(value, {"protocol", "type", "pid"})
    if mapping != ready_message(expected_pid):
        raise DeepResearchProtocolError("invalid child ready message")


def run_message(job: DeepResearchJob) -> dict[str, JsonValue]:
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "run",
        "job": job.to_wire(),
    }


def parse_run_message(value: object) -> DeepResearchJob:
    mapping = _exact_object(value, {"protocol", "type", "job"})
    if mapping["protocol"] != DEEP_RESEARCH_PROTOCOL_VERSION or mapping["type"] != "run":
        raise DeepResearchProtocolError("invalid run message")
    return DeepResearchJob.from_wire(mapping["job"])


def result_message(job_id: str, result: DeepResearchResult) -> dict[str, JsonValue]:
    _validate_identity(job_id, DEEP_RESEARCH_TERMINAL_MESSAGE_ID)
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "result",
        "job_id": job_id,
        "message_id": DEEP_RESEARCH_TERMINAL_MESSAGE_ID,
        "result": result.to_wire(),
    }


def error_message(job_id: str, code: str, exception_type: str) -> dict[str, JsonValue]:
    _validate_identity(job_id, DEEP_RESEARCH_TERMINAL_MESSAGE_ID)
    if _ERROR_TOKEN_RE.fullmatch(code) is None or _ERROR_TOKEN_RE.fullmatch(exception_type) is None:
        raise DeepResearchProtocolError("invalid child error token")
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "error",
        "job_id": job_id,
        "message_id": DEEP_RESEARCH_TERMINAL_MESSAGE_ID,
        "code": code,
        "exception_type": exception_type,
    }


def cancel_message(job_id: str) -> dict[str, JsonValue]:
    _validate_identity(job_id, DEEP_RESEARCH_TERMINAL_MESSAGE_ID)
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "cancel",
        "job_id": job_id,
        "message_id": DEEP_RESEARCH_TERMINAL_MESSAGE_ID,
    }


def callback_request_message(
    request: DeepResearchCallbackRequest,
) -> dict[str, JsonValue]:
    if not isinstance(request, DeepResearchCallbackRequest):
        raise DeepResearchProtocolError("invalid callback request")
    return request.to_wire()


def parse_callback_request(value: object) -> DeepResearchCallbackRequest:
    mapping = _exact_object(
        value,
        {
            "protocol",
            "type",
            "job_id",
            "message_id",
            "operation",
            "arguments",
        },
    )
    if (
        mapping["protocol"] != DEEP_RESEARCH_PROTOCOL_VERSION
        or mapping["type"] != "callback_request"
    ):
        raise DeepResearchProtocolError("invalid callback request message")
    arguments = mapping["arguments"]
    if not isinstance(arguments, dict):
        raise DeepResearchProtocolError("invalid callback arguments")
    try:
        operation = DeepResearchCallbackOperation(mapping["operation"])
        return DeepResearchCallbackRequest(
            job_id=mapping["job_id"],  # type: ignore[arg-type]
            message_id=mapping["message_id"],  # type: ignore[arg-type]
            operation=operation,
            arguments=arguments,
        )
    except (TypeError, ValueError) as exc:
        raise DeepResearchProtocolError("invalid callback request") from exc


def callback_result_message(
    request: DeepResearchCallbackRequest,
    result: JsonValue,
) -> dict[str, JsonValue]:
    _validate_callback_result(request.operation, result)
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "callback_result",
        "job_id": request.job_id,
        "message_id": request.message_id,
        "operation": request.operation.value,
        "result": result,
    }


def callback_error_message(
    request: DeepResearchCallbackRequest,
    code: str,
    exception_type: str,
) -> dict[str, JsonValue]:
    if (
        _ERROR_TOKEN_RE.fullmatch(code) is None
        or _ERROR_TOKEN_RE.fullmatch(exception_type) is None
    ):
        raise DeepResearchProtocolError("invalid callback error token")
    return {
        "protocol": DEEP_RESEARCH_PROTOCOL_VERSION,
        "type": "callback_error",
        "job_id": request.job_id,
        "message_id": request.message_id,
        "operation": request.operation.value,
        "code": code,
        "exception_type": exception_type,
    }


def parse_callback_response(
    value: object,
    *,
    request: DeepResearchCallbackRequest,
) -> JsonValue:
    if not isinstance(value, dict):
        raise DeepResearchProtocolError("callback response must be an object")
    message_type = value.get("type")
    common_fields = {
        "protocol",
        "type",
        "job_id",
        "message_id",
        "operation",
    }
    if message_type == "callback_result":
        mapping = _exact_object(value, common_fields | {"result"})
        _validate_callback_response_identity(mapping, request=request)
        result = mapping["result"]
        _validate_callback_result(request.operation, result)
        return result
    if message_type == "callback_error":
        mapping = _exact_object(
            value,
            common_fields | {"code", "exception_type"},
        )
        _validate_callback_response_identity(mapping, request=request)
        code = mapping["code"]
        exception_type = mapping["exception_type"]
        if (
            not isinstance(code, str)
            or not isinstance(exception_type, str)
            or _ERROR_TOKEN_RE.fullmatch(code) is None
            or _ERROR_TOKEN_RE.fullmatch(exception_type) is None
        ):
            raise DeepResearchProtocolError("invalid callback error message")
        raise DeepResearchCallbackReportedError(code, exception_type)
    raise DeepResearchProtocolError("invalid callback response type")


def parse_terminal_message(value: object, *, job_id: str) -> DeepResearchResult:
    if not isinstance(value, dict):
        raise DeepResearchProtocolError("terminal message must be an object")
    message_type = value.get("type")
    if message_type == "result":
        mapping = _exact_object(
            value,
            {"protocol", "type", "job_id", "message_id", "result"},
        )
        if not _is_terminal_identity(mapping, job_id=job_id):
            raise DeepResearchProtocolError("invalid result identity")
        return DeepResearchResult.from_wire(mapping["result"])
    if message_type == "error":
        mapping = _exact_object(
            value,
            {
                "protocol",
                "type",
                "job_id",
                "message_id",
                "code",
                "exception_type",
            },
        )
        if not _is_terminal_identity(mapping, job_id=job_id):
            raise DeepResearchProtocolError("invalid error identity")
        code = mapping["code"]
        exception_type = mapping["exception_type"]
        if (
            not isinstance(code, str)
            or not isinstance(exception_type, str)
            or _ERROR_TOKEN_RE.fullmatch(code) is None
            or _ERROR_TOKEN_RE.fullmatch(exception_type) is None
        ):
            raise DeepResearchProtocolError("invalid child error message")
        raise DeepResearchChildReportedError(code, exception_type)
    if message_type == "cancel":
        mapping = _exact_object(
            value,
            {"protocol", "type", "job_id", "message_id"},
        )
        if not _is_terminal_identity(mapping, job_id=job_id):
            raise DeepResearchProtocolError("invalid cancellation identity")
        raise DeepResearchChildReportedError("child_cancelled", "CancelledError")
    raise DeepResearchProtocolError("invalid terminal message type")


def _recv_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    while len(chunks) < size:
        chunk = sock.recv(size - len(chunks))
        if not chunk:
            raise DeepResearchProtocolError("unexpected EOF in deep-research frame")
        chunks.extend(chunk)
    return bytes(chunks)


async def _recv_async_exact(sock: socket.socket, size: int) -> bytes:
    chunks = bytearray()
    loop = asyncio.get_running_loop()
    while len(chunks) < size:
        chunk = await loop.sock_recv(sock, size - len(chunks))
        if not chunk:
            raise DeepResearchProtocolError("unexpected EOF in deep-research frame")
        chunks.extend(chunk)
    return bytes(chunks)


def _exact_object(value: object, expected_fields: set[str]) -> dict[str, JsonValue]:
    if not isinstance(value, dict) or set(value) != expected_fields:
        raise DeepResearchProtocolError("message has an invalid field set")
    if not all(isinstance(key, str) for key in value):
        raise DeepResearchProtocolError("message keys must be strings")
    return value  # type: ignore[return-value]


def _validate_identity(job_id: object, message_id: object) -> None:
    if not isinstance(job_id, str) or _JOB_ID_RE.fullmatch(job_id) is None:
        raise DeepResearchProtocolError("invalid deep-research job id")
    if (
        not isinstance(message_id, str)
        or _MESSAGE_ID_RE.fullmatch(message_id) is None
    ):
        raise DeepResearchProtocolError("invalid deep-research message id")


def _validate_callback_arguments(
    operation: DeepResearchCallbackOperation,
    arguments: dict[str, JsonValue],
) -> None:
    if operation is DeepResearchCallbackOperation.SEARCH:
        mapping = _exact_object(arguments, {"query", "max_results"})
        _require_non_empty_text("query", mapping["query"])
        _require_positive_int("max_results", mapping["max_results"])
        return
    if operation is DeepResearchCallbackOperation.READ:
        mapping = _exact_object(arguments, {"url"})
        _require_non_empty_text("url", mapping["url"])
        return
    mapping = _exact_object(
        arguments,
        {
            "prompt",
            "context",
            "research_id",
            "aspect_ratio",
            "num_images",
            "style",
        },
    )
    _require_non_empty_text("prompt", mapping["prompt"])
    for name in ("context", "research_id", "aspect_ratio", "style"):
        value = mapping[name]
        if not isinstance(value, str):
            raise DeepResearchProtocolError(f"{name} must be a string")
        _validate_json_value(value)
    _require_positive_int("num_images", mapping["num_images"])


def _validate_callback_result(
    operation: DeepResearchCallbackOperation,
    result: object,
) -> None:
    if operation is DeepResearchCallbackOperation.SEARCH:
        if not isinstance(result, list):
            raise DeepResearchProtocolError("search callback result must be a list")
        for item in result:
            mapping = _exact_object(item, {"url", "title", "snippet"})
            _require_non_empty_text("url", mapping["url"])
            for name in ("title", "snippet"):
                if not isinstance(mapping[name], str):
                    raise DeepResearchProtocolError(
                        f"search callback {name} must be a string"
                    )
        return
    if operation is DeepResearchCallbackOperation.READ:
        mapping = _exact_object(result, {"url", "title", "content"})
        _require_non_empty_text("url", mapping["url"])
        for name in ("title", "content"):
            if not isinstance(mapping[name], str):
                raise DeepResearchProtocolError(
                    f"read callback {name} must be a string"
                )
        return
    if not isinstance(result, list):
        raise DeepResearchProtocolError("image callback result must be a list")
    for image in result:
        _validate_safe_image_metadata(image)


def _validate_safe_image_metadata(value: object) -> None:
    mapping = _exact_object(value, {"url", "prompt", "alt_text"})
    _require_non_empty_text("url", mapping["url"])
    for name in ("prompt", "alt_text"):
        if not isinstance(mapping[name], str):
            raise DeepResearchProtocolError(
                f"image callback {name} must be a string"
            )
    url = mapping["url"]
    assert isinstance(url, str)
    parsed = urlsplit(url)
    published_segments = url.removeprefix("/outputs/images/").split("/")
    is_published_url = (
        url.startswith("/outputs/images/")
        and all(segment not in {"", ".", ".."} for segment in published_segments)
        and "\\" not in url
    )
    is_http_url = parsed.scheme.lower() in {"http", "https"} and bool(parsed.netloc)
    if not is_published_url and not is_http_url:
        raise DeepResearchProtocolError("image metadata contains a filesystem path")


def _validate_callback_response_identity(
    mapping: dict[str, JsonValue],
    *,
    request: DeepResearchCallbackRequest,
) -> None:
    expected = (
        DEEP_RESEARCH_PROTOCOL_VERSION,
        request.job_id,
        request.message_id,
        request.operation.value,
    )
    actual = (
        mapping["protocol"],
        mapping["job_id"],
        mapping["message_id"],
        mapping["operation"],
    )
    if actual != expected:
        raise DeepResearchProtocolError("invalid callback response identity")


def _is_terminal_identity(
    mapping: dict[str, JsonValue],
    *,
    job_id: str,
) -> bool:
    return (
        mapping["protocol"] == DEEP_RESEARCH_PROTOCOL_VERSION
        and mapping["job_id"] == job_id
        and mapping["message_id"] == DEEP_RESEARCH_TERMINAL_MESSAGE_ID
    )


def _require_non_empty_text(name: str, value: object) -> None:
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a non-empty string")
    try:
        value.encode("utf-8")
    except UnicodeError as exc:
        raise ValueError(f"{name} must be valid UTF-8") from exc


def _require_positive_int(name: str, value: object) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(f"{name} must be a positive integer")


def _as_gpt_researcher_llm(model: str) -> str:
    return f"openai:{model.strip()}"


def _validate_json_value(value: object, *, depth: int = 0) -> None:
    if depth > 64:
        raise DeepResearchProtocolError("JSON nesting is too deep")
    if value is None or isinstance(value, (bool, str)):
        if isinstance(value, str):
            try:
                value.encode("utf-8")
            except UnicodeError as exc:
                raise DeepResearchProtocolError("JSON string is not valid UTF-8") from exc
        return
    if isinstance(value, int):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise DeepResearchProtocolError("JSON number must be finite")
        return
    if isinstance(value, list | tuple):
        for item in value:
            _validate_json_value(item, depth=depth + 1)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise DeepResearchProtocolError("JSON object keys must be strings")
            _validate_json_value(key, depth=depth + 1)
            _validate_json_value(item, depth=depth + 1)
        return
    raise DeepResearchProtocolError("value is not strict JSON")
