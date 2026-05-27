import os
import asyncio
import json
import glob
import codecs
import anyio
import time
from datetime import datetime
from uuid import uuid4
import logging
from pprint import pformat
from ..config.settings import settings
from fastapi import Request, Response
from fastapi.responses import StreamingResponse
from typing import Callable
import gzip
import io
from ..db.api_keys_db import ApiKeysDB
from ..db.tokens_usage_db import TokensUsageDB
from ..services.rate_limiter import RateLimiter
from ..services.access_control import UsdBudgetLedger
from ..services.active_requests import update_active_request
from ..services.upstream_routing_state import UpstreamRoutingState
from ..utils.log_redaction import redact_headers_for_log, redact_request_body_text
from ..utils.usage_tracking import (
    backfill_zero_token_counts,
    enrich_tokens_usage as enrich_usage_metadata,
    extract_tokens_usage,
    initialize_tokens_usage,
)

logger = logging.getLogger(__name__)
_STREAM_END = object()
STREAM_CHUNK_QUEUE_MAXSIZE = 0  # Unbounded — avoids blocking the event loop on put()
STREAM_LOG_CAPTURE_MAX_CHARS = 262_144
STREAM_LOG_TRUNCATION_MARKER = "\n\n[TRUNCATED]\n"
_LOG_CLEANUP_INTERVAL = 10  # Only check for old log cleanup every N writes
# Upper bound for waiting on the chunk-processing task during shutdown of a streaming
# response. Guards against indefinite hangs if the worker is blocked on disk I/O or
# an external SQLite lock — observability must not become a liveness hazard.
CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS = 5.0
_log_write_counter = 0


class ChatLoggingState:
    """Encapsulates the mutable module-level state for chat logging middleware.

    Instead of bare globals (``tokens_usage_db``, ``api_keys_db``, etc.) the
    state lives in a single object.  The module-level ``state`` instance is the
    only public handle — tests and the lifespan bind attributes on it.
    """

    tokens_usage_db: TokensUsageDB | None = None
    api_keys_db: ApiKeysDB | None = None
    rate_limiter: RateLimiter | None = None
    usd_budget_ledger: UsdBudgetLedger | None = None


# Module-level singleton — the only public handle for state binding.
state = ChatLoggingState()


# Backwards-compatible setters kept for external callers (lifespan, tests).
# Each delegates to the ``state`` instance so that no code path can diverge.
def set_tokens_usage_db(db: TokensUsageDB | None) -> None:
    """Bind (or unbind) the TokensUsageDB used for chat usage recording."""
    state.tokens_usage_db = db


def set_api_keys_db(db: ApiKeysDB | None) -> None:
    """Bind (or unbind) the ApiKeysDB used for per-key budget tracking."""
    state.api_keys_db = db


def set_rate_limiter(limiter: RateLimiter | None) -> None:
    """Bind (or unbind) the RateLimiter used for per-key TPM attribution."""
    state.rate_limiter = limiter


def set_usd_budget_ledger(ledger: UsdBudgetLedger | None) -> None:
    """Bind (or unbind) the in-memory USD reservation ledger."""
    state.usd_budget_ledger = ledger


def _require_tokens_usage_db() -> TokensUsageDB:
    if state.tokens_usage_db is None:
        raise RuntimeError(
            "chat_logging.tokens_usage_db is not bound. "
            "Call set_tokens_usage_db() from the application lifespan or test setup before recording usage."
        )
    return state.tokens_usage_db


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


