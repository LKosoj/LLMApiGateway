import unittest

from llm_gateway_core.utils.log_redaction import redact_payload_for_log


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


if __name__ == "__main__":
    unittest.main()
