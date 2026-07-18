import json
import unittest

from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat_dispatch import _finalize_chat_success_response
from llm_gateway_core.api.v1.chat_sanitizers import (
    _bracketed_json_slices,
    expects_json_object_response,
    extract_sanitized_json_object_content,
)
from tests._async_compat import run_async


class ExtractSanitizedJsonObjectContentBracketRecoveryTests(unittest.TestCase):
    def test_extract_sanitized_json_object_content_recovers_prose_wrapped_via_bracket_slice(self):
        text = 'Sure, here you go: {"a": 1, "b": 2} — let me know if that helps!'

        result = extract_sanitized_json_object_content(text)

        self.assertEqual(result, '{"a": 1, "b": 2}')
        self.assertEqual(json.loads(result), {"a": 1, "b": 2})

    def test_extract_sanitized_json_object_content_prefers_longest_slice(self):
        # A short "[...]" fragment and a longer "{...}" object both appear in
        # the text; _bracketed_json_slices must offer the longer candidate
        # first, and since only a dict-shaped candidate is accepted as a JSON
        # object payload, the object slice is the one recovery must return.
        text = 'result=[1,2] and full answer: {"a": 1, "b": {"c": 2}}'

        slices = _bracketed_json_slices(text)

        self.assertEqual(slices[0], '{"a": 1, "b": {"c": 2}}')
        self.assertGreater(len(slices[0]), len(slices[1]))

        result = extract_sanitized_json_object_content(text)

        self.assertEqual(result, '{"a": 1, "b": {"c": 2}}')


class JsonSchemaResponseFormatTests(unittest.TestCase):
    def test_json_schema_response_format_now_triggers_sanitization(self):
        request_body_json = {
            "response_format": {
                "type": "json_schema",
                "json_schema": {"name": "answer", "schema": {"type": "object"}},
            }
        }

        self.assertTrue(expects_json_object_response(request_body_json))


def _completion_response(content: str) -> dict:
    return {
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": content},
            }
        ]
    }


def _json_schema_request() -> dict:
    return {
        "response_format": {
            "type": "json_schema",
            "json_schema": {"name": "answer", "schema": {"type": "object"}},
        }
    }


class JsonSchemaThinkStripGuaranteeTests(unittest.TestCase):
    """json_schema requests received unconditional think-tag stripping before
    Package D widened JSON_OBJECT_RESPONSE_FORMAT_TYPES to cover them (they hit
    the ``elif strip_think_tags`` branch of _finalize_chat_success_response).
    That guarantee must survive for content the JSON sanitizer cannot heal."""

    def test_json_schema_with_strip_think_tags_strips_tags_when_json_extraction_fails(self):
        response = _completion_response("<think>secret reasoning</think>no json here at all")

        result = _finalize_chat_success_response(
            response,
            "gateway-model",
            _json_schema_request(),
            strip_think_tags=True,
        )

        content = result["choices"][0]["message"]["content"]
        self.assertNotIn("<think>", content)
        self.assertNotIn("secret reasoning", content)
        self.assertIn("no json here at all", content)

    def test_json_object_with_strip_think_tags_keeps_historical_no_strip_semantics(self):
        # json_object was already covered by the JSON sanitizer before Package D,
        # so think-strip never ran for it on extraction failure — keep that as-is.
        original_content = "<think>secret reasoning</think>no json here at all"
        response = _completion_response(original_content)

        result = _finalize_chat_success_response(
            response,
            "gateway-model",
            {"response_format": {"type": "json_object"}},
            strip_think_tags=True,
        )

        self.assertEqual(result["choices"][0]["message"]["content"], original_content)

    def test_json_schema_streaming_strips_think_tags_via_delta_sanitizer(self):
        # Characterization: on the streaming path the guarantee is provided by
        # _sanitize_openai_json_object_stream itself, whose delta sanitizer
        # (_sanitize_json_object_stream_delta) strips think blocks
        # unconditionally — no strip_think_tags gate exists or is needed in
        # _finalize_chat_success_response's streaming branch. This test locks
        # that guarantee for json_schema streams against future refactors.
        chunks = [
            {"choices": [{"delta": {"content": "<think>secret"}}]},
            {"choices": [{"delta": {"content": " reasoning</think>no json"}}]},
            {"choices": [{"delta": {"content": " here at all"}, "finish_reason": "stop"}]},
        ]

        async def source():
            for chunk in chunks:
                yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            yield b"data: [DONE]\n\n"

        response = StreamingResponse(source(), media_type="text/event-stream")
        sanitized = _finalize_chat_success_response(
            response,
            "gateway-model",
            _json_schema_request(),
            strip_think_tags=True,
        )

        async def collect() -> bytes:
            return b"".join([chunk async for chunk in sanitized.body_iterator])

        raw = run_async(collect()).decode("utf-8")
        self.assertNotIn("secret reasoning", raw)
        self.assertIn("no json", raw)


if __name__ == "__main__":
    unittest.main()
