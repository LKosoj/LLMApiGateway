import unittest

from llm_gateway_core.services.tool_call_rescue import (
    DIALECT_MARKERS,
    RescueResult,
    build_tool_schema_map,
    could_become_dialect_marker,
    extract_balanced_json,
    rescue_inline_tool_calls,
)


WEATHER_SCHEMA_MAP = {
    "get_weather": {
        "type": "object",
        "properties": {"location": {"type": "string"}},
    }
}


class ExtractBalancedJsonTests(unittest.TestCase):
    def test_extracts_simple_object(self):
        text = '{"a": 1}tail'
        result = extract_balanced_json(text, 0)
        self.assertEqual(result, ('{"a": 1}', 8))

    def test_extracts_nested_object(self):
        text = '{"a": {"b": {"c": 1}}, "d": 2}tail'
        result = extract_balanced_json(text, 0)
        self.assertIsNotNone(result)
        json_text, end = result
        self.assertEqual(json_text, '{"a": {"b": {"c": 1}}, "d": 2}')
        self.assertEqual(text[end:], "tail")

    def test_braces_inside_strings_do_not_affect_nesting(self):
        text = '{"note": "use { and } inside a string"}tail'
        result = extract_balanced_json(text, 0)
        self.assertIsNotNone(result)
        json_text, end = result
        self.assertEqual(json_text, text[:-4])
        self.assertEqual(text[end:], "tail")

    def test_escaped_quote_inside_string_does_not_end_string_early(self):
        text = r'{"note": "she said \"hi } there\""}tail'
        result = extract_balanced_json(text, 0)
        self.assertIsNotNone(result)
        json_text, end = result
        self.assertEqual(text[end:], "tail")
        # The extracted slice must be valid, balanced JSON on its own.
        import json as _json

        self.assertEqual(_json.loads(json_text), {"note": 'she said "hi } there"'})

    def test_returns_none_for_unterminated_object(self):
        text = '{"a": 1'
        self.assertIsNone(extract_balanced_json(text, 0))

    def test_returns_none_when_start_is_not_a_brace(self):
        text = 'not json'
        self.assertIsNone(extract_balanced_json(text, 0))

    def test_start_offset_is_respected(self):
        text = 'prefix {"a": 1} suffix'
        result = extract_balanced_json(text, 7)
        self.assertEqual(result, ('{"a": 1}', 15))


class CouldBecomeDialectMarkerTests(unittest.TestCase):
    def test_empty_text_could_become_any_marker(self):
        self.assertTrue(could_become_dialect_marker(""))

    def test_whitespace_only_could_become_any_marker(self):
        self.assertTrue(could_become_dialect_marker("   \n"))

    def test_prefix_of_kimi_marker(self):
        self.assertTrue(could_become_dialect_marker("<|tool_calls"))

    def test_prefix_of_function_tag_marker(self):
        self.assertTrue(could_become_dialect_marker("<function"))

    def test_full_marker_is_its_own_prefix(self):
        self.assertTrue(could_become_dialect_marker("<tool_call>"))

    def test_diverging_text_is_not_a_prefix(self):
        self.assertFalse(could_become_dialect_marker("Sure, here is the answer"))

    def test_all_declared_markers_are_self_prefixes(self):
        for marker in DIALECT_MARKERS:
            self.assertTrue(could_become_dialect_marker(marker))


class BuildToolSchemaMapTests(unittest.TestCase):
    def test_builds_map_from_openai_tools(self):
        tools = [
            {
                "type": "function",
                "function": {
                    "name": "get_weather",
                    "parameters": {"type": "object", "properties": {"location": {"type": "string"}}},
                },
            }
        ]
        schema_map = build_tool_schema_map(tools)
        self.assertEqual(
            schema_map,
            {"get_weather": {"type": "object", "properties": {"location": {"type": "string"}}}},
        )

    def test_non_list_input_returns_empty_map(self):
        self.assertEqual(build_tool_schema_map(None), {})
        self.assertEqual(build_tool_schema_map("not a list"), {})

    def test_malformed_tool_entries_are_skipped(self):
        tools = [{"type": "function"}, "not a dict", {"function": {"parameters": {}}}]
        self.assertEqual(build_tool_schema_map(tools), {})


