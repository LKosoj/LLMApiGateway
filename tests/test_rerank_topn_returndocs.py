import unittest

from llm_gateway_core.api.v1.embeddings import add_return_documents, apply_top_n


class RerankTopNReturnDocumentsTests(unittest.TestCase):
    def test_apply_top_n_slices_results_when_value_is_provided(self):
        results = [
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.8},
            {"index": 2, "score": 0.7},
        ]

        self.assertEqual(
            apply_top_n(results, 2),
            [
                {"index": 0, "score": 0.9},
                {"index": 1, "score": 0.8},
            ],
        )

    def test_apply_top_n_returns_all_results_when_value_is_none(self):
        results = [
            {"index": 0, "score": 0.9},
            {"index": 1, "score": 0.8},
        ]

        self.assertEqual(apply_top_n(results, None), results)

    def test_add_return_documents_adds_original_documents_using_index_or_position(self):
        results = [
            {"index": 1, "score": 0.9},
            {"score": 0.8},
        ]

        self.assertEqual(
            add_return_documents(results, ["Doc A", "Doc B"], True),
            [
                {"index": 1, "score": 0.9, "document": "Doc B"},
                {"score": 0.8, "document": "Doc B"},
            ],
        )

    def test_add_return_documents_strips_document_field_when_disabled(self):
        results = [
            {"index": 0, "score": 0.9, "document": "downstream-doc"},
        ]

        self.assertEqual(
            add_return_documents(results, ["Doc A"], False),
            [
                {"index": 0, "score": 0.9},
            ],
        )


if __name__ == "__main__":
    unittest.main()
