import hashlib
import hmac
import os
import secrets
import subprocess
import time

import pytest
from playwright.sync_api import Page, expect

from tests.ui_server_helpers import get_free_port, wait_for_gateway


def build_session_signature(issued_at: int, expires_at: int, nonce: str, gateway_api_key: str) -> str:
    secret = gateway_api_key.encode("utf-8")
    payload = f"{issued_at}.{expires_at}.{nonce}.master.".encode("utf-8")
    return hmac.new(secret, payload, hashlib.sha256).hexdigest()


def create_authenticated_session(gateway_api_key: str) -> str:
    issued_at = int(time.time())
    expires_at = issued_at + 365 * 24 * 60 * 60
    nonce = secrets.token_urlsafe(24)
    signature = build_session_signature(issued_at, expires_at, nonce, gateway_api_key)
    return f"{issued_at}.{expires_at}.{nonce}.master..{signature}"


@pytest.fixture(scope="function")
def server():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"

        providers_path.write_text(
            (
                '[{"openai": {"baseUrl": "http://api.openai.com", "apikey": "key"}}, '
                '{"nvidia": {"baseUrl": "https://integrate.api.nvidia.com/v1", "apikey": "nvidia-key"}}]'
            ),
            encoding="utf-8",
        )
        fallback_rules_path.write_text("[]", encoding="utf-8")
        operation_rules_path.write_text(
            '{"embeddings": [], "rerank": [], "images_generations": [], "images_edits": []}',
            encoding="utf-8",
        )

        env = os.environ.copy()
        env["GATEWAY_API_KEY"] = "test-key"
        env["FALLBACK_PROVIDER"] = "openai"
        env["PROVIDERS_FILENAME"] = str(providers_path)
        env["FALLBACK_RULES_FILENAME"] = str(fallback_rules_path)
        env["OPERATION_RULES_FILENAME"] = str(operation_rules_path)
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        env["LOG_LEVEL"] = "DEBUG"
        base_url = f"http://localhost:{port}"

        proc = subprocess.Popen(
            ["./.venv/bin/python", "main.py"],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )

        wait_for_gateway(base_url, proc)

        yield base_url

        proc.terminate()
        proc.wait()


def expand_first_card(page: Page, container_selector: str) -> None:
    card = page.locator(f"{container_selector} .rule-card").first
    card_classes = card.get_attribute("class") or ""
    if "collapsed" in card_classes.split():
        card.locator(".accordion-toggle").click()


