from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import (
    Browser,
    BrowserContext,
    Page,
    expect,
)

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "editor-concurrency-master"
OPERATION_PATH = "/v1/config/model-operations/structured"


def _write_config(root: Path) -> None:
    (root / "providers.json").write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {
                            "embed-model": {},
                            "embed-model-2": {},
                            "rerank-model": {},
                        },
                    }
                },
                {
                    "secondary": {
                        "baseUrl": "https://secondary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"secondary-model": {}},
                    }
                },
            ]
        ),
        encoding="utf-8",
    )
    (root / "models_fallback_rules.json").write_text("[]\n", encoding="utf-8")
    (root / "models_operation_rules.json").write_text(
        json.dumps(
            {
                "embeddings": [
                    {
                        "gateway_model_name": "embed-base",
                        "routes": [
                            {
                                "provider": "primary",
                                "model": "embed-model",
                                "target_path": "/embeddings",
                            },
                            {
                                "provider": "primary",
                                "model": "embed-model-2",
                                "target_path": "/embeddings",
                            },
                        ],
                    }
                ],
                "rerank": [
                    {
                        "gateway_model_name": "rerank-base",
                        "routes": [
                            {
                                "provider": "primary",
                                "model": "rerank-model",
                                "target_path": "/score",
                            }
                        ],
                    }
                ],
                "images_generations": [],
                "images_edits": [],
            }
        ),
        encoding="utf-8",
    )
    for filename, content in (
        ("models_model_rules.json", "{}\n"),
        ("models_fusion_rules.json", "[]\n"),
        ("models_router_rules.json", "[]\n"),
    ):
        (root / filename).write_text(content, encoding="utf-8")


@pytest.fixture
def editor_server(tmp_path: Path) -> Iterator[str]:
    _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(tmp_path / "providers.json"),
            "FALLBACK_RULES_FILENAME": str(
                tmp_path / "models_fallback_rules.json"
            ),
            "MODEL_RULES_FILENAME": str(tmp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(
                tmp_path / "models_operation_rules.json"
            ),
            "FUSION_RULES_FILENAME": str(tmp_path / "models_fusion_rules.json"),
            "ROUTER_RULES_FILENAME": str(tmp_path / "models_router_rules.json"),
            "SESSION_COOKIE_SECURE": "false",
            "VERIFY_MODELS_ON_STARTUP": "off",
            "LOG_LEVEL": "WARNING",
        }
    )
    base_url = f"http://127.0.0.1:{port}"

    with isolated_gateway_process(env=env, temp_path=tmp_path) as process:
        wait_for_gateway(base_url, process)
        yield base_url


def _login(context: BrowserContext, base_url: str) -> None:
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/rules-editor"},
    )
    assert response.status == 200


def _open_editor(
    context: BrowserContext,
    base_url: str,
    tab: str,
) -> tuple[Page, list[tuple[str, str | None]], str]:
    page = context.new_page()
    catalog_requests: list[str] = []

    def serve_primary_models(route) -> None:
        catalog_requests.append(route.request.url)
        route.fulfill(
            json={
                "provider": "primary",
                "models": [
                    {"id": "embed-model"},
                    {"id": "embed-model-2"},
                    {"id": "rerank-model"},
                ],
            }
        )

    page.route(
        "**/v1/config/providers/primary/models",
        serve_primary_models,
    )
    operation_requests: list[tuple[str, str | None]] = []

    def record_request(request) -> None:
        if request.url.endswith(OPERATION_PATH):
            operation_requests.append(
                (request.method, request.headers.get("if-match"))
            )

    page.on("request", record_request)
    page.goto(f"{base_url}/v1/ui/rules-editor")
    expect(page.locator("#saveButton")).to_be_enabled()
    entity_target = "embeddings" if tab == "embeddings" else "rerank"
    message = "Embeddings Routes" if tab == "embeddings" else "Rerank Routes"
    with page.expect_response(
        lambda response: response.url.endswith(OPERATION_PATH)
        and response.request.method == "GET"
    ) as loaded_response:
        page.locator(f'[data-entity-target="{entity_target}"]').click()
    assert loaded_response.value.status == 200
    etag = loaded_response.value.headers["etag"]
    expect(page.locator("#messageArea")).to_contain_text(
        f"{message} loaded successfully"
    )
    assert catalog_requests == []
    return page, operation_requests, etag


