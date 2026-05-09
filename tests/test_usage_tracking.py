"""Tests for llm_gateway_core.utils.usage_tracking module."""

import json
import unittest

from llm_gateway_core.utils.usage_tracking import (
    ModelCostRates,
    _estimate_cost_saved,
    _split_response_text,
    backfill_zero_token_counts,
    build_model_cost_rate_registry,
    estimate_prompt_tokens,
    estimate_token_count,
    extract_tokens_usage,
)


class ExtractTokensUsageTests(unittest.TestCase):
    """Tests for extract_tokens_usage function."""

    def test_openai_format_usage(self):
        """Test extraction from OpenAI format (prompt_tokens/completion_tokens)."""
        payload = {
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 20,
                "total_tokens": 30,
                "cost": 0.005,
            },
            "model": "gpt-4",
            "provider": "openai",
            "request_id": "req-123",
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["prompt_tokens"], 10)
        self.assertEqual(result["completion_tokens"], 20)
        self.assertEqual(result["total_tokens"], 30)
        self.assertEqual(result["cost"], 0.005)
        self.assertEqual(result["model"], "gpt-4")
        self.assertEqual(result["provider"], "openai")
        self.assertEqual(result["request_id"], "req-123")

    def test_anthropic_format_usage(self):
        """Test extraction from Anthropic format (input_tokens/output_tokens)."""
        payload = {
            "usage": {
                "input_tokens": 15,
                "output_tokens": 25,
            },
            "model": "claude-sonnet-4-20250514",
            "provider": "anthropic",
            "request_id": "msg-456",
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["prompt_tokens"], 15)
        self.assertEqual(result["completion_tokens"], 25)
        # Total should be calculated automatically
        self.assertEqual(result["total_tokens"], 40)
        self.assertEqual(result["model"], "claude-sonnet-4-20250514")
        self.assertEqual(result["provider"], "anthropic")
        self.assertEqual(result["request_id"], "msg-456")

    def test_anthropic_format_usage_includes_cache_tokens(self):
        payload = {
            "usage": {
                "input_tokens": 13,
                "cache_creation_input_tokens": 100,
                "cache_read_input_tokens": 13120,
                "output_tokens": 5,
            },
            "model": "kimi-for-coding",
            "provider": "kimicode",
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["prompt_tokens"], 13233)
        self.assertEqual(result["completion_tokens"], 5)
        self.assertEqual(result["total_tokens"], 13238)
        self.assertEqual(result["cached_tokens"], 13120)

    def test_anthropic_format_with_explicit_total(self):
        """Test Anthropic format when total_tokens is explicitly provided."""
        payload = {
            "usage": {
                "input_tokens": 15,
                "output_tokens": 25,
                "total_tokens": 50,  # Explicit total (might include cache reads)
            },
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["prompt_tokens"], 15)
        self.assertEqual(result["completion_tokens"], 25)
        # Should use explicit total, not calculated
        self.assertEqual(result["total_tokens"], 50)

    def test_empty_payload(self):
        """Test with None payload."""
        result = extract_tokens_usage(None)
        self.assertEqual(result["prompt_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 0)
        self.assertEqual(result["total_tokens"], 0)

    def test_missing_usage_field(self):
        """Test when usage field is missing."""
        payload = {"model": "test-model"}
        result = extract_tokens_usage(payload)
        self.assertEqual(result["prompt_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 0)
        self.assertEqual(result["total_tokens"], 0)
        self.assertEqual(result["model"], "test-model")

    def test_openai_usage_negative_values_are_clamped(self):
        payload = {
            "usage": {
                "prompt_tokens": -10,
                "completion_tokens": -5,
                "total_tokens": -15,
                "cost": -1.25,
                "prompt_tokens_details": {"cached_tokens": -3},
                "completion_tokens_details": {"reasoning_tokens": -2},
            }
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["prompt_tokens"], 0)
        self.assertEqual(result["completion_tokens"], 0)
        self.assertEqual(result["total_tokens"], 0)
        self.assertEqual(result["cost"], 0)
        self.assertEqual(result["cached_tokens"], 0)
        self.assertEqual(result["reasoning_tokens"], 0)


class EstimatePromptTokensTests(unittest.TestCase):
    def test_returns_positive_count_for_valid_request_body(self):
        body = json.dumps({
            "messages": [
                {"role": "system", "content": "You are a helpful assistant."},
                {"role": "user", "content": "Hello, how are you?"},
            ],
            "model": "gpt-4",
        })
        count = estimate_prompt_tokens(body, "gpt-4")
        self.assertGreater(count, 0)

    def test_returns_zero_for_empty_string(self):
        self.assertEqual(estimate_prompt_tokens("", "gpt-4"), 0)

    def test_returns_zero_for_invalid_json(self):
        self.assertEqual(estimate_prompt_tokens("{broken", "gpt-4"), 0)

    def test_works_with_unknown_model_via_fallback_encoding(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        count = estimate_prompt_tokens(body, "totally-unknown-model-xyz")
        self.assertGreater(count, 0)

    def test_works_with_none_model(self):
        body = json.dumps({"messages": [{"role": "user", "content": "hi"}]})
        count = estimate_prompt_tokens(body, None)
        self.assertGreater(count, 0)


class EstimateTokenCountTests(unittest.TestCase):
    def test_returns_positive_count_for_nonempty_text(self):
        count = estimate_token_count("Hello, how can I help you today?", "gpt-4")
        self.assertGreater(count, 0)

    def test_returns_zero_for_empty_string(self):
        self.assertEqual(estimate_token_count("", "gpt-4"), 0)

    def test_works_with_unknown_model(self):
        count = estimate_token_count("some text", "unknown-model-abc")
        self.assertGreater(count, 0)


class SplitResponseTextTests(unittest.TestCase):
    def test_no_markers_returns_full_text_as_completion(self):
        reasoning, completion = _split_response_text("Hello world!")
        self.assertEqual(reasoning, "")
        self.assertEqual(completion, "Hello world!")

    def test_reasoning_only(self):
        text = "\n\n[Reasoning]:\nThinking about the answer..."
        reasoning, completion = _split_response_text(text)
        self.assertEqual(reasoning, "Thinking about the answer...")
        self.assertEqual(completion, "")

    def test_reasoning_and_response(self):
        text = "\n\n[Reasoning]:\nLet me think step by step.\n\n[Response]:\nThe answer is 42."
        reasoning, completion = _split_response_text(text)
        self.assertEqual(reasoning, "Let me think step by step.")
        self.assertEqual(completion, "The answer is 42.")

    def test_response_text_before_reasoning_is_preserved(self):
        text = "Preamble text\n\n[Reasoning]:\nThinking...\n\n[Response]:\nFinal answer."
        reasoning, completion = _split_response_text(text)
        self.assertEqual(reasoning, "Thinking...")
        self.assertIn("Preamble text", completion)
        self.assertIn("Final answer.", completion)

    def test_tool_use_stripped_from_completion(self):
        text = "\n\n[Reasoning]:\nThinking...\n\n[Response]:\nHere is the result.\n\n[Tool Use: search]\n{\"query\":\"test\"}"
        reasoning, completion = _split_response_text(text)
        self.assertEqual(reasoning, "Thinking...")
        self.assertEqual(completion, "Here is the result.")


class BackfillZeroTokenCountsTests(unittest.TestCase):
    def test_backfills_both_when_zero(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)
        self.assertEqual(usage["total_tokens"], usage["prompt_tokens"] + usage["completion_tokens"])

    def test_does_not_overwrite_nonzero_prompt(self):
        usage = {"prompt_tokens": 42, "completion_tokens": 0, "total_tokens": 42, "model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertEqual(usage["prompt_tokens"], 42)
        self.assertGreater(usage["completion_tokens"], 0)

    def test_does_not_overwrite_nonzero_completion(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 10, "total_tokens": 10, "model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 10)

    def test_returns_false_when_nothing_to_backfill(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30, "model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertFalse(changed)
        self.assertEqual(usage["prompt_tokens"], 10)
        self.assertEqual(usage["completion_tokens"], 20)
        self.assertEqual(usage["total_tokens"], 30)

    def test_handles_empty_body_and_response(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}

        changed = backfill_zero_token_counts(usage, "", "")

        self.assertFalse(changed)
        self.assertEqual(usage["prompt_tokens"], 0)
        self.assertEqual(usage["completion_tokens"], 0)

    def test_uses_gateway_model_when_model_missing(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "gateway_model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)

    def test_backfills_reasoning_tokens_separately(self):
        usage = {
            "prompt_tokens": 0, "completion_tokens": 0,
            "reasoning_tokens": 0, "total_tokens": 0, "model": "gpt-4",
        }
        body = json.dumps({"messages": [{"role": "user", "content": "Think step by step"}]})
        response = "\n\n[Reasoning]:\nStep 1: analyze.\nStep 2: conclude.\n\n[Response]:\nThe answer is 42."

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertGreater(usage["prompt_tokens"], 0)
        self.assertGreater(usage["completion_tokens"], 0)
        self.assertGreater(usage["reasoning_tokens"], 0)
        # reasoning and completion should be separate counts
        self.assertNotEqual(usage["reasoning_tokens"], usage["completion_tokens"])

    def test_does_not_overwrite_nonzero_reasoning(self):
        usage = {
            "prompt_tokens": 10, "completion_tokens": 0,
            "reasoning_tokens": 50, "total_tokens": 10, "model": "gpt-4",
        }
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "\n\n[Reasoning]:\nThinking...\n\n[Response]:\nDone."

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)  # completion was backfilled
        self.assertEqual(usage["reasoning_tokens"], 50)  # not overwritten
        self.assertGreater(usage["completion_tokens"], 0)

    def test_sets_is_estimated_true_when_backfill_happens(self):
        usage = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0, "model": "gpt-4"}
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertTrue(usage.get("is_estimated"))

    def test_does_not_set_is_estimated_when_nothing_changed(self):
        usage = {
            "prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30,
            "model": "gpt-4",
        }
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertFalse(changed)
        # is_estimated should not be set (absent or falsy) when nothing was backfilled
        self.assertFalse(usage.get("is_estimated"))

    def test_sets_is_estimated_when_only_completion_is_backfilled(self):
        usage = {
            "prompt_tokens": 42, "completion_tokens": 0,
            "total_tokens": 42, "model": "gpt-4",
        }
        body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})
        response = "Hi there!"

        changed = backfill_zero_token_counts(usage, body, response)

        self.assertTrue(changed)
        self.assertTrue(usage.get("is_estimated"))


class CostSavedTests(unittest.TestCase):
    def test_estimate_cost_saved_with_separate_input_output_rates(self):
        saved = _estimate_cost_saved(
            prompt_tokens=100,
            completion_tokens=1000,
            primary_rate=ModelCostRates(input_rate=10, output_rate=30),
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )
        self.assertAlmostEqual(saved, 0.0279, places=6)

    def test_estimate_cost_saved_returns_none_for_missing_rate(self):
        saved = _estimate_cost_saved(
            prompt_tokens=100,
            completion_tokens=1000,
            primary_rate=None,
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )
        self.assertIsNone(saved)

    def test_estimate_cost_saved_handles_zero_tokens(self):
        saved = _estimate_cost_saved(
            prompt_tokens=0,
            completion_tokens=0,
            primary_rate=ModelCostRates(input_rate=10, output_rate=30),
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )
        self.assertEqual(saved, 0.0)

    def test_estimate_cost_saved_handles_invalid_rate(self):
        saved = _estimate_cost_saved(
            prompt_tokens=100,
            completion_tokens=50,
            primary_rate={"input_rate": "not-a-number", "output_rate": 30},
            fallback_rate=ModelCostRates(input_rate=1, output_rate=3),
        )
        self.assertIsNone(saved)

    def test_build_model_cost_rate_registry_from_provider_models(self):
        providers_config = {
            "primary": {
                "models": {
                    "primary-model": {
                        "input_rate": 10,
                        "output_rate": 30,
                    },
                },
            },
            "fallback": {
                "models": [
                    {
                        "model": "fallback-model",
                        "pricing": {
                            "input_rate": 1,
                            "output_rate": 3,
                        },
                    },
                ],
            },
        }

        registry = build_model_cost_rate_registry(providers_config)

        self.assertEqual(
            registry[("primary", "primary-model")],
            ModelCostRates(input_rate=10.0, output_rate=30.0),
        )
        self.assertEqual(
            registry[("fallback", "fallback-model")],
            ModelCostRates(input_rate=1.0, output_rate=3.0),
        )

    def test_extract_prefers_upstream_cost_saved_field(self):
        payload = {
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                "cost": 0.02, "cost_saved": 0.007,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["cost_saved"], 0.007)

    def test_extract_accepts_saved_cost_alias(self):
        payload = {
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                "cost": 0.02, "saved_cost": 0.004,
                "prompt_tokens_details": {"cached_tokens": 200},
            },
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["cost_saved"], 0.004)

    def test_extract_falls_back_to_rate_based_estimate_when_no_upstream_field(self):
        registry = build_model_cost_rate_registry({
            "primary": {
                "models": {
                    "primary-model": {
                        "input_rate": 10,
                        "output_rate": 30,
                    },
                },
            },
            "fallback": {
                "models": {
                    "fallback-model": {
                        "input_rate": 1,
                        "output_rate": 3,
                    },
                },
            },
        })
        payload = {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 1000,
                "total_tokens": 1100,
            },
            "provider": "fallback",
            "model": "fallback-model",
        }

        result = extract_tokens_usage(
            payload,
            cost_rate_registry=registry,
            primary_provider="primary",
            primary_model="primary-model",
        )

        self.assertAlmostEqual(result["cost_saved"], 0.0279, places=6)

    def test_extract_returns_zero_when_no_rate_context(self):
        payload = {
            "usage": {
                "prompt_tokens": 1000, "completion_tokens": 500, "total_tokens": 1500,
                "cost": 0.015,
            },
        }

        result = extract_tokens_usage(payload)

        self.assertEqual(result["cost_saved"], 0)


if __name__ == "__main__":
    unittest.main()
