import unittest
from unittest import mock

from llm_gateway_core.services.capability_guard import (
    DEFAULT_OUTPUT_RESERVE_TOKENS,
    OUTPUT_RESERVE_CAP_TOKENS,
    REASON_CONTEXT_WINDOW_EXCEEDED,
    REASON_TOOLS_UNSUPPORTED,
    REASON_VISION_UNSUPPORTED,
    estimate_request_tokens_for_routing,
    filter_capable_candidates,
    request_needs_tools,
    request_needs_vision,
)


class RequestNeedsVisionTests(unittest.TestCase):
    def test_needs_vision_detects_image_url_block(self):
        request_body = {
            "model": "gateway/model",
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": "Describe this image"},
                        {"type": "image_url", "image_url": {"url": "https://example.com/a.png"}},
                    ],
                }
            ],
        }
        self.assertTrue(request_needs_vision(request_body))

    def test_needs_vision_false_without_image_blocks(self):
        request_body = {
            "model": "gateway/model",
            "messages": [
                {"role": "user", "content": "Plain text message"},
                {
                    "role": "user",
                    "content": [{"type": "text", "text": "Still no images"}],
                },
            ],
        }
        self.assertFalse(request_needs_vision(request_body))


class RequestNeedsToolsTests(unittest.TestCase):
    def test_needs_tools_false_for_empty_tools_list(self):
        self.assertFalse(request_needs_tools({"tools": []}))
        self.assertFalse(request_needs_tools({}))

    def test_needs_tools_true_for_non_empty_tools_list(self):
        self.assertTrue(request_needs_tools({"tools": [{"type": "function", "function": {"name": "f"}}]}))


class OutputReserveTests(unittest.TestCase):
    def test_output_reserve_defaults_when_max_tokens_missing(self):
        without_max_tokens = estimate_request_tokens_for_routing({"messages": []})
        with_zero_max_tokens = estimate_request_tokens_for_routing({"messages": [], "max_tokens": 0})
        with_invalid_max_tokens = estimate_request_tokens_for_routing({"messages": [], "max_tokens": "oops"})

        # All three fall back to the same default output reserve, so their
        # estimates only differ by whatever the JSON encoding of the extra
        # key costs the underlying prompt estimator (which is negligible /
        # identical across these near-empty payloads).
        self.assertEqual(without_max_tokens, with_zero_max_tokens)
        self.assertGreaterEqual(without_max_tokens, DEFAULT_OUTPUT_RESERVE_TOKENS)
        self.assertGreaterEqual(with_invalid_max_tokens, DEFAULT_OUTPUT_RESERVE_TOKENS)

    def test_output_reserve_capped_at_2000(self):
        under_cap = estimate_request_tokens_for_routing({"messages": [], "max_tokens": 500})
        at_cap = estimate_request_tokens_for_routing({"messages": [], "max_tokens": OUTPUT_RESERVE_CAP_TOKENS})
        over_cap = estimate_request_tokens_for_routing({"messages": [], "max_tokens": 100000})

        self.assertEqual(at_cap, over_cap)
        self.assertLess(under_cap, at_cap)


class FilterCapableCandidatesTests(unittest.TestCase):
    def setUp(self):
        self.request_body = {
            "model": "gateway/model",
            "messages": [{"role": "user", "content": "hello"}],
        }

    def test_filter_keeps_candidates_without_metadata(self):
        candidates = [
            {"provider": "a", "model": "model-a"},
            {"provider": "b", "model": "model-b"},
        ]
        kept, rejections = filter_capable_candidates(
            candidates,
            self.request_body,
            gateway_model="gateway/model",
        )
        self.assertEqual(kept, candidates)
        self.assertEqual(rejections, [])

    def test_filter_drops_vision_unsupported_candidate(self):
        vision_request = {
            "messages": [
                {
                    "role": "user",
                    "content": [{"type": "image_url", "image_url": {"url": "https://example.com/a.png"}}],
                }
            ]
        }
        candidates = [
            {"provider": "a", "model": "model-a", "supports_vision": False},
            {"provider": "b", "model": "model-b"},
        ]
        kept, rejections = filter_capable_candidates(
            candidates,
            vision_request,
            gateway_model="gateway/model",
        )
        self.assertEqual(kept, [candidates[1]])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].provider, "a")
        self.assertEqual(rejections[0].model, "model-a")
        self.assertEqual(rejections[0].reason, REASON_VISION_UNSUPPORTED)

    def test_filter_drops_tools_unsupported_candidate(self):
        tools_request = {
            "messages": [{"role": "user", "content": "hi"}],
            "tools": [{"type": "function", "function": {"name": "f"}}],
        }
        candidates = [
            {"provider": "a", "model": "model-a", "supports_tools": False},
            {"provider": "b", "model": "model-b"},
        ]
        kept, rejections = filter_capable_candidates(
            candidates,
            tools_request,
            gateway_model="gateway/model",
        )
        self.assertEqual(kept, [candidates[1]])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].reason, REASON_TOOLS_UNSUPPORTED)

    def test_filter_drops_context_window_exceeded_candidate(self):
        candidates = [
            {"provider": "a", "model": "model-a", "context_window": 1},
            {"provider": "b", "model": "model-b"},
        ]
        kept, rejections = filter_capable_candidates(
            candidates,
            self.request_body,
            gateway_model="gateway/model",
        )
        self.assertEqual(kept, [candidates[1]])
        self.assertEqual(len(rejections), 1)
        self.assertEqual(rejections[0].provider, "a")
        self.assertEqual(rejections[0].reason, REASON_CONTEXT_WINDOW_EXCEEDED)

    def test_filter_token_estimate_computed_once_and_only_when_context_window_present(self):
        candidates_without_context_window = [
            {"provider": "a", "model": "model-a"},
            {"provider": "b", "model": "model-b"},
        ]
        with mock.patch(
            "llm_gateway_core.services.capability_guard.estimate_prompt_tokens",
            wraps=lambda *args, **kwargs: 10,
        ) as estimate_mock:
            filter_capable_candidates(
                candidates_without_context_window,
                self.request_body,
                gateway_model="gateway/model",
            )
            estimate_mock.assert_not_called()

        candidates_with_context_window = [
            {"provider": "a", "model": "model-a", "context_window": 100000},
            {"provider": "b", "model": "model-b", "context_window": 200000},
        ]
        with mock.patch(
            "llm_gateway_core.services.capability_guard.estimate_prompt_tokens",
            wraps=lambda *args, **kwargs: 10,
        ) as estimate_mock:
            kept, rejections = filter_capable_candidates(
                candidates_with_context_window,
                self.request_body,
                gateway_model="gateway/model",
            )
            estimate_mock.assert_called_once()
        self.assertEqual(kept, candidates_with_context_window)
        self.assertEqual(rejections, [])


if __name__ == "__main__":
    unittest.main()
