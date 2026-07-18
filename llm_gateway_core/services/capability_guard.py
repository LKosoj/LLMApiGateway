from __future__ import annotations

import json
import logging
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from typing import Any

from ..utils.usage_tracking import estimate_prompt_tokens

logger = logging.getLogger(__name__)

# Rough per-image token cost added on top of the text prompt estimate when a
# request contains one or more ``image_url`` content blocks.
IMAGE_TOKEN_ESTIMATE = 1000
# Output token reserve used when the request does not specify ``max_tokens``.
DEFAULT_OUTPUT_RESERVE_TOKENS = 1000
# Upper bound applied to a caller-supplied ``max_tokens`` when reserving room
# for the response inside the context-window estimate.
OUTPUT_RESERVE_CAP_TOKENS = 2000

NO_CAPABLE_MODEL_ERROR_CODE = "no_capable_model"
REASON_VISION_UNSUPPORTED = "vision_unsupported"
REASON_TOOLS_UNSUPPORTED = "tools_unsupported"
REASON_CONTEXT_WINDOW_EXCEEDED = "context_window_exceeded"


@dataclass(frozen=True)
class CandidateCapabilityRejection:
    provider: str | None
    model: str | None
    reason: str


def _iter_message_content_blocks(request_body_json: Mapping[str, Any]) -> Iterator[Mapping[str, Any]]:
    messages = request_body_json.get("messages")
    if not isinstance(messages, list):
        return
    for message in messages:
        if not isinstance(message, Mapping):
            continue
        content = message.get("content")
        if not isinstance(content, list):
            continue
        for block in content:
            if isinstance(block, Mapping):
                yield block


def request_needs_vision(request_body_json: Mapping[str, Any]) -> bool:
    """Detect OpenAI-form ``image_url`` content blocks in the request messages."""
    return any(block.get("type") == "image_url" for block in _iter_message_content_blocks(request_body_json))


def request_needs_tools(request_body_json: Mapping[str, Any]) -> bool:
    """A request needs tool support only when ``tools`` is a non-empty list."""
    tools = request_body_json.get("tools")
    return isinstance(tools, list) and len(tools) > 0


def _count_image_blocks(request_body_json: Mapping[str, Any]) -> int:
    return sum(1 for block in _iter_message_content_blocks(request_body_json) if block.get("type") == "image_url")


def _output_reserve_tokens(request_body_json: Mapping[str, Any]) -> int:
    max_tokens = request_body_json.get("max_tokens")
    if isinstance(max_tokens, bool) or not isinstance(max_tokens, (int, float)) or max_tokens <= 0:
        return DEFAULT_OUTPUT_RESERVE_TOKENS
    return min(int(max_tokens), OUTPUT_RESERVE_CAP_TOKENS)


def estimate_request_tokens_for_routing(request_body_json: Mapping[str, Any]) -> int:
    """Rough prompt+image+output-reserve token estimate used only for routing decisions."""
    prompt_tokens = estimate_prompt_tokens(json.dumps(request_body_json))
    image_tokens = _count_image_blocks(request_body_json) * IMAGE_TOKEN_ESTIMATE
    return prompt_tokens + image_tokens + _output_reserve_tokens(request_body_json)


def filter_capable_candidates(
    candidates: list[dict[str, Any]],
    request_body_json: Mapping[str, Any],
    *,
    gateway_model: str,
) -> tuple[list[dict[str, Any]], list[CandidateCapabilityRejection]]:
    """Drop fallback-chain candidates that declare they cannot serve this request.

    A candidate is only rejected when its metadata explicitly says so
    (``supports_vision``/``supports_tools`` is ``False``, or ``context_window``
    is set and the estimated request size exceeds it). Missing metadata
    (``None``) never filters a candidate out.
    """
    needs_vision = request_needs_vision(request_body_json)
    needs_tools = request_needs_tools(request_body_json)

    # Thunk-cached: the token estimate is only computed if some candidate
    # actually declares a context_window, and at most once even then.
    estimated_tokens_cache: list[int] = []

    def estimated_tokens() -> int:
        if not estimated_tokens_cache:
            estimated_tokens_cache.append(estimate_request_tokens_for_routing(request_body_json))
        return estimated_tokens_cache[0]

    kept: list[dict[str, Any]] = []
    rejections: list[CandidateCapabilityRejection] = []

    for candidate in candidates:
        provider = candidate.get("provider")
        model = candidate.get("model")

        if needs_vision and candidate.get("supports_vision") is False:
            rejections.append(CandidateCapabilityRejection(provider, model, REASON_VISION_UNSUPPORTED))
            logger.debug(
                "Capability guard dropped candidate provider=%s model=%s for gateway model '%s': %s",
                provider,
                model,
                gateway_model,
                REASON_VISION_UNSUPPORTED,
            )
            continue

        if needs_tools and candidate.get("supports_tools") is False:
            rejections.append(CandidateCapabilityRejection(provider, model, REASON_TOOLS_UNSUPPORTED))
            logger.debug(
                "Capability guard dropped candidate provider=%s model=%s for gateway model '%s': %s",
                provider,
                model,
                gateway_model,
                REASON_TOOLS_UNSUPPORTED,
            )
            continue

        context_window = candidate.get("context_window")
        if context_window is not None and estimated_tokens() > context_window:
            rejections.append(CandidateCapabilityRejection(provider, model, REASON_CONTEXT_WINDOW_EXCEEDED))
            logger.debug(
                "Capability guard dropped candidate provider=%s model=%s for gateway model '%s': %s "
                "(estimated_tokens=%s, context_window=%s)",
                provider,
                model,
                gateway_model,
                REASON_CONTEXT_WINDOW_EXCEEDED,
                estimated_tokens(),
                context_window,
            )
            continue

        kept.append(candidate)

    return kept, rejections
