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
from llm_gateway_core.services.request_handler import OperationDispatcher
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
                dispatcher = getattr(client.app.state, "operation_dispatcher", None)
                self.assertIsInstance(dispatcher, OperationDispatcher)
                self.assertIs(client.app.state.http_client, fake_http_client)
                yield client, dispatcher, fake_http_client

    def test_post_images_alias_uses_generation_route_and_records_usage(self):
        downstream_payload = {
            "created": 123,
            "data": [{"b64_json": "image-bytes"}],
            "usage": {"input_tokens": 10, "output_tokens": 20, "total_tokens": 30},
        }

        with self._client(_FakeDownstreamResponse(downstream_payload)) as (client, dispatcher, fake_http_client):
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
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()
        self.assertEqual(
            client.app.state.tokens_usage_db.insert_usage.call_args[0][0]["operation"],
            "images_generation",
        )

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
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()

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
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()

    def test_post_images_edits_multipart_falls_back_after_payload_too_large(self):
        fallback_payload = {
            "created": 458,
            "data": [{"b64_json": "fallback-edited-image"}],
        }

        with self._client(
            [
                _FakeDownstreamResponse({"error": {"message": "payload too large"}}, status_code=413),
                _FakeDownstreamResponse(fallback_payload),
            ]
        ) as (client, _dispatcher, fake_http_client):
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/image-edit",
                    "prompt": "Add a red border",
                },
                files={
                    "image": ("source.png", io.BytesIO(b"source-image"), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json(), fallback_payload)
        self.assertEqual(fake_http_client.post.await_count, 2)
        first_call, second_call = fake_http_client.post.await_args_list
        self.assertEqual(first_call.kwargs["data"]["model"], "gpt-image-1")
        self.assertEqual(second_call.kwargs["data"]["model"], "gpt-image-1-fallback")
        self.assertEqual(second_call.kwargs["data"]["input_fidelity"], "low")
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()

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
                            "b64_json": "c291cmNlLWltYWdl",
                            "content_type": "image/png",
                        }
                    ],
                    "mask": {
                        "filename": "mask.png",
                        "data_url": "data:image/png;base64,bWFzay1pbWFnZQ==",
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
        self.assertEqual(image_file, ("source.png", b"source-image", "image/png"))
        self.assertEqual(mask_file, ("mask.png", b"mask-image", "image/png"))

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
            response = client.post(
                "/v1/images/edits",
                data={
                    "model": "gateway/image-edit",
                    "prompt": "Add a red border",
                },
                files={
                    "image": ("source.png", io.BytesIO(b"source-image"), "image/png"),
                    "mask": ("mask.png", io.BytesIO(b"mask-image"), "image/png"),
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
        client.app.state.tokens_usage_db.insert_usage.assert_called_once()
        self.assertEqual(
            client.app.state.tokens_usage_db.insert_usage.call_args[0][0]["operation"],
            "images_edit",
        )

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
                        "image": ("source.png", io.BytesIO(b"source-image"), "image/png"),
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
                    "image": ("source.png", io.BytesIO(b"source-image"), "image/png"),
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
                    "image": ("source.png", io.BytesIO(b"source-image"), "image/png"),
                    "mask": ("mask.png", io.BytesIO(b"mask-image"), "image/png"),
                },
                headers={"Authorization": "Bearer test-gateway-key"},
            )

        self.assertEqual(response.status_code, 400)
        self.assertIn("Unsupported image request fields", response.json()["detail"])
        fake_http_client.post.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
