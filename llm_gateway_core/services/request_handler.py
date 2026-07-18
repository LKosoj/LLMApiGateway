import asyncio
import codecs
import copy
import hashlib
import httpx
import logging
import time
import zlib
from collections import deque
from collections.abc import Awaitable, Callable, MutableMapping
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Any, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.active_requests import update_active_request_from_state
from llm_gateway_core.services.error_classifier import (
    CONTEXT_OVERFLOW_CONTEXT_MARKERS,
    CONTEXT_OVERFLOW_MAXIMUM_CONTEXT_PATTERN,
    CONTEXT_OVERFLOW_TOKEN_VALUE_PATTERN,
    _is_context_overflow_error,
)
from llm_gateway_core.services.model_policy import resolve_model_name
from llm_gateway_core.services.payload_transform import apply_payload_transforms
from llm_gateway_core.services.stream_observation import (
    SSEEvent,
    SSEFramer,
    SSEFramingError,
    StreamObservationCapacity,
    StreamObservationError,
    StreamObservationLease,
    StreamObservationStateError,
    parse_sse_json,
)
from llm_gateway_core.utils.log_redaction import safe_exception_type_name
from llm_gateway_core.utils.text_sanitize import sanitize_payload

DEFAULT_RETRY_COUNT = 0
DEFAULT_RETRY_DELAY_SECONDS = 0.0
# Upstream Retry-After hints beyond this threshold would deadlock the gateway waiting
# on a single slow provider. Clamp to the same 120s ceiling that bounds retry_delay.
MAX_RETRY_AFTER_SECONDS = 120.0
STREAM_RAW_CHUNK_MAX_BYTES = 64 * 1024
STREAM_ACCEPT_ENCODING = "gzip, deflate, identity"


class StreamPayloadError(ValueError):
    """A complete upstream SSE data event is not valid JSON."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"Upstream SSE payload is invalid: reason={reason_code}")


class LocalStreamObservationError(RuntimeError):
    """A credential-safe local streaming failure that must not trigger fallback."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(f"Local stream observation failed: reason={reason_code}")


def _log_stream_inspection_failure(
    reason_code: str,
    payload: bytes | bytearray | memoryview,
    exception: BaseException | None = None,
    *,
    request_id: object = None,
) -> None:
    _log_stream_inspection_failure_metadata(
        reason_code,
        payload_length=len(payload),
        payload_sha256=hashlib.sha256(payload).hexdigest(),
        exception=exception,
        request_id=request_id,
    )


def _log_stream_inspection_failure_metadata(
    reason_code: str,
    *,
    payload_length: int,
    payload_sha256: str,
    exception: BaseException | None = None,
    request_id: object = None,
) -> None:
    safe_request_id = _safe_stream_request_id(request_id)
    logging.warning(
        "Upstream stream inspection failure reason=%s type=%s length=%d sha256=%s request_id=%s",
        reason_code,
        safe_exception_type_name(exception) if exception is not None else "none",
        payload_length,
        payload_sha256,
        safe_request_id,
    )


def _safe_stream_request_id(request_id: object) -> str:
    if not isinstance(request_id, str) or not request_id:
        return "none"
    if (
        len(request_id) <= 128
        and request_id.isascii()
        and all(character.isalnum() or character in "._-" for character in request_id)
    ):
        return request_id
    return "sha256:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


class _ContextOverflowStreamDetector:
    """Detect context-overflow signals without retaining an HTTP error body."""

    __slots__ = (
        "_decoder",
        "_detected",
        "_saw_context_length",
        "_saw_context_marker",
        "_saw_context_scope",
        "_saw_context_window",
        "_saw_maximum_context",
        "_saw_requested",
        "_saw_token_value",
        "_saw_tokens",
        "_saw_too_many_tokens",
        "_tail",
    )

    _TAIL_CHARS = 256

    def __init__(self) -> None:
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        self._detected = False
        self._saw_context_length = False
        self._saw_context_marker = False
        self._saw_context_scope = False
        self._saw_context_window = False
        self._saw_maximum_context = False
        self._saw_requested = False
        self._saw_token_value = False
        self._saw_tokens = False
        self._saw_too_many_tokens = False
        self._tail = ""

    def feed(self, payload: bytes) -> None:
        self._inspect(self._decoder.decode(payload))

    def finish(self) -> bool:
        self._inspect(self._decoder.decode(b"", final=True))
        return self._detected

    def _inspect(self, text: str) -> None:
        if self._detected:
            return
        window = self._tail + text
        normalized = window.lower()
        if _is_context_overflow_error(window):
            self._detected = True
            return

        self._saw_context_length |= "context length" in normalized
        self._saw_context_marker |= any(
            marker in normalized for marker in CONTEXT_OVERFLOW_CONTEXT_MARKERS
        )
        self._saw_context_scope |= any(
            scope in normalized for scope in ("prompt", "input", "context")
        )
        self._saw_context_window |= "context window" in normalized
        self._saw_maximum_context |= bool(
            CONTEXT_OVERFLOW_MAXIMUM_CONTEXT_PATTERN.search(normalized)
        )
        self._saw_requested |= "requested" in normalized
        self._saw_token_value |= bool(
            CONTEXT_OVERFLOW_TOKEN_VALUE_PATTERN.search(normalized)
        )
        self._saw_tokens |= "tokens" in normalized
        self._saw_too_many_tokens |= "too many tokens" in normalized
        self._detected = (
            (
                (self._saw_context_window or self._saw_context_length)
                and self._saw_context_marker
            )
            or (self._saw_maximum_context and self._saw_token_value)
            or (
                self._saw_requested
                and self._saw_tokens
                and (self._saw_context_marker or self._saw_token_value)
            )
            or (self._saw_too_many_tokens and self._saw_context_scope)
        )
        self._tail = window[-self._TAIL_CHARS :]