def record_tokens_usage(tokens_usage):
    try:
        usd_budget_reserved = bool(tokens_usage.pop("_usd_budget_reserved", False))
        key_tpm_limit = tokens_usage.pop("_key_tpm_limit", None)
        # Ensure total_tokens is recalculated before saving
        tokens_usage["total_tokens"] = tokens_usage.get("prompt_tokens", 0) + tokens_usage.get("completion_tokens", 0)

        logger.info(f"Recording token usage: {tokens_usage.get('prompt_tokens')}+{tokens_usage.get('completion_tokens')}={tokens_usage.get('total_tokens')} for model {tokens_usage.get('gateway_model')}")
        try:
            _require_tokens_usage_db().insert_usage(tokens_usage)
        except Exception as db_error:
            logger.error("Failed to insert token usage data into database: %s", db_error, exc_info=True)

        key_id = tokens_usage.get("api_key_id")
        if key_id is not None and state.api_keys_db is not None:
            try:
                state.api_keys_db.record_spent(int(key_id), float(tokens_usage.get("cost") or 0.0))
            except Exception as budget_error:
                logger.error("Failed to update spent_usd for key %s: %s", key_id, budget_error)

        if key_id is not None and usd_budget_reserved and state.usd_budget_ledger is not None:
            try:
                state.usd_budget_ledger.commit(
                    int(key_id),
                    float(tokens_usage.get("cost") or 0.0),
                )
            except Exception as ledger_error:
                logger.error("Failed to commit USD budget reservation for key %s: %s", key_id, ledger_error)

        if key_id is not None and state.rate_limiter is not None:
            try:
                state.rate_limiter.add_tokens(int(key_id), int(tokens_usage.get("total_tokens") or 0), tpm_limit=key_tpm_limit)
            except Exception as tpm_error:
                logger.error("Failed to attribute tokens to RateLimiter for key %s: %s", key_id, tpm_error)
    except Exception as usage_error:
        logger.error("Failed to record token usage side effects: %s", usage_error, exc_info=True)


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
        os.makedirs("logs", exist_ok=True)
        log_path = os.path.join("./logs", filename)

        # Write the new log file
        with open(log_path, "w", encoding="utf-8") as f:
            log_content = log_content.replace("\\n\\n", "\r\n\r\n").replace("\\n", "\r\n")
            f.write(log_content)

        # Clean up old logs periodically (not every write — glob+sort is expensive)
        global _log_write_counter
        _log_write_counter += 1
        if _log_write_counter % _LOG_CLEANUP_INTERVAL == 0:
            log_files = sorted(glob.glob(os.path.join("./logs", "*.txt")), key=os.path.getmtime)
            max_logs = settings.log_file_limit or 50
            while len(log_files) > max_logs:
                try:
                    os.remove(log_files.pop(0))
                except Exception:
                    pass
    except Exception as e:
        logger.error(f"Failed to write chat log: {e}", exc_info=True)


def record_chat_observability(req_headers, req_body_str, llm_response_accum, tokens_usage):
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

    record_tokens_usage(tokens_usage)

    if settings.log_chat_messages:
        write_log(req_headers, req_body_str, llm_response_accum, tokens_usage)


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


def _mark_usd_budget_reservation_finalized(request: Request) -> None:
    if getattr(request.state, "usd_budget_reserved", False):
        request.state.usd_budget_finalized = True


def _release_usd_budget_reservation_for_request(request: Request) -> None:
    if not getattr(request.state, "usd_budget_reserved", False):
        return
    if getattr(request.state, "usd_budget_finalized", False):
        return

    key_id = getattr(request.state, "usd_budget_reserved_key_id", None)
    if key_id is not None and state.usd_budget_ledger is not None:
        try:
            state.usd_budget_ledger.release(int(key_id))
        except Exception as ledger_error:
            logger.error("Failed to release USD budget reservation for key %s: %s", key_id, ledger_error)
    request.state.usd_budget_finalized = True


def _extract_gateway_model_from_request_body(req_body_str: str) -> str | None:
    if not req_body_str:
        return None

    try:
        request_payload = json.loads(req_body_str)
    except Exception:
        logger.debug("Could not parse request body to extract gateway model for usage statistics.")
        return None

    gateway_model = request_payload.get("model")
    if isinstance(gateway_model, str) and gateway_model:
        return gateway_model
    return None


def _extract_operation_from_url(url_path: str) -> str | None:
    path = url_path.rstrip("/")
    if path.endswith("/chat/completions") or path.endswith("/responses"):
        return "chat"
    if path.endswith("/messages"):
        return "messages"
    return None


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


def enrich_tokens_usage(tokens_usage, request: Request, gateway_model: str | None = None, operation: str | None = None):
    if operation is None:
        operation = _extract_operation_from_url(request.url.path)
    return enrich_usage_metadata(tokens_usage, request, gateway_model=gateway_model, operation=operation)


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


