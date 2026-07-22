import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from ..config.loader import (
    ANTHROPIC_API_VERSION,
    ProviderDetails,
    resolve_provider_config_api_key,
    resolve_provider_config_auth_headers,
)

logger = logging.getLogger(__name__)

PROVIDER_MODELS_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class ProviderModelsCacheEntry:
    models: list[str]
    entries: dict[str, dict]
    fetched_at: float


def provider_explicit_models(provider_config: ProviderDetails) -> list[str] | None:
    """Return the explicit, deduplicated model-id list declared on a provider.

    A provider may declare ``available_models`` as a list of model ids to pin
    the set of usable models instead of querying the upstream ``/models``
    endpoint. When ``available_models`` is absent, not a list, or contains no
    valid string ids, this returns ``None`` and callers fall back to querying
    the provider as before. The separate ``models`` field stays reserved for
    per-model metadata (``upstream_limits`` etc.) and is not consulted here.
    """
    raw = getattr(provider_config, "available_models", None)
    if not isinstance(raw, list):
        return None
    model_ids: list[str] = []
    seen: set[str] = set()
    for entry in raw:
        if not isinstance(entry, str):
            continue
        model_id = entry.strip()
        if not model_id or model_id in seen:
            continue
        seen.add(model_id)
        model_ids.append(model_id)
    return model_ids or None


class ProviderModelsService:
    def __init__(
        self,
        ttl_seconds: int = PROVIDER_MODELS_CACHE_TTL_SECONDS,
        time_func=time.monotonic,
    ) -> None:
        self.ttl_seconds = ttl_seconds
        self._time_func = time_func
        self._cache: dict[str, ProviderModelsCacheEntry] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    def clear(self) -> None:
        self._cache.clear()

    def snapshot_cache_entry(
        self, provider_name: str
    ) -> ProviderModelsCacheEntry | None:
        """Return a still-fresh cached entry for ``provider_name``, or ``None``.

        Used to graft a warm entry into another service (e.g. a candidate
        snapshot's fresh cache during a config update) without breaking
        encapsulation. Stale entries are ignored so the receiver never adopts
        an expired snapshot.
        """
        entry = self._cache.get(provider_name)
        if entry is None:
            return None
        now = self._time_func()
        if now - entry.fetched_at >= self.ttl_seconds:
            return None
        return entry

    def install_cache_entry(
        self,
        provider_name: str,
        entry: ProviderModelsCacheEntry,
    ) -> None:
        """Install ``entry`` under ``provider_name`` without contacting upstream.

        The caller is responsible for ensuring the entry is compatible with
        this service's TTL and describes the same provider config.
        """
        self._cache[provider_name] = entry

    async def get_models(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
        auth_headers: dict[str, str] | None = None,
    ) -> list[str]:
        explicit_models = provider_explicit_models(provider_config)
        if explicit_models is not None:
            logger.debug(
                "Using %d explicitly configured models for provider '%s'.",
                len(explicit_models),
                provider_name,
            )
            return explicit_models

        entry = await self._get_cached_entry(provider_name, provider_config, http_client, auth_headers)
        return entry.models

    async def get_model_entries(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
        auth_headers: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        """Raw /models catalog entries keyed by exact model id (own-provider match only).

        Returns ``{}`` for a provider pinned to explicit ``available_models``:
        there is no upstream catalog to consult, which is not an error — the
        F-auto capability resolver simply falls through to its next source.
        """
        explicit_models = provider_explicit_models(provider_config)
        if explicit_models is not None:
            return {}

        entry = await self._get_cached_entry(provider_name, provider_config, http_client, auth_headers)
        return dict(entry.entries)

    async def _get_cached_entry(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
        auth_headers: dict[str, str] | None,
    ) -> ProviderModelsCacheEntry:
        now = self._time_func()
        cached_entry = self._cache.get(provider_name)
        if cached_entry and now - cached_entry.fetched_at < self.ttl_seconds:
            return cached_entry

        provider_lock = self._locks.setdefault(provider_name, asyncio.Lock())
        async with provider_lock:
            now = self._time_func()
            cached_entry = self._cache.get(provider_name)
            if cached_entry and now - cached_entry.fetched_at < self.ttl_seconds:
                return cached_entry

            entries = await self._fetch_model_entries(provider_name, provider_config, http_client, auth_headers)
            fresh_entry = ProviderModelsCacheEntry(models=list(entries.keys()), entries=entries, fetched_at=now)
            if entries:
                self._cache[provider_name] = fresh_entry
            else:
                self._cache.pop(provider_name, None)
                logger.warning(
                    "Provider '%s' returned an empty /models list; not caching empty result.",
                    provider_name,
                )
            return fresh_entry

    async def _fetch_model_entries(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
        auth_headers: dict[str, str] | None = None,
    ) -> dict[str, dict]:
        provider_base_url = provider_config.baseUrl.rstrip("/")
        provider_api_key = None if auth_headers is not None else resolve_provider_config_api_key(provider_config)
        if getattr(provider_config, "type", "openai") == "anthropic":
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_API_VERSION,
                **(auth_headers or resolve_provider_config_auth_headers(provider_config, provider_api_key)),
            }
            target_url = f"{provider_base_url}/v1/models"
        else:
            headers = {
                "Content-Type": "application/json",
                **(auth_headers or resolve_provider_config_auth_headers(provider_config, provider_api_key)),
            }
            target_url = f"{provider_base_url}/models"

        try:
            response = await http_client.get(target_url, headers=headers)
        except httpx.RequestError as exc:
            raise ValueError(
                f"Could not load models for provider '{provider_name}': {exc}"
            ) from exc

        if response.status_code >= 400:
            response_text = response.text.strip()
            short_response_text = response_text[:300] + ("..." if len(response_text) > 300 else "")
            raise ValueError(
                f"Could not load models for provider '{provider_name}': "
                f"provider responded with HTTP {response.status_code}: {short_response_text}"
            )

        try:
            payload = response.json()
        except ValueError as exc:
            raise ValueError(
                f"Could not load models for provider '{provider_name}': invalid JSON in /models response."
            ) from exc

        raw_models = payload.get("data")
        if not isinstance(raw_models, list):
            raw_models = payload.get("models")
        if not isinstance(raw_models, list):
            raise ValueError(
                f"Could not load models for provider '{provider_name}': "
                "provider /models response must contain a 'data' or 'models' list."
            )

        entries_by_id: dict[str, dict] = {}
        for model_entry in raw_models:
            if not isinstance(model_entry, dict):
                continue
            model_id = model_entry.get("id")
            if not isinstance(model_id, str) or not model_id or model_id in entries_by_id:
                continue
            entries_by_id[model_id] = model_entry

        logger.info(
            "Loaded %d models for provider '%s' from %s",
            len(entries_by_id),
            provider_name,
            target_url,
        )
        return entries_by_id
