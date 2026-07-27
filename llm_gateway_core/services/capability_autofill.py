"""F-auto: capability-field autofill for fallback-chain candidates.

Fills unset ``supports_vision``/``supports_tools``/``context_window`` fields
on ``models_fallback_rules.json`` candidates from catalog metadata (the
candidate's own provider first, the public OpenRouter catalog otherwise) and
materializes the result back into ``models_fallback_rules.json`` through the
existing transactional config-update path. A candidate field is only ever
touched while it is either unset or currently owned by a previous autofill
run (tracked via ``capabilities_autofilled``); any manually-set value is
permanently hands-off.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

import httpx

from ..config.config_store import ConfigFile
from ..config.loader import (
    ModelFallbackConfig,
    ProviderDetails,
    resolve_provider_config_api_key,
    resolve_provider_config_auth_headers,
)
from ..utils.log_redaction import safe_exception_type_name
from .config_updates import ConfigUpdateCoordinator, ConfigUpdateError
from .openrouter_free_models import (
    CapabilityMetadata,
    OpenRouterFreeModelsService,
    _openrouter_metadata_key,
    parse_capability_metadata,
)
from .provider_models import ProviderModelsService

if TYPE_CHECKING:
    from .runtime_config import RuntimeGenerationManager, RuntimeLease

logger = logging.getLogger(__name__)

CAPABILITY_FIELDS: tuple[str, ...] = ("supports_vision", "supports_tools", "context_window")


def _format_error_locations(error: ConfigUpdateError) -> str:
    """Name the config sources a failed update points at, or return ''.

    A bare error code says the write was rejected but not which file drifted;
    ``config_sources_out_of_sync`` carries that in ``errors[*]["loc"]``.
    """
    locations = sorted(
        {
            str(part)
            for entry in error.errors or ()
            for part in entry.get("loc") or ()
        }
    )
    if not locations:
        return ""
    return f" ({', '.join(locations)})"


@dataclass(frozen=True, slots=True)
class _WorklistEntry:
    """One fallback-chain candidate, with a mutable handle into the working copy."""

    gateway_model_name: str
    index: int | str  # int for fallback_models[i]; "context_overflow_fallback" for the singleton.
    candidate: dict[str, Any]


class CapabilityAutofillService:
    """Owns the F-auto materialize run and its last-run status."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._running = False
        self._last_run_at: str | None = None
        self._last_error: str | None = None
        self._last_status: dict[str, list[dict[str, Any]]] = {}

    async def get_status(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "lastRunAt": self._last_run_at,
                "lastError": self._last_error,
                "resolutions": copy.deepcopy(self._last_status),
            }

    async def materialize(
        self,
        *,
        runtime_manager: RuntimeGenerationManager,
        config_update_coordinator: ConfigUpdateCoordinator,
        shared_http_client: httpx.AsyncClient,
        openrouter_free_models_service: OpenRouterFreeModelsService,
    ) -> None:
        """Resolve unfilled capability fields and, on a real diff, write them back.

        Never raises: every failure (lease acquisition, provider fetch,
        config-update conflict) is logged and absorbed so callers (the
        startup/periodic loop and editor-save fire-and-forget task) can
        invoke this without their own error handling.
        """
        async with self._lock:
            self._running = True
            lease: RuntimeLease | None = None
            try:
                lease = runtime_manager.acquire_current()
                await self._materialize_with_lease(
                    lease,
                    config_update_coordinator=config_update_coordinator,
                    shared_http_client=shared_http_client,
                    openrouter_free_models_service=openrouter_free_models_service,
                )
                self._last_error = None
            except ConfigUpdateError as exc:
                logger.warning(
                    "Capability autofill materialize skipped a write: %s%s. "
                    "The next trigger will retry.",
                    exc.code,
                    _format_error_locations(exc),
                )
                self._last_error = f"{exc} The next trigger will retry."
            except Exception as exc:
                logger.exception("Capability autofill materialize failed.")
                self._last_error = safe_exception_type_name(exc)
            finally:
                if lease is not None:
                    lease.release()
                self._running = False
                self._last_run_at = _utc_now_iso()

    async def _materialize_with_lease(
        self,
        lease: RuntimeLease,
        *,
        config_update_coordinator: ConfigUpdateCoordinator,
        shared_http_client: httpx.AsyncClient,
        openrouter_free_models_service: OpenRouterFreeModelsService,
    ) -> None:
        snapshot = lease.snapshot
        config_loader = snapshot.config_loader
        providers_config: dict[str, ProviderDetails] = config_loader.providers_config
        base_fallback_rules: dict[str, dict[str, Any]] = config_loader._fallback_rules_base or {}

        working_rules = copy.deepcopy(base_fallback_rules)
        worklist = _collect_worklist(working_rules)

        provider_capabilities = await self._load_provider_capabilities(
            _providers_needing_resolution(worklist),
            providers_config=providers_config,
            provider_models_service=snapshot.provider_models_service,
            proxy_http_clients=snapshot.proxy_http_clients,
            shared_http_client=shared_http_client,
        )
        openrouter_index = await openrouter_free_models_service.get_capability_index()

        status_resolutions = _resolve_worklist(
            worklist,
            provider_capabilities=provider_capabilities,
            openrouter_index=openrouter_index,
        )

        if working_rules == base_fallback_rules:
            self._last_status = status_resolutions
            return

        candidate_bytes = _serialize_fallback_rules(_build_model_fallback_configs(working_rules))
        await config_update_coordinator.update(
            base_snapshot=snapshot,
            config_file=ConfigFile.FALLBACK_RULES,
            candidate_bytes=candidate_bytes,
            expected_revision=None,
            comments_backup=True,
        )
        self._last_status = status_resolutions

    async def _load_provider_capabilities(
        self,
        provider_names: set[str],
        *,
        providers_config: dict[str, ProviderDetails],
        provider_models_service: ProviderModelsService,
        proxy_http_clients: Any,
        shared_http_client: httpx.AsyncClient,
    ) -> dict[str, dict[str, CapabilityMetadata]]:
        result: dict[str, dict[str, CapabilityMetadata]] = {}
        for provider_name in provider_names:
            provider_config = providers_config.get(provider_name)
            if provider_config is None:
                logger.warning(
                    "Capability autofill: provider '%s' is not configured; skipping.",
                    provider_name,
                )
                continue
            try:
                api_key = resolve_provider_config_api_key(provider_config)
                auth_headers = resolve_provider_config_auth_headers(provider_config, api_key)
                http_client = proxy_http_clients.get(provider_name, shared_http_client)
                entries = await provider_models_service.get_model_entries(
                    provider_name,
                    provider_config,
                    http_client,
                    auth_headers=auth_headers,
                )
            except Exception:
                logger.exception(
                    "Capability autofill: failed to fetch models for provider '%s'.",
                    provider_name,
                )
                continue
            result[provider_name] = {
                model_id: parse_capability_metadata(raw_entry)
                for model_id, raw_entry in entries.items()
            }
        return result


