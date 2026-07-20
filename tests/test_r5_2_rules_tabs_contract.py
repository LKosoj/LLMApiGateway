import re
from pathlib import Path


EDITOR_SOURCE = Path("frontend/editor/src")
RULES_HTML = Path("static/rules-editor.html")


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


def test_rules_editor_uses_bespoke_manual_tabs_controller() -> None:
    source = _editor_source()

    # The top `.tabs` tablist is gone, so the shared `window.gatewayUi.createTabs()`
    # helper (which requires pre-existing `[role="tab"]` markup) can no longer bind
    # to the rules editor; a bespoke controller replicates just the manual-activation
    # contract (veto hook, activate/reselect hooks, repair-on-hidden) against the
    # sidebar's `.editor-entity-item` buttons instead.
    assert "ctx.state.rulesTabsController = window.gatewayUi.createTabs" not in source
    assert "export function createRulesTabsController(ctx)" in source
    assert "ctx.state.rulesTabsController = createRulesTabsController(ctx);" in source
    assert "ctx.beforeRulesTabActivate" in source
    assert "ctx.activateRulesTab" in source
    assert "ctx.reselectRulesTab" in source
    assert "async function repair()" in source
    assert "document.querySelectorAll('.editor-entity-item')" in source
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
    elements_block = re.search(
        r"export function createEditorElements\(\) \{(.*?)\n\}", source, re.DOTALL
    )

    assert elements_block is not None
    panels = set(re.findall(r"editorContainer[A-Za-z]+", elements_block.group(1)))
    assert panels == {
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
    }

    # The top tablist is gone; the sidebar is now the sole place the twelve
    # entity keys are declared, grouped by category (Providers/Fallback/
    # Operation/Fusion/Router/Pricing) rather than in the old top-tab order.
    sidebar_match = re.search(r'<aside class="editor-sidebar".*?</aside>', html, re.DOTALL)
    assert sidebar_match is not None
    assert re.findall(r'data-entity-target="([^"]+)"', sidebar_match.group(0)) == [
        "providers",
        "rules",
        "model-rules",
        "embeddings",
        "rerank",
        "images",
        "audio",
        "web",
        "fusion",
        "router",
        "openrouter-free",
        "fallback-eval",
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
