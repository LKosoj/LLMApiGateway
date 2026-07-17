from __future__ import annotations

import json
from pathlib import Path

import pytest

from llm_gateway_core.config.config_store import ConfigFile, ConfigSourceBundle
from llm_gateway_core.config.loader import ConfigError, ConfigLoader, RuleValidationError
from llm_gateway_core.config.schema_validation import empty_operation_rules
from llm_gateway_core.services.model_policy import resolve_model_name


VALID_PAYLOADS = {
    ConfigFile.PROVIDERS: [
        {"primary": {"baseUrl": "https://primary.example/v1", "apikey": "DIRECT-KEY"}}
    ],
    ConfigFile.FALLBACK_RULES: [
        {
            "gateway_model_name": "gateway/chat",
            "fallback_models": [{"provider": "primary", "model": "upstream-chat"}],
        }
    ],
    ConfigFile.MODEL_RULES: {},
    ConfigFile.OPERATION_RULES: {},
    ConfigFile.FUSION_RULES: [],
    ConfigFile.ROUTER_RULES: [],
}
FILENAMES = {
    ConfigFile.PROVIDERS: "providers.json",
    ConfigFile.FALLBACK_RULES: "models_fallback_rules.json",
    ConfigFile.MODEL_RULES: "models_model_rules.json",
    ConfigFile.OPERATION_RULES: "models_operation_rules.json",
    ConfigFile.FUSION_RULES: "models_fusion_rules.json",
    ConfigFile.ROUTER_RULES: "models_router_rules.json",
}
OPTIONAL_FILES = set(ConfigFile) - {ConfigFile.PROVIDERS, ConfigFile.FALLBACK_RULES}


@pytest.fixture(autouse=True)
def _fallback_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("llm_gateway_core.config.loader.settings.fallback_provider", "primary")


def _write_sources(root: Path, *, include_optional: bool = True) -> None:
    for config_file, payload in VALID_PAYLOADS.items():
        if not include_optional and config_file in OPTIONAL_FILES:
            continue
        (root / FILENAMES[config_file]).write_text(
            json.dumps(payload),
            encoding="utf-8",
        )


def _bundle(root: Path, *, include_optional: bool = True) -> ConfigSourceBundle:
    _write_sources(root, include_optional=include_optional)
    return ConfigSourceBundle.capture(root)


def _loader_from_disk(root: Path) -> ConfigLoader:
    return ConfigLoader(
        providers_filename=str(root / FILENAMES[ConfigFile.PROVIDERS]),
        fallback_rules_filename=str(root / FILENAMES[ConfigFile.FALLBACK_RULES]),
        model_rules_filename=str(root / FILENAMES[ConfigFile.MODEL_RULES]),
        operation_rules_filename=str(root / FILENAMES[ConfigFile.OPERATION_RULES]),
        fusion_rules_filename=str(root / FILENAMES[ConfigFile.FUSION_RULES]),
        router_rules_filename=str(root / FILENAMES[ConfigFile.ROUTER_RULES]),
    )


def _graph(loader: ConfigLoader) -> dict[str, object]:
    return {
        "providers": {
            name: details.model_dump(mode="json")
            for name, details in loader.providers_config.items()
        },
        "fallback_base": loader._fallback_rules_base,  # noqa: SLF001
        "fallback": loader.fallback_rules,
        "model": loader.model_rules,
        "operation": loader.operation_rules,
        "fusion": loader.fusion_rules,
        "router": loader.router_rules,
    }


