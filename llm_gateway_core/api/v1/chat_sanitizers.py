from __future__ import annotations

import json
import logging
import re


JSON_OBJECT_RESPONSE_FORMAT_TYPES = frozenset({"json_object"})
JSON_MARKDOWN_CODE_BLOCK_RE = re.compile(
    r"^\s*```(?:json)?\s*(?P<payload>.*?)\s*```\s*$",
    re.IGNORECASE | re.DOTALL,
)
JSON_THINK_BLOCK_RE = re.compile(r"<think\b[^>]*>.*?</think>", re.IGNORECASE | re.DOTALL)


def expects_json_object_response(request_body_json: dict) -> bool:
    response_format = request_body_json.get("response_format")
    if not isinstance(response_format, dict):
        return False

    response_type = response_format.get("type")
    return isinstance(response_type, str) and response_type in JSON_OBJECT_RESPONSE_FORMAT_TYPES


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
