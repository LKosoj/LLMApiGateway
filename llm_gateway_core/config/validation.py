import json5
import logging
import math
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Dict, List, Mapping, Optional
from urllib.parse import urlsplit

from .placeholder_secrets import is_placeholder_secret, placeholder_secret_error
from .schema_validation import (
    SECURITY_HEADERS,
    empty_operation_rules as _empty_operation_rules,
    validate_provider_name_list as _validate_provider_name_list,
)
from .schemas import (
    FUSION_PANEL_MAX,
    FusionModelConfig,
    ModelFallbackConfig,
    ModelRulesConfig,
    ModelsOperationConfig,
    ProviderConfig,
    ProviderDetails,
    RouterModelConfig,
)
from .settings import settings
from ..services.model_policy import is_model_excluded
from ..utils.api_keys import select_next_api_key, split_api_keys

class ConfigError(RuntimeError):
    """Raised when configuration loading or validation fails at startup.

    Replaces direct ``sys.exit(1)`` so callers (lifespan, tests) can decide
    how to react — tests can assert on the message, and the process still
    aborts at startup because the exception propagates out of the lifespan.
    """


class RuleValidationError(ValueError):
    """A cross-validation failure whose message names no credential.

    Everything the ``validate_*_mapping`` family interpolates is an identifier
    out of the candidate configuration — gateway model, provider, pool, route
    and field names — so the message stays safe to hand back to the master
    client that submitted that candidate. A json5 syntax error or a Pydantic
    report is not safe: both quote raw input, which is why they keep the frozen
    generic message at the HTTP boundary instead.

    Marking the safe failures with their own type is what lets the boundary
    tell the two apart, since by then both arrive wrapped in a ``ConfigError``.
    """


@contextmanager
def rule_validation() -> Iterator[None]:
    """Mark a block whose ``ValueError``s are safe to show to the submitter.

    Wraps a ``validate_*_mapping`` call rather than each ``raise`` inside it:
    the guarantee belongs to the whole family, and re-typing every raise site
    would leave the next added one silently unmarked.
    """
    try:
        yield
    except RuleValidationError:
        raise
    except ValueError as exc:
        raise RuleValidationError(str(exc)) from exc


# Pinned anthropic-version header used for every outbound call to a provider
# of type "anthropic" (/v1/messages and /v1/models).
ANTHROPIC_API_VERSION = "2023-06-01"
ENV_REFERENCE_RE = re.compile(r"^\$\{([A-Za-z_][A-Za-z0-9_]*)\}$")
ENV_REFERENCE_NAME_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")
KNOWN_PROVIDER_ENV_NAMES = frozenset(
    {
        "ANTHROPIC_API_KEY",
        "COHERE_API_KEY",
        "CLOUDRU_API_KEY",
        "NVIDIA_API_KEY",
        "OPENAI_API_KEY",
        "OPENROUTER_API_KEY",
        "VSEGPT_API_KEY",
    }
)
SUPPORTED_PROXY_SCHEMES = frozenset({"http", "https", "socks5"})


def resolve_provider_api_key(api_key_reference_or_literal: str | None) -> str | None:
    return select_next_api_key(resolve_provider_api_key_value(api_key_reference_or_literal))


def resolve_provider_api_keys(api_key_reference_or_literal: str | None) -> list[str]:
    return split_api_keys(resolve_provider_api_key_value(api_key_reference_or_literal))


def resolve_provider_config_api_key(provider_config: object) -> str | None:
    api_keys = resolve_provider_config_api_keys(provider_config)
    return select_next_api_key(",".join(api_keys))


def resolve_provider_config_api_keys(provider_config: object) -> list[str]:
    legacy_api_key = getattr(provider_config, "apikey", None)
    if legacy_api_key:
        return resolve_provider_api_keys(legacy_api_key)

    pools = getattr(provider_config, "upstream_key_pools", None)
    if not isinstance(pools, Mapping):
        return []

    api_keys: list[str] = []
    for pool_config in pools.values():
        key_specs = getattr(pool_config, "keys", None)
        if not isinstance(key_specs, list):
            continue
        for key_spec in key_specs:
            if getattr(key_spec, "enabled", True) is False:
                continue
            api_keys.extend(resolve_provider_api_keys(getattr(key_spec, "apikey", None)))
    return api_keys


def resolve_provider_config_auth_headers(provider_config: object, api_key: str | None = None) -> dict[str, str]:
    resolved_api_key = api_key or resolve_provider_config_api_key(provider_config)
    headers: dict[str, str] = {}
    if resolved_api_key:
        if getattr(provider_config, "type", "openai") == "anthropic":
            headers["x-api-key"] = resolved_api_key
        else:
            headers["Authorization"] = f"Bearer {resolved_api_key}"
    _apply_provider_custom_headers(headers, provider_config)
    return headers


def _apply_provider_custom_headers(headers: dict[str, str], provider_config: object) -> None:
    raw_headers = getattr(provider_config, "custom_headers", None)
    if not isinstance(raw_headers, Mapping):
        return
    for header_name, header_value in raw_headers.items():
        if not isinstance(header_name, str) or header_name.lower() in SECURITY_HEADERS:
            continue
        if header_value is None:
            continue
        headers[header_name] = str(header_value)


