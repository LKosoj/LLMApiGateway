import json
import unittest

from llm_gateway_core.services.tool_call_rescue import repair_tool_arguments


ARRAY_OBJECT_SCHEMA = {
    "type": "object",
    "properties": {
        "location": {"type": "string"},
        "tags": {"type": "array"},
        "options": {"type": "object"},
    },
}


class RepairToolArgumentsDoubleEncodingTests(unittest.TestCase):
    def test_unwraps_one_level_of_double_encoding(self):
        double_encoded = json.dumps(json.dumps({"location": "Paris"}))
        result = repair_tool_arguments(double_encoded, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(json.loads(result), {"location": "Paris"})

    def test_only_unwraps_one_level(self):
        # Triple-encoded: unwrapping once still leaves a JSON string, not a dict.
        triple_encoded = json.dumps(json.dumps(json.dumps({"location": "Paris"})))
        result = repair_tool_arguments(triple_encoded, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, triple_encoded)

    def test_string_wrapping_a_non_dict_is_left_unchanged(self):
        wrapped_list = json.dumps(json.dumps([1, 2, 3]))
        result = repair_tool_arguments(wrapped_list, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, wrapped_list)


class RepairToolArgumentsFieldCoercionTests(unittest.TestCase):
    def test_coerces_array_typed_string_field(self):
        arguments = json.dumps({"tags": '["a", "b"]'})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(json.loads(result), {"tags": ["a", "b"]})

    def test_coerces_object_typed_string_field(self):
        arguments = json.dumps({"options": '{"verbose": true}'})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(json.loads(result), {"options": {"verbose": True}})

    def test_string_typed_field_is_never_touched_even_if_it_looks_like_json(self):
        arguments = json.dumps({"location": '{"city": "Paris"}'})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)

    def test_mismatched_declared_type_is_not_coerced(self):
        # "tags" is declared as array but the string parses to an object.
        arguments = json.dumps({"tags": '{"not": "a list"}'})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)

    def test_unparseable_field_string_is_left_untouched(self):
        arguments = json.dumps({"tags": "not valid json"})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)

    def test_coercion_does_not_recurse_past_top_level(self):
        # "tags" top-level value is a JSON-encoded array containing one
        # element that is itself a string which merely looks like more JSON.
        # Only the top-level "tags" field participates in coercion; its
        # inner element must not be independently parsed.
        nested_schema = {
            "type": "object",
            "properties": {"tags": {"type": "array"}},
        }
        arguments = json.dumps({"tags": json.dumps(["nested-looking-string: [1,2,3]"])})
        result = repair_tool_arguments(arguments, nested_schema)
        self.assertEqual(json.loads(result), {"tags": ["nested-looking-string: [1,2,3]"]})


class RepairToolArgumentsPassthroughTests(unittest.TestCase):
    def test_invalid_json_is_returned_unchanged(self):
        arguments = "{not valid json"
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)

    def test_non_string_input_is_returned_unchanged(self):
        self.assertEqual(repair_tool_arguments(None, ARRAY_OBJECT_SCHEMA), None)

    def test_top_level_non_dict_json_is_returned_unchanged(self):
        arguments = json.dumps([1, 2, 3])
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)

    def test_missing_schema_leaves_arguments_unchanged(self):
        arguments = json.dumps({"tags": '["a", "b"]'})
        result = repair_tool_arguments(arguments, None)
        self.assertEqual(result, arguments)

    def test_no_changes_needed_returns_original_string(self):
        arguments = json.dumps({"location": "Paris"})
        result = repair_tool_arguments(arguments, ARRAY_OBJECT_SCHEMA)
        self.assertEqual(result, arguments)


if __name__ == "__main__":
    unittest.main()
