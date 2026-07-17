import json
import unittest

from llm_gateway_core.config.paths import resolve_db_dir
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.utils.usage_tracking import (
    FALLBACK_USAGE_SOURCE,
    _resolve_encoding,
    backfill_zero_token_counts,
)
from tests._async_compat import run_async


class UsageTrackingEncodingFallbackTests(unittest.TestCase):
    def tearDown(self):
        _resolve_encoding.cache_clear()

    def test_resolve_encoding_marks_unknown_model_as_fallback(self):
        _resolve_encoding.cache_clear()

        with self.assertLogs("llm_gateway_core.utils.usage_tracking", level="WARNING") as logs:
            estimator = _resolve_encoding("unknown-model")

        self.assertIsNotNone(estimator)
        self.assertTrue(estimator.is_fallback)
        self.assertIn("model_hint='unknown-model'", "\n".join(logs.output))

    def test_resolve_encoding_marks_missing_model_as_fallback(self):
        _resolve_encoding.cache_clear()

        with self.assertLogs("llm_gateway_core.utils.usage_tracking", level="WARNING") as logs:
            estimator = _resolve_encoding(None)

        self.assertIsNotNone(estimator)
        self.assertTrue(estimator.is_fallback)
        self.assertIn("model_hint=None", "\n".join(logs.output))

    def test_fallback_estimated_usage_is_persisted_with_source(self):
        db_path = resolve_db_dir() / "test_usage_source_estimate_fallback.sqlite"

        try:
            usage = {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "model": "unknown-model",
            }
            request_body = json.dumps({"messages": [{"role": "user", "content": "Hello"}]})

            changed = backfill_zero_token_counts(usage, request_body, "Hi there!")

            self.assertTrue(changed)
            self.assertTrue(usage["is_estimated"])
            self.assertEqual(usage["usage_source"], FALLBACK_USAGE_SOURCE)

            db = TokensUsageDB(db_filename=db_path.name)
            db.insert_usage(usage)

            latest_record = run_async(db.get_latest_usage_records(limit=1, offset=0))[0]

            self.assertEqual(latest_record["usage_source"], FALLBACK_USAGE_SOURCE)
        finally:
            db_path.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main()
