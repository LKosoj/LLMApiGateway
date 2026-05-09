import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from tests._async_compat import run_async
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.provider_models import ProviderModelsService


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


if __name__ == "__main__":
    unittest.main()
