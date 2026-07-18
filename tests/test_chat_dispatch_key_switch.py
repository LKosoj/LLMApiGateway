"""Integration tests for B-imm: immediate upstream-key switch on rate-limit/quota

blocks inside ``attempt_model_fallback_rule``. A blocked key (429, daily-quota
exhaustion, or an observed ``remaining == 0`` header) triggers an immediate
re-selection among the remaining key candidates for the *same* rule/sub-provider,
without consuming ``retry_count``, capped by the number of key candidates.
"""

import unittest
from contextlib import ExitStack
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.services.request_handler import RequestErrorDetail
from llm_gateway_core.services.upstream_routing_state import (
    SelectedUpstreamKey,
    UpstreamRoutingState,
)
from tests.chat_accounting_test_support import install_main_chat_accounting_double


def _successful_response() -> dict:
    return {
        "id": "chatcmpl-key-switch",
        "object": "chat.completion",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "ok"},
                "finish_reason": "stop",
            }
        ],
    }


def _rate_limited_error() -> RequestErrorDetail:
    return RequestErrorDetail("Rate limit exceeded, please slow down", status_code=429)


def _daily_quota_error() -> RequestErrorDetail:
    return RequestErrorDetail(
        "Daily quota exhausted. Try again after reset.", status_code=429
    )


def _two_key_groq_provider(*, session_affinity: bool = False) -> SimpleNamespace:
    kwargs = {"baseUrl": "https://api.groq.com/openai/v1", "apikey": "KEY-A,KEY-B"}
    if session_affinity:
        kwargs["routing"] = SimpleNamespace(
            strategy="round-robin",
            session_affinity=True,
            session_affinity_header="X-Session-Id",
            session_affinity_ttl_seconds=3600,
        )
    return SimpleNamespace(**kwargs)


class _KeySwitchScenario:
    """Boots ``main.app`` with a real, real-clock ``UpstreamRoutingState`` and a
    caller-supplied ``fallback_models`` chain / ``providers_config`` so tests can
    drive one or more chat completions against real key-switch logic.

    ``self.auth_headers`` records the ``Authorization`` header value seen by
    each ``make_llm_request`` call, *in call order*. It exists because the
    ``headers`` dict is mutated in place across a key switch (same object
    reused for the retry), so inspecting ``make_llm_request.await_args_list``
    after the request completes would only ever show the final key's headers
    for every recorded call.
    """

    def __init__(
        self,
        providers_config: dict,
        fallback_models: list[dict],
        make_llm_request_side_effect,
        *,
        max_total_attempts: int | None = None,
    ) -> None:
        self._stack = ExitStack()
        self._providers_config = providers_config
        self._fallback_models = fallback_models
        self._make_llm_request_side_effect = make_llm_request_side_effect
        self._max_total_attempts = max_total_attempts
        self.upstream_state = UpstreamRoutingState()
        self.auth_headers: list[str | None] = []

    def __enter__(self) -> "_KeySwitchScenario":
        stack = self._stack
        install_main_chat_accounting_double(stack)
        config_loader_cls = stack.enter_context(patch("main.ConfigLoader"))
        async_client_ctor = stack.enter_context(patch("main.create_shared_http_client"))
        stack.enter_context(patch("main.TokensUsageDB"))
        openrouter_service_cls = stack.enter_context(patch("main.OpenRouterFreeModelsService"))
        fallback_eval_service_cls = stack.enter_context(patch("main.FallbackModelEvalService"))
        self.make_llm_request = stack.enter_context(
            patch("llm_gateway_core.api.v1.chat.make_llm_request")
        )

        loader = Mock()
        loader.configured_paths = {}
        loader.providers_config = self._providers_config
        fallback_rule_entry: dict = {
            "fallback_models": self._fallback_models,
            "rotate_models": False,
        }
        if self._max_total_attempts is not None:
            fallback_rule_entry["max_total_attempts"] = self._max_total_attempts
        loader.fallback_rules = {"gateway-model": fallback_rule_entry}
        loader.operation_rules = {}
        loader.model_rules = {}
        loader.load_providers.return_value = loader.providers_config
        loader.load_fallback_rules.return_value = loader.fallback_rules
        loader.load_model_rules.return_value = loader.model_rules
        loader.load_operation_rules.return_value = loader.operation_rules
        loader.validate_fallback_operation_consistency.return_value = None
        loader.load_complete.return_value = loader
        config_loader_cls.return_value = loader

        fake_http_client = Mock()
        fake_http_client.aclose = AsyncMock()
        async_client_ctor.return_value = fake_http_client
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()
        stack.enter_context(
            patch("main.ConfigUpdateCoordinator", return_value=config_update_coordinator)
        )

        openrouter_service = Mock()
        openrouter_service.start_runtime = AsyncMock()
        openrouter_service.stop = AsyncMock()
        openrouter_service_cls.return_value = openrouter_service

        fallback_eval_service = Mock()
        fallback_eval_service.stop = AsyncMock()
        fallback_eval_service_cls.return_value = fallback_eval_service

        user_side_effect = self._make_llm_request_side_effect

        async def _recording_side_effect(*args, **kwargs):
            headers_arg = args[2] if len(args) > 2 else {}
            self.auth_headers.append(headers_arg.get("Authorization"))
            return await user_side_effect(*args, **kwargs)

        self.make_llm_request.side_effect = _recording_side_effect

        stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
        stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))
        stack.enter_context(patch("main.UpstreamRoutingState", return_value=self.upstream_state))

        self.client = stack.enter_context(TestClient(main.app))
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stack.close()

    def post_chat(self, headers: dict | None = None):
        request_headers = {"Authorization": "Bearer test-gateway-key"}
        if headers:
            request_headers.update(headers)
        return self.client.post(
            "/v1/chat/completions",
            json={"model": "gateway-model", "messages": [{"role": "user", "content": "hello"}]},
            headers=request_headers,
        )


