from __future__ import annotations

import json
import logging
import re


# Package D deliberately widens this from {"json_object"} to also cover
# "json_schema": both response_format types promise the client a parseable
# JSON payload, and Package D's degenerate-response detector (format_ignored)
# needs to recognize a json_schema request whose model ignored the format just
# as much as a json_object one. This is a conscious blast-radius expansion of
# the existing cosmetic sanitizer in expects_json_object_response() (it now
# also runs non-stream/stream JSON-object sanitization for json_schema
# requests), not just the new detector.
JSON_OBJECT_RESPONSE_FORMAT_TYPES = frozenset({"json_object", "json_schema"})
JSON_MARKDOWN_CODE_BLOCK_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<payload>.*?)\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)
JSON_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)


def response_format_type(request_body_json: dict) -> str | None:
    response_format = request_body_json.get("response_format")
    if not isinstance(response_format, dict):
        return None

    response_type = response_format.get("type")
    return response_type if isinstance(response_type, str) else None


def expects_json_object_response(request_body_json: dict) -> bool:
    return response_format_type(request_body_json) in JSON_OBJECT_RESPONSE_FORMAT_TYPES


def is_json_object_payload(text: str) -> bool:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return False

    return isinstance(parsed, dict)


def strip_think_blocks(text: str) -> str:
    return JSON_THINK_BLOCK_RE.sub("", text)


def unwrap_json_object_markdown_wrapper(text: str) -> str | None:
    stripped_text = text.strip()
    candidates: list[str] = []

    markdown_match = JSON_MARKDOWN_CODE_BLOCK_RE.fullmatch(stripped_text)
    if markdown_match:
        payload = markdown_match.group("payload").strip()
        if payload:
            candidates.append(payload)

    if stripped_text.lower().startswith("json"):
        prefixed_payload = stripped_text[4:].lstrip(" \t\r\n:")
        if prefixed_payload:
            candidates.append(prefixed_payload)

    for candidate in candidates:
        if is_json_object_payload(candidate):
            return candidate

    return None


def _bracketed_json_slices(text: str) -> list[str]:
    """Port of ``candidateJsonSlices`` (freellmapi): brace/bracket-bounded slices.

    Returns the substring from the first ``{`` to the last ``}`` and the
    substring from the first ``[`` to the last ``]`` (whichever are present),
    longest first, so a caller trying each in turn favors the more complete
    candidate before a shorter one.
    """
    slices: list[str] = []

    first_brace = text.find("{")
    last_brace = text.rfind("}")
    if first_brace != -1 and last_brace != -1 and first_brace < last_brace:
        slices.append(text[first_brace : last_brace + 1])

    first_bracket = text.find("[")
    last_bracket = text.rfind("]")
    if first_bracket != -1 and last_bracket != -1 and first_bracket < last_bracket:
        slices.append(text[first_bracket : last_bracket + 1])

    slices.sort(key=len, reverse=True)
    return slices


def extract_sanitized_json_object_content(text: str) -> str | None:
    stripped_text = text.strip()
    candidates = [stripped_text]

    without_think_blocks = strip_think_blocks(stripped_text).strip()
    if without_think_blocks != stripped_text:
        candidates.append(without_think_blocks)

    for candidate in candidates:
        if is_json_object_payload(candidate):
            return candidate

        unwrapped_candidate = unwrap_json_object_markdown_wrapper(candidate)
        if unwrapped_candidate is not None:
            return unwrapped_candidate

    # Last-resort candidate: a prose-wrapped JSON object recovered by slicing
    # from the first "{" to the last "}" (or "[" .. "]"), longest match first.
    for candidate in candidates:
        for bracket_slice in _bracketed_json_slices(candidate):
            if is_json_object_payload(bracket_slice):
                return bracket_slice

    return None


def sanitize_json_object_response_content(response_data: dict, requested_model: str) -> None:
    choices = response_data.get("choices")
    if not isinstance(choices, list):
        return

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        message = choice.get("message")
        if not isinstance(message, dict):
            continue

        content = message.get("content")
        if not isinstance(content, str):
            continue

        sanitized_content = extract_sanitized_json_object_content(content)
        if sanitized_content is None:
            continue

        if sanitized_content != content:
            logging.info("Sanitized non-stream JSON response content for model '%s'.", requested_model)
            message["content"] = sanitized_content


def sanitize_openai_response_content_think_tags(response_data: dict, requested_model: str) -> None:
    choices = response_data.get("choices")
    if not isinstance(choices, list):
        return

    for choice in choices:
        if not isinstance(choice, dict):
            continue

        message = choice.get("message")
        if not isinstance(message, dict):
            continue

        content = message.get("content")
        if not isinstance(content, str):
            continue

        sanitized_content = strip_think_blocks(content)
        if sanitized_content == content:
            continue

        logging.info("Stripped <think> tags from non-stream response content for model '%s'.", requested_model)
        message["content"] = sanitized_content
