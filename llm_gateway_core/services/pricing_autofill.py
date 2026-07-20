"""Discover models actually referenced by the runtime config and classify
their pricing status for the ``/v1/ui/pricing`` admin page.

``providers.json`` only lists models an operator has explicitly priced. Many
models referenced by ``models_fallback_rules.json``, ``models_operation_rules
.json``, ``models_fusion_rules.json`` and ``models_model_rules.json`` never
get an explicit entry there, so the pricing page used to hide them entirely.
This module collects every (provider, model) pair the runtime config can
actually dispatch to, then classifies each one that is missing from
``providers.json``:

* an OpenRouter model with a known catalog price gets auto-filled from that
  catalog (see :func:`classify_pricing_rows` / ``PricingSource
  .OPENROUTER_AUTOFILL``);
* everything else is surfaced with a status explaining why it has no
  configured rate, without silently writing a fabricated price.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import TYPE_CHECKING, Any

from .accounting import DEFAULT_OPERATION_COST_USD
from .model_policy import is_model_excluded

if TYPE_CHECKING:
    from ..config.loader import ConfigLoader
    from ..utils.usage_tracking import ModelCostRates

OPENROUTER_PROVIDER_NAME = "openrouter"
_OPENROUTER_FREE_SUFFIX = ":free"
_OPENROUTER_RATE_SCALE = 1_000_000


class PricingSource(str, Enum):
    """Where a pricing row's rate (or lack of one) comes from."""

    CONFIGURED = "configured"
    OPENROUTER_AUTOFILL = "openrouter_autofill"
    OPERATION_DEFAULT = "operation_default"
    UPSTREAM_ONLY = "upstream_only"
    AWAITING_OPENROUTER_CATALOG = "awaiting_openrouter_catalog"


@dataclass(frozen=True)
class PricingRow:
    provider: str
    model: str
    source: PricingSource
    input_rate: float | None = None
    output_rate: float | None = None
    default_cost_per_request: float | None = None


@dataclass(frozen=True)
class PricingAutofillAddition:
    """One OpenRouter model resolved to a real rate, ready to persist."""

    provider: str
    model: str
    input_rate: float
    output_rate: float


@dataclass(frozen=True)
class PricingClassification:
    rows: list[PricingRow]
    autofill_additions: list[PricingAutofillAddition]


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _operation_route_pairs(
    config_loader: "ConfigLoader",
    model_rules: Mapping[str, Any],
) -> set[tuple[str, str]]:
    pairs: set[tuple[str, str]] = set()
    operation_rules = _as_mapping(getattr(config_loader, "operation_rules", None))
    for section in operation_rules.values():
        # ``ConfigLoader`` parses each operation section into a mapping keyed
        # by gateway_model_name (not the on-disk list shape), so this is a
        # mapping of entries, not a list.
        for gw_name, entry in _as_mapping(section).items():
            if not isinstance(entry, Mapping):
                continue
            if is_model_excluded(gw_name, model_rules):
                continue
            for route in _as_list(entry.get("routes")):
                if not isinstance(route, Mapping):
                    continue
                provider, model = route.get("provider"), route.get("model")
                if isinstance(provider, str) and provider and isinstance(model, str) and model:
                    pairs.add((provider, model))
    return pairs


def collect_operation_used_models(config_loader: "ConfigLoader") -> set[tuple[str, str]]:
    """Return only the (provider, model) pairs used by operation_rules routes.

    Used to tell an "operation" model (embeddings/rerank/images/audio/pdf/
    web) apart from a plain chat model when classifying pricing rows.
    """
    model_rules = _as_mapping(getattr(config_loader, "model_rules", None))
    return _operation_route_pairs(config_loader, model_rules)


