"""Tests for the compress_messages compressor."""

from unittest.mock import patch

from llm_gateway_core.services.token_compression import compressor as compressor_module
from llm_gateway_core.services.token_compression.compressor import compress_messages


def _make_long_text(n: int = 600) -> str:
    return ("a" * 50 + "\n") * n


def test_compress_disabled():
    body = {"messages": [{"role": "tool", "content": _make_long_text()}]}
    result = compress_messages(body, enabled=False)
    assert result is None


def test_compress_empty_body():
    result = compress_messages({}, enabled=True)
    assert result is None


def test_compress_no_messages():
    result = compress_messages({"model": "gpt-4"}, enabled=True)
    assert result is None


def test_compress_openai_tool_string():
    """OpenAI tool message with string content."""
    text = _make_long_text()
    body = {"messages": [{"role": "tool", "content": text}]}
    result = compress_messages(body, enabled=True)
    # Compression should have occurred (dedup_log or smart_truncate)
    assert result is not None
    assert result.hits > 0
    assert body["messages"][0]["content"] != text


def test_compress_only_tool_role():
    """Only role:'tool' messages are compressed, not user/assistant."""
    user_text = _make_long_text()
    tool_text = _make_long_text()
    body = {
        "messages": [
            {"role": "user", "content": user_text},
            {"role": "tool", "content": tool_text},
        ]
    }
    compress_messages(body, enabled=True)
    # User message should be untouched
    assert body["messages"][0]["content"] == user_text


def test_compress_claude_tool_result_string():
    """Claude-style tool_result with string content."""
    text = _make_long_text()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "content": text,
                    }
                ],
            }
        ]
    }
    result = compress_messages(body, enabled=True)
    assert result is not None
    assert body["messages"][0]["content"][0]["content"] != text


def test_compress_claude_error_preserved():
    """tool_result with is_error=True is NOT compressed."""
    text = _make_long_text()
    body = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "x",
                        "is_error": True,
                        "content": text,
                    }
                ],
            }
        ]
    }
    compress_messages(body, enabled=True)
    assert body["messages"][0]["content"][0]["content"] == text


def test_compress_empty_messages_list():
    body = {"messages": []}
    result = compress_messages(body, enabled=True)
    assert result is None


def test_compress_short_content_passthrough():
    """Text shorter than MIN_COMPRESS_SIZE is not compressed."""
    short = "hello world"
    body = {"messages": [{"role": "tool", "content": short}]}
    result = compress_messages(body, enabled=True)
    # No hits — stats are None or hits==0
    assert result is None
    assert body["messages"][0]["content"] == short


def test_compress_stats_fields():
    text = _make_long_text()
    body = {"messages": [{"role": "tool", "content": text}]}
    stats = compress_messages(body, enabled=True)
    assert stats is not None
    assert stats.input_bytes > 0
    assert stats.output_bytes > 0
    assert stats.hits > 0
    assert isinstance(stats.filters_applied, list)
    assert len(stats.filters_applied) > 0


def test_compress_openai_responses_format():
    """OpenAI Responses API: function_call_output with string output."""
    text = _make_long_text()
    body = {
        "input": [
            {"type": "function_call_output", "output": text}
        ]
    }
    result = compress_messages(body, enabled=True)
    assert result is not None
    assert body["input"][0]["output"] != text


def test_compress_partial_mutation_rolled_back_on_exception():
    """If compression raises mid-loop, no message in the body is mutated."""
    text_a = _make_long_text()
    text_b = _make_long_text()
    body = {
        "messages": [
            {"role": "tool", "content": text_a},
            {"role": "tool", "content": text_b},
        ]
    }

    calls = {"n": 0}

    def flaky(text, stats):
        calls["n"] += 1
        if calls["n"] == 1:
            return "SHORTENED"
        raise RuntimeError("boom on the second block")

    with patch.object(compressor_module, "_compress_text", side_effect=flaky):
        result = compress_messages(body, enabled=True)

    assert result is None
    assert body["messages"][0]["content"] == text_a
    assert body["messages"][1]["content"] == text_b
