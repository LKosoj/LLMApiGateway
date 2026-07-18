"""Rescue tool calls that a model wrote as plain text instead of a structured
``tool_calls`` field, and repair malformed/double-encoded tool-call arguments.

Several provider "dialects" leak inline text tool-call markup into
``message.content`` instead of (or alongside) the structured OpenAI
``tool_calls`` field:

- Kimi/Moonshot: ``<|tool_calls_section_begin|>...<|tool_calls_section_end|>``
  wrapping one or more ``<|tool_call_begin|>functions.NAME:IDX
  <|tool_call_argument_begin|>{json}<|tool_call_end|>`` blocks.
- ``<function=NAME>{json}</function>`` (and the malformed variant missing the
  closing ``>`` before the JSON payload: ``<function=NAME{json}</function>``).
- ``<tool_call>{"name": ..., "arguments"|"parameters": ...}</tool_call>``.
- Bare or ```` ```json ````-fenced JSON matching ``{"name": ..., "arguments":
  ...}`` — strictly gated on the whole message being exactly that JSON *and*
  ``name`` being a tool the request actually declared, since an assistant
  message that merely contains a brace is not evidence of a tool call.

This module is pure (no request/DB/FastAPI state), modeled after
``chat_sanitizers.py`` / ``chat_model_behavior.py``. Callers (``chat_dispatch.py``,
``chat_streaming.py``) decide what to do with the result: this module only
detects and extracts.
"""
from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass, field


@dataclass(frozen=True)
class RescuedToolCall:
    name: str
    arguments: str


@dataclass(frozen=True)
class RescueResult:
    tool_calls: list[RescuedToolCall] = field(default_factory=list)
    cleaned_text: str | None = None
    dialect: str | None = None
    failed: bool = False


KIMI_SECTION_BEGIN = "<|tool_calls_section_begin|>"
KIMI_SECTION_END = "<|tool_calls_section_end|>"
KIMI_CALL_BEGIN = "<|tool_call_begin|>"
KIMI_CALL_ARGUMENT_BEGIN = "<|tool_call_argument_begin|>"
KIMI_CALL_END = "<|tool_call_end|>"

FUNCTION_TAG_MARKER = "<function="
FUNCTION_TAG_CLOSE = "</function>"

TOOL_CALL_TAG_OPEN = "<tool_call>"
TOOL_CALL_TAG_CLOSE = "</tool_call>"

JSON_FENCE_MARKER = "```json"

# All markers whose *start* a streaming hold-window prefix might still grow
# into (see ``could_become_dialect_marker``). The generic JSON markers ("{",
# "```json") are included here for the hold decision, but deliberately not
# treated as a "definitely this dialect, keep accumulating forever" signal
# anywhere in this module or its callers — a lone brace is not proof of a
# tool call, only a fully tag-delimited dialect (Kimi / <function=> /
# <tool_call>) is.
DIALECT_MARKERS: tuple[str, ...] = (
    KIMI_SECTION_BEGIN,
    FUNCTION_TAG_MARKER,
    TOOL_CALL_TAG_OPEN,
    JSON_FENCE_MARKER,
    "{",
)

# Subset of DIALECT_MARKERS whose complete appearance in accumulated text is
# an unambiguous signal of a tag-delimited dialect (as opposed to the bare
# JSON dialect, which is only ever resolved from a short, self-contained
# message — see the module docstring / chat_streaming.py's hold-window).
DIALECT_TAG_MARKERS: tuple[str, ...] = (
    KIMI_SECTION_BEGIN,
    FUNCTION_TAG_MARKER,
    TOOL_CALL_TAG_OPEN,
)

_JSON_FENCE_RE = re.compile(
    r"^```(?:json)?\s*(?P<payload>.*?)\s*```$",
    re.IGNORECASE | re.DOTALL,
)


def could_become_dialect_marker(text: str) -> bool:
    """Is ``text`` still a possible prefix of one of ``DIALECT_MARKERS``?

    Used by streaming callers to decide whether to keep holding buffered
    text back from the client instead of forwarding it transparently. Empty
    or whitespace-only text is trivially a prefix of everything.
    """
    if not text or text.isspace():
        return True
    return any(marker.startswith(text) for marker in DIALECT_MARKERS)


def extract_balanced_json(text: str, start: int) -> tuple[str, int] | None:
    """Extract a brace-balanced JSON object starting at ``text[start]``.

    Honors strings and escape sequences, so ``}``/``{`` inside a quoted
    string do not affect nesting depth. Returns ``(json_text, end_index)``
    where ``end_index`` is the index just past the closing ``}``, or
    ``None`` if ``text[start]`` is not ``{`` or the object is unterminated.
    """
    length = len(text)
    if start >= length or text[start] != "{":
        return None

    depth = 0
    in_string = False
    escape = False
    index = start
    while index < length:
        char = text[index]
        if in_string:
            if escape:
                escape = False
            elif char == "\\":
                escape = True
            elif char == '"':
                in_string = False
        else:
            if char == '"':
                in_string = True
            elif char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return text[start : index + 1], index + 1
        index += 1
    return None


