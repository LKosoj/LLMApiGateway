from pathlib import Path


def test_usage_stats_tables_use_a_readable_font_size():
    css_content = Path("static/usage-stats.css").read_text(encoding="utf-8")

    assert "table {" in css_content
    assert "font-size: 13px;" in css_content
    assert "font-size: 8px;" not in css_content


def test_shared_scaffold_does_not_import_theme_relative_to_ui_routes():
    css_content = Path("static/usage-stats.css").read_text(encoding="utf-8")

    assert "@import" not in css_content


def test_analytics_labels_use_accessible_text_color_on_sunken_surfaces():
    css_content = Path("static/usage-analytics.css").read_text(encoding="utf-8")

    assert ".analytics-filters span {\n    color: var(--text);" in css_content
    assert ".analytics-table th {\n    color: var(--text);\n}" in css_content
