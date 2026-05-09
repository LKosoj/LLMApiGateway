"""Unit tests for the tool-schema compatibility shim."""

from __future__ import annotations

import unittest

from llm_gateway_core.services.tool_schema_normalizer import (
    _MAX_SCHEMA_DEPTH,
    _normalize_schema,
    normalize_anthropic_tools,
    normalize_openai_tools,
    normalize_tool_schema,
)


class NormalizeTypeUnionTests(unittest.TestCase):
    def test_flattens_nullable_union(self):
        out = normalize_tool_schema({"type": ["string", "null"]})
        self.assertEqual(out["type"], "string")
        self.assertTrue(out["nullable"])

    def test_all_nulls_widen_to_string(self):
        out = normalize_tool_schema({"type": ["null"]})
        self.assertEqual(out["type"], "string")
        self.assertTrue(out["nullable"])

    def test_non_null_union_is_preserved(self):
        out = normalize_tool_schema({"type": ["string", "number"]})
        self.assertEqual(out["type"], ["string", "number"])


class NormalizeAnyOfNullableTests(unittest.TestCase):
    def test_anyOf_nullable_collapses(self):
        out = normalize_tool_schema(
            {"anyOf": [{"type": "string"}, {"type": "null"}]}
        )
        self.assertNotIn("anyOf", out)
        self.assertEqual(out["type"], "string")
        self.assertTrue(out["nullable"])

    def test_oneOf_nullable_collapses(self):
        out = normalize_tool_schema(
            {"oneOf": [{"type": "integer"}, {"type": "null"}]}
        )
        self.assertEqual(out["type"], "integer")
        self.assertTrue(out["nullable"])

    def test_multi_variant_anyOf_is_preserved(self):
        schema = {"anyOf": [{"type": "integer"}, {"type": "string"}]}
        out = normalize_tool_schema(schema)
        self.assertIn("anyOf", out)
        self.assertEqual(len(out["anyOf"]), 2)


class NormalizeExclusiveBoundsTests(unittest.TestCase):
    def test_boolean_exclusive_maximum_converted_to_numeric(self):
        out = normalize_tool_schema(
            {"type": "integer", "maximum": 10, "exclusiveMaximum": True}
        )
        self.assertEqual(out["exclusiveMaximum"], 10)
        self.assertNotIn("maximum", out)

    def test_false_boolean_bound_is_dropped(self):
        out = normalize_tool_schema(
            {"type": "integer", "maximum": 10, "exclusiveMaximum": False}
        )
        self.assertEqual(out["maximum"], 10)
        self.assertNotIn("exclusiveMaximum", out)


class NormalizeMetaKeysTests(unittest.TestCase):
    def test_meta_keys_are_stripped(self):
        out = normalize_tool_schema(
            {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "$id": "x",
                "$comment": "hello",
                "type": "object",
            }
        )
        for meta in ("$schema", "$id", "$comment"):
            self.assertNotIn(meta, out)
        self.assertEqual(out["type"], "object")


class RecursiveNormalizationTests(unittest.TestCase):
    def test_nested_properties_are_normalized(self):
        schema = {
            "type": "object",
            "properties": {
                "value": {"type": ["string", "null"]},
                "inner": {
                    "type": "object",
                    "properties": {
                        "count": {"anyOf": [{"type": "integer"}, {"type": "null"}]}
                    },
                },
            },
        }
        out = normalize_tool_schema(schema)
        self.assertEqual(out["properties"]["value"]["type"], "string")
        self.assertTrue(out["properties"]["value"]["nullable"])
        inner = out["properties"]["inner"]["properties"]["count"]
        self.assertEqual(inner["type"], "integer")
        self.assertTrue(inner["nullable"])

    def test_array_items_are_normalized(self):
        schema = {
            "type": "array",
            "items": {"type": ["string", "null"]},
        }
        out = normalize_tool_schema(schema)
        self.assertEqual(out["items"]["type"], "string")

    def test_does_not_mutate_input(self):
        original = {"type": ["string", "null"]}
        copy_before = dict(original)
        normalize_tool_schema(original)
        self.assertEqual(original, copy_before)


class NormalizeOpenAIToolsTests(unittest.TestCase):
    def test_openai_tool_parameters_normalized(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {
                        "$schema": "http://json-schema.org/draft-07/schema#",
                        "type": "object",
                        "properties": {
                            "city": {"type": ["string", "null"]},
                        },
                    },
                },
            }
        ]
        out = normalize_openai_tools(tools)
        params = out[0]["function"]["parameters"]
        self.assertNotIn("$schema", params)
        self.assertEqual(params["properties"]["city"]["type"], "string")
        self.assertTrue(params["properties"]["city"]["nullable"])

    def test_non_dict_tool_items_pass_through(self):
        tools = ["not-a-tool", {"type": "function", "function": "not-a-dict"}]
        out = normalize_openai_tools(tools)
        self.assertEqual(out, tools)

    def test_non_list_returns_input(self):
        self.assertEqual(normalize_openai_tools(None), None)


class NormalizeAnthropicToolsTests(unittest.TestCase):
    def test_anthropic_input_schema_normalized(self):
        tools = [
            {
                "name": "get_weather",
                "input_schema": {
                    "type": "object",
                    "properties": {
                        "city": {"anyOf": [{"type": "string"}, {"type": "null"}]},
                    },
                },
            }
        ]
        out = normalize_anthropic_tools(tools)
        schema = out[0]["input_schema"]
        city = schema["properties"]["city"]
        self.assertEqual(city["type"], "string")
        self.assertTrue(city["nullable"])


class NormalizeDepthLimitTests(unittest.TestCase):
    def test_depth_guard_stops_recursion_past_limit(self):
        """A schema deeper than ``_MAX_SCHEMA_DEPTH`` must not blow the stack.

        We call ``_normalize_schema`` directly to isolate the normalizer's
        own recursion from the ``copy.deepcopy`` in ``normalize_tool_schema``
        (``deepcopy`` has its own Python-level recursion limit that is a
        separate concern from the normalizer's depth guard).
        """
        depth = _MAX_SCHEMA_DEPTH * 4
        schema: dict = {"type": "object"}
        node = schema
        for _ in range(depth):
            child: dict = {"type": "object"}
            node["items"] = child
            node = child
        # Must not raise ``RecursionError``.
        out = _normalize_schema(schema)
        self.assertIsInstance(out, dict)


if __name__ == "__main__":
    unittest.main()