def _record_upstream_tokens_for_request(request: Request | None, tokens_usage: dict) -> None:
    if request is None:
        return
    upstream_state = getattr(request.app.state, "upstream_routing_state", None)
    if not isinstance(upstream_state, UpstreamRoutingState):
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
    held on disk/SQLite I/O. The prior ``threading.Thread`` implementation
    forced every chunk to cross a thread boundary; running in-loop avoids
    that overhead while the ``to_thread`` hop preserves non-blocking semantics
    for the heavy finalizer.
    """

    def __init__(
        self,
        req_headers,
        req_body_str,
        is_real_streaming,
        provider_name=None,
        provider_model=None,
        gateway_model=None,
        operation="chat",
        api_key_id=None,
        usd_budget_reserved=False,
        key_tpm_limit=None,
        request: Request | None = None,
    ):
        self.req_headers = req_headers
        self.req_body_str = req_body_str
        self.is_real_streaming = is_real_streaming
        self.request = request
        self.provider_name = provider_name
        self.provider_model = provider_model
        self.gateway_model = gateway_model
        self.operation = operation
        self.api_key_id = api_key_id
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=STREAM_CHUNK_QUEUE_MAXSIZE)
        self.llm_response_accum = ""
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
        if usd_budget_reserved:
            self.tokens_usage["_usd_budget_reserved"] = True
        if key_tpm_limit is not None:
            self.tokens_usage["_key_tpm_limit"] = key_tpm_limit
        self._finished = False
        self._log_written = False
        self._anthropic_tool_blocks: dict[object, dict[str, object]] = {}
        self._response_accum_truncated = False
        self._task: asyncio.Task | None = None

    def _write_log_once(self):
        if self._log_written:
            return

        _merge_request_usage_tracker(self.tokens_usage, self.request)
        _record_upstream_tokens_for_request(self.request, self.tokens_usage)
        _log_rtk_compression_stats(self.request)
        record_chat_observability(
            self.req_headers,
            self.req_body_str,
            self.llm_response_accum,
            self.tokens_usage,
        )
        self._log_written = True

    def start(self) -> None:
        if self._task is not None:
            return
        self._task = asyncio.create_task(self._run())

    def enqueue_chunk(self, chunk) -> None:
        # Queue is unbounded (STREAM_CHUNK_QUEUE_MAXSIZE=0), so put_nowait
        # never blocks and never raises QueueFull. Using the non-async form
        # keeps the producer path (enqueueing_generator) free of awaits.
        self.queue.put_nowait(chunk)

    async def finish(self) -> None:
        if self._finished:
            return

        self._finished = True
        await self.queue.put(_STREAM_END)

    async def wait(self, timeout: float) -> bool:
        """Wait for the processing task to finish within ``timeout`` seconds.

        Returns True on clean completion, False on timeout. On timeout, the
        underlying task is shielded so it continues in the background — the
        stream completion path must not stall on a stuck worker.
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

        remaining = STREAM_LOG_CAPTURE_MAX_CHARS - len(self.llm_response_accum)
        if remaining <= 0:
            self.llm_response_accum += STREAM_LOG_TRUNCATION_MARKER
            self._response_accum_truncated = True
            return

        if len(text) <= remaining:
            self.llm_response_accum += text
            return

        safe_remaining = max(remaining - len(STREAM_LOG_TRUNCATION_MARKER), 0)
        if safe_remaining > 0:
            self.llm_response_accum += text[:safe_remaining]
        self.llm_response_accum += STREAM_LOG_TRUNCATION_MARKER
        self._response_accum_truncated = True

    def _append_reasoning_piece(self, reasoning_piece: str) -> None:
        if not reasoning_piece:
            return

        if not self.llm_response_accum.endswith("\n\n[Reasoning]:\n") and "[Reasoning]:" not in self.llm_response_accum:
            self._append_log_text("\n\n[Reasoning]:\n")
        self._append_log_text(reasoning_piece)

    def _append_response_piece(self, content_piece: str, *, after_reasoning: bool = False) -> None:
        if not content_piece:
            return

        if after_reasoning or "[Reasoning]:" in self.llm_response_accum:
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
        state = self._anthropic_tool_blocks.setdefault(
            block_index,
            {
                "name": tool_name if isinstance(tool_name, str) and tool_name else f"tool_{block_index}",
                "header_written": False,
            },
        )
        if isinstance(tool_name, str) and tool_name:
            state["name"] = tool_name

        if not state["header_written"]:
            self._append_log_text(f"\n\n[Tool Use: {state['name']}]\n")
            state["header_written"] = True

        if isinstance(payload, str):
            self._append_log_text(payload)
        elif payload not in (None, {}):
            self._append_log_text(json.dumps(payload, ensure_ascii=False, separators=(",", ":")))

    def _process_decoded_parts(self, parts):
        for part in parts:
            try:
                # Find the 'data: ' line in the chunk (it might be prefixed by 'event: ')
                decoded_chunk = None
                for line in part.split("\n"):
                    line = line.strip()
                    if line.startswith("data: "):
                        decoded_chunk = line[len("data: "):].strip()
                        break
                
                # Fallback for non-SSE JSON (e.g. error responses)
                if not decoded_chunk and part.strip().startswith("{"):
                    decoded_chunk = part.strip()

                if not decoded_chunk:
                    continue

                chunk_json = json.loads(decoded_chunk)
                if not isinstance(chunk_json, dict):
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
                    new_usage = get_token_usage(usage_payload)
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
                    self._append_log_text(decoded_chunk)
                    self._write_log_once()
            except Exception as ex:
                logger.error(f"ChatLogging: error processing chunk part: {decoded_chunk}: {ex}", exc_info=True)

    async def _run(self):
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        while True:
            chunk = await self.queue.get()

            try:
                if chunk is _STREAM_END:
                    break

                # Decompress chunk if it's gzipped (detect by magic number 0x1f 0x8b)
                if isinstance(chunk, bytes) and len(chunk) > 2 and chunk[0] == 0x1f and chunk[1] == 0x8b:
                    try:
                        with gzip.GzipFile(fileobj=io.BytesIO(chunk)) as f:
                            chunk = f.read()
                    except Exception as e:
                        logger.debug(f"Failed to decompress response chunk: {e}")

                if not self.is_real_streaming:
                    text = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
                    buffer += text
                else:
                    text = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
                    buffer += text
                    parts = buffer.split("\n\n")
                    # Keep the last part in buffer if incomplete
                    buffer = parts.pop() if not buffer.endswith("\n\n") else ""
                    self._process_decoded_parts(parts)
            except Exception as ex:
                logger.error(f"ChatLogging: error processing chunk: {chunk}: {ex}", exc_info=True)

        if buffer:
            parts = [buffer]
            if self.is_real_streaming and buffer.endswith("\n\n"):
                parts = buffer.split("\n\n")
            try:
                self._process_decoded_parts(parts)
            except Exception:
                logger.exception("ChatLogging: error processing trailing stream chunk parts")

        # Offload the blocking record-to-DB + write-log-file path to a thread so
        # the event loop is never held on SQLite or disk I/O during stream teardown.
        try:
            await asyncio.to_thread(self._write_log_once)
        except Exception:
            logger.exception("ChatLogging: error writing final stream observability record")

