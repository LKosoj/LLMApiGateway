import logging
import json
import copy
import asyncio
import codecs
import functools
import random
import re
import tiktoken
import gzip
import io
import time
from uuid import uuid4
from fastapi import APIRouter, Request, HTTPException
from fastapi.responses import StreamingResponse

# Relative imports from the new structure
from ...config.loader import (
    ANTHROPIC_API_VERSION,
    ConfigLoader,
    resolve_provider_api_keys,
    resolve_provider_config_api_key,
    resolve_provider_api_key_value,
)
from ...config.settings import settings
from ...db.model_rotation_db import ModelRotationDB
from ...db.fallback_events_db import FallbackEventsDB
from ...services.access_control import enforce_virtual_key_access
from ...services.active_requests import update_active_request
from ...services.error_classifier import (
    _build_fallback_error_message,
    _is_context_overflow_error,
    _log_failed_attempt_warning,
    _normalize_provider_attempt_payload,
    classify_error,
)
from ...services.model_policy import resolve_model_name
from ...services.request_handler import (
    MAX_RETRY_AFTER_SECONDS,
    SECURITY_HEADER_NAMES,
    make_llm_request,
    normalize_retry_settings,
)
from ...services.payload_transform import apply_payload_transforms
from ...services.provider_auth import resolve_provider_auth_material
from ...services.upstream_routing_state import (
    UpstreamKeyCandidate,
    UpstreamRoutingState,
    fingerprint_api_key,
    upstream_limits_for_model,
)
from ...utils.api_keys import split_api_keys
from ...utils.log_redaction import redact_payload_for_log
from ...utils.text_sanitize import sanitize_payload
from ...utils.usage_tracking import extract_request_x_title, extract_tokens_usage
from .chat_sanitizers import (
    expects_json_object_response as _expects_json_object_response,
    sanitize_json_object_response_content as _sanitize_json_object_response_content,
    sanitize_openai_response_content_think_tags as _sanitize_openai_response_content_think_tags,
    strip_think_blocks as _strip_think_blocks,
)

# Bound by the FastAPI lifespan via ``set_model_rotation_db``. Defined at
# module import only as a declaration — no ``ModelRotationDB()`` is created
# here. This avoids the side effect of opening (or creating) the rotation
# SQLite file the moment anything imports chat.py, which previously leaked
# into test runs and scripts that didn't need rotation at all.
model_rotation_db: ModelRotationDB | None = None
TEMPORARY_MODEL_FAILURE_MARKERS = (
    "currently overloaded",
    "engine is overloaded",
    "overloaded",
    "rate_limit",
    "rate limit",
    "too many requests",
    "try again later",
    "temporarily unavailable",
    "temporary unavailable",
    "service unavailable",
)


def _provider_key_routing_strategy(provider_config: object, pool_config: object | None) -> str:
    pool_strategy = getattr(pool_config, "strategy", None)
    if pool_strategy:
        return str(pool_strategy)
    routing_config = getattr(provider_config, "routing", None)
    return str(getattr(routing_config, "strategy", "round-robin"))


def _provider_key_routing_affinity_enabled(provider_config: object, pool_config: object | None) -> bool:
    pool_affinity = getattr(pool_config, "session_affinity", None)
    if pool_affinity is not None:
        return bool(pool_affinity)
    routing_config = getattr(provider_config, "routing", None)
    return bool(getattr(routing_config, "session_affinity", False))


def _provider_key_routing_affinity_header(provider_config: object, pool_config: object | None) -> str:
    pool_header = getattr(pool_config, "session_affinity_header", None)
    if pool_header:
        return str(pool_header)
    routing_config = getattr(provider_config, "routing", None)
    return str(getattr(routing_config, "session_affinity_header", "X-Session-Id"))


def _provider_key_routing_affinity_ttl(provider_config: object, pool_config: object | None) -> int:
    pool_ttl = getattr(pool_config, "session_affinity_ttl_seconds", None)
    if pool_ttl is not None:
        return int(pool_ttl)
    routing_config = getattr(provider_config, "routing", None)
    return int(getattr(routing_config, "session_affinity_ttl_seconds", 3600))


def _upstream_key_candidates_for_provider(
    provider_name: str,
    provider_config: object,
    pool_name: str | None,
) -> tuple[list[UpstreamKeyCandidate], object | None, str]:
    if not pool_name:
        if not getattr(provider_config, "apikey", None) and getattr(provider_config, "upstream_key_pools", None):
            raise ValueError("fallback rule must specify upstream_key_pool for this pool-only provider.")
        api_keys = resolve_provider_api_keys(getattr(provider_config, "apikey", None))
        return (
            [UpstreamKeyCandidate(api_key=api_key, order=index) for index, api_key in enumerate(api_keys)],
            None,
            "default",
        )

    pools = getattr(provider_config, "upstream_key_pools", None)
    pool_config = pools.get(pool_name) if isinstance(pools, dict) else None
    if pool_config is None:
        raise ValueError(f"upstream_key_pool '{pool_name}' is not configured for provider.")

    candidates: list[UpstreamKeyCandidate] = []
    for key_index, key_spec in enumerate(pool_config.keys):
        if not getattr(key_spec, "enabled", True):
            continue
        resolved_value = resolve_provider_api_key_value(key_spec.apikey)
        for split_index, api_key in enumerate(split_api_keys(resolved_value)):
            candidate_id = key_spec.id
            if candidate_id and "," in str(resolved_value):
                candidate_id = f"{candidate_id}:{split_index}"
            candidates.append(
                UpstreamKeyCandidate(
                    api_key=api_key,
                    order=len(candidates),
                    priority=int(getattr(key_spec, "priority", 0)),
                    candidate_id=candidate_id or f"{pool_name}:{key_index}:{split_index}",
                )
            )

    if not candidates:
        raise ValueError(f"upstream_key_pool '{pool_name}' for provider has no enabled keys.")
    return candidates, pool_config, pool_name


def _auth_scope_for_request(request: Request) -> str:
    api_key_id = getattr(request.state, "api_key_id", None)
    if api_key_id is not None:
        return f"user:{api_key_id}"
    role = getattr(request.state, "api_key_role", "master")
    if role != "master":
        return f"role:{role}"
    token = _extract_gateway_auth_token_from_request(request) or settings.gateway_api_key
    return f"master:{fingerprint_api_key(token)}"


def _affinity_scope_for_request(request: Request) -> str:
    return _auth_scope_for_request(request)


def _rotation_scope_for_request(request: Request) -> str:
    return _auth_scope_for_request(request)


TEMPORARY_MODEL_FAILURE_STATUS_RE = re.compile(
    r"\b(?:downstream\s+error|http|status(?:\s+code)?|error)\s*(429|5[0-9]{2})\b",
    re.IGNORECASE,
)
ANTHROPIC_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")


def set_model_rotation_db(db: ModelRotationDB | None) -> None:
    """Bind (or unbind) the ModelRotationDB used by the chat router."""
    global model_rotation_db
    model_rotation_db = db


def _require_model_rotation_db(request: Request | None = None) -> ModelRotationDB:
    app_state = getattr(getattr(request, "app", None), "state", None)
    db = getattr(app_state, "model_rotation_db", None) if app_state is not None else None
    if db is None:
        db = model_rotation_db
    if db is None:
        raise RuntimeError(
            "chat.model_rotation_db is not bound. "
            "Call set_model_rotation_db() from the application lifespan or test setup before requesting rotation."
        )
    return db


def get_token_usage(chunk_data: dict) -> dict:
    """
    Extracts token usage information from the chunk data.
    Returns a dict with prompt_tokens, completion_tokens, total_tokens, etc.
    """
    return extract_tokens_usage(chunk_data)


def _is_temporary_model_failure(error_detail: object) -> bool:
    if not error_detail:
        return False

    status_code = getattr(error_detail, "status_code", None)
    try:
        status_code_int = int(status_code)
    except (TypeError, ValueError):
        status_code_int = None
    if status_code_int == 429 or (status_code_int is not None and 500 <= status_code_int <= 599):
        return True

    lower_error_text = str(error_detail).lower()
    if (
        "readtimeout" in lower_error_text
        or "read timed out" in lower_error_text
        or "connecttimeout" in lower_error_text
        or "connect timed out" in lower_error_text
        or "connecterror" in lower_error_text
        or "connection refused" in lower_error_text
    ):
        return True
    if '"code":"429"' in lower_error_text or TEMPORARY_MODEL_FAILURE_STATUS_RE.search(lower_error_text):
        return True
    return any(marker in lower_error_text for marker in TEMPORARY_MODEL_FAILURE_MARKERS)


router = APIRouter()
anthropic_router = APIRouter()
responses_router = APIRouter()
ANTHROPIC_API_KEY_HEADER_NAME = "X-Api-Key"
# Anthropic requires max_tokens; default chosen when neither client nor rule set one.
ANTHROPIC_DEFAULT_MAX_TOKENS = 32768
TOKEN_COUNT_ENCODING_FALLBACKS = ("o200k_base", "cl100k_base")
JSON_STREAM_OPENING_PREFIXES = (
    "```json\r\n",
    "```json\n",
    "```json",
    "json\r\n",
    "json\n",
    "json ",
    "json\t",
    "json:",
)
JSON_STREAM_CLOSING_FENCE_RE = re.compile(r"(?:\r?\n)?```[\t ]*$")
JSON_STREAM_SUFFIX_BUFFER_SIZE = 5


def _extract_authorization_token(auth_header: str) -> str:
    parts = auth_header.split()
    if len(parts) == 2 and parts[0].lower() == "bearer" and parts[1]:
        return parts[1]

    return auth_header


def _extract_gateway_auth_token_from_request(request: Request) -> str:
    auth_header = request.headers.get("Authorization", "")
    if auth_header:
        return _extract_authorization_token(auth_header)

    return request.headers.get(ANTHROPIC_API_KEY_HEADER_NAME, "")


def _gateway_internal_api_key_for_request(request: Request) -> str:
    record = getattr(request.state, "api_key_record", None)
    if record is not None and getattr(record, "api_key", None):
        return record.api_key

    auth_token = _extract_gateway_auth_token_from_request(request)
    if auth_token:
        return auth_token

    return settings.gateway_api_key


