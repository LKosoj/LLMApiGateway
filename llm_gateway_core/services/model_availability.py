"""Startup check: verify configured models exist on each provider.

Fetches `/models` from every provider referenced in the fallback rules (and
operation rules) and diffs the returned catalog against what the gateway is
configured to route. Can run in ``warn`` mode (log only) or ``strict`` mode
(abort startup).
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

import httpx

from ..config.loader import ProviderDetails
from .provider_models import ProviderModelsService

logger = logging.getLogger(__name__)


@dataclass
class ProviderCheckResult:
    provider: str
    available_models: list[str] = field(default_factory=list)
    missing_models: list[str] = field(default_factory=list)
    error: str | None = None


@dataclass
class ModelAvailabilityReport:
    results: list[ProviderCheckResult]

    @property
    def has_failures(self) -> bool:
        return any(r.error is not None or r.missing_models for r in self.results)

    @property
    def missing_total(self) -> int:
        return sum(len(r.missing_models) for r in self.results)


def _collect_provider_model_pairs(
    fallback_rules: Mapping[str, Mapping[str, Any]] | None,
    operation_rules: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
) -> dict[str, set[str]]:
    """Return provider -> set(model names) that the gateway is configured to use."""
    pairs: dict[str, set[str]] = {}

    def _add(provider: Any, model: Any) -> None:
        if not isinstance(provider, str) or not isinstance(model, str):
            return
        pairs.setdefault(provider, set()).add(model)

    if isinstance(fallback_rules, Mapping):
        for rule in fallback_rules.values():
            if not isinstance(rule, Mapping):
                continue
            fallback_models = rule.get("fallback_models") or []
            if isinstance(fallback_models, Iterable):
                for fm in fallback_models:
                    if isinstance(fm, Mapping):
                        _add(fm.get("provider"), fm.get("model"))
            context_overflow = rule.get("context_overflow_fallback")
            if isinstance(context_overflow, Mapping):
                _add(context_overflow.get("provider"), context_overflow.get("model"))

    if isinstance(operation_rules, Mapping):
        for section in operation_rules.values():
            if not isinstance(section, Mapping):
                continue
            for route_config in section.values():
                if not isinstance(route_config, Mapping):
                    continue
                routes = route_config.get("routes")
                if isinstance(routes, Iterable) and not isinstance(routes, (str, bytes)):
                    for route in routes:
                        if isinstance(route, Mapping):
                            _add(route.get("provider"), route.get("model"))
                    continue
                _add(route_config.get("provider"), route_config.get("model"))

    return pairs


async def _check_single_provider(
    provider_name: str,
    configured_models: set[str],
    providers_config: Mapping[str, ProviderDetails],
    service: ProviderModelsService,
    http_client: httpx.AsyncClient,
) -> ProviderCheckResult:
    provider_config = providers_config.get(provider_name)
    if provider_config is None:
        return ProviderCheckResult(
            provider=provider_name,
            error=f"Provider '{provider_name}' not found in providers.json",
        )

    try:
        available = await service.get_models(provider_name, provider_config, http_client)
    except ValueError as exc:
        return ProviderCheckResult(provider=provider_name, error=str(exc))
    except Exception as exc:  # defensive — asyncio/timeouts/etc.
        return ProviderCheckResult(
            provider=provider_name,
            error=f"Unexpected error fetching /models: {exc}",
        )

    missing = sorted(m for m in configured_models if m not in set(available))
    return ProviderCheckResult(
        provider=provider_name,
        available_models=list(available),
        missing_models=missing,
    )


async def verify_configured_models(
    *,
    providers_config: Mapping[str, ProviderDetails],
    fallback_rules: Mapping[str, Mapping[str, Any]] | None,
    operation_rules: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    provider_models_service: ProviderModelsService,
    http_client: httpx.AsyncClient,
) -> ModelAvailabilityReport:
    """Fetch /models from every referenced provider and diff against config."""
    pairs = _collect_provider_model_pairs(fallback_rules, operation_rules)
    if not pairs:
        return ModelAvailabilityReport(results=[])

    tasks = [
        _check_single_provider(
            provider_name=name,
            configured_models=models,
            providers_config=providers_config,
            service=provider_models_service,
            http_client=http_client,
        )
        for name, models in pairs.items()
    ]
    results = await asyncio.gather(*tasks)
    return ModelAvailabilityReport(results=list(results))


def log_availability_report(report: ModelAvailabilityReport) -> None:
    if not report.results:
        logger.info("Startup model-availability check: nothing to verify.")
        return

    for result in report.results:
        if result.error is not None:
            logger.warning(
                "Startup model check: provider '%s' unavailable — %s",
                result.provider, result.error,
            )
            continue
        if result.missing_models:
            logger.warning(
                "Startup model check: provider '%s' missing %d configured model(s): %s",
                result.provider, len(result.missing_models), ", ".join(result.missing_models),
            )
        else:
            logger.info(
                "Startup model check: provider '%s' OK (%d models available).",
                result.provider, len(result.available_models),
            )


async def run_startup_model_verification(
    *,
    mode: str,
    providers_config: Mapping[str, ProviderDetails],
    fallback_rules: Mapping[str, Mapping[str, Any]] | None,
    operation_rules: Mapping[str, Mapping[str, Mapping[str, Any]]] | None,
    provider_models_service: ProviderModelsService,
    http_client: httpx.AsyncClient,
) -> ModelAvailabilityReport | None:
    """Run verification according to ``mode``.

    ``mode`` must be one of ``"off"``, ``"warn"``, ``"strict"``.

    - ``off``: no-op, returns None.
    - ``warn``: run the check and log warnings.
    - ``strict``: run the check and raise RuntimeError if any model is missing
      or any provider errored.
    """
    normalized = (mode or "").strip().lower()
    if normalized in ("", "off", "false", "disabled", "no"):
        logger.info("Startup model-availability check is disabled (VERIFY_MODELS_ON_STARTUP=%s).", mode)
        return None

    if normalized not in ("warn", "strict"):
        raise ValueError(
            f"Invalid VERIFY_MODELS_ON_STARTUP value: {mode!r}. "
            "Expected one of 'off', 'warn', 'strict'."
        )

    report = await verify_configured_models(
        providers_config=providers_config,
        fallback_rules=fallback_rules,
        operation_rules=operation_rules,
        provider_models_service=provider_models_service,
        http_client=http_client,
    )
    log_availability_report(report)

    if normalized == "strict" and report.has_failures:
        raise RuntimeError(
            "Startup model verification failed. "
            f"Missing model references: {report.missing_total}. See log for details."
        )

    return report
