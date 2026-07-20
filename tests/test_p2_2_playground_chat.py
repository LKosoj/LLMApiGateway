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
        _route_streaming_completion(page, STREAM_WORDS)

        page.locator("#simpleChatInput").fill("Say hello")
        page.locator("#simpleChatForm .run-button").click()

        assistant_content = page.locator(".chat-message.assistant .chat-content")
        # Wait for the first delta to render, then cancel mid-stream: the
        # remaining ~11 deltas (each paced ~40ms apart) give a wide enough
        # window for the cancel click to land before the stream completes.
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