class _StreamObservationRetention:
    """Own exact process-wide capacity for bytes retained by one stream."""

    __slots__ = ("_capacity", "_lease")

    def __init__(self, capacity: StreamObservationCapacity) -> None:
        self._capacity = capacity
        self._lease: StreamObservationLease | None = None

    async def retain(self, byte_count: int) -> None:
        if byte_count <= 0:
            return

        snapshot = self._capacity.snapshot
        if self._lease is not None and (
            snapshot.waiters
            or snapshot.active_items >= snapshot.max_items
            or snapshot.active_bytes + byte_count > snapshot.max_bytes
        ):
            self._capacity.record_failure("retention_capacity_exhausted")
            raise StreamObservationStateError("retention_capacity_exhausted")

        lease = await self._capacity.acquire(byte_count)
        lease.release_item()
        if self._lease is None:
            self._lease = lease
        else:
            self._lease.absorb(lease)

    def consume(self, byte_count: int) -> None:
        if byte_count <= 0:
            return
        if self._lease is None:
            self._capacity.record_failure("retention_bytes_exceeded")
            raise StreamObservationStateError("retention_bytes_exceeded")
        self._lease.consume(byte_count)
        if self._lease.released:
            self._lease = None

    def absorb(self, lease: StreamObservationLease) -> None:
        lease.release_item()
        if self._lease is None:
            self._lease = lease
        else:
            self._lease.absorb(lease)

    def release_all(self) -> None:
        if self._lease is None:
            return
        self._lease.release_all()
        self._lease = None


class _LeasedDecodedChunk:
    __slots__ = ("data", "lease")

    def __init__(self, data: bytes, lease: StreamObservationLease) -> None:
        self.data = data
        self.lease = lease


