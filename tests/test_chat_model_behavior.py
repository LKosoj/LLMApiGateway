import unittest

from llm_gateway_core.api.v1.chat_model_behavior import (
    ModelBehaviorFailureDetail,
    detect_degenerate_non_stream_response,
)


class OpenAiDegenerateCompletionTests(unittest.TestCase):
    def test_openai_empty_message_no_tool_calls_is_empty_completion(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")

    def test_openai_tool_calls_with_null_content_is_not_degenerate(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": "call_1",
                                "type": "function",
                                "function": {"name": "lookup", "arguments": "{}"},
                            }
                        ],
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_finish_reason_tool_calls_is_not_degenerate(self):
        # No tool_calls array present, but finish_reason already says
        # "tool_calls": diagnosing/repairing that dialect mismatch is
        # Package E's job, not Package D's.
        response_data = {
            "choices": [
                {
                    "finish_reason": "tool_calls",
                    "message": {"role": "assistant", "content": None},
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_length_finish_reason_empty_content_is_empty_completion(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {"role": "assistant", "content": ""},
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")

    def test_openai_length_finish_reason_with_partial_content_is_not_degenerate(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "length",
                    "message": {
                        "role": "assistant",
                        "content": "Some partial text that got cut off",
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_refusal_field_is_not_degenerate(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "refusal": "I can't help with that request.",
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_missing_choices_is_empty_completion(self):
        for response_data in ({"id": "resp-no-choices"}, {"choices": []}):
            with self.subTest(response_data=response_data):
                result = detect_degenerate_non_stream_response(
                    response_data, {}, is_anthropic_provider=False
                )

                self.assertIsInstance(result, ModelBehaviorFailureDetail)
                self.assertEqual(result.behavior_class, "empty_completion")

    def test_openai_multimodal_content_array_only_text_parts_counted(self):
        # Only an image_url part, no "text" part: if non-text parts were
        # (incorrectly) counted as content, this would look non-empty.
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": [
                            {
                                "type": "image_url",
                                "image_url": {"url": "https://example.com/pic.png"},
                            }
                        ],
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")

    def test_openai_n_greater_than_one_only_checks_first_choice(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "First choice has real text."},
                },
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": ""},
                },
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_bracket_recoverable_json_is_not_degenerate(self):
        request_body_json = {"response_format": {"type": "json_object"}}
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": 'Sure! Here is the JSON: {"key": "value"} Hope that helps.',
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, request_body_json, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_no_json_at_all_with_json_format_is_format_ignored(self):
        request_body_json = {"response_format": {"type": "json_object"}}
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "I'm not going to provide that, sorry.",
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, request_body_json, is_anthropic_provider=False
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "format_ignored")

    def test_openai_reasoning_content_without_content_is_not_degenerate(self):
        # Thinking models split the answer into "reasoning_content" (chain of
        # thought) and "content" (the actual answer). A non-empty
        # reasoning_content with an empty content field is a known, benign
        # pattern for these models, not a degenerate response.
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "reasoning_content": "Let me think about this step by step...",
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_legacy_function_call_with_empty_content_is_not_degenerate(self):
        response_data = {
            "choices": [
                {
                    "finish_reason": "function_call",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "function_call": {"name": "lookup", "arguments": "{}"},
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=False
        )

        self.assertIsNone(result)

    def test_openai_reasoning_content_does_not_mask_missing_json_when_format_requested(self):
        # reasoning_content only rescues the empty_completion verdict; it
        # must not mask a JSON-mode request that produced no JSON, since the
        # client only ever sees "content", never the reasoning trace.
        request_body_json = {"response_format": {"type": "json_object"}}
        response_data = {
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {
                        "role": "assistant",
                        "content": "",
                        "reasoning_content": "Thinking about the JSON shape...",
                    },
                }
            ]
        }

        result = detect_degenerate_non_stream_response(
            response_data, request_body_json, is_anthropic_provider=False
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")


class AnthropicDegenerateCompletionTests(unittest.TestCase):
    def test_anthropic_empty_content_blocks_is_empty_completion(self):
        response_data = {"content": [], "stop_reason": "end_turn"}

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=True
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")

    def test_anthropic_tool_use_block_is_not_degenerate(self):
        response_data = {
            "content": [
                {"type": "tool_use", "id": "toolu_1", "name": "lookup", "input": {}}
            ],
            "stop_reason": "tool_use",
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=True
        )

        self.assertIsNone(result)

    def test_anthropic_thinking_block_without_text_is_not_degenerate(self):
        response_data = {
            "content": [
                {"type": "thinking", "thinking": "Working through the problem..."},
            ],
            "stop_reason": "end_turn",
        }

        result = detect_degenerate_non_stream_response(
            response_data, {}, is_anthropic_provider=True
        )

        self.assertIsNone(result)

    def test_anthropic_thinking_block_does_not_mask_missing_json_when_format_requested(self):
        request_body_json = {"response_format": {"type": "json_object"}}
        response_data = {
            "content": [
                {"type": "thinking", "thinking": "Thinking about the JSON shape..."},
            ],
            "stop_reason": "end_turn",
        }

        result = detect_degenerate_non_stream_response(
            response_data, request_body_json, is_anthropic_provider=True
        )

        self.assertIsInstance(result, ModelBehaviorFailureDetail)
        self.assertEqual(result.behavior_class, "empty_completion")


class ModelBehaviorFailureDetailTests(unittest.TestCase):
    def test_model_behavior_detail_rejects_unknown_class(self):
        with self.assertRaises(ValueError):
            ModelBehaviorFailureDetail("bad", behavior_class="not_a_registered_class")


if __name__ == "__main__":
    unittest.main()