def build_tool_schema_map(request_tools: object) -> dict[str, dict]:
    """Build a ``{tool_name: parameters_schema}`` map from a request's ``tools``."""
    schema_map: dict[str, dict] = {}
    if not isinstance(request_tools, list):
        return schema_map
    for tool in request_tools:
        if not isinstance(tool, dict):
            continue
        function = tool.get("function")
        if not isinstance(function, dict):
            continue
        name = function.get("name")
        if not isinstance(name, str) or not name:
            continue
        parameters = function.get("parameters")
        schema_map[name] = parameters if isinstance(parameters, dict) else {}
    return schema_map


def repair_tool_arguments(arguments_json: str, schema: object) -> str:
    """Repair common tool-call argument malformations against ``schema``.

    - Unwraps one level of double-encoding (arguments is itself a JSON
      string containing a JSON object).
    - Coerces a top-level string value to an array/object *only* when the
      schema declares that parameter's type as ``array``/``object`` *and*
      the string parses exactly to that type. String-typed parameters are
      never touched, and coercion never recurses past the top level.
    - Any parse failure (at any step) returns ``arguments_json`` unchanged.
    """
    if not isinstance(arguments_json, str):
        return arguments_json

    try:
        parsed = json.loads(arguments_json)
    except (ValueError, TypeError):
        return arguments_json

    unwrapped = False
    if isinstance(parsed, str):
        try:
            inner = json.loads(parsed)
        except (ValueError, TypeError):
            return arguments_json
        if not isinstance(inner, dict):
            return arguments_json
        parsed = inner
        unwrapped = True

    if not isinstance(parsed, dict):
        return arguments_json

    properties = {}
    if isinstance(schema, Mapping):
        raw_properties = schema.get("properties")
        if isinstance(raw_properties, Mapping):
            properties = raw_properties

    repaired = dict(parsed)
    field_changed = False
    for key, value in parsed.items():
        if not isinstance(value, str):
            continue
        property_schema = properties.get(key)
        if not isinstance(property_schema, Mapping):
            continue
        declared_type = property_schema.get("type")
        if declared_type not in ("array", "object"):
            continue
        try:
            candidate = json.loads(value)
        except (ValueError, TypeError):
            continue
        if declared_type == "array" and isinstance(candidate, list):
            repaired[key] = candidate
            field_changed = True
        elif declared_type == "object" and isinstance(candidate, dict):
            repaired[key] = candidate
            field_changed = True

    if not unwrapped and not field_changed:
        return arguments_json

    try:
        return json.dumps(repaired)
    except (TypeError, ValueError):
        return arguments_json


def _coerce_arguments_value(value: object) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        try:
            return json.dumps(value)
        except (TypeError, ValueError):
            return None
    if value is None:
        return "{}"
    return None


def _parse_kimi_dialect(content: str) -> RescueResult | None:
    section_start = content.find(KIMI_SECTION_BEGIN)
    if section_start == -1:
        return None
    body_start = section_start + len(KIMI_SECTION_BEGIN)
    section_end = content.find(KIMI_SECTION_END, body_start)
    if section_end == -1:
        return None

    section_body = content[body_start:section_end]
    calls: list[RescuedToolCall] = []
    cursor = 0
    while True:
        call_begin = section_body.find(KIMI_CALL_BEGIN, cursor)
        if call_begin == -1:
            break
        header_start = call_begin + len(KIMI_CALL_BEGIN)
        arg_begin = section_body.find(KIMI_CALL_ARGUMENT_BEGIN, header_start)
        if arg_begin == -1:
            return None
        header = section_body[header_start:arg_begin].strip()
        name = header
        if name.startswith("functions."):
            name = name[len("functions."):]
        if ":" in name:
            name = name.rsplit(":", 1)[0]
        name = name.strip()
        if not name:
            return None
        json_start = arg_begin + len(KIMI_CALL_ARGUMENT_BEGIN)
        extracted = extract_balanced_json(section_body, json_start)
        if extracted is None:
            return None
        json_text, json_end = extracted
        end_marker = section_body.find(KIMI_CALL_END, json_end)
        if end_marker == -1:
            return None
        calls.append(RescuedToolCall(name=name, arguments=json_text))
        cursor = end_marker + len(KIMI_CALL_END)

    if not calls:
        return None

    cleaned = (content[:section_start] + content[section_end + len(KIMI_SECTION_END):]).strip()
    return RescueResult(
        tool_calls=calls,
        cleaned_text=cleaned or None,
        dialect="kimi",
        failed=False,
    )