def resolve_provider_api_key_value(api_key_reference_or_literal: str | None) -> str | None:
    if not api_key_reference_or_literal:
        return None

    env_name = _explicit_env_reference_name(api_key_reference_or_literal, "apikey")
    if env_name is None:
        _warn_unbraced_env_like_literal(api_key_reference_or_literal, "apikey")
        return api_key_reference_or_literal

    return _resolve_explicit_env_reference(env_name, "apikey")


def resolve_provider_proxy(proxy_reference_or_url: str | None) -> str | None:
    """Resolve a proxy URL from an explicit environment reference or a literal URL.

    Environment resolution is opt-in: use ``${PROXY_ENV}``. Without that
    wrapper the value is treated as a literal, even if an environment variable
    with the same name exists.
    """
    if not proxy_reference_or_url:
        return None

    env_name = _explicit_env_reference_name(proxy_reference_or_url, "proxy")
    if env_name is None:
        _warn_unbraced_env_like_literal(proxy_reference_or_url, "proxy")
        return proxy_reference_or_url

    return _resolve_explicit_env_reference(env_name, "proxy")


def _explicit_env_reference_name(value: str, field_name: str) -> str | None:
    match = ENV_REFERENCE_RE.fullmatch(value)
    if match:
        return match.group(1)

    if value.startswith("${") and value.endswith("}"):
        raise ConfigError(
            f"Invalid env reference syntax for provider field '{field_name}'. "
            "Use '${VAR_NAME}' with a non-empty environment variable name."
        )

    return None


def _resolve_explicit_env_reference(env_name: str, field_name: str) -> str:
    resolved = os.getenv(env_name)
    if resolved:
        if field_name == "apikey" and is_placeholder_secret(resolved):
            raise ConfigError(placeholder_secret_error(env_name, resolved))
        return resolved

    raise ConfigError(
        f"env var {env_name} referenced but missing or empty for provider field '{field_name}'"
    )


def _warn_unbraced_env_like_literal(value: str | None, field_name: str) -> None:
    if not _looks_like_env_reference(value):
        return
    if os.getenv(value) is None:
        return

    logging.warning(
        "Provider field '%s' value '%s' looks like an environment variable name, "
        "but '${VAR}' syntax was not used; treating it as a literal.",
        field_name,
        value,
    )


def _looks_like_env_reference(value: str | None) -> bool:
    if not value:
        return False

    normalized_value = value.strip()
    if normalized_value != value or not normalized_value:
        return False
    if any(char.isspace() for char in normalized_value):
        return False
    if normalized_value in KNOWN_PROVIDER_ENV_NAMES:
        return True
    return bool(ENV_REFERENCE_NAME_RE.fullmatch(normalized_value))


def _provider_env_reference_error(provider_name: str, field_name: str, env_name: str) -> str:
    return (
        f"env var {env_name} referenced but missing for provider "
        f"'{provider_name}' field '{field_name}'"
    )


