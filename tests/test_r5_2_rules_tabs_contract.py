import re
from pathlib import Path


EDITOR_SOURCE = Path("frontend/editor/src")
RULES_HTML = Path("static/rules-editor.html")


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


def test_rules_editor_uses_shared_manual_tabs_controller() -> None:
    source = _editor_source()

    assert "window.gatewayUi.createTabs(ctx.elements.rulesTabList" in source
    assert "activation: 'manual'" in source
    assert "beforeActivate: ctx.beforeRulesTabActivate" in source
    assert "onActivate: ctx.activateRulesTab" in source
    assert "onReselect: ctx.reselectRulesTab" in source
    assert "panels: ctx.elements.rulesTabPanels" in source
    assert "updateRulesTabA11y" not in source
    assert "tabRules.addEventListener('click'" not in source


def test_rules_editor_preserves_veto_retry_polling_and_repair_contracts() -> None:
    source = _editor_source()

    assert "context.previousKey !== context.key" in source
    assert "isCurrentEditorDirty()" in source
    assert "return confirm(" in source
    assert "ctx.state.providersLoadState === 'loading'" in source
    assert "ctx.state.providersLoadState === 'error'" in source
    assert "ctx.stopOpenRouterFreePolling();" in source
    assert "ctx.stopFallbackEvalPolling();" in source
    assert "context.signal.aborted" in source
    assert "context.isCurrent()" in source
    assert "await ctx.state.rulesTabsController.repair();" in source


def test_rules_editor_declares_all_twelve_keys_and_panels_in_tab_order() -> None:
    source = _editor_source()
    html = RULES_HTML.read_text(encoding="utf-8")
    panel_block = re.search(r"const rulesTabPanels = \[(.*?)\];", source, re.DOTALL)

    assert panel_block is not None
    panels = re.findall(r"editorContainer[A-Za-z]+", panel_block.group(1))
    assert panels == [
        "editorContainerRules",
        "editorContainerEmbeddings",
        "editorContainerRerank",
        "editorContainerImages",
        "editorContainerAudio",
        "editorContainerWeb",
        "editorContainerFusion",
        "editorContainerRouter",
        "editorContainerModelRules",
        "editorContainerOpenRouterFree",
        "editorContainerFallbackEval",
        "editorContainerProviders",
    ]
    assert re.findall(r'data-tab="([^"]+)" role="tab"', html) == [
        "rules",
        "embeddings",
        "rerank",
        "images",
        "audio",
        "web",
        "fusion",
        "router",
        "model-rules",
        "openrouter-free",
        "fallback-eval",
        "providers",
    ]


def test_rules_editor_initial_document_load_is_dispatched_once_by_tabs() -> None:
    source = _editor_source()
    init_block = re.search(
        r"async function initEditor\(\) \{(.*?)\n    \}",
        source,
        re.DOTALL,
    )

    assert init_block is not None
    assert init_block.group(1).count(
        "await ctx.state.rulesTabsController.activate(ctx.state.activeEditor, { reason: 'initial' });"
    ) == 1
    assert "loadRulesEditor" not in init_block.group(1)
