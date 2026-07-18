import codecs
import json
import logging
import re
import time
from collections.abc import Mapping
from uuid import uuid4

from fastapi import Request
from fastapi.responses import StreamingResponse

from ...services.accounting import AccountingValidationError
from ...services.chat_accounting import (
    ChatTerminalObservation,
    build_direct_chat_terminal_observation,
)
from ...services.request_handler import replace_streaming_response_body
from ...services.stream_observation import SSEEvent, parse_sse_json
from ...utils.text_sanitize import sanitize_payload
from ...utils.usage_tracking import ModelCostRates, estimate_token_count, extract_tokens_usage
from .chat_accounting import ChatStreamDialect
from .chat_dialects import (
    _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH,
    _anthropic_tool_name_to_openai,
    _anthropic_usage_to_openai_usage,
    _build_openai_usage_to_responses_usage,
    _coerce_anthropic_usage_token_count,
    _map_finish_reason_to_anthropic,
)
from .chat_sanitizers import strip_think_blocks as _strip_think_blocks
from ...services.tool_call_rescue import (
    DIALECT_TAG_MARKERS,
    RescuedToolCall,
    RescueResult,
    could_become_dialect_marker,
    repair_tool_arguments,
    rescue_inline_tool_calls,
)

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
_STREAM_TEMPLATE_FIELDS = ("id", "object", "created", "model", "system_fingerprint")


def get_token_usage(chunk_data: dict) -> dict:
    """
    Extracts token usage information from the chunk data.
    Returns a dict with prompt_tokens, completion_tokens, total_tokens, etc.
    """
    return extract_tokens_usage(chunk_data)


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
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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
    return replace_streaming_response_body(
        response,
        sanitized_stream_generator(),
    )


TOOL_CALL_RESCUE_HOLD_MAX_CHARS = 256


def _new_tool_call_rescue_state() -> dict:
    return {
        "mode": "holding",  # "holding" -> "dialect" | "transparent"
        "raw_buffer": "",
        "detect_buffer": "",
        "detect_think_state": _new_stream_think_state(),
        "role_emitted": False,
    }


def _tool_call_rescue_wrap_chunk(template_chunk: dict, choice_index: int, delta: dict) -> dict:
    synthetic_chunk: dict = {
        "choices": [{"index": choice_index, "delta": delta}],
    }
    synthetic_chunk.update(_extract_stream_template(template_chunk))
    return synthetic_chunk


def _tool_call_rescue_flush_transparent(state: dict, template_chunk: dict, choice_index: int) -> list[dict]:
    raw = state["raw_buffer"]
    state["raw_buffer"] = ""
    state["mode"] = "transparent"
    if not raw:
        return []
    delta: dict = {}
    if not state["role_emitted"]:
        delta["role"] = "assistant"
        state["role_emitted"] = True
    delta["content"] = raw
    return [_tool_call_rescue_wrap_chunk(template_chunk, choice_index, delta)]


def _tool_call_rescue_synthesize_success(
    state: dict,
    template_chunk: dict,
    choice_index: int,
    rescue_result: RescueResult,
) -> list[dict]:
    chunks: list[dict] = []
    role_delta: dict = {}
    if not state["role_emitted"]:
        role_delta["role"] = "assistant"
        state["role_emitted"] = True
    if rescue_result.cleaned_text:
        content_delta = dict(role_delta)
        content_delta["content"] = rescue_result.cleaned_text
        chunks.append(_tool_call_rescue_wrap_chunk(template_chunk, choice_index, content_delta))
        role_delta = {}
    tool_call_deltas = []
    for tool_call_index, call in enumerate(rescue_result.tool_calls):
        tool_call_deltas.append(
            {
                "index": tool_call_index,
                "id": f"call_rescued_{tool_call_index}",
                "type": "function",
                "function": {
                    "name": call.name,
                    "arguments": call.arguments,
                },
            }
        )
    tool_calls_delta = dict(role_delta)
    tool_calls_delta["tool_calls"] = tool_call_deltas
    chunks.append(_tool_call_rescue_wrap_chunk(template_chunk, choice_index, tool_calls_delta))
    state["mode"] = "transparent"
    state["raw_buffer"] = ""
    return chunks


