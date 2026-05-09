def test_editor_js_has_unsaved_changes_check():
    with open('static/editor.js', 'r') as f:
        content = f.read()
    
    assert "hasUnsavedChanges" in content
    assert "confirm(" in content
    assert "originalRulesContent" in content
    assert "originalProvidersContent" in content