def test_from_source_bundle_loads_complete_graph_without_disk_access(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bundle = _bundle(tmp_path)
    monkeypatch.setattr(
        ConfigSourceBundle,
        "capture",
        lambda *_args, **_kwargs: pytest.fail("unexpected disk capture"),
    )
    monkeypatch.setattr("builtins.open", lambda *_args, **_kwargs: pytest.fail("unexpected open"))
    monkeypatch.setattr(Path, "open", lambda *_args, **_kwargs: pytest.fail("unexpected Path.open"))

    loader = ConfigLoader.from_source_bundle(bundle)
    returned = loader.load_complete()

    assert returned is loader
    assert loader.source_bundle is bundle
    assert set(loader.providers_config) == {"primary"}
    assert set(loader.fallback_rules) == {"gateway/chat"}
    assert loader.operation_rules == empty_operation_rules()


def test_disk_and_bundle_entrypoints_build_equal_graphs(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)

    disk_loader = _loader_from_disk(tmp_path).load_complete()
    bundle_loader = ConfigLoader.from_source_bundle(bundle).load_complete()

    assert _graph(disk_loader) == _graph(bundle_loader)
    assert bundle_loader.source_bundle is bundle


def test_missing_optional_documents_use_canonical_shapes(tmp_path: Path) -> None:
    loader = ConfigLoader.from_source_bundle(
        _bundle(tmp_path, include_optional=False)
    ).load_complete()

    assert loader.operation_rules == empty_operation_rules()
    assert loader.fusion_rules == {}
    assert loader.router_rules == {}
    assert loader.model_rules == {
        "aliases": {},
        "prefixes": [],
        "excluded_models": [],
        "upstream_model_pools": {},
    }


@pytest.mark.parametrize("config_file", list(ConfigFile))
def test_each_invalid_document_rejects_complete_load(
    tmp_path: Path,
    config_file: ConfigFile,
) -> None:
    bundle = _bundle(tmp_path).with_candidate(config_file, b"not-json")

    with pytest.raises(ConfigError, match=config_file.value):
        ConfigLoader.from_source_bundle(bundle).load_complete()


@pytest.mark.parametrize("config_file", list(ConfigFile))
def test_each_existing_zero_byte_document_is_invalid(
    tmp_path: Path,
    config_file: ConfigFile,
) -> None:
    bundle = _bundle(tmp_path).with_candidate(config_file, b"")

    with pytest.raises(ConfigError, match=config_file.value):
        ConfigLoader.from_source_bundle(bundle).load_complete()


@pytest.mark.parametrize("config_file", list(ConfigFile))
def test_one_leading_bom_is_accepted_and_retained(
    tmp_path: Path,
    config_file: ConfigFile,
) -> None:
    bundle = _bundle(tmp_path)
    original = bundle[config_file].content
    assert original is not None
    candidate = b"\xef\xbb\xbf" + original
    candidate_bundle = bundle.with_candidate(config_file, candidate)

    loader = ConfigLoader.from_source_bundle(candidate_bundle).load_complete()

    assert loader.source_bundle is candidate_bundle
    assert loader.source_bundle[config_file].content == candidate


@pytest.mark.parametrize("config_file", list(ConfigFile))
@pytest.mark.parametrize("placement", ["repeated", "embedded"])
def test_repeated_or_embedded_bom_is_rejected(
    tmp_path: Path,
    config_file: ConfigFile,
    placement: str,
) -> None:
    bundle = _bundle(tmp_path)
    original = bundle[config_file].content
    assert original is not None
    if placement == "repeated":
        candidate = b"\xef\xbb\xbf\xef\xbb\xbf" + original
    else:
        candidate = original[:1] + b"\xef\xbb\xbf" + original[1:]

    with pytest.raises(ConfigError, match=config_file.value):
        ConfigLoader.from_source_bundle(
            bundle.with_candidate(config_file, candidate)
        ).load_complete()


def test_invalid_utf8_is_rejected(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path).with_candidate(ConfigFile.MODEL_RULES, b"{\xff}")

    with pytest.raises(ConfigError, match="model_rules"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_failed_complete_load_preserves_every_published_graph(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path).with_candidate(ConfigFile.ROUTER_RULES, b"not-json")
    loader = ConfigLoader.from_source_bundle(bundle)
    sentinels = [{"sentinel": index} for index in range(7)]
    (
        loader.providers_config,
        loader._fallback_rules_base,  # noqa: SLF001
        loader.fallback_rules,
        loader.model_rules,
        loader.operation_rules,
        loader.fusion_rules,
        loader.router_rules,
    ) = sentinels

    with pytest.raises(ConfigError, match="router_rules"):
        loader.load_complete()

    assert loader.providers_config is sentinels[0]
    assert loader._fallback_rules_base is sentinels[1]  # noqa: SLF001
    assert loader.fallback_rules is sentinels[2]
    assert loader.model_rules is sentinels[3]
    assert loader.operation_rules is sentinels[4]
    assert loader.fusion_rules is sentinels[5]
    assert loader.router_rules is sentinels[6]


def test_repeated_builds_share_no_mutable_config_nodes(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    first = ConfigLoader.from_source_bundle(bundle).load_complete()
    second = ConfigLoader.from_source_bundle(bundle).load_complete()

    second.fallback_rules["gateway/chat"]["fallback_models"][0]["model"] = "changed"

    assert first.fallback_rules["gateway/chat"]["fallback_models"][0]["model"] == "upstream-chat"
    assert first.fallback_rules is not second.fallback_rules
    assert first._fallback_rules_base is not second._fallback_rules_base  # noqa: SLF001


def test_explicit_empty_provider_candidate_does_not_fall_back_to_active_state(tmp_path: Path) -> None:
    loader = ConfigLoader.from_source_bundle(_bundle(tmp_path)).load_complete()

    with pytest.raises(ValueError, match="Providers must be loaded"):
        loader.validate_fallback_rules_mapping(
            loader.fallback_rules,
            providers_config={},
        )


@pytest.mark.parametrize(
    "provider_patch",
    [
        {"baseUrl": "https://", "apikey": "DIRECT-KEY"},
        {
            "baseUrl": "https://primary.example",
            "apikey": "DIRECT-KEY",
            "models": {"m": {"input_rate": 1.0}},
        },
        {
            "baseUrl": "https://primary.example",
            "apikey": "DIRECT-KEY",
            "models": {"m": {"input_rate": float("nan"), "output_rate": 1.0}},
        },
    ],
)
def test_complete_load_rejects_invalid_provider_boundaries(
    tmp_path: Path,
    provider_patch: dict[str, object],
) -> None:
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.PROVIDERS,
        json.dumps([{"primary": provider_patch}]).encode(),
    )

    with pytest.raises(ConfigError):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_complete_load_validates_fusion_web_tool_operation_reference(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.FUSION_RULES,
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/fusion",
                    "panel": [{"provider": "primary", "model": "panel"}],
                    "main_model": {"provider": "primary", "model": "main"},
                    "web_tools": {"search_model": "gateway/missing-search"},
                }
            ]
        ).encode(),
    )

    with pytest.raises(ConfigError, match="cross-graph"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_complete_load_rejects_alias_collision_with_fusion(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle = bundle.with_candidate(
        ConfigFile.MODEL_RULES,
        json.dumps({"aliases": {"gateway/fusion": "gateway/chat"}}).encode(),
    )
    bundle = bundle.with_candidate(
        ConfigFile.FUSION_RULES,
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/fusion",
                    "panel": [{"provider": "primary", "model": "panel"}],
                    "main_model": {"provider": "primary", "model": "main"},
                }
            ]
        ).encode(),
    )

    with pytest.raises(ConfigError, match="cross-graph"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_complete_load_rejects_excluded_router_internal_target(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    bundle = bundle.with_candidate(
        ConfigFile.MODEL_RULES,
        json.dumps({"excluded_models": ["gateway/chat"]}).encode(),
    )
    bundle = bundle.with_candidate(
        ConfigFile.ROUTER_RULES,
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/chat",
                    "targets": [{"type": "gateway_model", "model": "gateway/chat"}],
                }
            ]
        ).encode(),
    )

    with pytest.raises(ConfigError, match="cross-graph"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_complete_load_rejects_unknown_web_search_query_model(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.OPERATION_RULES,
        json.dumps(
            {
                "web_search": [
                    {
                        "gateway_model_name": "gateway/search",
                        "query_model": "gateway/missing-chat",
                    }
                ]
            }
        ).encode(),
    )

    with pytest.raises(ConfigError, match="cross-graph"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


@pytest.mark.parametrize(
    ("config_file", "candidate", "expected_message"),
    [
        (
            ConfigFile.FALLBACK_RULES,
            [
                {
                    "gateway_model_name": "gateway/chat",
                    "fallback_models": [{"provider": "ghost", "model": "upstream-chat"}],
                }
            ],
            "Invalid provider 'ghost' used in fallback rule for 'gateway/chat'. "
            "Provider not found in configuration.",
        ),
        (
            ConfigFile.FUSION_RULES,
            [
                {
                    "gateway_model_name": "gateway/fusion",
                    "panel": [{"provider": "ghost", "model": "panel"}],
                    "main_model": {"provider": "primary", "model": "main"},
                }
            ],
            "Invalid provider 'ghost' used in panel member for fusion model "
            "'gateway/fusion'. Provider not found in configuration.",
        ),
        (
            ConfigFile.ROUTER_RULES,
            [
                {
                    "gateway_model_name": "gateway/router",
                    "selector_model": "gateway/ghost",
                    "targets": [{"type": "gateway_model", "model": "gateway/chat"}],
                }
            ],
            "Router model 'gateway/router' references unknown selector_model "
            "'gateway/ghost' (must be a gateway chat model in fallback rules).",
        ),
        (
            ConfigFile.OPERATION_RULES,
            {
                "embeddings": [
                    {
                        "gateway_model_name": "gateway/embed",
                        "routes": [
                            {
                                "provider": "ghost",
                                "model": "upstream-embed",
                                "target_path": "/embeddings",
                            }
                        ],
                    }
                ]
            },
            "Invalid provider 'ghost' used in operation route for 'gateway/embed' "
            "in 'embeddings'. Provider not found in configuration.",
        ),
    ],
    ids=["fallback", "fusion", "router", "operation"],
)
def test_a_rejected_rule_names_the_gateway_model_and_the_target(
    tmp_path: Path,
    config_file: ConfigFile,
    candidate: object,
    expected_message: str,
) -> None:
    bundle = _bundle(tmp_path).with_candidate(
        config_file,
        json.dumps(candidate).encode(),
    )

    with pytest.raises(ConfigError) as error:
        ConfigLoader.from_source_bundle(bundle).load_complete()

    cause = error.value.__cause__
    assert isinstance(cause, RuleValidationError)
    assert str(cause) == expected_message


def test_a_rejected_cross_graph_reference_names_both_models(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.FUSION_RULES,
        json.dumps(
            [
                {
                    "gateway_model_name": "gateway/fusion",
                    "panel": [{"provider": "primary", "model": "panel"}],
                    "main_model": {"provider": "primary", "model": "main"},
                    "web_tools": {"search_model": "gateway/missing-search"},
                }
            ]
        ).encode(),
    )

    with pytest.raises(ConfigError) as error:
        ConfigLoader.from_source_bundle(bundle).load_complete()

    cause = error.value.__cause__
    assert isinstance(cause, RuleValidationError)
    assert str(cause) == (
        "Fusion model 'gateway/fusion' references unknown web search model "
        "'gateway/missing-search'."
    )


@pytest.mark.parametrize(
    ("config_file", "candidate"),
    [
        (
            ConfigFile.FALLBACK_RULES,
            b'[{"gateway_model_name": "gateway/chat", "apikey": "sk-MUST-NOT-LEAK",]',
        ),
        (
            ConfigFile.FALLBACK_RULES,
            json.dumps(
                [
                    {
                        "gateway_model_name": "gateway/chat",
                        "fallback_models": [
                            {"provider": "primary", "model": {"k": "sk-MUST-NOT-LEAK"}}
                        ],
                    }
                ]
            ).encode(),
        ),
        (
            ConfigFile.FUSION_RULES,
            json.dumps(
                [
                    {
                        "gateway_model_name": "gateway/fusion",
                        "panel": "sk-MUST-NOT-LEAK",
                        "main_model": {"provider": "primary", "model": "main"},
                    }
                ]
            ).encode(),
        ),
    ],
    ids=["json5-syntax", "fallback-shape", "fusion-shape"],
)
def test_a_failure_that_quotes_the_submitted_bytes_stays_unmarked(
    tmp_path: Path,
    config_file: ConfigFile,
    candidate: bytes,
) -> None:
    bundle = _bundle(tmp_path).with_candidate(config_file, candidate)

    with pytest.raises(ConfigError) as error:
        ConfigLoader.from_source_bundle(bundle).load_complete()

    assert not isinstance(error.value.__cause__, RuleValidationError)
    assert "MUST-NOT-LEAK" not in str(error.value)


@pytest.mark.parametrize(
    ("field_name", "malformed_url"),
    [
        ("baseUrl", "https://[broken-ipv6"),
        ("baseUrl", "https://provider.example:not-a-port"),
        ("baseUrl", "https://:443"),
        ("proxy", "http://[broken-ipv6"),
        ("proxy", "http://proxy.example:not-a-port"),
        ("proxy", "http://:8080"),
    ],
)
def test_malformed_provider_urls_are_wrapped_by_safe_document_boundary(
    tmp_path: Path,
    field_name: str,
    malformed_url: str,
) -> None:
    provider = {
        "baseUrl": "https://primary.example",
        "apikey": "DIRECT-KEY",
        field_name: malformed_url,
    }
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.PROVIDERS,
        json.dumps([{"primary": provider}]).encode(),
    )

    with pytest.raises(ConfigError) as error:
        ConfigLoader.from_source_bundle(bundle).load_complete()

    assert str(error.value) == "Invalid 'providers' configuration."
    assert malformed_url not in str(error.value)


def test_provider_models_none_is_valid(tmp_path: Path) -> None:
    provider = {
        "baseUrl": "https://primary.example",
        "apikey": "DIRECT-KEY",
        "models": None,
    }
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.PROVIDERS,
        json.dumps([{"primary": provider}]).encode(),
    )

    loader = ConfigLoader.from_source_bundle(bundle).load_complete()

    assert loader.providers_config["primary"].models is None