def _gateway_name(page: Page, section: str):
    return page.locator(f"#editor-container-{section} .gateway-model-input").first


def _expand_first_card(page: Page, section: str) -> None:
    card = page.locator(f"#editor-container-{section} .rule-card").first
    if "collapsed" in (card.get_attribute("class") or "").split():
        card.locator(".accordion-toggle").click()


def _beforeunload_is_blocked(page: Page) -> bool:
    return bool(
        page.evaluate(
            """
            () => {
                const event = new Event('beforeunload', {cancelable: true});
                window.dispatchEvent(event);
                return event.defaultPrevented;
            }
            """
        )
    )


def test_operation_conflicts_preserve_local_state_and_explicit_reload(
    browser: Browser,
    editor_server: str,
) -> None:
    context_a = browser.new_context(locale="en-US")
    context_b = browser.new_context(locale="en-US")
    try:
        _login(context_a, editor_server)
        _login(context_b, editor_server)
        page_a, requests_a, etag_a = _open_editor(
            context_a, editor_server, "embeddings"
        )
        page_b, requests_b, etag_b = _open_editor(
            context_b, editor_server, "embeddings"
        )
        page_b_rerank, requests_b_rerank, etag_b_rerank = _open_editor(
            context_b, editor_server, "rerank"
        )

        initial_etags = {etag_a, etag_b, etag_b_rerank}
        assert len(initial_etags) == 1
        base_etag = initial_etags.pop()
        assert base_etag is not None

        _expand_first_card(page_a, "embeddings")
        _gateway_name(page_a, "embeddings").fill("embed-from-a")
        save_start = len(requests_a)
        with page_a.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ) as saved_response:
            page_a.locator("#saveButton").click()
        assert saved_response.value.status == 200
        next_etag = saved_response.value.headers["etag"]
        assert next_etag != base_etag
        assert requests_a[save_start:] == [("POST", base_etag)]
        expect(page_a.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )

        _expand_first_card(page_b, "embeddings")
        _gateway_name(page_b, "embeddings").fill("embed-from-b")
        same_section_start = len(requests_b)
        with page_b.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ) as same_section_response:
            page_b.locator("#saveButton").click()
        assert same_section_response.value.status == 409
        assert requests_b[same_section_start:] == [("POST", base_etag)]
        conflict = page_b.locator("#editorConflictState")
        expect(conflict).to_be_visible()
        expect(conflict).to_be_focused()
        expect(_gateway_name(page_b, "embeddings")).to_have_value("embed-from-b")
        expect(
            page_b.locator("#editor-container-embeddings .model-input").first
        ).to_have_value("embed-model")
        expect(page_b.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "true"
        )
        assert _beforeunload_is_blocked(page_b)

        repeated_start = len(requests_b)
        page_b.locator("#saveButton").click()
        expect(conflict).to_be_focused()
        assert requests_b[repeated_start:] == [("POST", base_etag)]
        expect(_gateway_name(page_b, "embeddings")).to_have_value("embed-from-b")

        _expand_first_card(page_b_rerank, "rerank")
        _gateway_name(page_b_rerank, "rerank").fill("rerank-from-b")
        different_section_start = len(requests_b_rerank)
        with page_b_rerank.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ) as different_section_response:
            page_b_rerank.locator("#saveButton").click()
        assert different_section_response.value.status == 409
        assert requests_b_rerank[different_section_start:] == [
            ("POST", base_etag)
        ]
        expect(page_b_rerank.locator("#editorConflictState")).to_be_focused()
        expect(_gateway_name(page_b_rerank, "rerank")).to_have_value(
            "rerank-from-b"
        )

        page_b.evaluate(
            """
            () => {
                const originalFetch = window.fetch.bind(window);
                let delayed = false;
                window.fetch = (input, options = {}) => {
                    const url = typeof input === 'string' ? input : input.url;
                    const method = String(options.method || 'GET').toUpperCase();
                    if (!delayed && url.endsWith('/v1/config/model-operations/structured') && method === 'GET') {
                        delayed = true;
                        return new Promise((resolve, reject) => {
                            window.__releaseEditorGet = () => {
                                originalFetch(input, options).then(resolve, reject);
                            };
                        });
                    }
                    return originalFetch(input, options);
                };
            }
            """
        )
        delayed_reload_start = len(requests_b)
        page_b.locator("#reloadEditorDocumentButton").click()
        page_b.wait_for_function("typeof window.__releaseEditorGet === 'function'")
        assert page_b.locator("#editor-container-embeddings").evaluate(
            "element => element.inert"
        )
        page_b.evaluate(
            """
            () => {
                const input = document.querySelector('#editor-container-embeddings .gateway-model-input');
                input.value = 'edit-during-load';
                input.dispatchEvent(new Event('input', {bubbles: true}));
                window.__releaseEditorGet();
            }
            """
        )
        page_b.wait_for_function(
            "() => !document.querySelector('#editor-container-embeddings').inert"
        )
        expect(_gateway_name(page_b, "embeddings")).to_have_value(
            "edit-during-load"
        )
        expect(
            page_b.locator("#editor-container-embeddings .model-input").first
        ).to_have_value("embed-model")
        expect(conflict).to_be_visible()
        assert requests_b[delayed_reload_start:] == [("GET", None)]

        accepted_reload_start = len(requests_b)
        page_b.locator("#reloadEditorDocumentButton").click()
        expect(conflict).to_be_hidden()
        expect(_gateway_name(page_b, "embeddings")).to_have_value("embed-from-a")
        expect(
            page_b.locator("#editor-container-embeddings .model-input").first
        ).to_have_value("embed-model")
        assert requests_b[accepted_reload_start:] == [("GET", None)]
        expect(page_b.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )
        assert not _beforeunload_is_blocked(page_b)

        malicious_message = "<img src=x onerror=window.__unsafeEditorHtml=true>" + (
            "x" * 500
        )

        def reject_malicious_save(route) -> None:
            if route.request.method == "POST":
                route.fulfill(
                    status=422,
                    json={
                        "detail": {
                            "code": "VALIDATION_FAILED",
                            "message": malicious_message,
                        }
                    },
                )
                return
            route.continue_()

        page_b.route(f"**{OPERATION_PATH}", reject_malicious_save)
        _gateway_name(page_b, "embeddings").fill("malicious-error-attempt")
        page_b.locator("#saveButton").click()
        raw_detail = page_b.locator("#messageRawDetail")
        expect(raw_detail).to_contain_text("<img src=x")
        expect(raw_detail).to_have_attribute("lang", "und")
        expect(raw_detail).to_have_attribute("dir", "auto")
        expect(raw_detail.locator("img")).to_have_count(0)
        assert len(raw_detail.inner_text()) <= 240
        assert not page_b.evaluate("window.__unsafeEditorHtml === true")
    finally:
        context_b.close()
        context_a.close()