def _read_json_request_body(request_body_bytes: bytes, endpoint_name: str, request: Request = None) -> dict:
    if not request_body_bytes:
        return {}

    # Handle GZip compression in request body
    if request and "gzip" in request.headers.get("content-encoding", "").lower():
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(request_body_bytes)) as f:
                request_body_bytes = f.read()
        except Exception as e:
            logging.warning(f"Failed to decompress GZip request body for {endpoint_name}: {e}")

    try:
        request_body_json = json.loads(request_body_bytes.decode("utf-8"))
        if not isinstance(request_body_json, dict):
            raise ValueError("Request body must be a JSON object")
        return sanitize_payload(request_body_json)
    except Exception as e:
        logging.error(f"Error reading request body for {endpoint_name}: {str(e)}", exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error reading request body: {str(e)}")


def _buffer_json_stream_suffix(content: str, state: dict) -> str:
    if not content:
        return ""

    combined = state["suffix_buffer"] + content
    if len(combined) <= JSON_STREAM_SUFFIX_BUFFER_SIZE:
        state["suffix_buffer"] = combined
        return ""

    split_at = len(combined) - JSON_STREAM_SUFFIX_BUFFER_SIZE
    state["suffix_buffer"] = combined[split_at:]
    return combined[:split_at]


def _flush_json_stream_suffix(state: dict) -> str:
    if not state["strip_closing_fence"]:
        buffered = state["suffix_buffer"]
        state["suffix_buffer"] = ""
        return buffered

    buffered = state["suffix_buffer"]
    state["suffix_buffer"] = ""
    if not buffered:
        return ""

    closing_match = JSON_STREAM_CLOSING_FENCE_RE.search(buffered)
    if closing_match and closing_match.end() == len(buffered):
        return buffered[:closing_match.start()]
    return buffered


def _strip_stream_think_blocks(content: str, state: dict) -> str:
    if not content:
        return ""

    think_open_tag = "<think>"
    think_close_tag = "</think>"
    max_partial_len = max(len(think_open_tag), len(think_close_tag)) - 1

    combined = state["think_buffer"] + content
    state["think_buffer"] = ""
    lower_combined = combined.lower()
    result_parts: list[str] = []
    cursor = 0

    while cursor < len(combined):
        if state["inside_think_block"]:
            close_idx = lower_combined.find(think_close_tag, cursor)
            if close_idx == -1:
                state["think_buffer"] = combined[max(cursor, len(combined) - max_partial_len):]
                return "".join(result_parts)

            cursor = close_idx + len(think_close_tag)
            state["inside_think_block"] = False
            continue

        open_idx = lower_combined.find(think_open_tag, cursor)
        if open_idx == -1:
            safe_end = max(cursor, len(combined) - max_partial_len)
            result_parts.append(combined[cursor:safe_end])
            state["think_buffer"] = combined[safe_end:]
            return "".join(result_parts)

        result_parts.append(combined[cursor:open_idx])
        cursor = open_idx + len(think_open_tag)
        state["inside_think_block"] = True

    return "".join(result_parts)


def _flush_stream_think_buffer(state: dict) -> str:
    buffered = state["think_buffer"]
    state["think_buffer"] = ""

    if state["inside_think_block"]:
        state["inside_think_block"] = False
        return ""

    if buffered and "<think>".startswith(buffered.lower()):
        return ""

    return buffered


def _sanitize_json_object_stream_content_fragment(content: str, state: dict) -> str:
    if not state["prefix_resolved"]:
        state["prefix_buffer"] += content

        for prefix in JSON_STREAM_OPENING_PREFIXES:
            if state["prefix_buffer"].startswith(prefix):
                state["prefix_resolved"] = True
                state["strip_closing_fence"] = prefix.startswith("```")
                remainder = state["prefix_buffer"][len(prefix):]
                state["prefix_buffer"] = ""
                if not state["strip_closing_fence"]:
                    return remainder
                return _buffer_json_stream_suffix(remainder, state)

        if any(prefix.startswith(state["prefix_buffer"]) for prefix in JSON_STREAM_OPENING_PREFIXES):
            return ""

        state["prefix_resolved"] = True
        content = state["prefix_buffer"]
        state["prefix_buffer"] = ""
        return content

    if not state["strip_closing_fence"]:
        return content

    return _buffer_json_stream_suffix(content, state)


def _sanitize_json_object_stream_delta(content: str, state: dict) -> str:
    content = _strip_stream_think_blocks(content, state)
    if not content:
        return ""

    return _sanitize_json_object_stream_content_fragment(content, state)


_STREAM_TEMPLATE_FIELDS = ("id", "object", "created", "model", "system_fingerprint")


def _extract_stream_template(chunk_json: dict) -> dict:
    """Extract only the scalar metadata fields needed for synthetic chunks."""
    return {f: chunk_json[f] for f in _STREAM_TEMPLATE_FIELDS if f in chunk_json}


def _build_openai_stream_delta_chunk(template_chunk: dict, choice: dict, choice_index: int, content: str) -> dict:
    synthetic_chunk: dict = {
        "choices": [
            {
                "index": choice.get("index", choice_index),
                "delta": {"content": content},
            }
        ]
    }
    synthetic_chunk.update(_extract_stream_template(template_chunk))
    return synthetic_chunk


def _new_stream_think_state() -> dict:
    return {
        "inside_think_block": False,
        "think_buffer": "",
    }


def _sanitize_openai_stream_think_tags(response: StreamingResponse, requested_model: str) -> StreamingResponse:
    async def sanitized_stream_generator():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        choice_states: dict[int, dict] = {}
        last_template_chunk: dict | None = None

        def get_choice_state(choice_index: int) -> dict:
            state = choice_states.get(choice_index)
            if state is None:
                state = _new_stream_think_state()
                choice_states[choice_index] = state
            return state

        def serialize_event(payload: dict) -> bytes:
            return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

        async for chunk in response.body_iterator:
            text_chunk = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
            buffer += text_chunk
            events = buffer.split("\n\n")
            buffer = events.pop() if not buffer.endswith("\n\n") else ""

            for event in events:
                stripped_event = event.strip()
                if not stripped_event:
                    continue

                if stripped_event == "data: [DONE]":
                    for choice_index, state in choice_states.items():
                        pending_content = _flush_stream_think_buffer(state)
                        if pending_content and last_template_chunk is not None:
                            template_choice = {"index": choice_index}
                            yield serialize_event(
                                _build_openai_stream_delta_chunk(
                                    last_template_chunk,
                                    template_choice,
                                    choice_index,
                                    pending_content,
                                )
                            )
                    yield b"data: [DONE]\n\n"
                    continue

                if not stripped_event.startswith("data: "):
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                data = stripped_event[len("data: "):]
                try:
                    chunk_json = sanitize_payload(json.loads(data))
                except Exception:
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                if not isinstance(chunk_json, dict):
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                last_template_chunk = _extract_stream_template(chunk_json)
                choices = chunk_json.get("choices")
                if not isinstance(choices, list):
                    yield serialize_event(chunk_json)
                    continue

                sanitized_choices: list[dict] = []
                for choice_index, choice in enumerate(choices):
                    if not isinstance(choice, dict):
                        sanitized_choices.append(choice)
                        continue

                    state = get_choice_state(choice_index)
                    finish_reason = choice.get("finish_reason")
                    if finish_reason is not None:
                        pending_content = _flush_stream_think_buffer(state)
                        if pending_content:
                            yield serialize_event(
                                _build_openai_stream_delta_chunk(chunk_json, choice, choice_index, pending_content)
                            )
                        sanitized_choices.append(choice)
                        continue

                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        sanitized_choices.append(choice)
                        continue

                    content = delta.get("content")
                    if not isinstance(content, str):
                        sanitized_choices.append(choice)
                        continue

                    sanitized_content = _strip_stream_think_blocks(content, state)
                    if sanitized_content:
                        delta["content"] = sanitized_content
                        sanitized_choices.append(choice)
                        continue

                    delta.pop("content", None)
                    if delta:
                        sanitized_choices.append(choice)

                if not sanitized_choices and not chunk_json.get("usage"):
                    continue

                chunk_json["choices"] = sanitized_choices
                yield serialize_event(chunk_json)

        if buffer.strip():
            yield buffer.encode("utf-8")

    logging.info("Enabled streaming <think> tag stripping for model '%s'.", requested_model)
    return StreamingResponse(
        sanitized_stream_generator(),
        status_code=response.status_code,
        media_type=response.media_type,
        headers=dict(response.headers),
    )


def _sanitize_anthropic_response_content_think_tags(response_data: dict, requested_model: str) -> None:
    """Strip literal ``<think>...</think>`` blocks from a native Anthropic reply.

    Native Anthropic responses carry text in ``content`` blocks (``{"type": "text",
    "text": ...}``), not in the OpenAI ``choices[].message.content`` shape, so the
    OpenAI sanitizer above does not reach them.
    """
    content = response_data.get("content")
    if not isinstance(content, list):
        return

    stripped_any = False
    for block in content:
        if not isinstance(block, dict) or block.get("type") != "text":
            continue
        text = block.get("text")
        if not isinstance(text, str):
            continue
        sanitized_text = _strip_think_blocks(text)
        if sanitized_text != text:
            block["text"] = sanitized_text
            stripped_any = True

    if stripped_any:
        logging.info(
            "Stripped <think> tags from non-stream Anthropic response content for model '%s'.",
            requested_model,
        )


def _extract_sse_data_payload(event: str) -> str | None:
    data_lines = [
        line[len("data:"):].lstrip()
        for line in event.split("\n")
        if line.startswith("data:")
    ]
    if not data_lines:
        return None
    return "\n".join(data_lines)


def _sanitize_anthropic_stream_think_tags(response: StreamingResponse, requested_model: str) -> StreamingResponse:
    """Strip ``<think>`` blocks from a native Anthropic SSE stream.

    Text arrives via ``content_block_delta`` events (``delta.type == "text_delta"``).
    State is kept per content-block index so tags split across deltas are handled,
    and any buffered partial is flushed right before that block's
    ``content_block_stop``. All other events pass through verbatim.
    """
    async def sanitized_stream_generator():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        block_states: dict[int, dict] = {}

        def get_state(block_index: int) -> dict:
            state = block_states.get(block_index)
            if state is None:
                state = _new_stream_think_state()
                block_states[block_index] = state
            return state

        async for chunk in response.body_iterator:
            text_chunk = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
            buffer += text_chunk
            events = buffer.split("\n\n")
            buffer = events.pop() if not buffer.endswith("\n\n") else ""

            for event in events:
                if not event.strip():
                    continue

                data_payload = _extract_sse_data_payload(event)
                if data_payload is None:
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                try:
                    payload = json.loads(data_payload)
                except Exception:
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                if not isinstance(payload, dict):
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                event_type = payload.get("type")

                if event_type == "content_block_delta":
                    delta = payload.get("delta")
                    if (
                        isinstance(delta, dict)
                        and delta.get("type") == "text_delta"
                        and isinstance(delta.get("text"), str)
                    ):
                        block_index = payload.get("index", 0)
                        state = get_state(block_index)
                        sanitized_text = _strip_stream_think_blocks(delta["text"], state)
                        if not sanitized_text:
                            continue
                        delta["text"] = sanitized_text
                        yield _format_anthropic_sse_event("content_block_delta", payload)
                        continue
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                if event_type == "content_block_stop":
                    block_index = payload.get("index", 0)
                    state = block_states.get(block_index)
                    if state is not None:
                        pending_text = _flush_stream_think_buffer(state)
                        if pending_text:
                            yield _format_anthropic_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": block_index,
                                    "delta": {"type": "text_delta", "text": pending_text},
                                },
                            )
                    yield f"{event}\n\n".encode("utf-8")
                    continue

                yield f"{event}\n\n".encode("utf-8")

        if buffer.strip():
            yield buffer.encode("utf-8")

    logging.info("Enabled streaming <think> tag stripping for Anthropic model '%s'.", requested_model)
    return StreamingResponse(
        sanitized_stream_generator(),
        status_code=response.status_code,
        media_type=response.media_type,
        headers=dict(response.headers),
    )


def _sanitize_openai_json_object_stream(response: StreamingResponse, requested_model: str) -> StreamingResponse:
    async def sanitized_stream_generator():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        choice_states: dict[int, dict] = {}
        last_template_chunk: dict | None = None

        def get_choice_state(choice_index: int) -> dict:
            state = choice_states.get(choice_index)
            if state is None:
                state = {
                    "prefix_resolved": False,
                    "prefix_buffer": "",
                    "strip_closing_fence": False,
                    "suffix_buffer": "",
                    **_new_stream_think_state(),
                }
                choice_states[choice_index] = state
            return state

        def serialize_event(payload: dict) -> bytes:
            return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

        def flush_choice_buffers() -> list[bytes]:
            if last_template_chunk is None:
                return []

            output_chunks: list[bytes] = []
            for choice_index, state in choice_states.items():
                pending_content = _flush_stream_think_buffer(state)
                if pending_content:
                    sanitized_pending_content = _sanitize_json_object_stream_content_fragment(pending_content, state)
                    if sanitized_pending_content:
                        template_choice = {"index": choice_index}
                        output_chunks.append(
                            serialize_event(
                                _build_openai_stream_delta_chunk(
                                    last_template_chunk,
                                    template_choice,
                                    choice_index,
                                    sanitized_pending_content,
                                )
                            )
                        )
                flushed_suffix = _flush_json_stream_suffix(state)
                if flushed_suffix:
                    template_choice = {"index": choice_index}
                    output_chunks.append(
                        serialize_event(
                            _build_openai_stream_delta_chunk(
                                last_template_chunk,
                                template_choice,
                                choice_index,
                                flushed_suffix,
                            )
                        )
                    )
            return output_chunks

        async def emit_sanitized_event(event: str):
            nonlocal last_template_chunk
            stripped_event = event.strip()
            if not stripped_event:
                return

            if stripped_event == "data: [DONE]":
                for output_chunk in flush_choice_buffers():
                    yield output_chunk
                yield b"data: [DONE]\n\n"
                return

            if not stripped_event.startswith("data: "):
                yield f"{event}\n\n".encode("utf-8")
                return

            data = stripped_event[len("data: "):]
            try:
                chunk_json = sanitize_payload(json.loads(data))
            except Exception:
                yield f"{event}\n\n".encode("utf-8")
                return

            if not isinstance(chunk_json, dict):
                yield f"{event}\n\n".encode("utf-8")
                return

            last_template_chunk = _extract_stream_template(chunk_json)
            choices = chunk_json.get("choices")
            if not isinstance(choices, list):
                yield serialize_event(chunk_json)
                return

            sanitized_choices: list[dict] = []
            for choice_index, choice in enumerate(choices):
                if not isinstance(choice, dict):
                    sanitized_choices.append(choice)
                    continue

                state = get_choice_state(choice_index)
                finish_reason = choice.get("finish_reason")
                if finish_reason is not None:
                    pending_content = _flush_stream_think_buffer(state)
                    if pending_content:
                        sanitized_pending_content = _sanitize_json_object_stream_content_fragment(pending_content, state)
                        if sanitized_pending_content:
                            yield serialize_event(
                                _build_openai_stream_delta_chunk(chunk_json, choice, choice_index, sanitized_pending_content)
                            )
                    flushed_suffix = _flush_json_stream_suffix(state)
                    if flushed_suffix:
                        yield serialize_event(
                            _build_openai_stream_delta_chunk(chunk_json, choice, choice_index, flushed_suffix)
                        )
                    sanitized_choices.append(choice)
                    continue

                delta = choice.get("delta")
                if not isinstance(delta, dict):
                    sanitized_choices.append(choice)
                    continue

                content = delta.get("content")
                if not isinstance(content, str):
                    sanitized_choices.append(choice)
                    continue

                sanitized_content = _sanitize_json_object_stream_delta(content, state)
                if sanitized_content:
                    delta["content"] = sanitized_content
                    sanitized_choices.append(choice)
                    continue

                delta.pop("content", None)
                if delta:
                    sanitized_choices.append(choice)

            if not sanitized_choices and not chunk_json.get("usage"):
                return

            chunk_json["choices"] = sanitized_choices
            yield serialize_event(chunk_json)

        async for chunk in response.body_iterator:
            text_chunk = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
            buffer += text_chunk
            events = buffer.split("\n\n")
            buffer = events.pop() if not buffer.endswith("\n\n") else ""

            for event in events:
                async for sanitized_event in emit_sanitized_event(event):
                    yield sanitized_event

        if buffer.strip():
            async for sanitized_event in emit_sanitized_event(buffer):
                yield sanitized_event

        for output_chunk in flush_choice_buffers():
            yield output_chunk

    logging.info("Enabled streaming JSON wrapper sanitization for model '%s'.", requested_model)
    return StreamingResponse(
        sanitized_stream_generator(),
        status_code=response.status_code,
        media_type=response.media_type,
        headers=dict(response.headers),
    )


def _finalize_chat_success_response(
    response_data: object,
    requested_model: str,
    request_body_json: dict,
    *,
    strip_think_tags: bool = False,
    is_anthropic_raw: bool = False,
) -> object:
    # Native Anthropic replies bypass the OpenAI round-trip and therefore the
    # OpenAI-shaped sanitizers below. Handle the flag in their own shape; the
    # JSON-object sanitization stays OpenAI-only and is intentionally untouched.
    if is_anthropic_raw:
        if strip_think_tags:
            if isinstance(response_data, dict):
                _sanitize_anthropic_response_content_think_tags(response_data, requested_model)
            elif isinstance(response_data, StreamingResponse):
                return _sanitize_anthropic_stream_think_tags(response_data, requested_model)
        return response_data

    expects_json_object_response = _expects_json_object_response(request_body_json)

    if isinstance(response_data, dict):
        if expects_json_object_response:
            _sanitize_json_object_response_content(response_data, requested_model)
        elif strip_think_tags:
            _sanitize_openai_response_content_think_tags(response_data, requested_model)
        return response_data

    if isinstance(response_data, StreamingResponse):
        if expects_json_object_response:
            return _sanitize_openai_json_object_stream(response_data, requested_model)
        if strip_think_tags:
            return _sanitize_openai_stream_think_tags(response_data, requested_model)
        return response_data

    return response_data


def _extract_anthropic_text(content: object, detail_prefix: str = "Anthropic content") -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        # If it's not a list or string, try to stringify it as a last resort
        return str(content) if content is not None else ""

    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue
        
        block_type = block.get("type")
        
        # Extract text from various known block types
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "thinking":
            text_parts.append(block.get("thinking", ""))
        elif block_type == "redacted_thinking":
            text_parts.append(block.get("data", ""))
        elif block_type == "tool_result":
            # Nested content in tool results
            inner_content = block.get("content")
            if inner_content:
                text_parts.append(_extract_anthropic_text(inner_content, "Nested tool_result"))
        
        # Silently skip image, document, and other non-textual blocks
        # to maintain maximum compatibility with complex payloads.

    return "".join(text_parts)


