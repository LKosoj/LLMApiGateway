import copy
import unittest

from llm_gateway_core.services.error_classifier import _normalize_provider_attempt_payload


def _tool_call() -> dict:
    return {
        "id": "call_1",
        "type": "function",
        "function": {"name": "run_sql", "arguments": "{}"},
    }


class EmptyTextContentBlockNormalizationTests(unittest.TestCase):
    """api.kimi.com rejects ``{"type": "text", "text": ""}`` with
    ``400 text content is empty``; clients that always normalize content into
    OpenAI parts produce exactly that for a tool-call-only assistant turn.
    """

    def test_tool_call_only_assistant_message_loses_its_empty_content(self):
        payload = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "run it"}]},
                {
                    "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "tool_calls": [_tool_call()],
                },
            ]
        }

        _normalize_provider_attempt_payload(payload, provider_model="k3")

        assistant_message = payload["messages"][1]
        self.assertNotIn("content", assistant_message)
        self.assertEqual(assistant_message["tool_calls"], [_tool_call()])
        self.assertEqual(payload["messages"][0]["content"], [{"type": "text", "text": "run it"}])

    def test_empty_text_block_is_dropped_but_other_blocks_survive(self):
        payload = {
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": ""},
                        {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                        {"type": "text", "text": "what is this?"},
                    ],
                }
            ]
        }

        _normalize_provider_attempt_payload(payload, provider_model="k3")

        self.assertEqual(
            payload["messages"][0]["content"],
            [
                {"type": "image_url", "image_url": {"url": "https://example.test/a.png"}},
                {"type": "text", "text": "what is this?"},
            ],
        )

    def test_message_without_tool_calls_keeps_its_only_empty_block(self):
        # Removing the content here would turn a malformed request into a
        # silently different one; the provider must be free to reject it.
        payload = {"messages": [{"role": "user", "content": [{"type": "text", "text": ""}]}]}
        expected = copy.deepcopy(payload["messages"])

        _normalize_provider_attempt_payload(payload, provider_model="k3")

        self.assertEqual(payload["messages"], expected)

    def test_whitespace_only_and_string_content_are_left_untouched(self):
        # Whitespace-only text blocks are accepted upstream, and a plain string
        # content is a different shape that this normalization does not own.
        payload = {
            "messages": [
                {"role": "user", "content": [{"type": "text", "text": "   \n "}]},
                {"role": "assistant", "content": "", "tool_calls": [_tool_call()]},
                {"role": "tool", "tool_call_id": "call_1", "content": ""},
            ]
        }
        expected = copy.deepcopy(payload["messages"])

        _normalize_provider_attempt_payload(payload, provider_model="k3")

        self.assertEqual(payload["messages"], expected)

    def test_payload_without_messages_is_not_touched(self):
        payload = {"model": "k3", "messages": "not-a-list"}

        _normalize_provider_attempt_payload(payload, provider_model="k3")

        self.assertEqual(payload["messages"], "not-a-list")


if __name__ == "__main__":
    unittest.main()
