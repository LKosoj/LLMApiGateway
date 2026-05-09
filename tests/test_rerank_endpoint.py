import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader


VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
    }
  },
  {
    "cohere": {
      "baseUrl": "https://cohere.example/v2",
      "apikey": "COHERE-KEY"
    }
  },
  {
    "nvidia": {
      "baseUrl": "https://integrate.api.nvidia.com/v1",
      "apikey": "NVIDIA-KEY"
    }
  }
]
""".strip()

VALID_FALLBACK_RULES_TEXT = """
[
  {
    "gateway_model_name": "chat-model",
    "fallback_models": [
      {
        "provider": "openai",
        "model": "gpt-4o-mini"
      }
    ],
    "rotate_models": false
  }
]
""".strip()

VALID_OPERATION_RULES_TEXT = """
{
  "embeddings": [],
  "rerank": [
    {
      "gateway_model_name": "gateway/rerank-v1",
      "routes": [
        {
          "provider": "cohere",
          "model": "rerank-v3.5",
          "custom_body_params": {
            "top_n": 2,
            "return_documents": true
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/nvidia-rerank-v1",
      "routes": [
        {
          "provider": "nvidia",
          "model": "nv-rerank-qa-mistral-4b:1",
          "target_path": "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
          "request_format": "query_passages",
          "response_format": "rankings_logit"
        }
      ]
    }
  ]
}
""".strip()


class _FakeCleanupTask:
    def cancel(self):
        return None

    def __await__(self):
        async def _done():
            return None

        return _done().__await__()


class _FakeDownstreamResponse:
    def __init__(self, payload, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class RerankEndpointTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "openai")
        self.fallback_provider_patcher.start()
        self.config_loader = ConfigLoader(
            providers_filename=str(self.providers_path),
            fallback_rules_filename=str(self.rules_path),
            operation_rules_filename=str(self.operation_rules_path),
        )
        self.config_loader.load_providers()
        self.config_loader.load_fallback_rules()
        self.config_loader.load_operation_rules()

    def tearDown(self):
        self.fallback_provider_patcher.stop()
        self.temp_dir.cleanup()

    @contextmanager
    def _client(self, downstream_response):
        fake_http_client = Mock()
        if isinstance(downstream_response, list):
            fake_http_client.post = AsyncMock(side_effect=downstream_response)
        else:
            fake_http_client.post = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))

            with TestClient(main.app) as client:
                yield client, fake_http_client

    def test_post_rerank_with_valid_body_accepts_request_and_uses_default_score_path(self):
        downstream_payload = {
            "id": "rerank-resp",
            "results": [{"index": 1, "relevance_score": 0.98, "document": {"text": "Doc B"}}],
            "meta": {"api_version": "2"},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "top_n": 5,
                    "return_documents": False,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [
                    {
                        "index": 1,
                        "score": 0.98,
                    }
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://cohere.example/v2/score")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["headers"],
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer COHERE-KEY",
            },
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "rerank-v3.5",
                "text_1": "What is the refund policy?",
                "text_2": ["Doc A", "Doc B"],
                "top_n": 2,
                "return_documents": True,
            },
        )

    def test_post_rerank_applies_top_n_and_reinserts_original_documents(self):
        downstream_payload = {
            "results": [
                {"index": 1, "relevance_score": 0.98},
                {"index": 0, "relevance_score": 0.42},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "top_n": 1,
                    "return_documents": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [
                    {
                        "index": 1,
                        "score": 0.98,
                        "document": "Doc B",
                    }
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()

    def test_post_rerank_uses_nvidia_native_request_shape_and_response_normalization(self):
        downstream_payload = {
            "rankings": [
                {"index": 2, "logit": 4.3359375},
                {"index": 1, "logit": -7.9921875},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/nvidia-rerank-v1",
                    "query": "What is the GPU memory bandwidth of H100 SXM?",
                    "documents": [
                        "Doc A",
                        "Doc B",
                        "Doc C",
                    ],
                    "top_n": 1,
                    "return_documents": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [
                    {
                        "index": 2,
                        "score": 4.3359375,
                        "document": "Doc C",
                    }
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(
            fake_http_client.post.await_args.args[0],
            "https://ai.api.nvidia.com/v1/retrieval/nvidia/reranking",
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "nv-rerank-qa-mistral-4b:1",
                "query": {"text": "What is the GPU memory bandwidth of H100 SXM?"},
                "passages": [
                    {"text": "Doc A"},
                    {"text": "Doc B"},
                    {"text": "Doc C"},
                ],
            },
        )

    def test_post_rerank_uses_query_texts_request_shape_and_scores_response(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["rerank"].append(
            {
                "gateway_model_name": "gateway/query-texts-rerank-v1",
                "routes": [
                    {
                        "provider": "cohere",
                        "model": "Qwen/Qwen3-Reranker-0.6B",
                        "target_path": "/rerank",
                        "request_format": "query_texts",
                        "response_format": "scores",
                        "response_output_format": "jina_results",
                    }
                ],
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")
        self.config_loader.load_operation_rules()

        downstream_payload = {"scores": [0.12, 0.98, 0.42]}

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/query-texts-rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B", "Doc C"],
                    "top_n": 1,
                    "return_documents": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "results": [
                    {
                        "index": 1,
                        "relevance_score": 0.98,
                        "document": "Doc B",
                    }
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://cohere.example/v2/rerank")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "Qwen/Qwen3-Reranker-0.6B",
                "query": "What is the refund policy?",
                "texts": ["Doc A", "Doc B", "Doc C"],
            },
        )

    def test_post_rerank_rejects_documents_that_are_not_list_of_strings(self):
        invalid_documents_cases = (
            "not-a-list",
            ["Doc A", 123],
        )

        for documents in invalid_documents_cases:
            with self.subTest(documents=documents):
                with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
                    response = client.post(
                        "/v1/rerank",
                        json={
                            "model": "gateway/rerank-v1",
                            "query": "What is the refund policy?",
                            "documents": documents,
                        },
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                self.assertEqual(response.status_code, 400)
                self.assertEqual(
                    response.json()["detail"],
                    "Missing or invalid 'documents' in request body; expected list[str]",
                )
                fake_http_client.post.assert_not_awaited()

    def test_post_rerank_without_query_returns_400(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={"model": "gateway/rerank-v1", "documents": ["Doc A"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'query' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_post_rerank_without_model_returns_400(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={"query": "What is the refund policy?", "documents": ["Doc A"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_post_rerank_with_unknown_model_returns_404(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "unknown-rerank-model",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(response.json()["detail"], "No rerank route configured for model 'unknown-rerank-model'.")
        fake_http_client.post.assert_not_awaited()

    def test_post_rerank_returns_502_for_invalid_downstream_response_shape(self):
        with self._client(_FakeDownstreamResponse({"meta": {"provider": "cohere"}})) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 502)
        self.assertEqual(
            response.json()["detail"],
            "Invalid downstream rerank response: Downstream rerank response must contain 'results' or 'data'.",
        )
        fake_http_client.post.assert_awaited_once()

    def test_post_rerank_preserves_non_retryable_downstream_4xx(self):
        for status_code in (400, 500):
            with self.subTest(status_code=status_code):
                with self._client(
                    _FakeDownstreamResponse(
                        {"error": {"message": f"rerank-downstream-{status_code}"}},
                        status_code=status_code,
                    )
                ) as (client, fake_http_client):
                    response = client.post(
                        "/v1/rerank",
                        json={
                            "model": "gateway/rerank-v1",
                            "query": "What is the refund policy?",
                            "documents": ["Doc A", "Doc B"],
                        },
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )

                self.assertEqual(response.status_code, 400 if status_code == 400 else 503)
                self.assertEqual(response.json()["detail"], f"rerank-downstream-{status_code}")
                fake_http_client.post.assert_awaited_once()

    def test_post_rerank_retries_same_route_when_operation_route_configures_retry(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["rerank"][0]["routes"][0]["retry_count"] = 1
        operation_rules["rerank"][0]["routes"][0]["retry_delay"] = 0
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        downstream_payload = {
            "results": [
                {"index": 1, "relevance_score": 0.98},
            ]
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "cohere-busy"}}, status_code=503),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        ) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "top_n": 1,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [
                    {
                        "index": 1,
                        "score": 0.98,
                    }
                ]
            },
        )
        self.assertEqual(fake_http_client.post.await_count, 2)

    def test_post_rerank_falls_back_to_next_route_after_downstream_failure(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["rerank"][0]["routes"].append(
            {
                "provider": "openai",
                "model": "fallback-rerank-v1",
                "target_path": "/rerank",
                "custom_body_params": {
                    "top_n": 1,
                },
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        downstream_payload = {
            "results": [
                {"index": 0, "relevance_score": 0.81},
            ]
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "primary-rerank-down"}}, status_code=503),
                _FakeDownstreamResponse(downstream_payload, status_code=200),
            ]
        ) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "top_n": 1,
                    "return_documents": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "data": [
                    {
                        "index": 0,
                        "score": 0.81,
                        "document": "Doc A",
                    }
                ]
            },
        )
        self.assertEqual(fake_http_client.post.await_count, 2)
        self.assertEqual(fake_http_client.post.await_args_list[0].args[0], "https://cohere.example/v2/score")
        self.assertEqual(fake_http_client.post.await_args_list[1].args[0], "https://openai.example/v1/rerank")
        self.assertEqual(
            fake_http_client.post.await_args_list[1].kwargs["json"],
            {
                "model": "fallback-rerank-v1",
                "text_1": "What is the refund policy?",
                "text_2": ["Doc A", "Doc B"],
                "top_n": 1,
            },
        )

    def test_post_rerank_can_return_jina_style_gateway_output_format(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["rerank"][0]["routes"][0]["response_output_format"] = "jina_results"
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")

        downstream_payload = {
            "results": [
                {"index": 8, "relevance_score": 0.28858972},
                {"index": 4, "relevance_score": 0.28425363},
                {"index": 0, "relevance_score": 0.27785203},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, fake_http_client):
            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "Organic skincare products for sensitive skin",
                    "documents": ["Doc A", "Doc B", "Doc C"],
                    "top_n": 3,
                    "return_documents": False,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "results": [
                    {"index": 8, "relevance_score": 0.28858972},
                    {"index": 4, "relevance_score": 0.28425363},
                    {"index": 0, "relevance_score": 0.27785203},
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
