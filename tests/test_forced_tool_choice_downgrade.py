"""Learned downgrade of a rejected forced ``tool_choice``.

alibaba's compatible-mode endpoint accepts ``tools`` but answers
``tool_choice: "required"`` (or a named-function object) with a 400 whenever
the model runs in thinking mode, which it cannot be talked out of. The gateway
retries such an attempt once as ``"auto"`` and remembers the rejection per
(provider, model) so later requests skip the doomed attempt.
"""

import unittest
from unittest.mock import Mock

from llm_gateway_core.services.error_classifier import (
    downgrade_forced_tool_choice,
    has_forced_tool_choice,
    is_forced_tool_choice_unsupported_error,
)
from llm_gateway_core.services.request_handler import RequestErrorDetail
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests.test_chat_dispatch_ratelimit_headers import _ChatRateLimitHeaderScenario

ALIBABA_REJECTION = (
    '{"error":{"code":"invalid_parameter_error","param":null,"message":"The tool_choice '
    'parameter does not support being set to required or object in thinking mode",'
    '"type":"invalid_request_error"},"id":"chatcmpl-e3189b65"}'
)


class ForcedToolChoiceErrorDetectionTests(unittest.TestCase):
    def test_alibaba_thinking_mode_rejection_is_detected(self):
        self.assertTrue(is_forced_tool_choice_unsupported_error(ALIBABA_REJECTION))

    def test_detection_works_on_a_parsed_error_body(self):
        self.assertTrue(
            is_forced_tool_choice_unsupported_error(
                {"error": {"message": "tool_choice must be one of: auto, none"}}
            )
        )

    def test_unrelated_errors_are_not_detected(self):
        # A tools-schema complaint names no tool_choice, and a tool_choice
        # mention without a rejection marker is not a refusal of the forced
        # form -- a downgrade would fix neither.
        for error_detail in (
            '{"error":{"message":"tools[0].function.name is invalid"}}',
            '{"error":{"message":"This model does not support tools"}}',
            '{"error":{"message":"tool_choice was applied to 3 tools"}}',
            "Model failed with 429 Too Many Requests",
            None,
        ):
            with self.subTest(error_detail=error_detail):
                self.assertFalse(is_forced_tool_choice_unsupported_error(error_detail))


class ForcedToolChoiceDowngradeTests(unittest.TestCase):
    def test_required_is_lowered_to_auto(self):
        payload = {"tool_choice": "required", "tools": [{"type": "function"}]}
        self.assertTrue(downgrade_forced_tool_choice(payload))
        self.assertEqual(payload["tool_choice"], "auto")

    def test_named_function_object_is_lowered_to_auto(self):
        payload = {"tool_choice": {"type": "function", "function": {"name": "run_sql"}}}
        self.assertTrue(downgrade_forced_tool_choice(payload))
        self.assertEqual(payload["tool_choice"], "auto")

    def test_non_forced_tool_choices_are_left_alone(self):
        for tool_choice in ("auto", "none"):
            with self.subTest(tool_choice=tool_choice):
                payload = {"tool_choice": tool_choice}
                self.assertFalse(has_forced_tool_choice(payload))
                self.assertFalse(downgrade_forced_tool_choice(payload))
                self.assertEqual(payload["tool_choice"], tool_choice)

    def test_payload_without_tool_choice_is_untouched(self):
        payload = {"tools": [{"type": "function"}]}
        self.assertFalse(downgrade_forced_tool_choice(payload))
        self.assertNotIn("tool_choice", payload)


class ForcedToolChoiceRoutingStateTests(unittest.TestCase):
    def test_rejection_is_remembered_per_provider_and_model(self):
        state = UpstreamRoutingState()
        self.assertFalse(state.forced_tool_choice_unsupported("alibaba", "qwen3.8-max-preview"))

        state.record_forced_tool_choice_unsupported("alibaba", "qwen3.8-max-preview")

        self.assertTrue(state.forced_tool_choice_unsupported("alibaba", "qwen3.8-max-preview"))
        # What one model refuses says nothing about its neighbours.
        self.assertFalse(state.forced_tool_choice_unsupported("alibaba", "qwen3.6-flash"))
        self.assertFalse(state.forced_tool_choice_unsupported("z.ai", "qwen3.8-max-preview"))

    def test_rejection_is_shared_across_upstream_keys(self):
        # The refusal comes from the model, not from the credential, so a key
        # switch must not un-learn it.
        state = UpstreamRoutingState()
        state.record_failure("alibaba", "qwen3.8-max-preview", "key-a", "boom", temporary=False, apply_penalty=False)
        state.record_forced_tool_choice_unsupported("alibaba", "qwen3.8-max-preview")
        state.record_failure("alibaba", "qwen3.8-max-preview", "key-b", "boom", temporary=False, apply_penalty=False)

        rows = state.get_status_rows()
        self.assertEqual(len(rows), 2)
        self.assertTrue(all(row["forced_tool_choice_unsupported"] for row in rows))

    def test_status_rows_report_untouched_models_as_supported(self):
        state = UpstreamRoutingState()
        state.record_failure("groq", "groq-model", "key-a", "boom", temporary=False, apply_penalty=False)

        self.assertFalse(state.get_status_rows()[0]["forced_tool_choice_unsupported"])