class _ConfigValidationMixin:
    @staticmethod
    def _validate_complete_provider_config(
        providers_config: Dict[str, ProviderDetails],
    ) -> None:
        for provider_name, details in providers_config.items():
            _ConfigValidationMixin._validate_complete_url(details.baseUrl, {"http", "https"})
            if details.proxy:
                _ConfigValidationMixin._validate_complete_url(
                    resolve_provider_proxy(details.proxy) or "",
                    SUPPORTED_PROXY_SCHEMES,
                )
            if details.models is None:
                continue
            if not isinstance(details.models, Mapping):
                raise ValueError(f"Provider '{provider_name}' models must be a mapping.")
            for model_name, metadata in details.models.items():
                if not isinstance(metadata, Mapping):
                    raise ValueError(
                        f"Provider '{provider_name}' model '{model_name}' metadata must be a mapping."
                    )
                has_input = "input_rate" in metadata
                has_output = "output_rate" in metadata
                if has_input != has_output:
                    raise ConfigError(
                        f"Incomplete pricing rates for provider '{provider_name}' model '{model_name}'."
                    )
                if not has_input:
                    continue
                for field_name in ("input_rate", "output_rate"):
                    value = metadata[field_name]
                    if (
                        isinstance(value, bool)
                        or not isinstance(value, (int, float))
                        or not math.isfinite(float(value))
                        or float(value) < 0
                    ):
                        raise ConfigError(
                            f"Invalid {field_name} for provider '{provider_name}' model '{model_name}'."
                        )

    def _parse_and_validate_complete_providers(
        self,
        payload: str,
    ) -> Dict[str, ProviderDetails]:
        providers_config = self.parse_and_validate_providers_payload(
            payload,
            strict_env=True,
        )
        self._validate_complete_provider_config(providers_config)
        return providers_config

    @staticmethod
    def _validate_complete_url(url: str, supported_schemes: set[str] | frozenset[str]) -> None:
        try:
            parsed_url = urlsplit(url)
            hostname = parsed_url.hostname
            parsed_url.port
        except (UnicodeError, ValueError):
            raise ValueError("Malformed URL.") from None
        if (
            parsed_url.scheme not in supported_schemes
            or not hostname
            or any(character.isspace() for character in hostname)
        ):
            raise ValueError("Malformed URL.")

    @staticmethod
    def _validate_complete_cross_graph(
        *,
        model_rules: Dict[str, Any],
        fallback_rules: Dict[str, Dict[str, Any]],
        operation_rules: Dict[str, Dict[str, Dict[str, Any]]],
        fusion_rules: Dict[str, Dict[str, Any]],
        router_rules: Dict[str, Dict[str, Any]],
    ) -> None:
        fallback_names = set(fallback_rules)
        fusion_names = set(fusion_rules)
        router_names = set(router_rules)
        operation_names = {
            model_name
            for section in operation_rules.values()
            for model_name in section
        }
        aliases = set(model_rules.get("aliases", {}))
        collisions = (fallback_names & fusion_names) | (
            aliases & (fusion_names | router_names | operation_names)
        )
        if collisions:
            raise ValueError(f"Gateway model name collision: '{sorted(collisions)[0]}'.")

        for gateway_model_name, config in operation_rules.get("web_search", {}).items():
            query_model = config.get("query_model") if isinstance(config, dict) else None
            if query_model is not None and query_model not in fallback_names:
                raise ValueError(
                    f"Web search model '{gateway_model_name}' references unknown query_model "
                    f"'{query_model}' (must be a gateway chat model in fallback rules)."
                )

        for fusion_name, config in fusion_rules.items():
            web_tools = config.get("web_tools")
            if not isinstance(web_tools, Mapping):
                continue
            search_model = web_tools.get("search_model")
            read_model = web_tools.get("read_model")
            if search_model not in operation_rules.get("web_search", {}):
                raise ValueError(
                    f"Fusion model '{fusion_name}' references unknown web search model '{search_model}'."
                )
            if read_model is not None and read_model not in operation_rules.get("web_read", {}):
                raise ValueError(
                    f"Fusion model '{fusion_name}' references unknown web read model '{read_model}'."
                )
            for target in (search_model, read_model):
                if target is not None and is_model_excluded(str(target), model_rules):
                    raise ValueError(f"Fusion model '{fusion_name}' references an excluded model.")

        for router_name, config in router_rules.items():
            internal_targets = [config.get("selector_model")]
            for target in config.get("targets", []):
                internal_targets.append(target.get("model") or target.get("gateway_model"))
            if any(
                target is not None and is_model_excluded(str(target), model_rules)
                for target in internal_targets
            ):
                raise ValueError(f"Router model '{router_name}' references an excluded model.")

        prefixes = model_rules.get("prefixes", [])
        for prefix in prefixes if isinstance(prefixes, list) else []:
            target = prefix.get("target") if isinstance(prefix, Mapping) else None
            if target is not None and is_model_excluded(str(target), model_rules):
                raise ValueError("A model prefix references an excluded model.")
            target_prefix = prefix.get("target_prefix") if isinstance(prefix, Mapping) else None
            if target_prefix is not None and is_model_excluded(str(target_prefix), model_rules):
                raise ValueError("A model prefix target namespace is excluded.")

        internal_operation_fields = {
            "query_model",
            "search_model",
            "read_model",
            "rerank_model",
            "analysis_model",
            "fast_model",
            "smart_model",
            "strategic_model",
            "embedding_model",
            "image_generation_model",
        }
        for section in operation_rules.values():
            for config in section.values():
                for field_name in internal_operation_fields:
                    target = config.get(field_name)
                    if target is not None and is_model_excluded(str(target), model_rules):
                        raise ValueError("An operation rule references an excluded model.")

    def _build_providers_config(self, raw_provider_list: Any) -> Dict[str, ProviderDetails]:
        if not isinstance(raw_provider_list, list):
            raise ValueError("Invalid format: Expected a list of provider objects.")

        providers_config_temp: Dict[str, ProviderDetails] = {}
        seen_provider_names: set[str] = set()
        for item_dict in raw_provider_list:
            validated_entry = ProviderConfig.model_validate(item_dict)
            provider_name = list(validated_entry.root.keys())[0]
            if provider_name in seen_provider_names:
                raise ValueError(
                    f"Duplicate provider '{provider_name}' found in providers.json. Provider names must be unique."
                )
            provider_details = validated_entry.root[provider_name]
            seen_provider_names.add(provider_name)
            providers_config_temp[provider_name] = provider_details

        return providers_config_temp

    def _build_fallback_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of rule objects.")

        fallback_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            rule = ModelFallbackConfig(**item)
            if rule.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{rule.gateway_model_name}' found in models_fallback_rules.json. Gateway model names must be unique."
                )
            seen_gateway_model_names.add(rule.gateway_model_name)
            rule_config = {
                "fallback_models": [fm.model_dump(exclude_none=True) for fm in rule.fallback_models],
                "rotate_models": rule.rotate_models,
                "dynamic_penalty": rule.dynamic_penalty,
                "strip_think_tags": rule.strip_think_tags,
                "compress_tool_results": rule.compress_tool_results,
                "tool_call_rescue": rule.tool_call_rescue,
            }
            if rule.max_total_attempts is not None:
                rule_config["max_total_attempts"] = rule.max_total_attempts
            if rule.context_overflow_fallback is not None:
                rule_config["context_overflow_fallback"] = rule.context_overflow_fallback.model_dump(exclude_none=True)
            fallback_rules_temp[rule.gateway_model_name] = rule_config

        return fallback_rules_temp

    def _build_model_rules_config(self, raw_rules: Any) -> Dict[str, Any]:
        if raw_rules in (None, ""):
            return {}
        config = ModelRulesConfig.model_validate(raw_rules)
        model_rules = config.model_dump(exclude_none=True)
        return model_rules

    def _model_pool_fallback_rules(
        self,
        model_rules: Dict[str, Any],
        base_fallback_rules: Dict[str, Dict[str, Any]] | None = None,
    ) -> Dict[str, Dict[str, Any]]:
        pool_rules: dict[str, dict[str, Any]] = {}
        upstream_model_pools = model_rules.get("upstream_model_pools", {})
        if not isinstance(upstream_model_pools, dict):
            return pool_rules

        effective_base_rules = self.fallback_rules if base_fallback_rules is None else base_fallback_rules
        for gateway_model_name, pool_config in upstream_model_pools.items():
            if gateway_model_name in effective_base_rules:
                raise ValueError(
                    f"upstream_model_pools entry '{gateway_model_name}' conflicts with an existing fallback rule."
                )
            fallback_models = pool_config.get("fallback_models", [])
            pool_rule = {
                "fallback_models": fallback_models,
                "rotate_models": bool(pool_config.get("rotate_models", False)),
                "dynamic_penalty": bool(pool_config.get("dynamic_penalty", False)),
                "strip_think_tags": bool(pool_config.get("strip_think_tags", False)),
                "compress_tool_results": bool(pool_config.get("compress_tool_results", False)),
                "tool_call_rescue": bool(pool_config.get("tool_call_rescue", False)),
            }
            if pool_config.get("max_total_attempts") is not None:
                pool_rule["max_total_attempts"] = pool_config["max_total_attempts"]
            if pool_config.get("context_overflow_fallback") is not None:
                pool_rule["context_overflow_fallback"] = pool_config["context_overflow_fallback"]
            pool_rules[gateway_model_name] = pool_rule
        return pool_rules

    def _validate_model_rules_mapping(
        self,
        model_rules: Dict[str, Any],
        combined_fallback_rules: Dict[str, Dict[str, Any]],
    ) -> None:
        aliases = model_rules.get("aliases", {})
        if isinstance(aliases, dict):
            for alias, target in aliases.items():
                if alias in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules alias '{alias}' conflicts with an existing fallback rule."
                    )
                if target not in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules alias '{alias}' references unknown target model '{target}'."
                    )
                if is_model_excluded(alias, model_rules) or is_model_excluded(target, model_rules):
                    raise ValueError(
                        f"model_rules alias '{alias}' must not reference an excluded model."
                    )

        prefixes = model_rules.get("prefixes", [])
        if isinstance(prefixes, list):
            seen_prefixes: set[str] = set()
            for prefix_rule in prefixes:
                prefix = prefix_rule.get("prefix") if isinstance(prefix_rule, dict) else None
                if not isinstance(prefix, str):
                    continue
                if prefix in seen_prefixes:
                    raise ValueError(f"Duplicate model_rules prefix '{prefix}'.")
                seen_prefixes.add(prefix)
                target = prefix_rule.get("target")
                if target and target not in combined_fallback_rules:
                    raise ValueError(
                        f"model_rules prefix '{prefix}' references unknown target model '{target}'."
                    )

    def _build_fusion_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of fusion model objects.")

        fusion_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            config = FusionModelConfig(**item)
            if config.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{config.gateway_model_name}' found in models_fusion_rules.json. "
                    "Gateway model names must be unique."
                )
            seen_gateway_model_names.add(config.gateway_model_name)
            data = config.model_dump(exclude_none=True)
            data.pop("gateway_model_name", None)
            fusion_rules_temp[config.gateway_model_name] = data

        return fusion_rules_temp

    def _build_router_rules_config(self, raw_rules: Any) -> Dict[str, Dict[str, Any]]:
        if raw_rules in (None, ""):
            raw_rules = []
        if not isinstance(raw_rules, list):
            raise ValueError("Invalid format: Expected a list of router model objects.")

        router_rules_temp: Dict[str, Dict[str, Any]] = {}
        seen_gateway_model_names: set[str] = set()
        for item in raw_rules:
            config = RouterModelConfig(**item)
            if config.gateway_model_name in seen_gateway_model_names:
                raise ValueError(
                    "Duplicate gateway_model_name "
                    f"'{config.gateway_model_name}' found in models_router_rules.json. "
                    "Gateway model names must be unique."
                )
            seen_gateway_model_names.add(config.gateway_model_name)
            data = config.model_dump(exclude_none=True)
            data.pop("gateway_model_name", None)
            router_rules_temp[config.gateway_model_name] = data

        return router_rules_temp

    def _build_operation_config(self, raw_rules: Any) -> Dict[str, Dict[str, Dict[str, Any]]]:
        operation_rules = ModelsOperationConfig.model_validate(raw_rules)
        operation_config_temp = _empty_operation_rules()

        for section_name, section_rules in (
            ("embeddings", operation_rules.embeddings),
            ("rerank", operation_rules.rerank),
            ("images_generations", operation_rules.images_generations),
            ("images_edits", operation_rules.images_edits),
            ("audio_speech", operation_rules.audio_speech),
            ("audio_transcriptions", operation_rules.audio_transcriptions),
            ("web_search", operation_rules.web_search),
            ("web_read", operation_rules.web_read),
            ("web_research", operation_rules.web_research),
            ("web_deep_research", operation_rules.web_deep_research),
            ("pdf_conversions", operation_rules.pdf_conversions),
        ):
            if section_name not in operation_config_temp:
                if not section_rules:
                    continue
                operation_config_temp[section_name] = {}

            section_config = operation_config_temp[section_name]
            seen_gateway_model_names: set[str] = set()
            for item in section_rules:
                if item.gateway_model_name in seen_gateway_model_names:
                    raise ValueError(
                        "Duplicate gateway_model_name "
                        f"'{item.gateway_model_name}' found in {section_name} operation routes. "
                        "Gateway model names must be unique within a section."
                    )

                seen_gateway_model_names.add(item.gateway_model_name)
                item_config = item.model_dump(exclude_none=True)
                routes_attr = getattr(item, "routes", None)
                if routes_attr is not None:
                    item_config["routes"] = [route.model_dump(exclude_none=True) for route in routes_attr]
                item_config.pop("gateway_model_name", None)
                section_config[item.gateway_model_name] = item_config

        return operation_config_temp

    def validate_fallback_rules_mapping(
        self,
        fallback_rules_to_validate: Dict[str, Dict[str, Any]],
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> None:
        effective_providers = self.providers_config if providers_config is None else providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating fallback rules.")

        for gateway_model_name, config in fallback_rules_to_validate.items():
            fallback_models = config.get("fallback_models", [])
            if not fallback_models:
                raise ValueError(f"Gateway model '{gateway_model_name}' must have at least one fallback model defined.")

            for fallback_model_rule in fallback_models:
                self._validate_single_fallback_rule(
                    gateway_model_name,
                    fallback_model_rule,
                    effective_providers,
                )

            context_overflow_fallback = config.get("context_overflow_fallback")
            if context_overflow_fallback:
                self._validate_single_fallback_rule(
                    gateway_model_name,
                    context_overflow_fallback,
                    effective_providers,
                    rule_label="context_overflow_fallback",
                )

    def _validate_single_fallback_rule(
        self,
        gateway_model_name: str,
        fallback_model_rule: Dict[str, Any],
        effective_providers: Dict[str, ProviderDetails],
        rule_label: str = "fallback rule",
    ) -> None:
        provider = fallback_model_rule.get("provider")
        model = fallback_model_rule.get("model")

        if not provider:
            raise ValueError(f"'provider' is missing for a {rule_label} under '{gateway_model_name}'.")
        if not model:
            raise ValueError(
                f"'model' is missing for a {rule_label} under '{gateway_model_name}' (provider: {provider})."
            )
        if provider not in effective_providers:
            raise ValueError(
                f"Invalid provider '{provider}' used in {rule_label} for '{gateway_model_name}'. "
                "Provider not found in configuration."
            )
        provider_config = effective_providers[provider]
        upstream_key_pool = fallback_model_rule.get("upstream_key_pool")
        if upstream_key_pool:
            if upstream_key_pool not in provider_config.upstream_key_pools:
                raise ValueError(
                    f"Invalid upstream_key_pool '{upstream_key_pool}' used in {rule_label} "
                    f"for '{gateway_model_name}' (provider: {provider}). Pool not found in provider configuration."
                )
        elif provider_config.apikey is None and provider_config.upstream_key_pools:
            raise ValueError(
                f"'upstream_key_pool' is required for {rule_label} under '{gateway_model_name}' "
                f"because provider '{provider}' is configured with upstream_key_pools but no legacy apikey."
            )

        _validate_provider_name_list(
            fallback_model_rule.get("providers_order"),
            "providers_order",
        )

    def validate_fusion_rules_mapping(
        self,
        fusion_rules_to_validate: Dict[str, Dict[str, Any]],
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> None:
        if not fusion_rules_to_validate:
            # No fusion models configured — nothing references providers, so skip validation.
            return

        effective_providers = self.providers_config if providers_config is None else providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating fusion rules.")

        for gateway_model_name, config in fusion_rules_to_validate.items():
            panel = config.get("panel", [])
            if not panel:
                raise ValueError(
                    f"Fusion model '{gateway_model_name}' must have at least one panel member."
                )
            if len(panel) > FUSION_PANEL_MAX:
                raise ValueError(
                    f"Fusion model '{gateway_model_name}' has {len(panel)} panel members; "
                    f"the maximum is {FUSION_PANEL_MAX}."
                )

            reserve = config.get("reserve") or []
            if len(reserve) > FUSION_PANEL_MAX:
                raise ValueError(
                    f"Fusion model '{gateway_model_name}' has {len(reserve)} reserve models; "
                    f"the maximum is {FUSION_PANEL_MAX}."
                )

            members: List[tuple[str, Dict[str, Any]]] = [("panel member", member) for member in panel]
            members.extend(("reserve model", member) for member in reserve)
            members.append(("main_model", config.get("main_model") or {}))
            judge_model = config.get("judge_model")
            if judge_model:
                members.append(("judge_model", judge_model))

            for role_label, member in members:
                provider = member.get("provider")
                if not provider:
                    raise ValueError(
                        f"'provider' is missing for a {role_label} under fusion model '{gateway_model_name}'."
                    )
                if not member.get("model"):
                    raise ValueError(
                        f"'model' is missing for a {role_label} under fusion model '{gateway_model_name}'."
                    )
                if provider not in effective_providers:
                    raise ValueError(
                        f"Invalid provider '{provider}' used in {role_label} for fusion model "
                        f"'{gateway_model_name}'. Provider not found in configuration."
                    )

    def validate_router_rules_mapping(
        self,
        router_rules_to_validate: Dict[str, Dict[str, Any]],
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        fusion_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        if not router_rules_to_validate:
            return

        effective_fallback_rules = self.fallback_rules if fallback_rules is None else fallback_rules
        effective_fusion_rules = self.fusion_rules if fusion_rules is None else fusion_rules
        if not effective_fallback_rules:
            raise ValueError("Fallback rules must be loaded before validating router rules.")

        for router_model_name, config in router_rules_to_validate.items():
            if router_model_name in effective_fallback_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' conflicts with an existing fallback rule."
                )
            if router_model_name in effective_fusion_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' conflicts with an existing Fusion rule."
                )

            selector_model = config.get("selector_model")
            if selector_model not in effective_fallback_rules:
                raise ValueError(
                    f"Router model '{router_model_name}' references unknown selector_model "
                    f"'{selector_model}' (must be a gateway chat model in fallback rules)."
                )

            seen_target_ids: set[str] = set()
            for target in config.get("targets", []):
                target_type = target.get("type")
                if target_type == "gateway_model":
                    target_model = target.get("model")
                    target_id = f"gateway:{target_model}"
                    if target_model not in effective_fallback_rules:
                        raise ValueError(
                            f"Router model '{router_model_name}' references unknown target model "
                            f"'{target_model}' (must be a gateway chat model in fallback rules)."
                        )
                elif target_type == "fallback_entry":
                    target_gateway_model = target.get("gateway_model")
                    target_index = target.get("index")
                    target_id = f"fallback_entry:{target_gateway_model}:{target_index}"
                    target_rule = effective_fallback_rules.get(target_gateway_model)
                    if target_rule is None:
                        raise ValueError(
                            f"Router model '{router_model_name}' references unknown fallback_entry "
                            f"gateway_model '{target_gateway_model}'."
                        )
                    fallback_models = target_rule.get("fallback_models") or []
                    if not isinstance(target_index, int) or target_index >= len(fallback_models):
                        raise ValueError(
                            f"Router model '{router_model_name}' references fallback_entry "
                            f"'{target_gateway_model}' index {target_index}, but the fallback chain "
                            f"has {len(fallback_models)} entries."
                        )
                else:
                    raise ValueError(
                        f"Router model '{router_model_name}' has unsupported target type '{target_type}'."
                    )

                if target_id in seen_target_ids:
                    raise ValueError(
                        f"Router model '{router_model_name}' contains duplicate target '{target_id}'."
                    )
                seen_target_ids.add(target_id)

    def validate_operation_routes(
        self,
        operation_routes_to_validate: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> None:
        effective_operation_routes = (
            self.operation_rules if operation_routes_to_validate is None else operation_routes_to_validate
        )
        effective_providers = self.providers_config if providers_config is None else providers_config
        if not effective_providers:
            raise ValueError("Providers must be loaded before validating operation routes.")
        effective_fallback_rules = (
            self.fallback_rules if fallback_rules is None else fallback_rules
        )

        for section_name, section_routes in effective_operation_routes.items():
            for gateway_model_name, config in section_routes.items():
                routes = config.get("routes", [])
                if not routes:
                    if section_name in {
                        "web_search",
                        "web_read",
                        "web_research",
                        "web_deep_research",
                    }:
                        continue
                    raise ValueError(
                        f"Gateway model '{gateway_model_name}' in '{section_name}' must have at least one route defined."
                    )

                for route in routes:
                    provider = route.get("provider")
                    if not provider:
                        raise ValueError(
                            f"'provider' is missing for an operation route under '{gateway_model_name}' in "
                            f"'{section_name}'."
                        )
                    if provider not in effective_providers:
                        raise ValueError(
                            f"Invalid provider '{provider}' used in operation route for '{gateway_model_name}' "
                            f"in '{section_name}'. Provider not found in configuration."
                        )

        web_search_models = set(effective_operation_routes.get("web_search", {}))
        web_read_models = set(effective_operation_routes.get("web_read", {}))
        rerank_models = set(effective_operation_routes.get("rerank", {}))
        embedding_models = set(effective_operation_routes.get("embeddings", {}))
        image_generation_models = set(effective_operation_routes.get("images_generations", {}))
        chat_models = set(effective_fallback_rules or {})
        for gateway_model_name, config in effective_operation_routes.get("web_research", {}).items():
            search_model = config.get("search_model") if isinstance(config, dict) else None
            read_model = config.get("read_model") if isinstance(config, dict) else None
            rerank_model = config.get("rerank_model") if isinstance(config, dict) else None
            analysis_model = config.get("analysis_model") if isinstance(config, dict) else None
            if search_model not in web_search_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown search_model '{search_model}'."
                )
            if read_model not in web_read_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown read_model '{read_model}'."
                )
            if rerank_model is not None and rerank_model not in rerank_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown rerank_model "
                    f"'{rerank_model}' (must be a gateway rerank model)."
                )
            if analysis_model not in chat_models:
                raise ValueError(
                    f"Web research model '{gateway_model_name}' references unknown analysis_model "
                    f"'{analysis_model}' (must be a gateway chat model in fallback rules)."
                )
        for gateway_model_name, config in effective_operation_routes.get("web_deep_research", {}).items():
            search_model = config.get("search_model") if isinstance(config, dict) else None
            read_model = config.get("read_model") if isinstance(config, dict) else None
            fast_model = config.get("fast_model") if isinstance(config, dict) else None
            smart_model = config.get("smart_model") if isinstance(config, dict) else None
            strategic_model = config.get("strategic_model") if isinstance(config, dict) else None
            embedding_model = config.get("embedding_model") if isinstance(config, dict) else None
            image_generation_model = config.get("image_generation_model") if isinstance(config, dict) else None
            if search_model is not None and search_model not in web_search_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown search_model '{search_model}'."
                )
            if read_model is not None and read_model not in web_read_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown read_model '{read_model}'."
                )
            for field_name, value in (
                ("fast_model", fast_model),
                ("smart_model", smart_model),
                ("strategic_model", strategic_model),
            ):
                if value not in chat_models:
                    raise ValueError(
                        f"Web deep research model '{gateway_model_name}' references unknown "
                        f"{field_name} '{value}' (must be a gateway chat model in fallback rules)."
                    )
            if embedding_model is not None and embedding_model not in embedding_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown embedding_model "
                    f"'{embedding_model}' (must be a gateway embeddings model)."
                )
            if image_generation_model is not None and image_generation_model not in image_generation_models:
                raise ValueError(
                    f"Web deep research model '{gateway_model_name}' references unknown image_generation_model "
                    f"'{image_generation_model}' (must be a gateway images_generations model)."
                )

    def validate_fallback_operation_consistency(
        self,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        operation_rules: Optional[Dict[str, Dict[str, Dict[str, Any]]]] = None,
    ) -> None:
        """Cross-validate that chat models from fallback rules are consistent
        with operation rules.

        Catches configuration bugs where a model referenced by an operation
        rule (e.g. web_research.analysis_model) is not defined as a chat model
        in the fallback rules, or where a chat model exists but has no fallback
        chain and is only used by operations — which could leave it unreachable
        for direct chat routing.
        """
        effective_fallback = self.fallback_rules if fallback_rules is None else fallback_rules
        effective_operation = self.operation_rules if operation_rules is None else operation_rules

        chat_models = set(effective_fallback.keys())

        # All *analysis_model*, *fast_model*, *smart_model*, *strategic_model*
        # in operation rules must be defined in fallback (chat) rules.
        for section_name in ("web_research", "web_deep_research"):
            section = effective_operation.get(section_name, {})
            for gw_model, config in section.items():
                if not isinstance(config, dict):
                    continue
                for field in ("analysis_model", "fast_model", "smart_model", "strategic_model"):
                    value = config.get(field)
                    if value and value not in chat_models:
                        raise ValueError(
                            f"Operation rule '{section_name}.{gw_model}' references "
                            f"{field}='{value}', which is not defined in fallback (chat) rules."
                        )

    def parse_and_validate_providers_payload(
        self,
        payload_text: str,
        *,
        strict_env: bool = False,
    ) -> Dict[str, ProviderDetails]:
        raw_provider_list = json5.loads(payload_text)
        providers_config = self._build_providers_config(raw_provider_list)
        if not self._perform_provider_semantic_validation(
            providers_config,
            raise_on_error=strict_env,
            strict_env=strict_env,
        ):
            raise ValueError("Semantic validation failed for providers configuration.")
        return providers_config

    def parse_and_validate_fallback_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        fallback_rules = self._build_fallback_rules_config(raw_rules)
        with rule_validation():
            self.validate_fallback_rules_mapping(fallback_rules, providers_config=providers_config)
        return fallback_rules

    def parse_and_validate_model_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        model_rules, _combined_fallback_rules = self.parse_and_validate_model_rules_payload_with_fallbacks(
            payload_text,
            providers_config=providers_config,
            fallback_rules=fallback_rules,
        )
        return model_rules

    def parse_and_validate_model_rules_payload_with_fallbacks(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
        raw_rules = json5.loads(payload_text)
        model_rules = self._build_model_rules_config(raw_rules)
        base_fallback_rules = self.fallback_rules if fallback_rules is None else fallback_rules
        combined_fallback_rules = self._combine_fallback_rules_with_model_rules(
            model_rules,
            base_fallback_rules,
            providers_config=providers_config,
        )
        return model_rules, combined_fallback_rules

    def _combine_fallback_rules_with_model_rules(
        self,
        model_rules: Dict[str, Any],
        base_fallback_rules: Dict[str, Dict[str, Any]],
        *,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        pool_rules = self._model_pool_fallback_rules(model_rules, base_fallback_rules)
        combined_fallback_rules = {**base_fallback_rules, **pool_rules}
        with rule_validation():
            self.validate_fallback_rules_mapping(
                combined_fallback_rules,
                providers_config=providers_config,
            )
            self._validate_model_rules_mapping(model_rules, combined_fallback_rules)
        return combined_fallback_rules

    def parse_and_validate_operation_routes_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Dict[str, Any]]]:
        raw_rules = json5.loads(payload_text)
        operation_routes = self._build_operation_config(raw_rules)
        with rule_validation():
            self.validate_operation_routes(
                operation_routes,
                providers_config=providers_config,
                fallback_rules=fallback_rules,
            )
        return operation_routes

    def parse_and_validate_router_rules_payload(
        self,
        payload_text: str,
        fallback_rules: Optional[Dict[str, Dict[str, Any]]] = None,
        fusion_rules: Optional[Dict[str, Dict[str, Any]]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        router_rules = self._build_router_rules_config(raw_rules)
        with rule_validation():
            self.validate_router_rules_mapping(
                router_rules,
                fallback_rules=fallback_rules,
                fusion_rules=fusion_rules,
            )
        return router_rules

    def _perform_provider_semantic_validation(
        self,
        providers_to_validate: Dict[str, ProviderDetails],
        raise_on_error: bool = False,
        strict_env: bool = False,
    ) -> bool:
        """
        Performs semantic validation on a dictionary of provider configurations.
        Checks for fallback provider existence and API key environment variables.
        Returns True if all critical checks pass, False otherwise.
        If raise_on_error is True, raises ConfigError on critical failure.
        """
        all_valid = True
        fallback_provider_name = settings.fallback_provider
        if fallback_provider_name not in providers_to_validate:
            available_providers = ", ".join(sorted(providers_to_validate.keys())) or "<none>"
            message = (
                f"FALLBACK_PROVIDER '{fallback_provider_name}' is not defined in providers.json. "
                f"Available providers: {available_providers}."
            )
            logging.error(message)
            if raise_on_error:
                raise ConfigError(message)
            all_valid = False # Mark as invalid but continue checking other things if not raising

        env_errors: list[str] = []
        for provider_name, config in providers_to_validate.items():
            for field_name, field_value in (("apikey", config.apikey), ("proxy", config.proxy)):
                if not field_value:
                    continue

                try:
                    env_name = _explicit_env_reference_name(field_value, field_name)
                except ConfigError as exc:
                    message = str(exc)
                    logging.error(message)
                    env_errors.append(message)
                    continue

                if env_name is None:
                    if _looks_like_env_reference(field_value):
                        logging.warning(
                            "Provider '%s' field '%s' value '%s' looks like an environment variable name, "
                            "but '${VAR}' syntax was not used; treating it as a literal.",
                            provider_name,
                            field_name,
                            field_value,
                        )
                    continue

                resolved_env_value = os.getenv(env_name)
                if resolved_env_value:
                    if field_name == "apikey" and is_placeholder_secret(resolved_env_value):
                        message = placeholder_secret_error(env_name, resolved_env_value)
                        logging.error(message)
                        env_errors.append(message)
                    continue

                message = _provider_env_reference_error(provider_name, field_name, env_name)
                logging.error(message)
                env_errors.append(message)

            for pool_name, pool_config in config.upstream_key_pools.items():
                for key_index, key_spec in enumerate(pool_config.keys):
                    field_name = f"upstream_key_pools.{pool_name}.keys[{key_index}].apikey"
                    try:
                        env_name = _explicit_env_reference_name(key_spec.apikey, field_name)
                    except ConfigError as exc:
                        message = str(exc)
                        logging.error(message)
                        env_errors.append(message)
                        continue

                    if env_name is None:
                        if _looks_like_env_reference(key_spec.apikey):
                            logging.warning(
                                "Provider '%s' field '%s' value '%s' looks like an environment variable name, "
                                "but '${VAR}' syntax was not used; treating it as a literal.",
                                provider_name,
                                field_name,
                                key_spec.apikey,
                            )
                        continue

                    resolved_env_value = os.getenv(env_name)
                    if resolved_env_value:
                        if is_placeholder_secret(resolved_env_value):
                            message = placeholder_secret_error(env_name, resolved_env_value)
                            logging.error(message)
                            env_errors.append(message)
                        continue

                    message = _provider_env_reference_error(provider_name, field_name, env_name)
                    logging.error(message)
                    env_errors.append(message)

        if env_errors:
            if raise_on_error:
                raise ConfigError("; ".join(env_errors))
            all_valid = False

        return all_valid

    def _validate_providers(self):
        """Legacy wrapper for initial load validation. Raises ConfigError on failure."""
        if not self._perform_provider_semantic_validation(self.providers_config, raise_on_error=True):
            # This path should not normally be reachable (raise_on_error=True raises before returning False),
            # but keep a safeguard so a silent failure can't leak through.
            logging.critical("Provider semantic validation failed during initial load.")
            raise ConfigError("Provider semantic validation failed during initial load.")

    def parse_and_validate_fusion_rules_payload(
        self,
        payload_text: str,
        providers_config: Optional[Dict[str, ProviderDetails]] = None,
    ) -> Dict[str, Dict[str, Any]]:
        raw_rules = json5.loads(payload_text)
        fusion_rules = self._build_fusion_rules_config(raw_rules)
        with rule_validation():
            self.validate_fusion_rules_mapping(fusion_rules, providers_config=providers_config)
        return fusion_rules
    def _validate_fallback_rules(self):
        """Performs post-load validation on fallback rules."""
        if not self.providers_config:
             # Ensure providers are loaded first if validation depends on them
             logging.warning("Providers not loaded yet. Loading providers before validating fallback rules.")
             self.load_providers()
             if not self.providers_config:
                 message = "Failed to load providers, cannot validate fallback rules."
                 logging.error(message)
                 raise ConfigError(message)
        try:
            self.validate_fallback_rules_mapping(self.fallback_rules, providers_config=self.providers_config)
        except ValueError as e:
            logging.error(str(e))
            raise ConfigError(str(e)) from e

    def _validate_operation_rules(self):
        """Performs post-load validation on operation rules."""
        if not self.providers_config:
            logging.warning("Providers not loaded yet. Loading providers before validating operation rules.")
            self.load_providers()
            if not self.providers_config:
                message = "Failed to load providers, cannot validate operation rules."
                logging.error(message)
                raise ConfigError(message)
        try:
            self.validate_operation_routes(providers_config=self.providers_config)
        except ValueError as e:
            logging.error(str(e))
            raise ConfigError(str(e)) from e
