"""Tests for provider proxy configuration and client creation."""

import os
import unittest
from unittest.mock import patch

from llm_gateway_core.config.loader import ProviderDetails, resolve_provider_proxy
from main import create_proxy_http_clients
from tests._async_compat import run_async


class ResolveProviderProxyTests(unittest.TestCase):
    def test_returns_none_for_none(self):
        self.assertIsNone(resolve_provider_proxy(None))

    def test_returns_none_for_empty_string(self):
        self.assertIsNone(resolve_provider_proxy(""))

    def test_returns_literal_url_when_no_env_var(self):
        url = "socks5://user:pass@host:1080"
        with patch.dict(os.environ, {}, clear=False):
            self.assertEqual(resolve_provider_proxy(url), url)

    def test_resolves_explicit_env_reference(self):
        with patch.dict(os.environ, {"MY_PROXY": "socks5://resolved@host:1080"}):
            self.assertEqual(resolve_provider_proxy("${MY_PROXY}"), "socks5://resolved@host:1080")

    def test_literal_url_is_not_resolved_from_env(self):
        with patch.dict(os.environ, {"http://proxy.example": "should-not-match"}, clear=False):
            result = resolve_provider_proxy("http://proxy.example")
            self.assertEqual(result, "http://proxy.example")


class ProviderDetailsProxyFieldTests(unittest.TestCase):
    def test_proxy_is_optional_and_defaults_to_none(self):
        details = ProviderDetails(baseUrl="https://api.example.com", apikey="key")
        self.assertIsNone(details.proxy)

    def test_proxy_can_be_set(self):
        details = ProviderDetails(
            baseUrl="https://api.example.com",
            apikey="key",
            proxy="PROXY_ENV_VAR",
        )
        self.assertEqual(details.proxy, "PROXY_ENV_VAR")


class CreateProxyHttpClientsTests(unittest.TestCase):
    def test_no_clients_when_no_proxy(self):
        providers = {
            "openai": ProviderDetails(baseUrl="https://api.openai.com", apikey="key"),
        }
        clients = create_proxy_http_clients(providers)
        self.assertEqual(clients, {})

    def test_creates_client_for_provider_with_proxy(self):
        providers = {
            "openai": ProviderDetails(baseUrl="https://api.openai.com", apikey="key"),
            "cloudru": ProviderDetails(
                baseUrl="https://api.cloudru.com",
                apikey="key",
                proxy="socks5://user:pass@host:1080",
            ),
        }
        clients = create_proxy_http_clients(providers)
        self.assertIn("cloudru", clients)
        self.assertNotIn("openai", clients)
        # Clean up
        for c in clients.values():
            run_async(c.aclose())

    def test_resolves_proxy_from_env_var(self):
        providers = {
            "cloudru": ProviderDetails(
                baseUrl="https://api.cloudru.com",
                apikey="key",
                proxy="${TEST_PROXY_URL}",
            ),
        }
        with patch.dict(os.environ, {"TEST_PROXY_URL": "socks5://resolved@host:1080"}):
            clients = create_proxy_http_clients(providers)
        self.assertIn("cloudru", clients)
        # Clean up
        for c in clients.values():
            run_async(c.aclose())


if __name__ == "__main__":
    unittest.main()
