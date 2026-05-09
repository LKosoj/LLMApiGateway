"""Tests verifying correctness of deepcopy optimizations.

Ensures that:
1. sanitize_payload produces a new object (so deepcopy before it is redundant).
2. _extract_stream_template extracts only needed scalar fields.
3. _build_openai_stream_delta_chunk works with extracted template.
4. redact_payload_for_log still produces an independent copy.
5. make_llm_request does not mutate the incoming payload.
"""

import copy

import httpx

from llm_gateway_core.utils.text_sanitize import sanitize_payload
from llm_gateway_core.utils.log_redaction import redact_payload_for_log
from llm_gateway_core.api.v1.chat import (
    _extract_stream_template,
    _build_openai_stream_delta_chunk,
)
from tests._async_compat import run_async


class TestSanitizePayloadCopies:
    def test_sanitize_returns_new_dict(self):
        original = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi"}]}
        result = sanitize_payload(original)
        assert result is not original
        assert result["messages"] is not original["messages"]
        assert result["messages"][0] is not original["messages"][0]

    def test_sanitize_does_not_mutate_original(self):
        original = {"model": "gpt-4", "messages": [{"role": "user", "content": "hi 😀"}]}
        original_copy = copy.deepcopy(original)
        sanitize_payload(original)
        assert original == original_copy

    def test_sanitize_handles_surrogates(self):
        text_with_surrogate = "hello \ud800 world"
        result = sanitize_payload({"text": text_with_surrogate})
        assert isinstance(result["text"], str)
        assert "\ud800" not in result["text"]


class TestExtractStreamTemplate:
    def test_extracts_scalar_fields(self):
        chunk = {
            "id": "chatcmpl-abc",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
            "system_fingerprint": "fp_123",
            "choices": [{"index": 0, "delta": {"content": "Hello"}}],
        }
        template = _extract_stream_template(chunk)
        assert template == {
            "id": "chatcmpl-abc",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
            "system_fingerprint": "fp_123",
        }
        assert "choices" not in template

    def test_missing_fields_omitted(self):
        chunk = {"id": "chatcmpl-abc", "choices": []}
        template = _extract_stream_template(chunk)
        assert template == {"id": "chatcmpl-abc"}

    def test_empty_chunk(self):
        assert _extract_stream_template({}) == {}


class TestBuildStreamDeltaChunkWithTemplate:
    def test_builds_synthetic_chunk_with_extracted_template(self):
        template = {
            "id": "chatcmpl-abc",
            "object": "chat.completion.chunk",
            "created": 1700000000,
            "model": "gpt-4",
        }
        choice = {"index": 0}
        result = _build_openai_stream_delta_chunk(template, choice, 0, "Hello")
        assert result["id"] == "chatcmpl-abc"
        assert result["model"] == "gpt-4"
        assert result["choices"][0]["delta"]["content"] == "Hello"


class TestRedactPayloadNoCopy:
    def test_redact_returns_independent_dict(self):
        original = {"model": "gpt-4", "temperature": 0.7}
        redacted = redact_payload_for_log(original)
        redacted["model"] = "changed"
        assert original["model"] == "gpt-4"

    def test_redact_strips_messages(self):
        payload = {"model": "gpt-4", "messages": [{"role": "user", "content": "secret"}]}
        redacted = redact_payload_for_log(payload)
        assert "messages" not in redacted

    def test_redact_leaf_values_unchanged(self):
        payload = {"model": "gpt-4", "temperature": 0.7, "stream": True}
        redacted = redact_payload_for_log(payload)
        assert redacted["model"] == "gpt-4"
        assert redacted["temperature"] == 0.7
        assert redacted["stream"] is True


class TestMakeLlmRequestPayloadIsolation:
    def test_make_llm_request_does_not_mutate_input_payload(self):
        """Verify make_llm_request does not mutate the caller's payload dict."""
        from llm_gateway_core.services.request_handler import make_llm_request

        original_payload = {
            "model": "gpt-4",
            "messages": [{"role": "user", "content": "hello \ud800"}],
            "stream": False,
        }
        payload_snapshot = copy.deepcopy(original_payload)

        # Create a mock client that returns a 500 error
        mock_response = httpx.Response(
            status_code=500,
            text="test error",
            request=httpx.Request("POST", "http://test"),
        )

        class MockClient:
            async def post(self, url, **kwargs):
                return mock_response

        result, error = run_async(
            make_llm_request(MockClient(), "http://test/v1/chat/completions", {}, original_payload, False)
        )

        # Payload should be untouched
        assert original_payload == payload_snapshot
