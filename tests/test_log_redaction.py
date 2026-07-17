import importlib
import os
import tempfile
import unittest
from unittest.mock import patch

from llm_gateway_core.middleware.chat_logging import write_log
from llm_gateway_core.utils.log_redaction import (
    redact_headers_for_log,
    redact_payload_for_log,
    safe_exception_type_name,
    sanitize_exception_type_name,
)


class LogRedactionTests(unittest.TestCase):
    def test_exception_type_names_use_a_bounded_ascii_identifier(self):
        unsafe_type = type("Unsafe\napi-secret", (RuntimeError,), {})

        self.assertEqual(safe_exception_type_name(RuntimeError()), "RuntimeError")
        self.assertEqual(safe_exception_type_name(unsafe_type()), "BaseException")
        self.assertEqual(sanitize_exception_type_name("X" * 129), "BaseException")

    def test_redaction_helpers_remove_sensitive_headers_and_messages(self):
        headers = {
            "Authorization": "Bearer top-secret",
            "Cookie": "session=abc",
            "X-Api-Key": "provider-secret",
            "X-Trace-Id": "trace-123",
        }
        payload = {
            "model": "demo-model",
            "messages": [{"role": "user", "content": "very secret prompt"}],
            "nested": {"messages": [{"role": "system", "content": "hidden"}]},
        }

        self.assertEqual(redact_headers_for_log(headers)["Authorization"], "<REDACTED>")
        self.assertEqual(redact_headers_for_log(headers)["Cookie"], "<REDACTED>")
        self.assertEqual(redact_headers_for_log(headers)["X-Api-Key"], "<REDACTED>")
        self.assertEqual(redact_headers_for_log(headers)["X-Trace-Id"], "trace-123")
        self.assertNotIn("messages", redact_payload_for_log(payload))
        self.assertNotIn("messages", redact_payload_for_log(payload)["nested"])

    def test_redaction_helpers_can_keep_messages_when_requested(self):
        payload = {
            "model": "demo-model",
            "messages": [{"role": "user", "content": "very secret prompt"}],
            "nested": {"messages": [{"role": "system", "content": "hidden"}]},
        }

        redacted_payload = redact_payload_for_log(payload, include_messages=True)
        self.assertIn("messages", redacted_payload)
        self.assertIn("messages", redacted_payload["nested"])
        self.assertEqual(redacted_payload["messages"][0]["content"], "very secret prompt")

    def test_write_log_omits_tokens_and_prompt_messages_from_file(self):
        headers = {
            "Authorization": "Bearer top-secret",
            "Cookie": "session=abc",
            "X-Api-Key": "provider-secret",
            "X-Trace-Id": "trace-123",
        }
        request_body = """
        {
          model: "demo-model",
          messages: [{ role: "user", content: "very secret prompt" }],
          stream: false
        }
        """
        tokens_usage = {
            "prompt_tokens": 1,
            "completion_tokens": 2,
            "total_tokens": 3,
            "reasoning_tokens": 0,
            "cached_tokens": 0,
            "cost": 0,
            "model": "demo-model",
            "provider": "demo-provider",
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            configured_logs = os.path.join(temp_dir, "configured logs")
            unrelated_cwd = os.path.join(temp_dir, "unrelated cwd")
            os.mkdir(unrelated_cwd)
            current_dir = os.getcwd()
            try:
                os.chdir(unrelated_cwd)
                with patch.dict(
                    os.environ,
                    {"LLMGATEWAY_LOG_DIR": configured_logs},
                    clear=False,
                ):
                    write_log(headers, request_body, "model response", tokens_usage)
                log_files = [
                    name
                    for name in os.listdir(configured_logs)
                    if name.endswith(".txt")
                ]
                self.assertEqual(len(log_files), 1)
                with open(
                    os.path.join(configured_logs, log_files[0]),
                    "r",
                    encoding="utf-8",
                ) as log_file:
                    content = log_file.read()
            finally:
                os.chdir(current_dir)

            self.assertFalse(os.path.exists(os.path.join(unrelated_cwd, "logs")))

        self.assertNotIn("Bearer top-secret", content)
        self.assertNotIn("session=abc", content)
        self.assertNotIn("provider-secret", content)
        self.assertNotIn("very secret prompt", content)
        self.assertNotIn('"messages"', content)
        self.assertIn("<REDACTED>", content)

    def test_log_chat_enabled_defaults_to_false_when_env_is_unset(self):
        import llm_gateway_core.config.settings as settings_module

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_CHAT_ENABLED", None)
            with patch("dotenv.load_dotenv", return_value=False):
                reloaded_settings = importlib.reload(settings_module)
            self.assertFalse(reloaded_settings.settings.log_chat_messages)

        importlib.reload(settings_module)

    def test_log_fallback_full_messages_defaults_to_false_when_env_is_unset(self):
        import llm_gateway_core.config.settings as settings_module

        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("LOG_FALLBACK_FULL_MESSAGES", None)
            with patch("dotenv.load_dotenv", return_value=False):
                reloaded_settings = importlib.reload(settings_module)
            self.assertFalse(reloaded_settings.settings.log_fallback_full_messages)

        importlib.reload(settings_module)


if __name__ == "__main__":
    unittest.main()
