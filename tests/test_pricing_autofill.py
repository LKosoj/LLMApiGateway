"""Unit tests for classify_pricing_rows() (pricing autofill classification).

Covers: OpenRouter catalog autofill (including ``:free`` suffix
normalization), operation vs. plain-chat fallback statuses, non-OpenRouter
providers never autofilling, an unloaded catalog producing
``awaiting_openrouter_catalog`` with no writes proposed, and already
configured pairs always taking priority over the used-models scan.
"""

from __future__ import annotations

import pytest

from llm_gateway_core.services.accounting import DEFAULT_OPERATION_COST_USD
from llm_gateway_core.services.pricing_autofill import (
    PricingSource,
    classify_pricing_rows,
)
from llm_gateway_core.utils.usage_tracking import ModelCostRates


def _classify(**kwargs):
    kwargs.setdefault("used_models", set())
    kwargs.setdefault("configured_registry", {})
    kwargs.setdefault("operation_model_pairs", set())
    kwargs.setdefault("openrouter_catalog", {})
    return classify_pricing_rows(**kwargs)


def _row(classification, provider, model):
    for row in classification.rows:
        if row.provider == provider and row.model == model:
            return row
    raise AssertionError(f"no row for ({provider!r}, {model!r})")


def test_configured_pair_always_wins_over_used_models_scan():
    classification = _classify(
        used_models={("openrouter", "openai/gpt-4o")},
        configured_registry={("openrouter", "openai/gpt-4o"): ModelCostRates(1.0, 2.0)},
        openrouter_catalog={"openai/gpt-4o": {"prompt": 0.000005, "completion": 0.000015}},
    )
    row = _row(classification, "openrouter", "openai/gpt-4o")
    assert row.source is PricingSource.CONFIGURED
    assert row.input_rate == 1.0
    assert row.output_rate == 2.0
    assert classification.autofill_additions == []


def test_openrouter_chat_model_autofills_from_catalog():
    classification = _classify(
        used_models={("openrouter", "openai/gpt-4o")},
        openrouter_catalog={
            "openai/gpt-4o": {"prompt": "0.000005", "completion": "0.000015"}
        },
    )
    row = _row(classification, "openrouter", "openai/gpt-4o")
    assert row.source is PricingSource.OPENROUTER_AUTOFILL
    assert row.input_rate == 5.0
    assert row.output_rate == 15.0
    assert row.default_cost_per_request is None
    assert len(classification.autofill_additions) == 1
    addition = classification.autofill_additions[0]
    assert (addition.provider, addition.model) == ("openrouter", "openai/gpt-4o")
    assert addition.input_rate == 5.0
    assert addition.output_rate == 15.0


def test_free_suffix_is_normalized_to_the_paid_base_model_for_lookup():
    classification = _classify(
        used_models={("openrouter", "openai/gpt-oss-120b:free")},
        openrouter_catalog={
            "openai/gpt-oss-120b": {"prompt": 0.0000001, "completion": 0.0000002}
        },
    )
    row = _row(classification, "openrouter", "openai/gpt-oss-120b:free")
    assert row.source is PricingSource.OPENROUTER_AUTOFILL
    assert row.input_rate == pytest.approx(0.1)
    assert row.output_rate == pytest.approx(0.2)
    assert classification.autofill_additions[0].model == "openai/gpt-oss-120b:free"


def test_openrouter_operation_model_missing_from_catalog_gets_operation_default():
    classification = _classify(
        used_models={("openrouter", "some/embedding-model")},
        operation_model_pairs={("openrouter", "some/embedding-model")},
        openrouter_catalog={},
    )
    row = _row(classification, "openrouter", "some/embedding-model")
    assert row.source is PricingSource.OPERATION_DEFAULT
    assert row.input_rate is None
    assert row.output_rate is None
    assert row.default_cost_per_request == DEFAULT_OPERATION_COST_USD
    assert classification.autofill_additions == []


def test_openrouter_chat_model_missing_from_catalog_gets_upstream_only():
    classification = _classify(
        used_models={("openrouter", "some/chat-model")},
        openrouter_catalog={},
    )
    row = _row(classification, "openrouter", "some/chat-model")
    assert row.source is PricingSource.UPSTREAM_ONLY
    assert row.input_rate is None
    assert row.output_rate is None
    assert row.default_cost_per_request is None


def test_invalid_catalog_pricing_falls_back_instead_of_autofilling():
    classification = _classify(
        used_models={("openrouter", "broken/model")},
        openrouter_catalog={"broken/model": {"prompt": "not-a-number", "completion": -1}},
    )
    row = _row(classification, "openrouter", "broken/model")
    assert row.source is PricingSource.UPSTREAM_ONLY
    assert classification.autofill_additions == []


def test_non_openrouter_provider_never_autofills_even_with_matching_catalog_key():
    classification = _classify(
        used_models={("custom-provider", "openai/gpt-4o")},
        openrouter_catalog={"openai/gpt-4o": {"prompt": 0.000005, "completion": 0.000015}},
    )
    row = _row(classification, "custom-provider", "openai/gpt-4o")
    assert row.source is PricingSource.UPSTREAM_ONLY
    assert classification.autofill_additions == []


def test_non_openrouter_operation_model_gets_operation_default_not_autofill():
    classification = _classify(
        used_models={("custom-provider", "rerank-v3")},
        operation_model_pairs={("custom-provider", "rerank-v3")},
    )
    row = _row(classification, "custom-provider", "rerank-v3")
    assert row.source is PricingSource.OPERATION_DEFAULT
    assert row.default_cost_per_request == DEFAULT_OPERATION_COST_USD


def test_catalog_not_loaded_yields_awaiting_status_and_no_autofill():
    classification = _classify(
        used_models={("openrouter", "openai/gpt-4o"), ("openrouter", "some/embedding-model")},
        operation_model_pairs={("openrouter", "some/embedding-model")},
        openrouter_catalog=None,
    )
    for model in ("openai/gpt-4o", "some/embedding-model"):
        row = _row(classification, "openrouter", model)
        assert row.source is PricingSource.AWAITING_OPENROUTER_CATALOG
        assert row.input_rate is None
        assert row.output_rate is None
    assert classification.autofill_additions == []


def test_rows_are_sorted_by_provider_then_model():
    classification = _classify(
        used_models={
            ("openrouter", "zzz-model"),
            ("aaa-provider", "aaa-model"),
        },
    )
    pairs = [(row.provider, row.model) for row in classification.rows]
    assert pairs == sorted(pairs)
