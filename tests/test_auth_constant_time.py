"""Verify that API-key comparison uses hmac.compare_digest (constant-time)."""
import unittest

from llm_gateway_core.middleware.auth import _tokens_match


class ConstantTimeTokenMatchTests(unittest.TestCase):
    def test_matching_tokens(self):
        self.assertTrue(_tokens_match("secret-key-123", "secret-key-123"))

    def test_mismatched_tokens(self):
        self.assertFalse(_tokens_match("secret-key-123", "secret-key-456"))

    def test_different_lengths(self):
        self.assertFalse(_tokens_match("abc", "abcdef"))

    def test_none_provided(self):
        self.assertFalse(_tokens_match(None, "expected"))

    def test_none_expected(self):
        self.assertFalse(_tokens_match("provided", None))

    def test_empty_provided(self):
        self.assertFalse(_tokens_match("", "expected"))

    def test_empty_expected(self):
        self.assertFalse(_tokens_match("provided", ""))

    def test_both_none(self):
        self.assertFalse(_tokens_match(None, None))
