import asyncio
import logging
import time
from dataclasses import dataclass

import httpx

from ..config.loader import ANTHROPIC_API_VERSION, ProviderDetails, resolve_provider_api_key

logger = logging.getLogger(__name__)

PROVIDER_MODELS_CACHE_TTL_SECONDS = 15 * 60


@dataclass
class ProviderModelsCacheEntry:
    models: list[str]
    fetched_at: float


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

    async def get_models(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> list[str]:
        now = self._time_func()
        cached_entry = self._cache.get(provider_name)
        if cached_entry and now - cached_entry.fetched_at < self.ttl_seconds:
            return cached_entry.models

        provider_lock = self._locks.setdefault(provider_name, asyncio.Lock())
        async with provider_lock:
            now = self._time_func()
            cached_entry = self._cache.get(provider_name)
            if cached_entry and now - cached_entry.fetched_at < self.ttl_seconds:
                return cached_entry.models

            models = await self._fetch_models(provider_name, provider_config, http_client)
            if models:
                self._cache[provider_name] = ProviderModelsCacheEntry(models=models, fetched_at=now)
            else:
                self._cache.pop(provider_name, None)
                logger.warning(
                    "Provider '%s' returned an empty /models list; not caching empty result.",
                    provider_name,
                )
            return models

    async def _fetch_models(
        self,
        provider_name: str,
        provider_config: ProviderDetails,
        http_client: httpx.AsyncClient,
    ) -> list[str]:
        provider_base_url = provider_config.baseUrl.rstrip("/")
        provider_api_key = resolve_provider_api_key(provider_config.apikey)
        if getattr(provider_config, "type", "openai") == "anthropic":
            headers = {
                "Content-Type": "application/json",
                "anthropic-version": ANTHROPIC_API_VERSION,
                **({"x-api-key": provider_api_key} if provider_api_key else {}),
            }
            target_url = f"{provider_base_url}/v1/models"
        else:
            headers = {
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {provider_api_key}"} if provider_api_key else {}),
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

        model_ids: list[str] = []
        seen_model_ids: set[str] = set()
        for model_entry in raw_models:
            if not isinstance(model_entry, dict):
                continue
            model_id = model_entry.get("id")
            if not isinstance(model_id, str) or not model_id or model_id in seen_model_ids:
                continue
            seen_model_ids.add(model_id)
            model_ids.append(model_id)

        logger.info(
            "Loaded %d models for provider '%s' from %s",
            len(model_ids),
            provider_name,
            target_url,
        )
        return model_ids
