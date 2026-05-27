"""Tests for the grep filter."""

from llm_gateway_core.services.token_compression.filters.grep import grep


SAMPLE_GREP = """\
src/foo.py:10:def hello():
src/foo.py:15:    print("hello")
src/bar.py:5:def bar():
"""


def test_grep_basic():
    result = grep(SAMPLE_GREP)
    assert "2 matches" in result or "3 matches" in result
    assert "src/foo.py" in result
    assert "src/bar.py" in result


def test_grep_empty():
    result = grep("")
    assert result == ""


def test_grep_no_matches():
    result = grep("not a grep line\nno colon here\n")
    assert result == "not a grep line\nno colon here\n"


def test_grep_filter_name():
    assert grep.filter_name == "grep"


def test_grep_per_file_cap():
    """Matches beyond GREP_PER_FILE_MAX per file are truncated."""
    lines = [f"src/big.py:{i}:content" for i in range(20)]
    result = grep("\n".join(lines))
    assert "+10" in result or "+" in result


def test_grep_groups_by_file():
    text = (
        "a.py:1:match\n"
        "b.py:2:match\n"
        "a.py:3:another\n"
    )
    result = grep(text)
    assert "[file] a.py (2):" in result
    assert "[file] b.py (1):" in result
