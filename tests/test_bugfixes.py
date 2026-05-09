"""Tests for bug fixes identified during code review."""

import os
import unittest
from unittest.mock import patch

from tests._async_compat import run_async
from llm_gateway_core.utils.usage_tracking import extract_tokens_usage


class ReasoningCompletionConventionTests(unittest.TestCase):
    """completion_tokens stores provider total; reasoning_tokens is separate."""

    def test_reasoning_tokens_less_than_completion_tokens(self):
        """Standard case: reasoning included in completion count (OpenAI style)."""
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
                "completion_tokens_details": {"reasoning_tokens": 20},
            }
        }
        result = extract_tokens_usage(payload)
        self.assertEqual(result["reasoning_tokens"], 20)
        self.assertEqual(result["completion_tokens"], 50)

    def test_reasoning_tokens_equal_to_completion_tokens(self):
        """Edge case: all completion tokens are reasoning."""
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
                "completion_tokens_details": {"reasoning_tokens": 20},
            }
        }
        result = extract_tokens_usage(payload)
        self.assertEqual(result["reasoning_tokens"], 20)
        self.assertEqual(result["completion_tokens"], 20)

    def test_reasoning_tokens_greater_than_completion_tokens(self):
        """Bug case: provider reports reasoning_tokens separately, not included in completion."""
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 10,
                "total_tokens": 130,
                "completion_tokens_details": {"reasoning_tokens": 20},
            }
        }
        result = extract_tokens_usage(payload)
        self.assertEqual(result["reasoning_tokens"], 20)
        self.assertEqual(result["completion_tokens"], 10)

    def test_zero_reasoning_tokens(self):
        """No reasoning tokens — completion_tokens untouched."""
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 50,
                "total_tokens": 150,
            }
        }
        result = extract_tokens_usage(payload)
        self.assertEqual(result["reasoning_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 50)


class ResponsesStreamJsonParsingTests(unittest.TestCase):
    """Fix: _openai_stream_to_responses must handle malformed JSON gracefully."""

    def test_malformed_json_in_responses_stream_does_not_crash(self):
        from llm_gateway_core.api.v1.chat import _openai_stream_to_responses
        from fastapi.responses import StreamingResponse

        # Simulate a stream with one valid chunk and one malformed chunk
        async def mock_body_iterator():
            yield b"data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"delta\":{\"content\":\"hi\"},\"index\":0}]}\n\n"
            yield b"data: {MALFORMED JSON}\n\n"
            yield b"data: [DONE]\n\n"

        mock_response = StreamingResponse(
            mock_body_iterator(),
            media_type="text/event-stream",
        )

        result = _openai_stream_to_responses(mock_response, "test-model")

        # Consuming the stream should not raise
        async def consume():
            chunks = []
            async for chunk in result.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = run_async(consume())
        # Should have at least the completed response event
        self.assertTrue(len(chunks) > 0)


class StreamBufferRemainderTests(unittest.TestCase):
    """Fix: stream converters must process data remaining in the buffer after the iterator ends."""

    def test_anthropic_stream_processes_final_buffer(self):
        from llm_gateway_core.api.v1.chat import _openai_stream_to_anthropic
        from fastapi import Request
        from fastapi.responses import StreamingResponse

        # Simulate a stream where usage arrives in the final chunk WITHOUT trailing newline
        async def mock_body_iterator():
            yield b"data: {\"id\":\"chatcmpl-1\",\"choices\":[{\"delta\":{\"content\":\"hello\"},\"index\":0}]}\n\n"
            # Final chunk with usage but no trailing \n — stays in buffer
            yield b"data: {\"id\":\"chatcmpl-1\",\"choices\":[],\"usage\":{\"prompt_tokens\":10,\"completion_tokens\":5}}"

        mock_response = StreamingResponse(
            mock_body_iterator(),
            media_type="text/event-stream",
        )

        # Create a minimal Request-like object
        scope = {"type": "http", "method": "POST", "path": "/v1/messages"}
        mock_request = Request(scope=scope)

        result = _openai_stream_to_anthropic(mock_request, mock_response, "test-model")

        async def consume():
            chunks = []
            async for chunk in result.body_iterator:
                chunks.append(chunk)
            return chunks

        chunks = run_async(consume())
        # The last event should be message_stop
        last_chunk = chunks[-1].decode("utf-8") if isinstance(chunks[-1], bytes) else chunks[-1]
        self.assertIn("message_stop", last_chunk)

        # Find the message_delta event and check it has usage from buffer
        message_delta_chunks = []
        for c in chunks:
            text = c.decode("utf-8") if isinstance(c, bytes) else c
            if "message_delta" in text:
                message_delta_chunks.append(text)
        self.assertTrue(len(message_delta_chunks) > 0)


class SettingsEnvParsingTests(unittest.TestCase):
    """Fix: settings.py must give clear error for invalid env var values."""

    def test_invalid_gateway_port_raises_clear_error(self):
        from llm_gateway_core.config.settings import _get_positive_int_env

        with patch.dict(os.environ, {"TEST_PORT": "not_a_number"}):
            with self.assertRaises(ValueError) as ctx:
                _get_positive_int_env("TEST_PORT", 9000)
            self.assertIn("TEST_PORT", str(ctx.exception))
            self.assertIn("not_a_number", str(ctx.exception))

    def test_negative_port_raises_clear_error(self):
        from llm_gateway_core.config.settings import _get_positive_int_env

        with patch.dict(os.environ, {"TEST_PORT": "-1"}):
            with self.assertRaises(ValueError) as ctx:
                _get_positive_int_env("TEST_PORT", 9000)
            self.assertIn("TEST_PORT", str(ctx.exception))

    def test_valid_port_accepted(self):
        from llm_gateway_core.config.settings import _get_positive_int_env

        with patch.dict(os.environ, {"TEST_PORT": "8080"}):
            result = _get_positive_int_env("TEST_PORT", 9000)
            self.assertEqual(result, 8080)

    def test_missing_env_uses_default(self):
        from llm_gateway_core.config.settings import _get_positive_int_env

        # Ensure the variable is not set
        env = os.environ.copy()
        env.pop("TEST_MISSING_VAR", None)
        with patch.dict(os.environ, env, clear=True):
            result = _get_positive_int_env("TEST_MISSING_VAR", 42)
            self.assertEqual(result, 42)


class ChatLoggingDbInstanceTests(unittest.TestCase):
    """chat_logging must fail loudly when its TokensUsageDB was never bound.

    Previously a silent ``TokensUsageDB()`` fallback meant typos in wiring
    (lifespan forgetting to bind, or tests forgetting to stub) inserted into
    an orphan SQLite file instead of surfacing the bug.
    """

    def test_require_tokens_usage_db_raises_when_unbound(self):
        from llm_gateway_core.middleware import chat_logging

        original_db = chat_logging.state.tokens_usage_db
        try:
            chat_logging.set_tokens_usage_db(None)
            with self.assertRaises(RuntimeError):
                chat_logging._require_tokens_usage_db()
        finally:
            chat_logging.set_tokens_usage_db(original_db)

    def test_bound_db_is_returned(self):
        from llm_gateway_core.middleware import chat_logging
        from llm_gateway_core.db.tokens_usage_db import TokensUsageDB

        original_db = chat_logging.state.tokens_usage_db
        sentinel_db = TokensUsageDB()
        try:
            chat_logging.set_tokens_usage_db(sentinel_db)
            self.assertIs(chat_logging._require_tokens_usage_db(), sentinel_db)
        finally:
            chat_logging.set_tokens_usage_db(original_db)


if __name__ == "__main__":
    unittest.main()
