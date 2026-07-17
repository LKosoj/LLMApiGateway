import json
import os
import tempfile
from pathlib import Path

import pytest
from playwright.sync_api import Page, expect

from tests.ui_server_helpers import (
    create_authenticated_session,
    get_free_port,
    isolated_gateway_process,
    wait_for_gateway,
)


pytestmark = pytest.mark.browser


def _expand_first_card(page: Page, container_selector: str) -> None:
    card = page.locator(f"{container_selector} .rule-card").first
    if "collapsed" in (card.get_attribute("class") or "").split():
        card.locator(".accordion-toggle").click()


@pytest.fixture
def isolated_rules_editor():
    with tempfile.TemporaryDirectory() as temp_dir:
        temp_path = Path(temp_dir)
        providers_path = temp_path / "providers.json"
        fallback_rules_path = temp_path / "models_fallback_rules.json"
        operation_rules_path = temp_path / "models_operation_rules.json"
        fusion_rules_path = temp_path / "models_fusion_rules.json"
        model_rules_path = temp_path / "models_model_rules.json"
        router_rules_path = temp_path / "models_router_rules.json"

        providers_path.write_text(
            json.dumps(
                [
                    {
                        "openai": {
                            "baseUrl": "http://127.0.0.1:9/v1",
                            "apikey": "test-provider-key",
                        }
                    }
                ]
            ),
            encoding="utf-8",
        )
        fallback_rules_path.write_text(
            json.dumps(
                [
                    {
                        "gateway_model_name": "llmgateway/chat",
                        "fallback_models": [
                            {"provider": "openai", "model": "gpt-4o-mini"}
                        ],
                        "rotate_models": False,
                    }
                ]
            ),
            encoding="utf-8",
        )
        operation_rules_path.write_text(
            json.dumps(
                {
                    "embeddings": [
                        {
                            "gateway_model_name": "llmgateway/embed",
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "text-embedding-3-small",
                                    "target_path": "/embeddings",
                                }
                            ],
                        }
                    ],
                    "rerank": [
                        {
                            "gateway_model_name": "llmgateway/rerank",
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "rerank-model",
                                    "target_path": "/score",
                                }
                            ],
                        }
                    ],
                    "images_generations": [
                        {
                            "gateway_model_name": "llmgateway/image",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.25},
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "gpt-image-1",
                                    "target_path": "/images/generations",
                                }
                            ],
                        }
                    ],
                    "images_edits": [
                        {
                            "gateway_model_name": "llmgateway/image-edit",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0},
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "gpt-image-1",
                                    "target_path": "/images/edits",
                                }
                            ],
                        }
                    ],
                    "audio_speech": [
                        {
                            "gateway_model_name": "llmgateway/speech",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.6},
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "tts-1",
                                    "target_path": "/audio/speech",
                                }
                            ],
                        }
                    ],
                    "audio_transcriptions": [
                        {
                            "gateway_model_name": "llmgateway/transcribe",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.2},
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "transcribe-1",
                                    "target_path": "/audio/transcriptions",
                                }
                            ],
                        }
                    ],
                    "pdf_conversions": [
                        {
                            "gateway_model_name": "llmgateway/pdf",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.7},
                            "routes": [
                                {
                                    "provider": "openai",
                                    "model": "pdf-model",
                                    "target_path": "/pdf",
                                }
                            ],
                        }
                    ],
                    "web_search": [
                        {
                            "gateway_model_name": "llmgateway/search",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.05},
                        }
                    ],
                    "web_read": [{"gateway_model_name": "llmgateway/read"}],
                    "web_research": [
                        {
                            "gateway_model_name": "llmgateway/research",
                            "search_model": "llmgateway/search",
                            "read_model": "llmgateway/read",
                            "rerank_model": "llmgateway/rerank",
                            "analysis_model": "llmgateway/chat",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.3},
                        }
                    ],
                    "web_deep_research": [
                        {
                            "gateway_model_name": "llmgateway/deep-research",
                            "search_model": "llmgateway/search",
                            "read_model": "llmgateway/read",
                            "fast_model": "llmgateway/chat",
                            "smart_model": "llmgateway/chat",
                            "strategic_model": "llmgateway/chat",
                            "cost_calculator": {"unit": "operation", "rate_usd": 0.8},
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        fusion_rules_path.write_text("[]", encoding="utf-8")
        model_rules_path.write_text("{}", encoding="utf-8")
        router_rules_path.write_text("[]", encoding="utf-8")

        env = os.environ.copy()
        env.update(
            {
                "GATEWAY_API_KEY": "test-key",
                "FALLBACK_PROVIDER": "openai",
                "PROVIDERS_FILENAME": str(providers_path),
                "FALLBACK_RULES_FILENAME": str(fallback_rules_path),
                "OPERATION_RULES_FILENAME": str(operation_rules_path),
                "FUSION_RULES_FILENAME": str(fusion_rules_path),
                "MODEL_RULES_FILENAME": str(model_rules_path),
                "ROUTER_RULES_FILENAME": str(router_rules_path),
                "LOG_LEVEL": "WARNING",
            }
        )
        port = get_free_port()
        env["GATEWAY_PORT"] = str(port)
        base_url = f"http://127.0.0.1:{port}"

        with isolated_gateway_process(env=env, temp_path=temp_path) as proc:
            wait_for_gateway(base_url, proc)
            yield base_url, operation_rules_path


def test_operation_cost_calculators_round_trip_across_rules_editor_tabs(
    page: Page,
    isolated_rules_editor,
):
    base_url, operation_rules_path = isolated_rules_editor
    console_problems: list[str] = []
    page.on(
        "console",
        lambda message: console_problems.append(f"{message.type}: {message.text}")
        if message.type in {"error", "warning"}
        else None,
    )
    page.route(
        "**/v1/config/providers/openai/models",
        lambda route: route.fulfill(
            json={"provider": "openai", "models": [{"id": "fixture-model"}]}
        ),
    )
    page.context.add_cookies(
        [
            {
                "name": "llmgateway_session",
                "value": create_authenticated_session(base_url, "test-key"),
                "url": base_url,
            }
        ]
    )
    page.goto(f"{base_url}/v1/ui/rules-editor")

    page.click("#tabImages")
    expect(page.locator("#messageArea")).to_contain_text("Images Routes loaded successfully")
    _expand_first_card(page, "#imageGenerationList")
    image_rate = page.locator("#imageGenerationList .cost-calculator-rate-input")
    expect(image_rate).to_have_attribute("min", "0")
    expect(image_rate).to_have_attribute("step", "any")
    expect(image_rate).to_have_attribute("placeholder", "0.1")
    expect(image_rate).to_have_value("0.25")
    image_rate.fill("0.4")
    _expand_first_card(page, "#imageEditList")
    expect(page.locator("#imageEditList .cost-calculator-rate-input")).to_have_value("0")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")
    _expand_first_card(page, "#audioSpeechList")
    speech_rate = page.locator("#audioSpeechList .cost-calculator-rate-input")
    expect(speech_rate).to_have_value("0.6")
    speech_rate.fill("")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.click("#tabWeb")
    expect(page.locator("#messageArea")).to_contain_text("Web Services loaded successfully")
    _expand_first_card(page, "#webSearchList")
    search_rate = page.locator("#webSearchList .cost-calculator-rate-input")
    expect(search_rate).to_have_value("0.05")
    search_rate.fill("0.15")
    _expand_first_card(page, "#webReadList")
    expect(page.locator("#webReadList .cost-calculator-rate-input")).to_have_value("")
    page.click("#saveButton")
    expect(page.locator("#messageArea")).to_contain_text("updated successfully")

    page.reload()
    page.click("#tabImages")
    expect(page.locator("#messageArea")).to_contain_text("Images Routes loaded successfully")
    _expand_first_card(page, "#imageGenerationList")
    expect(page.locator("#imageGenerationList .cost-calculator-rate-input")).to_have_value("0.4")
    _expand_first_card(page, "#imageEditList")
    expect(page.locator("#imageEditList .cost-calculator-rate-input")).to_have_value("0")

    page.click("#tabAudio")
    expect(page.locator("#messageArea")).to_contain_text("Audio Routes loaded successfully")
    _expand_first_card(page, "#audioSpeechList")
    expect(page.locator("#audioSpeechList .cost-calculator-rate-input")).to_have_value("")

    page.click("#tabWeb")
    expect(page.locator("#messageArea")).to_contain_text("Web Services loaded successfully")
    _expand_first_card(page, "#webSearchList")
    expect(page.locator("#webSearchList .cost-calculator-rate-input")).to_have_value("0.15")

    persisted = json.loads(operation_rules_path.read_text(encoding="utf-8"))
    assert persisted["images_generations"][0]["cost_calculator"]["rate_usd"] == 0.4
    assert persisted["images_edits"][0]["cost_calculator"]["rate_usd"] == 0
    assert "cost_calculator" not in persisted["audio_speech"][0]
    assert persisted["web_search"][0]["cost_calculator"]["rate_usd"] == 0.15
    assert "cost_calculator" not in persisted["web_read"][0]
    assert persisted["pdf_conversions"][0]["cost_calculator"]["rate_usd"] == 0.7
    assert console_problems == []
