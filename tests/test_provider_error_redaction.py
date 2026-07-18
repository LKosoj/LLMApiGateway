import unittest

from llm_gateway_core.utils.provider_error_redaction import (
    MAX_PROVIDER_ERROR_TEXT_LENGTH,
    redact_provider_error_text,
)


class ProviderErrorRedactionTests(unittest.TestCase):
    def test_empty_and_none_input_returns_generic_message(self):
        self.assertEqual(redact_provider_error_text(None), "Provider error")
        self.assertEqual(redact_provider_error_text(""), "Provider error")
        self.assertEqual(redact_provider_error_text("   "), "Provider error")

    def test_clean_text_is_unchanged_except_whitespace_collapse(self):
        clean = "Model unavailable right now"
        self.assertEqual(redact_provider_error_text(clean), clean)

        messy_whitespace = "Model   unavailable\nright  now"
        self.assertEqual(
            redact_provider_error_text(messy_whitespace),
            "Model unavailable right now",
        )

    def test_bearer_token_is_redacted(self):
        text = "Upstream rejected the request: Authorization header Bearer abcDEF123456xyz was invalid."
        redacted = redact_provider_error_text(text)

        self.assertNotIn("abcDEF123456xyz", redacted)
        self.assertIn("Bearer [redacted]", redacted)

    def test_bearer_token_is_redacted_case_insensitively(self):
        text = "bearer sometoken123 rejected"
        redacted = redact_provider_error_text(text)

        self.assertNotIn("sometoken123", redacted)
        self.assertIn("[redacted]", redacted)

    def test_key_value_pair_keeps_name_and_redacts_value(self):
        text = 'Rejected credentials: api_key="abc123secretvalue" is invalid'
        redacted = redact_provider_error_text(text)

        self.assertNotIn("abc123secretvalue", redacted)
        self.assertIn("api_key", redacted)
        self.assertIn("[redacted]", redacted)

    def test_authorization_basic_scheme_value_is_fully_redacted(self):
        text = "Rejected: authorization: Basic dXNlcjpwYXNzd29yZA=="
        redacted = redact_provider_error_text(text)

        self.assertNotIn("dXNlcjpwYXNzd29yZA==", redacted)
        self.assertNotIn("Basic", redacted)
        self.assertIn("authorization:[redacted]", redacted)

    def test_api_key_token_scheme_value_is_fully_redacted(self):
        text = "Rejected credentials: api-key: Token xyz"
        redacted = redact_provider_error_text(text)

        self.assertNotIn("xyz", redacted)
        self.assertNotIn("Token", redacted)
        self.assertIn("api-key:[redacted]", redacted)

    def test_ordinary_basic_and_token_phrases_are_not_redacted(self):
        self.assertEqual(redact_provider_error_text("Basic plan required"), "Basic plan required")
        self.assertEqual(redact_provider_error_text("token limit exceeded"), "token limit exceeded")

    def test_capitalized_status_words_after_scheme_are_not_redacted(self):
        for text in (
            "Token REQUIRED to access this resource",
            "ApiKey MISSING from request headers",
            "Token INVALID or expired",
        ):
            with self.subTest(text=text):
                self.assertEqual(redact_provider_error_text(text), text)

    def test_standalone_basic_scheme_with_base64_token_is_redacted(self):
        text = "Basic dXNlcjpwYXNzd29yZA=="
        redacted = redact_provider_error_text(text)

        self.assertNotIn("dXNlcjpwYXNzd29yZA==", redacted)
        self.assertEqual(redacted, "Basic [redacted]")

    def test_key_value_pair_matches_token_secret_and_authorization_names(self):
        for name, value in (
            ("access_token", "xyz789supersecret"),
            ("token", "plaintoken000111"),
            ("secret", "shh-do-not-tell"),
            ("authorization", "raw-credential-blob"),
        ):
            with self.subTest(name=name):
                text = f"{name}={value} was rejected upstream"
                redacted = redact_provider_error_text(text)
                self.assertNotIn(value, redacted)
                self.assertIn(name, redacted)

    def test_prefixed_provider_key_is_redacted(self):
        for value in (
            "sk-proj-abcdef1234567890",
            "gsk_abcdef1234567890",
            "lgk_abcdef1234567890",
            "AIzaSyAbCdEf1234567890",
        ):
            with self.subTest(value=value):
                text = f"Upstream rejected key {value} for this request"
                redacted = redact_provider_error_text(text)
                self.assertNotIn(value, redacted)
                self.assertIn("[redacted-key]", redacted)

    def test_jwt_shaped_token_is_redacted(self):
        jwt = (
            "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9."
            "eyJzdWIiOiIxMjM0NTY3ODkwIiwibmFtZSI6IkpvaG4iLCJhZG1pbiI6dHJ1ZX0."
            "SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV_adQssw5c"
        )
        text = f"Upstream session identifier {jwt} has expired"
        redacted = redact_provider_error_text(text)

        self.assertNotIn(jwt, redacted)
        self.assertIn("[redacted-token]", redacted)

    def test_url_is_redacted(self):
        text = "Failed to fetch https://upstream.example.com/v1/secret-path?token=abc"
        redacted = redact_provider_error_text(text)

        self.assertNotIn("https://upstream.example.com", redacted)
        self.assertIn("[redacted-url]", redacted)

    def test_long_text_is_truncated_with_ellipsis(self):
        text = "Upstream error detail. " * 50
        redacted = redact_provider_error_text(text)

        self.assertLessEqual(len(redacted), MAX_PROVIDER_ERROR_TEXT_LENGTH)
        self.assertTrue(redacted.endswith("..."))

    def test_is_idempotent_for_already_clean_text(self):
        clean = "Provider rejected the request due to invalid parameters"
        once = redact_provider_error_text(clean)
        twice = redact_provider_error_text(once)

        self.assertEqual(once, twice)


if __name__ == "__main__":
    unittest.main()
