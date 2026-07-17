import io
import json
import tempfile
import unittest
from contextlib import ExitStack, contextmanager
from pathlib import Path
from unittest.mock import AsyncMock, Mock, patch

import httpx
from fastapi.testclient import TestClient

import main
from llm_gateway_core.api.v1 import images as images_api
from llm_gateway_core.api.v1.image_adapters import (
    _base64_encoded_size,
    build_downstream_image_request,
    estimate_image_request_processing_weight,
)
from llm_gateway_core.api.v1.operation_runtime import ValidatedUpload
from llm_gateway_core.config.loader import ConfigLoader, OperationRoute
from llm_gateway_core.config.settings import IMAGE_DATA_URL_OVERHEAD_BYTES
from llm_gateway_core.services.request_handler import OperationDispatcher
from tests.images_audio_accounting_test_support import (
    install_images_audio_accounting_passthrough,
)

PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
MASK_PNG_BYTES = b"\x89PNG\r\n\x1a\n" + b"\x01" * 16

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
      "baseUrl": "https://integrate.api.nvidia.com",
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
  "images_generations": [
    {
      "gateway_model_name": "gateway/image-gen",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-image-1",
          "target_path": "/images/generations",
          "custom_body_params": {
            "size": "1024x1024",
            "quality": "high"
          }
        },
        {
          "provider": "openai",
          "model": "gpt-image-1-fallback",
          "target_path": "/images/generations",
          "custom_body_params": {
            "size": "1024x1024",
            "quality": "auto"
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/nvidia-image-gen",
      "routes": [
        {
          "provider": "nvidia",
          "model": "black-forest-labs/flux.2-klein-4b",
          "target_path": "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b",
          "request_format": "nvidia_genai_json",
          "response_format": "nvidia_artifacts",
          "request_mapping": {
            "fields": {
              "prompt": "prompt",
              "seed": "seed",
              "width": {
                "from": "size",
                "transform": "size_width"
              },
              "height": {
                "from": "size",
                "transform": "size_height"
              },
              "": "n"
            }
          },
          "response_mapping": {
            "artifacts_path": "artifacts",
            "base64_field": "base64"
          },
          "custom_body_params": {
            "steps": 4
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/image-gen-compat",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-image-1",
          "target_path": "/images/generations",
          "request_mapping": {
            "omit_client_fields": ["response_format", "seed"]
          }
        }
      ]
    }
  ],
  "images_edits": [
    {
      "gateway_model_name": "gateway/image-edit",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-image-1",
          "custom_body_params": {
            "input_fidelity": "high"
          }
        },
        {
          "provider": "openai",
          "model": "gpt-image-1-fallback",
          "custom_body_params": {
            "input_fidelity": "low"
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/image-edit-multipart",
      "routes": [
        {
          "provider": "openai",
          "model": "gpt-image-1",
          "target_path": "/images/edits",
          "request_format": "openai_images_multipart",
          "custom_body_params": {
            "input_fidelity": "high"
          }
        }
      ]
    },
    {
      "gateway_model_name": "gateway/nvidia-image-edit",
      "routes": [
        {
          "provider": "nvidia",
          "model": "black-forest-labs/flux.2-klein-4b",
          "target_path": "/v1/genai/black-forest-labs/flux.2-klein-4b",
          "request_format": "nvidia_genai_json",
          "response_format": "nvidia_artifacts",
          "request_mapping": {
            "fields": {
              "prompt": "prompt",
              "image": {
                "from": "images",
                "transform": "first_image_to_reference",
                "pattern": "^data:image/png;example_id,[0-3]$",
                "validation_error": "NVIDIA FLUX.2-klein-4b preview edit route supports only predefined example images in format data:image/png;example_id,{0-3}. Uploaded files are not supported by this provider API."
              },
              "width": {
                "from": "size",
                "transform": "size_width"
              },
              "height": {
                "from": "size",
                "transform": "size_height"
              },
              "seed": "seed"
            }
          },
          "response_mapping": {
            "artifacts_path": "artifacts",
            "base64_field": "base64"
          },
          "custom_body_params": {
            "steps": 4
          }
        },
        {
          "provider": "openai",
          "model": "gpt-image-1-fallback",
          "target_path": "/images/edits",
          "custom_body_params": {
            "input_fidelity": "low"
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
    def __init__(self, payload, status_code: int = 200, text: str | None = None):
        self._payload = payload
        self.status_code = status_code
        self.text = text if text is not None else json.dumps(payload)

    def json(self):
        return self._payload


class ImagesEndpointTests(unittest.TestCase):
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
        if isinstance(downstream_response, (list, tuple)):
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

    def test_post_images_alias_uses_generation_route_and_records_usage(self):
        downstream_payload = {
            "created": 123,
            "data": [{"b64_json": "image-bytes"}],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            self.assertIsNotNone(dispatcher.lookup_route("images_generations", "gateway/image-gen"))

            response = client.post(
                "/v1/images",
                json={
                    "model": "gateway/image-gen",
                    "prompt": "Draw a lighthouse at sunrise",
                    "size": "512x512",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://openai.example/v1/images/generations")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "gpt-image-1",
                "prompt": "Draw a lighthouse at sunrise",
                "size": "1024x1024",
                "quality": "high",
            },
        )
        tokens_usage_db.insert_usage.assert_not_called()

    def test_post_images_generations_falls_back_after_downstream_error_status(self):
        fallback_payload = {
            "created": 124,
            "data": [{"b64_json": "fallback-image-bytes"}],
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "rate limited"}}, status_code=429),
                _FakeDownstreamResponse(fallback_payload),
            ]
        ) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "gateway/image-gen",
                    "prompt": "Draw a lighthouse at sunrise",
                    "size": "512x512",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fallback_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        first_call, second_call = fake_http_client.post.await_args_list
        self.assertEqual(first_call.kwargs["json"]["model"], "gpt-image-1")
        self.assertEqual(second_call.kwargs["json"]["model"], "gpt-image-1-fallback")
        self.assertEqual(second_call.kwargs["json"]["quality"], "auto")
        tokens_usage_db.insert_usage.assert_not_called()

    def test_post_images_generations_rejects_streaming(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "gateway/image-gen",
                    "prompt": "Draw a lighthouse at sunrise",
                    "stream": True,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Image streaming is not supported.")
        fake_http_client.post.assert_not_awaited()

    def test_post_images_generations_can_omit_client_fields_for_downstream_compatibility(self):
        downstream_payload = {
            "created": 125,
            "data": [{"b64_json": "image-bytes"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "gateway/image-gen-compat",
                    "prompt": "Draw a lighthouse at sunrise",
                    "size": "1024x1024",
                    "response_format": "b64_json",
                    "seed": 42,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "gpt-image-1",
                "prompt": "Draw a lighthouse at sunrise",
                "size": "1024x1024",
            },
        )

    def test_post_images_edits_json_uses_edit_route(self):
        downstream_payload = {
            "created": 456,
            "data": [{"url": "https://example.com/edited.png"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("images_edits", "gateway/image-edit"))

            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/image-edit",
                    "prompt": "Remove the background",
                    "images": [{"image_url": "https://example.com/source.png"}],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        self.assertEqual(fake_http_client.post.await_args.args[0], "https://openai.example/v1/images/edits")
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "model": "gpt-image-1",
                "prompt": "Remove the background",
                "images": [{"image_url": "https://example.com/source.png"}],
                "input_fidelity": "high",
            },
        )

    def test_post_images_edits_falls_back_after_invalid_downstream_response(self):
        fallback_payload = {
            "created": 457,
            "data": [{"b64_json": "fallback-edited-image"}],
        }

        with self._client(
            [
                _FakeDownstreamResponse({"unexpected": []}),
                _FakeDownstreamResponse(fallback_payload),
            ]
        ) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/nvidia-image-edit",
                    "prompt": "Replace the sky with aurora",
                    "images": ["data:image/png;example_id,1"],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fallback_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        first_call, second_call = fake_http_client.post.await_args_list
        self.assertEqual(
            first_call.args[0],
            "https://integrate.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b",
        )
        self.assertEqual(second_call.args[0], "https://openai.example/v1/images/edits")
        self.assertEqual(second_call.kwargs["json"]["model"], "gpt-image-1-fallback")
        self.assertEqual(second_call.kwargs["json"]["input_fidelity"], "low")
        tokens_usage_db.insert_usage.assert_not_called()

    def test_post_images_edits_multipart_falls_back_after_payload_too_large(self):
        fallback_payload = {
            "created": 458,
            "data": [{"b64_json": "fallback-edited-image"}],
        }
        captured_files = []
        attempted_contents = []
        admission_snapshots = []
        validate_upload = images_api._validate_image_upload

        async def capture_upload_file(upload):
            validated = await validate_upload(upload)
            captured_files.append(validated.file)
            return validated

        async def downstream_post(*_args, **kwargs):
            snapshot = client.app.state.services.upload_admission.snapshot
            admission_snapshots.append(
                (snapshot.active_bytes, snapshot.active_leases, snapshot.waiters)
            )
            upload_file = kwargs["files"][0][1][1]
            self.assertIs(upload_file, captured_files[0])
            self.assertFalse(upload_file.closed)
            attempted_contents.append(upload_file.read())
            if len(attempted_contents) == 1:
                return _FakeDownstreamResponse(
                    {"error": {"message": "payload too large"}},
                    status_code=413,
                )
            return _FakeDownstreamResponse(fallback_payload)

        with patch.object(images_api, "_validate_image_upload", new=capture_upload_file):
            with self._client(downstream_post) as (client, _dispatcher, fake_http_client):
                tokens_usage_db = client.app.state.services.tokens_usage_db
                response = client.post(
                    "/v1/images/edits",
                    data={
                        "model": "gateway/image-edit",
                        "prompt": "Add a red border",
                    },
                    files={
                        "image": ("source.png", io.BytesIO(PNG_BYTES), "image/png"),
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )
                terminal_snapshot = client.app.state.services.upload_admission.snapshot

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fallback_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        first_call, second_call = fake_http_client.post.await_args_list
        self.assertEqual(first_call.kwargs["data"]["model"], "gpt-image-1")
        self.assertEqual(second_call.kwargs["data"]["model"], "gpt-image-1-fallback")
        self.assertEqual(second_call.kwargs["data"]["input_fidelity"], "low")
        self.assertEqual(attempted_contents, [PNG_BYTES, PNG_BYTES])
        self.assertEqual(
            admission_snapshots,
            [(len(PNG_BYTES), 1, 0), (len(PNG_BYTES), 1, 0)],
        )
        self.assertEqual(
            (
                terminal_snapshot.active_bytes,
                terminal_snapshot.active_leases,
                terminal_snapshot.waiters,
            ),
            (0, 0, 0),
        )
        self.assertTrue(captured_files[0].closed)
        tokens_usage_db.insert_usage.assert_not_called()

    def test_post_images_edits_json_can_force_openai_multipart_downstream(self):
        downstream_payload = {
            "created": 457,
            "data": [{"b64_json": "edited-image"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("images_edits", "gateway/image-edit-multipart"))

            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/image-edit-multipart",
                    "prompt": "Add a blue frame",
                    "images": [
                        {
                            "filename": "source.png",
                            "b64_json": "iVBORw0KGgoAAAAAAAAAAAAAAAAAAAAA",
                            "content_type": "image/png",
                        }
                    ],
                    "mask": {
                        "filename": "mask.png",
                        "data_url": "data:image/png;base64,iVBORw0KGgoBAQEBAQEBAQEBAQEBAQEB",
                    },
                    "size": "1024x1024",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        post_kwargs = fake_http_client.post.await_args.kwargs
        self.assertNotIn("json", post_kwargs)
        self.assertEqual(post_kwargs["headers"], {"Authorization": "Bearer DIRECT-KEY"})
        self.assertEqual(
            dict(post_kwargs["data"]),
            {
                "model": "gpt-image-1",
                "prompt": "Add a blue frame",
                "size": "1024x1024",
                "input_fidelity": "high",
            },
        )
        self.assertEqual([field_name for field_name, _ in post_kwargs["files"]], ["image[]", "mask"])
        image_file = post_kwargs["files"][0][1]
        mask_file = post_kwargs["files"][1][1]
        self.assertEqual(image_file, ("source.png", PNG_BYTES, "image/png"))
        self.assertEqual(mask_file, ("mask.png", MASK_PNG_BYTES, "image/png"))

    def test_post_images_edits_json_multipart_rejects_http_image_url(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/image-edit-multipart",
                    "prompt": "Add a blue frame",
                    "images": [{"image_url": "https://example.com/source.png"}],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.json()["detail"],
            "HTTP image URLs cannot be converted to multipart; send base64/data_url image content.",
        )
        fake_http_client.post.assert_not_awaited()

    def test_post_images_edits_multipart_proxies_files_and_scalar_fields(self):
        downstream_payload = {
            "created": 789,
            "data": [{"b64_json": "edited-image"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            tokens_usage_db = client.app.state.services.tokens_usage_db
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/image-edit",
                    "prompt": "Add a red border",
                },
                files={
                    "image": ("source.png", io.BytesIO(PNG_BYTES), "image/png"),
                    "mask": ("mask.png", io.BytesIO(MASK_PNG_BYTES), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), downstream_payload)
        post_kwargs = fake_http_client.post.await_args.kwargs
        self.assertNotIn("json", post_kwargs)
        self.assertEqual(post_kwargs["headers"], {"Authorization": "Bearer DIRECT-KEY"})
        self.assertEqual(
            dict(post_kwargs["data"]),
            {
                "model": "gpt-image-1",
                "prompt": "Add a red border",
                "input_fidelity": "high",
            },
        )
        self.assertEqual([field_name for field_name, _ in post_kwargs["files"]], ["image[]", "mask"])
        tokens_usage_db.insert_usage.assert_not_called()

    def test_post_images_edits_closes_parsed_upload_after_success(self):
        captured_files = []
        validate_upload = images_api._validate_image_upload

        async def capture_upload_file(upload):
            validated = await validate_upload(upload)
            captured_files.append(validated.file)
            return validated

        async def downstream_post(*_args, **kwargs):
            upload_file = kwargs["files"][0][1][1]
            self.assertIs(upload_file, captured_files[0])
            self.assertFalse(upload_file.closed)
            self.assertEqual(upload_file.read(), PNG_BYTES)
            return _FakeDownstreamResponse({"data": [{"b64_json": "edited-image"}]})

        with patch.object(images_api, "_validate_image_upload", new=capture_upload_file):
            with self._client(downstream_post) as (
                client,
                _dispatcher,
                fake_http_client,
            ):
                response = client.post(
                    "/v1/images/edits",
                    data={
                        "model": "gateway/image-edit",
                        "prompt": "Add a red border",
                    },
                    files={"image": ("source.png", PNG_BYTES, "image/png")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(len(captured_files), 1)
        self.assertIsInstance(captured_files[0], tempfile.SpooledTemporaryFile)
        self.assertTrue(captured_files[0].closed)

    def test_post_images_edits_multipart_rejects_invalid_image_upload(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/image-edit",
                    "prompt": "Add a red border",
                },
                files={
                    "image": ("source.png", io.BytesIO(b"not-image"), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.json()["detail"], "Invalid image file content.")
        fake_http_client.post.assert_not_awaited()

    def test_post_images_edits_multipart_enforces_file_total_and_count_limits(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            with patch.object(images_api.settings, "image_upload_max_file_bytes", len(PNG_BYTES) - 1):
                per_file_response = client.post(
                    "/v1/images/edits",
                    data={"model": "gateway/image-edit", "prompt": "Edit"},
                    files={"image": ("source.png", PNG_BYTES, "image/png")},
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

            with (
                patch.object(images_api.settings, "image_upload_max_file_bytes", len(PNG_BYTES)),
                patch.object(images_api.settings, "image_upload_max_total_bytes", (2 * len(PNG_BYTES)) - 1),
            ):
                total_response = client.post(
                    "/v1/images/edits",
                    data={"model": "gateway/image-edit", "prompt": "Edit"},
                    files=[
                        ("image", ("first.png", PNG_BYTES, "image/png")),
                        ("image[]", ("second.png", PNG_BYTES, "image/png")),
                    ],
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

            count_response = client.post(
                "/v1/images/edits",
                data={"model": "gateway/image-edit", "prompt": "Edit"},
                files=[
                    ("image", (f"source-{index}.png", PNG_BYTES, "image/png"))
                    for index in range(5)
                ],
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(per_file_response.status_code, 413)
        self.assertEqual(total_response.status_code, 413)
        self.assertEqual(count_response.status_code, 400)
        self.assertIn("At most four", count_response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_local_upload_capacity_failure_does_not_fall_back(self):
        with patch.object(main.settings, "upload_admission_timeout_seconds", 0.01):
            with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
                admission = client.app.state.services.upload_admission
                blocking_lease = client.portal.call(
                    admission.acquire,
                    admission.snapshot.max_bytes,
                )
                try:
                    response = client.post(
                        "/v1/images/edits",
                        data={"model": "gateway/image-edit", "prompt": "Edit"},
                        files={"image": ("source.png", PNG_BYTES, "image/png")},
                        headers={"Authorization": "Bearer test-gateway-key"},
                    )
                finally:
                    client.portal.call(blocking_lease.release)

        self.assertEqual(response.status_code, 503)
        self.assertEqual(
            response.json()["detail"],
            "Upload processing capacity is unavailable.",
        )
        fake_http_client.post.assert_not_awaited()

    def test_image_adapter_materializes_validated_upload_only_for_json_route(self):
        upload_file = io.BytesIO(PNG_BYTES)
        second_upload_file = io.BytesIO(PNG_BYTES)
        client_content_type = "image/png;" + ("a" * 256)
        mapping_content_type = "image/png;" + ("b" * 512)
        client_typed_upload = ValidatedUpload(
            filename="source.png",
            content_type=client_content_type,
            size=len(PNG_BYTES),
            file=upload_file,
        )
        mapping_typed_upload = ValidatedUpload(
            filename="source-without-type.png",
            content_type=None,
            size=len(PNG_BYTES),
            file=second_upload_file,
        )
        route = OperationRoute(
            provider="nvidia",
            model="image-model",
            target_path="/images/edits",
            request_format="nvidia_genai_json",
            request_mapping={
                "fields": {
                    "prompt": "prompt",
                    "image": {
                        "from": "images",
                        "transform": "to_data_url_list",
                        "content_type": mapping_content_type,
                    },
                    "image_copy": {
                        "from": "images",
                        "transform": "to_data_url_list",
                        "content_type": mapping_content_type,
                    },
                }
            },
        )
        payload = {
            "model": "gateway/model",
            "prompt": "Edit",
            "images": [client_typed_upload, mapping_typed_upload],
        }

        weight = estimate_image_request_processing_weight(
            payload,
            route,
            "images_edits",
            source_transport="multipart",
        )
        prepared = build_downstream_image_request(
            payload,
            route,
            "images_edits",
            source_transport="multipart",
        )

        client_encoded_size = (
            _base64_encoded_size(len(PNG_BYTES))
            + max(
                IMAGE_DATA_URL_OVERHEAD_BYTES,
                len(f"data:{client_content_type};base64,".encode("utf-8")),
            )
        )
        mapping_encoded_size = (
            _base64_encoded_size(len(PNG_BYTES))
            + max(
                IMAGE_DATA_URL_OVERHEAD_BYTES,
                len(f"data:{mapping_content_type};base64,".encode("utf-8")),
            )
        )
        self.assertEqual(
            weight,
            (2 * (client_encoded_size + mapping_encoded_size))
            + len(PNG_BYTES)
            + mapping_encoded_size,
        )
        self.assertTrue(
            prepared.json_payload["image"][0].startswith(
                f"data:{client_content_type};base64,"
            )
        )
        self.assertTrue(
            prepared.json_payload["image"][1].startswith(
                f"data:{mapping_content_type};base64,"
            )
        )
        self.assertEqual(
            prepared.json_payload["image_copy"],
            prepared.json_payload["image"],
        )
        self.assertEqual(upload_file.tell(), 0)
        self.assertEqual(second_upload_file.tell(), 0)

    def test_post_images_edits_multipart_works_with_real_async_httpx_client(self):
        captured_request: dict[str, object] = {}

        def downstream_handler(request: httpx.Request) -> httpx.Response:
            captured_request["url"] = str(request.url)
            captured_request["content_type"] = request.headers.get("content-type")
            captured_request["body"] = request.content
            return httpx.Response(
                200,
                json={
                    "created": 456,
                    "data": [{"b64_json": "edited-image"}],
                    "usage": {"input_tokens": 3, "output_tokens": 4, "total_tokens": 7},
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
                    "/v1/images/edits",
                    data={
                        "model": "gateway/image-edit",
                        "prompt": "Replace the sky with sunset clouds",
                    },
                    files={
                        "image": ("source.png", io.BytesIO(PNG_BYTES), "image/png"),
                    },
                    headers={"Authorization": "Bearer test-gateway-key"},
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["data"], [{"b64_json": "edited-image"}])
        self.assertEqual(captured_request["url"], "https://openai.example/v1/images/edits")
        self.assertIn("multipart/form-data", str(captured_request["content_type"]))
        body = captured_request["body"]
        self.assertIsInstance(body, bytes)
        self.assertIn(b'name=\"model\"', body)
        self.assertIn(b'gpt-image-1', body)
        self.assertIn(b'name=\"prompt\"', body)
        self.assertIn(b'Replace the sky with sunset clouds', body)
        self.assertIn(b'name=\"image[]\"', body)
        self.assertIn(b'name=\"input_fidelity\"', body)

    def test_post_images_generations_supports_nvidia_mapping_and_normalizes_artifacts_response(self):
        downstream_payload = {
            "artifacts": [{"base64": "nvidia-image-bytes"}],
            "usage": {"input_tokens": 5, "output_tokens": 7, "total_tokens": 12},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
            self.assertIsNotNone(dispatcher.lookup_route("images_generations", "gateway/nvidia-image-gen"))

            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "gateway/nvidia-image-gen",
                    "prompt": "Draw a neon city",
                    "size": "1536x1024",
                    "seed": 7,
                    "n": 2,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertIsInstance(response_json.get("created"), int)
        self.assertEqual(response_json["data"], [{"b64_json": "nvidia-image-bytes"}])
        self.assertEqual(response_json["usage"], downstream_payload["usage"])
        fake_http_client.post.assert_awaited_once()
        self.assertEqual(
            fake_http_client.post.await_args.args[0],
            "https://ai.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b",
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["json"],
            {
                "prompt": "Draw a neon city",
                "seed": 7,
                "width": 1536,
                "height": 1024,
                "steps": 4,
            },
        )
        self.assertEqual(
            fake_http_client.post.await_args.kwargs["headers"],
            {
                "Content-Type": "application/json",
                "Authorization": "Bearer NVIDIA-KEY",
            },
        )

    def test_post_images_generations_rejects_unsupported_fields_for_nvidia_mapping(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/generations",
                json={
                    "model": "gateway/nvidia-image-gen",
                    "prompt": "Draw a neon city",
                    "quality": "high",
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported image request fields", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_post_images_edits_json_can_map_nvidia_example_reference(self):
        downstream_payload = {
            "artifacts": [{"base64": "edited-nvidia-image"}],
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/nvidia-image-edit",
                    "prompt": "Replace the sky with aurora",
                    "images": ["data:image/png;example_id,1"],
                    "seed": 11,
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        response_json = response.json()
        self.assertIsInstance(response_json.get("created"), int)
        self.assertEqual(response_json["data"], [{"b64_json": "edited-nvidia-image"}])
        post_kwargs = fake_http_client.post.await_args.kwargs
        self.assertNotIn("files", post_kwargs)
        self.assertEqual(
            fake_http_client.post.await_args.args[0],
            "https://integrate.api.nvidia.com/v1/genai/black-forest-labs/flux.2-klein-4b",
        )
        self.assertEqual(post_kwargs["headers"]["Authorization"], "Bearer NVIDIA-KEY")
        self.assertEqual(post_kwargs["json"]["prompt"], "Replace the sky with aurora")
        self.assertEqual(post_kwargs["json"]["seed"], 11)
        self.assertEqual(post_kwargs["json"]["steps"], 4)
        self.assertEqual(post_kwargs["json"]["image"], "data:image/png;example_id,1")

    def test_post_images_edits_multipart_rejects_uploaded_files_for_nvidia_preview_route(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/nvidia-image-edit",
                    "prompt": "Replace the sky with aurora",
                },
                files={
                    "image": ("source.png", io.BytesIO(PNG_BYTES), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("supports only predefined example images", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_post_images_edits_rejects_multiple_images_for_nvidia_preview_route(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                json={
                    "model": "gateway/nvidia-image-edit",
                    "prompt": "Blend both references into one scene",
                    "images": [
                        "data:image/png;example_id,0",
                        "data:image/png;example_id,1",
                    ],
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("must contain exactly one image", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()

    def test_post_images_edits_rejects_unmapped_mask_for_nvidia_route(self):
        with self._client(_FakeDownstreamResponse({"unused": True})) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/nvidia-image-edit",
                    "prompt": "Replace the sky with aurora",
                },
                files={
                    "image": ("source.png", io.BytesIO(PNG_BYTES), "image/png"),
                    "mask": ("mask.png", io.BytesIO(MASK_PNG_BYTES), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported image request fields", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
