"""Tests for the git_status filter."""

from llm_gateway_core.services.token_compression.filters.git_status import git_status

PORCELAIN_STATUS = """\
## main...origin/main
 M src/foo.py
?? new_file.txt
A  staged_new.py
"""

LONG_STATUS = """\
On branch main
Changes to be committed:
  (use "git restore --staged <file>..." to unstage)
        new file:   staged_new.py

Changes not staged for commit:
  (use "git add <file>..." to update what will be committed)
        modified:   src/foo.py

Untracked files:
  (use "git add <file>..." to include in what will be committed)
        new_file.txt
"""


def test_git_status_porcelain():
    result = git_status(PORCELAIN_STATUS)
    assert "main" in result
    assert "Modified" in result or "Untracked" in result or "Staged" in result


def test_git_status_long_format():
    result = git_status(LONG_STATUS)
    assert "main" in result
    assert "staged_new.py" in result or "Staged" in result


def test_git_status_empty():
    result = git_status("")
    assert result == "Clean working tree"


def test_git_status_clean():
    result = git_status("nothing to commit, working tree clean\n")
    # Should still return something; the "clean" message
    assert result is not None


def test_git_status_filter_name():
    assert git_status.filter_name == "git-status"


def test_git_status_many_files_truncated():
    """Files list is capped at STATUS_MAX_FILES."""
    lines = ["## main"]
    for i in range(20):
        lines.append(f"?? file{i}.py")
    result = git_status("\n".join(lines))
    assert "more" in result
