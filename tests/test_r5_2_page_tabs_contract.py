from pathlib import Path


STATIC_DIR = Path(__file__).resolve().parents[1] / "static"


def _source(name: str) -> str:
    return (STATIC_DIR / name).read_text(encoding="utf-8")


def test_docs_tabs_use_the_shared_manual_controller() -> None:
    source = _source("gateway-docs.js")

    assert source.count("window.gatewayUi.createTabs") == 1
    assert 'getKey: (button) => button.dataset.docsTab || "api"' in source
    assert 'initialKey: "api"' in source
    assert 'activation: "manual"' in source
    assert "onActivate: activateDocsTab" in source
    assert "onReselect: activateDocsTab" in source
    assert 'button.addEventListener("click"' not in source
    assert "activationContext.signal.aborted || !activationContext.isCurrent()" in source


def test_usage_tabs_use_two_isolated_shared_controllers() -> None:
    source = _source("usage-stats.js")

    assert source.count("window.gatewayUi.createTabs") == 2
    assert "getKey: (button) => button.dataset.tab" in source
    assert "initialKey: 'analytics'" in source
    assert "panels: orderedTabContents" in source
    assert "getKey: (button) => button.dataset.subtab" in source
    assert "initialKey: 'summary'" in source
    assert "panels: orderedFallbackSubContents" in source
    assert source.count("activation: 'manual'") == 2
    assert "onReselect: activateUsageTab" in source
    assert "onReselect: activateFallbackSubTab" in source
    assert "tabButtons.forEach(button =>" not in source
    assert "fallbackSubTabs.forEach(btn =>" not in source
    assert "isActivationCurrent(activationContext)" in source
    assert "isFallbackLoadCurrent(activationContext)" in source


def test_playground_tabs_use_two_nested_shared_controllers() -> None:
    source = _source("web-playground.js")

    assert source.count("window.gatewayUi.createTabs") == 2
    assert "getKey: (button) => button.dataset.playgroundSectionTab" in source
    assert 'initialKey: "web"' in source
    assert "panels: sectionPanels" in source
    assert "getKey: (button) => button.dataset.webTab" in source
    assert 'initialKey: "search"' in source
    assert "panels: webPanels" in source
    assert source.count('activation: "manual"') == 2
    assert "onReselect: activateSection" in source
    assert "onReselect: activateWebTab" in source
    assert "SECTION_BUTTONS.forEach((btn)" not in source
    assert "WEB_TAB_BUTTONS.forEach((btn)" not in source
    assert "activationContext ? {signal: activationContext.signal} : undefined" in source
    assert "activationContext.signal.aborted || !activationContext.isCurrent()" in source
