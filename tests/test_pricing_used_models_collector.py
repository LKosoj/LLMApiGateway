"""Unit tests for collect_used_models()/collect_operation_used_models().

These functions scan the loaded rule-config graph (fallback_rules,
operation_rules, fusion_rules, router_rules, model_rules.upstream_model_pools)
for every (provider, model) pair the runtime can actually dispatch to. A
plain ``SimpleNamespace`` stands in for ``ConfigLoader`` since the collector
only reads plain attributes.

Each rule-set is a mapping keyed by ``gateway_model_name`` (that is how
``ConfigLoader.load_complete()`` actually stores ``fallback_rules``,
``fusion_rules``, ``router_rules`` and each ``operation_rules`` section --
the on-disk JSON files are lists with an embedded "gateway_model_name" field,
but the loader collapses that into the dict key), so the fixtures below
mirror that shape rather than the on-disk list shape.
"""

from __future__ import annotations

from types import SimpleNamespace

from llm_gateway_core.services.pricing_autofill import (
    collect_operation_used_models,
    collect_used_models,
)


def _loader(
    *,
    fallback_rules=None,
    operation_rules=None,
    fusion_rules=None,
    router_rules=None,
    model_rules=None,
):
    return SimpleNamespace(
        fallback_rules=fallback_rules if fallback_rules is not None else {},
        operation_rules=operation_rules if operation_rules is not None else {},
        fusion_rules=fusion_rules if fusion_rules is not None else {},
        router_rules=router_rules if router_rules is not None else {},
        model_rules=model_rules if model_rules is not None else {},
    )


def test_collects_fallback_rule_models_and_context_overflow():
    loader = _loader(
        fallback_rules={
            "llmgateway/chat": {
                "fallback_models": [
                    {"provider": "openrouter", "model": "openai/gpt-4o"},
                    {"provider": "openrouter", "model": "anthropic/claude-3.5"},
                ],
                "context_overflow_fallback": {
                    "provider": "openrouter",
                    "model": "openai/gpt-4o-128k",
                },
            }
        }
    )
    assert collect_used_models(loader) == {
        ("openrouter", "openai/gpt-4o"),
        ("openrouter", "anthropic/claude-3.5"),
        ("openrouter", "openai/gpt-4o-128k"),
    }


def test_collects_operation_rule_routes_across_operation_kinds():
    loader = _loader(
        operation_rules={
            "embeddings": {
                "llmgateway/embed": {
                    "routes": [
                        {"provider": "openrouter", "model": "openai/text-embedding-3-small"}
                    ],
                }
            },
            "rerank": {
                "llmgateway/rerank": {
                    "routes": [{"provider": "cohere", "model": "rerank-v3"}],
                }
            },
            # web_* operation entries reference other gateway models by name
            # instead of a direct provider/model pair; they must not blow up
            # the scan and must not contribute a bogus pair.
            "web_search": {
                "llmgateway/web_search": {
                    "query_model": "llmgateway/chat",
                }
            },
        }
    )
    assert collect_used_models(loader) == {
        ("openrouter", "openai/text-embedding-3-small"),
        ("cohere", "rerank-v3"),
    }


def test_collects_fusion_rule_members_panel_main_judge_reserve():
    loader = _loader(
        fusion_rules={
            "llmgateway/fusion": {
                "panel": [
                    {"provider": "openrouter", "model": "model-a"},
                    {"provider": "openrouter", "model": "model-b"},
                ],
                "main_model": {"provider": "openrouter", "model": "model-main"},
                "judge_model": {"provider": "openrouter", "model": "model-judge"},
                "reserve": [{"provider": "openrouter", "model": "model-reserve"}],
            }
        }
    )
    assert collect_used_models(loader) == {
        ("openrouter", "model-a"),
        ("openrouter", "model-b"),
        ("openrouter", "model-main"),
        ("openrouter", "model-judge"),
        ("openrouter", "model-reserve"),
    }


def test_collects_upstream_model_pool_fallback_models():
    loader = _loader(
        model_rules={
            "upstream_model_pools": {
                "llmgateway/pool": {
                    "fallback_models": [
                        {"provider": "openrouter", "model": "pool-model-1"},
                    ]
                }
            }
        }
    )
    assert collect_used_models(loader) == {("openrouter", "pool-model-1")}


def test_router_rules_resolve_selector_and_targets_via_gateway_index():
    loader = _loader(
        fallback_rules={
            "llmgateway/light": {
                "fallback_models": [{"provider": "openrouter", "model": "light-model"}],
            },
            "llmgateway/heavy": {
                "fallback_models": [{"provider": "openrouter", "model": "heavy-model"}],
            },
        },
        router_rules={
            "llmgateway/router": {
                "selector_model": "llmgateway/light",
                "targets": [
                    {"type": "gateway_model", "model": "llmgateway/heavy"},
                ],
            }
        },
    )
    assert collect_used_models(loader) == {
        ("openrouter", "light-model"),
        ("openrouter", "heavy-model"),
    }


def test_router_target_fallback_entry_resolves_via_gateway_model_field():
    loader = _loader(
        fallback_rules={
            "llmgateway/heavy": {
                "fallback_models": [{"provider": "openrouter", "model": "heavy-model"}],
            },
        },
        router_rules={
            "llmgateway/router": {
                "selector_model": "llmgateway/heavy",
                "targets": [
                    {"type": "fallback_entry", "gateway_model": "llmgateway/heavy"},
                ],
            }
        },
    )
    assert collect_used_models(loader) == {("openrouter", "heavy-model")}


def test_dedup_across_rule_sets_for_the_same_pair():
    loader = _loader(
        fallback_rules={
            "llmgateway/chat": {
                "fallback_models": [{"provider": "openrouter", "model": "shared-model"}],
            }
        },
        fusion_rules={
            "llmgateway/fusion": {
                "main_model": {"provider": "openrouter", "model": "shared-model"},
            }
        },
    )
    result = collect_used_models(loader)
    assert result == {("openrouter", "shared-model")}
    assert len(result) == 1


def test_excluded_gateway_model_contributes_nothing():
    loader = _loader(
        fallback_rules={
            "llmgateway/deprecated": {
                "fallback_models": [{"provider": "openrouter", "model": "deprecated-model"}],
            }
        },
        model_rules={"excluded_models": ["llmgateway/deprecated"]},
    )
    assert collect_used_models(loader) == set()


def test_empty_rule_sets_collect_nothing():
    assert collect_used_models(_loader()) == set()
    assert collect_operation_used_models(_loader()) == set()


def test_collect_operation_used_models_only_returns_operation_routes():
    loader = _loader(
        fallback_rules={
            "llmgateway/chat": {
                "fallback_models": [{"provider": "openrouter", "model": "chat-model"}],
            }
        },
        operation_rules={
            "embeddings": {
                "llmgateway/embed": {
                    "routes": [{"provider": "openrouter", "model": "embed-model"}],
                }
            }
        },
    )
    assert collect_used_models(loader) == {
        ("openrouter", "chat-model"),
        ("openrouter", "embed-model"),
    }
    assert collect_operation_used_models(loader) == {("openrouter", "embed-model")}
