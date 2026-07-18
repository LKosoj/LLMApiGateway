import unittest
from contextlib import ExitStack
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main

from llm_gateway_core.services.upstream_routing_state import (
    DAILY_QUOTA_MIN_COOLDOWN_SECONDS,
    DEFAULT_COOLDOWN_SECONDS,
    RATE_LIMIT_ESCALATION_LADDER_SECONDS,
    RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS,
    UpstreamKeyCandidate,
    UpstreamQuotaLimits,
    UpstreamRoutingState,
    fingerprint_api_key,
)
from tests.chat_accounting_test_support import install_main_chat_accounting_double


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


class _RateLimitErrorWithMessage(str):
    """A 429-classified upstream error carrying an arbitrary body message.

    Modeled as a ``str`` subclass (mirroring production's ``RequestErrorDetail``
    in request_handler.py) because error-signal extraction in error_classifier.py
    only walks str/dict/list payloads; a plain object with a custom ``__str__``
    is invisible to it.
    """

    status_code = 429

    def __new__(cls, message: str) -> "_RateLimitErrorWithMessage":
        return super().__new__(cls, message)


class _ServerError(str):
    """A non-429 temporary upstream error (legacy cooldown branch)."""

    status_code = 503

    def __new__(cls) -> "_ServerError":
        return super().__new__(cls, "internal server error, please retry")


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
    loader.configured_paths = {}
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
    loader.model_rules = {}
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    loader.load_model_rules.return_value = loader.model_rules
    loader.load_operation_rules.return_value = loader.operation_rules
    loader.validate_fallback_operation_consistency.return_value = None
    loader.load_complete.return_value = loader
    return loader


def _build_pool_chat_config_loader() -> Mock:
    loader = Mock()
    loader.configured_paths = {}
    provider = SimpleNamespace(
        baseUrl="https://pool.example",
        apikey=None,
        type="openai",
        routing=SimpleNamespace(
            strategy="priority",
            session_affinity=True,
            session_affinity_header="X-Workspace-Session",
            session_affinity_ttl_seconds=3600,
        ),
        upstream_key_pools={
            "main": SimpleNamespace(
                strategy=None,
                session_affinity=None,
                session_affinity_header=None,
                session_affinity_ttl_seconds=None,
                keys=[
                    SimpleNamespace(id="low", apikey="LOW_POOL_SECRET", priority=10, enabled=True),
                    SimpleNamespace(id="high", apikey="HIGH_POOL_SECRET", priority=100, enabled=True),
                ],
            )
        },
        models=None,
    )
    loader.providers_config = {"pool-provider": provider}
    loader.fallback_rules = {
        "gateway-model": {
            "fallback_models": [
                {
                    "provider": "pool-provider",
                    "model": "pool-model",
                    "upstream_key_pool": "main",
                    "use_provider_order_as_fallback": False,
                },
            ],
            "rotate_models": False,
            "dynamic_penalty": False,
        }
    }
    loader.operation_rules = {}
    loader.model_rules = {}
    loader.load_providers.return_value = loader.providers_config
    loader.load_fallback_rules.return_value = loader.fallback_rules
    loader.load_model_rules.return_value = loader.model_rules
    loader.load_operation_rules.return_value = loader.operation_rules
    loader.validate_fallback_operation_consistency.return_value = None
    loader.load_complete.return_value = loader
    return loader


def _build_affinity_chat_config_loader() -> Mock:
    loader = _build_pool_chat_config_loader()
    provider = loader.providers_config["pool-provider"]
    provider.routing = SimpleNamespace(
        strategy="round-robin",
        session_affinity=True,
        session_affinity_header="X-Workspace-Session",
        session_affinity_ttl_seconds=3600,
    )
    provider.upstream_key_pools["main"].strategy = "round-robin"
    provider.upstream_key_pools["main"].keys = [
        SimpleNamespace(id="a", apikey="POOL_SECRET_A", priority=0, enabled=True),
        SimpleNamespace(id="b", apikey="POOL_SECRET_B", priority=0, enabled=True),
    ]
    return loader