@pytest.mark.parametrize("etag", [None, 'W/"weak-revision"'])
def test_missing_or_weak_config_etag_keeps_save_disabled(
    browser: Browser,
    editor_server: str,
    etag: str | None,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        _login(context, editor_server)
        page = context.new_page()

        def serve_rules_without_strong_etag(route) -> None:
            headers = {"ETag": etag} if etag is not None else {}
            route.fulfill(
                json={"rules": [], "providers": ["primary"]},
                headers=headers,
            )

        page.route(
            "**/v1/config/models-rules/structured",
            serve_rules_without_strong_etag,
        )
        page.goto(f"{editor_server}/v1/ui/rules-editor")
        expect(page.locator("#messageArea")).to_contain_text(
            "omitted a valid revision"
        )
        raw_detail = page.locator("#messageRawDetail")
        expect(raw_detail).to_be_hidden()
        expect(raw_detail).to_have_attribute("lang", "und")
        expect(raw_detail).to_have_attribute("dir", "auto")
        expect(page.locator("#saveButton")).to_be_disabled()
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )
    finally:
        context.close()


def test_lazy_catalog_retry_locale_and_stale_response_isolation(
    browser: Browser,
    editor_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        _login(context, editor_server)
        page, operation_requests, initial_etag = _open_editor(
            context, editor_server, "embeddings"
        )
        primary_pattern = "**/v1/config/providers/primary/models"
        page.unroute(primary_pattern)
        primary_requests: list[str] = []
        pending_primary: list[object] = []
        secondary_requests: list[str] = []

        def handle_primary_models(route) -> None:
            primary_requests.append(route.request.url)
            if len(primary_requests) == 1:
                route.fulfill(status=503, json={"detail": "temporary failure"})
                return
            pending_primary.append(route)

        def handle_secondary_models(route) -> None:
            secondary_requests.append(route.request.url)
            route.fulfill(
                json={
                    "provider": "secondary",
                    "models": [{"id": "secondary-model"}],
                }
            )

        page.route(primary_pattern, handle_primary_models)
        page.route(
            "**/v1/config/providers/secondary/models",
            handle_secondary_models,
        )

        _expand_first_card(page, "embeddings")
        rows = page.locator(
            "#editor-container-embeddings .fallback-list > .fallback-row"
        )
        statuses = rows.locator(".model-status")
        models = rows.locator(".model-input")
        expect(statuses).to_have_count(2)
        expect(statuses.nth(0)).to_have_attribute("data-state", "error")
        expect(statuses.nth(1)).to_have_attribute("data-state", "error")
        assert len(primary_requests) == 1
        assert models.evaluate_all("inputs => inputs.map(input => input.value)") == [
            "embed-model",
            "embed-model-2",
        ]
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )

        request_count = len(primary_requests)
        page.evaluate("() => window.gatewayI18n.changeLanguage('ru')")
        expect(statuses.nth(0)).to_contain_text("Не удалось загрузить модели")
        expect(statuses.nth(0).locator(".model-catalog-retry")).to_have_text(
            "Повторить"
        )
        assert len(primary_requests) == request_count
        assert models.evaluate_all("inputs => inputs.map(input => input.value)") == [
            "embed-model",
            "embed-model-2",
        ]
        page.evaluate("() => window.gatewayI18n.changeLanguage('en')")

        with page.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ) as saved_response:
            page.locator("#saveButton").click()
        assert saved_response.value.status == 200
        assert operation_requests == [("GET", None), ("POST", initial_etag)]
        saved_payload = context.request.get(f"{editor_server}{OPERATION_PATH}").json()
        assert [
            route["model"] for route in saved_payload["embeddings"][0]["routes"]
        ] == ["embed-model", "embed-model-2"]
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )

        statuses.nth(0).locator(".model-catalog-retry").click()
        statuses.nth(1).locator(".model-catalog-retry").click()
        expect(statuses.nth(0)).to_have_attribute("data-state", "loading")
        expect(statuses.nth(1)).to_have_attribute("data-state", "loading")
        assert len(primary_requests) == 2
        assert len(pending_primary) == 1

        page.locator('[data-entity-target="providers"]').click()
        expect(page.locator("#messageArea")).to_contain_text(
            "Providers loaded successfully"
        )
        with page.expect_response(
            lambda response: response.url.endswith(
                "/v1/config/providers/structured"
            )
            and response.request.method == "POST"
        ) as providers_saved:
            page.locator("#saveButton").click()
        assert providers_saved.value.status == 200
        expect(page.locator("#messageArea")).to_contain_text(
            "updated successfully"
        )

        with page.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "GET"
        ):
            page.locator('[data-entity-target="embeddings"]').click()
        expect(page.locator("#messageArea")).to_contain_text(
            "Embeddings Routes loaded successfully"
        )
        _expand_first_card(page, "embeddings")
        rows = page.locator(
            "#editor-container-embeddings .fallback-list > .fallback-row"
        )
        statuses = rows.locator(".model-status")
        models = rows.locator(".model-input")
        expect(statuses.nth(0)).to_have_attribute("data-state", "loading")
        expect(statuses.nth(1)).to_have_attribute("data-state", "loading")
        assert len(primary_requests) == 3
        assert len(pending_primary) == 2

        pending_primary[0].fulfill(
            json={
                "provider": "primary",
                "models": [{"id": "obsolete-model"}],
            }
        )
        page.evaluate(
            "() => new Promise(resolve => requestAnimationFrame("
            "() => requestAnimationFrame(resolve)))"
        )
        expect(statuses.nth(0)).to_have_attribute("data-state", "loading")
        expect(statuses.nth(1)).to_have_attribute("data-state", "loading")

        page.locator(
            "#editor-container-embeddings .add-fallback-button"
        ).first.click()
        expect(rows).to_have_count(3)
        rows.nth(2).locator(".provider-select").select_option("primary")
        expect(statuses.nth(2)).to_have_attribute("data-state", "loading")
        assert len(primary_requests) == 3

        rows.nth(0).locator(".provider-select").select_option("secondary")
        expect(statuses.nth(0)).to_have_attribute("data-state", "ready")
        expect(models.nth(0)).to_have_value("")
        assert len(secondary_requests) == 1
        expect(statuses.nth(1)).to_have_attribute("data-state", "loading")
        expect(statuses.nth(2)).to_have_attribute("data-state", "loading")

        pending_primary[1].fulfill(
            json={
                "provider": "primary",
                "models": [
                    {"id": "embed-model"},
                    {"id": "embed-model-2"},
                ],
            }
        )
        expect(statuses.nth(1)).to_have_attribute("data-state", "ready")
        expect(statuses.nth(2)).to_have_attribute("data-state", "ready")
        expect(models.nth(1)).to_have_value("embed-model-2")
        expect(rows.nth(2).locator("datalist option")).to_have_count(2)
        expect(
            rows.nth(2).locator("datalist option[value='obsolete-model']")
        ).to_have_count(0)
        expect(statuses.nth(0)).to_have_attribute("data-state", "ready")
        expect(rows.nth(0).locator(".provider-select")).to_have_value("secondary")
        expect(models.nth(0)).to_have_value("")
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "true"
        )
        assert _beforeunload_is_blocked(page)
    finally:
        context.close()


