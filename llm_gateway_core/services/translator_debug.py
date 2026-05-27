"""Translation debug service.

Provides a stateless helper that runs a request through the full
OpenAI ↔ Anthropic translation pipeline and returns each intermediate
step, so developers can inspect what the gateway does to their payloads
without sending a real request to a provider.
"""

from __future__ import annotations

import copy
from typing import Any

from ..api.v1.chat import (
    _anthropic_request_to_openai_payload,
    _anthropic_response_to_openai,
    _openai_request_to_anthropic_payload,
    _openai_response_to_anthropic,
)

# ──────────────────────────────────────────────────────────────────────────────
# Default mock response used when the caller does not supply one.
# ──────────────────────────────────────────────────────────────────────────────

_DEFAULT_OPENAI_MOCK: dict[str, Any] = {
    "id": "chatcmpl-mock000000000000",
    "object": "chat.completion",
    "created": 0,
    "model": "mock-model",
    "choices": [
        {
            "index": 0,
            "message": {"role": "assistant", "content": "Hello, this is a mock response."},
            "finish_reason": "stop",
        }
    ],
    "usage": {"prompt_tokens": 10, "completion_tokens": 8, "total_tokens": 18},
}

_DEFAULT_ANTHROPIC_MOCK: dict[str, Any] = {
    "id": "msg_mock000000000000",
    "type": "message",
    "role": "assistant",
    "model": "mock-model",
    "content": [{"type": "text", "text": "Hello, this is a mock response."}],
    "stop_reason": "end_turn",
    "stop_sequence": None,
    "usage": {"input_tokens": 10, "output_tokens": 8},
}


def build_translation_steps(
    source_format: str,
    target_format: str,
    request_body: dict[str, Any],
    mock_provider_response: dict[str, Any] | None,
) -> list[dict[str, Any]]:
    """Run the request through the translation pipeline and return 7 step dicts.

    Each dict has keys: step, name, title, payload, notes.

    No fallback is used — any translation error propagates as an exception,
    so callers see the real error rather than silent degradation.
    """

    steps: list[dict[str, Any]] = []

    def _step(
        step: int,
        name: str,
        title: str,
        payload: Any,
        notes: str | None = None,
    ) -> None:
        steps.append(
            {
                "step": step,
                "name": name,
                "title": title,
                "payload": payload,
                "notes": notes,
            }
        )

    # ── Step 1: client request (as-is) ───────────────────────────────────────
    _step(
        1,
        "client_request",
        "Client Request",
        copy.deepcopy(request_body),
        "The raw request body as received from the client.",
    )

    # ── Step 2: detected format ───────────────────────────────────────────────
    requires_translation = source_format != target_format
    _step(
        2,
        "detected_format",
        "Detected Format",
        {
            "source_format": source_format,
            "target_format": target_format,
            "requires_translation": requires_translation,
        },
        "Formats resolved from the request parameters.",
    )

    # ── Step 3: intermediate OpenAI request ──────────────────────────────────
    if source_format == "anthropic":
        openai_request, requested_model = _anthropic_request_to_openai_payload(
            copy.deepcopy(request_body)
        )
        step3_notes = (
            "Anthropic request converted to OpenAI format via "
            "_anthropic_request_to_openai_payload."
        )
    else:
        # source is already openai — pass through unchanged
        openai_request = copy.deepcopy(request_body)
        requested_model = request_body.get("model", "unknown")
        step3_notes = "Source is already OpenAI format; no conversion needed."

    _step(
        3,
        "intermediate_openai_request",
        "Intermediate OpenAI Request",
        copy.deepcopy(openai_request),
        step3_notes,
    )

    # ── Step 4: target request ────────────────────────────────────────────────
    if target_format == "anthropic":
        target_request = _openai_request_to_anthropic_payload(copy.deepcopy(openai_request))
        step4_notes = (
            "OpenAI request converted to Anthropic format via "
            "_openai_request_to_anthropic_payload."
        )
    else:
        # target is openai — same as step 3
        target_request = copy.deepcopy(openai_request)
        step4_notes = "Target is OpenAI format; step 3 payload forwarded unchanged."

    _step(
        4,
        "target_request",
        "Target Request (Provider Format)",
        copy.deepcopy(target_request),
        step4_notes,
    )

    # ── Step 5: provider response mock ───────────────────────────────────────
    if mock_provider_response is not None:
        provider_mock = copy.deepcopy(mock_provider_response)
        step5_notes = "Provider response mock supplied by caller."
    elif target_format == "anthropic":
        provider_mock = copy.deepcopy(_DEFAULT_ANTHROPIC_MOCK)
        provider_mock["model"] = requested_model
        step5_notes = (
            "No mock supplied; using default Anthropic-format mock response. "
            "Provide mock_provider_response for a realistic test."
        )
    else:
        provider_mock = copy.deepcopy(_DEFAULT_OPENAI_MOCK)
        provider_mock["model"] = requested_model
        step5_notes = (
            "No mock supplied; using default OpenAI-format mock response. "
            "Provide mock_provider_response for a realistic test."
        )

    _step(
        5,
        "provider_response_mock",
        "Provider Response (Mock)",
        copy.deepcopy(provider_mock),
        step5_notes,
    )

    # ── Step 6: intermediate OpenAI response ─────────────────────────────────
    if target_format == "anthropic":
        openai_response = _anthropic_response_to_openai(
            copy.deepcopy(provider_mock), requested_model
        )
        step6_notes = (
            "Anthropic provider response converted back to OpenAI format via "
            "_anthropic_response_to_openai."
        )
    else:
        # target was openai — mock is already openai-shaped
        openai_response = copy.deepcopy(provider_mock)
        step6_notes = "Provider returned OpenAI format; no conversion needed."

    _step(
        6,
        "intermediate_openai_response",
        "Intermediate OpenAI Response",
        copy.deepcopy(openai_response),
        step6_notes,
    )

    # ── Step 7: client response ───────────────────────────────────────────────
    if source_format == "anthropic":
        client_response = _openai_response_to_anthropic(
            copy.deepcopy(openai_response), requested_model
        )
        step7_notes = (
            "OpenAI response converted to Anthropic format for the original caller "
            "via _openai_response_to_anthropic."
        )
    else:
        # client expects openai — same as step 6
        client_response = copy.deepcopy(openai_response)
        step7_notes = "Client expects OpenAI format; step 6 payload forwarded unchanged."

    _step(
        7,
        "client_response",
        "Client Response",
        copy.deepcopy(client_response),
        step7_notes,
    )

    return steps
