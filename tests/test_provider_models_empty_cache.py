import unittest
from unittest.mock import AsyncMock, Mock

import httpx

from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.provider_models import ProviderModelsService
from tests._async_compat import run_async


def _models_response(payload: dict) -> httpx.Response:
    return httpx.Response(
        200,
        json=payload,
        request=httpx.Request("GET", "https://provider.example/models"),
    )


class ProviderModelsEmptyCacheTests(unittest.TestCase):
    def test_empty_data_response_is_not_cached_for_full_ttl(self):
        http_client = Mock()
        http_client.get = AsyncMock(
            side_effect=[
                _models_response({"data": []}),
                _models_response({"data": [{"id": "model-a"}]}),
            ]
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 100.0)
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="DIRECT-KEY")

        with self.assertLogs("llm_gateway_core.services.provider_models", level="WARNING") as logs:
            first_models = run_async(service.get_models("provider-a", provider_config, http_client))
        self.assertNotIn("provider-a", service._cache)
        second_models = run_async(service.get_models("provider-a", provider_config, http_client))

        self.assertEqual(first_models, [])
        self.assertEqual(second_models, ["model-a"])
        self.assertEqual(http_client.get.await_count, 2)
        self.assertIn("not caching empty result", "\n".join(logs.output))

    def test_empty_models_response_is_not_cached_for_full_ttl(self):
        http_client = Mock()
        http_client.get = AsyncMock(
            side_effect=[
                _models_response({"models": []}),
                _models_response({"models": [{"id": "model-b"}]}),
            ]
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 100.0)
        provider_config = ProviderDetails(baseUrl="https://provider.example", apikey="DIRECT-KEY")

        first_models = run_async(service.get_models("provider-b", provider_config, http_client))
        self.assertNotIn("provider-b", service._cache)
        second_models = run_async(service.get_models("provider-b", provider_config, http_client))

        self.assertEqual(first_models, [])
        self.assertEqual(second_models, ["model-b"])
        self.assertEqual(http_client.get.await_count, 2)


if __name__ == "__main__":
    unittest.main()
