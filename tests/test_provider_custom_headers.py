"""Provider-level custom_headers must ride on every outbound provider call."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from llm_gateway_core.api.v1.chat import _attempt_model_fallback_rule
from llm_gateway_core.config.loader import (
    ProviderDetails,
    resolve_provider_config_auth_headers,
)
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async

import httpx


class ProviderCustomHeadersResolutionTests(unittest.TestCase):
    def test_auth_headers_include_provider_user_agent(self):
        provider = ProviderDetails(
            baseUrl="https://provider.example/v1",
            apikey="DIRECT-KEY",
            custom_headers={"User-Agent": "Cline/1.0"},
        )

        headers = resolve_provider_config_auth_headers(provider)

        self.assertEqual(headers["Authorization"], "Bearer DIRECT-KEY")
        self.assertEqual(headers["User-Agent"], "Cline/1.0")

    def test_missing_custom_headers_keeps_auth_only(self):
        provider = ProviderDetails(
            baseUrl="https://provider.example/v1",
            apikey="DIRECT-KEY",
        )

        headers = resolve_provider_config_auth_headers(provider)

        self.assertEqual(headers, {"Authorization": "Bearer DIRECT-KEY"})


class ProviderCustomHeadersChatTests(unittest.TestCase):
    def _success_payload(self):
        return {
            "id": "ok",
            "choices": [
                {
                    "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"},
                }
            ],
        }

    def _run_attempt(self, provider, rule, requested_model="gpt-5.6-sol"):
        make_request = AsyncMock(return_value=(self._success_payload(), None))
        fake_request = SimpleNamespace(state=SimpleNamespace(), headers={})
        with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request):
            response_data, error_detail, _attempt_number = run_async(
                _attempt_model_fallback_rule(
                    fake_request,
                    Mock(),
                    {"agentrouter": provider},
                    requested_model,
                    {
                        "model": requested_model,
                        "messages": [{"role": "user", "content": "hi"}],
                        "stream": True,
                    },
                    rule,
                    True,
                    proxy_http_clients={},
                    upstream_routing_state=UpstreamRoutingState(),
                )
            )
        self.assertIsNone(error_detail)
        self.assertEqual(response_data, self._success_payload())
        return make_request.await_args.args[2]

    def test_playground_direct_rule_sends_provider_user_agent(self):
        provider = ProviderDetails(
            baseUrl="https://agentrouter.org/v1",
            apikey="DIRECT-KEY",
            custom_headers={"User-Agent": "Cline/1.0"},
        )
        # Playground builds only provider+model, with no rule custom_headers.
        outbound = self._run_attempt(
            provider,
            {"provider": "agentrouter", "model": "gpt-5.6-sol"},
        )

        self.assertEqual(outbound["User-Agent"], "Cline/1.0")
        self.assertEqual(outbound["Authorization"], "Bearer DIRECT-KEY")

    def test_rule_custom_headers_override_provider_user_agent(self):
        provider = ProviderDetails(
            baseUrl="https://agentrouter.org/v1",
            apikey="DIRECT-KEY",
            custom_headers={"User-Agent": "Cline/1.0"},
        )
        outbound = self._run_attempt(
            provider,
            {
                "provider": "agentrouter",
                "model": "gpt-5.6-sol",
                "custom_headers": {"User-Agent": "OtherClient/2.0"},
            },
        )

        self.assertEqual(outbound["User-Agent"], "OtherClient/2.0")


class ProviderCustomHeadersModelsTests(unittest.TestCase):
    def test_models_fetch_sends_provider_user_agent(self):
        http_client = Mock()
        http_client.get = AsyncMock(
            return_value=httpx.Response(
                200,
                json={"data": [{"id": "gpt-5.6-sol"}]},
                request=httpx.Request("GET", "https://agentrouter.org/v1/models"),
            )
        )
        service = ProviderModelsService(ttl_seconds=900, time_func=lambda: 0.0)
        provider = ProviderDetails(
            baseUrl="https://agentrouter.org/v1",
            apikey="DIRECT-KEY",
            custom_headers={"User-Agent": "Cline/1.0"},
        )

        models = run_async(service.get_models("agentrouter", provider, http_client))

        self.assertEqual(models, ["gpt-5.6-sol"])
        self.assertEqual(
            http_client.get.await_args.kwargs["headers"]["User-Agent"],
            "Cline/1.0",
        )


if __name__ == "__main__":
    unittest.main()
