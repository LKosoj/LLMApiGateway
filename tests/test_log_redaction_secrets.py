import unittest

from fastapi import FastAPI
from fastapi.testclient import TestClient

import main
from llm_gateway_core.middleware.request_logging import RequestLoggingASGIMiddleware
from llm_gateway_core.utils.log_redaction import (
    is_sensitive_payload_key,
    redact_exception_message,
    redact_payload_for_log,
)


class LogRedactionSecretsTests(unittest.TestCase):
    def test_redacts_top_level_secrets_and_keeps_messages_when_requested(self):
        payload = {
            "messages": [{"role": "user", "content": "hello"}],
            "api_key": "sk-top-level",
            "authorization": "Bearer sk-auth",
            "model": "demo-model",
        }

        redacted = redact_payload_for_log(payload, include_messages=True)

        self.assertEqual(redacted["api_key"], "***REDACTED***")
        self.assertEqual(redacted["authorization"], "***REDACTED***")
        self.assertEqual(redacted["messages"], [{"role": "user", "content": "hello"}])
        self.assertEqual(redacted["model"], "demo-model")

    def test_redacts_nested_dict_secrets(self):
        payload = {
            "nested": {
                "token": "nested-token",
                "password": "nested-password",
                "safe": "visible",
            }
        }

        redacted = redact_payload_for_log(payload)

        self.assertEqual(redacted["nested"]["token"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["password"], "***REDACTED***")
        self.assertEqual(redacted["nested"]["safe"], "visible")

    def test_redacts_secrets_inside_lists_and_mixed_case_keys(self):
        payload = {
            "items": [
                {"apikey": "sk-list"},
                {"X-Api-Key": "sk-header-style"},
                {"Bearer": "sk-bearer"},
                {"value": "not-secret"},
            ]
        }

        redacted = redact_payload_for_log(payload)

        self.assertEqual(redacted["items"][0]["apikey"], "***REDACTED***")
        self.assertEqual(redacted["items"][1]["X-Api-Key"], "***REDACTED***")
        self.assertEqual(redacted["items"][2]["Bearer"], "***REDACTED***")
        self.assertEqual(redacted["items"][3]["value"], "not-secret")

    def test_messages_are_still_removed_by_default(self):
        payload = {
            "messages": [{"role": "user", "content": "hidden prompt"}],
            "nested": {"messages": [{"role": "system", "content": "hidden"}]},
            "api_key": "sk-secret",
        }

        redacted = redact_payload_for_log(payload)

        self.assertNotIn("messages", redacted)
        self.assertNotIn("messages", redacted["nested"])
        self.assertEqual(redacted["api_key"], "***REDACTED***")

    def test_is_sensitive_payload_key_catches_expanded_secrets_and_preserves_token_counts(self):
        for secret_key in (
            "secret",
            "client_secret",
            "access_token",
            "refresh_token",
            "private_key",
            "x-auth-token",
            "custom_api_key",
            "session_cookie",
        ):
            with self.subTest(secret_key=secret_key):
                self.assertTrue(is_sensitive_payload_key(secret_key))

        for safe_key in (
            "max_tokens",
            "max_completion_tokens",
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "model",
            "temperature",
        ):
            with self.subTest(safe_key=safe_key):
                self.assertFalse(is_sensitive_payload_key(safe_key))

    def test_redact_exception_message_masks_secrets(self):
        exc = RuntimeError(
            "Upstream failed with Bearer sk-live-secret12345 and api_key=mysecret https://user:pass@host/v1"
        )
        redacted = redact_exception_message(exc)

        self.assertNotIn("sk-live-secret12345", redacted)
        self.assertNotIn("mysecret", redacted)
        self.assertNotIn("https://user:pass@host", redacted)
        self.assertIn("[redacted]", redacted)
        self.assertEqual(redact_exception_message(RuntimeError("")), "")

    def test_global_exception_handler_and_request_logging_redact_exception_logs(self):
        app = FastAPI()

        @app.get("/explode")
        async def explode():
            raise RuntimeError("Upstream rejected Bearer sk-secret-999999")

        app.add_exception_handler(Exception, main.global_exception_handler)
        app.add_middleware(RequestLoggingASGIMiddleware)

        with (
            self.assertLogs("main", level="ERROR") as main_logs,
            TestClient(app, raise_server_exceptions=False) as client,
        ):
            resp = client.get("/explode")

        self.assertEqual(resp.status_code, 500)
        self.assertTrue(main_logs.output)
        self.assertNotIn("sk-secret-999999", "\n".join(main_logs.output))


if __name__ == "__main__":
    unittest.main()
