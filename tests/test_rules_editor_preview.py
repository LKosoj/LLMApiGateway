import re
from pathlib import Path


EDITOR_HTML = Path("static/rules-editor.html")
EDITOR_JS = Path("static/editor.js")


def _find_matching_brace(content, open_brace_index):
    depth = 0
    for index in range(open_brace_index, len(content)):
        char = content[index]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                return index
    raise AssertionError("Could not find matching closing brace")


def _extract_function_body(content, name):
    patterns = [
        rf"(?:async\s+)?function\s+{re.escape(name)}\s*\([^)]*\)\s*\{{",
        rf"const\s+{re.escape(name)}\s*=\s*(?:async\s*)?\([^)]*\)\s*=>\s*\{{",
        rf"const\s+{re.escape(name)}\s*=\s*(?:async\s*)?function\s*\([^)]*\)\s*\{{",
    ]
    for pattern in patterns:
        match = re.search(pattern, content)
        if not match:
            continue
        open_brace_index = content.find("{", match.start())
        close_brace_index = _find_matching_brace(content, open_brace_index)
        return content[open_brace_index + 1:close_brace_index]
    raise AssertionError(f"Function {name} is not defined")


def _extract_click_listener(content, element_name):
    marker_patterns = [
        f"{element_name}.addEventListener('click'",
        f'{element_name}.addEventListener("click"',
    ]
    start = next((content.find(marker) for marker in marker_patterns if content.find(marker) != -1), -1)
    if start == -1:
        raise AssertionError(f"Click listener for {element_name} is not defined")
    next_listener = content.find(".addEventListener", start + 1)
    if next_listener == -1:
        return content[start:]
    return content[start:next_listener]


def _assert_no_direct_config_save(content):
    assert "saveRules(" not in content
    assert "saveButton.click(" not in content
    has_post = re.search(r"method\s*:\s*['\"]POST['\"]", content)
    has_rules_save_endpoint = "/v1/config/models-rules/structured" in content
    assert not (has_post and has_rules_save_endpoint)


def test_rules_editor_html_contains_preview_controls():
    content = EDITOR_HTML.read_text(encoding="utf-8")

    assert "Preview Changes" in content
    assert "Suggest Eval Order" in content
    assert 'id="rulesPreviewArea"' in content


def test_editor_js_contains_preview_and_suggestion_functions():
    content = EDITOR_JS.read_text(encoding="utf-8")

    assert "rulesPreviewArea" in content
    assert _extract_function_body(content, "previewRulesChanges")
    assert _extract_function_body(content, "renderSuggestedFallbackOrder")


def test_preview_and_suggest_do_not_autosave_rules():
    content = EDITOR_JS.read_text(encoding="utf-8")

    preview_body = _extract_function_body(content, "previewRulesChanges")
    suggest_body = _extract_function_body(content, "renderSuggestedFallbackOrder")
    preview_listener = _extract_click_listener(content, "previewRulesButton")
    suggest_listener = _extract_click_listener(content, "suggestEvalOrderButton")

    for block in (preview_body, suggest_body, preview_listener, suggest_listener):
        _assert_no_direct_config_save(block)