@pytest.mark.parametrize("models", [[], {"model": []}])
def test_provider_models_requires_mapping_shapes(tmp_path: Path, models: object) -> None:
    provider = {
        "baseUrl": "https://primary.example",
        "apikey": "DIRECT-KEY",
        "models": models,
    }
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.PROVIDERS,
        json.dumps([{"primary": provider}]).encode(),
    )

    with pytest.raises(ConfigError, match="providers"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


@pytest.mark.parametrize("excluded_model", ["blocked/*", "blocked/"])
def test_target_prefix_must_not_resolve_into_excluded_namespace(
    tmp_path: Path,
    excluded_model: str,
) -> None:
    model_rules = {
        "prefixes": [{"prefix": "public/", "target_prefix": "blocked/"}],
        "excluded_models": [excluded_model],
    }
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.MODEL_RULES,
        json.dumps(model_rules).encode(),
    )

    with pytest.raises(ConfigError, match="cross-graph"):
        ConfigLoader.from_source_bundle(bundle).load_complete()


def test_target_prefix_allows_unrelated_exact_excluded_descendant(tmp_path: Path) -> None:
    model_rules = {
        "prefixes": [{"prefix": "public/", "target_prefix": "blocked/"}],
        "excluded_models": ["blocked/model"],
    }
    bundle = _bundle(tmp_path).with_candidate(
        ConfigFile.MODEL_RULES,
        json.dumps(model_rules).encode(),
    )

    loader = ConfigLoader.from_source_bundle(bundle).load_complete()

    resolution = resolve_model_name("public/other", loader.model_rules)
    assert resolution.effective_model == "blocked/other"


