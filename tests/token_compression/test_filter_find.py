"""Tests for the find filter."""

from llm_gateway_core.services.token_compression.filters.find import find


SAMPLE_FIND = """\
./src/foo.py
./src/bar.py
./tests/test_foo.py
"""


def test_find_basic():
    result = find(SAMPLE_FIND)
    assert "files in" in result
    assert "src/" in result or "./src/" in result


def test_find_empty():
    result = find("")
    assert result == ""


def test_find_no_dirs():
    result = find("foo.py\nbar.py\n")
    assert "files in" in result
    assert "./" in result


def test_find_filter_name():
    assert find.filter_name == "find"


def test_find_per_dir_cap():
    """Files beyond FIND_PER_DIR_MAX per dir are truncated."""
    lines = [f"./src/file{i}.py" for i in range(15)]
    result = find("\n".join(lines))
    assert "+5" in result


def test_find_total_dir_cap():
    """Dirs beyond FIND_TOTAL_DIR_MAX are truncated."""
    lines = [f"./dir{i}/file.py" for i in range(25)]
    result = find("\n".join(lines))
    assert "more dirs" in result
