"""Tests for the git_log filter."""

from llm_gateway_core.services.token_compression.filters.git_log import git_log
from llm_gateway_core.services.token_compression.constants import SMART_TRUNCATE_MIN_LINES


def test_git_log_small_passthrough():
    text = "commit abc1234\nAuthor: x\nDate: y\n\n    msg\n"
    result = git_log(text)
    assert result == text


def test_git_log_large_truncated():
    lines = []
    for i in range(SMART_TRUNCATE_MIN_LINES + 50):
        lines.append(f"commit {i:07x}")
        lines.append("Author: test")
        lines.append("")
    text = "\n".join(lines)
    result = git_log(text)
    assert "lines truncated" in result
    assert len(result) < len(text)


def test_git_log_filter_name():
    assert git_log.filter_name == "git-log"


def test_git_log_commit_count_in_header():
    lines = []
    for i in range(SMART_TRUNCATE_MIN_LINES + 10):
        lines.append(f"commit {i:040x}")
        lines.append("")
    text = "\n".join(lines)
    result = git_log(text)
    assert "commits" in result