def _parse_function_tag_dialect(content: str) -> RescueResult | None:
    calls: list[RescuedToolCall] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        marker_start = content.find(FUNCTION_TAG_MARKER, cursor)
        if marker_start == -1:
            break
        name_start = marker_start + len(FUNCTION_TAG_MARKER)
        gt_index = content.find(">", name_start)
        brace_index = content.find("{", name_start)
        if brace_index == -1:
            return None
        if gt_index != -1 and gt_index < brace_index:
            name = content[name_start:gt_index]
            json_start = gt_index + 1
        else:
            # Malformed variant: no closing '>' before the JSON payload.
            name = content[name_start:brace_index]
            json_start = brace_index
        name = name.strip()
        if not name:
            return None
        extracted = extract_balanced_json(content, json_start)
        if extracted is None:
            return None
        json_text, json_end = extracted
        remainder = content[json_end:]
        close_index = remainder.find(FUNCTION_TAG_CLOSE)
        if close_index == -1 or remainder[:close_index].strip():
            return None
        block_end = json_end + close_index + len(FUNCTION_TAG_CLOSE)
        calls.append(RescuedToolCall(name=name, arguments=json_text))
        spans.append((marker_start, block_end))
        cursor = block_end

    if not calls:
        return None

    cleaned = content
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = cleaned.strip()
    return RescueResult(
        tool_calls=calls,
        cleaned_text=cleaned or None,
        dialect="function_tag",
        failed=False,
    )


def _parse_tool_call_tag_dialect(content: str) -> RescueResult | None:
    calls: list[RescuedToolCall] = []
    spans: list[tuple[int, int]] = []
    cursor = 0
    while True:
        open_index = content.find(TOOL_CALL_TAG_OPEN, cursor)
        if open_index == -1:
            break
        json_start = open_index + len(TOOL_CALL_TAG_OPEN)
        while json_start < len(content) and content[json_start].isspace():
            json_start += 1
        extracted = extract_balanced_json(content, json_start)
        if extracted is None:
            return None
        json_text, json_end = extracted
        remainder = content[json_end:]
        close_index = remainder.find(TOOL_CALL_TAG_CLOSE)
        if close_index == -1 or remainder[:close_index].strip():
            return None
        block_end = json_end + close_index + len(TOOL_CALL_TAG_CLOSE)

        try:
            parsed = json.loads(json_text)
        except (ValueError, TypeError):
            return None
        if not isinstance(parsed, dict):
            return None
        name = parsed.get("name")
        if not isinstance(name, str) or not name:
            return None
        if "arguments" in parsed:
            args_value = parsed.get("arguments")
        elif "parameters" in parsed:
            args_value = parsed.get("parameters")
        else:
            return None
        arguments_json = _coerce_arguments_value(args_value)
        if arguments_json is None:
            return None

        calls.append(RescuedToolCall(name=name, arguments=arguments_json))
        spans.append((open_index, block_end))
        cursor = block_end

    if not calls:
        return None

    cleaned = content
    for start, end in reversed(spans):
        cleaned = cleaned[:start] + cleaned[end:]
    cleaned = cleaned.strip()
    return RescueResult(
        tool_calls=calls,
        cleaned_text=cleaned or None,
        dialect="tool_call_tag",
        failed=False,
    )


def _parse_bare_json_dialect(content: str, tool_schema_map: Mapping[str, object]) -> RescueResult | None:
    stripped = content.strip()
    if not stripped or not tool_schema_map:
        return None

    fence_match = _JSON_FENCE_RE.match(stripped)
    if fence_match and fence_match.end() == len(stripped):
        candidate = fence_match.group("payload").strip()
    else:
        candidate = stripped

    if not candidate.startswith("{"):
        return None

    try:
        parsed = json.loads(candidate)
    except (ValueError, TypeError):
        return None
    if not isinstance(parsed, dict):
        return None

    name = parsed.get("name")
    if not isinstance(name, str) or name not in tool_schema_map:
        return None

    if "arguments" in parsed:
        args_value = parsed.get("arguments")
    elif "parameters" in parsed:
        args_value = parsed.get("parameters")
    else:
        return None
    arguments_json = _coerce_arguments_value(args_value)
    if arguments_json is None:
        return None

    return RescueResult(
        tool_calls=[RescuedToolCall(name=name, arguments=arguments_json)],
        cleaned_text=None,
        dialect="bare_json",
        failed=False,
    )


def rescue_inline_tool_calls(content: str, tool_schema_map: Mapping[str, object]) -> RescueResult:
    """Detect and extract tool calls written as plain text in ``content``.

    Checks the tag-delimited dialects (Kimi, ``<function=>``, ``<tool_call>``)
    in that order first: if one of their start markers is present but the
    dialect cannot be fully parsed, the result is ``failed=True`` (the caller
    should treat this as a model-behavior failure, not silently pass the raw
    text through). Only when none of those markers are present is the
    strictly schema-gated bare/fenced-JSON dialect attempted; a non-match
    there is not a failure, just "no dialect detected".
    """
    if not isinstance(content, str) or not content:
        return RescueResult()

    if KIMI_SECTION_BEGIN in content:
        result = _parse_kimi_dialect(content)
        return result if result is not None else RescueResult(dialect="kimi", failed=True)

    if FUNCTION_TAG_MARKER in content:
        result = _parse_function_tag_dialect(content)
        return result if result is not None else RescueResult(dialect="function_tag", failed=True)

    if TOOL_CALL_TAG_OPEN in content:
        result = _parse_tool_call_tag_dialect(content)
        return result if result is not None else RescueResult(dialect="tool_call_tag", failed=True)

    bare_result = _parse_bare_json_dialect(content, tool_schema_map)
    if bare_result is not None:
        return bare_result

    return RescueResult()
