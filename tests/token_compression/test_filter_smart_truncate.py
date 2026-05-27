"""Tests for the smart_truncate filter."""

from llm_gateway_core.services.token_compression.filters.smart_truncate import smart_truncate
from llm_gateway_core.services.token_compression.constants import (
    SMART_TRUNCATE_MIN_LINES,
    SMART_TRUNCATE_HEAD,
    SMART_TRUNCATE_TAIL,
)


def test_smart_truncate_small_passthrough():
    text = "line1\nline2\nline3\n"
    result = smart_truncate(text)
    assert result == text


def test_smart_truncate_large():
    total = SMART_TRUNCATE_MIN_LINES + 50  # 300
    lines = [f"line{i}" for i in range(total)]
    text = "\n".join(lines)
    result = smart_truncate(text)
    assert "lines truncated" in result
    result_lines = result.split("\n")
    assert result_lines[0] == "line0"
    expected_cut = total - SMART_TRUNCATE_HEAD - SMART_TRUNCATE_TAIL  # 120
    assert result_lines[SMART_TRUNCATE_HEAD] == f"... +{expected_cut} lines truncated"


def test_smart_truncate_preserves_head_and_tail():
    lines = [f"line{i}" for i in range(SMART_TRUNCATE_MIN_LINES + 10)]
    text = "\n".join(lines)
    result = smart_truncate(text)
    result_lines = result.split("\n")
    # First SMART_TRUNCATE_HEAD lines
    assert result_lines[0] == "line0"
    # Last SMART_TRUNCATE_TAIL lines
    last_line = f"line{SMART_TRUNCATE_MIN_LINES + 9}"
    assert result_lines[-1] == last_line


def test_smart_truncate_filter_name():
    assert smart_truncate.filter_name == "smart-truncate"
