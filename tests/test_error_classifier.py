import unittest

from llm_gateway_core.api.v1.chat_model_behavior import ModelBehaviorFailureDetail
from llm_gateway_core.services.error_classifier import (
    classify_error,
    is_daily_quota_exhausted_error,
    parse_learned_limit_from_error,
)


class ErrorClassifierRegressionTests(unittest.TestCase):
    def test_5xx_status_patterns_use_bounded_regex(self):
        vectors = (
            ("status 500ms", "unknown"),
            ("500mb error", "unknown"),
            ("error 500 upstream", "http_500"),
            ("HTTP 500", "http_500"),
            ("500 internal server error", "http_500"),
        )

        for message, expected in vectors:
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)

    def test_classify_error_returns_behavior_class_verbatim(self):
        detail = ModelBehaviorFailureDetail(
            "Model returned an empty completion with no tool call.",
            behavior_class="empty_completion",
        )

        self.assertEqual(classify_error(detail), "empty_completion")


class DailyQuotaExhaustedErrorTests(unittest.TestCase):
    def test_daily_quota_exhausted_requires_both_day_and_quota_markers(self):
        self.assertTrue(
            is_daily_quota_exhausted_error("Daily quota exhausted for this model.")
        )
        self.assertTrue(
            is_daily_quota_exhausted_error(
                {"error": {"message": "You have used up your per-day allocation."}}
            )
        )
        # Day marker only, no quota/allocation/exhaustion marker.
        self.assertFalse(
            is_daily_quota_exhausted_error("Daily usage report is now available.")
        )
        # Quota marker only, no daily/per-day/today/24h marker.
        self.assertFalse(
            is_daily_quota_exhausted_error("Your quota allocation has been increased.")
        )
        self.assertFalse(is_daily_quota_exhausted_error("rate limit exceeded"))


class ParseLearnedLimitFromErrorTests(unittest.TestCase):
    def test_parse_learned_limit_from_error_ignores_ambiguous_axis(self):
        self.assertIsNone(
            parse_learned_limit_from_error("Your account limit: 500 has been reached.")
        )

    def test_parse_learned_limit_parses_comma_separated_value(self):
        result = parse_learned_limit_from_error("tokens per day limit: 10,000 exceeded")
        self.assertEqual(result, ("tpd", 10000))


if __name__ == "__main__":
    unittest.main()
