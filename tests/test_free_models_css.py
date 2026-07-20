from pathlib import Path


def test_free_models_retry_and_copy_buttons_meet_touch_target_minimum_on_mobile():
    css_content = Path("static/free-models.css").read_text(encoding="utf-8")

    mobile_block = css_content.split("@media (max-width: 600px) {", 1)[1]
    assert ".free-models-retry,\n    .free-models-copy {\n        min-height: 44px;\n    }" in mobile_block
