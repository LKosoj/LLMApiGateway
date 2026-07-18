"""Detect degenerate ("bad success") non-streaming chat completions.

Some providers answer with HTTP 200 and a syntactically valid body that is
nevertheless useless: an empty assistant message with no tool call, or a
JSON-mode request whose response never contains parseable JSON. Package D
treats these as attempt failures (skipBench: the *model's* behavior, not the
upstream key, is at fault) so the fallback chain moves on instead of handing
the client a hollow 200. Only non-streaming responses are inspected here; the
streaming path is untouched.

Modeled after ``chat_sanitizers.py``: pure functions over plain dicts, no
request/DB/FastAPI state.
"""
from __future__ import annotations

import logging

from .chat_sanitizers import (
    expects_json_object_response,
    extract_sanitized_json_object_content,
)

logger = logging.getLogger(__name__)

# Single registry of model-behavior failure classes. ``unparsed_tool_call_dialect``
# is a placeholder for the upcoming tool-call-rescue package (Package E) and is
# not produced anywhere yet.
MODEL_BEHAVIOR_FAILURE_CLASSES: frozenset[str] = frozenset(
    {"empty_completion", "format_ignored", "unparsed_tool_call_dialect"}
)


class ModelBehaviorFailureDetail(str):
    """``str`` subclass carrying the detected model-behavior failure class.

    Modeled after ``RequestErrorDetail`` (services/request_handler.py):
    wrapped as ``str`` so existing call sites (logging, comparisons, error
    classification) keep working without changes. ``behavior_class`` is
    validated against :data:`MODEL_BEHAVIOR_FAILURE_CLASSES` at construction
    time so callers cannot silently introduce an unregistered class.
    """

    behavior_class: str

    def __new__(cls, message: str, *, behavior_class: str) -> "ModelBehaviorFailureDetail":
        if behavior_class not in MODEL_BEHAVIOR_FAILURE_CLASSES:
            raise ValueError(f"Unknown model behavior failure class: {behavior_class!r}")
        instance = super().__new__(cls, message)
        instance.behavior_class = behavior_class
        return instance


def _extract_openai_message_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        text_parts: list[str] = []
        for part in content:
            if isinstance(part, dict) and part.get("type") == "text":
                text = part.get("text")
                if isinstance(text, str):
                    text_parts.append(text)
        return "".join(text_parts)
    return ""


def _detect_degenerate_openai_completion(
    response_data: dict,
    request_body_json: dict,
) -> ModelBehaviorFailureDetail | None:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices:
        return ModelBehaviorFailureDetail(
            "Model response has no 'choices'.",
            behavior_class="empty_completion",
        )

    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        return ModelBehaviorFailureDetail(
            "Model response has a malformed first choice.",
            behavior_class="empty_completion",
        )

    message = first_choice.get("message")
    if not isinstance(message, dict):
        return ModelBehaviorFailureDetail(
            "Model response has no 'message' in the first choice.",
            behavior_class="empty_completion",
        )

    # A legitimate refusal is not a gateway/model failure.
    if message.get("refusal"):
        return None

    tool_calls = message.get("tool_calls")
    has_tool_calls = isinstance(tool_calls, list) and bool(tool_calls)
    function_call = message.get("function_call")
    has_function_call = isinstance(function_call, dict) and bool(function_call)
    finish_reason = first_choice.get("finish_reason")
    # Diagnosing/repairing unparsed tool-call dialects is Package E's job.
    if has_tool_calls or has_function_call or finish_reason == "tool_calls":
        return None

    text_content = _extract_openai_message_text(message.get("content")).strip()
    if not text_content:
        # Thinking-model responses split the answer into "reasoning_content"
        # (chain-of-thought) and "content" (the actual answer); the rest of
        # the codebase already treats reasoning_content as real response
        # content (request_handler.py::_stream_chunk_has_response_content,
        # chat_dialects.py::_openai_response_to_responses). A non-empty
        # reasoning_content therefore isn't itself a degenerate response —
        # but it must not mask a JSON-mode request that got no JSON: the
        # client only ever sees `content`, never the reasoning trace.
        reasoning_content = message.get("reasoning_content")
        has_reasoning = isinstance(reasoning_content, str) and bool(reasoning_content.strip())
        if has_reasoning and not expects_json_object_response(request_body_json):
            return None
        return ModelBehaviorFailureDetail(
            "Model returned an empty completion with no tool call.",
            behavior_class="empty_completion",
        )

    if expects_json_object_response(request_body_json) and extract_sanitized_json_object_content(text_content) is None:
        return ModelBehaviorFailureDetail(
            "Model ignored the requested JSON response_format.",
            behavior_class="format_ignored",
        )

    return None


def _detect_degenerate_anthropic_completion(
    response_data: dict,
    request_body_json: dict,
) -> ModelBehaviorFailureDetail | None:
    content_blocks = response_data.get("content")
    if not isinstance(content_blocks, list):
        return ModelBehaviorFailureDetail(
            "Model response has no 'content' blocks.",
            behavior_class="empty_completion",
        )

    stop_reason = response_data.get("stop_reason")
    has_tool_use = any(
        isinstance(block, dict) and block.get("type") == "tool_use" for block in content_blocks
    )
    if has_tool_use or stop_reason == "tool_use":
        return None

    text_parts = [
        block.get("text", "")
        for block in content_blocks
        if isinstance(block, dict) and block.get("type") == "text"
    ]
    text_content = "".join(part for part in text_parts if isinstance(part, str)).strip()
    if not text_content:
        # Mirrors the OpenAI-shape reasoning_content exception: extended
        # thinking blocks (type=="thinking") are real model output, but the
        # client only ever sees text blocks, so thinking must not mask a
        # JSON-mode request that produced no JSON text.
        has_thinking = any(
            isinstance(block, dict)
            and block.get("type") == "thinking"
            and isinstance(block.get("thinking"), str)
            and block.get("thinking").strip()
            for block in content_blocks
        )
        if has_thinking and not expects_json_object_response(request_body_json):
            return None
        return ModelBehaviorFailureDetail(
            "Model returned an empty completion with no tool_use block.",
            behavior_class="empty_completion",
        )

    if expects_json_object_response(request_body_json) and extract_sanitized_json_object_content(text_content) is None:
        return ModelBehaviorFailureDetail(
            "Model ignored the requested JSON response_format.",
            behavior_class="format_ignored",
        )

    return None


def detect_degenerate_non_stream_response(
    response_data: object,
    request_body_json: dict,
    *,
    is_anthropic_provider: bool,
) -> ModelBehaviorFailureDetail | None:
    """Detect a degenerate HTTP-200 non-streaming chat completion.

    Only inspects the first choice / response (``n > 1`` is out of scope: a
    client requesting multiple completions still only needs one usable
    result to avoid failover). Returns ``None`` when the response is not
    recognizably a dict in the expected shape, since malformed non-dict
    bodies are already handled upstream by ``make_llm_request``.
    """
    if not isinstance(response_data, dict):
        return None
    if is_anthropic_provider:
        return _detect_degenerate_anthropic_completion(response_data, request_body_json)
    return _detect_degenerate_openai_completion(response_data, request_body_json)
