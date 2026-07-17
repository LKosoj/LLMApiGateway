"""R5.8 contracts and browser regression for the Quota i18n migration."""

from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _leaf_keys(value: object, prefix: str = "") -> set[str]:
    if not isinstance(value, dict):
        return set()
    result: set[str] = set()
    for key, child in value.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(child, str):
            result.add(path)
        else:
            result.update(_leaf_keys(child, path))
    return result


def _function_body(source: str, signature: str) -> str:
    start = source.index(signature)
    opening = source.index("{", start)
    depth = 0
    for index in range(opening, len(source)):
        if source[index] == "{":
            depth += 1
        elif source[index] == "}":
            depth -= 1
            if depth == 0:
                return source[opening + 1 : index]
    raise AssertionError(f"Unclosed JavaScript function: {signature}")


def test_quota_catalogs_and_html_cover_the_complete_page() -> None:
    english = json.loads(
        (ROOT / "static/locales/en/quota.json").read_text(encoding="utf-8")
    )
    russian = json.loads(
        (ROOT / "static/locales/ru/quota.json").read_text(encoding="utf-8")
    )
    required = {
        "budget",
        "disabled",
        "emptyAction",
        "emptyHint",
        "emptyTitle",
        "fallbacks24h",
        "heading",
        "initialError",
        "lastUpdated",
        "left",
        "loading",
        "noLimit",
        "pageTitle",
        "plan",
        "requestsPerMinute",
        "resets",
        "retry",
        "spent",
        "stale",
        "tokensPerMinute",
        "unlimited",
        "upstreamError",
        "upstreamErrorSummary",
        "upstreamKinds.antigravity",
        "upstreamKinds.geminiCli",
        "upstreamKinds.githubCopilot",
        "upstreamTitle",
        "upstreamTrackedExternally",
    }
    assert _leaf_keys(english) == _leaf_keys(russian)
    assert required <= _leaf_keys(english)

    html = (ROOT / "static/quota.html").read_text(encoding="utf-8")
    css = (ROOT / "static/quota.css").read_text(encoding="utf-8")
    assert 'data-i18n="quota:retry"' in html
    assert 'data-i18n-aria-label="common:theme.switchToDark"' in html
    assert "Toggle dark mode" not in html
    countdown_block = css.split(".countdown-badge {", 1)[1].split("}", 1)[0]
    assert "color: var(--text);" in countdown_block


def test_quota_locale_rerender_is_in_place_and_does_not_restart_work() -> None:
    source = (ROOT / "static/quota.js").read_text(encoding="utf-8")
    locale_body = _function_body(source, "function rerenderLocale()")

    assert "i18n.subscribe(rerenderLocale)" in source
    assert "updateQuotaCardsLocale()" in locale_body
    assert "updateUpstreamCardsLocale()" in locale_body
    assert "publishQuotaStatus()" in locale_body
    assert "captureLocaleUiState()" in locale_body
    assert "restoreLocaleUiState(" in locale_body
    for forbidden in (
        "renderCards(",
        "renderKeyCard(",
        "renderUpstreamSnapshots(",
        "renderUpstreamCard(",
        "fetchQuotaData(",
        "loadUpstreamQuotas(",
        "schedulePoll(",
        "clearPollTimer(",
        "clearCountdownTimers(",
        "startCountdownTimer(",
        "apiFetch(",
        "fetch(",
        ".innerHTML",
        "replaceChildren(",
    ):
        assert forbidden not in locale_body


def test_quota_uses_dom_safe_rendering_runtime_formatters_and_closed_enums() -> None:
    source = (ROOT / "static/quota.js").read_text(encoding="utf-8")

    assert ".innerHTML" not in source
    assert "i18n.formatNumber" in source
    assert "i18n.formatCurrency" in source
    assert "i18n.formatDate" in source
    assert ".toLocaleString(" not in source
    assert "const UPSTREAM_KIND_KEYS = Object.freeze" in source
    assert "Object.hasOwn(UPSTREAM_KIND_KEYS" in source
    assert "`quota:upstreamKinds.${" not in source
    assert "upstream-card-error-summary" in source
    assert "upstream-card-error-detail" in source
    assert "detail.lang = 'und'" in source
    assert "detail.dir = 'auto'" in source