def _post_tool_call_chat(scenario: _ChatRateLimitHeaderScenario, tool_choice: object = "required"):
    return scenario.client.post(
        "/v1/chat/completions",
        json={
            "model": "gateway-model",
            "messages": [{"role": "user", "content": "weather in Moscow?"}],
            "tools": [
                {
                    "type": "function",
                    "function": {
                        "name": "get_weather",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            "tool_choice": tool_choice,
        },
        headers={"Authorization": "Bearer test-gateway-key"},
    )


def _success_response() -> dict:
    return {
        "id": "groq-success",
        "choices": [{"finish_reason": "stop", "message": {"role": "assistant", "content": "ok"}}],
    }


def _sent_tool_choices(make_llm_request: Mock) -> list[object]:
    return [call.args[3].get("tool_choice") for call in make_llm_request.call_args_list]


class ForcedToolChoiceDispatchTests(unittest.TestCase):
    """The configured rule leaves ``retry_count`` unset, so it normalizes to 0:
    any second upstream call proves the downgrade retry did not consume a retry.
    """

    def test_rejected_forced_tool_choice_is_retried_as_auto(self):
        async def fake_make_llm_request(*args, **_kwargs):
            if args[3].get("tool_choice") == "required":
                return None, RequestErrorDetail(ALIBABA_REJECTION, status_code=400)
            return _success_response(), None

        with _ChatRateLimitHeaderScenario(fake_make_llm_request) as scenario:
            response = _post_tool_call_chat(scenario)

            self.assertEqual(response.status_code, 200)
            self.assertEqual(_sent_tool_choices(scenario.make_llm_request), ["required", "auto"])
            self.assertTrue(
                scenario.upstream_state.forced_tool_choice_unsupported("groq", "groq-model")
            )

    def test_learned_rejection_downgrades_the_next_request_upfront(self):
        async def fake_make_llm_request(*args, **_kwargs):
            if args[3].get("tool_choice") == "required":
                return None, RequestErrorDetail(ALIBABA_REJECTION, status_code=400)
            return _success_response(), None

        with _ChatRateLimitHeaderScenario(fake_make_llm_request) as scenario:
            first = _post_tool_call_chat(scenario)
            scenario.make_llm_request.reset_mock()
            second = _post_tool_call_chat(scenario)

            self.assertEqual((first.status_code, second.status_code), (200, 200))
            # No doomed "required" attempt this time.
            self.assertEqual(_sent_tool_choices(scenario.make_llm_request), ["auto"])

    def test_downgrade_is_applied_at_most_once_per_rule(self):
        async def always_reject(*_args, **_kwargs):
            return None, RequestErrorDetail(ALIBABA_REJECTION, status_code=400)

        with _ChatRateLimitHeaderScenario(always_reject) as scenario:
            response = _post_tool_call_chat(scenario)

            self.assertNotEqual(response.status_code, 200)
            self.assertEqual(_sent_tool_choices(scenario.make_llm_request), ["required", "auto"])

    def test_other_400s_do_not_trigger_a_downgrade_retry(self):
        async def reject_with_unrelated_error(*_args, **_kwargs):
            return None, RequestErrorDetail(
                '{"error":{"message":"text content is empty"}}', status_code=400
            )

        with _ChatRateLimitHeaderScenario(reject_with_unrelated_error) as scenario:
            response = _post_tool_call_chat(scenario)

            self.assertNotEqual(response.status_code, 200)
            self.assertEqual(_sent_tool_choices(scenario.make_llm_request), ["required"])
            self.assertFalse(
                scenario.upstream_state.forced_tool_choice_unsupported("groq", "groq-model")
            )

    def test_non_forced_tool_choice_is_not_downgraded(self):
        # An upstream blaming tool_choice when the caller never forced one is
        # complaining about something a downgrade cannot fix.
        async def always_reject(*_args, **_kwargs):
            return None, RequestErrorDetail(ALIBABA_REJECTION, status_code=400)

        with _ChatRateLimitHeaderScenario(always_reject) as scenario:
            response = _post_tool_call_chat(scenario, tool_choice="auto")

            self.assertNotEqual(response.status_code, 200)
            self.assertEqual(_sent_tool_choices(scenario.make_llm_request), ["auto"])
            self.assertFalse(
                scenario.upstream_state.forced_tool_choice_unsupported("groq", "groq-model")
            )


if __name__ == "__main__":
    unittest.main()
