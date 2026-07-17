import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import audio as audio_api
from llm_gateway_core.api.v1.audio_adapters import (
    AudioAdapterResponse,
    AudioMaterializationTooLarge,
)
from llm_gateway_core.config.loader import ConfigLoader
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.images_audio_accounting_test_support import (
    install_images_audio_accounting_passthrough,
)

WAV_BYTES = b"RIFF\x24\x00\x00\x00WAVEfmt " + b"\x00" * 32

VALID_PROVIDERS_TEXT = """
[
  {
    "openai": {
      "baseUrl": "https://openai.example/v1",
      "apikey": "DIRECT-KEY"
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
  "rerank": [],
  "images_generations": [],
  "images_edits": [],
  "audio_transcriptions": [
    {
      "gateway_model_name": "gateway/audio-transcribe",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-4o-mini-transcribe",
          "custom_body_params": {
            "language": "en"
          }
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
    ):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        if isinstance(payload, bytes):
            self.content = payload
            self.text = payload.decode("utf-8", errors="replace")
        elif isinstance(payload, str):
            self.content = payload.encode("utf-8")
            self.text = payload
        else:
            serialized = json.dumps(payload)
            self.content = serialized.encode("utf-8")
            self.text = serialized

    def json(self):
        if isinstance(self._payload, (dict, list)):
            return self._payload
        raise ValueError("Payload is not JSON")


class AudioTranscriptionsApiTests(unittest.TestCase):
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
        elif callable(downstream_response):
            fake_http_client.post = AsyncMock(side_effect=downstream_response)
        else:
            fake_http_client.post = AsyncMock(return_value=downstream_response)
        fake_http_client.aclose = AsyncMock()
        config_update_coordinator = Mock()
        config_update_coordinator.close = AsyncMock()

        with ExitStack() as stack:
            install_images_audio_accounting_passthrough(stack)
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
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))

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

    def test_audio_transcriptions_valid_multipart_request_proxies_to_downstream_and_records_usage(self):
        downstream_payload = {
            "text": "hello world",
            "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            self.assertIsNotNone(dispatcher.lookup_route("audio_transcriptions", "gateway/audio-transcribe"))

            response = client.post(
                "/v1/audio/transcriptions",
                files=[
                    ("file", ("sample.wav", WAV_BYTES, "audio/wav")),
                    ("model", (None, "gateway/audio-transcribe")),
                    ("response_format", (None, "json")),
                    ("timestamp_granularities[]", (None, "word")),
                    ("timestamp_granularities[]", (None, "segment")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        self.assertEqual(
            fake_http_client.post.await_args.args[0],
            "https://openai.example/v1/audio/transcriptions",
        )
        forwarded_data = fake_http_client.post.await_args.kwargs["data"]
        self.assertEqual(
            forwarded_data,
            {
                "model": "gpt-4o-mini-transcribe",
                "response_format": "json",
                "timestamp_granularities[]": ["word", "segment"],
                "language": "en",
            },
        )
        self.assertNotIn("Content-Type", fake_http_client.post.await_args.kwargs["headers"])
        timeout = fake_http_client.post.await_args.kwargs["timeout"]
        self.assertEqual(timeout.connect, 30.0)
        self.assertEqual(timeout.read, 1200.0)
        self.assertEqual(timeout.write, 60.0)
        self.assertEqual(timeout.pool, 30.0)
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["files"][0][0],
            "file",
        )
        tokens_usage_db.insert_usage.assert_not_called()

    def test_audio_transcriptions_closes_parsed_upload_after_success(self):
        captured_files = []
        validate_upload = audio_api._validate_upload

        async def capture_upload_file(upload):
            captured_files.append(upload.file)
            return await validate_upload(upload)

        async def downstream_response(*_args, **kwargs):
            upload_stream = kwargs["files"][0][1][1]
            self.assertFalse(upload_stream.closed)
            return _FakeDownstreamResponse({"text": "hello world"})

        with patch.object(audio_api, "_validate_upload", new=capture_upload_file):
            with self._client(downstream_response) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/audio/transcriptions",
                    data={"model": "gateway/audio-transcribe"},
                    files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(len(captured_files), 1)
        self.assertIsInstance(captured_files[0], tempfile.SpooledTemporaryFile)
        self.assertTrue(captured_files[0].closed)

    def test_audio_transcriptions_text_response_is_passed_through(self):
        with self._client(
            _FakeDownstreamResponse(
                "plain transcript body",
                content_type="text/plain; charset=utf-8",
            )
        ) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe", "response_format": "text"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "plain transcript body")
        self.assertEqual(response.headers["content-type"], "text/plain; charset=utf-8")
        fake_http_client.post.assert_awaited_once()
        tokens_usage_db.insert_usage.assert_not_called()

    def test_audio_transcriptions_falls_back_to_next_route_after_downstream_failure(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["audio_transcriptions"][0]["routes"].append(
            {
                "provider": "nvidia",
                "model": "nvidia/fallback-transcribe",
                "target_path": "/audio/transcriptions",
                "custom_body_params": {
                    "language": "ru",
                },
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")
        self.config_loader.load_operation_rules()

        downstream_payload = {
            "text": "fallback transcript",
            "usage": {"prompt_tokens": 5, "completion_tokens": 2, "total_tokens": 7},
        }

        downstream_responses = [
            _FakeDownstreamResponse({"error": {"message": "primary-down"}}, status_code=503),
            _FakeDownstreamResponse(downstream_payload, status_code=200),
        ]
        forwarded_uploads = []
        admission_snapshots = []

        async def downstream_response(*_args, **kwargs):
            snapshot = client.app.state.services.upload_admission.snapshot
            admission_snapshots.append(
                (snapshot.active_bytes, snapshot.active_leases, snapshot.waiters)
            )
            upload_stream = kwargs["files"][0][1][1]
            self.assertFalse(upload_stream.closed)
            forwarded_uploads.append(upload_stream.read())
            return downstream_responses.pop(0)

        with self._client(downstream_response) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.post(
                "/v1/audio/transcriptions",
                files=[
                    ("file", ("sample.wav", WAV_BYTES, "audio/wav")),
                    ("model", (None, "gateway/audio-transcribe")),
                    ("response_format", (None, "json")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )
            terminal_snapshot = client.app.state.services.upload_admission.snapshot

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        self.assertEqual(
            fake_http_client.post.await_args_list[0].args[0],
            "https://openai.example/v1/audio/transcriptions",
        )
        self.assertEqual(
            fake_http_client.post.await_args_list[1].args[0],
            "https://integrate.api.nvidia.com/v1/audio/transcriptions",
        )
        self.assertEqual(
            fake_http_client.post.await_args_list[1].kwargs["headers"]["Authorization"],
            "Bearer NVIDIA-KEY",
        )
        self.assertEqual(
            fake_http_client.post.await_args_list[1].kwargs["data"],
            {
                "model": "nvidia/fallback-transcribe",
                "response_format": "json",
                "language": "ru",
            },
        )
        self.assertEqual(forwarded_uploads, [WAV_BYTES, WAV_BYTES])
        self.assertEqual(
            admission_snapshots,
            [(len(WAV_BYTES), 1, 0), (len(WAV_BYTES), 1, 0)],
        )
        self.assertEqual(
            (
                terminal_snapshot.active_bytes,
                terminal_snapshot.active_leases,
                terminal_snapshot.waiters,
            ),
            (0, 0, 0),
        )
        self.assertEqual(fake_http_client.post.await_args_list[1].kwargs["files"][0][0], "file")
        self.assertEqual(fake_http_client.post.await_args_list[1].kwargs["files"][0][1][0], "sample.wav")
        self.assertEqual(fake_http_client.post.await_args_list[1].kwargs["files"][0][1][2], "audio/wav")
        tokens_usage_db.insert_usage.assert_not_called()

    def test_audio_transcriptions_local_capacity_failure_does_not_fallback(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["audio_transcriptions"][0]["routes"].append(
            {
                "provider": "nvidia",
                "model": "nvidia/fallback-transcribe",
            }
        )
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")
        self.config_loader.load_operation_rules()

        with self._client(_FakeDownstreamResponse({"unused": True})) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            client.app.state.services.upload_admission.close()
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 503)
        self.assertEqual(response.json()["detail"], "Upload processing capacity is unavailable.")
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_rejects_more_than_one_file_at_parser_boundary(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (
            client,
            _dispatcher,
            fake_http_client,
        ):
            response = client.post(
                "/v1/audio/transcriptions",
                files=[
                    ("file", ("first.wav", WAV_BYTES, "audio/wav")),
                    ("file", ("second.wav", WAV_BYTES, "audio/wav")),
                    ("model", (None, "gateway/audio-transcribe")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_nvidia_route_ignores_unsupported_openai_fields(self):
        self.operation_rules_path.write_text(
            json.dumps(
                {
                    "embeddings": [],
                    "rerank": [],
                    "images_generations": [],
                    "images_edits": [],
                    "audio_transcriptions": [
                        {
                            "gateway_model_name": "gateway/audio-transcribe",
                            "routes": [
                                {
                                    "provider": "nvidia",
                                    "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                                    "request_format": "nvidia_riva_grpc",
                                    "custom_headers": {"function-id": "func-123"},
                                }
                            ],
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )
        self.config_loader.load_operation_rules()

        with patch(
            "llm_gateway_core.api.v1.audio.transcribe_with_nvidia_riva_grpc",
            AsyncMock(
                return_value=AudioAdapterResponse(
                    body={"text": "hello from nvidia"},
                    content_type="application/json",
                )
            ),
        ) as adapter_mock:
            with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
                tokens_usage_db = client.app.state.services.tokens_usage_db
                response = client.post(
                    "/v1/audio/transcriptions",
                    files=[
                        ("file", ("sample.wav", WAV_BYTES, "audio/wav")),
                        ("model", (None, "gateway/audio-transcribe")),
                        ("response_format", (None, "json")),
                        ("temperature", (None, "0")),
                        ("prompt", (None, "ignore me")),
                    ],
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), {"text": "hello from nvidia"})
        fake_http_client.post.assert_not_awaited()
        self.assertEqual(
            adapter_mock.await_args.kwargs["provider_base_url"],
            "https://integrate.api.nvidia.com/v1",
        )
        self.assertEqual(
            adapter_mock.await_args.kwargs["route_custom_headers"],
            {"function-id": "func-123"},
        )
        self.assertEqual(
            adapter_mock.await_args.kwargs["request_payload"]["model"],
            "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
        )
        self.assertNotIn("temperature", adapter_mock.await_args.kwargs["request_payload"])
        self.assertNotIn("prompt", adapter_mock.await_args.kwargs["request_payload"])
        tokens_usage_db.insert_usage.assert_not_called()

    def test_audio_transcriptions_local_riva_expansion_413_does_not_fallback(self):
        operation_rules = json.loads(self.operation_rules_path.read_text(encoding="utf-8"))
        operation_rules["audio_transcriptions"][0]["routes"] = [
            {
                "provider": "nvidia",
                "model": "nvidia/parakeet-1_1b-rnnt-multilingual-asr",
                "request_format": "nvidia_riva_grpc",
                "custom_headers": {"function-id": "func-123"},
            },
            {
                "provider": "openai",
                "model": "gpt-4o-mini-transcribe",
            },
        ]
        self.operation_rules_path.write_text(json.dumps(operation_rules), encoding="utf-8")
        self.config_loader.load_operation_rules()

        with patch(
            "llm_gateway_core.api.v1.audio.transcribe_with_nvidia_riva_grpc",
            AsyncMock(side_effect=AudioMaterializationTooLarge()),
        ) as adapter_mock:
            with self._client(_FakeDownstreamResponse({"unused": True})) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/audio/transcriptions",
                    data={"model": "gateway/audio-transcribe"},
                    files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 413)
        adapter_mock.assert_awaited_once()
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_multipart_works_with_real_async_httpx_client(self):
        captured_request: dict[str, object] = {}

        def downstream_handler(request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            captured_request["content_type"] = request.headers.get("content-type")
            captured_request["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "text": "hello world",
                    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
                },
            )

        real_http_client = httpx.AsyncClient(transport=httpx.MockTransport(downstream_handler))

        with ExitStack() as stack:
            install_images_audio_accounting_passthrough(stack)
            stack.enter_context(patch("main.ConfigLoader", return_value=self.config_loader))
            stack.enter_context(patch("main.create_shared_http_client", return_value=real_http_client))
            stack.enter_context(patch("main.TokensUsageDB"))
            stack.enter_context(patch("main.start_usage_stats_cleanup_task", return_value=_FakeCleanupTask()))
            stack.enter_context(patch.object(main.settings, "gateway_api_key", "test-gateway-key"))
            stack.enter_context(patch.object(main.settings, "fallback_provider", "openai"))

            with TestClient(main.app) as client:
                response = client.post(
                    "/v1/audio/transcriptions",
                    files=[
                        ("file", ("sample.wav", WAV_BYTES, "audio/wav")),
                        ("model", (None, "gateway/audio-transcribe")),
                        ("response_format", (None, "json")),
                        ("timestamp_granularities[]", (None, "word")),
                        ("timestamp_granularities[]", (None, "segment")),
                    ],
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["text"], "hello world")
        self.assertEqual(captured_request["url"], "https://openai.example/v1/audio/transcriptions")
        self.assertIn("multipart/form-data", str(captured_request["content_type"]))
        body = captured_request["body"]
        self.assertIsInstance(body, bytes)
        self.assertIn(b'name=\"model\"', body)
        self.assertIn(b'gpt-4o-mini-transcribe', body)
        self.assertIn(b'name=\"response_format\"', body)
        self.assertIn(b'json', body)
        self.assertIn(b'name=\"timestamp_granularities[]\"', body)
        self.assertIn(b'word', body)
        self.assertIn(b'segment', body)
        self.assertIn(b'name=\"language\"', body)
        self.assertIn(b'en', body)
        self.assertIn(b'name=\"file\"', body)
        self.assertIn(WAV_BYTES, body)

    def test_audio_transcriptions_rejects_missing_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Missing 'model' in request body")
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_preserves_downstream_413_status(self):
        with self._client(
            _FakeDownstreamResponse(
                {"error": {"message": "Payload Too Large"}},
                status_code=413,
            )
        ) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                files=[
                    ("file", ("sample.wav", WAV_BYTES, "audio/wav")),
                    ("model", (None, "gateway/audio-transcribe")),
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 413)
        self.assertEqual(response.json()["detail"], "Downstream request failed with status 413.")
        self.assertNotIn("Payload Too Large", response.text)
        fake_http_client.post.assert_awaited_once()

    def test_audio_transcriptions_rejects_missing_file(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe"},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Audio transcriptions endpoint expects multipart/form-data.")
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_rejects_invalid_audio_upload(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe"},
                files={"file": ("sample.wav", b"not-audio", "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid audio file content.")
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_unknown_model(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "unknown-audio-model"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 404)
        self.assertEqual(
            response.json()["detail"],
            "No audio transcription route configured for model 'unknown-audio-model'.",
        )
        fake_http_client.post.assert_not_awaited()

    def test_audio_transcriptions_rejects_streaming(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/audio/transcriptions",
                data={"model": "gateway/audio-transcribe", "stream": "true"},
                files={"file": ("sample.wav", WAV_BYTES, "audio/wav")},
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Audio transcription streaming is not supported.")
        fake_http_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