def test_delayed_save_blocks_drag_and_completed_drag_marks_dirty(
    browser: Browser,
    editor_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        _login(context, editor_server)
        page, operation_requests, initial_etag = _open_editor(
            context, editor_server, "embeddings"
        )
        _expand_first_card(page, "embeddings")
        route_rows = page.locator(
            "#editor-container-embeddings .fallback-list > .fallback-row"
        )
        expect(route_rows).to_have_count(2)
        initial_order = route_rows.locator(".model-input").evaluate_all(
            "inputs => inputs.map(input => input.value)"
        )
        assert initial_order == ["embed-model", "embed-model-2"]

        page.evaluate(
            """
            () => {
                const rows = document.querySelectorAll(
                    '#editor-container-embeddings .fallback-list > .fallback-row'
                );
                rows[1].draggable = false;
                document.querySelector('#addEmbeddingButton').disabled = true;
                document.querySelector('[data-entity-target="rerank"]').disabled = true;
            }
            """
        )
        _gateway_name(page, "embeddings").fill("embed-during-save")

        delayed_post: dict[str, object] = {}

        def delay_first_post(route) -> None:
            if route.request.method == "POST" and not delayed_post:
                delayed_post["route"] = route
                return
            route.continue_()

        page.route(f"**{OPERATION_PATH}", delay_first_post)
        page.locator("#saveButton").click()
        page.wait_for_function(
            "document.querySelector('#editor-container-embeddings').inert"
        )

        assert route_rows.evaluate_all("rows => rows.map(row => row.draggable)") == [
            False,
            False,
        ]
        blocked_drag = page.evaluate(
            """
            () => {
                const rows = Array.from(document.querySelectorAll(
                    '#editor-container-embeddings .fallback-list > .fallback-row'
                ));
                const transfer = new DataTransfer();
                const start = new DragEvent('dragstart', {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: transfer,
                });
                const target = rows[1].getBoundingClientRect();
                const over = new DragEvent('dragover', {
                    bubbles: true,
                    cancelable: true,
                    clientY: target.bottom + 1,
                    dataTransfer: transfer,
                });
                rows[0].dispatchEvent(start);
                rows[1].dispatchEvent(over);
                return {
                    startPrevented: start.defaultPrevented,
                    overPrevented: over.defaultPrevented,
                    draggedRow: window._draggedRow || null,
                    order: rows[0].parentNode
                        ? Array.from(rows[0].parentNode.querySelectorAll('.model-input'))
                            .map(input => input.value)
                        : [],
                };
            }
            """
        )
        assert blocked_drag == {
            "startPrevented": True,
            "overPrevented": True,
            "draggedRow": None,
            "order": initial_order,
        }
        assert context.request.get(f"{editor_server}{OPERATION_PATH}").json()[
            "embeddings"
        ][0]["gateway_model_name"] == "embed-base"

        with page.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ) as saved_response:
            delayed_post["route"].continue_()
        expect(page.locator("#messageArea")).to_contain_text(
            "updated successfully"
        )
        next_etag = saved_response.value.headers["etag"]
        assert operation_requests[1:] == [("POST", initial_etag)]
        assert not page.locator("#editor-container-embeddings").evaluate(
            "element => element.inert"
        )
        assert route_rows.evaluate_all("rows => rows.map(row => row.draggable)") == [
            True,
            False,
        ]
        expect(page.locator("#addEmbeddingButton")).to_be_disabled()
        expect(page.locator('[data-entity-target="rerank"]')).to_be_disabled()
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )
        assert not _beforeunload_is_blocked(page)

        completed_drag = page.evaluate(
            """
            () => {
                const rows = Array.from(document.querySelectorAll(
                    '#editor-container-embeddings .fallback-list > .fallback-row'
                ));
                const transfer = new DataTransfer();
                rows[0].dispatchEvent(new DragEvent('dragstart', {
                    bubbles: true,
                    cancelable: true,
                    dataTransfer: transfer,
                }));
                const target = rows[1].getBoundingClientRect();
                rows[1].dispatchEvent(new DragEvent('dragover', {
                    bubbles: true,
                    cancelable: true,
                    clientY: target.bottom + 1,
                    dataTransfer: transfer,
                }));
                rows[0].dispatchEvent(new DragEvent('dragend', {
                    bubbles: true,
                    dataTransfer: transfer,
                }));
                return Array.from(rows[0].parentNode.querySelectorAll('.model-input'))
                    .map(input => input.value);
            }
            """
        )
        assert completed_drag == list(reversed(initial_order))
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "true"
        )
        assert _beforeunload_is_blocked(page)

        with page.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
        ):
            page.locator("#saveButton").click()
        assert operation_requests[1:] == [
            ("POST", initial_etag),
            ("POST", next_etag),
        ]
        saved_payload = context.request.get(
            f"{editor_server}{OPERATION_PATH}"
        ).json()
        assert [
            route["model"] for route in saved_payload["embeddings"][0]["routes"]
        ] == list(reversed(initial_order))
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )
        assert not _beforeunload_is_blocked(page)
    finally:
        context.close()


