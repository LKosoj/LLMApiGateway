"""Tests for the read_numbered filter."""

from llm_gateway_core.services.token_compression.filters.read_numbered import read_numbered
from llm_gateway_core.services.token_compression.constants import SMART_TRUNCATE_MIN_LINES


def test_read_numbered_small_passthrough():
    text = "  1|line one\n  2|line two\n"
    result = read_numbered(text)
    assert result == text


def test_read_numbered_large():
    lines = [f"  {i}|content line {i}" for i in range(1, SMART_TRUNCATE_MIN_LINES + 51)]
    text = "\n".join(lines)
    result = read_numbered(text)
    assert "lines truncated (file continues)" in result
    assert len(result) < len(text)


def test_read_numbered_filter_name():
    assert read_numbered.filter_name == "read-numbered"


def test_read_numbered_preserves_head_and_tail():
    lines = [f"  {i}|content" for i in range(1, SMART_TRUNCATE_MIN_LINES + 20)]
    text = "\n".join(lines)
    result = read_numbered(text)
    assert "  1|content" in result
    assert f"  {SMART_TRUNCATE_MIN_LINES + 19}|content" in result
