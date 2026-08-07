from __future__ import annotations

import json
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
EDITOR_JS = ROOT / "static" / "editor.js"
EDITOR_SOURCE = ROOT / "frontend" / "editor" / "src"
EDITOR_HTML = ROOT / "static" / "rules-editor.html"


def _editor_source() -> str:
    return "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted(EDITOR_SOURCE.glob("*.mjs"))
    )


def test_editor_javascript_has_one_revision_per_physical_document() -> None:
    result = subprocess.run(
        ["node", "--check", str(EDITOR_JS)],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    source = _editor_source()
    assert "const documentBases = new Map" in source
    for document_name in (
        "fallback",
        "operation",
        "fusion",
        "router",
        "providers",
        "model",
    ):
        assert f"'{document_name}'" in source
    for section_name in ("embeddings", "rerank", "images", "audio", "web"):
        assert f"'{section_name}'" not in source.partition("const documentBases = new Map")[2].partition(");")[0]


def test_editor_saves_use_if_match_without_pre_save_operation_get() -> None:
    source = _editor_source()

    assert "'If-Match': base.etag" in source
    assert "getOperationBasePayload()" in source
    for function_name in (
        "saveEmbeddings",
        "saveRerank",
        "saveImages",
        "saveAudio",
        "saveWeb",
    ):
        function_source = source.split(f"async function {function_name}()", 1)[1].split("\n    }", 1)[0]
        assert "fetchOperationRulesPayload" not in function_source
        assert "getOperationBasePayload()" in function_source


def test_all_six_document_save_paths_use_the_shared_cas_boundary() -> None:
    source = _editor_source()

    for document_name, endpoint in (
        ("fallback", "/v1/config/models-rules/structured"),
        ("operation", "/v1/config/model-operations/structured"),
        ("fusion", "/v1/config/fusion-rules/structured"),
        ("router", "/v1/config/router-rules/structured"),
        ("providers", "/v1/config/providers/structured"),
        ("model", "/v1/config/model-rules"),
    ):
        assert f"'{document_name}',\n" in source
        assert f"'{endpoint}',\n" in source
    assert "async function saveConfigDocument" in source
    assert "async function saveOperationPayload" in source


def test_editor_conflict_panel_uses_strict_paired_i18n_catalogs() -> None:
    html = EDITOR_HTML.read_text(encoding="utf-8")
    assert 'data-i18n-page="rules-editor"' in html
    assert '/static/vendor/ui-runtime.bundle.js' in html
    assert 'id="editorConflictState"' in html
    assert 'role="alert"' in html
    assert 'tabindex="-1"' in html
    assert 'id="reloadEditorDocumentButton"' in html
    assert 'id="cancelBusyRetryButton"' in html

    catalogs = {
        locale: json.loads(
            (ROOT / "static" / "locales" / locale / "editor.json").read_text(
                encoding="utf-8"
            )
        )
        for locale in ("en", "ru")
    }
    assert catalogs["en"].keys() == catalogs["ru"].keys()
    assert catalogs["en"]["conflict"].keys() == catalogs["ru"]["conflict"].keys()
    assert set(catalogs["en"]["conflict"]) == {
        "title",
        "message",
        "reload",
        "outOfSyncTitle",
        "outOfSyncMessage",
        "resync",
        "resynced",
        "resyncFailed",
        "busyTitle",
        "busyMessage",
        "busyExhausted",
        "busyRetry",
        "busyCancel",
    }
    assert catalogs["en"]["catalog"].keys() == catalogs["ru"]["catalog"].keys()
    assert set(catalogs["en"]["catalog"]) == {
        "empty",
        "error",
        "idle",
        "loading",
        "noProvider",
        "ready",
        "retry",
        "selected",
        "unavailable",
    }


def test_busy_generation_retries_the_same_request_instead_of_reloading() -> None:
    source = _editor_source()

    mode_source = source.split("function conflictModeFor(body)", 1)[1].split(
        "\n    }",
        1,
    )[0]
    assert "'config_sources_out_of_sync'" in mode_source
    assert "'config_generation_busy'" in mode_source
    assert "return 'busy';" in mode_source
    assert "return 'revision';" in mode_source

    save_source = source.split("async function saveConfigDocument", 1)[1].split(
        "\n    }",
        1,
    )[0]
    # The replay must reuse the base captured before the loop: a busy generation
    # is not a content conflict, so re-reading the base would silently widen the
    # compare-and-swap window.
    assert "getDocumentBase(documentName)" not in save_source.split(
        "for (let busyAttempts = 0",
        1,
    )[1]
    assert "'If-Match': base.etag" in save_source
    assert "busyAttempts >= ctx.constants.BUSY_RETRY_ATTEMPTS" in save_source
    assert "await waitForBusyRetry()" in save_source

    # Cancelling the wait needs its own control: the reload button is disabled
    # by syncInteractionLock for the whole save, so it can never serve as one.
    lock_source = source.split("function syncInteractionLock()", 1)[1].split(
        "\n    }",
        1,
    )[0]
    assert "#reloadEditorDocumentButton" in lock_source
    assert "cancelBusyRetryButton" not in lock_source
    assert "ctx.cancelBusyRetry();" in source

    # The busy notice must not offer a reload: the document on disk never
    # changed, so reloading it only discards the operator's edits.
    busy_copy = source.split("        busy: {", 1)[1].split("        },", 1)[0]
    assert "action:" not in busy_copy
    assert "reloadEditorDocumentButton.hidden = waitingForRetry || !copy.action" in source


def test_editor_errors_are_bounded_and_never_insert_raw_html() -> None:
    source = _editor_source()

    assert "MAX_SAFE_ERROR_LENGTH" in source
    assert "safeResponseError" in source
    for unsafe_sink in (".innerHTML", ".outerHTML", "insertAdjacentHTML", "document.write"):
        assert unsafe_sink not in source


def test_editor_exposes_dirty_state_and_guards_late_loads() -> None:
    source = _editor_source()
    css = (ROOT / "static" / "editor.css").read_text(encoding="utf-8")

    assert "editorMutationVersion" in source
    assert "loadRequestIds" in source
    assert "requestMutationVersion" in source
    assert "data-editor-dirty" in source
    assert '#saveButton[data-editor-dirty="true"]' in css
    assert ".editor-conflict-state:focus-visible" in css


def test_async_apply_is_inert_and_rechecked_before_base_commit() -> None:
    source = _editor_source()
    load_source = source.split("async function loadConfigDocument", 1)[1].split(
        "async function saveConfigDocument", 1
    )[0]

    assert "lockedSubtrees" in source
    assert ".inert = true" in source
    assert "await application" in load_source
    post_apply_source = load_source.split("await application", 1)[1]
    assert "loadRequestIds.get(documentName) !== requestId" in post_apply_source
    assert "editorMutationVersion !== requestMutationVersion" in post_apply_source
    assert post_apply_source.index("editorMutationVersion !== requestMutationVersion") < (
        post_apply_source.index("commitDocumentBase")
    )


def test_drag_reordering_fails_closed_during_lock_and_marks_mutation() -> None:
    source = _editor_source()
    reorder_source = source.split("function setupRowReordering", 1)[1].split(
        "function createMoveButtons", 1
    )[0]

    assert "lockedDraggables" in source
    assert reorder_source.count("isInteractionLocked()") >= 2
    assert "row.draggable = false" in source
    assert "editorMutationVersion += 1" in reorder_source
    assert "updateDirtyIndicator()" in reorder_source
