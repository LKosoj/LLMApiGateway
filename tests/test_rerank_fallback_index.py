import unittest

from llm_gateway_core.api.v1.embeddings import add_return_documents, apply_top_n, normalize_rerank_response


class RerankFallbackIndexTests(unittest.TestCase):
    def test_return_documents_recovers_original_index_from_downstream_document_when_index_missing(self):
        downstream_response = {
            "results": [
                {"relevance_score": 0.98, "document": {"text": "Doc C"}},
                {"relevance_score": 0.42, "document": {"text": "Doc A"}},
            ]
        }
        original_documents = ["Doc A", "Doc B", "Doc C"]

        normalized = normalize_rerank_response(downstream_response, include_index_metadata=True)
        top_results = apply_top_n(normalized["data"], 1)
        results = add_return_documents(top_results, original_documents, True)

        self.assertEqual(
            results,
            [
                {
                    "index": 2,
                    "score": 0.98,
                    "document": "Doc C",
                }
            ],
        )

    def test_return_documents_omits_document_when_missing_index_cannot_be_recovered(self):
        downstream_response = {"results": [{"relevance_score": 0.98}]}
        original_documents = ["Doc A", "Doc B", "Doc C"]

        normalized = normalize_rerank_response(downstream_response, include_index_metadata=True)
        results = add_return_documents(normalized["data"], original_documents, True)

        self.assertEqual(results, [{"index": 0, "score": 0.98}])


if __name__ == "__main__":
    unittest.main()
