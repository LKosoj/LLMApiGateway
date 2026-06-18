import unittest

from llm_gateway_core.services.payload_transform import apply_payload_transforms


class PayloadTransformTests(unittest.TestCase):
    def test_apply_payload_transforms_defaults_overrides_and_filters(self):
        payload = {"model": "provider-model", "temperature": 0.2, "metadata": {"keep": True}, "seed": 7}

        transformed = apply_payload_transforms(
            payload,
            {
                "defaults": {"temperature": 0.8, "top_p": 0.9},
                "overrides": {"parallel_tool_calls": False},
                "filters": ["seed"],
            },
        )

        self.assertEqual(
            transformed,
            {
                "model": "provider-model",
                "temperature": 0.2,
                "metadata": {"keep": True},
                "top_p": 0.9,
                "parallel_tool_calls": False,
            },
        )
        self.assertIn("seed", payload)

    def test_payload_transforms_reject_reserved_fields(self):
        with self.assertRaisesRegex(ValueError, "reserved"):
            apply_payload_transforms(
                {"model": "provider-model"},
                {"overrides": {"model": "other-model"}},
            )

    def test_payload_transforms_reject_nested_paths(self):
        with self.assertRaisesRegex(ValueError, "top-level"):
            apply_payload_transforms(
                {"model": "provider-model"},
                {"filters": ["metadata.user"]},
            )


if __name__ == "__main__":
    unittest.main()