def collect_used_models(config_loader: "ConfigLoader") -> set[tuple[str, str]]:
    """Return every (provider, model) pair the runtime config can dispatch to.

    Scans ``fallback_rules``, ``operation_rules`` (embeddings/rerank/images/
    audio/pdf/web), ``fusion_rules`` (panel/main/judge/reserve members) and
    ``model_rules.upstream_model_pools``. ``router_rules`` targets and
    selectors reference other gateway models by name (never a provider/model
    pair directly), so they are resolved against the gateway-model index
    built while scanning the rule sets above. A gateway model excluded via
    ``model_rules.excluded_models`` contributes nothing (matches
    ``resolve_model_name``, which refuses to route to it at all).
    """
    model_rules = _as_mapping(getattr(config_loader, "model_rules", None))
    gateway_index: dict[str, set[tuple[str, str]]] = {}
    pairs: set[tuple[str, str]] = set()

    def _record(gw_name: str | None, provider: Any, model: Any) -> None:
        if not (isinstance(provider, str) and provider and isinstance(model, str) and model):
            return
        pair = (provider, model)
        pairs.add(pair)
        if gw_name:
            gateway_index.setdefault(gw_name, set()).add(pair)

    # ``ConfigLoader`` parses fallback_rules/fusion_rules/router_rules (and
    # each operation_rules section) into a mapping keyed by gateway_model_name
    # -- the on-disk files are lists with an embedded "gateway_model_name"
    # field, but the loader collapses that into the dict key and drops the
    # field from the per-entry config. Every rule-set below is therefore a
    # mapping of (gateway_model_name -> config), not a list.
    for gw_name, rule in _as_mapping(getattr(config_loader, "fallback_rules", None)).items():
        if not isinstance(rule, Mapping):
            continue
        if is_model_excluded(gw_name, model_rules):
            continue
        for fallback_model in _as_list(rule.get("fallback_models")):
            if isinstance(fallback_model, Mapping):
                _record(gw_name, fallback_model.get("provider"), fallback_model.get("model"))
        overflow = rule.get("context_overflow_fallback")
        if isinstance(overflow, Mapping):
            _record(gw_name, overflow.get("provider"), overflow.get("model"))

    operation_rules = _as_mapping(getattr(config_loader, "operation_rules", None))
    for section in operation_rules.values():
        for gw_name, entry in _as_mapping(section).items():
            if not isinstance(entry, Mapping):
                continue
            if is_model_excluded(gw_name, model_rules):
                continue
            for route in _as_list(entry.get("routes")):
                if isinstance(route, Mapping):
                    _record(gw_name, route.get("provider"), route.get("model"))

    for gw_name, rule in _as_mapping(getattr(config_loader, "fusion_rules", None)).items():
        if not isinstance(rule, Mapping):
            continue
        if is_model_excluded(gw_name, model_rules):
            continue
        for member in _as_list(rule.get("panel")):
            if isinstance(member, Mapping):
                _record(gw_name, member.get("provider"), member.get("model"))
        for key in ("main_model", "judge_model"):
            member = rule.get(key)
            if isinstance(member, Mapping):
                _record(gw_name, member.get("provider"), member.get("model"))
        for member in _as_list(rule.get("reserve")):
            if isinstance(member, Mapping):
                _record(gw_name, member.get("provider"), member.get("model"))

    upstream_model_pools = _as_mapping(model_rules.get("upstream_model_pools"))
    for gw_name, pool in upstream_model_pools.items():
        if not isinstance(gw_name, str) or is_model_excluded(gw_name, model_rules):
            continue
        pool_dict = _as_mapping(pool)
        for fallback_model in _as_list(pool_dict.get("fallback_models")):
            if isinstance(fallback_model, Mapping):
                _record(gw_name, fallback_model.get("provider"), fallback_model.get("model"))

    for router_name, rule in _as_mapping(getattr(config_loader, "router_rules", None)).items():
        if not isinstance(rule, Mapping):
            continue
        if is_model_excluded(router_name, model_rules):
            continue
        selector = rule.get("selector_model")
        if isinstance(selector, str):
            pairs |= gateway_index.get(selector, set())
        for target in _as_list(rule.get("targets")):
            if not isinstance(target, Mapping):
                continue
            if target.get("type") == "gateway_model":
                ref_name = target.get("model")
            else:
                ref_name = target.get("gateway_model")
            if isinstance(ref_name, str):
                pairs |= gateway_index.get(ref_name, set())

    return pairs