def test_audio_tab_is_visible(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.goto(f"{server}/v1/ui/rules-editor")

    audio_tab = page.locator("#tabAudio")
    expect(audio_tab).to_be_visible()
    expect(audio_tab).to_have_text("Audio")


def test_create_and_save_audio_speech_route(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.route(
        "**/v1/config/models-rules/structured",
        lambda route: route.fulfill(json={"rules": [], "providers": ["openai"]}),
    )
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(json={"provider": "openai", "models": [{"id": "tts-1"}]}),
    )

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    page.click("#addAudioSpeechButton")
    expect(page.locator("#audioSpeechList .add-fallback-button")).to_have_text("Add Route")
    expect(page.locator("#audioSpeechList .fallback-row-actions .danger-button")).to_have_text("Remove Route")
    page.fill("#audioSpeechList .gateway-model-input", "my-tts-model")
    page.select_option("#audioSpeechList .provider-select", "openai")
    page.fill("#audioSpeechList .model-input", "tts-1")
    page.fill("#audioSpeechList .target-path-input", "/audio/speech")
    page.locator("#audioSpeechList details.advanced-options summary").click()
    page.fill("#audioSpeechList .voices-target-path-input", "/voices")
    page.fill("#audioSpeechList .retry-delay-input", "0.5")
    page.fill("#audioSpeechList .retry-count-input", "1")
    page.fill("#audioSpeechList .custom-body-params-input", '{"voice": "alloy"}')

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    assert "collapsed" in (page.locator("#audioSpeechList .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#audioSpeechList")
    expect(page.locator("#audioSpeechList .gateway-model-input")).to_have_value("my-tts-model")
    expect(page.locator("#audioSpeechList .target-path-input")).to_have_value("/audio/speech")
    page.locator("#audioSpeechList details.advanced-options summary").click()
    expect(page.locator("#audioSpeechList .voices-target-path-input")).to_have_value("/voices")
    expect(page.locator("#audioSpeechList .retry-delay-input")).to_have_value("0.5")
    expect(page.locator("#audioSpeechList .retry-count-input")).to_have_value("1")
    expect(page.locator("#audioSpeechList .custom-body-params-input")).to_have_value('{\n  "voice": "alloy"\n}')


def test_create_and_save_audio_transcription_route(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.route(
        "**/v1/config/models-rules/structured",
        lambda route: route.fulfill(json={"rules": [], "providers": ["openai"]}),
    )
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "gpt-4o-mini-transcribe"}]}
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    page.click("#addAudioTranscriptionButton")
    expect(page.locator("#audioTranscriptionsList .add-fallback-button")).to_have_text("Add Fallback Route")
    expect(page.locator("#audioTranscriptionsList .fallback-row-actions .danger-button")).to_have_text(
        "Remove Fallback Route"
    )
    page.fill("#audioTranscriptionsList .gateway-model-input", "my-audio-model")
    page.select_option("#audioTranscriptionsList .provider-select", "openai")
    page.fill("#audioTranscriptionsList .model-input", "gpt-4o-mini-transcribe")
    page.fill("#audioTranscriptionsList .target-path-input", "/audio/transcriptions")
    page.locator("#audioTranscriptionsList details.advanced-options summary").click()
    page.fill("#audioTranscriptionsList .retry-delay-input", "2")
    page.fill("#audioTranscriptionsList .retry-count-input", "1")
    page.fill("#audioTranscriptionsList .custom-body-params-input", '{"language": "en", "response_format": "json"}')

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    assert "collapsed" in (page.locator("#audioTranscriptionsList .rule-card").first.get_attribute("class") or "")
    expand_first_card(page, "#audioTranscriptionsList")
    expect(page.locator("#audioTranscriptionsList .gateway-model-input")).to_have_value("my-audio-model")
    expect(page.locator("#audioTranscriptionsList .target-path-input")).to_have_value("/audio/transcriptions")
    page.locator("#audioTranscriptionsList details.advanced-options summary").click()
    expect(page.locator("#audioTranscriptionsList .retry-delay-input")).to_have_value("2")
    expect(page.locator("#audioTranscriptionsList .retry-count-input")).to_have_value("1")
    expect(page.locator("#audioTranscriptionsList .custom-body-params-input")).to_have_value(
        '{\n  "language": "en",\n  "response_format": "json"\n}'
    )


def test_create_and_save_nvidia_audio_transcription_route(page: Page, server):
    session = create_authenticated_session("test-key")
    page.context.add_cookies([{"name": "llmgateway_session", "value": session, "url": server}])

    page.route(
        "**/v1/config/models-rules/structured",
        lambda route: route.fulfill(json={"rules": [], "providers": ["openai", "nvidia"]}),
    )
    page.route(
        "**/v1/config/providers/nvidia/models",
        lambda route: route.fulfill(
            json={
                "provider": "nvidia",
                "models": [{"id": "nvidia/parakeet-1_1b-rnnt-multilingual-asr"}],
            }
        ),
    )

    page.goto(f"{server}/v1/ui/rules-editor")
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    page.click("#addAudioTranscriptionButton")
    page.fill("#audioTranscriptionsList .gateway-model-input", "my-nvidia-audio-model")
    page.select_option("#audioTranscriptionsList .provider-select", "nvidia")
    page.fill("#audioTranscriptionsList .model-input", "nvidia/parakeet-1_1b-rnnt-multilingual-asr")
    page.locator("#audioTranscriptionsList details.advanced-options summary").click()
    page.select_option("#audioTranscriptionsList .request-format-select", "nvidia_riva_grpc")
    page.fill("#audioTranscriptionsList .custom-headers-input", '{"function-id": "func-123"}')
    page.fill("#audioTranscriptionsList .custom-body-params-input", '{"language": "multi"}')

    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")

    expand_first_card(page, "#audioTranscriptionsList")
    page.locator("#audioTranscriptionsList details.advanced-options summary").click()
    expect(page.locator("#audioTranscriptionsList .provider-select")).to_have_value("nvidia")
    expect(page.locator("#audioTranscriptionsList .request-format-select")).to_have_value("nvidia_riva_grpc")
    expect(page.locator("#audioTranscriptionsList .custom-headers-input")).to_have_value(
        '{\n  "function-id": "func-123"\n}'
    )
