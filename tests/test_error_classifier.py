import unittest

from llm_gateway_core.services.error_classifier import classify_error


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


if __name__ == "__main__":
    unittest.main()
