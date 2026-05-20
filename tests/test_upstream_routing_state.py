import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main

from llm_gateway_core.services.upstream_routing_state import (
    UpstreamQuotaLimits,
    UpstreamRoutingState,
    fingerprint_api_key,
)


class _Clock:
    def __init__(self) -> None:
        self.value = 1_000.0

    def time(self) -> float:
        return self.value

    def monotonic(self) -> float:
        return self.value


class _UpstreamError:
    status_code = 429

    def __str__(self) -> str:
        return "rate limit exceeded"


class _OrderingProbeState(UpstreamRoutingState):
    def __init__(self) -> None:
        super().__init__()
        self.order_call_count = 0

    def order_rules_by_penalty(self, rules, providers_config):
        self.order_call_count += 1
        return list(reversed(rules))


def _provider_config(api_key: str) -> SimpleNamespace:
    return SimpleNamespace(apikey=api_key)


def _successful_chat_response() -> dict:
    return {
        "id": "chatcmpl-test",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }


def _build_chat_config_loader(*, dynamic_penalty: bool) -> Mock:
    loader = Mock()
    loader.providers_config = {
        "first-provider": SimpleNamespace(
            baseUrl="https://first.example",
            apikey="FIRST_PROVIDER_SECRET",
        ),
        "second-provider": SimpleNamespace(
            baseUrl="https://second.example",
            apikey="SECOND_PROVIDER_SECRET",
        ),
    }
    loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "first-provider",
                    "model": "first-model",
                    "use_provider_order_as_fallback": False,
                },
                {
                    "provider": "second-provider",
                    "model": "second-model",
                    "use_provider_order_as_fallback": False,
                },
            ],
            "rotate_models": False,
            "dynamic_penalty": dynamic_penalty,
        }
    }
    loader.operation_rules = {}
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    loader.load_operation_rules.return_value = loader.operation_rules
    loader.validate_fallback_operation_consistency.return_value = None
    return loader


def _first_chat_attempt_url(*, dynamic_penalty: bool) -> tuple[str, int]:
    with ExitStack() as stack:
        config_loader_cls = stack.enter_context(patch("main.ConfigLoader"))
        async_client_ctor = stack.enter_context(patch("main.httpx.AsyncClient"))
        stack.enter_context(patch("main.TokensUsageDB"))
        openrouter_service_cls = stack.enter_context(patch("main.OpenRouterFreeModelsService"))
        fallback_eval_service_cls = stack.enter_context(patch("main.FallbackModelEvalService"))
        make_llm_request = stack.enter_context(patch("llm_gateway_core.api.v1.chat.make_llm_request"))

        config_loader_cls.return_value = _build_chat_config_loader(dynamic_penalty=dynamic_penalty)

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client

        openrouter_service = Mock()
        openrouter_service.start = AsyncMock()
        openrouter_service.stop = AsyncMock()
        openrouter_service_cls.return_value = openrouter_service

        fallback_eval_service = Mock()
        fallback_eval_service.stop = AsyncMock()
        fallback_eval_service_cls.return_value = fallback_eval_service

        make_llm_request.return_value = (_successful_chat_response(), None)

        stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
        stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

        with TestClient(main.app) as client:
            probe_state = _OrderingProbeState()
            main.app.state.upstream_routing_state = probe_state
            response = client.post(
                "/v1/chat/completions",
                json={
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

    if response.status_code != 200:
        raise AssertionError(response.text)
    return make_llm_request.await_args_list[0].args[1], probe_state.order_call_count


class UpstreamRoutingStateTests(unittest.TestCase):
    def test_fingerprint_api_key_is_stable_distinct_and_does_not_include_secret(self):
        secret = "sk-test-secret-value-1234567890"

        fingerprint = fingerprint_api_key(secret)

        self.assertEqual(fingerprint, fingerprint_api_key(secret))
        self.assertNotEqual(fingerprint, fingerprint_api_key("sk-test-secret-value-other"))
        self.assertNotIn(secret, fingerprint)
        self.assertNotIn("secret-value", fingerprint)
        self.assertEqual(fingerprint_api_key(None), "keyless")

    def test_cooldown_and_quota_are_tracked_per_key_without_leaking_raw_keys(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider = "openrouter"
        model = "provider-model"
        api_keys = ["sk-primary-secret", "sk-secondary-secret"]
        limits = UpstreamQuotaLimits(rpm=1, tpm=100)

        first = state.select_key(provider, model, api_keys, limits=limits)
        self.assertTrue(first.available)
        self.assertIn(first.api_key, api_keys)

        state.record_attempt_start(provider, model, first.fingerprint)
        state.record_failure(
            provider,
            model,
            first.fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
            retry_after=30,
        )

        second = state.select_key(provider, model, api_keys, limits=limits)
        self.assertTrue(second.available)
        self.assertEqual(second.api_key, next(api_key for api_key in api_keys if api_key != first.api_key))

        state.record_attempt_start(provider, model, second.fingerprint)
        state.record_tokens(provider, model, second.fingerprint, 100)

        blocked = state.select_key(provider, model, api_keys, limits=limits)
        self.assertFalse(blocked.available)
        self.assertIsNone(blocked.api_key)
        self.assertIn("cooldown", blocked.blocked_reason)
        self.assertTrue(
            "rpm quota exhausted" in blocked.blocked_reason
            or "tpm quota exhausted" in blocked.blocked_reason
        )

        rows = state.get_status_rows()
        rows_text = repr(rows)
        self.assertNotIn(api_keys[0], rows_text)
        self.assertNotIn(api_keys[1], rows_text)

        first_row = next(row for row in rows if row["upstream_key_fingerprint"] == first.fingerprint)
        second_row = next(row for row in rows if row["upstream_key_fingerprint"] == second.fingerprint)
        self.assertGreater(first_row["cooldown_remaining_seconds"], 0)
        self.assertEqual(second_row["requests_last_minute"], 1)
        self.assertEqual(second_row["tokens_last_minute"], 100)

    def test_order_rules_by_penalty_reorders_rules_by_recorded_penalty(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        penalized_key = "sk-penalized-secret"
        healthy_key = "sk-healthy-secret"
        providers_config = {
            "penalized": _provider_config(penalized_key),
            "healthy": _provider_config(healthy_key),
        }
        rules = [
            {"provider": "penalized", "model": "expensive-model"},
            {"provider": "healthy", "model": "cheap-model"},
        ]
        state.record_failure(
            "penalized",
            "expensive-model",
            fingerprint_api_key(penalized_key),
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
        )

        ordered = state.order_rules_by_penalty(rules, providers_config)

        self.assertEqual(
            [(rule["provider"], rule["model"]) for rule in ordered],
            [
                ("healthy", "cheap-model"),
                ("penalized", "expensive-model"),
            ],
        )
        self.assertEqual(
            [(rule["provider"], rule["model"]) for rule in rules],
            [
                ("penalized", "expensive-model"),
                ("healthy", "cheap-model"),
            ],
        )

    def test_dynamic_penalty_ordering_is_opt_in_for_gateway_rule(self):
        static_url, static_order_calls = _first_chat_attempt_url(dynamic_penalty=False)
        dynamic_url, dynamic_order_calls = _first_chat_attempt_url(dynamic_penalty=True)

        self.assertEqual(static_url, "https://first.example/chat/completions")
        self.assertEqual(static_order_calls, 0)
        self.assertEqual(dynamic_url, "https://second.example/chat/completions")
        self.assertEqual(dynamic_order_calls, 1)


if __name__ == "__main__":
    unittest.main()