def _post_chat_with_loader(
    config_loader: Mock,
    *,
    request_headers: dict[str, str] | None = None,
    upstream_state: UpstreamRoutingState | None = None,
    request_json: dict | None = None,
) -> tuple[object, Mock]:
    with ExitStack() as stack:
        install_main_chat_accounting_double(stack)
        config_loader_cls = stack.enter_context(patch("main.ConfigLoader"))
        async_client_ctor = stack.enter_context(
            patch("main.create_shared_http_client")
        )
        stack.enter_context(patch("main.TokensUsageDB"))
        openrouter_service_cls = stack.enter_context(patch("main.OpenRouterFreeModelsService"))
        fallback_eval_service_cls = stack.enter_context(patch("main.FallbackModelEvalService"))
        make_llm_request = stack.enter_context(patch("llm_gateway_core.api.v1.chat.make_llm_request"))

        config_loader_cls.return_value = config_loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        stack.enter_context(
            patch(
                "main.ConfigUpdateCoordinator",
                return_value=config_update_coordinator,
            )
        )

        openrouter_service = Mock()
        openrouter_service.start_runtime = AsyncMock()
        openrouter_service.stop = AsyncMock()
        openrouter_service_cls.return_value = openrouter_service

        fallback_eval_service = Mock()
        fallback_eval_service.stop = AsyncMock()
        fallback_eval_service_cls.return_value = fallback_eval_service

        make_llm_request.return_value = (_successful_chat_response(), None)

        stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
        stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
        if upstream_state is not None:
            stack.enter_context(
                patch("main.UpstreamRoutingState", return_value=upstream_state)
            )

        with TestClient(main.app) as client:
            headers = {"Authorization": "Bearer test-gateway-key"}
            headers.update(request_headers or {})
            response = client.post(
                "/v1/chat/completions",
                json=request_json or {
                    "model": "gateway-model",
                    "messages": [{"role": "user", "content": "hello"}],
                },
                headers=headers,
            )

    if response.status_code != 200:
        raise AssertionError(response.text)
    return response, make_llm_request


