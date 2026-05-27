"""Tests for the tree filter."""

from llm_gateway_core.services.token_compression.filters.tree import tree
from llm_gateway_core.services.token_compression.constants import TREE_MAX_LINES

SAMPLE_TREE = """\
.
├── src
│   ├── foo.py
│   └── bar.py
└── tests
    └── test_foo.py

2 directories, 3 files
"""


def test_tree_removes_summary():
    result = tree(SAMPLE_TREE)
    assert "directories" not in result
    assert "3 files" not in result


def test_tree_keeps_structure():
    result = tree(SAMPLE_TREE)
    assert "src" in result
    assert "foo.py" in result


def test_tree_empty():
    result = tree("")
    assert result == ""


def test_tree_filter_name():
    assert tree.filter_name == "tree"


def test_tree_max_lines_cap():
    lines = [f"│   file{i}.py" for i in range(TREE_MAX_LINES + 50)]
    text = "\n".join(lines)
    result = tree(text)
    assert "more lines" in result
    assert len(result.split("\n")) <= TREE_MAX_LINES + 1
