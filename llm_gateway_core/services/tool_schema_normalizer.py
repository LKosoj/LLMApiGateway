"""Compatibility shim for JSON Schema inside tool/function specifications.

OpenAI, Anthropic, Gemini and older OpenAI-compatible providers each expect a
slightly different dialect of JSON Schema when describing tool arguments.
Typical divergences that break strict validators downstream:

* ``type`` expressed as a list (``["string", "null"]``) — draft-07 style, but
  many servers only accept a single string. We flatten by dropping ``"null"``
  and, if only ``"null"`` remains, widen to ``"string"`` while setting
  ``nullable: true``.
* ``anyOf`` / ``oneOf`` unions used purely to express nullability — equivalent
  to ``nullable: true`` in the OpenAPI/3.0 dialect the stricter servers prefer.
* ``exclusiveMaximum`` / ``exclusiveMinimum`` as booleans (draft-04) while the
  schema also carries ``maximum`` / ``minimum`` numbers. Stricter servers
  interpret the boolean form as the number itself and reject the mix.
* ``$schema`` / ``$id`` / ``$comment`` — harmless but often rejected.
* ``format`` hints that are valid JSON Schema but not recognised by the model
  (e.g. ``"uuid"``, ``"date-time"``) — we keep common ones and drop the rest
  only when the server is known to reject unknown formats. We do NOT drop by
  default because they are useful to most modern providers.

The normalizer is intentionally conservative: it never *adds* new semantics;
it only *rewrites* equivalents into the most broadly-compatible form. Inputs
that are already compatible flow through unchanged.
"""

from __future__ import annotations

import copy
from typing import Any

_META_KEYS = ("$schema", "$id", "$comment")
_DEFINITION_KEYS = ("definitions", "$defs")

# Guard against deeply nested or recursive schemas: a pathological
# ``$ref``-expanded schema (or a hand-crafted hostile tool definition) could
# recurse thousands of levels deep and blow Python's stack. 64 levels is well
# above anything realistic for LLM tool arguments.
_MAX_SCHEMA_DEPTH = 64


def _copy_schema_tree(value: Any) -> Any:
    """Copy a JSON-like tree without consuming the Python call stack."""
    if not isinstance(value, (dict, list)):
        return copy.deepcopy(value)

    root: dict[Any, Any] | list[Any] = {} if isinstance(value, dict) else []
    memo: dict[int, dict[Any, Any] | list[Any]] = {id(value): root}
    pending: list[tuple[dict[Any, Any] | list[Any], dict[Any, Any] | list[Any]]] = [
        (value, root)
    ]
    while pending:
        source, target = pending.pop()
        items = source.items() if isinstance(source, dict) else enumerate(source)
        for key, item in items:
            if isinstance(item, (dict, list)):
                copied = memo.get(id(item))
                if copied is None:
                    copied = {} if isinstance(item, dict) else []
                    memo[id(item)] = copied
                    pending.append((item, copied))
            else:
                copied = copy.deepcopy(item)
            if isinstance(target, dict):
                target[copy.deepcopy(key)] = copied
            else:
                target.append(copied)
    return root


def _flatten_type_union(schema: dict[str, Any]) -> None:
    """Collapse ``{"type": ["string", "null"]}`` into the non-null variant."""
    t = schema.get("type")
    if not isinstance(t, list):
        return
    non_null = [item for item in t if item != "null"]
    has_null = "null" in t
    if len(non_null) == 1:
        schema["type"] = non_null[0]
        if has_null:
            schema["nullable"] = True
    elif not non_null and has_null:
        schema["type"] = "string"
        schema["nullable"] = True


def _collapse_nullable_any_of(schema: dict[str, Any]) -> None:
    """Rewrite ``{"anyOf": [<S>, {"type": "null"}]}`` as ``<S> + nullable``."""
    for key in ("anyOf", "oneOf"):
        candidates = schema.get(key)
        if not isinstance(candidates, list) or len(candidates) != 2:
            continue
        null_variant = next(
            (c for c in candidates if isinstance(c, dict) and c.get("type") == "null"),
            None,
        )
        other_variant = next(
            (c for c in candidates if c is not null_variant),
            None,
        )
        if null_variant is None or not isinstance(other_variant, dict):
            continue

        schema.pop(key)
        for subkey, subvalue in other_variant.items():
            if subkey not in schema:
                schema[subkey] = subvalue
        schema["nullable"] = True
        return