# The retry pause is 15 s in production. Shrinking only the long timers keeps
# the real code path (window.setTimeout) under test without waiting for it.
_FAST_RETRY_TIMERS = """
const originalSetTimeout = window.setTimeout.bind(window);
window.setTimeout = (handler, delay, ...args) =>
    originalSetTimeout(handler, Number(delay) >= 5000 ? 20 : delay, ...args);
"""

_BUSY_BODY = {
    "detail": {
        "code": "config_generation_busy",
        "message": "The previous runtime generation is still retiring.",
    }
}


def _serve_busy_saves(page: Page, busy_saves: int) -> None:
    """Answer the first `busy_saves` POSTs with 409 config_generation_busy."""
    served = {"count": 0}

    def handle(route) -> None:
        if route.request.method != "POST":
            route.continue_()
            return
        if busy_saves < 0 or served["count"] < busy_saves:
            served["count"] += 1
            route.fulfill(status=409, json=_BUSY_BODY)
            return
        route.continue_()

    page.route(f"**{OPERATION_PATH}", handle)


def test_busy_generation_retries_the_save_with_the_same_if_match(
    browser: Browser,
    editor_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    context.add_init_script(_FAST_RETRY_TIMERS)
    try:
        _login(context, editor_server)
        page, requests, base_etag = _open_editor(context, editor_server, "embeddings")
        _serve_busy_saves(page, busy_saves=2)

        _expand_first_card(page, "embeddings")
        _gateway_name(page, "embeddings").fill("embed-after-busy")
        save_start = len(requests)
        with page.expect_response(
            lambda response: response.url.endswith(OPERATION_PATH)
            and response.request.method == "POST"
            and response.status == 200
        ) as saved_response:
            page.locator("#saveButton").click()

        assert saved_response.value.headers["etag"] != base_etag
        # Every replay carries the base revision: a busy generation never
        # invalidates the candidate the operator already validated against.
        assert requests[save_start:] == [("POST", base_etag)] * 3
        conflict = page.locator("#editorConflictState")
        expect(conflict).to_be_hidden()
        expect(_gateway_name(page, "embeddings")).to_have_value("embed-after-busy")
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "false"
        )
        assert not _beforeunload_is_blocked(page)

        saved_payload = context.request.get(f"{editor_server}{OPERATION_PATH}").json()
        assert (
            saved_payload["embeddings"][0]["gateway_model_name"]
            == "embed-after-busy"
        )
    finally:
        context.close()