class _DecodedResponseReader:
    """Decode raw HTTP content in capacity-reserved bounded pieces."""

    __slots__ = (
        "_capacity",
        "_chunk_limit",
        "_closed",
        "_compression_format_decided",
        "_decompressor",
        "_deflate_probe",
        "_drain_decoder",
        "_encoding",
        "_pending",
        "_raw_iterator",
        "_raw_retention",
    )

    def __init__(
        self,
        response: object,
        *,
        capacity: StreamObservationCapacity,
        chunk_limit: int,
    ) -> None:
        response_headers = getattr(response, "headers", None) or {}
        content_encoding = ""
        if hasattr(response_headers, "get"):
            content_encoding = (
                response_headers.get("content-encoding")
                or response_headers.get("Content-Encoding")
                or ""
            )
        if not isinstance(content_encoding, str):
            content_encoding = str(content_encoding)
        encoding = content_encoding.strip().lower()
        if encoding in {"", "identity"}:
            decompressor = None
            encoding = "identity"
        elif encoding == "gzip":
            decompressor = zlib.decompressobj(16 + zlib.MAX_WBITS)
        elif encoding == "deflate":
            decompressor = None
        else:
            raise httpx.DecodingError("unsupported_content_encoding")

        self._capacity = capacity
        self._chunk_limit = min(chunk_limit, capacity.snapshot.max_bytes // 3)
        if self._chunk_limit <= 0:
            raise LocalStreamObservationError("stream_decode_capacity_invalid")
        self._closed = False
        self._compression_format_decided = encoding != "deflate"
        self._decompressor = decompressor
        self._deflate_probe = b""
        self._drain_decoder = False
        self._encoding = encoding
        self._pending = b""
        self._raw_iterator = response.aiter_raw(
            chunk_size=self._chunk_limit,
        )
        self._raw_retention = _StreamObservationRetention(capacity)

    async def read(
        self,
        *,
        copy_retention: _StreamObservationRetention | None = None,
        output_limit: int | None = None,
        fail_if_blocked: bool = False,
    ) -> _LeasedDecodedChunk | None:
        if self._closed:
            raise LocalStreamObservationError("decoder_closed")
        piece_limit = self._chunk_limit
        if output_limit is not None:
            piece_limit = min(piece_limit, output_limit)
        if piece_limit <= 0:
            raise LocalStreamObservationError("decoder_output_limit")

        while True:
            if not self._pending and not self._drain_decoder:
                if fail_if_blocked:
                    snapshot = self._capacity.snapshot
                    if (
                        snapshot.waiters
                        or snapshot.active_items >= snapshot.max_items
                        or snapshot.active_bytes + self._chunk_limit > snapshot.max_bytes
                    ):
                        self._capacity.record_failure("raw_capacity_exhausted")
                        raise LocalStreamObservationError("raw_capacity_exhausted")
                await self._raw_retention.retain(self._chunk_limit)
                try:
                    raw_chunk = await anext(self._raw_iterator)
                except StopAsyncIteration:
                    self._raw_retention.consume(self._chunk_limit)
                    if self._encoding != "identity" and (
                        not self._compression_format_decided
                        or self._decompressor is None
                        or not self._decompressor.eof
                    ):
                        raise httpx.DecodingError("compressed_stream_incomplete")
                    return None
                except BaseException:
                    self._raw_retention.consume(self._chunk_limit)
                    raise
                if not isinstance(raw_chunk, bytes):
                    self._raw_retention.consume(self._chunk_limit)
                    raise LocalStreamObservationError("raw_chunk_type")
                if not raw_chunk:
                    self._raw_retention.consume(self._chunk_limit)
                    continue
                if len(raw_chunk) > self._chunk_limit:
                    self._raw_retention.consume(self._chunk_limit)
                    raise LocalStreamObservationError("raw_chunk_limit_exceeded")
                unused_raw_budget = self._chunk_limit - len(raw_chunk)
                if unused_raw_budget:
                    self._raw_retention.consume(unused_raw_budget)
                self._pending = raw_chunk

            if self._encoding == "identity":
                piece_limit = min(piece_limit, len(self._pending))

            if fail_if_blocked:
                snapshot = self._capacity.snapshot
                reservation_bytes = piece_limit
                if copy_retention is not None:
                    reservation_bytes += piece_limit
                if (
                    snapshot.waiters
                    or snapshot.active_items >= snapshot.max_items
                    or snapshot.active_bytes + reservation_bytes > snapshot.max_bytes
                ):
                    self._capacity.record_failure("decode_capacity_exhausted")
                    raise LocalStreamObservationError("decode_capacity_exhausted")

            copy_reserved = False
            if copy_retention is not None:
                await copy_retention.retain(piece_limit)
                copy_reserved = True

            output_lease: StreamObservationLease | None = None
            try:
                if copy_reserved:
                    snapshot = self._capacity.snapshot
                    if (
                        snapshot.waiters
                        or snapshot.active_items >= snapshot.max_items
                        or snapshot.active_bytes + piece_limit > snapshot.max_bytes
                    ):
                        self._capacity.record_failure("decode_capacity_exhausted")
                        raise StreamObservationStateError("decode_capacity_exhausted")
                output_lease = await self._capacity.acquire(piece_limit)
                data = self._decode_piece(piece_limit)
            except BaseException:
                if output_lease is not None:
                    output_lease.release_all()
                if copy_reserved:
                    copy_retention.consume(piece_limit)
                raise

            unused_bytes = piece_limit - len(data)
            if unused_bytes:
                output_lease.consume(unused_bytes)
                if copy_reserved:
                    copy_retention.consume(unused_bytes)
            if data:
                return _LeasedDecodedChunk(data, output_lease)

    def _decode_piece(self, output_limit: int) -> bytes:
        if self._encoding == "identity":
            data = self._pending[:output_limit]
            self._pending = self._pending[len(data):]
            self._raw_retention.consume(len(data))
            return data

        if self._encoding == "deflate" and not self._compression_format_decided:
            source = self._deflate_probe + self._pending
            if len(source) < 2:
                self._deflate_probe = source
                self._pending = b""
                return b""
            cmf, flg = source[0], source[1]
            has_zlib_header = (
                cmf & 0x0F == 8
                and cmf >> 4 <= 7
                and ((cmf << 8) + flg) % 31 == 0
            )
            self._decompressor = zlib.decompressobj(
                zlib.MAX_WBITS if has_zlib_header else -zlib.MAX_WBITS
            )
            self._compression_format_decided = True
            self._deflate_probe = b""
            self._pending = source

        if self._decompressor is None:
            raise LocalStreamObservationError("stream_decoder_uninitialized")
        source = self._pending if self._pending else b""
        try:
            data = self._decompressor.decompress(source, output_limit)
        except zlib.error as exc:
            raise httpx.DecodingError("compressed_stream_invalid") from exc
        if self._decompressor.unused_data:
            raise httpx.DecodingError("compressed_stream_trailing_data")

        remaining = self._decompressor.unconsumed_tail
        consumed_bytes = len(source) - len(remaining)
        if consumed_bytes:
            self._raw_retention.consume(consumed_bytes)
        self._pending = remaining
        self._drain_decoder = len(data) == output_limit and not self._pending
        return data

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._pending = b""
        self._deflate_probe = b""
        self._raw_retention.release_all()
        close_iterator = getattr(self._raw_iterator, "aclose", None)
        if close_iterator is not None:
            await close_iterator()


class _StreamResourceOwner:
    """One cancellation-safe, idempotent owner for an upstream stream."""

    __slots__ = (
        "_close_callback",
        "_close_completed",
        "_close_exception",
        "_close_lock",
        "_close_task",
    )

    def __init__(self, close_callback: Callable[[], Awaitable[None]]) -> None:
        self._close_callback = close_callback
        self._close_completed = False
        self._close_exception: BaseException | None = None
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def close(self) -> None:
        async with self._close_lock:
            if self._close_completed:
                if self._close_exception is not None:
                    raise self._close_exception
                return
            if self._close_task is None:
                self._close_task, creation_exception = await _start_cleanup_task(
                    self._close_callback,
                    failure_label="stream resource",
                )
                if creation_exception is not None:
                    self._close_completed = True
                    self._close_exception = creation_exception
                    raise creation_exception
            close_task = self._close_task
        first_cancellation: asyncio.CancelledError | None = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as exc:
                if first_cancellation is None:
                    first_cancellation = exc
        cleanup_exception: BaseException | None = None
        try:
            close_task.result()
        except BaseException as cleanup_exc:
            cleanup_exception = cleanup_exc
        self._close_completed = True
        self._close_exception = cleanup_exception
        if cleanup_exception is not None:
            if first_cancellation is None:
                raise cleanup_exception
            logging.warning(
                "Cancelled stream cleanup failed type=%s",
                safe_exception_type_name(cleanup_exception),
            )
        if first_cancellation is not None:
            raise first_cancellation


async def _start_cleanup_task(
    cleanup_callback: Callable[[], Awaitable[None]],
    *,
    failure_label: str,
) -> tuple[asyncio.Task[None] | None, BaseException | None]:
    cleanup_awaitable = cleanup_callback()
    try:
        return asyncio.create_task(cleanup_awaitable), None
    except BaseException as creation_exc:
        try:
            await cleanup_awaitable
        except BaseException as cleanup_exc:
            logging.warning(
                "%s cleanup after task creation failure failed type=%s",
                failure_label,
                safe_exception_type_name(cleanup_exc),
            )
        return None, creation_exc


class _OwnedStreamingResponse(StreamingResponse):
    __slots__ = (
        "_close_completed",
        "_close_exception",
        "_close_lock",
        "_close_task",
        "_source_iterators",
        "_stream_owner",
    )

    def __init__(self, *args: object, stream_owner: _StreamResourceOwner, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)
        self._stream_owner = stream_owner
        self._source_iterators: list[object] = []
        self._close_completed = False
        self._close_exception: BaseException | None = None
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    def replace_body_iterator(self, body_iterator: object) -> None:
        self._source_iterators.append(self.body_iterator)
        self.body_iterator = body_iterator

    async def _close_owned_stream_once(self) -> None:
        iterator_error: BaseException | None = None
        iterators = [self.body_iterator, *reversed(self._source_iterators)]
        self._source_iterators.clear()
        for iterator in iterators:
            close_iterator = getattr(iterator, "aclose", None)
            if close_iterator is None:
                continue
            try:
                await close_iterator()
            except BaseException as exc:
                if iterator_error is None:
                    iterator_error = exc
                else:
                    logging.warning(
                        "Additional owned iterator cleanup failed type=%s",
                        safe_exception_type_name(exc),
                    )
        try:
            await self._stream_owner.close()
        except BaseException as exc:
            if iterator_error is None:
                raise
            logging.warning(
                "Owned stream cleanup failed after iterator failure type=%s",
                safe_exception_type_name(exc),
            )
        if iterator_error is not None:
            raise iterator_error

    async def _close_owned_stream(self) -> None:
        async with self._close_lock:
            if self._close_completed:
                if self._close_exception is not None:
                    raise self._close_exception
                return
            if self._close_task is None:
                self._close_task, creation_exception = await _start_cleanup_task(
                    self._close_owned_stream_once,
                    failure_label="owned stream",
                )
                if creation_exception is not None:
                    self._close_completed = True
                    self._close_exception = creation_exception
                    raise creation_exception
            close_task = self._close_task
        first_cancellation: asyncio.CancelledError | None = None
        while not close_task.done():
            try:
                await asyncio.shield(close_task)
            except asyncio.CancelledError as exc:
                if first_cancellation is None:
                    first_cancellation = exc
        cleanup_exception: BaseException | None = None
        try:
            close_task.result()
        except BaseException as cleanup_exc:
            cleanup_exception = cleanup_exc
        self._close_completed = True
        self._close_exception = cleanup_exception
        if cleanup_exception is not None:
            if first_cancellation is None:
                raise cleanup_exception
            logging.warning(
                "Cancelled owned stream cleanup failed type=%s",
                safe_exception_type_name(cleanup_exception),
            )
        if first_cancellation is not None:
            raise first_cancellation

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        try:
            await super().__call__(scope, receive, send)
        except BaseException:
            try:
                await self._close_owned_stream()
            except BaseException as cleanup_exc:
                logging.warning(
                    "Owned stream cleanup suppressed for primary failure type=%s",
                    safe_exception_type_name(cleanup_exc),
                )
            raise
        await self._close_owned_stream()


def replace_streaming_response_body(
    response: StreamingResponse,
    body_iterator: object,
) -> StreamingResponse:
    """Replace a transformed stream while retaining upstream cleanup ownership."""
    if isinstance(response, _OwnedStreamingResponse):
        response.replace_body_iterator(body_iterator)
    else:
        response.body_iterator = body_iterator
    return response


def _build_streaming_response_result(
    response: StreamingResponse,
    error_detail: object,
) -> tuple[StreamingResponse, object]:
    return response, error_detail


class RequestErrorDetail(str):
    """``str`` subclass carrying optional metadata alongside the error message.

    Wrapped as ``str`` so existing call sites (logging, comparisons,
    error classification) keep working without changes. The ``retry_after``
    attribute holds the upstream ``Retry-After`` hint (already clamped and
    converted to seconds) when available, and ``status_code`` is the HTTP
    status that produced the failure (if applicable). ``retry_after_uncapped``
    holds the same hint before :data:`MAX_RETRY_AFTER_SECONDS` clamping, for
    callers (e.g. cooldown scheduling) that need the raw upstream value.
    """

    retry_after: float | None
    retry_after_uncapped: float | None
    status_code: int | None

    def __new__(
        cls,
        message: str,
        *,
        retry_after: float | None = None,
        retry_after_uncapped: float | None = None,
        status_code: int | None = None,
    ) -> "RequestErrorDetail":
        instance = super().__new__(cls, message)
        instance.retry_after = retry_after
        instance.retry_after_uncapped = retry_after_uncapped
        instance.status_code = status_code
        return instance


def parse_retry_after_header(value: object) -> float | None:
    """Parse an RFC 7231 ``Retry-After`` header value into seconds.

    Accepts either delta-seconds (``"15"``) or an HTTP-date
    (``"Wed, 21 Oct 2015 07:28:00 GMT"``). Returns ``None`` when the input
    cannot be parsed. The caller is responsible for clamping to a sane
    upper bound via :data:`MAX_RETRY_AFTER_SECONDS`.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = value.strip()
    if not value:
        return None

    try:
        seconds = float(value)
        if seconds < 0:
            return None
        return seconds
    except ValueError:
        pass

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - datetime.now(tz=timezone.utc)).total_seconds()
    if delta < 0:
        return 0.0
    return delta


def _clamped_retry_after(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0:
        return 0.0
    return min(value, MAX_RETRY_AFTER_SECONDS)
FORBIDDEN_CUSTOM_BODY_PARAM_KEYS = frozenset({"stream", "messages", "tool_choice", "tools", "model"})
SECURITY_HEADER_NAMES = frozenset({"authorization", "cookie", "x-api-key"})
PROTECTED_ROUTE_OVERRIDE_KEYS = frozenset({"prompt", "images", "mask"})
OPERATION_BODY_PARAM_ALLOWLIST = {
    "embeddings": frozenset({"dimensions", "encoding_format", "user"}),
    "rerank": frozenset({"top_n", "return_documents", "max_chunks_per_doc"}),
}
SUPPORTED_OPERATION_TYPES = frozenset(
    {
        "embeddings",
        "rerank",
        "images_generations",
        "images_edits",
        "audio_speech",
        "audio_transcriptions",
        "pdf_conversions",
        "web_search",
        "web_read",
        "web_research",
        "web_deep_research",
    }
)


class OperationDispatcher:
    """Dispatches operation requests for non-chat endpoints."""

    def __init__(
        self,
        providers_config: Dict[str, Any],
        operation_rules: Dict[str, Dict[str, Any]],
        http_client: httpx.AsyncClient,
        model_rules: Dict[str, Any] | None = None,
    ):
        """
        Initialize the dispatcher with providers, operation rules, and shared HTTP client.

        Args:
            providers_config: Dictionary mapping provider names to ProviderDetails
            operation_rules: Dictionary with operation sections,
                           each mapping gateway_model names to route configurations
            http_client: Shared httpx.AsyncClient from app.state, used for making requests
        """
        self._providers_config = providers_config
        self._operation_rules = operation_rules
        self._http_client = http_client
        self._model_rules = model_rules or {}

    def lookup_route(self, operation: str, gateway_model: str) -> Optional[OperationRoute]:
        """
        Look up the first configured route for a given operation type and gateway model.

        Endpoints that support ordered fallback should use ``lookup_routes`` instead.

        Args:
            operation: Operation type configured in models_operation_rules.json
            gateway_model: Gateway model name to look up

        Returns:
            The first OperationRoute for the model, or None if not found or operation unknown
        """
        routes = self.lookup_routes(operation, gateway_model)
        if not routes:
            return None
        return routes[0]

    def lookup_routes(self, operation: str, gateway_model: str) -> list[OperationRoute]:
        """
        Look up all configured routes for a given operation type and gateway model.

        The returned order matches models_operation_rules.json. Embeddings and rerank
        endpoints use this order as their fallback chain.
        """
        # For unknown operation types, return an empty route list.
        if operation not in SUPPORTED_OPERATION_TYPES:
            return []

        # Get the section for the operation type
        section = self._operation_rules.get(operation)
        if not section:
            return []

        try:
            model_resolution = resolve_model_name(gateway_model, self._model_rules)
        except ValueError:
            return []

        # Get routes for the gateway model
        model_config = section.get(model_resolution.effective_model)
        if not model_config:
            return []

        routes = model_config.get("routes")
        if not routes or not isinstance(routes, list):
            return []

        parsed_routes: list[OperationRoute] = []
        try:
            for route in routes:
                parsed_routes.append(OperationRoute(**route))
        except (ValueError, TypeError):
            return []

        return parsed_routes

    def build_target_url(self, route: OperationRoute, provider_config: ProviderDetails) -> str:
        """Build a downstream target URL from provider base URL and operation route path."""
        if route.target_path.startswith(("http://", "https://")):
            return route.target_path
        return f"{provider_config.baseUrl.rstrip('/')}/{route.target_path.lstrip('/')}"

    def build_headers(
        self,
        route: OperationRoute,
        provider_api_key: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Build downstream headers for an operation request."""
        headers: Dict[str, Any] = {
            "Content-Type": "application/json",
        }
        if auth_headers:
            headers.update(auth_headers)
        elif provider_api_key:
            headers["Authorization"] = f"Bearer {provider_api_key}"

        headers.update(route.custom_headers)
        return headers

    def filter_custom_headers(self, custom_headers: dict, allowlist: list[str]) -> Dict[str, Any]:
        """Keep only allowed custom headers while blocking security-sensitive headers."""
        allowed_headers = {header.lower() for header in allowlist}
        filtered_headers: Dict[str, Any] = {}

        for header_name, header_value in custom_headers.items():
            normalized_header_name = header_name.lower()
            if normalized_header_name in SECURITY_HEADER_NAMES:
                continue
            if normalized_header_name in allowed_headers:
                filtered_headers[header_name] = header_value

        return filtered_headers

    def merge_custom_params(self, base_payload: dict, route: OperationRoute, operation: str) -> dict:
        """Merge only allowed custom body params into the payload without overriding model."""
        merged_payload = copy.deepcopy(base_payload)
        allowed_params = OPERATION_BODY_PARAM_ALLOWLIST.get(operation, frozenset())

        for param_name, param_value in route.custom_body_params.items():
            normalized_param_name = param_name.lower()
            if normalized_param_name in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                continue
            if normalized_param_name not in allowed_params:
                continue
            if normalized_param_name == "model":
                continue
            merged_payload[param_name] = param_value

        return apply_payload_transforms(merged_payload, route.payload_transforms)

    def merge_all_non_reserved_custom_params(self, base_payload: dict, route: OperationRoute) -> dict:
        """Merge route custom body params while only blocking reserved keys."""
        merged_payload = copy.deepcopy(base_payload)

        for param_name, param_value in route.custom_body_params.items():
            normalized_param_name = param_name.lower()
            if normalized_param_name in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                continue
            if normalized_param_name == "model":
                continue
            if normalized_param_name in PROTECTED_ROUTE_OVERRIDE_KEYS:
                continue
            merged_payload[param_name] = param_value

        return apply_payload_transforms(merged_payload, route.payload_transforms)

    def build_payload(self, request_body: dict, route: OperationRoute, operation: str) -> dict:
        """Build downstream payload by replacing model and merging only allowed custom params."""
        payload = copy.deepcopy(request_body)
        for key in list(payload.keys()):
            if key.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                payload.pop(key)

        payload["model"] = route.model
        return self.merge_custom_params(payload, route, operation)

    def build_payload_with_route_overrides(self, request_body: dict, route: OperationRoute) -> dict:
        """Build downstream payload by replacing model and merging all non-reserved custom params."""
        payload = copy.deepcopy(request_body)
        for key in list(payload.keys()):
            if key.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                payload.pop(key)

        payload["model"] = route.model
        return self.merge_all_non_reserved_custom_params(payload, route)

    def set_request_state(
        self,
        request: Request,
        operation: str,
        route: OperationRoute,
        provider_name: str,
        provider_model: str,
    ) -> None:
        """Populate request.state with dispatcher metadata before the downstream request."""
        gateway_model = getattr(request.state, "llmgateway_gateway_model", None)
        if not isinstance(gateway_model, str) or not gateway_model:
            raise ValueError("request.state.llmgateway_gateway_model must be set before dispatching an operation")

        request.state.llmgateway_gateway_model = gateway_model
        request.state.llmgateway_provider = provider_name
        request.state.llmgateway_provider_model = provider_model
        request.state.llmgateway_operation = operation
        request.state.llmgateway_target_path = route.target_path
        update_active_request_from_state(request)


def _parse_sse_event_json(event: SSEEvent):
    data = event.data.strip()
    if event.done or data == "[DONE]":
        return "[DONE]"
    if not data:
        return None
    try:
        parsed = sanitize_payload(parse_sse_json(event))
    except (TypeError, ValueError, RecursionError) as exc:
        raise StreamPayloadError("invalid_json") from exc
    if not isinstance(parsed, dict):
        raise StreamPayloadError("json_not_object")
    return parsed


def _parse_stream_chunk_json(chunk_str: str, *, max_event_bytes: int):
    """Compatibility helper backed by the canonical incremental SSE framer."""
    encoded = chunk_str.encode("utf-8")
    framer = SSEFramer(max_event_bytes=max_event_bytes)
    batch = framer.feed(encoded + b"\n\n")
    if not batch.events:
        return None
    if len(batch.events) != 1:
        raise StreamPayloadError("multiple_events")
    return _parse_sse_event_json(batch.events[0])


def _is_success_stream_code(code: object, message: object) -> bool:
    if code != 0 and not (isinstance(code, str) and code.strip() == "0"):
        return False
    if message is None:
        return True
    if isinstance(message, str):
        return message.strip().lower() in {"", "success"}
    return False


def _extract_stream_error_detail(chunk_json: object) -> str | None:
    if not isinstance(chunk_json, dict):
        return None

    if "error" in chunk_json:
        error = chunk_json.get("error")
        if isinstance(error, dict):
            return error.get("message") or str(error)
        return str(error)

    if chunk_json.get("type") == "error":
        message = chunk_json.get("message")
        if message:
            return str(message)
        return str(chunk_json)

    if "detail" in chunk_json:
        return str(chunk_json.get("detail"))

    if "code" in chunk_json:
        code = chunk_json.get("code")
        message = chunk_json.get("message")
        if _is_success_stream_code(code, message):
            return None
        return str(message or code)

    return None


def _safe_stream_error_detail(
    chunk_json: dict,
    raw_detail: str,
) -> RequestErrorDetail:
    if _is_context_overflow_error(raw_detail):
        return RequestErrorDetail("Upstream stream reported context overflow.")

    error_payload = chunk_json.get("error")
    status_candidates = [chunk_json.get("status"), chunk_json.get("code")]
    if isinstance(error_payload, dict):
        status_candidates.extend(
            (error_payload.get("status"), error_payload.get("code"))
        )
    for candidate in status_candidates:
        if isinstance(candidate, bool):
            continue
        try:
            status_code = int(candidate)
        except (TypeError, ValueError):
            continue
        if status_code == 429 or 500 <= status_code <= 599:
            return RequestErrorDetail(
                f"Upstream stream reported HTTP status {status_code}.",
                status_code=status_code,
            )

    normalized = raw_detail.lower()
    if any(
        marker in normalized
        for marker in ("rate_limit", "rate limit", "too many requests")
    ):
        return RequestErrorDetail(
            "Upstream stream reported rate limit.",
            status_code=429,
        )
    if any(
        marker in normalized
        for marker in (
            "currently overloaded",
            "engine is overloaded",
            "overloaded",
            "try again later",
            "temporarily unavailable",
            "temporary unavailable",
            "service unavailable",
        )
    ):
        return RequestErrorDetail(
            "Upstream stream reported temporarily unavailable."
        )
    return RequestErrorDetail("Upstream stream reported an error event.")


def _normalize_non_negative_int(value: object, default: int, setting_name: str) -> int:
    if value is None:
        return default

    try:
        normalized_value = int(value)
    except (TypeError, ValueError):
        logging.warning(f"Invalid {setting_name}={value!r}. Falling back to default {default}.")
        return default

    if normalized_value < 0:
        logging.warning(f"Negative {setting_name}={normalized_value}. Falling back to default {default}.")
        return default

    return normalized_value


def _normalize_non_negative_float(value: object, default: float, setting_name: str) -> float:
    if value is None:
        return default

    try:
        normalized_value = float(value)
    except (TypeError, ValueError):
        logging.warning(f"Invalid {setting_name}={value!r}. Falling back to default {default}.")
        return default

    if normalized_value < 0:
        logging.warning(f"Negative {setting_name}={normalized_value}. Falling back to default {default}.")
        return default

    return normalized_value


def normalize_retry_settings(retry_count: object, retry_delay: object) -> tuple[int, float]:
    normalized_retry_count = _normalize_non_negative_int(retry_count, DEFAULT_RETRY_COUNT, "retry_count")
    normalized_retry_delay = _normalize_non_negative_float(retry_delay, DEFAULT_RETRY_DELAY_SECONDS, "retry_delay")
    return normalized_retry_count, normalized_retry_delay


def _format_request_error_detail(target_url: str, error: httpx.RequestError) -> str:
    error_type = error.__class__.__name__
    error_text = str(error).strip()
    if error_text:
        return f"{error_type} connecting to {target_url}: {error_text}"
    return f"{error_type} connecting to {target_url}"


def _stream_chunk_has_response_content(chunk_json: object) -> bool:
    if not isinstance(chunk_json, dict):
        return False

    choices = chunk_json.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                for key in ("content", "reasoning", "reasoning_content", "function_call", "tool_calls"):
                    if delta.get(key):
                        return True
            message = choice.get("message")
            if isinstance(message, dict) and message.get("content"):
                return True

    delta = chunk_json.get("delta")
    if isinstance(delta, dict):
        for key in ("text", "partial_json", "thinking"):
            if delta.get(key):
                return True

    content = chunk_json.get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        return bool(content)
    return False


async def _make_streaming_request(
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict,
    request_payload: dict,
    *,
    stream_observation_capacity: StreamObservationCapacity,
    stream_event_max_bytes: int,
    stream_request_id: str | None = None,
    response_headers_sink: MutableMapping[str, str] | None = None,
):
    """Prime, inspect, and stream one bounded upstream SSE response."""
    if (
        isinstance(stream_event_max_bytes, bool)
        or not isinstance(stream_event_max_bytes, int)
        or stream_event_max_bytes <= 0
    ):
        raise LocalStreamObservationError("stream_event_limit_invalid")
    if stream_event_max_bytes > stream_observation_capacity.snapshot.max_bytes:
        raise LocalStreamObservationError("stream_event_limit_exceeds_capacity")
    decoded_chunk_limit = min(
        STREAM_RAW_CHUNK_MAX_BYTES,
        stream_event_max_bytes,
        stream_observation_capacity.snapshot.max_bytes // 2,
    )
    if decoded_chunk_limit <= 0:
        raise LocalStreamObservationError("stream_decode_capacity_invalid")

    error_in_stream = False
    error_detail = None
    tokens_usage = None
    upstream_done = False
    framer = SSEFramer(max_event_bytes=stream_event_max_bytes)
    framer_retention = _StreamObservationRetention(stream_observation_capacity)

    def inspect_events(events: tuple[SSEEvent, ...]) -> bool:
        nonlocal error_detail, error_in_stream, saw_real_data_chunk
        nonlocal tokens_usage, upstream_done, first_content_at

        for event in events:
            try:
                chunk_json = _parse_sse_event_json(event)
            except StreamPayloadError as exc:
                payload = event.data.encode("utf-8")
                _log_stream_inspection_failure(
                    exc.reason_code,
                    payload,
                    exc,
                    request_id=stream_request_id,
                )
                error_detail = "Upstream stream contained an invalid SSE data event."
                error_in_stream = True
                return True

            if chunk_json == "[DONE]":
                upstream_done = True
                if not saw_real_data_chunk:
                    error_detail = "Stream ended before any content chunks were received."
                    error_in_stream = True
                return True
            if chunk_json is None:
                continue

            error_detail_candidate = _extract_stream_error_detail(chunk_json)
            if error_detail_candidate:
                _log_stream_inspection_failure(
                    "upstream_error_event",
                    event.data.encode("utf-8"),
                    request_id=stream_request_id,
                )
                error_detail = _safe_stream_error_detail(
                    chunk_json,
                    error_detail_candidate,
                )
                error_in_stream = True
                return True

            if isinstance(chunk_json, dict) and "usage" in chunk_json:
                tokens_usage = chunk_json.get("usage")
            if _stream_chunk_has_response_content(chunk_json):
                saw_real_data_chunk = True
                if first_content_at is None:
                    first_content_at = time.monotonic()

        return error_in_stream or upstream_done

    stream_headers = {
        key: value
        for key, value in headers.items()
        if key.lower() != "accept-encoding"
    }
    stream_headers["Accept-Encoding"] = STREAM_ACCEPT_ENCODING
    stream_started_at = time.monotonic()
    stream_context = client.stream(
        "POST",
        target_url,
        headers=stream_headers,
        json=request_payload,
    )
    response = await stream_context.__aenter__()
    if response_headers_sink is not None:
        response_headers_sink.update(
            {key.lower(): value for key, value in response.headers.items()}
        )
    try:
        reader: _DecodedResponseReader | None = None
        prefetched_chunks: deque[_LeasedDecodedChunk] = deque()
        stream_handed_off = False

        async def close_stream_resources() -> None:
            cleanup_error: BaseException | None = None
            framer.abort()
            while prefetched_chunks:
                prefetched_chunks.popleft().lease.release_all()
            framer_retention.release_all()
            if reader is not None:
                try:
                    await reader.aclose()
                except BaseException as exc:
                    cleanup_error = exc
            try:
                await stream_context.__aexit__(None, None, None)
            except BaseException as exc:
                if cleanup_error is None:
                    cleanup_error = exc
                else:
                    logging.warning(
                        "Upstream stream context cleanup failed type=%s",
                        safe_exception_type_name(exc),
                    )
            if cleanup_error is not None:
                raise cleanup_error

        stream_owner = _StreamResourceOwner(close_stream_resources)
    except BaseException:
        try:
            await stream_context.__aexit__(None, None, None)
        except BaseException as exc:
            logging.warning(
                "Upstream stream setup cleanup failed type=%s",
                safe_exception_type_name(exc),
            )
        raise

    primary_error: BaseException | None = None
    try:
        reader = _DecodedResponseReader(
            response,
            capacity=stream_observation_capacity,
            chunk_limit=decoded_chunk_limit,
        )
        prefetched_bytes = 0
        saw_real_data_chunk = False
        first_content_at: float | None = None

        if response.status_code >= 400:
            error_body_length = 0
            error_body_sha256 = hashlib.sha256()
            context_overflow_detector = _ContextOverflowStreamDetector()
            error_body_truncated = False
            while error_body_length < stream_event_max_bytes:
                try:
                    decoded = await reader.read(
                        output_limit=stream_event_max_bytes - error_body_length,
                        fail_if_blocked=error_body_length > 0,
                    )
                except LocalStreamObservationError as exc:
                    if exc.reason_code not in {
                        "decode_capacity_exhausted",
                        "raw_capacity_exhausted",
                    }:
                        raise
                    error_body_truncated = True
                    break
                if decoded is None:
                    break
                try:
                    error_body_length += len(decoded.data)
                    error_body_sha256.update(decoded.data)
                    context_overflow_detector.feed(decoded.data)
                finally:
                    decoded.lease.release_all()
                if error_body_length >= stream_event_max_bytes:
                    error_body_truncated = True
                    break

            _log_stream_inspection_failure_metadata(
                (
                    "upstream_http_error_truncated"
                    if error_body_truncated
                    else "upstream_http_error"
                ),
                payload_length=error_body_length,
                payload_sha256=error_body_sha256.hexdigest(),
                request_id=stream_request_id,
            )
            headers = getattr(response, "headers", None) or {}
            retry_after_uncapped = parse_retry_after_header(
                headers.get("retry-after") if hasattr(headers, "get") else None
            )
            retry_after = _clamped_retry_after(retry_after_uncapped)
            client_error = f"Upstream request failed with HTTP status {response.status_code}."
            if context_overflow_detector.finish():
                client_error = (
                    f"Upstream request failed with HTTP status {response.status_code}: "
                    "context overflow."
                )
            return None, RequestErrorDetail(
                client_error,
                retry_after=retry_after,
                retry_after_uncapped=retry_after_uncapped,
                status_code=response.status_code,
            )

        # Prime until the first complete SSE event so empty/error streams fail closed.
        while True:
            if prefetched_chunks:
                snapshot = stream_observation_capacity.snapshot
                if snapshot.waiters or snapshot.active_items >= snapshot.max_items:
                    stream_observation_capacity.record_failure(
                        "prefetch_item_capacity_exhausted"
                    )
                    raise LocalStreamObservationError(
                        "prefetch_item_capacity_exhausted"
                    )

            try:
                decoded = await reader.read(
                    copy_retention=framer_retention,
                    fail_if_blocked=bool(prefetched_chunks),
                )
            except LocalStreamObservationError:
                raise
            except StreamObservationError as exc:
                raise LocalStreamObservationError(exc.reason_code) from exc
            if decoded is None:
                try:
                    tail = framer.finish()
                except SSEFramingError as exc:
                    _log_stream_inspection_failure(
                        exc.reason_code,
                        b"",
                        exc,
                        request_id=stream_request_id,
                    )
                    error_detail = "Upstream stream framing failed before content."
                    error_in_stream = True
                else:
                    framer_retention.consume(tail.consumed_bytes)
                    inspect_events(tail.events)
                break

            keep_decoded = False
            try:
                if prefetched_bytes + len(decoded.data) > stream_event_max_bytes:
                    _log_stream_inspection_failure(
                        "priming_too_large",
                        decoded.data,
                        request_id=stream_request_id,
                    )
                    return None, "Stream priming exceeded maximum buffered bytes before content."

                prefetched_bytes += len(decoded.data)
                batch = framer.feed(decoded.data)
                framer_retention.consume(batch.consumed_bytes)
                inspect_events(batch.events)
                if not error_in_stream:
                    prefetched_chunks.append(decoded)
                    keep_decoded = True
            except SSEFramingError as exc:
                _log_stream_inspection_failure(
                    exc.reason_code,
                    decoded.data,
                    exc,
                    request_id=stream_request_id,
                )
                error_detail = "Upstream stream framing failed before content."
                error_in_stream = True
            finally:
                if not keep_decoded:
                    decoded.lease.release_all()
            if error_in_stream or saw_real_data_chunk or upstream_done:
                break

        if error_in_stream:
            return None, error_detail

        if not prefetched_chunks or not saw_real_data_chunk:
            return None, "Stream ended before any content chunks were received."

        async def combined_generator():
            nonlocal error_in_stream, error_detail, tokens_usage, saw_real_data_chunk
            nonlocal upstream_done
            try:
                while prefetched_chunks:
                    prefetched_chunk = prefetched_chunks.popleft()
                    try:
                        yield prefetched_chunk.data
                    finally:
                        prefetched_chunk.lease.release_all()

                if upstream_done:
                    return

                while True:
                    decoded = await reader.read(
                        copy_retention=framer_retention,
                        fail_if_blocked=framer.buffered_bytes > 0,
                    )
                    if decoded is None:
                        break
                    should_stop = False
                    try:
                        try:
                            batch = framer.feed(decoded.data)
                            framer_retention.consume(batch.consumed_bytes)
                        except SSEFramingError as exc:
                            _log_stream_inspection_failure(
                                exc.reason_code,
                                decoded.data,
                                exc,
                                request_id=stream_request_id,
                            )
                            error_detail = "Upstream stream framing failed after response start."
                            error_in_stream = True
                            should_stop = True
                        else:
                            should_stop = inspect_events(batch.events)

                        yield decoded.data
                    finally:
                        decoded.lease.release_all()

                    if should_stop:
                        break

                if not error_in_stream and not upstream_done:
                    try:
                        tail = framer.finish()
                    except SSEFramingError as exc:
                        _log_stream_inspection_failure(
                            exc.reason_code,
                            b"",
                            exc,
                            request_id=stream_request_id,
                        )
                        error_detail = "Upstream stream framing failed at EOF."
                        error_in_stream = True
                    else:
                        framer_retention.consume(tail.consumed_bytes)
                        inspect_events(tail.events)
                logging.info("Finished upstream stream observation.")
            finally:
                await stream_owner.close()

        streaming_response = _OwnedStreamingResponse(
            combined_generator(),
            stream_owner=stream_owner,
            media_type="text/event-stream",
            headers={"Transfer-Encoding": "chunked", "X-Accel-Buffering": "no"},
        )
        # TTFT is per-attempt: elapsed time from this attempt's POST to its
        # first real content chunk. Unlike duration_ms (admission through
        # every fallback attempt), it is not expected to be <= duration_ms.
        streaming_response.llmgateway_ttft_ms = max(
            0, int((first_content_at - stream_started_at) * 1000)
        )
        result = _build_streaming_response_result(streaming_response, error_detail)
        stream_handed_off = True
        return result
    except BaseException as exc:
        primary_error = exc
        raise
    finally:
        if not stream_handed_off:
            try:
                await stream_owner.close()
            except BaseException as cleanup_exc:
                if primary_error is None:
                    raise
                logging.warning(
                    "Upstream stream cleanup suppressed for primary failure type=%s",
                    safe_exception_type_name(cleanup_exc),
                )


async def _make_json_request(
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict,
    request_payload: dict,
    *,
    response_headers_sink: MutableMapping[str, str] | None = None,
):
    """Non-stream branch of make_llm_request.

    Returns ``(response_json, None)`` on success or ``(None, error_detail)``
    on any downstream failure. Extracted verbatim from make_llm_request() —
    no behavior change.
    """
    response = await client.post(target_url, headers=headers, json=request_payload)
    if response_headers_sink is not None:
        response_headers_sink.update(
            {key.lower(): value for key, value in response.headers.items()}
        )
    logging.debug(f"Response received from {target_url}")

    if response.status_code >= 400:
        error_text = response.text
        logging.warning(f"Downstream error {response.status_code} from {target_url}: {error_text}")
        headers = getattr(response, "headers", None) or {}
        retry_after_uncapped = parse_retry_after_header(
            headers.get("retry-after") if hasattr(headers, "get") else None
        )
        retry_after = _clamped_retry_after(retry_after_uncapped)
        return None, RequestErrorDetail(
            error_text,
            retry_after=retry_after,
            retry_after_uncapped=retry_after_uncapped,
            status_code=response.status_code,
        )

    try:
        response_json = response.json()
    except ValueError as json_err:
        error_detail = f"Invalid JSON response from {target_url}. Error={json_err}. Response= {response.text[:1000]}..."
        logging.error(error_detail, exc_info=True)
        return None, error_detail

    if not isinstance(response_json, dict):
        error_detail = (
            f"Non-object JSON body from {target_url}: "
            f"type={type(response_json).__name__}. Response= {response.text[:1000]}..."
        )
        logging.error(error_detail)
        return None, error_detail

    if "error" in response_json or "detail" in response_json:
        if "error" in response_json:
            error = response_json.get("error")
            if isinstance(error, dict):
                error_detail = error.get("message")
            elif isinstance(error, str):
                error_detail = error
            else:
                error_detail = repr(error)
        else:
            error_detail = None
        error_detail = error_detail or response_json.get("detail")
        logging.warning(f"Error detected in non-stream response from {target_url}: {error_detail}")
        return None, error_detail
    return response_json, None


async def make_llm_request(
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict,
    payload: dict,
    is_streaming: bool,
    *,
    stream_observation_capacity: StreamObservationCapacity | None = None,
    stream_event_max_bytes: int | None = None,
    stream_request_id: str | None = None,
    response_headers_sink: MutableMapping[str, str] | None = None,
):
    """Makes the downstream request and handles streaming/non-streaming responses."""
    request_payload = sanitize_payload(payload)

    logging.debug(
        "make_llm_request(): Sending request for model '%s'. Payload: %s",
        request_payload.get("model"),
        {k: v for k, v in request_payload.items() if k != "messages"},
    )
    if is_streaming:
        if stream_observation_capacity is None:
            raise LocalStreamObservationError("stream_capacity_required")
        if stream_event_max_bytes is None:
            raise LocalStreamObservationError("stream_event_limit_required")

    try:
        if is_streaming:
            return await _make_streaming_request(
                client,
                target_url,
                headers,
                request_payload,
                stream_observation_capacity=stream_observation_capacity,
                stream_event_max_bytes=stream_event_max_bytes,
                stream_request_id=stream_request_id,
                response_headers_sink=response_headers_sink,
            )
        return await _make_json_request(
            client,
            target_url,
            headers,
            request_payload,
            response_headers_sink=response_headers_sink,
        )
    except MemoryError:
        raise
    except LocalStreamObservationError:
        raise
    except StreamObservationError as exc:
        raise LocalStreamObservationError(exc.reason_code) from exc
    except httpx.RequestError as e:
        error_detail = _format_request_error_detail(target_url, e)
        logging.error(error_detail, exc_info=True)
        return None, error_detail
    except Exception as e:
        error_detail = f"Unexpected error during request to {target_url}: {str(e)}"
        logging.error(error_detail, exc_info=True)
        return None, error_detail