def _anthropic_image_source_to_openai_part(source: object) -> dict:
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="Anthropic image source must be an object")

    source_type = source.get("type")
    if source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise HTTPException(status_code=400, detail="Anthropic image URL source requires non-empty 'url'")
        return {"type": "image_url", "image_url": {"url": url}}

    if source_type == "base64":
        data = source.get("data")
        media_type = source.get("media_type")
        if not isinstance(data, str) or not data:
            raise HTTPException(status_code=400, detail="Anthropic base64 image source requires non-empty 'data'")
        if not isinstance(media_type, str) or not media_type:
            raise HTTPException(status_code=400, detail="Anthropic base64 image source requires non-empty 'media_type'")
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic image source type: {source_type}")


def _anthropic_document_source_to_openai_parts(source: object, block: dict) -> list[dict]:
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="Anthropic document source must be an object")

    source_type = source.get("type")
    translated_parts: list[dict] = []

    title = block.get("title")
    context = block.get("context")
    prefix_parts: list[str] = []
    if isinstance(title, str) and title:
        prefix_parts.append(f"Document title: {title}")
    if isinstance(context, str) and context:
        prefix_parts.append(f"Document context: {context}")
    if prefix_parts:
        translated_parts.append({"type": "text", "text": "\n".join(prefix_parts) + "\n"})

    if source_type == "text":
        data = source.get("data")
        if not isinstance(data, str):
            raise HTTPException(status_code=400, detail="Anthropic plain text document source requires string 'data'")
        translated_parts.append({"type": "text", "text": data})
        return translated_parts

    if source_type == "content":
        nested_content = source.get("content")
        if not isinstance(nested_content, list):
            raise HTTPException(status_code=400, detail="Anthropic content document source requires list 'content'")
        translated_parts.extend(_anthropic_content_blocks_to_openai_parts(nested_content))
        return translated_parts

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic document source type: {source_type}")


def _anthropic_content_blocks_to_openai_parts(blocks: object) -> list[dict]:
    if not isinstance(blocks, list):
        raise HTTPException(status_code=400, detail="Anthropic content must be a list of content blocks")

    translated_parts: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")

        block_type = block.get("type")
        if block_type == "text":
            translated_parts.append({"type": "text", "text": block.get("text", "")})
            continue

        if block_type == "image":
            translated_parts.append(_anthropic_image_source_to_openai_part(block.get("source")))
            continue

        if block_type == "document":
            translated_parts.extend(_anthropic_document_source_to_openai_parts(block.get("source"), block))
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported Anthropic user content block type: {block_type}")

    return translated_parts


def _collapse_openai_parts_to_content(parts: list[dict]) -> object:
    if not parts:
        return ""

    if all(part.get("type") == "text" for part in parts):
        return "".join(part.get("text", "") for part in parts)

    return parts


def _anthropic_tool_result_to_openai_content(content: object) -> object:
    if isinstance(content, str):
        return content

    translated_parts = _anthropic_content_blocks_to_openai_parts(content)
    return _collapse_openai_parts_to_content(translated_parts)


def _anthropic_tools_to_openai(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="Anthropic tools must be a list")

    translated_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Anthropic tools must be objects")
        name = tool.get("name")
        input_schema = tool.get("input_schema")
        if not name or not isinstance(input_schema, dict):
            raise HTTPException(status_code=400, detail="Anthropic tools require 'name' and object 'input_schema'")

        translated_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description"),
                    "parameters": input_schema,
                },
            }
        )

    return translated_tools


def _anthropic_tool_choice_to_openai(tool_choice: object) -> object:
    if isinstance(tool_choice, str):
        tool_choice = {"type": tool_choice}

    if not isinstance(tool_choice, dict):
        raise HTTPException(status_code=400, detail="Anthropic tool_choice must be an object or string")

    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        tool_name = tool_choice.get("name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="Anthropic tool_choice type 'tool' requires 'name'")
        return {"type": "function", "function": {"name": tool_name}}

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic tool_choice type: {choice_type}")


def _anthropic_message_to_openai_messages(message: dict) -> list[dict]:
    role = message.get("role")
    content = message.get("content", "")

    if role == "assistant":
        assistant_message: dict[str, object] = {"role": "assistant"}
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "thinking":
                    text_parts.append(block.get("thinking", ""))
                elif block_type == "redacted_thinking":
                    text_parts.append(block.get("data", ""))
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    )
                else:
                    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic assistant content block type: {block_type}")
        else:
            raise HTTPException(status_code=400, detail="Anthropic assistant content must be a string or a list of content blocks")

        assistant_message["content"] = "".join(text_parts) if text_parts else None
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls

        return [assistant_message]

    if role != "user":
        raise HTTPException(status_code=400, detail=f"Unsupported Anthropic role: {role}")

    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="Anthropic user content must be a string or a list of content blocks")

    translated_messages: list[dict] = []
    user_content_parts: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")
        block_type = block.get("type")
        if block_type == "tool_result":
            if user_content_parts:
                translated_messages.append({"role": "user", "content": _collapse_openai_parts_to_content(user_content_parts)})
                user_content_parts = []
            translated_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _anthropic_tool_result_to_openai_content(block.get("content", "")),
                }
            )
        else:
            user_content_parts.extend(_anthropic_content_blocks_to_openai_parts([block]))

    if user_content_parts:
        translated_messages.append({"role": "user", "content": _collapse_openai_parts_to_content(user_content_parts)})

    if not translated_messages:
        translated_messages.append({"role": "user", "content": ""})

    return translated_messages


def _anthropic_request_to_openai_payload(anthropic_payload: dict) -> tuple[dict, str]:
    requested_model = anthropic_payload.get("model")
    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    anthropic_messages = anthropic_payload.get("messages")
    if not isinstance(anthropic_messages, list) or not anthropic_messages:
        raise HTTPException(status_code=400, detail="Anthropic request must contain a non-empty 'messages' list")

    openai_messages: list[dict] = []

    system_content = anthropic_payload.get("system")
    if system_content is not None:
        openai_messages.append(
            {
                "role": "system",
                "content": _extract_anthropic_text(system_content),
            }
        )

    for message in anthropic_messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Anthropic messages must be objects")
        openai_messages.extend(_anthropic_message_to_openai_messages(message))

    openai_payload = {
        "model": requested_model,
        "messages": openai_messages,
        "stream": anthropic_payload.get("stream", False),
    }
    
    if openai_payload["stream"]:
        openai_payload["stream_options"] = {"include_usage": True}

    # Passthrough only safe, standard OpenAI fields
    openai_safe_fields = {
        "max_tokens",
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "service_tier",
        "user",
    }
    for field in openai_safe_fields:
        if field in anthropic_payload:
            openai_payload[field] = copy.deepcopy(anthropic_payload[field])

    if "stop_sequences" in anthropic_payload:
        openai_payload["stop"] = anthropic_payload["stop_sequences"]

    if "tools" in anthropic_payload:
        openai_payload["tools"] = _anthropic_tools_to_openai(anthropic_payload["tools"])

    if "tool_choice" in anthropic_payload:
        openai_payload["tool_choice"] = _anthropic_tool_choice_to_openai(anthropic_payload["tool_choice"])

    output_config = anthropic_payload.get("output_config")
    if isinstance(output_config, dict):
        response_format = output_config.get("format")
        if isinstance(response_format, dict):
            openai_payload["response_format"] = copy.deepcopy(response_format)

    return openai_payload, requested_model


def _openai_content_to_plain_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _openai_user_content_to_anthropic_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return [{"type": "text", "text": ""}]
    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="OpenAI user content must be string or a list of content blocks")

    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(status_code=400, detail="OpenAI content blocks must be objects")
        part_type = part.get("type")
        if part_type == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part_type == "image_url":
            image_url_field = part.get("image_url")
            url = image_url_field.get("url") if isinstance(image_url_field, dict) else image_url_field
            if not isinstance(url, str) or not url:
                raise HTTPException(status_code=400, detail="OpenAI image_url block requires a 'url' string")
            if url.startswith("data:"):
                try:
                    header, b64data = url.split(",", 1)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Malformed data URL for image")
                media_type = header[len("data:"):].split(";", 1)[0] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64data},
                })
            else:
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported OpenAI user content block type: {part_type}")

    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def _openai_tool_content_to_anthropic(content: object) -> object:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        normalized: list[dict] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                normalized.append({"type": "text", "text": block.get("text", "")})
            else:
                normalized.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})
        return normalized
    return json.dumps(content, ensure_ascii=False)


def _openai_tool_name_to_anthropic(name: str, tool_name_map: dict[str, str]) -> str:
    mapped_name = tool_name_map.get(name)
    if mapped_name is not None:
        return mapped_name

    used_names = set(tool_name_map.values())
    candidate = name
    if not ANTHROPIC_TOOL_NAME_RE.fullmatch(candidate):
        candidate = re.sub(r"[^A-Za-z0-9_-]", "_", candidate)
        if not candidate or not candidate[0].isalpha():
            candidate = f"tool_{candidate}"

    base_candidate = candidate or "tool"
    suffix = 2
    while candidate in used_names or not ANTHROPIC_TOOL_NAME_RE.fullmatch(candidate):
        candidate = f"{base_candidate}_{suffix}"
        suffix += 1

    tool_name_map[name] = candidate
    return candidate


def _anthropic_tool_name_to_openai(name: object, tool_name_reverse_map: dict[str, str]) -> str:
    if not isinstance(name, str):
        return ""
    return tool_name_reverse_map.get(name, name)


def _openai_tools_to_anthropic(tools: object, tool_name_map: dict[str, str] | None = None) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="'tools' must be a list")
    if tool_name_map is None:
        tool_name_map = {}
    translated: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Tool entries must be objects")
        function_payload = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function_payload, dict):
            raise HTTPException(status_code=400, detail="Tool entry must contain a 'function' object")
        name = function_payload.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=400, detail="Each tool requires a 'name'")
        entry: dict = {
            "name": _openai_tool_name_to_anthropic(name, tool_name_map),
            "input_schema": function_payload.get("parameters") or {"type": "object", "properties": {}},
        }
        description = function_payload.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        translated.append(entry)
    return translated


def _openai_tool_choice_to_anthropic(
    choice: object,
    tool_name_map: dict[str, str] | None = None,
) -> object | None:
    if tool_name_map is None:
        tool_name_map = {}
    if isinstance(choice, str):
        if choice == "auto":
            return {"type": "auto"}
        if choice == "required":
            return {"type": "any"}
        if choice == "none":
            return {"type": "none"}
        return None
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            fn = choice.get("function") or {}
            tool_name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(tool_name, str) and tool_name:
                return {"type": "tool", "name": _openai_tool_name_to_anthropic(tool_name, tool_name_map)}
        return None
    return None


def _openai_request_to_anthropic_payload(
    openai_payload: dict,
    tool_name_map: dict[str, str] | None = None,
) -> dict:
    """Convert an OpenAI chat-completions payload into an Anthropic messages payload.

    This is the symmetric counterpart of ``_anthropic_request_to_openai_payload``
    and is used when the selected provider speaks the native Anthropic dialect.
    """
    messages = openai_payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="'messages' must be a list")
    if tool_name_map is None:
        tool_name_map = {}

    system_texts: list[str] = []
    anthropic_messages: list[dict] = []
    pending_user_blocks: list[dict] = []

    def _flush_pending_user() -> None:
        if pending_user_blocks:
            anthropic_messages.append({"role": "user", "content": list(pending_user_blocks)})
            pending_user_blocks.clear()

    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Each message must be an object")
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _openai_content_to_plain_text(content)
            if text:
                system_texts.append(text)
            continue

        if role == "user":
            _flush_pending_user()
            anthropic_messages.append({
                "role": "user",
                "content": _openai_user_content_to_anthropic_blocks(content),
            })
            continue

        if role == "assistant":
            _flush_pending_user()
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            blocks.append({"type": "text", "text": text})
            tool_calls = message.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function") or {}
                    raw_args = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
                    try:
                        parsed = json.loads(raw_args) if raw_args else {}
                    except Exception:
                        parsed = {"raw_arguments": raw_args}
                    if not isinstance(parsed, dict):
                        parsed = {"value": parsed}
                    tool_name = fn.get("name") if isinstance(fn, dict) else None
                    if isinstance(tool_name, str) and tool_name:
                        tool_name = _openai_tool_name_to_anthropic(tool_name, tool_name_map)
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": tool_name,
                        "input": parsed,
                    })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            anthropic_messages.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            pending_user_blocks.append({
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": _openai_tool_content_to_anthropic(content),
            })
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported OpenAI message role: {role}")

    _flush_pending_user()

    anthropic_payload: dict = {
        "model": openai_payload.get("model"),
        "messages": anthropic_messages,
    }
    if system_texts:
        anthropic_payload["system"] = "\n\n".join(system_texts)

    max_tokens_value = openai_payload.get("max_tokens")
    if isinstance(max_tokens_value, bool) or not isinstance(max_tokens_value, (int, float)) or int(max_tokens_value) <= 0:
        anthropic_payload["max_tokens"] = ANTHROPIC_DEFAULT_MAX_TOKENS
    else:
        anthropic_payload["max_tokens"] = int(max_tokens_value)

    if openai_payload.get("stream"):
        anthropic_payload["stream"] = True

    for field_name in ("temperature", "top_p"):
        if field_name in openai_payload:
            anthropic_payload[field_name] = openai_payload[field_name]

    if "stop" in openai_payload:
        stop_value = openai_payload["stop"]
        if isinstance(stop_value, str):
            anthropic_payload["stop_sequences"] = [stop_value]
        elif isinstance(stop_value, list):
            anthropic_payload["stop_sequences"] = [s for s in stop_value if isinstance(s, str)]

    if "tools" in openai_payload:
        anthropic_payload["tools"] = _openai_tools_to_anthropic(
            openai_payload["tools"],
            tool_name_map,
        )

    if "tool_choice" in openai_payload:
        translated_choice = _openai_tool_choice_to_anthropic(
            openai_payload["tool_choice"],
            tool_name_map,
        )
        if translated_choice is not None:
            anthropic_payload["tool_choice"] = translated_choice

    user_identifier = openai_payload.get("user")
    if isinstance(user_identifier, str) and user_identifier:
        anthropic_payload["metadata"] = {"user_id": user_identifier}

    return anthropic_payload


_ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _coerce_anthropic_usage_token_count(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _anthropic_usage_to_openai_usage(usage_src: object) -> dict:
    if not isinstance(usage_src, dict):
        usage_src = {}

    input_tokens = _coerce_anthropic_usage_token_count(usage_src.get("input_tokens"))
    cache_creation_input_tokens = _coerce_anthropic_usage_token_count(
        usage_src.get("cache_creation_input_tokens")
    )
    cache_read_input_tokens = _coerce_anthropic_usage_token_count(
        usage_src.get("cache_read_input_tokens")
    )
    output_tokens = _coerce_anthropic_usage_token_count(usage_src.get("output_tokens"))

    prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    }
    if cache_creation_input_tokens or cache_read_input_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cache_read_input_tokens}

    return usage


def _anthropic_response_to_openai(
    response_data: dict,
    requested_model: str,
    tool_name_reverse_map: dict[str, str] | None = None,
) -> dict:
    """Convert a non-streaming Anthropic /v1/messages response to OpenAI chat.completion shape."""
    if not isinstance(response_data, dict):
        raise HTTPException(status_code=502, detail="Anthropic provider returned a non-object response")
    if tool_name_reverse_map is None:
        tool_name_reverse_map = {}

    content_blocks = response_data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif block_type == "tool_use":
            try:
                arguments_json = json.dumps(
                    block.get("input") or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except Exception:
                arguments_json = "{}"
            tool_calls.append({
                "id": block.get("id") or f"call_{uuid4().hex[:16]}",
                "type": "function",
                "function": {
                    "name": _anthropic_tool_name_to_openai(block.get("name"), tool_name_reverse_map),
                    "arguments": arguments_json,
                },
            })

    message: dict = {"role": "assistant"}
    message["content"] = "".join(text_parts) if text_parts else None
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH.get(
        response_data.get("stop_reason"),
        "stop",
    )

    return {
        "id": response_data.get("id") or f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_data.get("model") or requested_model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason},
        ],
        "usage": _anthropic_usage_to_openai_usage(response_data.get("usage")),
    }


def _anthropic_stream_to_openai(
    request: Request,
    response: StreamingResponse,
    requested_model: str,
    tool_name_reverse_map: dict[str, str] | None = None,
) -> StreamingResponse:
    """Convert an Anthropic /v1/messages SSE stream into OpenAI chat.completion.chunk SSE.

    Mirrors ``_openai_stream_to_anthropic`` but in the opposite direction:
    consumed by an OpenAI-format client when the fallback chain lands on a
    provider declared with ``type: "anthropic"``.
    """

    if tool_name_reverse_map is None:
        tool_name_reverse_map = {}

    def _wrap(payload: dict) -> bytes:
        return (
            b"data: "
            + json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            + b"\n\n"
        )

    async def openai_stream_generator():
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        created_ts = int(time.time())
        stream_id = f"chatcmpl-{uuid4().hex}"
        upstream_model = requested_model

        # Mapping Anthropic content_block index → OpenAI tool_calls[].index + metadata.
        tool_index_by_block: dict[int, int] = {}
        tool_state_by_block: dict[int, dict] = {}
        next_tool_index = 0

        input_tokens = 0
        cache_creation_input_tokens = 0
        cache_read_input_tokens = 0
        output_tokens = 0
        stop_reason: str | None = None
        role_emitted = False

        def _base_chunk() -> dict:
            return {
                "id": stream_id,
                "object": "chat.completion.chunk",
                "created": created_ts,
                "model": upstream_model,
                "choices": [{"index": 0, "delta": {}, "finish_reason": None}],
            }

        def _emit_role_once() -> list[bytes]:
            nonlocal role_emitted
            if role_emitted:
                return []
            role_emitted = True
            chunk = _base_chunk()
            chunk["choices"][0]["delta"] = {"role": "assistant", "content": ""}
            return [_wrap(chunk)]

        def _process_anthropic_event(event_name: str, event_data: dict) -> list[bytes]:
            nonlocal upstream_model, input_tokens, cache_creation_input_tokens
            nonlocal cache_read_input_tokens, output_tokens, stop_reason
            nonlocal next_tool_index
            out: list[bytes] = []

            if event_name == "message_start":
                message = event_data.get("message") or {}
                model_name = message.get("model")
                if isinstance(model_name, str) and model_name:
                    upstream_model = model_name
                usage = message.get("usage") or {}
                if isinstance(usage, dict):
                    input_tokens = _coerce_anthropic_usage_token_count(usage.get("input_tokens"))
                    cache_creation_input_tokens = _coerce_anthropic_usage_token_count(
                        usage.get("cache_creation_input_tokens")
                    )
                    cache_read_input_tokens = _coerce_anthropic_usage_token_count(
                        usage.get("cache_read_input_tokens")
                    )
                out.extend(_emit_role_once())
                return out

            if event_name == "content_block_start":
                block = event_data.get("content_block") or {}
                block_index = event_data.get("index")
                if not isinstance(block_index, int):
                    return out
                block_type = block.get("type")
                if block_type == "tool_use":
                    openai_tool_idx = next_tool_index
                    next_tool_index += 1
                    tool_index_by_block[block_index] = openai_tool_idx
                    tool_state_by_block[block_index] = {
                        "id": block.get("id") or f"call_{uuid4().hex[:16]}",
                        "name": _anthropic_tool_name_to_openai(block.get("name"), tool_name_reverse_map),
                    }
                    out.extend(_emit_role_once())
                    chunk = _base_chunk()
                    chunk["choices"][0]["delta"] = {
                        "tool_calls": [
                            {
                                "index": openai_tool_idx,
                                "id": tool_state_by_block[block_index]["id"],
                                "type": "function",
                                "function": {
                                    "name": tool_state_by_block[block_index]["name"],
                                    "arguments": "",
                                },
                            }
                        ]
                    }
                    out.append(_wrap(chunk))
                # text / thinking blocks: nothing to emit on start — OpenAI deltas
                # flow naturally as ``content`` / ``reasoning_content`` increments.
                return out

            if event_name == "content_block_delta":
                delta = event_data.get("delta") or {}
                delta_type = delta.get("type")
                if delta_type == "text_delta":
                    text = delta.get("text")
                    if isinstance(text, str) and text:
                        out.extend(_emit_role_once())
                        chunk = _base_chunk()
                        chunk["choices"][0]["delta"] = {"content": text}
                        out.append(_wrap(chunk))
                elif delta_type == "thinking_delta":
                    text = delta.get("thinking")
                    if isinstance(text, str) and text:
                        out.extend(_emit_role_once())
                        chunk = _base_chunk()
                        chunk["choices"][0]["delta"] = {"reasoning_content": text}
                        out.append(_wrap(chunk))
                elif delta_type == "input_json_delta":
                    block_index = event_data.get("index")
                    if isinstance(block_index, int) and block_index in tool_index_by_block:
                        partial = delta.get("partial_json")
                        if isinstance(partial, str) and partial:
                            chunk = _base_chunk()
                            chunk["choices"][0]["delta"] = {
                                "tool_calls": [
                                    {
                                        "index": tool_index_by_block[block_index],
                                        "function": {"arguments": partial},
                                    }
                                ]
                            }
                            out.append(_wrap(chunk))
                return out

            if event_name == "message_delta":
                delta = event_data.get("delta") or {}
                reason = delta.get("stop_reason")
                if isinstance(reason, str):
                    stop_reason = reason
                usage = event_data.get("usage") or {}
                if isinstance(usage, dict):
                    if "output_tokens" in usage:
                        output_tokens = _coerce_anthropic_usage_token_count(usage.get("output_tokens"))
                    if "input_tokens" in usage:
                        input_tokens = _coerce_anthropic_usage_token_count(usage.get("input_tokens"))
                    if "cache_creation_input_tokens" in usage:
                        cache_creation_input_tokens = _coerce_anthropic_usage_token_count(
                            usage.get("cache_creation_input_tokens")
                        )
                    if "cache_read_input_tokens" in usage:
                        cache_read_input_tokens = _coerce_anthropic_usage_token_count(
                            usage.get("cache_read_input_tokens")
                        )
                return out

            return out  # content_block_stop / message_stop / ping / error

        async for chunk in response.body_iterator:
            if isinstance(chunk, (bytes, bytearray)):
                text = decoder.decode(bytes(chunk))
            else:
                text = str(chunk)

            buffer += text
            # SSE events are separated by a blank line (\n\n). Extract complete events.
            while "\n\n" in buffer:
                raw_event, buffer = buffer.split("\n\n", 1)
                event_name = None
                data_lines: list[str] = []
                for line in raw_event.split("\n"):
                    if line.startswith(":"):
                        continue  # SSE comment / keep-alive
                    if line.startswith("event:"):
                        event_name = line[len("event:"):].strip() or None
                    elif line.startswith("data:"):
                        data_lines.append(line[len("data:"):].lstrip())
                if not data_lines:
                    continue
                data_str = "\n".join(data_lines).strip()
                if not data_str or data_str == "[DONE]":
                    continue
                try:
                    event_data = sanitize_payload(json.loads(data_str))
                except Exception:
                    continue
                if event_name is None and isinstance(event_data, dict):
                    event_name = event_data.get("type")
                if not isinstance(event_name, str):
                    continue
                if not isinstance(event_data, dict):
                    continue
                if event_name == "error":
                    error_payload = event_data.get("error")
                    if not isinstance(error_payload, dict):
                        error_payload = {"message": str(error_payload or event_data)}
                    yield _wrap({"error": error_payload})
                    return
                for out_chunk in _process_anthropic_event(event_name, event_data):
                    yield out_chunk

        # Make sure clients at least see a role frame, even for empty streams.
        for out_chunk in _emit_role_once():
            yield out_chunk

        openai_finish = _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH.get(stop_reason, "stop")
        final_chunk = _base_chunk()
        final_chunk["choices"][0]["finish_reason"] = openai_finish
        final_chunk["usage"] = _anthropic_usage_to_openai_usage(
            {
                "input_tokens": input_tokens,
                "cache_creation_input_tokens": cache_creation_input_tokens,
                "cache_read_input_tokens": cache_read_input_tokens,
                "output_tokens": output_tokens,
            }
        )
        _update_request_usage_tracker(request, final_chunk["usage"])
        yield _wrap(final_chunk)
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        openai_stream_generator(),
        media_type="text/event-stream",
        headers={
            "Transfer-Encoding": "chunked",
            "X-Accel-Buffering": "no",
        },
    )


def _resolve_model_name_for_token_count(config_loader_instance: ConfigLoader, requested_model: str) -> str:
    try:
        model_resolution = resolve_model_name(
            requested_model,
            getattr(config_loader_instance, "model_rules", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    model_config = config_loader_instance.fallback_rules.get(model_resolution.effective_model)
    if not model_config:
        if model_resolution.changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model policy resolved '{requested_model}' to "
                    f"'{model_resolution.effective_model}', but no chat route is configured for it."
                ),
            )
        return requested_model

    fallback_models = model_config.get("fallback_models") or []
    if not fallback_models:
        return requested_model

    first_rule = fallback_models[0]
    if not isinstance(first_rule, dict):
        return requested_model

    provider_model = first_rule.get("model")
    if isinstance(provider_model, str) and provider_model:
        return provider_model

    return requested_model


@functools.lru_cache(maxsize=64)
def _resolve_tiktoken_encoding(model_name: str):
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        for encoding_name in TOKEN_COUNT_ENCODING_FALLBACKS:
            try:
                return tiktoken.get_encoding(encoding_name)
            except ValueError:
                continue

    raise HTTPException(status_code=500, detail="Token counting is not configured correctly")


def _build_token_count_payload(openai_payload: dict) -> dict:
    token_count_payload = {"messages": openai_payload.get("messages", [])}

    for field in ("cache_control", "output_config", "thinking", "tool_choice", "tools"):
        if field in openai_payload:
            token_count_payload[field] = openai_payload[field]

    return token_count_payload


def _estimate_input_tokens(openai_payload: dict, model_name: str) -> int:
    encoding = _resolve_tiktoken_encoding(model_name)
    token_count_payload = _build_token_count_payload(openai_payload)
    serialized_payload = json.dumps(
        token_count_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(encoding.encode(serialized_payload))


def _map_finish_reason_to_anthropic(finish_reason: str | None) -> str | None:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }
    return mapping.get(finish_reason, finish_reason)


def _openai_tool_calls_to_anthropic_blocks(tool_calls: object) -> list[dict]:
    if not isinstance(tool_calls, list):
        return []

    anthropic_blocks = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function", {})
        raw_arguments = function.get("arguments", "{}")
        try:
            parsed_arguments = (
                sanitize_payload(json.loads(raw_arguments))
                if raw_arguments
                else {}
            )
        except Exception:
            parsed_arguments = {"raw_arguments": raw_arguments}

        if not isinstance(parsed_arguments, dict):
            parsed_arguments = {"value": parsed_arguments}

        anthropic_blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": function.get("name"),
                "input": parsed_arguments,
            }
        )

    return anthropic_blocks


