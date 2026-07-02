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

PDF_BYTES = b"%PDF-1.7\n%test\n"

VALID_PROVIDERS_TEXT = """
[
  {
    "converter": {
      "baseUrl": "https://converter.example/v1",
      "apikey": "DIRECT-KEY"
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
        "provider": "converter",
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
  "rerank": [],
  "images_generations": [],
  "images_edits": [],
  "pdf_conversions": [
    {
      "gateway_model_name": "gateway/pdf-converter",
      "routes": [
        {
          "provider": "converter",
          "model": "pdf-converter",
          "target_path": "https://converter.example/pdf/api"
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
    def __init__(
        self,
        payload,
        *,
        status_code: int = 200,
        content_type: str = "application/json",
        headers: dict[str, str] | None = None,
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type, **(headers or {})}
        if isinstance(payload, bytes):
            self.content = payload
            self.text = payload.decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            self.content = payload.encode("utf-8")
            self.text = payload
        else:
            self.text = json.dumps(payload)
            self.content = self.text.encode("utf-8")

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        raise ValueError("Payload is not JSON")


class PdfConversionApiTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.providers_path = Path(self.temp_dir.name) / "providers.json"
        self.rules_path = Path(self.temp_dir.name) / "models_fallback_rules.json"
        self.operation_rules_path = Path(self.temp_dir.name) / "models_operation_rules.json"
        self.providers_path.write_text(VALID_PROVIDERS_TEXT, encoding="utf-8")
        self.rules_path.write_text(VALID_FALLBACK_RULES_TEXT, encoding="utf-8")
        self.operation_rules_path.write_text(VALID_OPERATION_RULES_TEXT, encoding="utf-8")
        self.fallback_provider_patcher = patch.object(main.settings, "fallback_provider", "converter")
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
        fake_http_client.get = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()

        with ExitStack() as stack:
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.httpx.AsyncClient", return_value=fake_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "converter"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

            with TestClient(main.app) as client:
                dispatcher = getattr(client.app.state, "operation_dispatcher", None)
                self.assertIsInstance(dispatcher, OperationDispatcher)
                self.assertIs(client.app.state.http_client, fake_http_client)
                yield client, dispatcher, fake_http_client

    def test_pdf_convert_proxies_multipart_request_and_records_usage(self):
        downstream_payload = {
            "status": "completed",
            "artifacts": [{"format": "docx", "url": "/api/files/result.docx"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("pdf_conversions", "gateway/pdf-converter"))

            response = client.post(
                "/v1/pdf/convert",
                files=[
                    ("file", ("source.pdf", PDF_BYTES, "application/pdf")),
                    ("model", (None, "gateway/pdf-converter")),
                    ("formats", (None, "docx")),
                    ("formats", (None, "md")),
                    ("ocr", (None, "auto")),
                    ("target_language", (None, "English")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://converter.example/pdf/api/convert")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["data"],
            {
                "formats": ["docx", "md"],
                "ocr": "auto",
                "target_language": "English",
            },
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["files"][0],
            ("file", ("source.pdf", PDF_BYTES, "application/pdf")),
        )
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()
        call_args = dict(client.app.state.tokens_usage_db.insert_usage.call_args[0][0])
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(call_args["operation"], "pdf_conversion")
        self.assertEqual(call_args["gateway_model"], "gateway/pdf-converter")
        self.assertEqual(call_args["provider"], "converter")
        self.assertEqual(call_args["model"], "pdf-converter")

    def test_pdf_job_status_requires_model_and_proxies_get(self):
        downstream_payload = {
            "id": "job-1",
            "status": "running",
            "progress": {"current": 3, "total": 47},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            response = client.get(
                "/v1/pdf/jobs/job-1",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.get.assert_awaited_once()
        self.assertEqual(fake_http_client.get.await_args.args[0], "https://converter.example/pdf/api/jobs/job-1")
        client.app.state.tokens_usage_db.insert_usage.assert_not_called()

    def test_pdf_create_job_without_usage_does_not_record_usage(self):
        downstream_payload = {
            "id": "job-1",
            "status": "queued",
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/pdf/jobs",
                files=[
                    ("file", ("source.pdf", PDF_BYTES, "application/pdf")),
                    ("model", (None, "gateway/pdf-converter")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        client.app.state.tokens_usage_db.insert_usage.assert_not_called()
        client.app.state.tokens_usage_db.insert_usage_once.assert_not_called()

    def test_pdf_job_terminal_usage_is_recorded_once_per_job(self):
        downstream_payload = {
            "id": "job-1",
            "status": "completed",
            "usage": {
                "prompt_tokens": 10,
                "completion_tokens": 5,
                "total_tokens": 15,
                "cost": 0.25,
            },
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            client.app.state.tokens_usage_db.insert_usage_once.side_effect = [True, False]
            first_response = client.get(
                "/v1/pdf/jobs/job-1",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            second_response = client.get(
                "/v1/pdf/jobs/job-1",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(first_response.status_code, 200)
        self.assertEqual(second_response.status_code, 200)
        self.assertEqual(fake_http_client.get.await_count, 2)
        client.app.state.tokens_usage_db.insert_usage.assert_not_called()
        self.assertEqual(client.app.state.tokens_usage_db.insert_usage_once.call_count, 2)
        idempotency_key, usage_row = client.app.state.tokens_usage_db.insert_usage_once.call_args_list[0].args
        self.assertIn("pdf_job_usage|master|gateway/pdf-converter|converter|pdf-converter|job-1", idempotency_key)
        call_args = dict(usage_row)
        self.assertGreaterEqual(call_args.pop("duration_ms"), 0)
        self.assertEqual(call_args["operation"], "pdf_conversion")
        self.assertEqual(call_args["gateway_model"], "gateway/pdf-converter")
        self.assertEqual(call_args["provider"], "converter")
        self.assertEqual(call_args["model"], "pdf-converter")
        self.assertEqual(call_args["prompt_tokens"], 10)
        self.assertEqual(call_args["completion_tokens"], 5)
        self.assertEqual(call_args["total_tokens"], 15)
        self.assertEqual(call_args["cost"], 0.25)

    def test_pdf_job_failed_terminal_usage_is_recorded_once(self):
        downstream_payload = {
            "id": "job-2",
            "status": "failed",
            "usage": {
                "prompt_tokens": 3,
                "completion_tokens": 0,
                "total_tokens": 3,
                "cost": 0.05,
            },
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, _fake_http_client):
            client.app.state.tokens_usage_db.insert_usage_once.return_value = True
            response = client.get(
                "/v1/pdf/jobs/job-2",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        client.app.state.tokens_usage_db.insert_usage_once.assert_called_once()
        usage_row = dict(client.app.state.tokens_usage_db.insert_usage_once.call_args.args[1])
        self.assertEqual(usage_row["total_tokens"], 3)
        self.assertEqual(usage_row["cost"], 0.05)

    def test_pdf_download_proxies_raw_artifact(self):
        with self._client(
            _FakeDownstreamResponse(
                b"docx-bytes",
                content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                headers={"content-disposition": 'attachment; filename="result.docx"'},
            )
        ) as (client, _dispatcher, fake_http_client):
            response = client.get(
                "/v1/pdf/jobs/job-1/download/result.docx",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.content, b"docx-bytes")
        self.assertEqual(response.headers["content-disposition"], 'attachment; filename="result.docx"')
        fake_http_client.get.assert_awaited_once()
        self.assertEqual(
            fake_http_client.get.await_args.args[0],
            "https://converter.example/pdf/api/jobs/job-1/download/result.docx",
        )

    def test_pdf_convert_rejects_missing_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/pdf/convert",
                files={"file": ("source.pdf", PDF_BYTES, "application/pdf")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_pdf_convert_rejects_invalid_pdf_upload(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/pdf/convert",
                data={"model": "gateway/pdf-converter"},
                files={"file": ("source.pdf", b"not-pdf", "application/pdf")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid PDF file content.")
        fake_http_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
