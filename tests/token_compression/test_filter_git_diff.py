"""Tests for the git_diff filter."""

from llm_gateway_core.services.token_compression.filters.git_diff import git_diff


SAMPLE_DIFF = """\
diff --git a/src/foo.py b/src/foo.py
index abc123..def456 100644
--- a/src/foo.py
+++ b/src/foo.py
@@ -1,5 +1,5 @@
 def hello():
-    print("hello")
+    print("world")
     return True
"""


def test_git_diff_basic():
    result = git_diff(SAMPLE_DIFF)
    assert "src/foo.py" in result
    assert "+    print" in result or "print" in result


def test_git_diff_empty():
    result = git_diff("")
    assert result == ""


def test_git_diff_small():
    """Small diff (no hunks) returns minimal output."""
    result = git_diff("diff --git a/x b/x\n")
    assert "x" in result


def test_git_diff_filter_name():
    assert git_diff.filter_name == "git-diff"


def test_git_diff_hunk_truncation():
    """Hunks beyond GIT_DIFF_HUNK_MAX_LINES are truncated."""
    # Build a hunk with 200 '+' lines
    lines = [
        "diff --git a/big.py b/big.py",
        "@@ -1,200 +1,200 @@",
    ]
    for i in range(200):
        lines.append(f"+line{i}")
    result = git_diff("\n".join(lines))
    assert "lines truncated" in result


def test_git_diff_multiple_files():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-old\n"
        "+new\n"
        "diff --git a/b.py b/b.py\n"
        "@@ -1,2 +1,2 @@\n"
        "-foo\n"
        "+bar\n"
    )
    result = git_diff(diff)
    assert "a.py" in result
    assert "b.py" in result
