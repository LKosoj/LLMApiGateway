from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping


@dataclass(frozen=True)
class ModelResolution:
    requested_model: str
    effective_model: str
    matched_rule: str | None = None

    @property
    def changed(self) -> bool:
        return self.effective_model != self.requested_model


def resolve_model_name(requested_model: str, model_rules: Mapping[str, Any] | None) -> ModelResolution:
    if is_model_excluded(requested_model, model_rules):
        raise ValueError(f"Model '{requested_model}' is excluded by model_rules.")

    rules = model_rules or {}
    aliases = rules.get("aliases") if isinstance(rules, Mapping) else None
    if isinstance(aliases, Mapping) and requested_model in aliases:
        effective_model = str(aliases[requested_model])
        if is_model_excluded(effective_model, model_rules):
            raise ValueError(f"Model alias '{requested_model}' resolves to excluded model '{effective_model}'.")
        return ModelResolution(requested_model, effective_model, matched_rule="alias")

    prefixes = rules.get("prefixes") if isinstance(rules, Mapping) else None
    if isinstance(prefixes, list):
        for rule in prefixes:
            if not isinstance(rule, Mapping):
                continue
            prefix = rule.get("prefix")
            if not isinstance(prefix, str) or not requested_model.startswith(prefix):
                continue
            target = rule.get("target")
            if isinstance(target, str) and target:
                effective_model = target
            else:
                target_prefix = rule.get("target_prefix")
                if not isinstance(target_prefix, str):
                    continue
                effective_model = f"{target_prefix}{requested_model[len(prefix):]}"
            if is_model_excluded(effective_model, model_rules):
                raise ValueError(
                    f"Model prefix rule '{prefix}' resolves '{requested_model}' "
                    f"to excluded model '{effective_model}'."
                )
            return ModelResolution(requested_model, effective_model, matched_rule="prefix")

    return ModelResolution(requested_model, requested_model)


def is_model_excluded(model_name: str, model_rules: Mapping[str, Any] | None) -> bool:
    rules = model_rules or {}
    excluded_models = rules.get("excluded_models") if isinstance(rules, Mapping) else None
    if not isinstance(excluded_models, list):
        return False

    for pattern in excluded_models:
        if not isinstance(pattern, str):
            continue
        if pattern.endswith("*") and model_name.startswith(pattern[:-1]):
            return True
        if pattern == model_name:
            return True
    return False


def public_alias_entries(model_rules: Mapping[str, Any] | None) -> dict[str, str]:
    rules = model_rules or {}
    aliases = rules.get("aliases") if isinstance(rules, Mapping) else None
    if not isinstance(aliases, Mapping):
        return {}
    return {str(alias): str(target) for alias, target in aliases.items()}


def filter_excluded_models(
    gateway_models: dict[str, dict[str, Any]],
    model_rules: Mapping[str, Any] | None,
) -> dict[str, dict[str, Any]]:
    if not model_rules:
        return gateway_models
    return {
        model_name: model_entry
        for model_name, model_entry in gateway_models.items()
        if not is_model_excluded(model_name, model_rules)
    }
