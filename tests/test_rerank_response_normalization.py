import unittest

from llm_gateway_core.api.v1.embeddings import extract_index, extract_score, normalize_rerank_response


class RerankResponseNormalizationTests(unittest.TestCase):
    def test_extract_score_supports_score_relevance_and_similarity_formats(self):
        cases = (
            ({"score": "0.91"}, 0.91),
            ({"relevance_score": 0.82}, 0.82),
            ({"relevance": "0.73"}, 0.73),
            ({"similarity": 0.64}, 0.64),
        )

        for payload, expected_score in cases:
            with self.subTest(payload=payload):
                self.assertEqual(extract_score(payload), expected_score)

    def test_extract_index_uses_explicit_index_or_falls_back_to_original_position(self):
        self.assertEqual(extract_index({"index": "4"}, 0), 4)
        self.assertEqual(extract_index({"document_index": 7}, 0), 7)
        self.assertEqual(extract_index({}, 3), 3)

    def test_normalize_rerank_response_supports_cohere_jina_nvidia_and_custom_shapes(self):
        cohere_response = {
            "results": [
                {
                    "index": 1,
                    "relevance_score": 0.98,
                    "document": {"text": "Doc B"},
                }
            ]
        }
        jina_response = {
            "data": [
                {
                    "index": "0",
                    "similarity": "0.77",
                    "document": "Doc A",
                }
            ]
        }
        nvidia_response = {
            "rankings": [
                {
                    "index": 2,
                    "logit": 4.3359375,
                }
            ]
        }
        custom_response = {
            "results": [
                {
                    "score": 0.55,
                    "content": "Doc C",
                }
            ]
        }
        scores_response = {"scores": [0.12, "0.98", 0.42]}

        self.assertEqual(
            normalize_rerank_response(cohere_response),
            {"data": [{"index": 1, "score": 0.98, "document": "Doc B"}]},
        )
        self.assertEqual(
            normalize_rerank_response(jina_response),
            {"data": [{"index": 0, "score": 0.77, "document": "Doc A"}]},
        )
        self.assertEqual(
            normalize_rerank_response(nvidia_response, "rankings_logit"),
            {"data": [{"index": 2, "score": 4.3359375}]},
        )
        self.assertEqual(
            normalize_rerank_response(custom_response),
            {"data": [{"index": 0, "score": 0.55, "document": "Doc C"}]},
        )
        self.assertEqual(
            normalize_rerank_response(scores_response, "scores"),
            {
                "data": [
                    {"index": 1, "score": 0.98},
                    {"index": 2, "score": 0.42},
                    {"index": 0, "score": 0.12},
                ]
            },
        )

    def test_extract_score_supports_rankings_logit_response_format(self):
        self.assertEqual(extract_score({"logit": "4.33"}, "rankings_logit"), 4.33)


if __name__ == "__main__":
    unittest.main()