def _openai_response_to_anthropic(response_data: dict, requested_model: str) -> dict:
    choices = response_data.get("choices", [])
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {})
    usage = response_data.get("usage", {})
    
    content_blocks = []

    # Anthropic thinking blocks require provider signatures that the gateway
    # cannot synthesize from generic OpenAI-compatible reasoning fields.
    # Handle main text content
    text_content = message.get("content")
    if isinstance(text_content, str) and text_content:
        content_blocks.append({"type": "text", "text": text_content})
    elif isinstance(text_content, list):
        # Flatten OpenAI list content if present
        parts = []
        for block in text_content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            content_blocks.append({"type": "text", "text": "".join(parts)})

    # Handle tools
    tool_use_blocks = _openai_tool_calls_to_anthropic_blocks(message.get("tool_calls"))
    content_blocks.extend(tool_use_blocks)
    
    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    stop_reason = _map_finish_reason_to_anthropic(first_choice.get("finish_reason"))
    if tool_use_blocks and stop_reason is None:
        stop_reason = "tool_use"

    return {
        "id": response_data.get("id", "msg_llmgateway"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _format_anthropic_sse_event(event_name: str, payload: dict) -> bytes:
    event_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {event_json}\n\n".encode("utf-8")


def _is_openai_stream_error_chunk(chunk_json: dict) -> bool:
    return "error" in chunk_json and not chunk_json.get("choices")


def _update_request_usage_tracker(request: Request | None, usage_payload: object) -> None:
    if request is None or not isinstance(usage_payload, dict):
        return

    new_usage = get_token_usage({"usage": usage_payload})
    usage_tracker = getattr(request.state, "usage_tracker", None)
    if not isinstance(usage_tracker, dict):
        usage_tracker = {}
        request.state.usage_tracker = usage_tracker

    for key, value in new_usage.items():
        if isinstance(value, bool):
            if value:
                usage_tracker[key] = value
        elif isinstance(value, (int, float)):
            if value > 0:
                usage_tracker[key] = value
        elif isinstance(value, str) and value:
            usage_tracker[key] = value

    prompt_tokens = usage_tracker.get("prompt_tokens")
    completion_tokens = usage_tracker.get("completion_tokens")
    if isinstance(prompt_tokens, (int, float)) and isinstance(completion_tokens, (int, float)):
        if prompt_tokens > 0 and completion_tokens > 0 and not usage_tracker.get("total_tokens"):
            usage_tracker["total_tokens"] = prompt_tokens + completion_tokens


def _openai_stream_to_anthropic(request: Request, response: StreamingResponse, requested_model: str) -> StreamingResponse:
    async def anthropic_stream_generator():
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        stream_id = "msg_llmgateway_stream"
        finish_reason = None
        
        # Consistent usage dictionary for internal tracking
        usage = {"prompt_tokens": 0, "completion_tokens": 0}
        
        started = False
        text_block_index: int | None = None
        thinking_block_index: int | None = None
        next_content_index = 0
        tool_block_states: dict[int, dict] = {}
        failed = False

        def process_openai_chunk(chunk_json: dict) -> list[bytes]:
            nonlocal stream_id, finish_reason, usage, started, failed
            nonlocal text_block_index, thinking_block_index, next_content_index

            output_events: list[bytes] = []
            stream_id = chunk_json.get("id", stream_id)

            if _is_openai_stream_error_chunk(chunk_json):
                failed = True
                output_events.append(
                    _format_anthropic_sse_event(
                        "error",
                        {
                            "type": "error",
                            "error": {
                                "type": "api_error",
                                "message": "Upstream stream failed.",
                            },
                        },
                    )
                )
                return output_events

            chunk_usage = chunk_json.get("usage")
            if chunk_usage:
                _update_request_usage_tracker(request, chunk_usage)
                if "prompt_tokens" in chunk_usage:
                    usage["prompt_tokens"] = chunk_usage["prompt_tokens"]
                if "completion_tokens" in chunk_usage:
                    usage["completion_tokens"] = chunk_usage["completion_tokens"]

            if not started:
                started = True
                output_events.append(
                    _format_anthropic_sse_event(
                        "message_start",
                        {
                            "type": "message_start",
                            "message": {
                                "id": stream_id,
                                "type": "message",
                                "role": "assistant",
                                "model": requested_model,
                                "content": [],
                                "stop_reason": None,
                                "stop_sequence": None,
                                "usage": {
                                    "input_tokens": usage["prompt_tokens"],
                                    "output_tokens": 0,
                                },
                            },
                        },
                    )
                )

            choices = chunk_json.get("choices", [])
            first_choice = choices[0] if choices else {}
            delta = first_choice.get("delta", {})

            reasoning_delta = delta.get("reasoning_content") or delta.get("reasoning")
            if isinstance(reasoning_delta, str) and reasoning_delta:
                if thinking_block_index is None:
                    thinking_block_index = next_content_index
                    next_content_index += 1
                    output_events.append(
                        _format_anthropic_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": thinking_block_index,
                                "content_block": {"type": "thinking", "thinking": ""},
                            },
                        )
                    )
                output_events.append(
                    _format_anthropic_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": thinking_block_index,
                            "delta": {"type": "thinking_delta", "thinking": reasoning_delta},
                        },
                    )
                )

            content_delta = delta.get("content")
            if isinstance(content_delta, str) and content_delta:
                if text_block_index is None:
                    text_block_index = next_content_index
                    next_content_index += 1
                    output_events.append(
                        _format_anthropic_sse_event(
                            "content_block_start",
                            {
                                "type": "content_block_start",
                                "index": text_block_index,
                                "content_block": {"type": "text", "text": ""},
                            },
                        )
                    )
                output_events.append(
                    _format_anthropic_sse_event(
                        "content_block_delta",
                        {
                            "type": "content_block_delta",
                            "index": text_block_index,
                            "delta": {"type": "text_delta", "text": content_delta},
                        },
                    )
                )

            tool_call_deltas = delta.get("tool_calls", [])
            if isinstance(tool_call_deltas, list):
                for tool_call_delta in tool_call_deltas:
                    if not isinstance(tool_call_delta, dict):
                        continue
                    openai_index = tool_call_delta.get("index", 0)
                    state = tool_block_states.get(openai_index)
                    function_payload = tool_call_delta.get("function", {})

                    if state is None:
                        anthropic_index = next_content_index
                        next_content_index += 1
                        state = {
                            "anthropic_index": anthropic_index,
                            "id": tool_call_delta.get("id") or f"toolu_llmgateway_{openai_index}",
                            "name": function_payload.get("name") or f"tool_{openai_index}",
                        }
                        tool_block_states[openai_index] = state
                        output_events.append(
                            _format_anthropic_sse_event(
                                "content_block_start",
                                {
                                    "type": "content_block_start",
                                    "index": anthropic_index,
                                    "content_block": {
                                        "type": "tool_use",
                                        "id": state["id"],
                                        "name": state["name"],
                                        "input": {},
                                    },
                                },
                            )
                        )

                    arguments_delta = function_payload.get("arguments")
                    if isinstance(arguments_delta, str) and arguments_delta:
                        output_events.append(
                            _format_anthropic_sse_event(
                                "content_block_delta",
                                {
                                    "type": "content_block_delta",
                                    "index": state["anthropic_index"],
                                    "delta": {
                                        "type": "input_json_delta",
                                        "partial_json": arguments_delta,
                                    },
                                },
                            )
                        )

            if first_choice.get("finish_reason") is not None:
                finish_reason = _map_finish_reason_to_anthropic(first_choice.get("finish_reason"))

            return output_events

        async for chunk in response.body_iterator:
            if isinstance(chunk, bytes):
                text = decoder.decode(chunk)
            else:
                text = str(chunk)

            buffer += text
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                stripped_line = line.strip()
                if not stripped_line or not stripped_line.startswith("data: "):
                    continue

                data = stripped_line[len("data: "):]
                if data == "[DONE]":
                    continue

                try:
                    chunk_json = sanitize_payload(json.loads(data))
                except Exception:
                    continue
                for event in process_openai_chunk(chunk_json):
                    yield event
                if failed:
                    return

        # Process any remaining data left in the buffer after the stream ends.
        if buffer:
            stripped_line = buffer.strip()
            if stripped_line and stripped_line.startswith("data: "):
                data = stripped_line[len("data: "):]
                if data != "[DONE]":
                    try:
                        chunk_json = sanitize_payload(json.loads(data))
                        for event in process_openai_chunk(chunk_json):
                            yield event
                        if failed:
                            return
                    except Exception:
                        pass

        if not started:
            yield _format_anthropic_sse_event(
                "message_start",
                {
                    "type": "message_start",
                    "message": {
                        "id": stream_id,
                        "type": "message",
                        "role": "assistant",
                        "model": requested_model,
                        "content": [],
                        "stop_reason": None,
                        "stop_sequence": None,
                        "usage": usage,
                    },
                },
            )
            yield _format_anthropic_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": 0,
                    "content_block": {"type": "text", "text": ""},
                },
            )
            text_block_index = 0
            next_content_index = max(next_content_index, 1)

        open_content_block = (
            thinking_block_index is not None
            or text_block_index is not None
            or bool(tool_block_states)
        )
        if not open_content_block:
            text_block_index = next_content_index
            next_content_index += 1
            yield _format_anthropic_sse_event(
                "content_block_start",
                {
                    "type": "content_block_start",
                    "index": text_block_index,
                    "content_block": {"type": "text", "text": ""},
                },
            )

        if thinking_block_index is not None:
            yield _format_anthropic_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": thinking_block_index},
            )
        if text_block_index is not None:
            yield _format_anthropic_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": text_block_index},
            )
        for state in tool_block_states.values():
            yield _format_anthropic_sse_event(
                "content_block_stop",
                {"type": "content_block_stop", "index": state["anthropic_index"]},
            )
        yield _format_anthropic_sse_event(
            "message_delta",
            {
                "type": "message_delta",
                "delta": {
                    "stop_reason": finish_reason or "end_turn",
                    "stop_sequence": None,
                },
                "usage": {
                    "input_tokens": usage.get("prompt_tokens", 0),
                    "output_tokens": usage.get("completion_tokens", 0),
                },
            },
        )
        yield _format_anthropic_sse_event(
            "message_stop",
            {"type": "message_stop"},
        )

    return StreamingResponse(
        anthropic_stream_generator(),
        media_type="text/event-stream",
        headers={
            "Transfer-Encoding": "chunked",
            "X-Accel-Buffering": "no",
            "anthropic-version": "2023-06-01",
        },
    )


def _coerce_responses_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _responses_content_to_openai_content(content: object) -> object:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="Responses message content must be a string or a list")

    openai_parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(status_code=400, detail="Responses content parts must be objects")

        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            openai_parts.append({"type": "text", "text": _coerce_responses_text(part.get("text", ""))})
            continue

        if part_type == "input_image":
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url:
                openai_parts.append({"type": "image_url", "image_url": {"url": image_url}})
                continue
            if isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str) and url:
                    openai_parts.append({"type": "image_url", "image_url": {"url": url}})
                    continue
            raise HTTPException(status_code=400, detail="Responses input_image requires non-empty 'image_url'")

        raise HTTPException(status_code=400, detail=f"Unsupported Responses content part type: {part_type}")

    return _collapse_openai_parts_to_content(openai_parts)


def _responses_input_to_openai_messages(input_items: object) -> list[dict]:
    if isinstance(input_items, str):
        return [{"role": "user", "content": input_items}]

    if not isinstance(input_items, list):
        raise HTTPException(status_code=400, detail="Responses 'input' must be a string or a list")

    openai_messages: list[dict] = []
    for item in input_items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Responses input items must be objects")

        item_type = item.get("type") or ("message" if item.get("role") else None)
        if item_type == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise HTTPException(status_code=400, detail=f"Unsupported Responses message role: {role}")
            openai_messages.append(
                {
                    "role": role,
                    "content": _responses_content_to_openai_content(item.get("content", "")),
                }
            )
            continue

        if item_type == "function_call":
            tool_name = item.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                raise HTTPException(status_code=400, detail="Responses function_call requires non-empty 'name'")
            raw_arguments = item.get("arguments", "")
            if not isinstance(raw_arguments, str):
                raw_arguments = json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
            tool_call_id = item.get("call_id") or item.get("id") or f"call_{uuid4().hex}"
            openai_messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": raw_arguments,
                            },
                        }
                    ],
                }
            )
            continue

        if item_type == "function_call_output":
            tool_call_id = item.get("call_id") or item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise HTTPException(status_code=400, detail="Responses function_call_output requires non-empty 'call_id'")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            openai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": output,
                }
            )
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported Responses input item type: {item_type}")

    return openai_messages


def _responses_tools_to_openai(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="Responses tools must be a list")

    translated_tools: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Responses tools must be objects")

        tool_type = tool.get("type")
        if tool_type not in (None, "function"):
            raise HTTPException(status_code=400, detail=f"Unsupported Responses tool type: {tool_type}")

        function_payload = tool.get("function", {}) if isinstance(tool.get("function"), dict) else {}
        name = tool.get("name") or function_payload.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=400, detail="Responses tools require a non-empty function name")

        parameters = tool.get("parameters")
        if parameters is None:
            parameters = function_payload.get("parameters") or tool.get("parametersJsonSchema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}

        translated_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or function_payload.get("description"),
                    "parameters": parameters,
                },
            }
        )

    return translated_tools


def _responses_request_to_openai_payload(responses_payload: dict) -> tuple[dict, str]:
    requested_model = responses_payload.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    openai_messages: list[dict] = []

    instructions = responses_payload.get("instructions")
    instructions_text = _coerce_responses_text(instructions).strip()
    if instructions_text:
        openai_messages.append({"role": "system", "content": instructions_text})

    if "input" not in responses_payload:
        raise HTTPException(status_code=400, detail="Responses request must contain 'input'")

    openai_messages.extend(_responses_input_to_openai_messages(responses_payload.get("input")))

    openai_payload = {
        "model": requested_model,
        "messages": openai_messages,
        "stream": responses_payload.get("stream", False),
    }
    
    if openai_payload["stream"]:
        openai_payload["stream_options"] = {"include_usage": True}

    if "max_output_tokens" in responses_payload:
        openai_payload["max_tokens"] = responses_payload["max_output_tokens"]
    elif "max_tokens" in responses_payload:
        openai_payload["max_tokens"] = responses_payload["max_tokens"]

    for field in ("metadata", "parallel_tool_calls", "reasoning", "service_tier", "temperature", "top_p", "user"):
        if field in responses_payload:
            openai_payload[field] = copy.deepcopy(responses_payload[field])

    if "tools" in responses_payload:
        openai_payload["tools"] = _responses_tools_to_openai(responses_payload["tools"])

    if "tool_choice" in responses_payload:
        openai_payload["tool_choice"] = copy.deepcopy(responses_payload["tool_choice"])

    text_config = responses_payload.get("text")
    if isinstance(text_config, dict):
        text_format = text_config.get("format")
        if isinstance(text_format, dict) and text_format.get("type") not in (None, "text"):
            openai_payload["response_format"] = copy.deepcopy(text_format)

    if "response_format" in responses_payload and "response_format" not in openai_payload:
        openai_payload["response_format"] = copy.deepcopy(responses_payload["response_format"])

    return openai_payload, requested_model


def _build_openai_usage_to_responses_usage(usage: object) -> dict:
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_tokens_details, dict):
        prompt_tokens_details = {}

    completion_tokens_details = usage.get("completion_tokens_details")
    if not isinstance(completion_tokens_details, dict):
        completion_tokens_details = {}

    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {
            "cached_tokens": prompt_tokens_details.get("cached_tokens", 0),
        },
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {
            "reasoning_tokens": completion_tokens_details.get("reasoning_tokens", 0),
        },
        "total_tokens": usage.get("total_tokens", 0),
    }