def _normalize_openrouter_lookup_id(model: str) -> str:
    if model.endswith(_OPENROUTER_FREE_SUFFIX):
        return model[: -len(_OPENROUTER_FREE_SUFFIX)]
    return model


def _valid_non_negative_rate(value: Any) -> float | None:
    if isinstance(value, bool):
        return None
    try:
        rate = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(rate) or rate < 0:
        return None
    return rate


def _default_row(provider: str, model: str, source: PricingSource) -> PricingRow:
    default_cost = DEFAULT_OPERATION_COST_USD if source is PricingSource.OPERATION_DEFAULT else None
    return PricingRow(
        provider=provider,
        model=model,
        source=source,
        default_cost_per_request=default_cost,
    )


def classify_pricing_rows(
    *,
    used_models: set[tuple[str, str]],
    configured_registry: Mapping[tuple[str, str], "ModelCostRates"],
    operation_model_pairs: set[tuple[str, str]],
    openrouter_catalog: Mapping[str, Mapping[str, Any]] | None,
) -> PricingClassification:
    """Classify every configured or used (provider, model) pair.

    ``openrouter_catalog`` is ``None`` when the OpenRouter catalog has not
    been loaded yet (service not configured, or no successful refresh yet);
    in that case every missing ``openrouter`` model is reported as
    ``awaiting_openrouter_catalog`` and nothing is proposed for autofill.
    """
    rows: list[PricingRow] = []
    autofill_additions: list[PricingAutofillAddition] = []

    all_pairs = set(configured_registry.keys()) | used_models

    for provider, model in sorted(all_pairs):
        rates = configured_registry.get((provider, model))
        if rates is not None:
            rows.append(
                PricingRow(
                    provider=provider,
                    model=model,
                    source=PricingSource.CONFIGURED,
                    input_rate=rates.input_rate,
                    output_rate=rates.output_rate,
                )
            )
            continue

        is_operation = (provider, model) in operation_model_pairs
        fallback_source = PricingSource.OPERATION_DEFAULT if is_operation else PricingSource.UPSTREAM_ONLY

        if provider != OPENROUTER_PROVIDER_NAME:
            rows.append(_default_row(provider, model, fallback_source))
            continue

        if openrouter_catalog is None:
            rows.append(_default_row(provider, model, PricingSource.AWAITING_OPENROUTER_CATALOG))
            continue

        catalog_entry = openrouter_catalog.get(_normalize_openrouter_lookup_id(model))
        pricing = catalog_entry if isinstance(catalog_entry, Mapping) else None
        prompt_rate = _valid_non_negative_rate(pricing.get("prompt")) if pricing is not None else None
        completion_rate = _valid_non_negative_rate(pricing.get("completion")) if pricing is not None else None

        if prompt_rate is not None and completion_rate is not None:
            input_rate = prompt_rate * _OPENROUTER_RATE_SCALE
            output_rate = completion_rate * _OPENROUTER_RATE_SCALE
            rows.append(
                PricingRow(
                    provider=provider,
                    model=model,
                    source=PricingSource.OPENROUTER_AUTOFILL,
                    input_rate=input_rate,
                    output_rate=output_rate,
                )
            )
            autofill_additions.append(
                PricingAutofillAddition(
                    provider=provider,
                    model=model,
                    input_rate=input_rate,
                    output_rate=output_rate,
                )
            )
        else:
            rows.append(_default_row(provider, model, fallback_source))

    return PricingClassification(rows=rows, autofill_additions=autofill_additions)