def _coerce_exclusive_bounds(schema: dict[str, Any]) -> None:
    """Convert draft-04 boolean bounds into draft-07 numeric bounds."""
    for bound_key, bool_key in (
        ("maximum", "exclusiveMaximum"),
        ("minimum", "exclusiveMinimum"),
    ):
        if isinstance(schema.get(bool_key), bool) and isinstance(schema.get(bound_key), (int, float)):
            if schema[bool_key]:
                schema[bool_key] = schema[bound_key]
                schema.pop(bound_key, None)
            else:
                schema.pop(bool_key, None)


def _schema_contains_ref(schema: Any) -> bool:
    stack: list[tuple[Any, int]] = [(schema, 0)]
    seen: set[int] = set()

    while stack:
        node, depth = stack.pop()
        if depth > _MAX_SCHEMA_DEPTH:
            continue
        if isinstance(node, dict):
            node_id = id(node)
            if node_id in seen:
                continue
            seen.add(node_id)
            if "$ref" in node:
                return True
            stack.extend((value, depth + 1) for value in node.values())
            continue

        if isinstance(node, list):
            node_id = id(node)
            if node_id in seen:
                continue
            seen.add(node_id)
            stack.extend((item, depth + 1) for item in node)

    return False


def _strip_meta(schema: dict[str, Any], *, preserve_definitions: bool) -> None:
    for meta in _META_KEYS:
        schema.pop(meta, None)
    if not preserve_definitions:
        for definition_key in _DEFINITION_KEYS:
            schema.pop(definition_key, None)


def _normalize_schema(schema: Any, depth: int = 0, preserve_definitions: bool | None = None) -> Any:
    """Recursively normalize a JSON Schema node in-place and return it."""
    if preserve_definitions is None:
        preserve_definitions = _schema_contains_ref(schema)
    if depth > _MAX_SCHEMA_DEPTH:
        return schema
    if isinstance(schema, list):
        return [_normalize_schema(item, depth + 1, preserve_definitions) for item in schema]
    if not isinstance(schema, dict):
        return schema

    _strip_meta(schema, preserve_definitions=preserve_definitions)
    _collapse_nullable_any_of(schema)
    _flatten_type_union(schema)
    _coerce_exclusive_bounds(schema)

    for sub_key in ("properties", "patternProperties", "definitions", "$defs"):
        sub = schema.get(sub_key)
        if isinstance(sub, dict):
            for name, child in list(sub.items()):
                sub[name] = _normalize_schema(child, depth + 1, preserve_definitions)

    items = schema.get("items")
    if isinstance(items, (dict, list)):
        schema["items"] = _normalize_schema(items, depth + 1, preserve_definitions)

    for combinator in ("allOf", "anyOf", "oneOf"):
        combined = schema.get(combinator)
        if isinstance(combined, list):
            schema[combinator] = [
                _normalize_schema(item, depth + 1, preserve_definitions)
                for item in combined
            ]

    additional = schema.get("additionalProperties")
    if isinstance(additional, dict):
        schema["additionalProperties"] = _normalize_schema(additional, depth + 1, preserve_definitions)

    return schema


def normalize_tool_schema(schema: Any) -> Any:
    """Return a deep copy of *schema* rewritten into a broadly compatible form.

    Passing a non-dict (or missing) value returns the input unchanged so
    call sites do not need guard clauses.
    """
    if schema is None:
        return None
    if not isinstance(schema, (dict, list)):
        return schema
    return _normalize_schema(_copy_schema_tree(schema))


def normalize_openai_tools(tools: Any) -> Any:
    """Normalize the ``tools`` array of an OpenAI chat-completions request."""
    if not isinstance(tools, list):
        return tools
    normalized: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized.append(tool)
            continue
        tool_copy = _copy_schema_tree(tool)
        function = tool_copy.get("function")
        if isinstance(function, dict):
            parameters = function.get("parameters")
            if isinstance(parameters, dict):
                function["parameters"] = _normalize_schema(parameters)
        normalized.append(tool_copy)
    return normalized


def normalize_anthropic_tools(tools: Any) -> Any:
    """Normalize the ``tools`` array of an Anthropic messages request."""
    if not isinstance(tools, list):
        return tools
    normalized: list[Any] = []
    for tool in tools:
        if not isinstance(tool, dict):
            normalized.append(tool)
            continue
        tool_copy = _copy_schema_tree(tool)
        input_schema = tool_copy.get("input_schema")
        if isinstance(input_schema, dict):
            tool_copy["input_schema"] = _normalize_schema(input_schema)
        normalized.append(tool_copy)
    return normalized