def _openai_message_content_to_responses_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "output_text", "text": content, "annotations": []}]
    if content is None:
        return [{"type": "output_text", "text": "", "annotations": []}]
    if not isinstance(content, list):
        return [{"type": "output_text", "text": _coerce_responses_text(content), "annotations": []}]

    text_parts: list[str] = []
    media_parts: list[dict] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            logging.debug("Skipping unsupported OpenAI message content part in Responses conversion: %r", part)
            continue

        part_type = part.get("type")
        if part_type in {"text", "output_text"}:
            text_value = part.get("text")
            if isinstance(text_value, str):
                text_parts.append(text_value)
            elif text_value is not None:
                text_parts.append(_coerce_responses_text(text_value))
            continue

        if part_type in {"image_url", "output_image"}:
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                media_parts.append({"type": "output_image", "image_url": {"url": image_url}})
            else:
                logging.debug("Skipping OpenAI image content part without image URL in Responses conversion.")
            continue

        logging.debug("Skipping unsupported OpenAI message content part type in Responses conversion: %r", part_type)

    output_content: list[dict] = []
    if text_parts:
        output_content.append({"type": "output_text", "text": "".join(text_parts), "annotations": []})
    output_content.extend(media_parts)
    if not output_content:
        output_content.append({"type": "output_text", "text": "", "annotations": []})
    return output_content


def _openai_response_to_responses(response_data: dict, requested_model: str) -> dict:
    choices = response_data.get("choices", [])
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {})
    finish_reason = first_choice.get("finish_reason")
    usage = _build_openai_usage_to_responses_usage(response_data.get("usage"))

    output: list[dict] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {})
            call_id = tool_call.get("id") or f"call_{uuid4().hex}"
            output.append(
                {
                    "id": f"fc_{uuid4().hex}",
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name"),
                    "arguments": function.get("arguments", ""),
                    "status": "completed",
                }
            )
    else:
        # Handle reasoning
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            output.append(
                {
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reasoning,
                            "annotations": ["thought"],
                        }
                    ],
                }
            )

        # Handle main content
        output.append(
            {
                "id": f"msg_{uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": _openai_message_content_to_responses_content(message.get("content")),
            }
        )

    response_status = "completed" if finish_reason != "length" else "incomplete"
    responses_response = {
        "id": response_data.get("id", f"resp_{uuid4().hex}"),
        "object": "response",
        "created_at": response_data.get("created"),
        "status": response_status,
        "model": requested_model,
        "output": output,
        "usage": usage,
    }
    if finish_reason == "length":
        responses_response["incomplete_details"] = {"reason": "max_output_tokens"}
    return responses_response


def _format_responses_sse_event(payload: dict) -> bytes:
    event_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"data: {event_json}\n\n".encode("utf-8")


def _openai_stream_to_responses(
    response: StreamingResponse,
    requested_model: str,
    request: Request | None = None,
) -> StreamingResponse:
    async def responses_stream_generator():
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        response_id = f"resp_{uuid4().hex}"
        created_at = None
        usage = _build_openai_usage_to_responses_usage({})
        next_output_index = 0
        reasoning_output_index: int | None = None
        text_output_index: int | None = None
        reasoning_parts: list[str] = []
        text_parts: list[str] = []
        tool_output_indices: dict[int, int] = {}
        tool_item_ids: dict[int, str] = {}
        tool_call_ids: dict[int, str] = {}
        tool_call_names: dict[int, str] = {}
        tool_call_arguments: dict[int, list[str]] = {}
        failed = False

        def process_openai_chunk(chunk_json: dict) -> list[bytes]:
            nonlocal response_id, created_at, usage, failed
            nonlocal next_output_index, reasoning_output_index, text_output_index

            output_events: list[bytes] = []
            response_id = chunk_json.get("id", response_id)
            if created_at is None:
                created_at = chunk_json.get("created")

            if _is_openai_stream_error_chunk(chunk_json):
                failed = True
                output_events.append(
                    _format_responses_sse_event(
                        {
                            "type": "response.failed",
                            "response": {
                                "id": response_id,
                                "object": "response",
                                "created_at": created_at,
                                "status": "failed",
                                "model": requested_model,
                                "output": [],
                                "usage": usage,
                                "error": {
                                    "code": "upstream_stream_error",
                                    "message": "Upstream stream failed.",
                                },
                            },
                        }
                    )
                )
                return output_events

            choices = chunk_json.get("choices", [])
            first_choice = choices[0] if choices else {}
            delta = first_choice.get("delta", {})

            reasoning_text = delta.get("reasoning_content") or delta.get("reasoning")
            if reasoning_text:
                if reasoning_output_index is None:
                    reasoning_output_index = next_output_index
                    next_output_index += 1
                reasoning_parts.append(str(reasoning_text))
                output_events.append(
                    _format_responses_sse_event(
                        {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "output_index": reasoning_output_index,
                            "content_index": 0,
                            "delta": reasoning_text,
                            "annotations": ["thought"],
                        }
                    )
                )

            content_text = delta.get("content")
            if content_text:
                if text_output_index is None:
                    text_output_index = next_output_index
                    next_output_index += 1
                text_parts.append(str(content_text))
                output_events.append(
                    _format_responses_sse_event(
                        {
                            "type": "response.output_text.delta",
                            "response_id": response_id,
                            "output_index": text_output_index,
                            "content_index": 0,
                            "delta": content_text,
                        }
                    )
                )

            tool_call_deltas = delta.get("tool_calls", [])
            if isinstance(tool_call_deltas, list):
                for tool_call_delta in tool_call_deltas:
                    if not isinstance(tool_call_delta, dict):
                        continue
                    openai_index = tool_call_delta.get("index", 0)
                    output_index = tool_output_indices.get(openai_index)
                    if output_index is None:
                        output_index = next_output_index
                        next_output_index += 1
                        tool_output_indices[openai_index] = output_index
                    function_payload = tool_call_delta.get("function", {})
                    item_id = tool_item_ids.get(openai_index)
                    call_id = tool_call_ids.get(openai_index) or tool_call_delta.get("id") or f"call_{uuid4().hex}"
                    function_name = tool_call_names.get(openai_index) or function_payload.get("name") or f"tool_{openai_index}"

                    if item_id is None:
                        item_id = f"fc_{uuid4().hex}"
                        tool_item_ids[openai_index] = item_id
                        tool_call_ids[openai_index] = call_id
                        tool_call_names[openai_index] = function_name
                        output_events.append(
                            _format_responses_sse_event(
                                {
                                    "type": "response.output_item.added",
                                    "response_id": response_id,
                                    "output_index": output_index,
                                    "item": {
                                        "id": item_id,
                                        "type": "function_call",
                                        "call_id": call_id,
                                        "name": function_name,
                                        "arguments": "",
                                        "status": "in_progress",
                                    },
                                }
                            )
                        )

                    arguments_delta = function_payload.get("arguments")
                    if isinstance(arguments_delta, str) and arguments_delta:
                        tool_call_arguments.setdefault(openai_index, []).append(arguments_delta)
                        output_events.append(
                            _format_responses_sse_event(
                                {
                                    "type": "response.function_call_arguments.delta",
                                    "response_id": response_id,
                                    "item_id": item_id,
                                    "output_index": output_index,
                                    "delta": arguments_delta,
                                }
                            )
                        )

            chunk_usage = chunk_json.get("usage")
            if chunk_usage:
                _update_request_usage_tracker(request, chunk_usage)
                usage = _build_openai_usage_to_responses_usage(chunk_usage)

            return output_events

        async for chunk in response.body_iterator:
            text = decoder.decode(chunk) if isinstance(chunk, bytes) else str(chunk)
            buffer += text
            lines = buffer.split("\n")
            buffer = lines.pop()

            for line in lines:
                stripped_line = line.strip()
                if not stripped_line or not stripped_line.startswith("data: "):
                    continue

                data = stripped_line[len("data: "):]
                if data == "[DONE]":
                    continue

                try:
                    chunk_json = sanitize_payload(json.loads(data))
                except Exception:
                    continue
                for event in process_openai_chunk(chunk_json):
                    yield event
                if failed:
                    yield b"data: [DONE]\n\n"
                    return

        # Process any remaining data left in the buffer after the stream ends.
        if buffer:
            stripped_line = buffer.strip()
            if stripped_line and stripped_line.startswith("data: "):
                data = stripped_line[len("data: "):]
                if data != "[DONE]":
                    try:
                        chunk_json = sanitize_payload(json.loads(data))
                        for event in process_openai_chunk(chunk_json):
                            yield event
                        if failed:
                            yield b"data: [DONE]\n\n"
                            return
                    except Exception:
                        pass

        completed_items: list[tuple[int, dict]] = []
        if reasoning_output_index is not None and reasoning_parts:
            completed_items.append(
                (
                    reasoning_output_index,
                    {
                        "id": f"msg_{uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "".join(reasoning_parts),
                                "annotations": ["thought"],
                            }
                        ],
                    },
                )
            )
        if text_output_index is not None and text_parts:
            completed_items.append(
                (
                    text_output_index,
                    {
                        "id": f"msg_{uuid4().hex}",
                        "type": "message",
                        "role": "assistant",
                        "status": "completed",
                        "content": [
                            {
                                "type": "output_text",
                                "text": "".join(text_parts),
                                "annotations": [],
                            }
                        ],
                    },
                )
            )
        for openai_index, output_index in sorted(tool_output_indices.items(), key=lambda item: item[1]):
            item_id = tool_item_ids.get(openai_index)
            if item_id is None:
                continue
            completed_item = {
                "id": item_id,
                "type": "function_call",
                "call_id": tool_call_ids.get(openai_index) or f"call_{uuid4().hex}",
                "name": tool_call_names.get(openai_index) or f"tool_{openai_index}",
                "arguments": "".join(tool_call_arguments.get(openai_index, [])),
                "status": "completed",
            }
            completed_items.append((output_index, completed_item))

        completed_output: list[dict] = []
        for output_index, completed_item in sorted(completed_items, key=lambda item: item[0]):
            completed_output.append(completed_item)
            yield _format_responses_sse_event(
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": output_index,
                    "item": completed_item,
                }
            )

        completed_response = {
            "id": response_id,
            "object": "response",
            "created_at": created_at,
            "status": "completed",
            "model": requested_model,
            "output": completed_output,
            "usage": usage,
        }
        yield _format_responses_sse_event({"type": "response.completed", "response": completed_response})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        responses_stream_generator(),
        media_type="text/event-stream",
        headers={"Transfer-Encoding": "chunked", "X-Accel-Buffering": "no"},
    )