def _init_tokens_and_response():
    return "", initialize_tokens_usage()

async def log_chat_completions(request: Request, call_next: Callable) -> Response:
    if state.tokens_usage_db is None:
        app_db = getattr(request.app.state, "tokens_usage_db", None)
        if isinstance(app_db, TokensUsageDB):
            state.tokens_usage_db = app_db

    if state.api_keys_db is None:
        app_api_keys_db = getattr(request.app.state, "api_keys_db", None)
        if isinstance(app_api_keys_db, ApiKeysDB):
            state.api_keys_db = app_api_keys_db

    if state.rate_limiter is None:
        app_rate_limiter = getattr(request.app.state, "rate_limiter", None)
        if app_rate_limiter is not None:
            state.rate_limiter = app_rate_limiter

    if state.usd_budget_ledger is None:
        app_usd_budget_ledger = getattr(request.app.state, "usd_budget_ledger", None)
        if isinstance(app_usd_budget_ledger, UsdBudgetLedger):
            state.usd_budget_ledger = app_usd_budget_ledger

    operation = _extract_operation_from_url(request.url.path)
    if operation is None:
        return await call_next(request)

    try:
        # Capture request body and headers
        # Use request.body() which might exhaust the stream
        req_body_bytes = await request.body()
        req_headers = dict(request.headers)
        req_body_str = _decode_body(req_body_bytes, req_headers)

        # CRITICAL: Re-inject the body into the request stream so that the endpoint can read it again
        async def receive():
            return {"type": "http.request", "body": req_body_bytes, "more_body": False}
        
        request.scope["receive"] = receive

    except Exception as e:
        logger.error(f"Error capturing request body in log_chat middleware: {e}", exc_info=True)
        # We don't want to break the request if logging fails, but we might have consumed the body
        try:
            response = await call_next(request)
        except Exception:
            _release_usd_budget_reservation_for_request(request)
            raise
        _release_usd_budget_reservation_for_request(request)
        return response

    requested_gateway_model = _extract_gateway_model_from_request_body(req_body_str)
    update_active_request(request, gateway_model=requested_gateway_model, operation=operation)
    request_started_at = time.monotonic()
    try:
        response = await call_next(request)
    except Exception:
        _release_usd_budget_reservation_for_request(request)
        raise

    if response.status_code >= 400:
        _release_usd_budget_reservation_for_request(request)
        return response
    
    # CRITICAL FALLBACK: If body parsing failed (common with compression), use the model set by the controller in request.state
    if not requested_gateway_model:
        requested_gateway_model = getattr(request.state, "llmgateway_gateway_model", None)

    try:
        # If response is streaming, wrap its iterator to enqueue chunks for background processing
        content_type = response.headers.get("content-type", "").lower()
        is_real_streaming = "text/event-stream" in content_type
        is_streaming_response = isinstance(response, StreamingResponse) or "StreamingResponse" in type(response).__name__
        
        if is_streaming_response:
            original_iterator = response.body_iterator
            chunk_processor: ChunkProcessor | None = None

            async def enqueueing_generator():
                nonlocal chunk_processor
                try:
                    async for chunk in original_iterator:
                        if chunk_processor is None:
                            chunk_processor = ChunkProcessor(
                                req_headers,
                                req_body_str,
                                is_real_streaming,
                                provider_name=getattr(request.state, "llmgateway_provider", None),
                                provider_model=getattr(request.state, "llmgateway_provider_model", None),
                                gateway_model=requested_gateway_model,
                                operation=operation,
                                api_key_id=getattr(request.state, "api_key_id", None),
                                usd_budget_reserved=getattr(request.state, "usd_budget_reserved", False),
                                key_tpm_limit=getattr(getattr(request.state, "api_key_record", None), "tpm", None),
                            )
                            chunk_processor.request = request
                            chunk_processor.start()

                        chunk_processor.enqueue_chunk(chunk)
                        yield chunk
                finally:
                    if chunk_processor is not None:
                        chunk_processor.tokens_usage["duration_ms"] = int(
                            (time.monotonic() - request_started_at) * 1000
                        )
                        await chunk_processor.finish()
                        # Bounded wait: observability must not stall the event loop if the
                        # worker blocks on DB/disk I/O. If the task does not finish in time,
                        # we log and let it continue in the background — the stream completion
                        # path must not hang.
                        completed = await chunk_processor.wait(
                            CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS
                        )
                        if completed:
                            task_exception = chunk_processor.task_exception()
                            if task_exception is not None:
                                logger.error(
                                    "ChunkProcessor task finished with exception.",
                                    exc_info=(
                                        type(task_exception),
                                        task_exception,
                                        task_exception.__traceback__,
                                    ),
                                )
                                _release_usd_budget_reservation_for_request(request)
                            else:
                                _mark_usd_budget_reservation_finalized(request)
                        if not completed:
                            logger.warning(
                                "ChunkProcessor did not finish within %.1fs; "
                                "leaving it running to avoid blocking the event loop.",
                                CHUNK_PROCESSOR_JOIN_TIMEOUT_SECONDS,
                            )
                    else:
                        _release_usd_budget_reservation_for_request(request)

            response.body_iterator = enqueueing_generator()
        else:
            # For non-streaming responses, attempt to read full body content
            llm_response_accum, tokens_usage = _init_tokens_and_response()
            if hasattr(response, "body") and response.body:
                try:
                    response_data = json.loads(response.body.decode("utf-8"))
                    if isinstance(response_data, dict):
                        llm_response_accum = _extract_response_text_for_log(response_data)
                        tokens_usage = get_token_usage(response_data)
                    tokens_usage = enrich_tokens_usage(tokens_usage, request, requested_gateway_model, operation)
                except Exception as ex:
                    logger.error(f"ChatLogging: error processing non-streaming body: {ex}", exc_info=True)
            tokens_usage = enrich_tokens_usage(tokens_usage, request, requested_gateway_model, operation)
            tokens_usage = _merge_request_usage_tracker(tokens_usage, request)
            if getattr(request.state, "usd_budget_reserved", False):
                tokens_usage["_usd_budget_reserved"] = True
            key_tpm_rec = getattr(request.state, "api_key_record", None)
            if key_tpm_rec is not None and getattr(key_tpm_rec, "tpm", None):
                tokens_usage["_key_tpm_limit"] = key_tpm_rec.tpm
            tokens_usage["duration_ms"] = int((time.monotonic() - request_started_at) * 1000)
            _record_upstream_tokens_for_request(request, tokens_usage)
            _log_rtk_compression_stats(request)
            # Write log file immediately for non-streaming responses
            # Run observability tasks in a thread to avoid blocking the event loop
            await anyio.to_thread.run_sync(
                record_chat_observability,
                req_headers,
                req_body_str,
                llm_response_accum,
                tokens_usage
            )
            _mark_usd_budget_reservation_finalized(request)

    except Exception as e:
        _release_usd_budget_reservation_for_request(request)
        logger.error(f"Error in log_chat_completions middleware: {e}", exc_info=True)

    return response

def get_token_usage(chunk_data):
    """
    Extracts token usage information from the chunk data.
    """
    return extract_tokens_usage(chunk_data)
