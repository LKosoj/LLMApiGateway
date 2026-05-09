"""Tests verifying that json.loads handles all LLM API response formats correctly.

json5.loads was replaced with json.loads on the hot path (SSE chunk parsing,
request body parsing) for a ~745x speedup.  These tests verify that standard
json.loads handles every real-world payload shape that the gateway encounters.
"""

import json


def test_utf8_emoji_in_content():
    """Standard json.loads handles UTF-8 emoji without issues."""
    payload = '{"choices":[{"delta":{"content":"Hello 😀🎉👍 мир"}}]}'
    parsed = json.loads(payload)
    assert parsed["choices"][0]["delta"]["content"] == "Hello 😀🎉👍 мир"


def test_escaped_surrogate_pair():
    """json.loads correctly assembles a valid \\uD83D\\uDE00 surrogate pair into 😀."""
    payload = '{"text":"Hello \\uD83D\\uDE00"}'
    parsed = json.loads(payload)
    assert parsed["text"] == "Hello 😀"


def test_openai_chat_completion_chunk():
    """Typical OpenAI SSE chunk parses correctly."""
    chunk = (
        '{"id":"chatcmpl-abc","object":"chat.completion.chunk",'
        '"created":1700000000,"model":"gpt-4",'
        '"choices":[{"index":0,"delta":{"content":"Hello"},"finish_reason":null}]}'
    )
    parsed = json.loads(chunk)
    assert parsed["id"] == "chatcmpl-abc"
    assert parsed["choices"][0]["delta"]["content"] == "Hello"


def test_openai_usage_chunk():
    """OpenAI usage statistics chunk parses correctly."""
    chunk = (
        '{"id":"chatcmpl-abc","object":"chat.completion.chunk",'
        '"model":"gpt-4","usage":{"prompt_tokens":10,'
        '"completion_tokens":20,"total_tokens":30}}'
    )
    parsed = json.loads(chunk)
    assert parsed["usage"]["total_tokens"] == 30


def test_anthropic_message_start():
    """Anthropic message_start event parses correctly."""
    chunk = (
        '{"type":"message_start","message":{"id":"msg_123",'
        '"type":"message","role":"assistant","content":[],'
        '"model":"claude-3-opus-20240229",'
        '"usage":{"input_tokens":25,"output_tokens":1}}}'
    )
    parsed = json.loads(chunk)
    assert parsed["type"] == "message_start"
    assert parsed["message"]["usage"]["input_tokens"] == 25


def test_anthropic_content_block_delta():
    """Anthropic content_block_delta with text parses correctly."""
    chunk = '{"type":"content_block_delta","index":0,"delta":{"type":"text_delta","text":"Hello world"}}'
    parsed = json.loads(chunk)
    assert parsed["delta"]["text"] == "Hello world"


def test_anthropic_thinking_delta():
    """Anthropic thinking delta parses correctly."""
    chunk = '{"type":"content_block_delta","index":0,"delta":{"type":"thinking_delta","thinking":"Let me think..."}}'
    parsed = json.loads(chunk)
    assert parsed["delta"]["thinking"] == "Let me think..."


def test_error_response_json():
    """Provider error responses parse correctly."""
    error = '{"error":{"message":"Rate limit exceeded","type":"rate_limit_error","code":"429"}}'
    parsed = json.loads(error)
    assert parsed["error"]["message"] == "Rate limit exceeded"


def test_openrouter_provider_order():
    """OpenRouter provider order in payload parses correctly."""
    payload = '{"model":"gpt-4","messages":[],"provider":{"order":["openai","azure"]},"allow_fallbacks":false}'
    parsed = json.loads(payload)
    assert parsed["provider"]["order"] == ["openai", "azure"]


def test_response_format_json_object():
    """response_format with json_object type parses correctly."""
    payload = '{"model":"gpt-4","messages":[],"response_format":{"type":"json_object"}}'
    parsed = json.loads(payload)
    assert parsed["response_format"]["type"] == "json_object"


def test_unicode_in_tool_arguments():
    """Tool call arguments with unicode content parse correctly."""
    chunk = '{"choices":[{"delta":{"tool_calls":[{"function":{"arguments":"{\\"query\\":\\"привет мир 🌍\\"}"}}]}}]}'
    parsed = json.loads(chunk)
    args = parsed["choices"][0]["delta"]["tool_calls"][0]["function"]["arguments"]
    inner = json.loads(args)
    assert inner["query"] == "привет мир 🌍"


def test_multiline_content():
    """Content with newlines parses correctly."""
    payload = '{"choices":[{"message":{"content":"line1\\nline2\\n\\nline4"}}]}'
    parsed = json.loads(payload)
    assert "line1\nline2" in parsed["choices"][0]["message"]["content"]


def test_empty_usage():
    """Empty or zero usage values parse correctly."""
    payload = '{"usage":{"prompt_tokens":0,"completion_tokens":0,"total_tokens":0}}'
    parsed = json.loads(payload)
    assert parsed["usage"]["prompt_tokens"] == 0
