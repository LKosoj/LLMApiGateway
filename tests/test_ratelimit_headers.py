import unittest

from llm_gateway_core.services.ratelimit_headers import (
    parse_ratelimit_headers,
    parse_reset_duration_seconds,
)


class ParseResetDurationSecondsTests(unittest.TestCase):
    def test_parse_reset_duration_seconds_handles_compound_and_plain_forms(self):
        self.assertAlmostEqual(parse_reset_duration_seconds("1m59.56s"), 119.56, places=6)
        self.assertAlmostEqual(parse_reset_duration_seconds("7.66s"), 7.66, places=6)
        self.assertEqual(parse_reset_duration_seconds("45"), 45.0)
        self.assertIsNone(parse_reset_duration_seconds("garbage"))

    def test_parse_reset_duration_seconds_handles_hours(self):
        self.assertAlmostEqual(parse_reset_duration_seconds("1h2m3s"), 3723.0, places=6)

    def test_parse_reset_duration_seconds_rejects_empty_input(self):
        self.assertIsNone(parse_reset_duration_seconds(""))
        self.assertIsNone(parse_reset_duration_seconds("   "))


class ParseRatelimitHeadersTests(unittest.TestCase):
    def test_groq_headers_produce_rpm_and_tpm_observations(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "42",
            "x-ratelimit-reset-requests": "1m59.56s",
            "x-ratelimit-limit-tokens": "20000",
            "x-ratelimit-remaining-tokens": "19000",
            "x-ratelimit-reset-tokens": "7.66s",
        }

        observations = parse_ratelimit_headers(
            "https://api.groq.com/openai/v1/chat/completions",
            headers,
            now_monotonic=1000.0,
            now_wall=2_000_000.0,
        )

        by_axis = {observation.axis: observation for observation in observations}
        self.assertEqual(set(by_axis), {"rpm", "tpm"})

        rpm = by_axis["rpm"]
        self.assertEqual(rpm.limit, 100)
        self.assertEqual(rpm.remaining, 42)
        self.assertAlmostEqual(rpm.reset_at_monotonic, 1000.0 + 119.56, places=6)
        self.assertEqual(rpm.source, "header")

        tpm = by_axis["tpm"]
        self.assertEqual(tpm.limit, 20000)
        self.assertEqual(tpm.remaining, 19000)
        self.assertAlmostEqual(tpm.reset_at_monotonic, 1000.0 + 7.66, places=6)

    def test_cerebras_headers_produce_rpd_and_tpm_observations_with_plain_seconds_reset(self):
        headers = {
            "x-ratelimit-limit-requests-day": "1000",
            "x-ratelimit-remaining-requests-day": "999",
            "x-ratelimit-reset-requests-day": "86399",
            "x-ratelimit-limit-tokens-minute": "60000",
            "x-ratelimit-remaining-tokens-minute": "59000",
            "x-ratelimit-reset-tokens-minute": "30",
        }

        observations = parse_ratelimit_headers(
            "https://api.cerebras.ai/v1/chat/completions",
            headers,
            now_monotonic=500.0,
            now_wall=1_000_000.0,
        )

        by_axis = {observation.axis: observation for observation in observations}
        self.assertEqual(set(by_axis), {"rpd", "tpm"})

        rpd = by_axis["rpd"]
        self.assertEqual(rpd.limit, 1000)
        self.assertEqual(rpd.remaining, 999)
        self.assertAlmostEqual(rpd.reset_at_monotonic, 500.0 + 86399.0, places=6)

        tpm = by_axis["tpm"]
        self.assertEqual(tpm.limit, 60000)
        self.assertEqual(tpm.remaining, 59000)
        self.assertAlmostEqual(tpm.reset_at_monotonic, 500.0 + 30.0, places=6)

    def test_openrouter_headers_produce_epoch_ms_reset(self):
        now_wall = 1_700_000_000.0
        now_monotonic = 12345.0
        reset_epoch_ms = int((now_wall + 60.0) * 1000)
        headers = {
            "x-ratelimit-limit": "200",
            "x-ratelimit-remaining": "199",
            "x-ratelimit-reset": str(reset_epoch_ms),
        }

        observations = parse_ratelimit_headers(
            "https://openrouter.ai/api/v1/chat/completions",
            headers,
            now_monotonic=now_monotonic,
            now_wall=now_wall,
        )

        self.assertEqual(len(observations), 1)
        rpd = observations[0]
        self.assertEqual(rpd.axis, "rpd")
        self.assertEqual(rpd.limit, 200)
        self.assertEqual(rpd.remaining, 199)
        self.assertAlmostEqual(rpd.reset_at_monotonic, now_monotonic + 60.0, places=2)

    def test_openrouter_implausible_epoch_reset_is_discarded(self):
        headers = {
            "x-ratelimit-limit": "200",
            "x-ratelimit-remaining": "199",
            # Seconds, not milliseconds -> below the 1e12 plausibility floor.
            "x-ratelimit-reset": "1700000000",
        }

        observations = parse_ratelimit_headers(
            "https://openrouter.ai/api/v1/chat/completions",
            headers,
            now_monotonic=100.0,
            now_wall=1_700_000_000.0,
        )

        self.assertEqual(len(observations), 1)
        self.assertIsNone(observations[0].reset_at_monotonic)

    def test_missing_remaining_still_records_observation_with_none(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-reset-requests": "1m0s",
        }

        observations = parse_ratelimit_headers(
            "https://api.groq.com/openai/v1/chat/completions",
            headers,
            now_monotonic=0.0,
            now_wall=0.0,
        )

        rpm = next(observation for observation in observations if observation.axis == "rpm")
        self.assertEqual(rpm.limit, 100)
        self.assertIsNone(rpm.remaining)

    def test_all_axis_fields_absent_yields_no_observation(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "42",
            "x-ratelimit-reset-requests": "1m0s",
            # No tokens headers at all.
        }

        observations = parse_ratelimit_headers(
            "https://api.groq.com/openai/v1/chat/completions",
            headers,
            now_monotonic=0.0,
            now_wall=0.0,
        )

        axes = {observation.axis for observation in observations}
        self.assertEqual(axes, {"rpm"})

    def test_negative_remaining_is_clamped_to_zero(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "-5",
        }

        observations = parse_ratelimit_headers(
            "https://api.groq.com/openai/v1/chat/completions",
            headers,
            now_monotonic=0.0,
            now_wall=0.0,
        )

        rpm = next(observation for observation in observations if observation.axis == "rpm")
        self.assertEqual(rpm.remaining, 0)

    def test_unknown_host_returns_empty_tuple(self):
        headers = {
            "x-ratelimit-limit-requests": "100",
            "x-ratelimit-remaining-requests": "42",
        }

        observations = parse_ratelimit_headers(
            "https://unknown-provider.example/v1/chat/completions",
            headers,
            now_monotonic=0.0,
            now_wall=0.0,
        )

        self.assertEqual(observations, ())

    def test_header_lookup_is_case_insensitive(self):
        headers = {
            "X-RateLimit-Limit-Requests": "100",
            "X-RATELIMIT-REMAINING-REQUESTS": "42",
        }

        observations = parse_ratelimit_headers(
            "https://api.groq.com/openai/v1/chat/completions",
            headers,
            now_monotonic=0.0,
            now_wall=0.0,
        )

        rpm = next(observation for observation in observations if observation.axis == "rpm")
        self.assertEqual(rpm.limit, 100)
        self.assertEqual(rpm.remaining, 42)


if __name__ == "__main__":
    unittest.main()
