import asyncio
import hashlib
import json
import glob
import os
import anyio
import time
from datetime import datetime
from uuid import uuid4
import logging
from pprint import pformat
import zlib
from ..config.settings import settings
from ..config.paths import resolve_log_dir
from fastapi import Request
from typing import TYPE_CHECKING
import gzip
import io
from ..services.active_requests import active_request_id
from ..services.accounting import AccountingValidationError, classify_billing_policy
from ..services.upstream_routing_state import UpstreamRoutingState
from ..services.stream_observation import (
    SSEFramer,
    SSEFramingError,
    StreamObservationLease,
    StreamObservationTooLarge,
)
from ..api.v1.chat_accounting import (
    CHAT_TERMINAL_HANDOFF_STATE_KEY,
    ChatStreamDialect,
    ChatTerminalOwner,
    bind_chat_request,
    build_chat_response_observation,
    install_chat_terminal_handoff,
    release_chat,
    take_chat_terminal_owner,
)
from ..middleware.accounting_admission import (
    get_accounting_request_context,
    resolve_effective_http_route,
)
from ..middleware.response_observation import (
    ResponseObservationStateError,
    ResponseStart,
    TransportResult,
    WireMode,
    captured_request_body,
    enable_request_capture,
    publish_response_observation,
)
from ..services.runtime_config import AppServices, RuntimeSnapshot
from ..utils.log_redaction import (
    redact_headers_for_log,
    redact_request_body_text,
    safe_exception_type_name,
)
from ..utils.usage_tracking import (
    UPSTREAM_COST_PRESENT_KEY,
    apply_rate_based_cost,
    backfill_zero_token_counts,
    enrich_tokens_usage as enrich_usage_metadata,
    estimate_prompt_tokens,
    extract_tokens_usage,
)

logger = logging.getLogger(__name__)

if TYPE_CHECKING:
    from ..services.active_requests import ActiveRequestsRegistry
    from ..services.task_supervisor import TaskSupervisor

_STREAM_END = object()
_STREAM_ABORT = object()
STREAM_CHUNK_QUEUE_MAXSIZE = settings.stream_chunk_queue_maxsize
STREAM_LOG_CAPTURE_MAX_CHARS = 262_144
STREAM_TOOL_STATE_MAX_ITEMS = 1024
STREAM_TOOL_STATE_KEY_MAX_BYTES = 128
STREAM_LOG_TRUNCATION_MARKER = "\n\n[TRUNCATED]\n"
_LOG_CLEANUP_INTERVAL = 10  # Only check for old log cleanup every N writes
# Upper bound for waiting on the chunk-processing task during shutdown of a streaming
# response. Guards against indefinite hangs if the worker is blocked on disk I/O or
# blocking token calculation — observability must not become a liveness hazard.
CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS = 5.0
_log_write_counter = 0


def _safe_stream_request_id(request: Request | None) -> str:
    if request is None:
        return "none"
    request_id = active_request_id(request)
    if not isinstance(request_id, str) or not request_id:
        return "none"
    if (
        len(request_id) <= 128
        and request_id.isascii()
        and all(character.isalnum() or character in "._-" for character in request_id)
    ):
        return request_id
    return "sha256:" + hashlib.sha256(request_id.encode("utf-8")).hexdigest()


def _decode_body(body_bytes: bytes, headers: dict) -> str:
    """Safely decode request body, handling optional GZIP compression."""
    if not body_bytes:
        return ""
    
    content_encoding = headers.get("content-encoding", "").lower()
    try:
        if "gzip" in content_encoding:
            with gzip.GzipFile(fileobj=io.BytesIO(body_bytes)) as f:
                body_bytes = f.read()
        
        return body_bytes.decode("utf-8", errors="replace")
    except Exception as e:
        logger.debug(f"Failed to decode or decompress request body: {e}")
        return body_bytes.decode("utf-8", errors="replace")


