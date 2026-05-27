"""Tests for the ls filter."""

from llm_gateway_core.services.token_compression.filters.ls import ls

SAMPLE_LS = """\
total 24
drwxr-xr-x  4 user group  128 Jan  1 2024 src
-rw-r--r--  1 user group 1024 Jan  1 2024 README.md
-rw-r--r--  1 user group  512 Jan  1 2024 setup.py
drwxr-xr-x  2 user group   64 Jan  1 2024 node_modules
"""


def test_ls_basic():
    result = ls(SAMPLE_LS)
    assert "src/" in result
    assert "README.md" in result
    # node_modules should be filtered out (noise dir)
    assert "node_modules" not in result


def test_ls_empty():
    result = ls("")
    assert result == ""


def test_ls_no_parseable_lines():
    result = ls("not an ls line\n")
    assert result == "not an ls line\n"


def test_ls_filter_name():
    assert ls.filter_name == "ls"


def test_ls_summary():
    result = ls(SAMPLE_LS)
    assert "Summary:" in result


def test_ls_size_formatting():
    line = "-rw-r--r--  1 user group 1048576 Jan  1 2024 big_file.bin\n"
    result = ls("total 1\n" + line)
    assert "1.0M" in result
