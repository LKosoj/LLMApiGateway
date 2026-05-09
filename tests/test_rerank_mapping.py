import unittest

from llm_gateway_core.api.v1.embeddings import map_rerank_payload
from llm_gateway_core.config.loader import OperationRoute


class RerankMappingTests(unittest.TestCase):
    def test_map_rerank_payload_uses_exact_text_1_and_text_2_fields(self):
        route = OperationRoute(
            provider="cohere",
            model="rerank-v3.5",
            target_path="/score",
            custom_body_params={
                "top_n": 3,
                "return_documents": True,
            },
        )

        payload = map_rerank_payload("What is the refund policy?", ["Doc A", "Doc B"], route)

        self.assertEqual(
            payload,
            {
                "model": "rerank-v3.5",
                "text_1": "What is the refund policy?",
                "text_2": ["Doc A", "Doc B"],
                "top_n": 3,
                "return_documents": True,
            },
        )
        self.assertNotIn("query", payload)
        self.assertNotIn("documents", payload)
        self.assertNotIn("query_text", payload)
        self.assertNotIn("pairs", payload)

    def test_map_rerank_payload_adds_only_allowlisted_custom_body_params(self):
        route = OperationRoute(
            provider="cohere",
            model="rerank-v3.5",
            target_path="/score",
            custom_body_params={
                "top_n": 5,
                "return_documents": False,
                "max_chunks_per_doc": 7,
                "query_text": "blocked",
                "documents": ["blocked"],
            },
        )

        payload = map_rerank_payload("query", ["Doc"], route)

        self.assertEqual(payload["model"], "rerank-v3.5")
        self.assertEqual(payload["text_1"], "query")
        self.assertEqual(payload["text_2"], ["Doc"])
        self.assertEqual(payload["top_n"], 5)
        self.assertFalse(payload["return_documents"])
        self.assertEqual(payload["max_chunks_per_doc"], 7)
        self.assertNotIn("query_text", payload)
        self.assertNotIn("documents", payload)

    def test_map_rerank_payload_uses_native_nvidia_shape_for_absolute_retrieval_endpoint(self):
        route = OperationRoute(
            provider="nvidia",
            model="nv-rerank-qa-mistral-4b:1",
            target_path="https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
            request_format="query_passages",
            custom_body_params={
                "max_chunks_per_doc": 4,
                "top_n": 2,
            },
        )

        payload = map_rerank_payload("query", ["Doc A", "Doc B"], route)

        self.assertEqual(
            payload,
            {
                "model": "nv-rerank-qa-mistral-4b:1",
                "query": {"text": "query"},
                "passages": [{"text": "Doc A"}, {"text": "Doc B"}],
                "max_chunks_per_doc": 4,
            },
        )
        self.assertNotIn("text_1", payload)
        self.assertNotIn("text_2", payload)
        self.assertNotIn("top_n", payload)

    def test_map_rerank_payload_uses_query_texts_shape(self):
        route = OperationRoute(
            provider="custom",
            model="Qwen/Qwen3-Reranker-0.6B",
            target_path="/rerank",
            request_format="query_texts",
            custom_body_params={
                "top_n": 2,
                "return_documents": True,
            },
        )

        payload = map_rerank_payload("query", ["Doc A", "Doc B"], route)

        self.assertEqual(
            payload,
            {
                "model": "Qwen/Qwen3-Reranker-0.6B",
                "query": "query",
                "texts": ["Doc A", "Doc B"],
            },
        )
        self.assertNotIn("text_1", payload)
        self.assertNotIn("text_2", payload)
        self.assertNotIn("top_n", payload)


if __name__ == "__main__":
    unittest.main()