def write_log(req_headers, req_body_str, llm_response_accum, tokens_usage):
    try:
        sanitized_headers = redact_headers_for_log(req_headers)
        sanitized_request_body = redact_request_body_text(req_body_str)
        # Create log file with the required name format: "YY-MM-DD_HH:MM:ss:mmm.txt"
        log_time = datetime.now()
        filename = (
            log_time.strftime("%Y-%m-%d_%H-%M-%S")
            + (".%03d" % (log_time.microsecond // 1000))
            + f"_{uuid4()}.txt"
        )
        division_line = "-" * 100
        model = f"Model: {tokens_usage.get('model', 'unknown')}\n"
        provider = f"Provider: {tokens_usage.get('provider', 'unknown')}\n\n"
        estimated_suffix = "  (ESTIMATED via tiktoken — upstream usage missing)\n" if tokens_usage.get("is_estimated") else "\n"
        log_content = (
            f"{division_line}\nTokens Usage:{estimated_suffix}-{division_line}\n\n"
                f"Input: {tokens_usage.get('prompt_tokens', 0)}\n"
                f"Output: {tokens_usage.get('completion_tokens', 0)}\n"
                f"Cached: {tokens_usage.get('cached_tokens', 0)}\n"
                f"Reasoning: {tokens_usage.get('reasoning_tokens', 0)}\n"
                f"Total: {tokens_usage.get('total_tokens', 0)}\n"
                f"Cost: ${tokens_usage.get('cost', 0.0):0.6f}\n"
                f"{model}"
                f"{provider}"
            f"{division_line}\nRequest Headers:\n{division_line}\n\n{pformat(sanitized_headers, indent=2)}\n\n"
            f"{division_line}\nRequest Body:\n-{division_line}\n\n{sanitized_request_body}\n\n"
            f"{division_line}\nLLM Response:\n{division_line}\n\n{llm_response_accum}"
        )
        log_dir = os.fspath(resolve_log_dir())
        os.makedirs(log_dir, exist_ok=True)
        log_path = os.path.join(log_dir, filename)

        # Write the new log file
        with open(log_path, "w", encoding="utf-8") as f:
            log_content = log_content.replace("\\n\\n", "\r\n\r\n").replace("\\n", "\r\n")
            f.write(log_content)

        # Clean up old logs periodically (not every write — glob+sort is expensive)
        global _log_write_counter
        _log_write_counter += 1
        if _log_write_counter % _LOG_CLEANUP_INTERVAL == 0:
            log_files = sorted(
                glob.glob(os.path.join(log_dir, "*.txt")),
                key=os.path.getmtime,
            )
            max_logs = settings.log_file_limit or 50
            while len(log_files) > max_logs:
                try:
                    os.remove(log_files.pop(0))
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to write chat log: {e}", exc_info=True)


def record_chat_observability(
    req_headers,
    req_body_str,
    llm_response_accum,
    tokens_usage,
    cost_rate_registry=None,
    request: Request | None = None,
    *,
    services: "AppServices",
):
    if backfill_zero_token_counts(tokens_usage, req_body_str, llm_response_accum):
        logger.warning(
            "Upstream did not return usage (possibly interrupted stream); "
            "estimated token counts via tiktoken for model %s: "
            "prompt=%d, completion=%d, reasoning=%d, total=%d",
            tokens_usage.get("model") or tokens_usage.get("gateway_model"),
            tokens_usage.get("prompt_tokens", 0),
            tokens_usage.get("completion_tokens", 0),
            tokens_usage.get("reasoning_tokens", 0),
            tokens_usage.get("total_tokens", 0),
        )

    apply_rate_based_cost(tokens_usage, cost_rate_registry)

    if settings.log_chat_messages:
        write_log(req_headers, req_body_str, llm_response_accum, tokens_usage)


def _record_chat_observability_with_rates(
    req_headers,
    req_body_str,
    llm_response_accum,
    tokens_usage,
    cost_rate_registry,
    services: "AppServices",
    request: Request | None = None,
) -> None:
    if cost_rate_registry is None:
        record_chat_observability(
            req_headers,
            req_body_str,
            llm_response_accum,
            tokens_usage,
            request=request,
            services=services,
        )
        return
    record_chat_observability(
        req_headers,
        req_body_str,
        llm_response_accum,
        tokens_usage,
        cost_rate_registry,
        request=request,
        services=services,
    )


def _log_rtk_compression_stats(request: Request | None) -> None:
    if request is None:
        return
    stats = getattr(request.state, "llmgateway_compression_stats", None)
    if stats is None:
        return
    logger.info(
        "[RTK Compression] saved: %.1f%% | input: %d bytes | output: %d bytes | filters: %s",
        stats.saved_pct,
        stats.input_bytes,
        stats.output_bytes,
        stats.filters_applied,
    )


def _extract_operation_from_url(url_path: str) -> str | None:
    path = url_path.rstrip("/")
    if path.endswith("/chat/completions") or path.endswith("/responses"):
        return "chat"
    if path.endswith("/messages"):
        return "messages"
    return None


def _chat_stream_dialect(url_path: str) -> ChatStreamDialect:
    path = url_path.rstrip("/")
    if path.endswith("/chat/completions"):
        return ChatStreamDialect.OPENAI
    if path.endswith("/messages"):
        return ChatStreamDialect.ANTHROPIC
    if path.endswith("/responses"):
        return ChatStreamDialect.RESPONSES
    raise ValueError("unsupported chat accounting route")


async def _release_chat_owner(
    owner: ChatTerminalOwner,
    *,
    primary_error: BaseException | None = None,
) -> None:
    try:
        await release_chat(owner)
    except BaseException:
        if primary_error is None:
            raise


def _extract_responses_output_text(output_items: object) -> str:
    if not isinstance(output_items, list):
        return ""

    text_parts: list[str] = []
    for item in output_items:
        if not isinstance(item, dict) or item.get("type") != "message":
            continue

        content = item.get("content")
        if not isinstance(content, list):
            continue

        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") not in {"output_text", "text"}:
                continue
            text = block.get("text")
            if isinstance(text, str):
                text_parts.append(text)

    return "".join(text_parts)


def _extract_response_text_for_log(response_data: dict) -> str:
    if "choices" in response_data and isinstance(response_data["choices"], list):
        first_choice = response_data["choices"][0] if response_data["choices"] else {}
        if isinstance(first_choice, dict):
            message = first_choice.get("message", {})
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content

    content = response_data.get("content")
    if isinstance(content, list):
        text_parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            if block.get("type") == "text" and isinstance(block.get("text"), str):
                text_parts.append(block["text"])
        if text_parts:
            return "".join(text_parts)

    return _extract_responses_output_text(response_data.get("output"))


def enrich_tokens_usage(
    tokens_usage,
    request: Request,
    gateway_model: str | None = None,
    operation: str | None = None,
    *,
    config_loader,
    cost_rate_registry,
):
    if operation is None:
        operation = _extract_operation_from_url(request.url.path)
    return enrich_usage_metadata(
        tokens_usage,
        request,
        config_loader=config_loader,
        cost_rate_registry=cost_rate_registry,
        gateway_model=gateway_model,
        operation=operation,
    )


def _merge_request_usage_tracker(tokens_usage: dict, request: Request | None) -> dict:
    if request is None:
        return tokens_usage

    usage_tracker = getattr(request.state, "usage_tracker", None)
    if not isinstance(usage_tracker, dict):
        return tokens_usage

    for key, value in usage_tracker.items():
        if isinstance(value, bool):
            if value:
                tokens_usage[key] = value
        elif isinstance(value, (int, float)):
            if value > 0:
                tokens_usage[key] = value
        elif isinstance(value, str) and value:
            tokens_usage[key] = value

    prompt_tokens = tokens_usage.get("prompt_tokens")
    completion_tokens = tokens_usage.get("completion_tokens")
    if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
        if prompt_tokens > 0 and completion_tokens > 0:
            tokens_usage["total_tokens"] = prompt_tokens + completion_tokens

    return tokens_usage


def _record_upstream_tokens_for_request(
    request: Request | None,
    tokens_usage: dict,
    upstream_state: UpstreamRoutingState,
) -> None:
    if request is None:
        return
    provider = getattr(request.state, "llmgateway_provider", None)
    model = getattr(request.state, "llmgateway_provider_model", None)
    key_fingerprint = getattr(request.state, "llmgateway_upstream_key_fingerprint", None)
    if not provider or not model or not key_fingerprint:
        return
    upstream_state.record_tokens(
        provider,
        model,
        key_fingerprint,
        tokens_usage.get("total_tokens"),
    )


class ChunkProcessor:
    """Asyncio-based stream chunk processor.

    Consumes chunks from an ``asyncio.Queue`` in a background ``asyncio.Task``,
    parses SSE/JSON deltas into the token usage and response-log accumulators,
    and — once the stream ends — delegates the blocking ``record_chat_observability``
    call to a threadpool via ``asyncio.to_thread`` so the event loop is never
    held on token calculation or optional log-file I/O. The prior ``threading.Thread`` implementation
    forced every chunk to cross a thread boundary; running in-loop avoids
    that overhead while the ``to_thread`` hop preserves non-blocking semantics
    for the heavy finalizer.
    """

    def __init__(
        self,
        req_headers,
        req_body_str,
        is_real_streaming,
        *,
        services: "AppServices",
        config_loader,
        cost_rate_registry,
        provider_name=None,
        provider_model=None,
        gateway_model=None,
        operation="chat",
        api_key_id=None,
        request: Request | None = None,
    ):
        self.req_headers = req_headers
        self.req_body_str = req_body_str
        self.is_real_streaming = is_real_streaming
        self.request = request
        self.services = services
        self.config_loader = config_loader
        self.cost_rate_registry = cost_rate_registry
        self.provider_name = provider_name
        self.provider_model = provider_model
        self.gateway_model = gateway_model
        self.operation = operation
        self.api_key_id = api_key_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_CHUNK_QUEUE_MAXSIZE)
        self.llm_response_accum = ""
        self._response_accum_bytes = 0
        self.tokens_usage = {
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "total_tokens": 0,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cost": 0,
            "operation": operation,
        }
        if provider_name:
            self.tokens_usage["provider"] = provider_name
        if provider_model:
            self.tokens_usage["model"] = provider_model
        if gateway_model:
            self.tokens_usage["gateway_model"] = gateway_model
        if api_key_id is not None:
            self.tokens_usage["api_key_id"] = api_key_id
        self._finished = False
        self._finishing = False
        self._log_written = False
        self._anthropic_tool_blocks: dict[object, dict[str, object]] = {}
        self._response_accum_truncated = False
        self._reasoning_header_written = False
        self._task: asyncio.Task | None = None
        self._finish_task: asyncio.Task | None = None
        self._observation_failed = False
        self._gzip_decision_made = False
        self._gzip_probe = bytearray()
        self._gzip_probe_lease: StreamObservationLease | None = None
        self._gzip_decompressor = None
        self._gzip_complete = False
        self._owned_observation_bytes = 0
        self._retained_observation_bytes = 0
        self._queued_observation_items = 0
        self._queue_progress_event = asyncio.Event()

    def _write_log_once(self):
        if self._log_written:
            return

        if self.request is not None:
            self.tokens_usage = enrich_tokens_usage(
                self.tokens_usage,
                self.request,
                self.gateway_model,
                self.operation,
                config_loader=self.config_loader,
                cost_rate_registry=self.cost_rate_registry,
            )
        _merge_request_usage_tracker(self.tokens_usage, self.request)
        _record_upstream_tokens_for_request(
            self.request,
            self.tokens_usage,
            self.services.upstream_routing_state,
        )
        _log_rtk_compression_stats(self.request)
        _record_chat_observability_with_rates(
            self.req_headers,
            self.req_body_str,
            self.llm_response_accum,
            self.tokens_usage,
            self.cost_rate_registry,
            self.services,
            self.request,
        )
        self._log_written = True

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = self.services.task_supervisor.create_task(
            self._run(),
            name="chat-stream-observer",
        )

    def _record_stream_failure(
        self,
        reason_code: str,
        payload: bytes,
        exception: BaseException | None = None,
    ) -> None:
        self.services.stream_observation_capacity.record_failure(reason_code)
        logger.warning(
            "Stream observation failure reason=%s type=%s length=%d sha256=%s request_id=%s",
            reason_code,
            safe_exception_type_name(exception) if exception is not None else "none",
            len(payload),
            hashlib.sha256(payload).hexdigest(),
            _safe_stream_request_id(self.request),
        )

    @staticmethod
    def _chunk_bytes(chunk: object) -> bytes:
        if isinstance(chunk, bytes):
            return chunk
        if isinstance(chunk, bytearray):
            return bytes(chunk)
        if isinstance(chunk, memoryview):
            return chunk.tobytes()
        if isinstance(chunk, str):
            return chunk.encode("utf-8")
        return str(chunk).encode("utf-8")

    async def enqueue_chunk(self, chunk: object) -> None:
        if self._finishing or self._finished:
            raise RuntimeError("Cannot enqueue a stream chunk after finish.")
        if self._task is not None and self._task.done():
            self._observation_failed = True
            return
        if self._observation_failed:
            return

        raw_chunk = self._chunk_bytes(chunk)
        if not raw_chunk:
            return
        if self._gzip_complete:
            self._fail_observation("gzip_trailing_data", raw_chunk)
            return

        if not self._gzip_decision_made:
            if not self._gzip_probe and raw_chunk == b"\x1f":
                probe_lease = await self._acquire_observation_lease(
                    1,
                    raw_chunk,
                    too_large_reason="chunk_too_large",
                )
                if probe_lease is None:
                    return
                self._gzip_probe.extend(raw_chunk)
                self._gzip_probe_lease = probe_lease
                self._owned_observation_bytes += probe_lease.remaining_bytes
                return
            probe_prefix = bytes(self._gzip_probe)
            self._gzip_probe.clear()
            probe_lease = self._gzip_probe_lease
            self._gzip_probe_lease = None
            self._gzip_decision_made = True
            if (probe_prefix + raw_chunk[:2]).startswith(b"\x1f\x8b"):
                try:
                    self._gzip_decompressor = zlib.decompressobj(
                        16 + zlib.MAX_WBITS
                    )
                except BaseException as exc:
                    if probe_lease is not None:
                        self._release_owned_lease(probe_lease)
                    self._fail_observation(
                        "gzip_initialization_failed",
                        raw_chunk,
                        exc,
                    )
                    raise
            if probe_prefix:
                if self._gzip_decompressor is not None:
                    try:
                        probe_output = self._gzip_decompressor.decompress(
                            probe_prefix,
                            self.services.stream_event_max_bytes,
                        )
                        probe_has_tail = bool(
                            self._gzip_decompressor.unconsumed_tail
                        )
                        probe_has_unused_data = bool(
                            self._gzip_decompressor.unused_data
                        )
                    except BaseException as exc:
                        if probe_lease is not None:
                            self._release_owned_lease(probe_lease)
                        self._fail_observation("gzip_invalid", probe_prefix, exc)
                        if isinstance(exc, zlib.error):
                            return
                        raise
                    if (
                        probe_output
                        or probe_has_tail
                        or probe_has_unused_data
                    ):
                        if probe_lease is not None:
                            self._release_owned_lease(probe_lease)
                        self._fail_observation("gzip_invalid", probe_prefix)
                        return
                    if probe_lease is not None:
                        self._release_owned_lease(probe_lease)
                else:
                    if probe_lease is None:
                        raise RuntimeError("Gzip probe bytes have no capacity lease.")
                    self._publish_observed_chunk(
                        probe_prefix,
                        probe_lease,
                        already_owned=True,
                    )
                if self._observation_failed:
                    return

        if self._gzip_decompressor is not None:
            await self._enqueue_gzip_chunk(raw_chunk)
            return
        await self._enqueue_observed_chunk(raw_chunk)

    def _fail_observation(
        self,
        reason_code: str,
        payload: bytes,
        exception: BaseException | None = None,
    ) -> None:
        self._observation_failed = True
        self._gzip_decompressor = None
        self._gzip_probe.clear()
        if self._gzip_probe_lease is not None:
            self._release_owned_lease(self._gzip_probe_lease)
            self._gzip_probe_lease = None
        self._record_stream_failure(reason_code, payload, exception)
        try:
            self.queue.put_nowait(_STREAM_ABORT)
        except asyncio.QueueFull:
            pass

    async def _acquire_observation_lease(
        self,
        byte_count: int,
        payload: bytes,
        *,
        too_large_reason: str,
    ) -> StreamObservationLease | None:
        capacity = self.services.stream_observation_capacity
        snapshot = capacity.snapshot
        if byte_count > snapshot.max_bytes:
            self._fail_observation(too_large_reason, payload)
            return None
        while self._owned_observation_bytes and (
            snapshot.waiters
            or snapshot.active_items >= snapshot.max_items
            or snapshot.active_bytes + byte_count > snapshot.max_bytes
        ):
            if self._queued_observation_items:
                if self._task is None:
                    self._fail_observation(
                        "observation_progress_exhausted",
                        payload,
                    )
                    self._drain_pending_observation_queue()
                    return None
                self._queue_progress_event.clear()
                await self._queue_progress_event.wait()
                snapshot = capacity.snapshot
                continue
            break
        if self._retained_observation_bytes and (
            snapshot.waiters
            or snapshot.active_bytes + byte_count > snapshot.max_bytes
        ):
            self._fail_observation("observation_progress_exhausted", payload)
            return None
        try:
            return await capacity.acquire(byte_count)
        except StreamObservationTooLarge as exc:
            self._fail_observation(too_large_reason, payload, exc)
            return None

    def _publish_observed_chunk(
        self,
        observed_chunk: bytes,
        lease: StreamObservationLease,
        *,
        allow_finishing: bool = False,
        already_owned: bool = False,
    ) -> None:
        try:
            if (
                self._observation_failed
                or self._finished
                or (self._finishing and not allow_finishing)
                or (self._task is not None and self._task.done())
            ):
                if already_owned:
                    self._release_owned_lease(lease)
                else:
                    lease.release_all()
                return
            self.queue.put_nowait((observed_chunk, lease))
            self._queued_observation_items += 1
            if not already_owned:
                self._owned_observation_bytes += lease.remaining_bytes
        except asyncio.QueueFull:
            if already_owned:
                self._release_owned_lease(lease)
            else:
                lease.release_all()
            self._fail_observation("queue_capacity_invariant", observed_chunk)
        except BaseException:
            if already_owned:
                self._release_owned_lease(lease)
            else:
                lease.release_all()
            raise

    def _release_owned_lease(self, lease: StreamObservationLease) -> None:
        byte_count = lease.remaining_bytes
        if byte_count > self._owned_observation_bytes:
            raise RuntimeError("Stream observation byte ownership underflow.")
        self._owned_observation_bytes -= byte_count
        lease.release_all()

    def _drain_pending_observation_queue(self) -> None:
        while True:
            try:
                pending_item = self.queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if pending_item is _STREAM_END or pending_item is _STREAM_ABORT:
                continue
            _, pending_lease = pending_item
            if self._queued_observation_items <= 0:
                raise RuntimeError("Stream observation queue ownership underflow.")
            self._queued_observation_items -= 1
            self._release_owned_lease(pending_lease)
        self._queue_progress_event.set()

    async def _enqueue_observed_chunk(
        self,
        observed_chunk: bytes,
        *,
        allow_finishing: bool = False,
    ) -> None:
        lease = await self._acquire_observation_lease(
            len(observed_chunk),
            observed_chunk,
            too_large_reason="chunk_too_large",
        )
        if lease is None:
            return
        self._publish_observed_chunk(
            observed_chunk,
            lease,
            allow_finishing=allow_finishing,
        )

    async def _enqueue_gzip_chunk(self, raw_chunk: bytes) -> None:
        event_limit = self.services.stream_event_max_bytes
        pending_input = raw_chunk
        drain_output = True
        while (pending_input or drain_output) and not self._observation_failed:
            lease = await self._acquire_observation_lease(
                event_limit,
                raw_chunk,
                too_large_reason="gzip_capacity_too_small",
            )
            if lease is None:
                return
            try:
                observed_chunk = self._gzip_decompressor.decompress(
                    pending_input,
                    event_limit,
                )
                pending_input = self._gzip_decompressor.unconsumed_tail
                trailing_data = bool(self._gzip_decompressor.unused_data)
                gzip_eof = self._gzip_decompressor.eof
            except BaseException as exc:
                lease.release_all()
                self._fail_observation("gzip_invalid", raw_chunk, exc)
                if isinstance(exc, zlib.error):
                    return
                raise

            if trailing_data:
                lease.release_all()
                self._fail_observation("gzip_trailing_data", raw_chunk)
                return
            if not observed_chunk:
                lease.release_all()
            else:
                unused_bytes = event_limit - len(observed_chunk)
                if unused_bytes:
                    lease.consume(unused_bytes)
                self._publish_observed_chunk(observed_chunk, lease)
                if self._observation_failed:
                    return

            if gzip_eof:
                self._gzip_decompressor = None
                self._gzip_complete = True
                return
            drain_output = len(observed_chunk) == event_limit
            if not pending_input and not drain_output:
                return

    async def finish(self) -> None:
        if self._finish_task is None:
            self._finishing = True
            try:
                self._finish_task = self.services.task_supervisor.create_task(
                    self._finish(),
                    name="chat-stream-finish",
                )
            except BaseException:
                self._finishing = False
                raise
        await asyncio.shield(self._finish_task)

    async def _finish(self) -> None:
        if self._finished:
            return
        if self._task is not None and self._task.done():
            self._finished = True
            return
        if self._gzip_probe and not self._observation_failed:
            pending_probe = bytes(self._gzip_probe)
            self._gzip_probe.clear()
            probe_lease = self._gzip_probe_lease
            self._gzip_probe_lease = None
            self._gzip_decision_made = True
            if probe_lease is None:
                raise RuntimeError("Gzip probe bytes have no capacity lease.")
            self._publish_observed_chunk(
                pending_probe,
                probe_lease,
                allow_finishing=True,
                already_owned=True,
            )
        if (
            self._gzip_decompressor is not None
            and not self._observation_failed
            and not self._gzip_decompressor.eof
        ):
            self._fail_observation("gzip_invalid", b"")
        await self.queue.put(_STREAM_END)
        self._finished = True

    async def wait(self, timeout: float) -> bool:
        """Wait for the processing task to finish within ``timeout`` seconds.

        Returns True on clean completion, False on timeout. On timeout, the
        underlying task is shielded and remains owned by the process task
        supervisor, which drains it during application shutdown.
        """
        if self._task is None:
            return True
        try:
            await asyncio.wait_for(asyncio.shield(self._task), timeout=timeout)
            return True
        except asyncio.TimeoutError:
            return False
        except Exception:
            return True

    def done(self) -> bool:
        return self._task is not None and self._task.done()

    def task_exception(self) -> BaseException | None:
        if self._task is None or not self._task.done():
            return None
        try:
            return self._task.exception()
        except asyncio.CancelledError as exc:
            return exc

    def _append_log_text(self, text: str) -> None:
        if not text or self._response_accum_truncated:
            return

        remaining = STREAM_LOG_CAPTURE_MAX_CHARS - self._response_accum_bytes
        if remaining <= 0:
            self._response_accum_truncated = True
            return

        encoded = text.encode("utf-8")
        if len(encoded) <= remaining:
            self.llm_response_accum += text
            self._response_accum_bytes += len(encoded)
            return

        marker = STREAM_LOG_TRUNCATION_MARKER.encode("utf-8")
        safe_remaining = max(remaining - len(marker), 0)
        if safe_remaining > 0:
            prefix = encoded[:safe_remaining].decode("utf-8", errors="ignore")
            self.llm_response_accum += prefix
            self._response_accum_bytes += len(prefix.encode("utf-8"))
        if len(marker) <= STREAM_LOG_CAPTURE_MAX_CHARS - self._response_accum_bytes:
            self.llm_response_accum += STREAM_LOG_TRUNCATION_MARKER
            self._response_accum_bytes += len(marker)
        self._response_accum_truncated = True

    def _append_reasoning_piece(self, reasoning_piece: str) -> None:
        if not reasoning_piece:
            return

        # self._reasoning_header_written tracks the same fact the old
        # "[Reasoning]:" not in self.llm_response_accum scan did, without
        # re-scanning the whole (up to STREAM_LOG_CAPTURE_MAX_CHARS) accumulator
        # on every streamed delta.
        if not self._reasoning_header_written:
            self._append_log_text("\n\n[Reasoning]:\n")
            self._reasoning_header_written = True
        self._append_log_text(reasoning_piece)

    def _append_response_piece(self, content_piece: str, *, after_reasoning: bool = False) -> None:
        if not content_piece:
            return

        if after_reasoning or self._reasoning_header_written:
            if not self.llm_response_accum.endswith("\n\n[Response]:\n"):
                self._append_log_text("\n\n[Response]:\n")
        self._append_log_text(content_piece)

    def _append_anthropic_tool_input(
        self,
        block_index: object,
        payload: object,
        *,
        tool_name: object | None = None,
    ) -> None:
        if self._response_accum_truncated:
            self._anthropic_tool_blocks.clear()
            return
        if self._observation_failed:
            return
        if (
            isinstance(block_index, str)
            and len(block_index.encode("utf-8")) > STREAM_TOOL_STATE_KEY_MAX_BYTES
        ):
            self._anthropic_tool_blocks.clear()
            self._fail_observation("tool_state_key_too_large", b"")
            return
        if (
            isinstance(tool_name, str)
            and len(tool_name.encode("utf-8")) > STREAM_TOOL_STATE_KEY_MAX_BYTES
        ):
            self._anthropic_tool_blocks.clear()
            self._fail_observation("tool_state_name_too_large", b"")
            return
        try:
            state = self._anthropic_tool_blocks.get(block_index)
        except TypeError:
            self._anthropic_tool_blocks.clear()
            self._fail_observation("tool_state_key_invalid", b"")
            return
        if state is None:
            if len(self._anthropic_tool_blocks) >= STREAM_TOOL_STATE_MAX_ITEMS:
                self._anthropic_tool_blocks.clear()
                self._fail_observation("tool_state_capacity_exhausted", b"")
                return
            state = {
                "name": (
                    tool_name
                    if isinstance(tool_name, str) and tool_name
                    else f"tool_{block_index}"
                ),
                "header_written": False,
            }
            self._anthropic_tool_blocks[block_index] = state
        if isinstance(tool_name, str) and tool_name:
            state["name"] = tool_name

        if not state["header_written"]:
            self._append_log_text(f"\n\n[Tool Use: {state['name']}]\n")
            state["header_written"] = True

        if isinstance(payload, str):
            self._append_log_text(payload)
        elif payload not in (None, {}):
            self._append_log_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        if self._response_accum_truncated:
            self._anthropic_tool_blocks.clear()

    def _process_decoded_parts(self, parts, *, canonical_events: bool = False):
        for part in parts:
            decoded_chunk = None
            try:
                stripped_part = part.strip()
                if stripped_part == "[DONE]":
                    continue
                if canonical_events:
                    decoded_chunk = stripped_part
                elif stripped_part.startswith("{"):
                    decoded_chunk = stripped_part
                else:
                    data_lines = [
                        line[len("data:"):].lstrip()
                        for line in part.splitlines()
                        if line.startswith("data:")
                    ]
                    if data_lines:
                        decoded_chunk = "\n".join(data_lines)

                if not decoded_chunk:
                    if canonical_events:
                        raise ValueError("empty canonical SSE data payload")
                    continue

                chunk_json = json.loads(decoded_chunk)
                if chunk_json is None:
                    continue
                if not isinstance(chunk_json, dict):
                    if canonical_events:
                        raise ValueError("canonical SSE data payload is not an object")
                    continue
                
                # --- OpenAI Format Processing ---
                if "choices" in chunk_json:
                    for choice in chunk_json["choices"]:
                        delta = choice.get("delta", {})
                        message = choice.get("message", {})
                        
                        # Extract from delta (streaming) or message (non-streaming)
                        content_piece = delta.get("content") or message.get("content")
                        reasoning_piece = delta.get("reasoning_content") or delta.get("reasoning") or \
                                         message.get("reasoning_content") or message.get("reasoning")
                        
                        if reasoning_piece:
                            self._append_reasoning_piece(reasoning_piece)
                            
                        if content_piece:
                            self._append_response_piece(content_piece, after_reasoning=bool(reasoning_piece))
                
                # --- Anthropic Format Processing ---
                # Handled via message_start, content_block_start, content_block_delta, message_delta events
                event_type = chunk_json.get("type")
                if event_type == "content_block_start":
                    content_block = chunk_json.get("content_block", {})
                    if isinstance(content_block, dict) and content_block.get("type") == "tool_use":
                        self._append_anthropic_tool_input(
                            chunk_json.get("index"),
                            content_block.get("input"),
                            tool_name=content_block.get("name"),
                        )

                if event_type == "content_block_delta":
                    delta = chunk_json.get("delta", {})
                    # content_block_delta in Anthropic format has "text" or "thinking"
                    text_piece = delta.get("text")
                    thinking_piece = delta.get("thinking")
                    partial_json = delta.get("partial_json")
                    
                    if thinking_piece:
                        self._append_reasoning_piece(thinking_piece)
                        
                    if text_piece:
                        self._append_response_piece(text_piece, after_reasoning=bool(thinking_piece))
                    if partial_json:
                        self._append_anthropic_tool_input(chunk_json.get("index"), partial_json)

                if event_type == "response.output_text.delta":
                    delta_text = chunk_json.get("delta")
                    annotations = chunk_json.get("annotations")
                    if isinstance(delta_text, str) and delta_text:
                        if isinstance(annotations, list) and "thought" in annotations:
                            self._append_reasoning_piece(delta_text)
                        else:
                            self._append_response_piece(delta_text)

                if event_type == "response.output_item.added":
                    item = chunk_json.get("item", {})
                    if isinstance(item, dict) and item.get("type") == "function_call":
                        self._append_anthropic_tool_input(
                            item.get("id") or chunk_json.get("output_index"),
                            item.get("arguments", ""),
                            tool_name=item.get("name"),
                        )

                if event_type == "response.function_call_arguments.delta":
                    self._append_anthropic_tool_input(
                        chunk_json.get("item_id") or chunk_json.get("output_index"),
                        chunk_json.get("delta"),
                    )

                # Non-streaming Anthropic format
                if "content" in chunk_json and isinstance(chunk_json["content"], list):
                    for block in chunk_json["content"]:
                        if not isinstance(block, dict):
                            continue
                        block_type = block.get("type")
                        if block_type == "text":
                            self._append_response_piece(block.get("text", ""))
                        elif block_type == "thinking":
                            self._append_reasoning_piece(block.get("thinking", ""))
                        elif block_type == "tool_use":
                            self._append_anthropic_tool_input(
                                block.get("id"),
                                block.get("input"),
                                tool_name=block.get("name"),
                            )
                
                # Usage extraction (handles both OpenAI and Anthropic nested formats)
                # For Anthropic streaming, usage is in message_start or message_delta
                # For Anthropic non-streaming, it is in top-level usage or message.usage
                usage_candidate = chunk_json.get("usage")
                usage_payload = chunk_json
                if not usage_candidate and "message" in chunk_json and isinstance(chunk_json["message"], dict):
                    usage_candidate = chunk_json["message"].get("usage")
                if not usage_candidate and "response" in chunk_json and isinstance(chunk_json["response"], dict):
                    usage_candidate = chunk_json["response"].get("usage")
                    if usage_candidate:
                        usage_payload = {"usage": usage_candidate}
                
                if usage_candidate:
                    new_usage = get_token_usage(usage_payload, include_internal=True)
                    for key, value in new_usage.items():
                        # Only update if the new value is non-zero
                        # This prevents zeroing out cumulative counts in multi-event streams (Anthropic)
                        if isinstance(value, (int, float)) and value > 0:
                            self.tokens_usage[key] = value
                        elif isinstance(value, str) and value:
                            self.tokens_usage[key] = value
                    
                    # Recalculate total
                    self.tokens_usage["total_tokens"] = self.tokens_usage.get("prompt_tokens", 0) + \
                                                       self.tokens_usage.get("completion_tokens", 0)
                    
                    if self.provider_name:
                        self.tokens_usage["provider"] = self.provider_name
                    if self.provider_model:
                        self.tokens_usage["model"] = self.provider_model
                    if self.gateway_model:
                        self.tokens_usage["gateway_model"] = self.gateway_model
                    if self.operation:
                        self.tokens_usage["operation"] = self.operation

                if "error" in chunk_json:
                    self._append_log_text("[Upstream error event]")
            except Exception as ex:
                payload = (
                    decoded_chunk.encode("utf-8")
                    if isinstance(decoded_chunk, str)
                    else str(part).encode("utf-8")
                )
                self._fail_observation("event_payload_invalid", payload, ex)
                return

    @staticmethod
    def _retain_lease(
        retained_lease: StreamObservationLease | None,
        lease: StreamObservationLease,
    ) -> StreamObservationLease:
        if retained_lease is None:
            lease.release_item()
            return lease
        retained_lease.absorb(lease)
        return retained_lease

    def _release_consumed_bytes(
        self,
        retained_lease: StreamObservationLease | None,
        byte_count: int,
    ) -> StreamObservationLease | None:
        if byte_count == 0:
            return retained_lease
        if retained_lease is None:
            raise RuntimeError("SSE framer consumed bytes without a capacity lease.")
        if byte_count > self._owned_observation_bytes:
            raise RuntimeError("Stream observation byte ownership underflow.")
        retained_lease.consume(byte_count)
        self._owned_observation_bytes -= byte_count
        return None if retained_lease.released else retained_lease

    async def _run(self):
        framer = SSEFramer(max_event_bytes=self.services.stream_event_max_bytes)
        retained_lease: StreamObservationLease | None = None
        non_stream_buffer = bytearray()
        cancelled = False
        try:
            while True:
                item = await self.queue.get()
                if item is _STREAM_ABORT:
                    framer.abort()
                    non_stream_buffer.clear()
                    if retained_lease is not None:
                        self._release_owned_lease(retained_lease)
                        retained_lease = None
                    self._retained_observation_bytes = 0
                    continue
                if item is _STREAM_END:
                    if self.is_real_streaming and not self._observation_failed:
                        try:
                            batch = framer.finish()
                        except SSEFramingError as exc:
                            self._observation_failed = True
                            self._record_stream_failure(
                                exc.reason_code,
                                b"",
                                exc,
                            )
                            if retained_lease is not None:
                                self._release_owned_lease(retained_lease)
                                retained_lease = None
                            self._retained_observation_bytes = 0
                        else:
                            retained_lease = self._release_consumed_bytes(
                                retained_lease,
                                batch.consumed_bytes,
                            )
                            self._retained_observation_bytes = (
                                retained_lease.remaining_bytes
                                if retained_lease is not None
                                else 0
                            )
                            self._process_decoded_parts(
                                (event.data for event in batch.events),
                                canonical_events=True,
                            )
                    elif non_stream_buffer and not self._observation_failed:
                        try:
                            decoded_body = bytes(non_stream_buffer).decode(
                                "utf-8",
                                errors="strict",
                            )
                        except UnicodeDecodeError as exc:
                            self._observation_failed = True
                            self._record_stream_failure(
                                "non_stream_invalid_utf8",
                                bytes(non_stream_buffer),
                                exc,
                            )
                        else:
                            self._process_decoded_parts((decoded_body,))
                        if retained_lease is not None:
                            self._release_owned_lease(retained_lease)
                            retained_lease = None
                        self._retained_observation_bytes = 0
                        non_stream_buffer.clear()
                    break

                chunk, lease = item
                if self._queued_observation_items <= 0:
                    raise RuntimeError("Stream observation queue ownership underflow.")
                self._queued_observation_items -= 1
                self._queue_progress_event.set()
                if self._observation_failed:
                    framer.abort()
                    non_stream_buffer.clear()
                    if retained_lease is not None:
                        self._release_owned_lease(retained_lease)
                        retained_lease = None
                    self._retained_observation_bytes = 0
                    self._release_owned_lease(lease)
                    continue
                retained_lease = self._retain_lease(retained_lease, lease)
                self._retained_observation_bytes = retained_lease.remaining_bytes

                if self.is_real_streaming:
                    try:
                        batch = framer.feed(chunk)
                    except SSEFramingError as exc:
                        self._observation_failed = True
                        self._record_stream_failure(exc.reason_code, chunk, exc)
                        framer.abort()
                        if retained_lease is not None:
                            self._release_owned_lease(retained_lease)
                            retained_lease = None
                        self._retained_observation_bytes = 0
                        continue
                    retained_lease = self._release_consumed_bytes(
                        retained_lease,
                        batch.consumed_bytes,
                    )
                    self._retained_observation_bytes = (
                        retained_lease.remaining_bytes
                        if retained_lease is not None
                        else 0
                    )
                    self._process_decoded_parts(
                        (event.data for event in batch.events),
                        canonical_events=True,
                    )
                    continue

                if (
                    len(non_stream_buffer) + len(chunk)
                    > self.services.json_response_max_bytes
                ):
                    self._observation_failed = True
                    self._record_stream_failure(
                        "non_stream_body_too_large",
                        chunk,
                    )
                    non_stream_buffer.clear()
                    if retained_lease is not None:
                        self._release_owned_lease(retained_lease)
                        retained_lease = None
                    self._retained_observation_bytes = 0
                    continue
                non_stream_buffer.extend(chunk)
        except asyncio.CancelledError:
            cancelled = True
            self._record_stream_failure("processor_cancelled", b"")
            raise
        except BaseException as exc:
            self._record_stream_failure("processor_failed", b"", exc)
            raise
        finally:
            framer.abort()
            non_stream_buffer.clear()
            if retained_lease is not None:
                self._release_owned_lease(retained_lease)
            self._retained_observation_bytes = 0
            self._gzip_decompressor = None
            self._gzip_probe.clear()
            self._anthropic_tool_blocks.clear()
            if self._gzip_probe_lease is not None:
                self._release_owned_lease(self._gzip_probe_lease)
                self._gzip_probe_lease = None
            self._drain_pending_observation_queue()

            if not cancelled:
                try:
                    await anyio.to_thread.run_sync(
                        self._write_log_once,
                        abandon_on_cancel=False,
                    )
                except Exception as exc:
                    self._record_stream_failure(
                        "observability_write_failed",
                        b"",
                        exc,
                    )

def _schedule_active_request_prompt_estimate(
    request_id: str | None,
    registry: "ActiveRequestsRegistry",
    task_supervisor: "TaskSupervisor",
    req_body_str: str,
    gateway_model: str | None,
) -> None:
    """Reflect an estimated prompt token count on the in-flight usage record.

    Tiktoken encoding of a large body can take hundreds of milliseconds, so it
    runs in a worker thread as a background task instead of delaying the
    request path; the update is a no-op if the request finishes first.
    """
    if not request_id or not req_body_str:
        return

    async def _estimate_and_update() -> None:
        try:
            estimated = await anyio.to_thread.run_sync(
                estimate_prompt_tokens, req_body_str, gateway_model
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("ChatLogging: failed to estimate in-flight prompt tokens")
            return
        if estimated > 0:
            registry.update(
                request_id,
                prompt_tokens=estimated,
                total_tokens=estimated,
                is_estimated=True,
            )

    task_supervisor.create_task(
        _estimate_and_update(),
        name="chat-prompt-estimate",
    )


class _ChatResponseLifecycle:
    __slots__ = (
        "_active_registry",
        "_binding_attempted",
        "_chunk_processor",
        "_config_loader",
        "_cost_rate_registry",
        "_gateway_model",
        "_operation",
        "_owner",
        "_req_body_str",
        "_req_headers",
        "_request",
        "_request_id",
        "_request_started_at",
        "_route_template",
        "_services",
        "_transport_finished",
        "_wire_mode",
    )

    def __init__(
        self,
        *,
        request: Request,
        services: AppServices,
        runtime_snapshot: RuntimeSnapshot,
        owner: ChatTerminalOwner,
        route_template: str,
        operation: str,
    ) -> None:
        self._request = request
        self._services = services
        self._owner = owner
        self._route_template = route_template
        self._operation = operation
        self._config_loader = runtime_snapshot.config_loader
        self._cost_rate_registry = runtime_snapshot.cost_rate_registry
        self._active_registry = services.active_requests_registry
        self._request_id = active_request_id(request)
        self._request_started_at = time.monotonic()
        self._binding_attempted = False
        self._gateway_model: str | None = None
        self._req_headers: dict = {}
        self._req_body_str = ""
        self._chunk_processor: ChunkProcessor | None = None
        self._transport_finished = False
        self._wire_mode: WireMode | None = None

    def bind_request(self, *, allow_incomplete: bool = False) -> None:
        if self._binding_attempted:
            return

        try:
            request_body = captured_request_body(self._request.scope)
        except ResponseObservationStateError:
            if not allow_incomplete:
                raise
            request_body = b""
        self._req_headers = dict(self._request.headers)
        self._req_body_str = _decode_body(request_body, self._req_headers)
        gateway_model: object = None
        streaming: object = False
        try:
            payload = json.loads(self._req_body_str)
        except (TypeError, ValueError):
            payload = None
        if isinstance(payload, dict):
            gateway_model = payload.get("model")
            streaming = payload.get("stream", False)

        if isinstance(streaming, bool) and isinstance(gateway_model, str):
            try:
                bind_chat_request(
                    self._owner,
                    gateway_model=gateway_model,
                    streaming=streaming,
                    dialect=_chat_stream_dialect(self._route_template),
                )
            except AccountingValidationError:
                pass
            else:
                self._gateway_model = gateway_model

        self._binding_attempted = True
        self._active_registry.update(
            self._request_id,
            gateway_model=self._gateway_model,
            operation=self._operation,
        )
        _schedule_active_request_prompt_estimate(
            self._request_id,
            self._active_registry,
            self._services.task_supervisor,
            self._req_body_str,
            self._gateway_model,
        )

    def _ensure_chunk_processor(self, start: ResponseStart) -> ChunkProcessor:
        wire_mode = start.wire_mode
        self.bind_request(allow_incomplete=start.status_code >= 400)
        if self._wire_mode is not None and self._wire_mode is not wire_mode:
            raise AccountingValidationError
        if self._chunk_processor is None:
            processor = ChunkProcessor(
                self._req_headers,
                self._req_body_str,
                wire_mode is WireMode.SSE,
                services=self._services,
                config_loader=self._config_loader,
                cost_rate_registry=self._cost_rate_registry,
                provider_name=getattr(
                    self._request.state,
                    "llmgateway_provider",
                    None,
                ),
                provider_model=getattr(
                    self._request.state,
                    "llmgateway_provider_model",
                    None,
                ),
                gateway_model=self._gateway_model,
                operation=self._operation,
                api_key_id=getattr(self._request.state, "api_key_id", None),
                request=self._request,
            )
            processor.start()
            self._chunk_processor = processor
            self._wire_mode = wire_mode
        return self._chunk_processor

    async def observe_body_chunk(self, start: ResponseStart, body: bytes) -> None:
        processor = self._ensure_chunk_processor(start)
        await processor.enqueue_chunk(body)

    async def finish_transport(self, result: TransportResult) -> None:
        if not isinstance(result, TransportResult):
            raise AccountingValidationError
        if self._transport_finished:
            return
        self._transport_finished = True
        try:
            processor = self._chunk_processor
            if processor is None:
                return
            processor.tokens_usage["duration_ms"] = int(
                (time.monotonic() - self._request_started_at) * 1000
            )
            await processor.finish()
            completed = await processor.wait(CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS)
            if completed:
                task_exception = processor.task_exception()
                if task_exception is not None:
                    logger.error(
                        "Stream observation task failed type=%s request_id=%s",
                        safe_exception_type_name(task_exception),
                        _safe_stream_request_id(self._request),
                    )
            else:
                logger.warning(
                    "ChunkProcessor did not finish within %.1fs; "
                    "leaving it running to avoid blocking the event loop.",
                    CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS,
                )
        finally:
            self._active_registry.finish(self._request_id)


async def prepare_chat_response_observation(request: Request) -> None:
    """Publish the exact chat terminal owner before endpoint execution."""
    effective_route = resolve_effective_http_route(
        request.scope,
        request.app.routes,
    )
    if effective_route is None:
        return
    billing_policy = classify_billing_policy(
        effective_route.method,
        effective_route.route_template,
    )
    if billing_policy is None or billing_policy.operation != "chat":
        return

    operation = _extract_operation_from_url(effective_route.route_template)
    if operation is None:
        raise AccountingValidationError
    accounting_context = get_accounting_request_context(request.scope)
    if (
        accounting_context is None
        or accounting_context.method != effective_route.method
        or accounting_context.route_template != effective_route.route_template
        or accounting_context.policy != billing_policy
    ):
        raise AccountingValidationError
    try:
        services = request.app.state.services
        runtime_snapshot = request.state.runtime_snapshot
    except (AttributeError, KeyError, TypeError):
        raise AccountingValidationError from None
    if not isinstance(services, AppServices) or not isinstance(
        runtime_snapshot,
        RuntimeSnapshot,
    ):
        raise AccountingValidationError

    enable_request_capture(request.scope, settings.max_request_body_bytes)
    owner = take_chat_terminal_owner(request)
    handoff = None
    try:
        handoff = install_chat_terminal_handoff(request)
        lifecycle = _ChatResponseLifecycle(
            request=request,
            services=services,
            runtime_snapshot=runtime_snapshot,
            owner=owner,
            route_template=effective_route.route_template,
            operation=operation,
        )
        observation = build_chat_response_observation(
            owner,
            handoff,
            services,
            bind_request=lifecycle.bind_request,
            observe_body_chunk=lifecycle.observe_body_chunk,
            transport_finished=lifecycle.finish_transport,
        )
        publish_response_observation(request.scope, observation)
    except BaseException as exc:
        state = request.scope.get("state")
        if (
            handoff is not None
            and isinstance(state, dict)
            and state.get(CHAT_TERMINAL_HANDOFF_STATE_KEY) is handoff
        ):
            del state[CHAT_TERMINAL_HANDOFF_STATE_KEY]
        await _release_chat_owner(owner, primary_error=exc)
        raise


def get_token_usage(chunk_data, *, include_internal: bool = False):
    """
    Extracts token usage information from the chunk data.
    """
    tokens_usage = extract_tokens_usage(chunk_data)
    if not include_internal:
        tokens_usage.pop(UPSTREAM_COST_PRESENT_KEY, None)
    return tokens_usage
