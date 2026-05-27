"""Tests for the dedup_log filter."""

from llm_gateway_core.services.token_compression.filters.dedup_log import dedup_log
from llm_gateway_core.services.token_compression.constants import DEDUP_LINE_MAX


def test_dedup_log_removes_duplicates():
    text = "line1\nline1\nline1\nline2\n"
    result = dedup_log(text)
    assert "2 duplicate lines" in result
    assert result.count("line1") == 1


def test_dedup_log_no_duplicates():
    text = "a\nb\nc\n"
    result = dedup_log(text)
    assert "a" in result
    assert "b" in result
    assert "c" in result


def test_dedup_log_empty():
    result = dedup_log("")
    assert result == ""


def test_dedup_log_filter_name():
    assert dedup_log.filter_name == "dedup-log"


def test_dedup_log_blank_run_collapsed():
    text = "line1\n\n\n\nline2\n"
    result = dedup_log(text)
    # At most 1 blank line between
    assert "\n\n\n" not in result


def test_dedup_log_hard_cap():
    lines = [f"line{i}" for i in range(DEDUP_LINE_MAX + 100)]
    result = dedup_log("\n".join(lines))
    assert f"truncated at {DEDUP_LINE_MAX}" in result


def test_dedup_log_notice_before_blank():
    """Duplicate notice must appear before the following blank line, not after."""
    # Three identical "error line" entries followed by a blank line then new section
    text = "error line\nerror line\nerror line\n\nnext section\n"
    result = dedup_log(text)
    lines = result.split("\n")
    # Find positions
    notice_idx = next(i for i, line in enumerate(lines) if "duplicate" in line)
    blank_idx = next(i for i, line in enumerate(lines) if not line.strip() and i > 0)
    # Notice must come before the blank separator
    assert notice_idx < blank_idx, (
        f"Expected notice (line {notice_idx}) before blank (line {blank_idx}); got:\n{result}"
    )