def _first_chat_attempt_url(*, dynamic_penalty: bool) -> tuple[str, int]:
    probe_state = _OrderingProbeState()
    _response, make_llm_request = _post_chat_with_loader(
        _build_chat_config_loader(dynamic_penalty=dynamic_penalty),
        upstream_state=probe_state,
    )
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

    def test_retry_after_cooldown_uses_upstream_seconds_instead_of_default_floor(self):
        # NOTE: _UpstreamError always carries status_code=429, so record_failure now
        # routes it through the 429 escalation-ladder branch (Package B) instead of
        # the legacy default-cooldown branch. The legacy `retry_after` kwarg only
        # affects the *old* branch (non-429 temporary errors, see
        # test_non_rate_limit_temporary_failure_keeps_default_cooldown_and_retry_after_override);
        # for a 429 it is no longer applied at all, and a first hit uses the fixed
        # transient cooldown regardless of the upstream-reported retry_after value.
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-retry-after-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
            retry_after=1,
        )

        blocked = state.select_key("openrouter", "provider-model", [key])
        self.assertFalse(blocked.available)
        self.assertIn(f"cooldown {int(RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS)}s", blocked.blocked_reason)

        clock.value += RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS + 0.1
        selected = state.select_key("openrouter", "provider-model", [key])
        self.assertTrue(selected.available)

    def test_non_rate_limit_temporary_failure_keeps_default_cooldown_and_retry_after_override(self):
        # Regression guard: a non-429 temporary error (5xx/timeout-style) must keep
        # using the pre-existing default-cooldown / legacy retry_after-override
        # behavior untouched by the new 429 ladder and daily-quota branches.
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-server-error-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _ServerError(),
            temporary=True,
            apply_penalty=False,
        )
        self.assertAlmostEqual(
            state.get_status_rows()[0]["cooldown_remaining_seconds"],
            DEFAULT_COOLDOWN_SECONDS,
            delta=0.01,
        )

        clock.value += 1
        state.record_failure(
            "openrouter",
            "second-provider-model",
            fingerprint,
            _ServerError(),
            temporary=True,
            apply_penalty=False,
            retry_after=45,
        )
        row = next(
            row for row in state.get_status_rows() if row["model"] == "second-provider-model"
        )
        self.assertAlmostEqual(row["cooldown_remaining_seconds"], 45.0, delta=0.01)

    def test_rate_limit_first_hit_uses_transient_ninety_second_cooldown(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-first-hit-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        self.assertAlmostEqual(
            row["cooldown_remaining_seconds"], RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS, delta=0.01
        )
        self.assertEqual(row["cooldown_escalation_level"], 0)

    def test_second_rate_limit_hit_within_hour_becomes_exhaustion_and_starts_ladder(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-second-hit-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )
        clock.value += 1
        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        self.assertAlmostEqual(
            row["cooldown_remaining_seconds"], RATE_LIMIT_ESCALATION_LADDER_SECONDS[0], delta=0.01
        )
        self.assertEqual(row["cooldown_escalation_level"], 1)

    def test_repeated_exhaustion_events_escalate_through_ladder_within_24h(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-escalation-secret"
        fingerprint = fingerprint_api_key(key)

        expected_cooldowns = [
            RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS,
            RATE_LIMIT_ESCALATION_LADDER_SECONDS[0],
            RATE_LIMIT_ESCALATION_LADDER_SECONDS[1],
            RATE_LIMIT_ESCALATION_LADDER_SECONDS[2],
            RATE_LIMIT_ESCALATION_LADDER_SECONDS[3],
            RATE_LIMIT_ESCALATION_LADDER_SECONDS[3],
        ]

        for index, expected_cooldown in enumerate(expected_cooldowns):
            state.record_failure(
                "openrouter",
                "provider-model",
                fingerprint,
                _UpstreamError(),
                temporary=True,
                apply_penalty=False,
            )
            row = state.get_status_rows()[0]
            with self.subTest(hit=index):
                self.assertAlmostEqual(
                    row["cooldown_remaining_seconds"], expected_cooldown, delta=0.01
                )
            clock.value += 1

    def test_exhaustion_event_ages_out_of_24h_window_and_resets_ladder(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-aging-out-secret"
        fingerprint = fingerprint_api_key(key)

        for _ in range(3):
            state.record_failure(
                "openrouter",
                "provider-model",
                fingerprint,
                _UpstreamError(),
                temporary=True,
                apply_penalty=False,
            )
            clock.value += 1

        row = state.get_status_rows()[0]
        self.assertEqual(row["cooldown_escalation_level"], 2)

        clock.value += 86_400.0 + 100.0
        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        self.assertAlmostEqual(
            row["cooldown_remaining_seconds"], RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS, delta=0.01
        )
        self.assertEqual(row["cooldown_escalation_level"], 0)

    def test_rate_limit_retry_after_floor_extends_but_never_shortens_ladder_cooldown(self):
        state = UpstreamRoutingState()

        state.record_failure(
            "openrouter",
            "floor-low-provider-model",
            fingerprint_api_key("sk-floor-low-secret"),
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
            retry_after_floor_seconds=10.0,
        )
        low_row = next(
            row
            for row in state.get_status_rows()
            if row["model"] == "floor-low-provider-model"
        )
        self.assertAlmostEqual(
            low_row["cooldown_remaining_seconds"], RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS, delta=0.01
        )

        state.record_failure(
            "openrouter",
            "floor-high-provider-model",
            fingerprint_api_key("sk-floor-high-secret"),
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
            retry_after_floor_seconds=200.0,
        )
        high_row = next(
            row
            for row in state.get_status_rows()
            if row["model"] == "floor-high-provider-model"
        )
        self.assertAlmostEqual(high_row["cooldown_remaining_seconds"], 200.0, delta=0.01)

    def test_daily_quota_cooldown_targets_next_utc_midnight_with_sixty_second_floor(self):
        near_midnight_epoch = datetime(2024, 3, 1, 23, 59, 30, tzinfo=timezone.utc).timestamp()
        clock = _Clock()
        clock.value = near_midnight_epoch
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-daily-quota-near-midnight-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("Daily quota exhausted. Try again after reset."),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        self.assertAlmostEqual(
            row["cooldown_remaining_seconds"], DAILY_QUOTA_MIN_COOLDOWN_SECONDS, delta=0.5
        )

        mid_afternoon_epoch = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        afternoon_clock = _Clock()
        afternoon_clock.value = mid_afternoon_epoch
        afternoon_state = UpstreamRoutingState(
            time_func=afternoon_clock.time, monotonic_func=afternoon_clock.monotonic
        )
        afternoon_state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("Daily quota exhausted. Try again after reset."),
            temporary=True,
            apply_penalty=False,
        )

        afternoon_row = afternoon_state.get_status_rows()[0]
        self.assertAlmostEqual(
            afternoon_row["cooldown_remaining_seconds"], 14 * 3600.0, delta=0.5
        )

    def test_daily_quota_retry_after_overrides_midnight_calculation(self):
        mid_afternoon_epoch = datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc).timestamp()
        clock = _Clock()
        clock.value = mid_afternoon_epoch
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-daily-quota-override-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("Daily quota exhausted. Try again after reset."),
            temporary=True,
            apply_penalty=False,
            retry_after_floor_seconds=45.0,
        )

        row = state.get_status_rows()[0]
        self.assertAlmostEqual(row["cooldown_remaining_seconds"], 45.0, delta=0.01)

    def test_record_failure_returns_true_for_rate_limit_429(self):
        state = UpstreamRoutingState()
        key = "sk-record-failure-429-secret"
        fingerprint = fingerprint_api_key(key)

        result = state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )

        self.assertTrue(result)

    def test_record_failure_returns_true_for_daily_quota_exhaustion(self):
        state = UpstreamRoutingState()
        key = "sk-record-failure-daily-quota-secret"
        fingerprint = fingerprint_api_key(key)

        result = state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("Daily quota exhausted. Try again after reset."),
            temporary=True,
            apply_penalty=False,
        )

        self.assertTrue(result)

    def test_record_failure_returns_false_for_non_429_temporary_failure_but_still_sets_cooldown(self):
        # A plain 5xx/timeout-style failure still schedules a cooldown for future
        # requests (unchanged), but is not a rate-limit block: the caller must
        # not treat it as a trigger for an immediate key switch on the current
        # request even though a cooldown is now active for the next one.
        state = UpstreamRoutingState()
        key = "sk-record-failure-non-429-secret"
        fingerprint = fingerprint_api_key(key)

        result = state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _ServerError(),
            temporary=True,
            apply_penalty=False,
        )

        self.assertFalse(result)
        remaining = state.cooldown_remaining_seconds("openrouter", "provider-model", fingerprint)
        self.assertIsNotNone(remaining)
        self.assertGreater(remaining, 0)

    def test_record_failure_returns_false_when_not_temporary(self):
        state = UpstreamRoutingState()
        key = "sk-record-failure-permanent-secret"
        fingerprint = fingerprint_api_key(key)

        result = state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=False,
            apply_penalty=False,
        )

        self.assertFalse(result)

    def test_learn_limit_from_error_body_parses_axis_and_value(self):
        state = UpstreamRoutingState()
        key = "sk-learn-limit-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("requests per day limit: 250 exceeded"),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        observed_rpd = row["observed_limits"]["rpd"]
        self.assertEqual(observed_rpd["limit"], 250)
        self.assertEqual(observed_rpd["source"], "error_body")
        self.assertIsNone(observed_rpd["remaining"])
        self.assertIsNone(observed_rpd["reset_in_seconds"])

    def test_learn_limit_only_written_when_absent_or_more_conservative(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-conservative-limit-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("rpm limit: 100 reached"),
            temporary=True,
            apply_penalty=False,
        )
        self.assertEqual(state.get_status_rows()[0]["observed_limits"]["rpm"]["limit"], 100)

        clock.value += 1
        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("rpm limit: 150 reached"),
            temporary=True,
            apply_penalty=False,
        )
        self.assertEqual(
            state.get_status_rows()[0]["observed_limits"]["rpm"]["limit"],
            100,
            "a higher (less conservative) error-body limit must not overwrite the stored value",
        )

        clock.value += 1
        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _RateLimitErrorWithMessage("rpm limit: 50 reached"),
            temporary=True,
            apply_penalty=False,
        )
        self.assertEqual(
            state.get_status_rows()[0]["observed_limits"]["rpm"]["limit"],
            50,
            "a strictly lower (more conservative) error-body limit must overwrite the stored value",
        )

    def test_observed_limit_header_source_wins_over_error_body(self):
        state = UpstreamRoutingState()
        provider, model, fingerprint = "openrouter", "provider-model", "fp-header-wins"

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=100,
            remaining=40,
            reset_at_monotonic=None,
            source="header",
        )
        state.record_failure(
            provider,
            model,
            fingerprint,
            _RateLimitErrorWithMessage("rpm limit: 10 reached"),
            temporary=True,
            apply_penalty=False,
        )
        header_entry = state.get_status_rows()[0]["observed_limits"]["rpm"]
        self.assertEqual(header_entry["limit"], 100)
        self.assertEqual(header_entry["source"], "header")

        # A fresh header reading is always applied, even to raise the previous limit.
        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=200,
            remaining=190,
            reset_at_monotonic=None,
            source="header",
        )
        updated_entry = state.get_status_rows()[0]["observed_limits"]["rpm"]
        self.assertEqual(updated_entry["limit"], 200)
        self.assertEqual(updated_entry["remaining"], 190)

    def test_quota_block_reason_uses_min_of_configured_and_observed_limits(self):
        state = UpstreamRoutingState()
        provider, model = "openrouter", "provider-model"
        key = "sk-effective-limit-secret"
        fingerprint = fingerprint_api_key(key)
        configured = UpstreamQuotaLimits(rpm=1000)

        baseline = state.select_key(provider, model, [key], limits=configured)
        self.assertTrue(baseline.available)
        state.record_attempt_start(provider, model, fingerprint)

        still_available = state.select_key(provider, model, [key], limits=configured)
        self.assertTrue(still_available.available)

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=1,
            remaining=0,
            reset_at_monotonic=None,
            source="header",
        )

        blocked = state.select_key(provider, model, [key], limits=configured)
        self.assertFalse(blocked.available)
        self.assertIn("rpm quota exhausted", blocked.blocked_reason)

    def test_observed_zero_remaining_with_future_reset_blocks_key_until_reset(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider, model = "groq", "provider-model"
        key = "sk-zero-remaining-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=100,
            remaining=0,
            reset_at_monotonic=clock.value + 30.0,
            source="header",
        )

        blocked = state.select_key(provider, model, [key])
        self.assertFalse(blocked.available)
        self.assertIn("rpm", blocked.blocked_reason)
        self.assertIn("exhausted", blocked.blocked_reason)

        clock.value += 31.0
        available = state.select_key(provider, model, [key])
        self.assertTrue(available.available)

    def test_observed_zero_remaining_with_past_reset_does_not_block(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider, model = "groq", "provider-model"
        key = "sk-past-reset-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=100,
            remaining=0,
            reset_at_monotonic=clock.value - 5.0,
            source="header",
        )

        available = state.select_key(provider, model, [key])
        self.assertTrue(available.available)

    def test_observed_positive_remaining_does_not_block(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider, model = "groq", "provider-model"
        key = "sk-positive-remaining-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=100,
            remaining=5,
            reset_at_monotonic=clock.value + 30.0,
            source="header",
        )

        available = state.select_key(provider, model, [key])
        self.assertTrue(available.available)

    def test_get_status_rows_exposes_cooldown_escalation_level_and_observed_limits(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider, model = "openrouter", "provider-model"
        key = "sk-status-rows-secret"
        fingerprint = fingerprint_api_key(key)

        state.record_failure(
            provider,
            model,
            fingerprint,
            _RateLimitErrorWithMessage("rate limit exceeded"),
            temporary=True,
            apply_penalty=False,
        )
        clock.value += 1
        state.record_failure(
            provider,
            model,
            fingerprint,
            _RateLimitErrorWithMessage("tpd limit: 5,000 reached"),
            temporary=True,
            apply_penalty=False,
        )

        row = state.get_status_rows()[0]
        self.assertEqual(row["cooldown_escalation_level"], 1)
        self.assertEqual(row["observed_limits"]["tpd"]["limit"], 5000)
        self.assertEqual(row["observed_limits"]["tpd"]["source"], "error_body")
        self.assertIn("observed_at", row["observed_limits"]["tpd"])

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

    def test_order_rules_by_penalty_uses_rule_upstream_key_pool_only(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        main_key = "sk-main-pool-secret"
        alt_key = "sk-alt-pool-secret"
        providers_config = {
            "mixed": SimpleNamespace(
                apikey="sk-legacy-secret",
                upstream_key_pools={
                    "main": SimpleNamespace(keys=[SimpleNamespace(apikey=main_key, enabled=True)]),
                    "alt": SimpleNamespace(keys=[SimpleNamespace(apikey=alt_key, enabled=True)]),
                },
            )
        }
        rules = [
            {"provider": "mixed", "model": "shared-model", "upstream_key_pool": "main"},
            {"provider": "mixed", "model": "shared-model", "upstream_key_pool": "alt"},
        ]
        state.record_failure(
            "mixed",
            "shared-model",
            fingerprint_api_key(main_key),
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
        )

        ordered = state.order_rules_by_penalty(rules, providers_config)

        self.assertEqual([rule["upstream_key_pool"] for rule in ordered], ["alt", "main"])

    def test_order_rules_by_penalty_uses_legacy_key_for_rule_without_pool(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        legacy_key = "sk-legacy-secret"
        pool_key = "sk-pool-secret"
        providers_config = {
            "mixed": SimpleNamespace(
                apikey=legacy_key,
                upstream_key_pools={
                    "main": SimpleNamespace(keys=[SimpleNamespace(apikey=pool_key, enabled=True)]),
                },
            )
        }
        rules = [
            {"provider": "mixed", "model": "shared-model"},
            {"provider": "mixed", "model": "shared-model", "upstream_key_pool": "main"},
        ]
        state.record_failure(
            "mixed",
            "shared-model",
            fingerprint_api_key(legacy_key),
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
        )

        ordered = state.order_rules_by_penalty(rules, providers_config)

        self.assertEqual(ordered[0].get("upstream_key_pool"), "main")
        self.assertIsNone(ordered[1].get("upstream_key_pool"))

    def test_fill_first_strategy_reuses_first_available_key_until_blocked(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        candidates = [
            UpstreamKeyCandidate(api_key="sk-first", order=0),
            UpstreamKeyCandidate(api_key="sk-second", order=1),
        ]

        self.assertEqual(
            state.select_key_from_candidates("provider", "model", candidates, strategy="fill-first").api_key,
            "sk-first",
        )
        self.assertEqual(
            state.select_key_from_candidates("provider", "model", candidates, strategy="fill-first").api_key,
            "sk-first",
        )

        state.record_failure(
            "provider",
            "model",
            fingerprint_api_key("sk-first"),
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
        )

        self.assertEqual(
            state.select_key_from_candidates("provider", "model", candidates, strategy="fill-first").api_key,
            "sk-second",
        )

    def test_priority_strategy_uses_highest_priority_group(self):
        state = UpstreamRoutingState()
        candidates = [
            UpstreamKeyCandidate(api_key="sk-low", order=0, priority=10),
            UpstreamKeyCandidate(api_key="sk-high-a", order=1, priority=100),
            UpstreamKeyCandidate(api_key="sk-high-b", order=2, priority=100),
        ]

        selected = [
            state.select_key_from_candidates("provider", "model", candidates, strategy="priority").api_key
            for _ in range(4)
        ]

        self.assertNotIn("sk-low", selected)
        self.assertEqual(selected, ["sk-high-a", "sk-high-b", "sk-high-a", "sk-high-b"])

    def test_session_affinity_reuses_available_key_until_ttl_expires(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        candidates = [
            UpstreamKeyCandidate(api_key="sk-first", order=0),
            UpstreamKeyCandidate(api_key="sk-second", order=1),
        ]

        first = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="session-a",
            session_affinity_ttl_seconds=10,
            pool_name="pool-a",
        )
        second = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="session-a",
            session_affinity_ttl_seconds=10,
            pool_name="pool-a",
        )

        self.assertEqual(first.api_key, "sk-first")
        self.assertEqual(second.api_key, "sk-first")

        clock.value += 11
        after_ttl = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="session-a",
            session_affinity_ttl_seconds=10,
            pool_name="pool-a",
        )

        self.assertEqual(after_ttl.api_key, "sk-second")

    def test_session_affinity_reselects_when_bound_key_is_blocked(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        candidates = [
            UpstreamKeyCandidate(api_key="sk-first", order=0),
            UpstreamKeyCandidate(api_key="sk-second", order=1),
        ]

        first = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="session-a",
            session_affinity_ttl_seconds=10,
        )
        state.record_failure(
            "provider",
            "model",
            first.fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=True,
        )

        reselected = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="session-a",
            session_affinity_ttl_seconds=10,
        )

        self.assertEqual(first.api_key, "sk-first")
        self.assertEqual(reselected.api_key, "sk-second")

    def test_session_affinity_is_namespaced_by_affinity_scope(self):
        state = UpstreamRoutingState()
        candidates = [
            UpstreamKeyCandidate(api_key="sk-first", order=0),
            UpstreamKeyCandidate(api_key="sk-second", order=1),
        ]

        first_scope = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="same-session-id",
            affinity_scope="user:1",
        )
        second_scope = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="same-session-id",
            affinity_scope="user:2",
        )
        first_scope_again = state.select_key_from_candidates(
            "provider",
            "model",
            candidates,
            strategy="round-robin",
            session_id="same-session-id",
            affinity_scope="user:1",
        )

        self.assertEqual(first_scope.api_key, "sk-first")
        self.assertEqual(second_scope.api_key, "sk-second")
        self.assertEqual(first_scope_again.api_key, "sk-first")

    def test_dynamic_penalty_ordering_is_opt_in_for_gateway_rule(self):
        static_url, static_order_calls = _first_chat_attempt_url(dynamic_penalty=False)
        dynamic_url, dynamic_order_calls = _first_chat_attempt_url(dynamic_penalty=True)

        self.assertEqual(static_url, "https://first.example/chat/completions")
        self.assertEqual(static_order_calls, 0)
        self.assertEqual(dynamic_url, "https://second.example/chat/completions")
        self.assertEqual(dynamic_order_calls, 1)

    def test_chat_uses_configured_priority_upstream_key_pool(self):
        _response, make_llm_request = _post_chat_with_loader(_build_pool_chat_config_loader())

        headers = make_llm_request.await_args_list[0].args[2]
        self.assertEqual(headers["Authorization"], "Bearer HIGH_POOL_SECRET")

    def test_chat_session_affinity_reuses_bound_upstream_key(self):
        upstream_state = UpstreamRoutingState()

        _response, first_request = _post_chat_with_loader(
            _build_affinity_chat_config_loader(),
            request_headers={"X-Workspace-Session": "workspace-a"},
            upstream_state=upstream_state,
        )
        _response, second_request = _post_chat_with_loader(
            _build_affinity_chat_config_loader(),
            request_headers={"X-Workspace-Session": "workspace-a"},
            upstream_state=upstream_state,
        )

        first_headers = first_request.await_args_list[0].args[2]
        second_headers = second_request.await_args_list[0].args[2]
        self.assertEqual(first_headers["Authorization"], "Bearer POOL_SECRET_A")
        self.assertEqual(second_headers["Authorization"], "Bearer POOL_SECRET_A")

    def test_chat_pool_only_provider_without_rule_pool_fails_before_downstream_call(self):
        loader = _build_pool_chat_config_loader()
        loader.fallback_rules["gateway-model"]["fallback_models"][0].pop("upstream_key_pool")

        with self.assertRaises(AssertionError) as context:
            _post_chat_with_loader(loader)

        self.assertIn("upstream_key_pool", str(context.exception))

    def test_chat_applies_payload_transforms_to_downstream_payload(self):
        loader = _build_chat_config_loader(dynamic_penalty=False)
        loader.fallback_rules["gateway-model"]["fallback_models"][0]["payload_transforms"] = {
            "defaults": {"top_p": 0.9},
            "overrides": {"parallel_tool_calls": False},
            "filters": ["seed"],
        }

        _response, make_llm_request = _post_chat_with_loader(
            loader,
            request_json={
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hello"}],
                "temperature": 0.2,
                "seed": 42,
            },
        )

        downstream_payload = make_llm_request.await_args_list[0].args[3]
        self.assertEqual(downstream_payload["model"], "first-model")
        self.assertEqual(downstream_payload["temperature"], 0.2)
        self.assertEqual(downstream_payload["top_p"], 0.9)
        self.assertFalse(downstream_payload["parallel_tool_calls"])
        self.assertNotIn("seed", downstream_payload)

    def test_cooldown_remaining_seconds_returns_none_for_unknown_ref(self):
        state = UpstreamRoutingState()

        self.assertIsNone(
            state.cooldown_remaining_seconds("openrouter", "unknown-model", "unknown-fp")
        )

    def test_cooldown_remaining_seconds_reflects_recorded_failure_and_does_not_create_state(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        key = "sk-cooldown-lookup-secret"
        fingerprint = fingerprint_api_key(key)

        # Read-only lookups for refs that never recorded anything must not
        # create a _RuntimeState entry (no inflating internal state via
        # speculative probing, e.g. while building an exhaustion trail).
        self.assertIsNone(state.cooldown_remaining_seconds("openrouter", "provider-model", fingerprint))
        self.assertEqual(state.get_status_rows(), [])

        state.record_failure(
            "openrouter",
            "provider-model",
            fingerprint,
            _UpstreamError(),
            temporary=True,
            apply_penalty=False,
        )

        remaining = state.cooldown_remaining_seconds("openrouter", "provider-model", fingerprint)
        self.assertIsNotNone(remaining)
        self.assertAlmostEqual(remaining, RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS, delta=0.01)

        clock.value += RATE_LIMIT_TRANSIENT_COOLDOWN_SECONDS + 0.1
        self.assertIsNone(state.cooldown_remaining_seconds("openrouter", "provider-model", fingerprint))

    def test_has_zero_remaining_quota_block_reflects_observed_limit_lifecycle(self):
        clock = _Clock()
        state = UpstreamRoutingState(time_func=clock.time, monotonic_func=clock.monotonic)
        provider, model = "groq", "provider-model"
        key = "sk-zero-remaining-lifecycle-secret"
        fingerprint = fingerprint_api_key(key)

        self.assertFalse(state.has_zero_remaining_quota_block(provider, model, fingerprint))

        state.record_observed_limit(
            provider,
            model,
            fingerprint,
            "rpm",
            limit=100,
            remaining=0,
            reset_at_monotonic=clock.value + 30.0,
            source="header",
        )
        self.assertTrue(state.has_zero_remaining_quota_block(provider, model, fingerprint))

        clock.value += 31.0
        self.assertFalse(state.has_zero_remaining_quota_block(provider, model, fingerprint))

    def test_has_zero_remaining_quota_block_returns_false_for_unknown_ref_without_creating_state(self):
        state = UpstreamRoutingState()

        self.assertFalse(
            state.has_zero_remaining_quota_block("openrouter", "unknown-model", "unknown-fp")
        )
        self.assertEqual(state.get_status_rows(), [])

    def test_chat_model_alias_routes_to_effective_gateway_rule(self):
        loader = _build_chat_config_loader(dynamic_penalty=False)
        loader.model_rules = {"aliases": {"public-model": "gateway-model"}}

        _response, make_llm_request = _post_chat_with_loader(
            loader,
            request_json={
                "model": "public-model",
                "messages": [{"role": "user", "content": "hello"}],
            },
        )

        self.assertEqual(make_llm_request.await_args_list[0].args[1], "https://first.example/chat/completions")
        downstream_payload = make_llm_request.await_args_list[0].args[3]
        self.assertEqual(downstream_payload["model"], "first-model")

if __name__ == "__main__":
    unittest.main()
