"""Browser-level coverage for P2-2: Chat-first Playground + streaming + cancel.

Verifies, against a real gateway process with ``/v1/chat/completions`` mocked
at the browser network layer (``page.route``) to return an OpenAI-compatible
``text/event-stream`` body:
  * sending a message renders the user bubble immediately and the assistant
    bubble fills in progressively as SSE ``delta.content`` chunks arrive;
  * clicking Cancel mid-stream aborts the request, keeps the partial text
    already rendered, and marks the bubble as cancelled;
  * Enter in the message textarea submits the form, while Shift+Enter inserts
    a newline instead.
"""
from __future__ import annotations

import json
import os
from collections.abc import Iterator
from pathlib import Path

import pytest
from playwright.sync_api import Browser, BrowserContext, Route, expect

from tests.ui_server_helpers import (
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser
MASTER_KEY = "p2-2-playground-chat-master"

# Mirrors the client's CHAT_STREAM_CHUNK_DELAY_MS pacing (static/web-playground.js):
# each SSE delta is applied with a short delay so a Cancel click made mid-stream
# has a real window to land before the next delta renders.
STREAM_WORDS = [
    "Hello",
    " brave",
    " new",
    " world",
    " today",
    " and",
    " beyond",
    " truly",
    " forever",
    " indeed",
    " surely",
    " finally",
]
FULL_STREAM_TEXT = "".join(STREAM_WORDS)


def _sse_body(words: list[str]) -> str:
    frames = [
        f"data: {json.dumps({'choices': [{'delta': {'content': word}}]})}\n\n"
        for word in words
    ]
    frames.append("data: [DONE]\n\n")
    return "".join(frames)


def _write_config(root: Path) -> Path:
    providers_path = root / "providers.json"
    providers_path.write_text(
        json.dumps(
            [
                {
                    "primary": {
                        "baseUrl": "https://primary.invalid/v1",
                        "apikey": "test-only-key",
                        "models": {"chat": {"input_rate": 1.0, "output_rate": 2.0}},
                    }
                }
            ]
        ),
        encoding="utf-8",
    )
    sources = {
        "models_fallback_rules.json": json.dumps(
            [
                {
                    "gateway_model_name": "gateway/chat-stream",
                    "fallback_models": [
                        {"provider": "primary", "model": "chat"},
                    ],
                    "rotate_models": False,
                }
            ]
        ),
        "models_model_rules.json": "{}\n",
        "models_operation_rules.json": "{}\n",
        "models_fusion_rules.json": "[]\n",
        "models_router_rules.json": "[]\n",
    }
    for filename, content in sources.items():
        (root / filename).write_text(content, encoding="utf-8")
    return providers_path


@pytest.fixture
def chat_server(tmp_path: Path) -> Iterator[str]:
    providers_path = _write_config(tmp_path)
    port = get_free_port()
    env = os.environ.copy()
    env.update(
        {
            "GATEWAY_API_KEY": MASTER_KEY,
            "GATEWAY_HOST": "127.0.0.1",
            "GATEWAY_PORT": str(port),
            "FALLBACK_PROVIDER": "primary",
            "PROVIDERS_FILENAME": str(providers_path),
            "FALLBACK_RULES_FILENAME": str(tmp_path / "models_fallback_rules.json"),
            "MODEL_RULES_FILENAME": str(tmp_path / "models_model_rules.json"),
            "OPERATION_RULES_FILENAME": str(tmp_path / "models_operation_rules.json"),
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


def _login(browser: Browser, base_url: str) -> BrowserContext:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    response = context.request.post(
        f"{base_url}/auth/login",
        data={"api_key": MASTER_KEY, "next": "/v1/ui/playground"},
    )
    assert response.status == 200
    return context


def _open_chat_tab(page, base_url: str) -> None:
    page.goto(f"{base_url}/v1/ui/playground")
    page.locator('[data-playground-section-tab="chat"]').click()
    expect(page.locator("#simpleChatForm")).to_be_visible()


def _route_streaming_completion(page, words: list[str]) -> None:
    def handle(route: Route) -> None:
        route.fulfill(
            status=200,
            headers={"content-type": "text/event-stream"},
            body=_sse_body(words),
        )

    page.route("**/v1/chat/completions", handle)


def test_send_message_streams_assistant_reply_progressively(
    browser: Browser,
    chat_server: str,
) -> None:
    context = _login(browser, chat_server)
    try:
        page = context.new_page()
        _open_chat_tab(page, chat_server)
        _route_streaming_completion(page, STREAM_WORDS)

        page.locator("#simpleChatInput").fill("Say hello")
        page.locator("#simpleChatForm .run-button").click()

        expect(page.locator(".chat-message.user .chat-content")).to_contain_text(
            "Say hello"
        )
        # The assistant bubble progressively fills in as deltas stream, and
        # ends up with the full concatenated text once the stream completes.
        assistant_content = page.locator(".chat-message.assistant .chat-content")
        expect(assistant_content).to_contain_text(FULL_STREAM_TEXT, timeout=5000)

        # Streaming controls hide again and Send re-enables once done.
        expect(page.locator("#chatStreamControls")).to_be_hidden()
        expect(page.locator("#simpleChatForm .run-button")).to_be_enabled()
        # The message field is cleared as soon as the request was sent.
        expect(page.locator("#simpleChatInput")).to_have_value("")
    finally:
        context.close()


def test_cancel_during_stream_keeps_partial_text_and_marks_cancelled(
    browser: Browser,
    chat_server: str,
) -> None:
    context = _login(browser, chat_server)
    try:
        page = context.new_page()
        _open_chat_tab(page, chat_server)
        # Emulate a real progressive SSE stream via ReadableStream so the test
        # does not depend on any artificial client-side chunk delay.
        page.evaluate(
            """
            (words) => {
                const origFetch = window.fetch.bind(window);
                window.fetch = (input, init) => {
                    const url = typeof input === "string" ? input : input?.url || "";
                    if (!url.includes("/v1/chat/completions")) {
                        return origFetch(input, init);
                    }
                    const encoder = new TextEncoder();
                    const signal = init?.signal;
                    let timer = null;
                    const stream = new ReadableStream({
                        start(controller) {
                            const firstChunk = `data: ${JSON.stringify({choices: [{delta: {content: words[0]}}]})}\\n\\n`;
                            controller.enqueue(encoder.encode(firstChunk));
                            const onAbort = () => {
                                if (timer !== null) clearTimeout(timer);
                                try {
                                    controller.error(new DOMException("The user aborted a request.", "AbortError"));
                                } catch (_err) {
                                    // Stream already closed.
                                }
                            };
                            if (signal) {
                                if (signal.aborted) {
                                    onAbort();
                                    return;
                                }
                                signal.addEventListener("abort", onAbort, {once: true});
                            }
                            timer = setTimeout(() => {
                                const rest = words.slice(1).map(
                                    (word) => `data: ${JSON.stringify({choices: [{delta: {content: word}}]})}\\n\\n`
                                ).join("") + "data: [DONE]\\n\\n";
                                try {
                                    controller.enqueue(encoder.encode(rest));
                                    controller.close();
                                } catch (_err) {
                                    // Stream already aborted.
                                }
                            }, 5000);
                        },
                        cancel() {
                            if (timer !== null) clearTimeout(timer);
                        },
                    });
                    return Promise.resolve(new Response(stream, {
                        status: 200,
                        headers: {"content-type": "text/event-stream"},
                    }));
                };
            }
            """,
            STREAM_WORDS,
        )

        page.locator("#simpleChatInput").fill("Say hello")
        page.locator("#simpleChatForm .run-button").click()

        assistant_content = page.locator(".chat-message.assistant .chat-content")
        # Wait for the first delta to render, then cancel mid-stream while the
        # ReadableStream is still open waiting for subsequent chunks.
        expect(assistant_content).to_contain_text("Hello", timeout=5000)
        page.locator("#cancelChatButton").click()

        # Cancellation must not be a silent fallback: the bubble is marked
        # cancelled and never silently claims it reached the final word.
        expect(assistant_content).to_contain_text("cancelled", timeout=2000)
        text = assistant_content.inner_text()
        assert "Hello" in text
        assert "finally" not in text

        expect(page.locator("#chatStreamControls")).to_be_hidden()
        expect(page.locator("#simpleChatForm .run-button")).to_be_enabled()
    finally:
        context.close()


def test_enter_sends_and_shift_enter_inserts_newline(
    browser: Browser,
    chat_server: str,
) -> None:
    context = _login(browser, chat_server)
    try:
        page = context.new_page()
        _open_chat_tab(page, chat_server)
        _route_streaming_completion(page, ["ok"])

        input_box = page.locator("#simpleChatInput")
        input_box.click()
        input_box.type("line one")
        input_box.press("Shift+Enter")
        input_box.type("line two")
        expect(input_box).to_have_value("line one\nline two")

        # Plain Enter submits instead of inserting another newline.
        input_box.press("Enter")
        expect(input_box).to_have_value("")
        expect(page.locator(".chat-message.user .chat-content")).to_contain_text(
            "line two"
        )
    finally:
        context.close()


def test_fusion_per_model_insights_not_double_escaped(
    browser: Browser,
    chat_server: str,
) -> None:
    context = _login(browser, chat_server)
    try:
        page = context.new_page()
        page.route(
            "**/v1/ui/playground/models",
            lambda route: route.fulfill(
                status=200,
                json={
                    "chat": ["gateway/fusion-demo"],
                    "fusion": ["gateway/fusion-demo"],
                    "providers": [],
                },
            ),
        )
        page.route(
            "**/v1/chat/completions",
            lambda route: route.fulfill(
                status=200,
                json={
                    "choices": [{"message": {"role": "assistant", "content": "Final answer"}}],
                    "fusion": {
                        "main": {"provider": "p1", "model": "m1"},
                        "judge": {"provider": "p2", "model": "m2"},
                        "analysis": {
                            "agreements": [],
                            "disputes": [],
                            "per_model_insights": [
                                {"model": "A & B", "insight": "x < y & z"},
                            ],
                            "blind_spots": [],
                        },
                        "panel": [],
                    },
                },
            ),
        )
        _open_chat_tab(page, chat_server)

        page.locator("#simpleChatInput").fill("Compare")
        page.locator("#simpleChatForm .run-button").click()

        fusion_details = page.locator(".fusion-block")
        expect(fusion_details).to_be_visible()
        fusion_details.locator("summary").click()
        insight_item = page.locator(".fusion-analysis-section li").first
        expect(insight_item).to_have_text("A & B: x < y & z")
    finally:
        context.close()


def test_pdf_polling_stops_after_consecutive_network_errors(
    browser: Browser,
    chat_server: str,
) -> None:
    context = _login(browser, chat_server)
    try:
        page = context.new_page()
        # Compress long polling/backoff timers so 10 retries finish quickly.
        page.add_init_script(
            """(() => {
                const origSetTimeout = window.setTimeout.bind(window);
                window.setTimeout = (fn, delay, ...args) => {
                    const fastDelay = typeof delay === 'number' && delay >= 500 ? 5 : delay;
                    return origSetTimeout(fn, fastDelay, ...args);
                };
            })();"""
        )
        page.route(
            "**/v1/ui/playground/models",
            lambda route: route.fulfill(
                status=200,
                json={
                    "chat": ["gateway/chat-stream"],
                    "pdf_conversions": ["pdf-model"],
                    "providers": [],
                },
            ),
        )
        poll_attempts = {"count": 0}

        def handle_pdf(route: Route) -> None:
            if route.request.method == "POST":
                route.fulfill(
                    status=200,
                    json={"id": "job-1", "status": "running", "percent": 10},
                )
                return
            poll_attempts["count"] += 1
            route.abort("failed")

        page.route("**/v1/pdf/jobs**", handle_pdf)

        page.goto(f"{chat_server}/v1/ui/playground")
        page.locator('[data-playground-section-tab="pdf-conversion"]').click()
        expect(page.locator("#pdfConversionForm")).to_be_visible()
        page.locator("#pdfConversionForm input[type='file']").set_input_files(
            {"name": "sample.pdf", "mimeType": "application/pdf", "buffer": b"%PDF-1.4\n"}
        )
        run_button = page.locator("#pdfConversionForm .run-button")
        run_button.click()

        status = page.locator('[data-status-for="pdf-conversion"]')
        expect(status).to_have_class("playground-status error", timeout=5000)
        expect(status).to_contain_text("Lost connection after 10 retries")
        expect(run_button).to_be_enabled()
        assert poll_attempts["count"] == 10
    finally:
        context.close()


def test_login_safe_redirect_and_logout_failure_handling(
    browser: Browser,
    chat_server: str,
) -> None:
    context = browser.new_context(viewport={"width": 1280, "height": 900})
    try:
        page = context.new_page()
        page.route(
            "**/auth/login",
            lambda route: route.fulfill(
                status=200,
                json={"redirect_to": "https://evil.example/phish"},
            )
            if route.request.method == "POST"
            else route.continue_(),
        )
        page.route(
            "**/v1/ui/overview",
            lambda route: route.fulfill(
                status=200,
                headers={"content-type": "text/html"},
                body="<html><body>overview ok</body></html>",
            ),
        )
        page.goto(f"{chat_server}/auth/login")
        expect(page.locator("#submitButton")).to_be_enabled()
        page.locator("#apiKeyInput").fill(MASTER_KEY)
        page.locator("#submitButton").click()

        # Unsafe external redirect_to must fall back to /v1/ui/overview.
        expect(page).to_have_url(f"{chat_server}/v1/ui/overview")
    finally:
        context.close()

    # Now verify shared-auth logout(): non-OK returns false without redirect;
    # OK returns true and redirects to /auth/login.
    auth_context = _login(browser, chat_server)
    try:
        page = auth_context.new_page()
        page.route(
            "**/auth/logout",
            lambda route: route.fulfill(status=500, json={"detail": "boom"}),
        )
        page.goto(f"{chat_server}/v1/ui/playground")
        result = page.evaluate("() => window.gatewayAuth.logout()")
        assert result is False
        assert "/v1/ui/playground" in page.url

        page.unroute("**/auth/logout")
        result_ok = page.evaluate("() => window.gatewayAuth.logout()")
        assert result_ok is True
        expect(page).to_have_url(
            f"{chat_server}/auth/login?next=%2Fv1%2Fui%2Fplayground"
        )
    finally:
        auth_context.close()