async def _attempt_model_fallback_rule(
    request: Request,
    http_client: object,
    providers_config: dict,
    requested_model: str,
    request_body_json: dict,
    model_fallback_rule: dict,
    is_streaming: bool,
    attempt_label: str = "fallback model",
    *,
    fallback_events_db: FallbackEventsDB | None = None,
    request_id: str | None = None,
    attempt_number_start: int = 1,
    total_attempts_budget: int | None = None,
    stop_after_context_overflow: bool = False,
) -> tuple[object | None, str | None, int]:
    """Attempt a single provider+model, optionally retrying.

    ``total_attempts_budget`` enforces a *chain-wide* cap on the number of
    upstream calls: every attempt (initial or retry, regular or sub-provider)
    decrements the budget. When the budget is exhausted, the function returns
    immediately with the last error so the caller does not advance to the next
    fallback model. ``None`` disables the cap (legacy behavior).
    """
    provider_name = model_fallback_rule.get("provider")
    provider_model = model_fallback_rule.get("model")
    retry_count, retry_delay = normalize_retry_settings(
        model_fallback_rule.get("retry_count"),
        model_fallback_rule.get("retry_delay"),
    )
    subproviders_ordering = model_fallback_rule.get("providers_order")

    # Use a per-provider proxy client when configured, otherwise the shared client.
    proxy_clients = getattr(request.app.state, "proxy_http_clients", {})
    if provider_name and provider_name in proxy_clients:
        http_client = proxy_clients[provider_name]

    logging.info(
        "Attempting %s '%s' for gateway model '%s' via provider '%s' and subproviders ordering: %s",
        attempt_label,
        provider_model,
        requested_model,
        provider_name,
        subproviders_ordering,
    )

    attempt_number = attempt_number_start
    x_title = extract_request_x_title(request)

    def _record_event(
        success: bool,
        error_detail_val: str | None,
        duration_ms: int,
        attempt_payload_for_error: dict | None = None,
        upstream_key_fingerprint: str | None = None,
    ):
        nonlocal attempt_number
        if fallback_events_db and request_id:
            try:
                fallback_events_db.insert_event(
                    request_id=request_id,
                    gateway_model=requested_model,
                    attempt_number=attempt_number,
                    provider=provider_name or "unknown",
                    model=provider_model or "unknown",
                    success=success,
                    error_type=None if success else classify_error(error_detail_val),
                    error_message=None if success else _build_fallback_error_message(
                        error_detail_val,
                        attempt_payload_for_error,
                        is_streaming=is_streaming,
                    ),
                    duration_ms=duration_ms,
                    operation=getattr(request.state, "llmgateway_operation", "chat"),
                    api_key_id=getattr(request.state, "api_key_id", None),
                    upstream_key_fingerprint=upstream_key_fingerprint,
                    x_title=x_title,
                )
            except Exception as exc:
                logging.debug("Failed to record fallback event: %s", exc)
        attempt_number += 1

    provider_config = providers_config.get(provider_name)
    if provider_config is None:
        logging.error(
            "Provider '%s' referenced by model '%s' is missing from providers configuration.",
            provider_name,
            requested_model,
        )
        _record_event(False, "Configured provider is unavailable for the requested model.", 0)
        return None, "Configured provider is unavailable for the requested model.", attempt_number

    provider_base_url = provider_config.baseUrl
    upstream_state = getattr(request.app.state, "upstream_routing_state", None)
    if isinstance(upstream_state, UpstreamRoutingState):
        upstream_key_pool_name = model_fallback_rule.get("upstream_key_pool")
        try:
            key_candidates, key_pool_config, key_pool_name = _upstream_key_candidates_for_provider(
                provider_name,
                provider_config,
                upstream_key_pool_name,
            )
        except ValueError as exc:
            error_detail = f"Invalid upstream key routing configuration for provider '{provider_name}': {exc}"
            logging.error(error_detail)
            _record_event(False, error_detail, 0)
            return None, error_detail, attempt_number

        strategy = _provider_key_routing_strategy(provider_config, key_pool_config)
        affinity_enabled = _provider_key_routing_affinity_enabled(provider_config, key_pool_config)
        affinity_header = _provider_key_routing_affinity_header(provider_config, key_pool_config)
        affinity_ttl_seconds = _provider_key_routing_affinity_ttl(provider_config, key_pool_config)
        session_id = request.headers.get(affinity_header) if affinity_enabled else None
        affinity_scope = _affinity_scope_for_request(request) if affinity_enabled else None
        selected_key = upstream_state.select_key_from_candidates(
            provider_name or "unknown",
            provider_model or "unknown",
            key_candidates,
            limits=upstream_limits_for_model(provider_config, provider_model or ""),
            strategy=strategy,
            session_id=session_id,
            affinity_scope=affinity_scope,
            session_affinity_ttl_seconds=affinity_ttl_seconds,
            pool_name=key_pool_name,
        )
        if not selected_key.available:
            error_detail = (
                f"No upstream key is currently available for provider '{provider_name}' "
                f"and model '{provider_model}': {selected_key.blocked_reason}."
            )
            logging.warning(error_detail)
            _record_event(False, error_detail, 0, upstream_key_fingerprint=selected_key.fingerprint)
            return None, error_detail, attempt_number
        provider_api_key = selected_key.api_key
        upstream_key_fingerprint = selected_key.fingerprint
    else:
        provider_api_key = resolve_provider_config_api_key(provider_config)
        upstream_key_fingerprint = fingerprint_api_key(provider_api_key)
    auth_material = await resolve_provider_auth_material(
        request,
        provider_name=provider_name or "unknown",
        provider_config=provider_config,
        api_key=provider_api_key,
    )
    if auth_material.upstream_key_fingerprint:
        upstream_key_fingerprint = auth_material.upstream_key_fingerprint
    is_anthropic_provider = getattr(provider_config, "type", "openai") == "anthropic"
    client_expects_anthropic = isinstance(
        getattr(request.state, "llmgateway_original_anthropic_payload", None),
        dict,
    )
    anthropic_tool_name_reverse_map: dict[str, str] = {}

    if is_anthropic_provider:
        headers = {
            "Content-Type": "application/json",
            "anthropic-version": ANTHROPIC_API_VERSION,
            **auth_material.headers,
        }
        target_url = f"{provider_base_url.rstrip('/')}/v1/messages"
        original_anthropic_payload = getattr(
            request.state, "llmgateway_original_anthropic_payload", None
        )
        if isinstance(original_anthropic_payload, dict):
            # Anthropic client → Anthropic provider: preserve every native field
            # (system, cache_control, thinking, ...) that would be lost if we
            # round-tripped through the OpenAI shape.
            provider_payload_template = copy.deepcopy(original_anthropic_payload)
            if "max_tokens" not in provider_payload_template:
                provider_payload_template["max_tokens"] = ANTHROPIC_DEFAULT_MAX_TOKENS
        else:
            openai_to_anthropic_tool_name_map: dict[str, str] = {}
            provider_payload_template = _openai_request_to_anthropic_payload(
                request_body_json,
                tool_name_map=openai_to_anthropic_tool_name_map,
            )
            anthropic_tool_name_reverse_map = {
                anthropic_name: openai_name
                for openai_name, anthropic_name in openai_to_anthropic_tool_name_map.items()
            }
        provider_payload_template["model"] = provider_model
        # Anthropic does not accept OpenAI-only flags.
        provider_payload_template.pop("stream_options", None)
        if is_streaming:
            provider_payload_template["stream"] = True
        else:
            provider_payload_template.pop("stream", None)
    else:
        headers = {
            "Content-Type": "application/json",
            "HTTP-Referer": "https://github.com/fabiojbg/LLMApiGateway",
            "X-Title": "LLMGateway",
            **auth_material.headers,
        }
        target_url = f"{provider_base_url.rstrip('/')}/chat/completions"
        provider_payload_template = copy.deepcopy(request_body_json)
        provider_payload_template["model"] = provider_model
        if provider_name == "openrouter" and "usage" not in provider_payload_template:
            provider_payload_template["usage"] = {"include": True}

    custom_body_params = model_fallback_rule.get("custom_body_params", {})
    if custom_body_params:
        for key, value in custom_body_params.items():
            provider_payload_template[key] = value

    provider_payload_template = apply_payload_transforms(
        provider_payload_template,
        model_fallback_rule.get("payload_transforms"),
    )

    custom_headers = model_fallback_rule.get("custom_headers", {})
    if custom_headers:
        for key, value in custom_headers.items():
            # Defense-in-depth: loader already blocks these via FallbackModelRule
            # validation, but a hot-reload or in-memory mutation must never end up
            # overriding Authorization / Cookie / X-Api-Key at the provider call.
            if key.lower() in SECURITY_HEADER_NAMES:
                logging.warning(
                    "Blocked protected header '%s' from custom_headers for rule '%s' (provider=%s).",
                    key,
                    requested_model,
                    provider_name,
                )
                continue
            headers[key] = value

    if not is_anthropic_provider:
        _normalize_provider_attempt_payload(provider_payload_template, provider_model=provider_model)

    last_error_detail = f"Model '{provider_model}' failed without an explicit provider error."
    def _budget_exhausted() -> bool:
        return total_attempts_budget is not None and attempt_number > total_attempts_budget

    def _track_attempt_start() -> None:
        request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
        attempts = getattr(request.state, "llmgateway_fallback_attempts", None)
        if not isinstance(attempts, list):
            attempts = []
            request.state.llmgateway_fallback_attempts = attempts
        attempts.append(
            {
                "provider": provider_name,
                "model": provider_model,
                "upstream_key_fingerprint": upstream_key_fingerprint,
            }
        )
        if isinstance(upstream_state, UpstreamRoutingState):
            upstream_state.record_attempt_start(
                provider_name or "unknown",
                provider_model or "unknown",
                upstream_key_fingerprint,
            )

    def _track_attempt_result(success: bool, error_detail_val: object | None) -> None:
        if not isinstance(upstream_state, UpstreamRoutingState):
            return
        if success:
            upstream_state.record_success(
                provider_name or "unknown",
                provider_model or "unknown",
                upstream_key_fingerprint,
            )
            return
        temporary = _is_temporary_model_failure(error_detail_val)
        upstream_state.record_failure(
            provider_name or "unknown",
            provider_model or "unknown",
            upstream_key_fingerprint,
            error_detail_val,
            temporary=temporary,
            apply_penalty=bool(model_fallback_rule.get("dynamic_penalty_enabled")),
            retry_after=_extract_retry_after(error_detail_val),
        )

    remaining_attempts = retry_count
    while remaining_attempts >= 0:
        if _budget_exhausted():
            logging.warning(
                "Chain-wide attempt budget (%s) exhausted before trying %s for model '%s'.",
                total_attempts_budget,
                provider_model,
                requested_model,
            )
            break

        # Sub-provider ordering is an OpenRouter-style hint that has no meaning
        # for native Anthropic providers; force the plain path for them.
        if (
            not subproviders_ordering
            or not model_fallback_rule.get("use_provider_order_as_fallback")
            or is_anthropic_provider
        ):
            if subproviders_ordering and not is_anthropic_provider:
                logging.info(
                    "Attempting model '%s' in provider '%s' with provider order hint %s",
                    provider_model,
                    provider_name,
                    subproviders_ordering,
                )
            else:
                logging.info("Attempting model '%s' in provider '%s'", provider_model, provider_name)

            attempt_payload = copy.deepcopy(provider_payload_template)
            if subproviders_ordering and not is_anthropic_provider:
                attempt_payload["provider"] = {"order": subproviders_ordering}
                attempt_payload["allow_fallbacks"] = False

            update_active_request(
                request,
                gateway_model=requested_model,
                operation=getattr(request.state, "llmgateway_operation", "chat"),
                provider=provider_name,
                model=provider_model,
            )
            _track_attempt_start()
            t0 = time.monotonic()
            response_data, error_detail = await make_llm_request(
                http_client,
                target_url,
                headers,
                attempt_payload,
                is_streaming,
            )
            duration_ms = int((time.monotonic() - t0) * 1000)

            if response_data and error_detail is None:
                if is_anthropic_provider:
                    if client_expects_anthropic:
                        # Native end-to-end Anthropic: skip the lossy OpenAI
                        # round-trip. ``anthropic_messages`` reads this flag
                        # and forwards the payload/stream as-is.
                        request.state.llmgateway_response_is_anthropic_raw = True
                    elif is_streaming:
                        response_data = _anthropic_stream_to_openai(
                            request,
                            response_data,
                            requested_model,
                            tool_name_reverse_map=anthropic_tool_name_reverse_map,
                        )
                    else:
                        response_data = _anthropic_response_to_openai(
                            response_data,
                            requested_model,
                            tool_name_reverse_map=anthropic_tool_name_reverse_map,
                        )
                request.state.llmgateway_provider = provider_name
                request.state.llmgateway_provider_model = provider_model
                request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
                _track_attempt_result(True, None)
                _record_event(True, None, duration_ms, upstream_key_fingerprint=upstream_key_fingerprint)
                logging.info(
                    "Connection success to model '%s' in provider '%s'. %s response...",
                    provider_model,
                    provider_name,
                    "Starting streaming" if is_streaming else "Waiting",
                )
                return response_data, None, attempt_number

            _record_event(False, error_detail, duration_ms, attempt_payload, upstream_key_fingerprint)
            _track_attempt_result(False, error_detail)
            _log_failed_attempt_warning(
                provider_model,
                provider_name,
                error_detail,
                target_url,
                attempt_payload,
            )
            last_error_detail = f"Model {provider_model} failed with provider '{provider_name}': {error_detail}"
            if stop_after_context_overflow and _is_context_overflow_error(error_detail):
                logging.warning(
                    "Context overflow detected on model '%s' before retry; returning to dispatcher.",
                    provider_model,
                )
                return None, last_error_detail, attempt_number
            logging.debug(
                "Continuing after failed attempt for model '%s' in provider '%s'.",
                provider_model,
                provider_name,
            )
        else:
            logging.info(
                "Provider '%s' uses sub-provider ordering. Target model: %s. Order: %s",
                provider_name,
                provider_model,
                subproviders_ordering,
            )

            for sub_provider in subproviders_ordering:
                if _budget_exhausted():
                    logging.warning(
                        "Chain-wide attempt budget (%s) exhausted before sub-provider '%s' for model '%s'.",
                        total_attempts_budget,
                        sub_provider,
                        requested_model,
                    )
                    break

                logging.info(
                    "Attempting model '%s' on sub-provider '%s' in provider '%s'",
                    provider_model,
                    sub_provider,
                    provider_name,
                )
                attempt_payload = copy.deepcopy(provider_payload_template)
                attempt_payload["provider"] = {"order": [sub_provider]}
                attempt_payload["allow_fallbacks"] = False

                update_active_request(
                    request,
                    gateway_model=requested_model,
                    operation=getattr(request.state, "llmgateway_operation", "chat"),
                    provider=provider_name,
                    model=provider_model,
                )
                _track_attempt_start()
                t0 = time.monotonic()
                response_data, error_detail = await make_llm_request(
                    http_client,
                    target_url,
                    headers,
                    attempt_payload,
                    is_streaming,
                )
                duration_ms = int((time.monotonic() - t0) * 1000)

                if response_data and error_detail is None:
                    request.state.llmgateway_provider = provider_name
                    request.state.llmgateway_provider_model = provider_model
                    request.state.llmgateway_upstream_key_fingerprint = upstream_key_fingerprint
                    _track_attempt_result(True, None)
                    _record_event(True, None, duration_ms, upstream_key_fingerprint=upstream_key_fingerprint)
                    logging.info(
                        "Connection success with model '%s' in provider '%s' via '%s'. %s response...",
                        provider_model,
                        provider_name,
                        sub_provider,
                        "Starting streaming" if is_streaming else "Received",
                    )
                    return response_data, None, attempt_number

                _record_event(False, error_detail, duration_ms, attempt_payload, upstream_key_fingerprint)
                _track_attempt_result(False, error_detail)
                _log_failed_attempt_warning(
                    provider_model,
                    provider_name,
                    error_detail,
                    target_url,
                    attempt_payload,
                    sub_provider=sub_provider,
                )
                last_error_detail = (
                    f"Model '{provider_model}' failed from provider '{provider_name}' "
                    f"and sub-provider {sub_provider} : {error_detail}"
                )
                if stop_after_context_overflow and _is_context_overflow_error(error_detail):
                    logging.warning(
                        "Context overflow detected on model '%s' sub-provider '%s' before retry; returning to dispatcher.",
                        provider_model,
                        sub_provider,
                    )
                    return None, last_error_detail, attempt_number

            logging.warning("All sub-providers for '%s' failed.", provider_name)

        if remaining_attempts > 0 and not _budget_exhausted():
            sleep_seconds = _compute_retry_sleep_seconds(retry_delay, error_detail)
            if sleep_seconds > 0:
                logging.info(
                    "RETRYING %s in %.2f seconds (source=%s)... %s attempts left.",
                    provider_model,
                    sleep_seconds,
                    "retry-after" if _extract_retry_after(error_detail) is not None else "retry_delay+jitter",
                    remaining_attempts - 1,
                )
                await asyncio.sleep(sleep_seconds)
        remaining_attempts -= 1

    return None, last_error_detail, attempt_number


def _extract_retry_after(error_detail: object) -> float | None:
    """Return the ``retry_after`` metadata from a ``RequestErrorDetail`` error, if any."""
    retry_after = getattr(error_detail, "retry_after", None)
    if retry_after is None:
        return None
    try:
        retry_after_seconds = float(retry_after)
    except (TypeError, ValueError):
        return None
    if retry_after_seconds < 0:
        return None
    return retry_after_seconds


def _compute_retry_sleep_seconds(retry_delay: float, error_detail: object) -> float:
    """Resolve the effective sleep time before the next retry attempt.

    Preference order:
      1. Upstream ``Retry-After`` hint (clamped to :data:`MAX_RETRY_AFTER_SECONDS`);
         honored verbatim — no jitter — so we cooperate with provider-side rate limits.
      2. Configured ``retry_delay`` with +/-25% jitter to avoid thundering-herd
         retries when many requests race the same outage.
      3. ``0`` to indicate no sleep.
    """
    retry_after = _extract_retry_after(error_detail)
    if retry_after is not None and retry_after > 0:
        return min(retry_after, MAX_RETRY_AFTER_SECONDS)

    if retry_delay > 0:
        jittered = retry_delay * random.uniform(0.75, 1.25)
        return max(0.0, min(jittered, MAX_RETRY_AFTER_SECONDS))

    return 0.0


