import asyncio
import base64
import io
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi import HTTPException
from PIL import Image

from llm_gateway_core.api.v1.image_dimensions import image_dimensions_match
from tests import test_images_endpoint as endpoint_support


def encoded_image(width, height, image_format="PNG"):
    output = io.BytesIO()
    Image.new("RGB", (width, height)).save(output, format=image_format)
    return base64.b64encode(output.getvalue()).decode()


@pytest.fixture
def endpoint():
    instance = endpoint_support.ImagesEndpointTests()
    instance.setUp()
    try:
        yield instance
    finally:
        instance.tearDown()


@pytest.mark.parametrize("transport", ["generation", "json_edit", "multipart_edit"])
@pytest.mark.parametrize("failures", [0, 1, 2, 3])
def test_image_size_retries(endpoint, transport, failures):
    wrong = {"data": [{"b64_json": encoded_image(64, 32)}]}
    correct = {"data": [{"b64_json": encoded_image(32, 64)}]}
    responses = [endpoint_support._FakeDownstreamResponse(wrong) for _ in range(failures)]
    responses.append(endpoint_support._FakeDownstreamResponse(correct))
    with endpoint._client(responses) as (client, _, upstream):
        payload = {"model": "gateway/image-gen", "prompt": "Draw a tree", "size": "32x64"}
        kwargs = {"headers": {"Authorization": "Bearer test-gateway-key"}}
        path = "/v1/images/generations"
        if transport != "generation":
            path = "/v1/images/edits"
            payload["model"] = "gateway/image-edit"
        if transport == "json_edit":
            payload["images"] = [{"b64_json": encoded_image(32, 64)}]
        if transport == "multipart_edit":
            kwargs.update(data=payload, files={"image": ("input.png", endpoint_support.PNG_BYTES, "image/png")})
        else:
            kwargs["json"] = payload
        response = client.post(path, **kwargs)
    assert upstream.post.await_count == min(failures + 1, 3)
    assert response.status_code == (502 if failures == 3 else 200)
    if failures < 3:
        assert response.json() == correct
    else:
        assert "after 3 attempts" in response.json()["detail"]


@pytest.mark.parametrize("size", [None, "auto"])
def test_no_exact_size_does_not_inspect_response(endpoint, size):
    payload = {"model": "gateway/image-gen", "prompt": "Draw a tree"}
    if size is not None:
        payload["size"] = size
    result = {"data": [{"url": "https://example.com/image.png"}]}
    with endpoint._client(endpoint_support._FakeDownstreamResponse(result)) as (client, _, upstream):
        upstream.get.reset_mock()
        response = client.post("/v1/images/generations", json=payload,
                               headers={"Authorization": "Bearer test-gateway-key"})
    assert response.status_code == 200
    upstream.post.assert_awaited_once()
    upstream.get.assert_not_called()


@pytest.mark.parametrize("image_format", ["PNG", "JPEG", "WEBP"])
def test_checks_actual_image_bytes_and_every_image(image_format):
    payload = {"data": [{"b64_json": encoded_image(32, 64, image_format)},
                        {"b64_json": encoded_image(64, 32, image_format)}]}
    assert asyncio.run(image_dimensions_match(payload, (32, 64), AsyncMock())) is False


def test_reads_image_url_without_provider_auth():
    url = "https://example.com/image.png"
    upstream = AsyncMock()
    upstream.get.return_value = httpx.Response(
        200, content=base64.b64decode(encoded_image(32, 64)), request=httpx.Request("GET", url),
    )
    assert asyncio.run(image_dimensions_match({"data": [{"url": url}]}, (32, 64), upstream))
    upstream.get.assert_awaited_once_with(url, follow_redirects=True)


@pytest.mark.parametrize("payload", [{"data": []}, {"data": [{"b64_json": "broken"}]}])
def test_unreadable_image_is_an_explicit_error(payload):
    with pytest.raises(HTTPException) as exc:
        asyncio.run(image_dimensions_match(payload, (32, 64), AsyncMock()))
    assert exc.value.status_code == 502


def test_image_url_is_downloaded_on_each_size_attempt(endpoint):
    url = "https://example.com/image.png"
    payload = {"data": [{"url": url}]}
    with endpoint._client(endpoint_support._FakeDownstreamResponse(payload)) as (client, _, upstream):
        upstream.get = AsyncMock(side_effect=[
            httpx.Response(200, content=base64.b64decode(encoded_image(width, height)),
                           request=httpx.Request("GET", url))
            for width, height in [(64, 32), (32, 64)]
        ])
        response = client.post(
            "/v1/images/generations",
            json={"model": "gateway/image-gen", "prompt": "Draw a tree", "size": "32x64"},
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        assert upstream.get.await_count == 2
        assert upstream.post.await_count == 2
    assert response.status_code == 200
    assert response.json() == payload


@pytest.mark.parametrize("size", ["bad", "0x64", 32])
def test_invalid_exact_size_fails_before_generation(endpoint, size):
    with endpoint._client(endpoint_support._FakeDownstreamResponse({})) as (client, _, upstream):
        response = client.post(
            "/v1/images/generations",
            json={"model": "gateway/image-gen", "prompt": "Draw a tree", "size": size},
            headers={"Authorization": "Bearer test-gateway-key"},
        )
        upstream.post.assert_not_awaited()
    assert response.status_code == 400
