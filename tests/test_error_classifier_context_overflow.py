import unittest

from llm_gateway_core.services.error_classifier import _is_context_overflow_error, classify_error


class ContextOverflowClassifierRegressionTests(unittest.TestCase):
    def test_requested_tokens_requires_overflow_marker_or_numeric_value(self):
        vectors = (
            ("the requested tokens for messages API", False),
            ("requested 200000 tokens exceeds model maximum context", True),
            ("This model has a maximum context of 128k tokens", True),
        )

        for message, expected in vectors:
            with self.subTest(message=message):
                self.assertIs(_is_context_overflow_error(message), expected)

    def test_classify_error_uses_refined_context_overflow_detection(self):
        vectors = (
            ("the requested tokens for messages API", "unknown"),
            ("requested 200000 tokens exceeds model maximum context", "context_overflow"),
            ("This model has a maximum context of 128k tokens", "context_overflow"),
        )

        for message, expected in vectors:
            with self.subTest(message=message):
                self.assertEqual(classify_error(message), expected)


if __name__ == "__main__":
    unittest.main()
