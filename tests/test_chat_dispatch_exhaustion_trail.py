import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1.chat_dispatch import (
    _build_attempt_trail,
    _min_retry_after_seconds,
)
from llm_gateway_core.services.request_handler import RequestErrorDetail
from llm_gateway_core.services.upstream_routing_state import fingerprint_api_key
from tests.chat_accounting_test_support import install_main_chat_accounting_double


class _StubUpstreamRoutingState:
    """Duck-typed stand-in exposing only ``cooldown_remaining_seconds``."""

    def __init__(self, mapping: dict[tuple[str, str, str], float | None]) -> None:
        self._mapping = mapping
        self.calls: list[tuple[str, str, str]] = []

    def cooldown_remaining_seconds(self, provider: str, model: str, key_fingerprint: str) -> float | None:
        self.calls.append((provider, model, key_fingerprint))
        return self._mapping.get((provider, model, key_fingerprint))


class AttemptTrailUnitTests(unittest.TestCase):
    def test_build_attempt_trail_assigns_ordinal_keys_and_reuses_them_for_repeated_fingerprint(self):
        attempts = [
            {
                "provider": "p1",
                "model": "m1",
                "upstream_key_fingerprint": "fp-a",
                "error_class": "http_429",
                "http_status": 429,
            },
            {
                "provider": "p2",
                "model": "m2",
                "upstream_key_fingerprint": "fp-b",
                "error_class": "http_500",
                "http_status": 500,
            },
            {
                "provider": "p1",
                "model": "m1",
                "upstream_key_fingerprint": "fp-a",
                "error_class": "http_429",
                "http_status": 429,
            },
        ]

        trail = _build_attempt_trail(attempts)

        self.assertEqual([entry["key"] for entry in trail], ["key1", "key2", "key1"])
        for entry in trail:
            self.assertNotIn("upstream_key_fingerprint", entry)
            self.assertNotIn("fp-a", entry.values())
            self.assertNotIn("fp-b", entry.values())

    def test_build_attempt_trail_carries_error_class_and_http_status(self):
        attempts = [
            {
                "provider": "openrouter",
                "model": "gpt-test",
                "upstream_key_fingerprint": "fp-only",
                "error_class": "http_503",
                "http_status": 503,
            }
        ]

        trail = _build_attempt_trail(attempts)

        self.assertEqual(
            trail,
            [
                {
                    "provider": "openrouter",
                    "model": "gpt-test",
                    "key": "key1",
                    "error_class": "http_503",
                    "http_status": 503,
                }
            ],
        )

    def test_min_retry_after_seconds_takes_minimum_across_unique_candidates(self):
        attempts = [
            {"provider": "p1", "model": "m1", "upstream_key_fingerprint": "fp-a"},
            {"provider": "p2", "model": "m2", "upstream_key_fingerprint": "fp-b"},
            # Duplicate ref: must be deduplicated before querying.
            {"provider": "p1", "model": "m1", "upstream_key_fingerprint": "fp-a"},
        ]
        stub = _StubUpstreamRoutingState(
            {
                ("p1", "m1", "fp-a"): 5000.3,
                ("p2", "m2", "fp-b"): 200.9,
            }
        )

        result = _min_retry_after_seconds(attempts, stub)

        # Minimum across candidates, rounded up, and NOT clamped to the
        # retry-sleep ceiling (MAX_RETRY_AFTER_SECONDS=120s): the escalation
        # ladder can run cooldowns up to 24h and a truncated ETA would
        # mislead the client (architect correction to the original plan).
        self.assertEqual(result, 201)
        self.assertEqual(len(stub.calls), 2)

    def test_min_retry_after_seconds_returns_none_without_cooldown(self):
        attempts = [
            {"provider": "p1", "model": "m1", "upstream_key_fingerprint": "fp-a"},
        ]
        stub = _StubUpstreamRoutingState({})

        result = _min_retry_after_seconds(attempts, stub)

        self.assertIsNone(result)


def _config_loader_with_fallback_models(fallback_models: list[dict]) -> Mock:
    fake_config_loader = Mock()
    fake_config_loader.configured_paths = {}
    fake_config_loader.operation_rules = {}
    providers_config = {}
    for rule in fallback_models:
        providers_config[rule["provider"]] = SimpleNamespace(
            baseUrl=f"https://{rule['provider']}.example",
            apikey=rule.pop("_apikey"),
        )
    fake_config_loader.providers_config = providers_config
    fake_config_loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": fallback_models,
            "rotate_models": False,
        }
    }
    fake_config_loader.load_providers.return_value = fake_config_loader.providers_config
    fake_config_loader.load_fallback_rules.return_value = fake_config_loader.fallback_rules
    fake_config_loader.load_operation_rules.return_value = {}
    fake_config_loader.load_complete.return_value = fake_config_loader
    return fake_config_loader