def test_busy_retry_can_be_cancelled_and_keeps_local_edits(
    browser: Browser,
    editor_server: str,
) -> None:
    context = browser.new_context(locale="en-US")
    try:
        _login(context, editor_server)
        page, requests, base_etag = _open_editor(context, editor_server, "embeddings")
        _serve_busy_saves(page, busy_saves=-1)

        _expand_first_card(page, "embeddings")
        _gateway_name(page, "embeddings").fill("embed-cancelled-wait")
        save_start = len(requests)
        page.locator("#saveButton").click()

        conflict = page.locator("#editorConflictState")
        cancel_button = page.locator("#cancelBusyRetryButton")
        reload_button = page.locator("#reloadEditorDocumentButton")
        expect(conflict).to_be_visible()
        expect(conflict).to_have_attribute("data-mode", "busy")
        expect(conflict).to_have_attribute("data-waiting", "true")
        expect(conflict).to_contain_text("still serving requests")
        expect(cancel_button).to_be_visible()
        expect(reload_button).to_be_hidden()
        expect(page.locator("#messageArea")).to_contain_text("retrying the save")

        cancel_button.click()
        expect(conflict).to_have_attribute("data-waiting", "false")
        expect(conflict).to_contain_text("Automatic retries did not help")
        expect(cancel_button).to_be_hidden()
        # A busy generation offers no reload: re-reading the untouched document
        # would only cost the operator their edits.
        expect(reload_button).to_be_hidden()

        # Cancelling stops the loop dead: no further save leaves the browser.
        assert requests[save_start:] == [("POST", base_etag)]
        expect(_gateway_name(page, "embeddings")).to_have_value(
            "embed-cancelled-wait"
        )
        expect(page.locator("#saveButton")).to_have_attribute(
            "data-editor-dirty", "true"
        )
        expect(page.locator("#saveButton")).to_be_enabled()
        assert _beforeunload_is_blocked(page)
    finally:
        context.close()
