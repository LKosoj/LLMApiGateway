"""Tests for safe_apply (apply_filter.py)."""

from llm_gateway_core.services.token_compression.apply_filter import safe_apply


def test_safe_apply_basic():
    def fn(text):
        return "compressed"
    fn.filter_name = "test"  # type: ignore[attr-defined]
    result = safe_apply(fn, "a" * 100)
    assert result == "compressed"


def test_safe_apply_error_returns_original():
    def bad_fn(text):
        raise ValueError("oops")
    bad_fn.filter_name = "bad"
    original = "original text " * 5
    result = safe_apply(bad_fn, original)
    assert result == original


def test_safe_apply_size_growth_returns_original():
    """If result is larger than input, original is returned."""
    original = "short"

    def fn(text):
        return text + " " * 1000  # grows the input

    result = safe_apply(fn, original)
    assert result == original


def test_safe_apply_empty_result_returns_original():
    """If result is empty, original is returned."""
    original = "some text"

    def fn(text):
        return ""

    result = safe_apply(fn, original)
    assert result == original


def test_safe_apply_not_callable():
    result = safe_apply("not a function", "text")  # type: ignore[arg-type]
    assert result == "text"


def test_safe_apply_non_string_result_returns_original():
    def fn(text):
        return 42  # type: ignore[return-value]

    original = "text"
    result = safe_apply(fn, original)  # type: ignore[arg-type]
    assert result == original


def test_safe_apply_compression_ratio():
    """Verify actual compression is returned."""
    long_text = "hello\n" * 200
    def compress(text):
        return "hello\n"
    result = safe_apply(compress, long_text)
    assert result == "hello\n"
    assert len(result) < len(long_text)
