from __future__ import annotations

import json
import warnings

import pytest

with warnings.catch_warnings():
    warnings.filterwarnings(
        "ignore",
        message=(
            r"`langchain-community` is being sunset and is no longer actively "
            r"maintained\. See https://github\.com/langchain-ai/"
            r"langchain-community/issues/674 for details and migration guidance "
            r"toward standalone integration packages\."
        ),
        category=DeprecationWarning,
        module=r"gpt_researcher\.scraper\.arxiv\.arxiv",
    )
    from llm_gateway_core.api.v1.admin_pricing import (
        ModelPricing,
        PricingPatchError,
        patch_provider_pricing,
    )


def _item(
    provider: object,
    model: object,
    input_rate: object,
    output_rate: object,
) -> ModelPricing:
    return ModelPricing.model_construct(
        provider=provider,
        model=model,
        input_rate=input_rate,
        output_rate=output_rate,
    )


def test_patch_updates_only_rates_and_preserves_provider_and_model_metadata() -> None:
    source = """
    [
      {
        // provider comment is allowed in the captured JSON5 source
        "primary": {
          "baseUrl": "https://primary.example/v1",
          "apikey": "secret-reference",
          "available_models": ["kept", "metadata-only", "rate-only", "new"],
          "subscription_quota": {"requests_per_day": 100},
          "routing": {"strategy": "round-robin"},
          "future_provider_field": {"enabled": true},
          "models": {
            "kept": {
              "context_length": 131072,
              "upstream_limits": {"rpm": 30},
              "future_model_field": [1, 2, 3],
              "input_rate": 1.0,
              "output_rate": 2.0
            },
            "metadata-only": {
              "max_completion_tokens": 4096,
              "input_rate": 3.0,
              "output_rate": 4.0
            },
            "rate-only": {"input_rate": 5.0, "output_rate": 6.0}
          }
        }
      }
    ]
    """

    patched = json.loads(
        patch_provider_pricing(
            source,
            [
                _item("primary", "kept", 10.0, 20.0),
                _item("primary", "new", 0.0, 7.5),
            ],
        )
    )

    provider = patched[0]["primary"]
    assert provider["available_models"] == [
        "kept",
        "metadata-only",
        "rate-only",
        "new",
    ]
    assert provider["subscription_quota"] == {"requests_per_day": 100}
    assert provider["routing"] == {"strategy": "round-robin"}
    assert provider["future_provider_field"] == {"enabled": True}
    assert provider["models"]["kept"] == {
        "context_length": 131072,
        "upstream_limits": {"rpm": 30},
        "future_model_field": [1, 2, 3],
        "input_rate": 10.0,
        "output_rate": 20.0,
    }
    assert provider["models"]["metadata-only"] == {
        "max_completion_tokens": 4096
    }
    assert "rate-only" not in provider["models"]
    assert provider["models"]["new"] == {
        "input_rate": 0.0,
        "output_rate": 7.5,
    }
    assert patch_provider_pricing(source, []).endswith("\n")


def test_empty_full_list_removes_rates_without_removing_remaining_metadata() -> None:
    source = json.dumps(
        [
            {
                "primary": {
                    "baseUrl": "https://primary.example/v1",
                    "models": {
                        "metadata": {
                            "context_length": 8192,
                            "input_rate": 1,
                            "output_rate": 2,
                        },
                        "rates": {"input_rate": 3, "output_rate": 4},
                    },
                }
            }
        ]
    )

    patched = json.loads(patch_provider_pricing(source, []))

    assert patched[0]["primary"]["models"] == {
        "metadata": {"context_length": 8192}
    }


@pytest.mark.parametrize(
    "items",
    [
        [
            _item("primary", "model", 1.0, 2.0),
            _item("primary", "model", 3.0, 4.0),
        ],
        [_item("unknown", "model", 1.0, 2.0)],
        [_item(" primary", "model", 1.0, 2.0)],
        [_item("primary", "model ", 1.0, 2.0)],
        [_item("", "model", 1.0, 2.0)],
        [_item("primary", "", 1.0, 2.0)],
        [_item("primary", "model", -1.0, 2.0)],
        [_item("primary", "model", 1.0, -2.0)],
        [_item("primary", "model", float("nan"), 2.0)],
        [_item("primary", "model", float("inf"), 2.0)],
        [_item("primary", "model", float("-inf"), 2.0)],
        [_item("primary", "model", 1.0, float("nan"))],
        [_item("primary", "model", 1.0, float("inf"))],
        [_item("primary", "model", 1.0, float("-inf"))],
        [_item("primary", "model", True, 2.0)],
        [_item("primary", "model", 1.0, False)],
        [_item(1, "model", 1.0, 2.0)],
        [_item("primary", 1, 1.0, 2.0)],
        [_item("primary", "model", None, 2.0)],
        [_item("primary", "model", 1.0, object())],
    ],
)
def test_patch_rejects_invalid_items_before_producing_a_candidate(
    items: list[ModelPricing],
) -> None:
    source = json.dumps(
        [{"primary": {"baseUrl": "https://primary.example/v1"}}]
    )

    with pytest.raises(PricingPatchError, match="Pricing update is invalid"):
        patch_provider_pricing(source, items)


@pytest.mark.parametrize(
    "source",
    [
        "",
        "[",
        "null",
        "{}",
        "[null]",
        "[{}]",
        '[{"": {}}]',
        '[{"primary": []}]',
        '[{"primary": {"baseUrl": "https://primary.example/v1", "models": []}}]',
        '[{"primary": {"baseUrl": "https://primary.example/v1", "models": null}}]',
        (
            '[{"primary": {"baseUrl": "https://primary.example/v1", '
            '"models": {"model": null}}}]'
        ),
        (
            '[{"primary": {"baseUrl": "https://primary.example/v1", '
            '"models": {"model": []}}}]'
        ),
        '[{"primary": {}, "secondary": {}}]',
        '[{"primary": {}}, {"primary": {}}]',
    ],
)
def test_patch_rejects_unsupported_source_shapes(source: str) -> None:
    with pytest.raises(PricingPatchError, match="Pricing update is invalid"):
        patch_provider_pricing(source, [])


def test_patch_rejects_non_model_pricing_items() -> None:
    source = '[{"primary": {"baseUrl": "https://primary.example/v1"}}]'

    with pytest.raises(PricingPatchError, match="Pricing update is invalid"):
        patch_provider_pricing(source, [object()])  # type: ignore[list-item]


def test_patch_rejects_non_string_source_and_non_list_items() -> None:
    source = '[{"primary": {"baseUrl": "https://primary.example/v1"}}]'

    with pytest.raises(PricingPatchError, match="Pricing update is invalid"):
        patch_provider_pricing(None, [])  # type: ignore[arg-type]
    with pytest.raises(PricingPatchError, match="Pricing update is invalid"):
        patch_provider_pricing(source, ())  # type: ignore[arg-type]
