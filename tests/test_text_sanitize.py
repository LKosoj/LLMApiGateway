"""Tests for llm_gateway_core.utils.text_sanitize module."""

from llm_gateway_core.api.v1.chat import _read_json_request_body
from llm_gateway_core.services.request_handler import _parse_stream_chunk_json
from llm_gateway_core.utils.text_sanitize import remove_surrogates, sanitize_payload


class TestRemoveSurrogates:
    def test_no_surrogates(self):
        assert remove_surrogates("hello world") == "hello world"

    def test_empty_string(self):
        assert remove_surrogates("") == ""

    def test_lone_high_surrogate(self):
        text = "abc\ud800def"
        result = remove_surrogates(text)
        assert result == "abc\ufffddef"
        # Must be encodable to UTF-8 now
        result.encode("utf-8")

    def test_lone_low_surrogate(self):
        text = "abc\udc00def"
        result = remove_surrogates(text)
        assert result == "abc\ufffddef"

    def test_surrogate_pair(self):
        text = "abc\ud83d\ude00def"
        result = remove_surrogates(text)
        assert result == "abc😀def"

    def test_multiple_surrogates(self):
        text = "\ud800hello\udbff\udfff"
        result = remove_surrogates(text)
        assert "\ud800" not in result
        assert "\udbff" not in result
        assert "\udfff" not in result
        assert result == "�hello\U0010ffff"
        result.encode("utf-8")

    def test_emoji_preserved(self):
        text = "hello \U0001f600 world"
        assert remove_surrogates(text) == text


class TestSanitizePayload:
    def test_string(self):
        assert sanitize_payload("abc\ud83d\ude00def") == "abc😀def"

    def test_lone_surrogate_string(self):
        assert sanitize_payload("abc\ud800def") == "abc\ufffddef"

    def test_clean_string_unchanged(self):
        s = "hello world"
        assert sanitize_payload(s) is s  # same object, no copy

    def test_dict(self):
        payload = {"role": "user", "content": "text\ud800here"}
        result = sanitize_payload(payload)
        assert result == {"role": "user", "content": "text\ufffdhere"}

    def test_nested_dict(self):
        payload = {"messages": [{"role": "user", "content": "a\ud800b"}]}
        result = sanitize_payload(payload)
        assert result["messages"][0]["content"] == "a\ufffdb"

    def test_list(self):
        payload = ["a\ud800b", "clean", 42]
        result = sanitize_payload(payload)
        assert result == ["a\ufffdb", "clean", 42]

    def test_non_string_leaves(self):
        payload = {"count": 42, "flag": True, "val": None, "rate": 3.14}
        assert sanitize_payload(payload) == payload

    def test_empty_dict(self):
        assert sanitize_payload({}) == {}

    def test_empty_list(self):
        assert sanitize_payload([]) == []

    def test_deep_nesting(self):
        payload = {"a": {"b": {"c": [{"d": "x\ud800y"}]}}}
        result = sanitize_payload(payload)
        assert result["a"]["b"]["c"][0]["d"] == "x\ufffdy"

    def test_result_is_json_serializable(self):
        """The whole point: after sanitization json.dumps must not fail."""
        import json
        payload = {
            "model": "test",
            "messages": [
                {"role": "user", "content": "hello\ud800\udc00world\udbffend"}
            ],
        }
        result = sanitize_payload(payload)
        # This would raise UnicodeEncodeError without sanitization
        json.dumps(result, ensure_ascii=False)


class TestJson5SurrogateNormalization:
    def test_read_json_request_body_normalizes_surrogate_pairs(self):
        payload = _read_json_request_body(
            b'{"messages":[{"role":"user","content":"hello \\ud83d\\ude00"}]}',
            "chat",
        )

        assert payload["messages"][0]["content"] == "hello 😀"

    def test_parse_stream_chunk_json_normalizes_surrogate_pairs(self):
        chunk_json = _parse_stream_chunk_json(
            'data: {"choices":[{"delta":{"content":"\\ud83d\\ude00"}}]}'
        )

        assert chunk_json["choices"][0]["delta"]["content"] == "😀"