class ExhaustedFallbackIntegrationTests(unittest.TestCase):
    def setUp(self):
        self._accounting_stack = ExitStack()
        self.addCleanup(self._accounting_stack.close)
        self.accounting_service = install_main_chat_accounting_double(
            self._accounting_stack,
        )
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        coordinator_patcher = patch(
            "main.ConfigUpdateCoordinator",
            return_value=config_update_coordinator,
        )
        coordinator_patcher.start()
        self.addCleanup(coordinator_patcher.stop)

    def _post_chat(self, config_loader_cls, async_client_ctor, config_loader):
        config_loader_cls.return_value = config_loader
        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        with patch.object(main.settings, "gateway_api_key", "test-gateway-key"):
            with TestClient(main.app) as client:
                return client.post(
                    "/v1/chat/completions",
                    json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_exhausted_fallback_sanitizes_leaked_bearer_token_in_detail(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader = _config_loader_with_fallback_models(
            [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                    "_apikey": "DIRECT-KEY-1",
                }
            ]
        )
        make_llm_request_mock.return_value = (
            None,
            "Authorization rejected: Bearer sk-leaked-token-1234567890abcdef",
        )

        response = self._post_chat(config_loader_cls, async_client_ctor, config_loader)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("sk-leaked-token-1234567890abcdef", response.text)
        body = response.json()
        self.assertIn("Bearer [redacted]", body["detail"])

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_exhausted_fallback_body_includes_attempt_trail_with_anonymized_keys(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader = _config_loader_with_fallback_models(
            [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                    "_apikey": "DIRECT-KEY-1",
                },
                {
                    "provider": "second-provider",
                    "model": "second-model",
                    "use_provider_order_as_fallback": False,
                    "_apikey": "DIRECT-KEY-2",
                },
            ]
        )
        make_llm_request_mock.side_effect = [
            (None, "first provider exploded"),
            (None, "second provider exploded"),
        ]

        response = self._post_chat(config_loader_cls, async_client_ctor, config_loader)

        self.assertEqual(response.status_code, 503)
        body = response.json()
        self.assertEqual(len(body["attempts"]), 2)
        self.assertEqual(
            [entry["provider"] for entry in body["attempts"]],
            ["first-provider", "second-provider"],
        )
        self.assertEqual(body["attempts"][0]["key"], "key1")
        self.assertEqual(body["attempts"][1]["key"], "key2")
        for entry in body["attempts"]:
            self.assertEqual(set(entry.keys()), {"provider", "model", "key", "error_class", "http_status"})

        raw_fingerprint = fingerprint_api_key("DIRECT-KEY-1")
        self.assertNotIn(raw_fingerprint, response.text)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_exhausted_fallback_sets_retry_after_header_and_body_field_from_cooldown(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader = _config_loader_with_fallback_models(
            [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                    "_apikey": "DIRECT-KEY-1",
                }
            ]
        )
        make_llm_request_mock.return_value = (
            None,
            RequestErrorDetail("Rate limit exceeded, please slow down", status_code=429),
        )

        response = self._post_chat(config_loader_cls, async_client_ctor, config_loader)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.headers["Retry-After"], "90")
        body = response.json()
        self.assertEqual(body["retry_after_seconds"], 90)
        self.assertEqual(body["attempts"][0]["error_class"], "http_429")
        self.assertEqual(body["attempts"][0]["http_status"], 429)

    @patch("llm_gateway_core.api.v1.chat.make_llm_request")
    @patch("main.TokensUsageDB")
    @patch("main.create_shared_http_client")
    @patch("main.ConfigLoader")
    def test_exhausted_fallback_omits_retry_after_seconds_without_cooldown(
        self,
        config_loader_cls,
        async_client_ctor,
        _tokens_usage_db,
        make_llm_request_mock,
    ):
        config_loader = _config_loader_with_fallback_models(
            [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                    "_apikey": "DIRECT-KEY-1",
                }
            ]
        )
        make_llm_request_mock.return_value = (None, "Bad request: invalid payload format")

        response = self._post_chat(config_loader_cls, async_client_ctor, config_loader)

        self.assertEqual(response.status_code, 503)
        self.assertNotIn("Retry-After", response.headers)
        body = response.json()
        self.assertNotIn("retry_after_seconds", body)


if __name__ == "__main__":
    unittest.main()
