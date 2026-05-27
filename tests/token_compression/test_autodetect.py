"""Tests for the auto_detect_filter function."""

from llm_gateway_core.services.token_compression.autodetect import auto_detect_filter
from llm_gateway_core.services.token_compression.filters import (
    git_diff,
    git_status,
    git_log,
    build_output,
    grep,
    find,
    ls,
    tree,
    dedup_log,
    smart_truncate,
    read_numbered,
    search_list,
)
from llm_gateway_core.services.token_compression.constants import SMART_TRUNCATE_MIN_LINES


def test_detect_git_diff():
    text = "diff --git a/foo.py b/foo.py\n@@ -1,5 +1,5 @@\n"
    assert auto_detect_filter(text) is git_diff


def test_detect_git_status_long():
    text = "On branch main\nChanges not staged for commit:\n  modified:   foo.py\n"
    assert auto_detect_filter(text) is git_status


def test_detect_git_status_porcelain():
    # Mostly porcelain lines
    lines = ["## main"] + [f"?? file{i}.py" for i in range(5)]
    text = "\n".join(lines)
    assert auto_detect_filter(text) is git_status


def test_detect_git_log():
    # At least 2 commit lines in the detection window
    text = (
        "commit a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2\n"
        "Author: Alice <alice@example.com>\n"
        "Date:   Mon Jan 1 00:00:00 2024 +0000\n\n"
        "    first commit\n\n"
        "commit b2c3d4e5f6a1b2c3d4e5f6a1b2c3d4e5f6a1b2c3\n"
        "Author: Bob <bob@example.com>\n"
        "Date:   Tue Jan 2 00:00:00 2024 +0000\n\n"
        "    second commit\n"
    )
    assert auto_detect_filter(text) is git_log


def test_detect_build_output():
    text = "Compiling foo v0.1.0\nCompiling bar v0.2.0\n"
    assert auto_detect_filter(text) is build_output


def test_detect_grep():
    text = "src/foo.py:10:def hello():\nsrc/bar.py:5:import os:\n"
    assert auto_detect_filter(text) is grep


def test_detect_find():
    text = "./src/foo.py\n./src/bar.py\n./tests/test_foo.py\n"
    assert auto_detect_filter(text) is find


def test_detect_tree():
    text = ".\n├── src\n│   └── foo.py\n└── tests\n"
    assert auto_detect_filter(text) is tree


def test_detect_ls():
    text = "total 24\n-rw-r--r--  1 user group 1024 Jan  1 2024 foo.py\n"
    assert auto_detect_filter(text) is ls


def test_detect_search_list():
    text = "Result of search in 'src' (total 3 files):\n- src/foo.py\n"
    assert auto_detect_filter(text) is search_list


def test_detect_read_numbered():
    lines = [f"  {i}|content line {i}" for i in range(SMART_TRUNCATE_MIN_LINES + 5)]
    text = "\n".join(lines)
    assert auto_detect_filter(text) is read_numbered


def test_detect_dedup_log():
    # 5+ non-empty lines without special patterns
    lines = [f"log message {i}" for i in range(10)]
    text = "\n".join(lines)
    assert auto_detect_filter(text) is dedup_log


def test_detect_smart_truncate():
    # smart_truncate fires when: non_empty < 5 AND total lines >= SMART_TRUNCATE_MIN_LINES
    # Create exactly 4 non-empty lines + many blank lines to get total >= MIN_LINES
    few_nonempty = "a\n\nb\n\nc\n\nd\n" + ("\n" * (SMART_TRUNCATE_MIN_LINES + 10))
    result = auto_detect_filter(few_nonempty)
    # With 4 non-empty lines (< 5), should be smart_truncate
    assert result is smart_truncate


def test_detect_none():
    # Very short text — no pattern matches, < 5 non-empty, < MIN_LINES total
    text = "hello world"
    assert auto_detect_filter(text) is None