async def _dispatch_chat_request(
    request: Request,
    request_body_json: dict,
    *,
    enforce_model_access: bool = True,
):
    config_loader_instance: ConfigLoader = request.app.state.config_loader
    if not config_loader_instance:
        logging.error("ConfigLoader not found in application state within chat_completions.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")
    http_client: object = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logging.error("Shared httpx.AsyncClient not found in application state within chat_completions.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")

    providers_config = config_loader_instance.providers_config
    fallback_rules = config_loader_instance.fallback_rules

    # Reuse the middleware request_id so active and completed usage rows correlate.
    request_id = (
        getattr(request.state, "llmgateway_request_id", None)
        or getattr(request.state, "llmgateway_active_request_id", None)
        or str(uuid4())
    )
    request.state.llmgateway_request_id = request_id

    # Get fallback events DB (may be None if not initialized)
    fallback_events_db: FallbackEventsDB | None = getattr(request.app.state, "fallback_events_db", None)

    requested_model = request_body_json.get("model")
    is_streaming = request_body_json.get("stream", False)

    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    if enforce_model_access:
        enforce_virtual_key_access(request, requested_model)

    try:
        model_resolution = resolve_model_name(
            requested_model,
            getattr(config_loader_instance, "model_rules", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    routing_model = model_resolution.effective_model
    if model_resolution.changed:
        logging.info(
            "Model policy resolved requested model '%s' to routing model '%s' via %s.",
            requested_model,
            routing_model,
            model_resolution.matched_rule,
        )

    payload_to_log = redact_payload_for_log(request_body_json)
    logging.debug(
        "/v1/chat/completions: Request for model '%s'. Payload: %s",
        payload_to_log.get("model", requested_model),
        payload_to_log,
    )

    # Only set if not already set by a specific controller (like Anthropic)
    if not getattr(request.state, "llmgateway_gateway_model", None):
        request.state.llmgateway_gateway_model = requested_model

    # Always ensure operation is set
    if not getattr(request.state, "llmgateway_operation", None):
        request.state.llmgateway_operation = "chat"
    update_active_request(
        request,
        gateway_model=requested_model,
        operation=getattr(request.state, "llmgateway_operation", "chat"),
    )

    fusion_rules = getattr(config_loader_instance, "fusion_rules", None)
    fusion_config = fusion_rules.get(routing_model) if isinstance(fusion_rules, dict) else None
    if fusion_config is not None:
        if request.headers.get("x-llmgateway-fusion"):
            raise HTTPException(status_code=400, detail="Nested Fusion calls are not allowed.")
        if is_streaming:
            raise HTTPException(status_code=400, detail="Fusion models do not support streaming responses.")
        fusion_service = getattr(request.app.state, "fusion_service", None)
        if fusion_service is None:
            raise HTTPException(status_code=500, detail="Fusion service is not available.")
        proxy_http_clients = getattr(request.app.state, "proxy_http_clients", {})
        return await fusion_service.run(
            request=request,
            gateway_model_name=routing_model,
            fusion_config=fusion_config,
            request_body=request_body_json,
            http_client=http_client,
            proxy_http_clients=proxy_http_clients,
        )

    router_rules = getattr(config_loader_instance, "router_rules", None)
    router_config = router_rules.get(routing_model) if isinstance(router_rules, dict) else None
    if router_config is not None:
        router_service = getattr(request.app.state, "router_model_service", None)
        if router_service is None:
            raise HTTPException(status_code=500, detail="Router model service is not available.")
        return await router_service.run(
            request=request,
            gateway_model_name=routing_model,
            router_config=router_config,
            request_body=request_body_json,
        )

    model_config = fallback_rules.get(routing_model)
    context_overflow_fallback = None
    strip_think_tags = False
    max_total_attempts: int | None = None
    dynamic_penalty_enabled = False
    if not model_config:
        if model_resolution.changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model '{requested_model}' resolved to '{routing_model}', "
                    "but no routing rule is configured for the resolved model."
                ),
            )
        logging.warning(f"No specific fallback sequence found for model '{requested_model}'. Using '{settings.fallback_provider}' fallback provider.")

        model_fallbacks_sequence = [{"provider": settings.fallback_provider, "model": requested_model}]
        rotate_models = False
        logging.info(f"Using fallback provider: {settings.fallback_provider}")
    else:
        model_fallbacks_sequence = model_config["fallback_models"]
        rotate_models = model_config["rotate_models"]
        context_overflow_fallback = model_config.get("context_overflow_fallback")
        strip_think_tags = bool(model_config.get("strip_think_tags", False))
        max_total_attempts = model_config.get("max_total_attempts")
        dynamic_penalty_enabled = bool(model_config.get("dynamic_penalty", False))
        logging.info(f"Found routing rule for model '{requested_model}'. Provider sequence length: {len(model_fallbacks_sequence)}")
        logging.info(f"Model rotation is {'enabled' if rotate_models else 'disabled'} for model '{requested_model}'")
        if context_overflow_fallback:
            logging.info("Special context overflow fallback is configured for model '%s'.", requested_model)
        if strip_think_tags:
            logging.info("Literal <think> tag stripping is enabled for gateway model '%s'.", requested_model)
        if dynamic_penalty_enabled:
            logging.info("Dynamic upstream penalty ordering is enabled for gateway model '%s'.", requested_model)

    forced_owner_model = getattr(request.state, "llmgateway_forced_fallback_owner_model", None)
    forced_start_index = getattr(request.state, "llmgateway_forced_fallback_start_index", None)
    if model_config and forced_owner_model == routing_model and isinstance(forced_start_index, int):
        if forced_start_index < 0 or forced_start_index >= len(model_fallbacks_sequence):
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Invalid forced fallback start index {forced_start_index} "
                    f"for model '{routing_model}'."
                ),
            )
        model_fallbacks_sequence = model_fallbacks_sequence[forced_start_index:]
        rotate_models = False
        logging.info(
            "Router selected fallback entry %s for model '%s'; starting chain from that entry.",
            forced_start_index,
            routing_model,
        )

    compress_tool_results = bool(model_config.get("compress_tool_results", False)) if model_config else False
    if compress_tool_results:
        from llm_gateway_core.services.token_compression import compress_messages
        _rtk_stats = compress_messages(request_body_json, enabled=True)
        if _rtk_stats is not None:
            request.state.llmgateway_compression_stats = _rtk_stats

    if dynamic_penalty_enabled:
        upstream_state = getattr(request.app.state, "upstream_routing_state", None)
        if isinstance(upstream_state, UpstreamRoutingState):
            model_fallbacks_sequence = upstream_state.order_rules_by_penalty(
                list(model_fallbacks_sequence),
                providers_config,
            )

    model_fallbacks_sequence = [
        {**model_fallback_rule, "dynamic_penalty_enabled": dynamic_penalty_enabled}
        for model_fallback_rule in model_fallbacks_sequence
    ]
    if context_overflow_fallback:
        context_overflow_fallback = {
            **context_overflow_fallback,
            "dynamic_penalty_enabled": dynamic_penalty_enabled,
        }

    rotation_scope = _rotation_scope_for_request(request)

    start_index = 0
    if rotate_models and len(model_fallbacks_sequence) > 1:
        start_index = await _require_model_rotation_db(request).get_next_model_index(
            api_key=rotation_scope,
            gateway_model=requested_model,
            total_models=len(model_fallbacks_sequence),
        )
        logging.info(f"Model rotation: Starting with model index {start_index} for '{requested_model}'")

    if rotate_models and len(model_fallbacks_sequence) > 1:
        # Guard against hot-reload racing with rotation: the sequence captured here is a
        # local snapshot, but `get_next_model_index` uses its own `total_models`. If the
        # on-disk config was reloaded between both calls, `start_index` might exceed
        # `len(model_fallbacks_sequence)` and produce an empty reorder (users would see
        # "No providers were attempted"). Normalize against the local snapshot length.
        sequence_length = len(model_fallbacks_sequence)
        normalized_start_index = start_index % sequence_length
        if normalized_start_index != start_index:
            logging.warning(
                "Model rotation: start_index %s exceeds local sequence length %s (likely hot-reload race); "
                "normalizing to %s for '%s'.",
                start_index,
                sequence_length,
                normalized_start_index,
                requested_model,
            )
        start_index = normalized_start_index
        reordered_sequence = model_fallbacks_sequence[start_index:] + model_fallbacks_sequence[:start_index]
        model_fallbacks_sequence = reordered_sequence

    last_error_detail = "No providers were attempted."
    context_overflow_fallback_attempted = False
    attempt_number = 1
    for model_fallback_rule in model_fallbacks_sequence:
        if max_total_attempts is not None and attempt_number > max_total_attempts:
            logging.warning(
                "Chain-wide attempt budget (%s) exhausted for model '%s'; "
                "skipping remaining fallback models.",
                max_total_attempts,
                requested_model,
            )
            break

        response_data, error_detail, attempt_number = await _attempt_model_fallback_rule(
            request,
            http_client,
            providers_config,
            requested_model,
            request_body_json,
            model_fallback_rule,
            is_streaming,
            fallback_events_db=fallback_events_db,
            request_id=request_id,
            attempt_number_start=attempt_number,
            total_attempts_budget=max_total_attempts,
            stop_after_context_overflow=bool(
                context_overflow_fallback and not context_overflow_fallback_attempted
            ),
        )

        if response_data and error_detail is None:
            return _finalize_chat_success_response(
                response_data,
                requested_model,
                request_body_json,
                strip_think_tags=strip_think_tags,
                is_anthropic_raw=bool(
                    getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
                ),
            )

        last_error_detail = error_detail or last_error_detail

        if (
            context_overflow_fallback
            and not context_overflow_fallback_attempted
            and _is_context_overflow_error(last_error_detail)
        ):
            context_overflow_fallback_attempted = True
            logging.warning(
                "Context overflow detected for model '%s'. Switching current request to the dedicated context overflow fallback.",
                requested_model,
            )
            context_response, context_error_detail, attempt_number = await _attempt_model_fallback_rule(
                request,
                http_client,
                providers_config,
                requested_model,
                request_body_json,
                context_overflow_fallback,
                is_streaming,
                attempt_label="context overflow fallback model",
                fallback_events_db=fallback_events_db,
                request_id=request_id,
                attempt_number_start=attempt_number,
                total_attempts_budget=max_total_attempts,
            )
            if context_response and context_error_detail is None:
                return _finalize_chat_success_response(
                    context_response,
                    requested_model,
                    request_body_json,
                    strip_think_tags=strip_think_tags,
                    is_anthropic_raw=bool(
                        getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
                    ),
                )

            if context_error_detail:
                last_error_detail = context_error_detail

    logging.error(f"All providers failed for model '{requested_model}'. Last error: {last_error_detail}")
    raise HTTPException(status_code=503, detail=f"All configured providers failed for model '{requested_model}'. Last error: {last_error_detail}")


@router.post("/completions")
async def chat_completions(request: Request):
    request_body_bytes = await request.body()
    request_body_json = _read_json_request_body(request_body_bytes, "/v1/chat/completions", request)
    return await _dispatch_chat_request(request, request_body_json)


@responses_router.post("/responses")
async def openai_responses(request: Request):
    request_body_bytes = await request.body()
    responses_payload = _read_json_request_body(request_body_bytes, "/v1/responses", request)
    openai_payload, requested_model = _responses_request_to_openai_payload(responses_payload)
    
    # Set metadata for stats
    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = "chat"
    
    response_data = await _dispatch_chat_request(request, openai_payload)

    if openai_payload.get("stream", False):
        if not isinstance(response_data, StreamingResponse):
            raise HTTPException(status_code=500, detail="Internal server error: expected streaming response")
        return _openai_stream_to_responses(response_data, requested_model, request=request)

    if not isinstance(response_data, dict):
        raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
    _update_request_usage_tracker(request, response_data.get("usage"))
    return _openai_response_to_responses(response_data, requested_model)


@anthropic_router.post("/messages")
async def anthropic_messages(request: Request):
    request_body_bytes = await request.body()
    anthropic_payload = _read_json_request_body(request_body_bytes, "/v1/messages", request)

    requested_model = anthropic_payload.get("model")
    if requested_model:
        request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = "chat"

    # Preserve the raw Anthropic payload so that native Anthropic providers in
    # the fallback chain receive it verbatim, without a lossy round-trip
    # through the OpenAI shape (cache_control, thinking, metadata, ...).
    request.state.llmgateway_original_anthropic_payload = anthropic_payload

    openai_payload, requested_model = _anthropic_request_to_openai_payload(anthropic_payload)
    response_data = await _dispatch_chat_request(request, openai_payload)

    # A native Anthropic provider in the fallback chain produces an Anthropic-shaped
    # response already; forward it verbatim instead of round-tripping through OpenAI.
    is_anthropic_raw = bool(
        getattr(request.state, "llmgateway_response_is_anthropic_raw", False)
    )

    if openai_payload.get("stream", False):
        if not isinstance(response_data, StreamingResponse):
            raise HTTPException(status_code=500, detail="Internal server error: expected streaming response")
        if is_anthropic_raw:
            return response_data
        return _openai_stream_to_anthropic(request, response_data, requested_model)
    if not isinstance(response_data, dict):
        raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
    _update_request_usage_tracker(request, response_data.get("usage"))
    if is_anthropic_raw:
        return response_data
    return _openai_response_to_anthropic(response_data, requested_model)


@anthropic_router.post("/messages/count_tokens")
async def anthropic_messages_count_tokens(request: Request):
    request_body_bytes = await request.body()
    anthropic_payload = _read_json_request_body(request_body_bytes, "/v1/messages/count_tokens", request)
    openai_payload, requested_model = _anthropic_request_to_openai_payload(anthropic_payload)
    enforce_virtual_key_access(request, requested_model)

    config_loader_instance: ConfigLoader = request.app.state.config_loader
    if not config_loader_instance:
        logging.error("ConfigLoader not found in application state within anthropic_messages_count_tokens.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")

    provider_model_name = _resolve_model_name_for_token_count(config_loader_instance, requested_model)
    input_tokens = _estimate_input_tokens(openai_payload, provider_model_name)
    return {"input_tokens": input_tokens}

# Example of how to potentially add other endpoints to this router
# @router.get("/some_other_chat_endpoint")
# async def some_other_chat_endpoint():
#     return {"message": "Another chat endpoint"}
