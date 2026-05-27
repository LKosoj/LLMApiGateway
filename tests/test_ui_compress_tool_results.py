"""Tests for compress_tool_results UI elements in editor.js."""

EDITOR_JS = "static/editor.js"


def _read_editor_js() -> str:
    with open(EDITOR_JS, encoding="utf-8") as f:
        return f.read()


def test_compress_tool_results_checkbox_exists_in_build_rule_card():
    """buildRuleCard should create the compress-tool-results-checkbox input."""
    content = _read_editor_js()
    assert "compress-tool-results-checkbox" in content


def test_compress_tool_results_label_text():
    """The label for the checkbox should describe the RTK feature."""
    content = _read_editor_js()
    assert "Compress tool result outputs (RTK)" in content


def test_compress_tool_results_in_normalize_rule_card():
    """normalizeRuleCardForSave should include compress_tool_results field."""
    content = _read_editor_js()
    assert "compress_tool_results" in content
    # Specifically the save path should reference the checkbox
    assert "compressToolResultsCheckbox" in content


def test_compress_tool_results_boolean_in_save():
    """compress_tool_results should be saved as Boolean(checkbox?.checked)."""
    content = _read_editor_js()
    assert "Boolean(compressToolResultsCheckbox?.checked)" in content


def test_compress_tool_results_in_snapshot():
    """getRulesSnapshotPayload should also snapshot the compress_tool_results field."""
    content = _read_editor_js()
    # Both normalizeRuleCardForSave and getRulesSnapshotPayload use it
    count = content.count("compress_tool_results")
    assert count >= 2, f"Expected at least 2 occurrences of 'compress_tool_results', found {count}"


def test_compress_tool_results_initial_data_read():
    """buildRuleCard should read initialData.compress_tool_results to set checkbox."""
    content = _read_editor_js()
    assert "initialData.compress_tool_results" in content