def _collect_worklist(fallback_rules: dict[str, dict[str, Any]]) -> list[_WorklistEntry]:
    """Walk every fallback candidate, modeled on model_availability._collect_provider_model_pairs."""
    worklist: list[_WorklistEntry] = []
    for gateway_model_name, rule in fallback_rules.items():
        fallback_models = rule.get("fallback_models")
        if isinstance(fallback_models, list):
            for index, candidate in enumerate(fallback_models):
                if isinstance(candidate, dict):
                    worklist.append(_WorklistEntry(gateway_model_name, index, candidate))
        context_overflow_fallback = rule.get("context_overflow_fallback")
        if isinstance(context_overflow_fallback, dict):
            worklist.append(
                _WorklistEntry(gateway_model_name, "context_overflow_fallback", context_overflow_fallback)
            )
    return worklist


def _providers_needing_resolution(worklist: list[_WorklistEntry]) -> set[str]:
    """Providers with at least one candidate that has an eligible field to (re)resolve."""
    providers: set[str] = set()
    for entry in worklist:
        candidate = entry.candidate
        owned = set(candidate.get("capabilities_autofilled") or [])
        for field_name in CAPABILITY_FIELDS:
            if field_name in owned or candidate.get(field_name) is None:
                provider_name = candidate.get("provider")
                if isinstance(provider_name, str) and provider_name:
                    providers.add(provider_name)
                break
    return providers


