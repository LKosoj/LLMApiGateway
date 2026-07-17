import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import pdf as pdf_api
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.pdf_accounting_test_support import install_pdf_accounting_passthrough

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
        fake_http_client.uploaded_file_bodies = []
        fake_http_client.uploaded_files_open = []

        async def post(*_args, **kwargs):
            uploaded_bodies = []
            for _field_name, (_filename, content, _content_type) in kwargs.get(
                "files",
                [],
            ):
                if isinstance(content, bytes):
                    uploaded_bodies.append(content)
                    fake_http_client.uploaded_files_open.append(True)
                else:
                    fake_http_client.uploaded_files_open.append(not content.closed)
                    uploaded_bodies.append(content.read())
            fake_http_client.uploaded_file_bodies.append(uploaded_bodies)
            return downstream_response

        fake_http_client.post = AsyncMock(side_effect=post)
        fake_http_client.get = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
            install_pdf_accounting_passthrough(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(
                patch("main.create_shared_http_client", return_value=fake_http_client)
            )
            stack.enter_context(
                patch(
                    "main.ConfigUpdateCoordinator",
                    return_value=config_update_coordinator,
                )
            )
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "converter"))
            stack.enter_context(patch.object(main.settings, "verify_models_on_startup", "off"))

            with TestClient(main.app) as client:
                services = client.app.state.services
                self.assertIs(services.http_client, fake_http_client)
                lease = client.portal.call(services.runtime_manager.acquire_current)
                try:
                    dispatcher = lease.snapshot.operation_dispatcher
                    self.assertIsInstance(dispatcher, OperationDispatcher)
                    yield client, dispatcher, fake_http_client
                finally:
                    client.portal.call(lease.release)

    def test_pdf_convert_proxies_multipart_request_without_legacy_usage_write(self):
        downstream_payload = {
            "status": "completed",
            "artifacts": [{"format": "docx", "url": "/api/files/result.docx"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
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
        file_field, file_payload = fake_http_client.post.await_args.kwargs["files"][0]
        self.assertEqual(file_field, "file")
        self.assertEqual(file_payload[0], "source.pdf")
        self.assertEqual(file_payload[2], "application/pdf")
        self.assertEqual(fake_http_client.uploaded_file_bodies, [[PDF_BYTES]])
        self.assertEqual(fake_http_client.uploaded_files_open, [True])
        tokens_usage_db.insert_usage.assert_not_called()
        tokens_usage_db.insert_usage_once.assert_not_called()

    def test_pdf_convert_closes_parsed_upload_after_success(self):
        captured_files = []
        serialize_upload = pdf_api._serialize_upload

        async def capture_upload_file(upload):
            captured_files.append(upload.file)
            return await serialize_upload(upload)

        with patch.object(pdf_api, "_serialize_upload", new=capture_upload_file):
            with self._client(_FakeDownstreamResponse({"status": "completed"})) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/pdf/convert",
                    data={"model": "gateway/pdf-converter"},
                    files={"file": ("source.pdf", PDF_BYTES, "application/pdf")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(len(captured_files), 1)
        self.assertIsInstance(captured_files[0], tempfile.SpooledTemporaryFile)
        self.assertTrue(captured_files[0].closed)

    def test_pdf_convert_rejects_file_over_configured_limit_before_downstream(self):
        with patch.object(main.settings, "pdf_upload_max_bytes", len(PDF_BYTES) - 1):
            with self._client(_FakeDownstreamResponse({"unused": True})) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/pdf/convert",
                    data={"model": "gateway/pdf-converter"},
                    files={"file": ("source.pdf", PDF_BYTES, "application/pdf")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 413)
        fake_http_client.post.assert_not_awaited()

    def test_pdf_convert_rejects_second_file_before_downstream(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            response = client.post(
                "/v1/pdf/convert",
                data={"model": "gateway/pdf-converter"},
                files=[
                    ("file", ("source.pdf", PDF_BYTES, "application/pdf")),
                    ("file", ("second.pdf", PDF_BYTES, "application/pdf")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        fake_http_client.post.assert_not_awaited()

    def test_pdf_capacity_timeout_is_local_and_skips_downstream(self):
        with patch.object(main.settings, "upload_admission_timeout_seconds", 0.01):
            with self._client(_FakeDownstreamResponse({"unused": True})) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                admission = client.app.state.services.upload_admission
                active = client.portal.call(
                    admission.acquire,
                    admission.snapshot.max_bytes,
                )
                try:
                    response = client.post(
                        "/v1/pdf/convert",
                        data={"model": "gateway/pdf-converter"},
                        files={"file": ("source.pdf", PDF_BYTES, "application/pdf")},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )
                finally:
                    client.portal.call(active.release)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Upload processing capacity is unavailable.",
        )
        fake_http_client.post.assert_not_awaited()

    def test_pdf_job_status_requires_model_and_proxies_get(self):
        downstream_payload = {
            "id": "job-1",
            "status": "running",
            "progress": {"current": 3, "total": 47},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.get(
                "/v1/pdf/jobs/job-1",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.get.assert_awaited_once()
        self.assertEqual(fake_http_client.get.await_args.args[0], "https://converter.example/pdf/api/jobs/job-1")
        tokens_usage_db.insert_usage.assert_not_called()

    def test_pdf_create_job_without_usage_does_not_record_usage(self):
        downstream_payload = {
            "id": "job-1",
            "status": "queued",
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
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
        tokens_usage_db.insert_usage.assert_not_called()
        tokens_usage_db.insert_usage_once.assert_not_called()

    def test_pdf_job_terminal_success_is_proxied_without_legacy_usage_write(self):
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
            tokens_usage_db = client.app.state.services.tokens_usage_db
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
        tokens_usage_db.insert_usage.assert_not_called()
        tokens_usage_db.insert_usage_once.assert_not_called()

    def test_pdf_job_failed_terminal_usage_is_not_charged_by_legacy_path(self):
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
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.get(
                "/v1/pdf/jobs/job-2",
                params={"model": "gateway/pdf-converter"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        tokens_usage_db.insert_usage.assert_not_called()
        tokens_usage_db.insert_usage_once.assert_not_called()

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
