from pathlib import Path


def test_usage_stats_tables_use_8px_font_size():
    css_content = Path("static/usage-stats.css").read_text(encoding="utf-8")

    assert "table {" in css_content
    assert "font-size: 8px;" in css_content
