"""Tests for the search_list filter."""

from llm_gateway_core.services.token_compression.filters.search_list import search_list


SAMPLE_SEARCH = """\
Result of search in 'src' (total 3 files):
- src/foo.py
- src/bar/baz.py
- src/tests/test_foo.py
"""


def test_search_list_basic():
    result = search_list(SAMPLE_SEARCH)
    assert "3 files in" in result
    assert "src/" in result


def test_search_list_empty_paths():
    text = "Result of search in 'src' (total 0 files):\n"
    result = search_list(text)
    # No paths → passthrough
    assert result == text


def test_search_list_filter_name():
    assert search_list.filter_name == "search-list"


def test_search_list_per_dir_cap():
    header = "Result of search in 'src' (total 15 files):\n"
    paths = "\n".join(f"- src/file{i}.py" for i in range(15))
    result = search_list(header + paths)
    assert "+5" in result


def test_search_list_total_dir_cap():
    header = "Result of search in '.' (total 25 files):\n"
    paths = "\n".join(f"- dir{i}/file.py" for i in range(25))
    result = search_list(header + paths)
    assert "more dirs" in result


def test_search_list_groups_by_dir():
    header = "Result of search in 'src' (total 3 files):\n"
    paths = "- src/a.py\n- src/b.py\n- tests/c.py\n"
    result = search_list(header + paths)
    assert "src/ (2):" in result
    assert "tests/ (1):" in result