class KimiDialectTests(unittest.TestCase):
    def test_single_tool_call(self):
        content = (
            "<|tool_calls_section_begin|>"
            "<|tool_call_begin|>functions.get_weather:0<|tool_call_argument_begin|>"
            '{"location": "Paris"}'
            "<|tool_call_end|>"
            "<|tool_calls_section_end|>"
        )
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.dialect, "kimi")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Paris"}')
        self.assertIsNone(result.cleaned_text)

    def test_multiple_tool_calls_and_surrounding_text_preserved(self):
        content = (
            "Sure, let me check that.\n"
            "<|tool_calls_section_begin|>"
            "<|tool_call_begin|>functions.get_weather:0<|tool_call_argument_begin|>"
            '{"location": "Paris"}'
            "<|tool_call_end|>"
            "<|tool_call_begin|>functions.get_weather:1<|tool_call_argument_begin|>"
            '{"location": "Berlin"}'
            "<|tool_call_end|>"
            "<|tool_calls_section_end|>"
        )
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(len(result.tool_calls), 2)
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Paris"}')
        self.assertEqual(result.tool_calls[1].arguments, '{"location": "Berlin"}')
        self.assertEqual(result.cleaned_text, "Sure, let me check that.")

    def test_marker_present_but_unterminated_section_fails(self):
        content = "<|tool_calls_section_begin|>garbage, no end marker"
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertTrue(result.failed)
        self.assertEqual(result.tool_calls, [])

    def test_marker_present_but_missing_argument_begin_fails(self):
        content = (
            "<|tool_calls_section_begin|>"
            "<|tool_call_begin|>functions.get_weather:0"
            "<|tool_calls_section_end|>"
        )
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertTrue(result.failed)

    def test_no_marker_returns_empty_non_failed_result(self):
        result = rescue_inline_tool_calls("just a normal reply", WEATHER_SCHEMA_MAP)
        self.assertEqual(result, RescueResult())


class FunctionTagDialectTests(unittest.TestCase):
    def test_well_formed_function_tag(self):
        content = '<function=get_weather>{"location": "Paris"}</function>'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.dialect, "function_tag")
        self.assertEqual(len(result.tool_calls), 1)
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Paris"}')
        self.assertIsNone(result.cleaned_text)

    def test_malformed_variant_missing_closing_angle_bracket(self):
        content = '<function=get_weather{"location": "Paris"}</function>'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Paris"}')

    def test_prose_around_tag_preserved_as_cleaned_text(self):
        content = 'Checking now: <function=get_weather>{"location": "Paris"}</function> done.'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertEqual(result.cleaned_text, "Checking now:  done.")

    def test_marker_present_but_missing_closing_tag_fails(self):
        content = '<function=get_weather>{"location": "Paris"}'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertTrue(result.failed)


class ToolCallTagDialectTests(unittest.TestCase):
    def test_arguments_key(self):
        content = '<tool_call>{"name": "get_weather", "arguments": {"location": "Paris"}}</tool_call>'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.dialect, "tool_call_tag")
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Paris"}')

    def test_parameters_key(self):
        content = '<tool_call>{"name": "get_weather", "parameters": {"location": "Berlin"}}</tool_call>'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.tool_calls[0].arguments, '{"location": "Berlin"}')

    def test_marker_present_but_invalid_json_fails(self):
        content = "<tool_call>not json at all</tool_call>"
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertTrue(result.failed)

    def test_marker_present_but_missing_name_fails(self):
        content = '<tool_call>{"arguments": {"location": "Paris"}}</tool_call>'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertTrue(result.failed)


class BareJsonDialectTests(unittest.TestCase):
    def test_fenced_json_matching_declared_tool_is_rescued(self):
        content = '```json\n{"name": "get_weather", "arguments": {"location": "Paris"}}\n```'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.dialect, "bare_json")
        self.assertEqual(result.tool_calls[0].name, "get_weather")
        self.assertIsNone(result.cleaned_text)

    def test_bare_unfenced_json_matching_declared_tool_is_rescued(self):
        content = '{"name": "get_weather", "arguments": {"location": "Paris"}}'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertFalse(result.failed)
        self.assertEqual(result.tool_calls[0].name, "get_weather")

    def test_name_not_in_schema_map_is_left_untouched(self):
        content = '{"name": "unknown_tool", "arguments": {}}'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertEqual(result, RescueResult())

    def test_extra_prose_around_json_is_not_treated_as_bare_dialect(self):
        content = 'Sure: {"name": "get_weather", "arguments": {"location": "Paris"}} there you go.'
        result = rescue_inline_tool_calls(content, WEATHER_SCHEMA_MAP)
        self.assertEqual(result, RescueResult())

    def test_empty_schema_map_never_matches_bare_json(self):
        content = '{"name": "get_weather", "arguments": {"location": "Paris"}}'
        result = rescue_inline_tool_calls(content, {})
        self.assertEqual(result, RescueResult())


if __name__ == "__main__":
    unittest.main()