def _sanitize_openai_stream_tool_call_rescue(
    response: StreamingResponse,
    requested_model: str,
    tool_schema_map: Mapping[str, object],
) -> StreamingResponse:
    """Rescue inline text tool-call dialects in a streaming OpenAI-shaped response.

    Buffers each choice's content behind a bounded hold-window (see module
    docs / AGENTS-facing design notes): while the accumulated, think-stripped
    text is still a possible prefix of a known dialect marker and under
    ``TOOL_CALL_RESCUE_HOLD_MAX_CHARS``, nothing is forwarded to the client.
    Once one of the tag-delimited dialects' markers (Kimi / ``<function=>`` /
    ``<tool_call>``) is fully seen, the choice switches to unbounded
    accumulation until its content naturally ends (finish_reason/[DONE]),
    then the whole buffered text is parsed and either synthesized into
    ``delta.tool_calls`` chunks (success) or flushed as plain text
    (parse failure or, for the bare-JSON dialect, "not actually a match").

    Streaming responses commit their HTTP headers to the client before this
    generator's body starts running (Starlette sends ``http.response.start``
    before ever awaiting the wrapped body iterator), so once a dialect marker
    has been found there is no way back to a genuine provider failover if the
    accumulated text turns out to be unparsable — the raw text is delivered
    to the client as-is instead of aborting the connection.
    """
    source_iterator = response.body_iterator

    async def sanitized_stream_generator():
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        buffer = ""
        choice_states: dict[int, dict] = {}
        last_template_chunk: dict | None = None

        def get_state(choice_index: int) -> dict:
            state = choice_states.get(choice_index)
            if state is None:
                state = _new_tool_call_rescue_state()
                choice_states[choice_index] = state
            return state

        def serialize_event(payload: dict) -> bytes:
            return f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n".encode("utf-8")

        def finalize_choice(choice_index: int, state: dict, template: dict) -> tuple[list[bytes], bool]:
            if state["mode"] == "dialect":
                rescue_result = rescue_inline_tool_calls(state["raw_buffer"], tool_schema_map)
                if rescue_result.tool_calls and not rescue_result.failed:
                    repaired_calls = [
                        RescuedToolCall(
                            name=call.name,
                            arguments=repair_tool_arguments(call.arguments, tool_schema_map.get(call.name, {})),
                        )
                        for call in rescue_result.tool_calls
                    ]
                    rescue_result = RescueResult(
                        tool_calls=repaired_calls,
                        cleaned_text=rescue_result.cleaned_text,
                        dialect=rescue_result.dialect,
                        failed=rescue_result.failed,
                    )
                    chunks = _tool_call_rescue_synthesize_success(state, template, choice_index, rescue_result)
                    return [serialize_event(c) for c in chunks], True
                # A dialect marker was found but the payload could not be
                # parsed (or, degenerate case, ultimately did not match any
                # dialect). Headers are already committed to the client at
                # this point (see docstring): deliver the raw text as-is
                # rather than aborting the connection.
                chunks = _tool_call_rescue_flush_transparent(state, template, choice_index)
                return [serialize_event(c) for c in chunks], False
            chunks = _tool_call_rescue_flush_transparent(state, template, choice_index)
            return [serialize_event(c) for c in chunks], False

        async for chunk in source_iterator:
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
                        if state["mode"] == "transparent" and not state["raw_buffer"]:
                            continue
                        finalize_events, _synthesized = finalize_choice(
                            choice_index, state, last_template_chunk or {}
                        )
                        for out in finalize_events:
                            yield out
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
                extra_events: list[bytes] = []
                for choice_index, choice in enumerate(choices):
                    if not isinstance(choice, dict):
                        sanitized_choices.append(choice)
                        continue

                    state = get_state(choice_index)
                    finish_reason = choice.get("finish_reason")
                    if finish_reason is not None:
                        finalize_events, synthesized = finalize_choice(choice_index, state, chunk_json)
                        extra_events.extend(finalize_events)
                        if synthesized:
                            choice = dict(choice)
                            choice["finish_reason"] = "tool_calls"
                        sanitized_choices.append(choice)
                        continue

                    delta = choice.get("delta")
                    if not isinstance(delta, dict):
                        sanitized_choices.append(choice)
                        continue

                    content = delta.get("content")
                    if not isinstance(content, str):
                        # Non-text delta (role-only, native tool_calls, ...):
                        # not our dialect. Flush anything held and let
                        # everything from now on pass through untouched.
                        if state["mode"] != "transparent":
                            finalize_events, _synthesized = finalize_choice(choice_index, state, chunk_json)
                            extra_events.extend(finalize_events)
                        sanitized_choices.append(choice)
                        continue

                    if state["mode"] == "transparent":
                        sanitized_choices.append(choice)
                        continue

                    state["raw_buffer"] += content

                    if state["mode"] == "dialect":
                        # Already committed to full accumulation; nothing to
                        # emit until this choice's content ends.
                        continue

                    # mode == "holding": evaluate whether we now know more.
                    detect_delta = _strip_stream_think_blocks(content, state["detect_think_state"])
                    state["detect_buffer"] += detect_delta
                    if any(marker in state["detect_buffer"] for marker in DIALECT_TAG_MARKERS):
                        state["mode"] = "dialect"
                        continue
                    if (
                        len(state["detect_buffer"]) >= TOOL_CALL_RESCUE_HOLD_MAX_CHARS
                        or not could_become_dialect_marker(state["detect_buffer"])
                    ):
                        finalize_events = _tool_call_rescue_flush_transparent(state, chunk_json, choice_index)
                        extra_events.extend(serialize_event(c) for c in finalize_events)

                for out in extra_events:
                    yield out
                if not sanitized_choices and not chunk_json.get("usage"):
                    continue
                chunk_json["choices"] = sanitized_choices
                yield serialize_event(chunk_json)

        for choice_index, state in choice_states.items():
            if state["mode"] == "transparent" and not state["raw_buffer"]:
                continue
            finalize_events, _synthesized = finalize_choice(choice_index, state, last_template_chunk or {})
            for out in finalize_events:
                yield out
        if buffer.strip():
            yield buffer.encode("utf-8")

    logging.info("Enabled streaming tool-call rescue for model '%s'.", requested_model)
    return replace_streaming_response_body(
        response,
        sanitized_stream_generator(),
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


class _DirectChatStreamObservationBuilder:
    __slots__ = (
        "_anthropic_usage",
        "_completion_text_parts",
        "_cost_rate_registry",
        "_dialect",
        "_estimated_prompt_tokens",
        "_model",
        "_openai_usage_payload",
        "_provider",
        "_ttft_ms",
    )

    def __init__(
        self,
        *,
        dialect: ChatStreamDialect,
        provider: str,
        model: str,
        cost_rate_registry: Mapping[tuple[str, str], ModelCostRates],
        estimated_prompt_tokens: int = 0,
        ttft_ms: int | None = None,
    ) -> None:
        self._dialect = dialect
        self._provider = provider
        self._model = model
        self._cost_rate_registry = cost_rate_registry
        self._openai_usage_payload: Mapping[str, object] | None = None
        self._anthropic_usage: dict[str, object] = {}
        self._estimated_prompt_tokens = estimated_prompt_tokens
        self._completion_text_parts: list[str] = []
        self._ttft_ms = ttft_ms

    def _build(self, payload: Mapping[str, object]) -> ChatTerminalObservation:
        return build_direct_chat_terminal_observation(
            payload,
            provider=self._provider,
            model=self._model,
            cost_rate_registry=self._cost_rate_registry,
            ttft_ms=self._ttft_ms,
        )

    def _collect_openai_delta_text(self, payload: Mapping[str, object]) -> None:
        choices = payload.get("choices")
        if not isinstance(choices, list):
            return
        for choice in choices:
            if not isinstance(choice, Mapping):
                continue
            delta = choice.get("delta")
            if not isinstance(delta, Mapping):
                continue
            content = delta.get("content")
            if isinstance(content, str) and content:
                self._completion_text_parts.append(content)
            reasoning_content = delta.get("reasoning_content")
            if isinstance(reasoning_content, str) and reasoning_content:
                self._completion_text_parts.append(reasoning_content)

    def _collect_responses_delta_text(self, payload: Mapping[str, object]) -> None:
        # "response.output_text.delta" carries both real output text and
        # reasoning text (differentiated only by an "annotations" field).
        # "response.function_call_arguments.delta" carries tool-call argument
        # tokens, which the upstream bills the same as any other completion
        # tokens. Both are the complete set of text-bearing event types that
        # _openai_stream_to_responses ever synthesizes (see chat_streaming.py
        # _openai_stream_to_responses), so nothing else can carry billed text
        # here.
        if payload.get("type") not in (
            "response.output_text.delta",
            "response.function_call_arguments.delta",
        ):
            return
        delta_text = payload.get("delta")
        if isinstance(delta_text, str) and delta_text:
            self._completion_text_parts.append(delta_text)

    def _build_estimated(self) -> ChatTerminalObservation:
        """Degrade a stream that ended without an upstream usage chunk.

        Rather than losing the reservation (release, unbilled), estimate
        prompt/completion tokens via tiktoken from what was actually sent
        and delivered, and commit that as ``is_estimated`` usage.
        """
        completion_tokens = estimate_token_count(
            "".join(self._completion_text_parts),
            self._model,
        )
        prompt_tokens = self._estimated_prompt_tokens
        return self._build(
            {
                "usage": {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": prompt_tokens + completion_tokens,
                    "is_estimated": True,
                }
            }
        )

    def build_partial(self) -> ChatTerminalObservation | None:
        """Best-effort observation for a stream aborted before its terminal event.

        Used when the client disconnects or the request is cancelled
        mid-stream: returns the real usage if it was already seen, otherwise
        estimates from whatever content actually reached the client so far.
        Returns ``None`` only when nothing at all was ever streamed, since
        then there is nothing to bill.
        """
        if self._dialect is ChatStreamDialect.OPENAI:
            if self._openai_usage_payload is not None:
                return self._build(self._openai_usage_payload)
            if self._completion_text_parts or self._estimated_prompt_tokens:
                return self._build_estimated()
            return None
        if self._dialect is ChatStreamDialect.ANTHROPIC:
            if not self._anthropic_usage:
                return None
            partial_usage = dict(self._anthropic_usage)
            partial_usage.setdefault("output_tokens", 0)
            partial_usage["is_estimated"] = True
            return self._build({"usage": partial_usage})
        if self._dialect is ChatStreamDialect.RESPONSES:
            if self._completion_text_parts or self._estimated_prompt_tokens:
                return self._build_estimated()
            return None
        return None

    def observe(self, event: SSEEvent) -> ChatTerminalObservation | None:
        if not isinstance(event, SSEEvent):
            raise AccountingValidationError
        event_name = event.event
        if event.done:
            payload: object = "[DONE]"
        else:
            try:
                payload = parse_sse_json(event)
            except (TypeError, ValueError, RecursionError):
                raise AccountingValidationError from None
            if not isinstance(payload, Mapping):
                raise AccountingValidationError
        if self._dialect is ChatStreamDialect.OPENAI:
            if payload == "[DONE]":
                if self._openai_usage_payload is not None:
                    return self._build(self._openai_usage_payload)
                return self._build_estimated()
            if isinstance(payload, Mapping):
                if payload.get("usage") is not None:
                    self._openai_usage_payload = payload
                self._collect_openai_delta_text(payload)
            return None

        if self._dialect is ChatStreamDialect.RESPONSES:
            if event.done:
                return None
            self._collect_responses_delta_text(payload)
            if payload.get("type") != "response.completed":
                return None
            response_payload = payload.get("response")
            if not isinstance(response_payload, Mapping):
                raise AccountingValidationError
            return self._build(response_payload)

        if self._dialect is not ChatStreamDialect.ANTHROPIC or event.done:
            raise AccountingValidationError
        if not isinstance(payload, Mapping):
            return None
        payload_type = payload.get("type")
        if event_name is not None and payload_type != event_name:
            raise AccountingValidationError
        if event_name is None:
            event_name = payload_type if isinstance(payload_type, str) else None
        usage: object = payload.get("usage")
        if event_name == "message_start":
            message = payload.get("message")
            if not isinstance(message, Mapping):
                raise AccountingValidationError
            usage = message.get("usage")
        if usage is not None:
            if not isinstance(usage, Mapping):
                raise AccountingValidationError
            self._anthropic_usage.update(usage)
        if event_name == "message_stop":
            return self._build({"usage": dict(self._anthropic_usage)})
        return None


def _sanitize_anthropic_stream_think_tags(response: StreamingResponse, requested_model: str) -> StreamingResponse:
    """Strip ``<think>`` blocks from a native Anthropic SSE stream.

    Text arrives via ``content_block_delta`` events (``delta.type == "text_delta"``).
    State is kept per content-block index so tags split across deltas are handled,
    and any buffered partial is flushed right before that block's
    ``content_block_stop``. All other events pass through verbatim.
    """
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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
    return replace_streaming_response_body(
        response,
        sanitized_stream_generator(),
    )


def _sanitize_openai_json_object_stream(response: StreamingResponse, requested_model: str) -> StreamingResponse:
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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
    return replace_streaming_response_body(
        response,
        sanitized_stream_generator(),
    )


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
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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

        # Process any remaining event left in the buffer after the stream ends
        # (mirrors the leftover-buffer flush in the other SSE converters below).
        if buffer.strip():
            raw_event = buffer
            event_name = None
            data_lines = []
            for line in raw_event.split("\n"):
                if line.startswith(":"):
                    continue  # SSE comment / keep-alive
                if line.startswith("event:"):
                    event_name = line[len("event:"):].strip() or None
                elif line.startswith("data:"):
                    data_lines.append(line[len("data:"):].lstrip())
            if data_lines:
                data_str = "\n".join(data_lines).strip()
                if data_str and data_str != "[DONE]":
                    try:
                        event_data = sanitize_payload(json.loads(data_str))
                    except Exception:
                        event_data = None
                    if event_data is not None:
                        if event_name is None and isinstance(event_data, dict):
                            event_name = event_data.get("type")
                        if isinstance(event_name, str) and isinstance(event_data, dict):
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

    return replace_streaming_response_body(
        response,
        openai_stream_generator(),
    )


def _format_anthropic_sse_event(event_name: str, payload: dict) -> bytes:
    event_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
    return f"event: {event_name}\ndata: {event_json}\n\n".encode("utf-8")


def _is_openai_stream_error_chunk(chunk_json: dict) -> bool:
    # Check the *value* of "error", not just key presence: providers that emit
    # "error": null on the terminal usage-only chunk must not have it converted
    # into a client-facing response.failed / anthropic error event, which would
    # also drop the real usage carried by that chunk. Mirrors the accounting
    # boundary in chat_accounting._is_error_sse_event (OPENAI dialect).
    return chunk_json.get("error") is not None and not chunk_json.get("choices")


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
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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

    response.headers["anthropic-version"] = "2023-06-01"
    return replace_streaming_response_body(
        response,
        anthropic_stream_generator(),
    )


def _format_responses_sse_event(payload: dict) -> bytes:
    event_json = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return f"data: {event_json}\n\n".encode("utf-8")


def _openai_stream_to_responses(
    response: StreamingResponse,
    requested_model: str,
    request: Request | None = None,
) -> StreamingResponse:
    source_iterator = response.body_iterator

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

        async for chunk in source_iterator:
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

    return replace_streaming_response_body(
        response,
        responses_stream_generator(),
    )
