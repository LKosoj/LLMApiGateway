from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from llm_gateway_core.config.loader import (
    ConfigLoader,
    ModelsOperationConfig,
    ProviderDetails,
)


SUPPORTED_OPERATION_MODELS = (
    ("images_generations", {"gateway_model_name": "gateway/image", "routes": []}),
    ("images_edits", {"gateway_model_name": "gateway/image-edit", "routes": []}),
    ("audio_speech", {"gateway_model_name": "gateway/speech", "routes": []}),
    (
        "audio_transcriptions",
        {"gateway_model_name": "gateway/transcription", "routes": []},
    ),
    ("pdf_conversions", {"gateway_model_name": "gateway/pdf", "routes": []}),
    ("web_search", {"gateway_model_name": "gateway/web-search"}),
    ("web_read", {"gateway_model_name": "gateway/web-read"}),
    (
        "web_research",
        {
            "gateway_model_name": "gateway/web-research",
            "search_model": "gateway/web-search",
            "read_model": "gateway/web-read",
            "rerank_model": "gateway/rerank",
            "analysis_model": "gateway/chat",
        },
    ),
    (
        "web_deep_research",
        {
            "gateway_model_name": "gateway/web-deep-research",
            "search_model": "gateway/web-search",
            "read_model": "gateway/web-read",
            "fast_model": "gateway/fast",
            "smart_model": "gateway/smart",
            "strategic_model": "gateway/strategic",
        },
    ),
)


@pytest.mark.parametrize(("section", "model"), SUPPORTED_OPERATION_MODELS)
def test_supported_operation_models_preserve_configured_calculator(
    section: str,
    model: dict[str, object],
) -> None:
    payload = {
        section: [
            {
                **model,
                "cost_calculator": {"unit": "operation", "rate_usd": 0.25},
            }
        ]
    }

    validated = ModelsOperationConfig.model_validate(payload)
    dumped = validated.model_dump(exclude_none=True)
    built = ConfigLoader()._build_operation_config(payload)

    assert dumped[section][0]["cost_calculator"] == {
        "unit": "operation",
        "rate_usd": 0.25,
    }
    assert built[section][str(model["gateway_model_name"])]["cost_calculator"] == {
        "unit": "operation",
        "rate_usd": 0.25,
    }


def test_missing_calculator_stays_missing_and_explicit_zero_stays_configured() -> None:
    payload = {
        "images_generations": [
            {"gateway_model_name": "gateway/default", "routes": []},
            {
                "gateway_model_name": "gateway/free",
                "routes": [],
                "cost_calculator": {"unit": "operation", "rate_usd": 0},
            },
        ]
    }

    validated = ModelsOperationConfig.model_validate(payload)
    dumped = validated.model_dump(exclude_none=True)["images_generations"]
    built = ConfigLoader()._build_operation_config(payload)["images_generations"]

    assert "cost_calculator" not in dumped[0]
    assert "cost_calculator" not in built["gateway/default"]
    assert dumped[1]["cost_calculator"]["rate_usd"] == 0.0
    assert built["gateway/free"]["cost_calculator"] == {
        "unit": "operation",
        "rate_usd": 0.0,
    }


@pytest.mark.parametrize(
    "rate_usd",
    (
        -1,
        10**400,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        "0.1",
    ),
    ids=(
        "negative",
        "overflow",
        "nan",
        "positive-infinity",
        "negative-infinity",
        "true",
        "false",
        "numeric-string",
    ),
)
def test_calculator_rejects_invalid_rate(rate_usd: object) -> None:
    payload = {
        "images_generations": [
            {
                "gateway_model_name": "gateway/image",
                "routes": [],
                "cost_calculator": {
                    "unit": "operation",
                    "rate_usd": rate_usd,
                },
            }
        ]
    }

    with pytest.raises(ValidationError):
        ModelsOperationConfig.model_validate(payload)


@pytest.mark.parametrize(
    "calculator",
    (
        {"unit": "token", "rate_usd": 0.1},
        {"unit": "operation", "rate_usd": 0.1, "currency": "USD"},
    ),
)
def test_calculator_rejects_unknown_unit_and_extra_keys(
    calculator: dict[str, object],
) -> None:
    payload = {
        "images_generations": [
            {
                "gateway_model_name": "gateway/image",
                "routes": [],
                "cost_calculator": calculator,
            }
        ]
    }

    with pytest.raises(ValidationError):
        ModelsOperationConfig.model_validate(payload)


@pytest.mark.parametrize("section", ("embeddings", "rerank"))
def test_token_priced_models_reject_operation_calculator(section: str) -> None:
    payload = {
        section: [
            {
                "gateway_model_name": f"gateway/{section}",
                "routes": [],
                "cost_calculator": {"unit": "operation", "rate_usd": 0.1},
            }
        ]
    }

    with pytest.raises(ValidationError):
        ModelsOperationConfig.model_validate(payload)


def test_operation_rules_file_load_preserves_calculator(tmp_path) -> None:
    operation_rules_path = tmp_path / "models_operation_rules.json"
    operation_rules_path.write_text(
        json.dumps(
            {
                "images_generations": [
                    {
                        "gateway_model_name": "gateway/image",
                        "cost_calculator": {
                            "unit": "operation",
                            "rate_usd": 0.75,
                        },
                        "routes": [
                            {
                                "provider": "provider",
                                "model": "image-model",
                                "target_path": "/images/generations",
                            }
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    loader = ConfigLoader(operation_rules_filename=str(operation_rules_path))
    loader.providers_config = {
        "provider": ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
        )
    }

    loaded = loader.load_operation_rules()

    assert loaded["images_generations"]["gateway/image"]["cost_calculator"] == {
        "unit": "operation",
        "rate_usd": 0.75,
    }
