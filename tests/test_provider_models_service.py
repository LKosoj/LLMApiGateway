import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from tests._async_compat import run_async
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.provider_models import (
    ProviderModelsService,
    provider_explicit_models,
)


class ProviderModelsServiceTests(unittest.TestCase):
    def test_returns_cached_models_within_ttl(self):
        now = [100.0]
        http_client = Mock()
        http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "model-a"}, {"id": "model-b"}]},
                request=httpx.Request("GET", "https://provider.example/models"),
            )
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: now[0])
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="DIRECT-KEY")

        first_models = run_async(service.get_models("provider-a", provider_config, http_client))
        second_models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(first_models, ["model-a", "model-b"])
        self.assertEqual(second_models, ["model-a", "model-b"])
        self.assertEqual(http_client.get.await_count, 1)

    def test_refreshes_cache_after_ttl_expires(self):
        now = [100.0]
        http_client = Mock()
        http_client.get = AsyncMock(
            side_effect=[
                httpx.Response(
                    200,
                    json={"data": [{"id": "model-a"}]},
                    request=httpx.Request("GET", "https://provider.example/models"),
                ),
                httpx.Response(
                    200,
                    json={"data": [{"id": "model-b"}]},
                    request=httpx.Request("GET", "https://provider.example/models"),
                ),
            ]
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: now[0])
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="DIRECT-KEY")

        first_models = run_async(service.get_models("provider-a", provider_config, http_client))
        now[0] += 901.0
        second_models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(first_models, ["model-a"])
        self.assertEqual(second_models, ["model-b"])
        self.assertEqual(http_client.get.await_count, 2)

    def test_available_models_skip_api_call(self):
        http_client = Mock()
        http_client.get = AsyncMock()
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 0.0)
        provider_config = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            available_models=["model-a", "model-b"],
        )

        models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(http_client.get.await_count, 0)

    def test_available_models_combine_with_models_metadata(self):
        http_client = Mock()
        http_client.get = AsyncMock()
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 0.0)
        provider_config = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            available_models=["model-a", "model-b"],
            models={"model-a": {"upstream_limits": {"rpm": 10}}},
        )

        models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(models, ["model-a", "model-b"])
        self.assertEqual(http_client.get.await_count, 0)

    def test_models_metadata_without_available_models_queries_api(self):
        http_client = Mock()
        http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "model-a"}]},
                request=httpx.Request("GET", "https://provider.example/models"),
            )
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 0.0)
        provider_config = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="DIRECT-KEY",
            models={"model-a": {"upstream_limits": {"rpm": 10}}},
        )

        models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(models, ["model-a"])
        self.assertEqual(http_client.get.await_count, 1)


class ProviderExplicitModelsTests(unittest.TestCase):
    def _config(self, available_models):
        return ProviderDetails(
            baseUrl="https://provider.example",
            apikey="K",
            available_models=available_models,
        )

    def test_list_is_deduped_and_stripped(self):
        config = self._config(["  model-a ", "model-b", "model-a", "  ", ""])
        self.assertEqual(provider_explicit_models(config), ["model-a", "model-b"])

    def test_models_dict_alone_returns_none(self):
        config = ProviderDetails(
            baseUrl="https://provider.example",
            apikey="K",
            models={"model-a": {"upstream_limits": {"rpm": 10}}},
        )
        self.assertIsNone(provider_explicit_models(config))

    def test_missing_returns_none(self):
        self.assertIsNone(provider_explicit_models(self._config(None)))

    def test_empty_or_whitespace_only_list_returns_none(self):
        self.assertIsNone(provider_explicit_models(self._config([])))
        self.assertIsNone(provider_explicit_models(self._config(["   ", ""])))


if __name__ == "__main__":
    unittest.main()