def _resolve_worklist(
    worklist: list[_WorklistEntry],
    *,
    provider_capabilities: dict[str, dict[str, CapabilityMetadata]],
    openrouter_index: dict[str, CapabilityMetadata],
) -> dict[str, list[dict[str, Any]]]:
    """Apply the field-eligibility/resolution/ownership algorithm to every candidate.

    Mutates each candidate dict in place (they are references into the
    caller's working copy of the fallback rules) and returns the per-run
    status payload for the fields that resolved to a fresh value this run.
    """
    status_resolutions: dict[str, list[dict[str, Any]]] = {}
    for entry in worklist:
        candidate = entry.candidate
        model_id = candidate.get("model")
        provider_name = candidate.get("provider")
        provider_metadata = provider_capabilities.get(provider_name, {}).get(model_id)

        original_owned = set(candidate.get("capabilities_autofilled") or [])
        new_owned: set[str] = set()
        resolved_fields: dict[str, str] = {}

        for field_name in CAPABILITY_FIELDS:
            allowed = field_name in original_owned or candidate.get(field_name) is None
            if not allowed:
                continue
            resolution = _resolve_field(
                field_name,
                model_id=model_id,
                provider_metadata=provider_metadata,
                openrouter_index=openrouter_index,
            )
            if resolution is not None:
                value, source = resolution
                candidate[field_name] = value
                new_owned.add(field_name)
                resolved_fields[field_name] = source
            elif field_name in original_owned:
                # Anti-regression: a previously autofilled field that fails to
                # resolve this run keeps its existing value and stays owned.
                new_owned.add(field_name)

        if new_owned:
            candidate["capabilities_autofilled"] = sorted(new_owned)
        else:
            candidate.pop("capabilities_autofilled", None)

        if resolved_fields:
            status_resolutions.setdefault(entry.gateway_model_name, []).append(
                {
                    "index": entry.index,
                    "provider": provider_name,
                    "model": model_id,
                    "fields": {
                        field_name: {"source": source}
                        for field_name, source in resolved_fields.items()
                    },
                }
            )
    return status_resolutions


def _resolve_field(
    field_name: str,
    *,
    model_id: Any,
    provider_metadata: CapabilityMetadata | None,
    openrouter_index: dict[str, CapabilityMetadata],
) -> tuple[Any, str] | None:
    if provider_metadata is not None:
        value = getattr(provider_metadata, field_name)
        if value is not None:
            return value, "provider"
    if not isinstance(model_id, str) or not model_id:
        return None
    openrouter_metadata = openrouter_index.get(_openrouter_metadata_key(model_id))
    if openrouter_metadata is not None:
        value = getattr(openrouter_metadata, field_name)
        if value is not None:
            return value, "openrouter"
    return None


def _build_model_fallback_configs(
    fallback_rules: dict[str, dict[str, Any]],
) -> list[ModelFallbackConfig]:
    """Rebuild ``ModelFallbackConfig`` objects from the ``_fallback_rules_base`` shape.

    Mirrors the dict construction in
    ``llm_gateway_core/api/v1/rules_editor.py::_build_structured_rules_response``
    field-by-field, but produces validated pydantic models instead of an API
    response dict.
    """
    rules: list[ModelFallbackConfig] = []
    for gateway_model_name, config in fallback_rules.items():
        rule_payload: dict[str, Any] = {
            "gateway_model_name": gateway_model_name,
            "fallback_models": config.get("fallback_models", []),
            "rotate_models": config.get("rotate_models", False),
            "dynamic_penalty": config.get("dynamic_penalty", False),
            "strip_think_tags": config.get("strip_think_tags", False),
            "compress_tool_results": config.get("compress_tool_results", False),
            "tool_call_rescue": config.get("tool_call_rescue", False),
        }
        max_total_attempts = config.get("max_total_attempts")
        if max_total_attempts is not None:
            rule_payload["max_total_attempts"] = max_total_attempts
        context_overflow_fallback = config.get("context_overflow_fallback")
        if context_overflow_fallback is not None:
            rule_payload["context_overflow_fallback"] = context_overflow_fallback
        rules.append(ModelFallbackConfig(**rule_payload))
    return rules


def _serialize_fallback_rules(rules: list[ModelFallbackConfig]) -> bytes:
    """Same one-liner as rules_editor._serialize_structured_rules (see
    llm_gateway_core/api/v1/rules_editor.py). Duplicated rather than imported:
    this is a runtime service and must not depend on the API layer.
    """
    payload = [rule.model_dump(exclude_none=True) for rule in rules]
    return (json.dumps(payload, ensure_ascii=False, indent=2) + "\n").encode("utf-8")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
