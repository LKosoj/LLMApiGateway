import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher


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
    }
  ],
  "images_generations": [],
  "images_edits": []
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


class RerankApiTests(unittest.TestCase):
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
                dispatcher = getattr(client.app.state, "operation_dispatcher", None)
                self.assertIsInstance(dispatcher, OperationDispatcher)
                self.assertIs(client.app.state.http_client, fake_http_client)
                yield client, dispatcher, fake_http_client

    def test_rerank_valid_request(self):
        downstream_payload = {
            "results": [
                {"index": 1, "relevance_score": 0.98, "document": {"text": "Doc B"}},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

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
        self.assertEqual(response.json(), {"data": [{"index": 1, "score": 0.98}]})
        fake_http_client.post.assert_awaited_once()
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()
        call_args = dict(client.app.state.tokens_usage_db.insert_usage.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(
            call_args,
            {
                "prompt_tokens": 0,
                "completion_tokens": 0,
                "total_tokens": 0,
                "reasoning_tokens": 0,
                "cached_tokens": 0,
                "cost": 0,
                "cost_saved": 0,
                "is_estimated": False,
                "gateway_model": "gateway/rerank-v1",
                "operation": "rerank",
                "provider": "cohere",
                "model": "rerank-v3.5",
            },
        )

    def test_rerank_documents_not_list(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": "not-a-list",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "Missing or invalid 'documents' in request body; expected list[str]",
        )
        fake_http_client.post.assert_not_awaited()

    def test_rerank_missing_query(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={"model": "gateway/rerank-v1", "documents": ["Doc A"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'query' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_rerank_missing_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={"query": "What is the refund policy?", "documents": ["Doc A"]},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_rerank_mapping_correct(self):
        downstream_payload = {"results": [{"index": 0, "relevance_score": 0.55}]}

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            route = dispatcher.lookup_route("rerank", "gateway/rerank-v1")
            self.assertIsNotNone(route)

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
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
        self.assertNotIn("query", fake_http_client.post.await_args.kwargs["json"])
        self.assertNotIn("documents", fake_http_client.post.await_args.kwargs["json"])

    def test_rerank_response_normalized(self):
        downstream_payload = {
            "results": [
                {"index": "1", "relevance_score": "0.98", "document": {"text": "Doc B"}},
                {"score": 0.42, "content": "Doc C"},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertEqual(dispatcher.lookup_route("rerank", "gateway/rerank-v1").model, "rerank-v3.5")

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B", "Doc C"],
                    "return_documents": False,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(
            response.json(),
            {
                "data": [
                    {"index": 1, "score": 0.98},
                    {"index": 1, "score": 0.42},
                ]
            },
        )
        fake_http_client.post.assert_awaited_once()

    def test_rerank_top_n(self):
        downstream_payload = {
            "results": [
                {"index": 1, "relevance_score": 0.98},
                {"index": 0, "relevance_score": 0.42},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "top_n": 1,
                    "return_documents": False,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"index": 1, "score": 0.98}]})
        fake_http_client.post.assert_awaited_once()

    def test_rerank_return_documents(self):
        downstream_payload = {
            "results": [
                {"index": 1, "relevance_score": 0.98},
            ]
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                    "return_documents": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"data": [{"index": 1, "score": 0.98, "document": "Doc B"}]})
        fake_http_client.post.assert_awaited_once()

    def test_rerank_downstream_error(self):
        with self._client(
            _FakeDownstreamResponse(
                {"error": {"message": "rerank-downstream-500"}},
                status_code=500,
            )
        ) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("rerank", "gateway/rerank-v1"))

            response = client.post(
                "/v1/rerank",
                json={
                    "model": "gateway/rerank-v1",
                    "query": "What is the refund policy?",
                    "documents": ["Doc A", "Doc B"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "rerank-downstream-500")
        fake_http_client.post.assert_awaited_once()


if __name__ == "__main__":
    unittest.main()