def test_line_endings_are_retained_exactly_without_changing_graph(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path)
    variants = (b"{}\n", b"{}\r\n", b"{}\r", b"{}")
    graphs: list[dict[str, object]] = []

    for payload in variants:
        candidate = bundle.with_candidate(ConfigFile.MODEL_RULES, payload)
        loader = ConfigLoader.from_source_bundle(candidate).load_complete()
        assert loader.source_bundle is candidate
        assert loader.source_bundle[ConfigFile.MODEL_RULES].content == payload
        graphs.append(_graph(loader))

    assert all(graph == graphs[0] for graph in graphs[1:])


def _rich_bundle(root: Path) -> ConfigSourceBundle:
    _write_sources(root)
    payloads = {
        ConfigFile.PROVIDERS: [
            {
                "primary": {
                    "baseUrl": "https://primary.example/v1",
                    "models": {
                        "upstream-chat": {"input_rate": 1.0, "output_rate": 2.0}
                    },
                    "routing": {"strategy": "priority", "session_affinity": True},
                    "upstream_key_pools": {
                        "main": {
                            "keys": [
                                {"id": "key-1", "apikey": "DIRECT-KEY", "priority": 10}
                            ]
                        }
                    },
                }
            }
        ],
        ConfigFile.FALLBACK_RULES: [
            {
                "gateway_model_name": "gateway/chat",
                "fallback_models": [
                    {
                        "provider": "primary",
                        "model": "upstream-chat",
                        "upstream_key_pool": "main",
                        "custom_body_params": {"nested": {"temperature": 0.2}},
                    }
                ],
            }
        ],
        ConfigFile.MODEL_RULES: {
            "aliases": {"gateway/alias": "gateway/chat"},
            "prefixes": [{"prefix": "short/", "target": "gateway/chat"}],
            "upstream_model_pools": {
                "gateway/pool": {
                    "fallback_models": [
                        {
                            "provider": "primary",
                            "model": "pool-model",
                            "upstream_key_pool": "main",
                        }
                    ]
                }
            },
        },
        ConfigFile.OPERATION_RULES: {
            "embeddings": [
                {
                    "gateway_model_name": "gateway/embed",
                    "routes": [
                        {
                            "provider": "primary",
                            "model": "embed-model",
                            "target_path": "/embeddings",
                            "custom_body_params": {"nested": {"dimensions": 128}},
                        }
                    ],
                }
            ],
            "web_search": [
                {"gateway_model_name": "gateway/search", "query_model": "gateway/chat"}
            ],
            "web_read": [{"gateway_model_name": "gateway/read"}],
        },
        ConfigFile.FUSION_RULES: [
            {
                "gateway_model_name": "gateway/fusion",
                "panel": [
                    {"provider": "primary", "model": "panel", "reasoning": {"effort": "low"}}
                ],
                "main_model": {"provider": "primary", "model": "main"},
                "web_tools": {
                    "search_model": "gateway/search",
                    "read_model": "gateway/read",
                },
            }
        ],
        ConfigFile.ROUTER_RULES: [
            {
                "gateway_model_name": "gateway/router",
                "selector_model": "gateway/chat",
                "targets": [{"type": "gateway_model", "model": "gateway/pool"}],
            }
        ],
    }
    bundle = ConfigSourceBundle.capture(root)
    for config_file, payload in payloads.items():
        bundle = bundle.with_candidate(config_file, json.dumps(payload).encode())
    return bundle


