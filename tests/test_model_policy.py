import unittest

from llm_gateway_core.services.model_policy import resolve_model_name


class ModelPolicyTests(unittest.TestCase):
    def test_resolve_exact_alias(self):
        resolution = resolve_model_name(
            "public/fast",
            {"aliases": {"public/fast": "llmgateway/fast"}},
        )

        self.assertEqual(resolution.effective_model, "llmgateway/fast")
        self.assertEqual(resolution.matched_rule, "alias")

    def test_resolve_prefix_target_prefix(self):
        resolution = resolve_model_name(
            "codex/gpt-5",
            {"prefixes": [{"prefix": "codex/", "target_prefix": "llmgateway/codex/"}]},
        )

        self.assertEqual(resolution.effective_model, "llmgateway/codex/gpt-5")
        self.assertEqual(resolution.matched_rule, "prefix")

    def test_excluded_model_fails_closed(self):
        with self.assertRaisesRegex(ValueError, "excluded"):
            resolve_model_name("blocked/test", {"excluded_models": ["blocked/*"]})


if __name__ == "__main__":
    unittest.main()