class KeySwitchTests(unittest.TestCase):
    def test_rate_limit_switches_key_same_rule_and_succeeds_without_consuming_retry(self):
        # Also covers scenario 5 (retry_count=0 by default; the switch does not
        # decrement/consume it -- the 2nd attempt happens anyway).
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]
        responses = iter([(None, _rate_limited_error()), (_successful_response(), None)])

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        self.assertEqual(len(scenario.auth_headers), 2)
        self.assertIsNotNone(scenario.auth_headers[0])
        self.assertIsNotNone(scenario.auth_headers[1])
        self.assertNotEqual(scenario.auth_headers[0], scenario.auth_headers[1])

    def test_daily_quota_exhaustion_switches_key_and_succeeds(self):
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]
        responses = iter([(None, _daily_quota_error()), (_successful_response(), None)])

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        self.assertNotEqual(scenario.auth_headers[0], scenario.auth_headers[1])

    def test_observed_zero_remaining_without_429_switches_key(self):
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]
        call_count = {"value": 0}

        async def fake_make_llm_request(*_args, **kwargs):
            call_count["value"] += 1
            if call_count["value"] == 1:
                sink = kwargs.get("response_headers_sink")
                if sink is not None:
                    sink.update(
                        {
                            "x-ratelimit-limit-requests": "100",
                            "x-ratelimit-remaining-requests": "0",
                            # Comfortably longer than this test's run time.
                            "x-ratelimit-reset-requests": "1h0m0s",
                        }
                    )
                return None, RequestErrorDetail(
                    "Upstream request failed with HTTP status 500.", status_code=500
                )
            return _successful_response(), None

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        self.assertNotEqual(scenario.auth_headers[0], scenario.auth_headers[1])

    def test_non_blocking_failure_retries_same_key_without_switching(self):
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {
                "provider": "groq",
                "model": "groq-model",
                "use_provider_order_as_fallback": False,
                "retry_count": 1,
                "retry_delay": 0,
            }
        ]
        responses = iter(
            [
                (None, RequestErrorDetail("Upstream request failed with HTTP status 500.", status_code=500)),
                (_successful_response(), None),
            ]
        )

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        self.assertEqual(
            scenario.auth_headers[0],
            scenario.auth_headers[1],
            "a plain (non rate-limit) temporary failure must retry the same key, not switch",
        )

    def test_key_switch_budget_equals_number_of_key_candidates(self):
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]

        async def fake_make_llm_request(*_args, **kwargs):
            return None, _rate_limited_error()

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            scenario.make_llm_request.await_count,
            2,
            "the key-switch budget must equal the number of key candidates (2), not loop further",
        )
        self.assertNotEqual(scenario.auth_headers[0], scenario.auth_headers[1])
        body = response.json()
        # Scenario 8: the attempt trail carries anonymized labels for both keys.
        self.assertEqual(len(body["attempts"]), 2)
        self.assertEqual([entry["key"] for entry in body["attempts"]], ["key1", "key2"])
        for entry in body["attempts"]:
            self.assertEqual(entry["provider"], "groq")
            self.assertEqual(entry["error_class"], "http_429")
            self.assertEqual(entry["http_status"], 429)

    def test_total_attempts_budget_caps_key_switch_too(self):
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]

        async def fake_make_llm_request(*_args, **kwargs):
            return None, _rate_limited_error()

        with _KeySwitchScenario(
            providers_config, fallback_models, fake_make_llm_request, max_total_attempts=1
        ) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            scenario.make_llm_request.await_count,
            1,
            "total_attempts_budget=1 must stop the chain before a key-switch retry",
        )

    def test_single_key_provider_with_429_behaves_as_before(self):
        providers_config = {"groq": SimpleNamespace(baseUrl="https://api.groq.com/openai/v1", apikey="ONLY-KEY")}
        fallback_models = [
            {
                "provider": "groq",
                "model": "groq-model",
                "use_provider_order_as_fallback": False,
                "retry_count": 1,
                "retry_delay": 0,
            }
        ]

        async def fake_make_llm_request(*_args, **kwargs):
            return None, _rate_limited_error()

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            scenario.make_llm_request.await_count,
            2,
            "with a single key there is nowhere to switch to; the local retry_count still applies",
        )
        self.assertEqual(
            scenario.auth_headers[0],
            scenario.auth_headers[1],
            "with a single key candidate every attempt reuses the same (only) key",
        )

    def test_all_keys_exhausted_advances_to_next_fallback_rule(self):
        providers_config = {
            "groq": _two_key_groq_provider(),
            "backup": SimpleNamespace(baseUrl="https://backup.example", apikey="BACKUP-KEY"),
        }
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False},
            {"provider": "backup", "model": "backup-model", "use_provider_order_as_fallback": False},
        ]
        responses = iter(
            [
                (None, _rate_limited_error()),
                (None, _rate_limited_error()),
                (_successful_response(), None),
            ]
        )

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 3)
        third_call_url = scenario.make_llm_request.await_args_list[2].args[1]
        self.assertEqual(third_call_url, "https://backup.example/chat/completions")

    def test_session_affinity_reselects_a_different_key_after_block(self):
        providers_config = {"groq": _two_key_groq_provider(session_affinity=True)}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]
        responses = iter([(None, _rate_limited_error()), (_successful_response(), None)])

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat(headers={"X-Session-Id": "session-key-switch-1"})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        self.assertNotEqual(
            scenario.auth_headers[0],
            scenario.auth_headers[1],
            "session affinity must not pin the retry to the blocked key",
        )

    def test_sub_provider_branch_retries_same_sub_provider_with_new_key(self):
        provider_config = _two_key_groq_provider()
        provider_config.baseUrl = "https://openrouter.ai/api/v1"
        providers_config = {"openrouter": provider_config}
        fallback_models = [
            {
                "provider": "openrouter",
                "model": "openrouter-model",
                "use_provider_order_as_fallback": True,
                "providers_order": ["sub-a", "sub-b"],
            }
        ]
        responses = iter([(None, _rate_limited_error()), (_successful_response(), None)])

        async def fake_make_llm_request(*_args, **kwargs):
            return next(responses)

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 200)
        self.assertEqual(scenario.make_llm_request.await_count, 2)
        first_call = scenario.make_llm_request.await_args_list[0]
        second_call = scenario.make_llm_request.await_args_list[1]
        self.assertEqual(
            first_call.args[3]["provider"]["order"],
            second_call.args[3]["provider"]["order"],
            "a key switch must retry the same sub-provider, not advance to the next one",
        )
        self.assertEqual(first_call.args[3]["provider"]["order"], ["sub-a"])
        self.assertNotEqual(scenario.auth_headers[0], scenario.auth_headers[1])

    def test_behavior_class_failure_with_zero_remaining_header_does_not_switch_key(self):
        """Guard coverage: ``not is_behavior_class_failure`` must block the
        immediate key switch even when the *same* attempt's response also
        carries an observed ``remaining == 0`` rate-limit header (which alone
        would satisfy ``has_zero_remaining_quota_block()`` and trigger a
        switch). A behavior-class failure (``empty_completion`` here) keeps
        its pre-existing contract: skip local retry/key-switch and advance
        straight to the next fallback rule.
        """
        providers_config = {
            "groq": _two_key_groq_provider(),
            "backup": SimpleNamespace(baseUrl="https://backup.example", apikey="BACKUP-KEY"),
        }
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False},
            {"provider": "backup", "model": "backup-model", "use_provider_order_as_fallback": False},
        ]

        def _empty_completion_response() -> dict:
            return {
                "id": "chatcmpl-empty",
                "object": "chat.completion",
                "choices": [
                    {
                        "index": 0,
                        "message": {"role": "assistant", "content": ""},
                        "finish_reason": "stop",
                    }
                ],
            }

        call_urls: list[str] = []

        async def fake_make_llm_request(*args, **kwargs):
            call_urls.append(args[1])
            if len(call_urls) == 1:
                sink = kwargs.get("response_headers_sink")
                if sink is not None:
                    sink.update(
                        {
                            "x-ratelimit-limit-requests": "100",
                            "x-ratelimit-remaining-requests": "0",
                            # Comfortably longer than this test's run time.
                            "x-ratelimit-reset-requests": "1h0m0s",
                        }
                    )
                return _empty_completion_response(), None
            return None, _rate_limited_error()

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            response = scenario.post_chat()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            scenario.make_llm_request.await_count,
            2,
            "exactly 1 attempt for the groq rule (no key switch despite the "
            "zero-remaining header) plus 1 attempt for the next fallback rule",
        )
        self.assertEqual(call_urls[0], "https://api.groq.com/openai/v1/chat/completions")
        self.assertEqual(
            call_urls[1],
            "https://backup.example/chat/completions",
            "the request must advance to the next fallback rule, not retry groq with a switched key",
        )
        body = response.json()
        self.assertEqual(len(body["attempts"]), 2)
        self.assertEqual(body["attempts"][0]["provider"], "groq")
        self.assertEqual(
            body["attempts"][0]["error_class"],
            "empty_completion",
            "the attempt trail must record the behavior-class failure",
        )
        self.assertEqual(body["attempts"][1]["provider"], "backup")

    def test_key_switch_counter_is_the_sole_loop_limiter(self):
        """``key_switches_used`` must be what stops the immediate-key-switch
        loop, independent of the natural cooldown-based exclusion (a key
        becoming unavailable after being blocked). Monkeypatches
        ``UpstreamRoutingState.select_key_from_candidates`` on this scenario's
        real routing state so it always reports a key as available, with an
        alternating fingerprint on every call -- mimicking a pathological /
        racy selection strategy that never naturally runs out of candidates.
        Every upstream response is a 429, so with the counter working the
        loop must stop after exactly ``key_switch_budget`` (== number of key
        candidates == 2) switches plus the initial attempt. A hard iteration
        cap in the mock turns a regression (missing increment => infinite
        loop) into a prompt test failure instead of a hang.
        """
        providers_config = {"groq": _two_key_groq_provider()}
        fallback_models = [
            {"provider": "groq", "model": "groq-model", "use_provider_order_as_fallback": False}
        ]
        hard_iteration_cap = 10
        call_count = {"value": 0}

        async def fake_make_llm_request(*_args, **kwargs):
            call_count["value"] += 1
            if call_count["value"] > hard_iteration_cap:
                raise AssertionError(
                    "make_llm_request exceeded the hard iteration cap: the "
                    "key-switch loop did not terminate (key_switches_used "
                    "increment likely missing)"
                )
            return None, _rate_limited_error()

        fingerprint_toggle = {"value": 0}

        def _always_available_alternating_key(*_args, **_kwargs) -> SelectedUpstreamKey:
            fingerprint_toggle["value"] += 1
            return SelectedUpstreamKey(
                api_key="racy-key", fingerprint=f"racy-fp-{fingerprint_toggle['value']}"
            )

        with _KeySwitchScenario(providers_config, fallback_models, fake_make_llm_request) as scenario:
            with patch.object(
                scenario.upstream_state,
                "select_key_from_candidates",
                side_effect=_always_available_alternating_key,
            ):
                response = scenario.post_chat()

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            scenario.make_llm_request.await_count,
            3,
            "1 initial attempt + 2 key switches (key_switch_budget == len(key_candidates) == 2); "
            "with availability forced True, only the key_switches_used counter can cap the loop",
        )


if __name__ == "__main__":
    unittest.main()
