import unittest

from llm_gateway_core.api.v1.chat import _openai_response_to_responses


def _build_response_with_content(content):
    return {
        "id": "resp-1",
        "created": 1735689600,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {
                    "role": "assistant",
                    "content": content,
                },
            }
        ],
        "usage": {"prompt_tokens": 1, "completion_tokens": 2, "total_tokens": 3},
    }


class OpenAiResponseToResponsesTextTests(unittest.TestCase):
    def test_list_text_content_is_concatenated_to_output_text_string(self):
        responses_payload = _openai_response_to_responses(
            _build_response_with_content(
                [
                    {"type": "text", "text": "a"},
                    {"type": "text", "text": "b"},
                ]
            ),
            "gateway-model",
        )

        output_content = responses_payload["output"][0]["content"]
        self.assertEqual(output_content[0]["type"], "output_text")
        self.assertEqual(output_content[0]["text"], "ab")
        self.assertIsInstance(output_content[0]["text"], str)

    def test_non_text_image_part_becomes_output_image_and_unknown_part_is_skipped(self):
        responses_payload = _openai_response_to_responses(
            _build_response_with_content(
                [
                    {"type": "text", "text": "caption"},
                    {
                        "type": "image_url",
                        "image_url": {"url": "https://example.com/image.png"},
                    },
                    {"type": "unsupported", "value": "ignored"},
                ]
            ),
            "gateway-model",
        )

        output_content = responses_payload["output"][0]["content"]
        self.assertEqual(
            output_content,
            [
                {"type": "output_text", "text": "caption", "annotations": []},
                {
                    "type": "output_image",
                    "image_url": {"url": "https://example.com/image.png"},
                },
            ],
        )


if __name__ == "__main__":
    unittest.main()