def _mutable_object_ids(value: object, seen: set[int] | None = None) -> set[int]:
    if seen is None:
        seen = set()
    if value is None or isinstance(value, (str, bytes, int, float, bool)):
        return set()
    value_id = id(value)
    if value_id in seen:
        return set()
    seen.add(value_id)
    result = {value_id}
    if isinstance(value, dict):
        for item in value.values():
            result.update(_mutable_object_ids(item, seen))
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            result.update(_mutable_object_ids(item, seen))
    elif hasattr(value, "__dict__"):
        result.update(_mutable_object_ids(vars(value), seen))
    return result


def test_rich_repeated_builds_have_disjoint_mutable_graphs(tmp_path: Path) -> None:
    bundle = _rich_bundle(tmp_path)
    first = ConfigLoader.from_source_bundle(bundle).load_complete()
    second = ConfigLoader.from_source_bundle(bundle).load_complete()
    first_graphs = tuple(_graph(first).values())
    second_graphs = tuple(_graph(second).values())

    first_ids = _mutable_object_ids(first_graphs)
    second_ids = _mutable_object_ids(second_graphs)

    assert first_ids.isdisjoint(second_ids)

    second.providers_config["primary"].models["upstream-chat"]["input_rate"] = 999
    second.providers_config["primary"].routing.session_affinity = False
    second.providers_config["primary"].upstream_key_pools["main"].keys[0].priority = 1
    second._fallback_rules_base["gateway/chat"]["fallback_models"][0]["model"] = "base-changed"  # noqa: SLF001
    second.fallback_rules["gateway/pool"]["fallback_models"][0]["model"] = "pool-changed"
    second.model_rules["aliases"]["gateway/alias"] = "gateway/pool"
    second.operation_rules["embeddings"]["gateway/embed"]["routes"][0]["model"] = "embed-changed"
    second.fusion_rules["gateway/fusion"]["panel"][0]["model"] = "panel-changed"
    second.router_rules["gateway/router"]["targets"][0]["model"] = "gateway/chat"

    assert first.providers_config["primary"].models["upstream-chat"]["input_rate"] == 1.0
    assert first.providers_config["primary"].routing.session_affinity is True
    assert first.providers_config["primary"].upstream_key_pools["main"].keys[0].priority == 10
    assert first._fallback_rules_base["gateway/chat"]["fallback_models"][0]["model"] == "upstream-chat"  # noqa: SLF001
    assert first.fallback_rules["gateway/pool"]["fallback_models"][0]["model"] == "pool-model"
    assert first.model_rules["aliases"]["gateway/alias"] == "gateway/chat"
    assert first.operation_rules["embeddings"]["gateway/embed"]["routes"][0]["model"] == "embed-model"
    assert first.fusion_rules["gateway/fusion"]["panel"][0]["model"] == "panel"
    assert first.router_rules["gateway/router"]["targets"][0]["model"] == "gateway/pool"
