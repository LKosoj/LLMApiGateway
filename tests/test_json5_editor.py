def test_rules_editor_no_longer_uses_raw_codemirror_editor():
    with open('static/editor.js', 'r') as f:
        js_content = f.read()
    with open('static/rules-editor.html', 'r') as f:
        html_content = f.read()

    assert "CodeMirror" not in js_content
    assert "CodeMirror" not in html_content
    assert "/v1/config/providers/structured" in js_content


def test_rules_editor_html_hint_added():
    with open('static/rules-editor.html', 'r') as f:
        content = f.read()
    
    assert "JSON5" in content, "A hint about JSON5 support should be added to rules-editor.html"
    assert "comments" in content or "комментари" in content.lower(), "The hint should mention comments support"
