from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import web
from llm_gateway_core.services.accounting import DEFAULT_OPERATION_COST_USD
from llm_gateway_core.services.active_requests import operation_from_path
from tests._async_compat import run_async
from tests.test_web_accounting import READ_MODEL, _install_successful_commit, _request, _send_response


PATH = "/v1/jina/extract"
URL = "https://example.com/article"
ARTICLE = {"url": URL, "title": "Article", "content": "# Content", "images": [{"url": "https://example.com/a.png"}]}


def _jina_request(payload, *, headers=None, form=False):
    request, services = _request(PATH)
    request.scope["headers"] = [
        (b"content-type", b"application/x-www-form-urlencoded" if form else b"application/json"),
        *((key.encode(), value.encode()) for key, value in (headers or {}).items()),
    ]
    request._body = (urlencode(payload) if form else json.dumps(payload)).encode()
    _install_successful_commit(services.accounting_service)
    return request, services


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_jina_extract_default_model_and_response_formats(form, as_json):
    async def scenario():
        request, services = _jina_request(
            {"url": URL}, headers={"accept": "application/json"} if as_json else {}, form=form,
        )
        with (
            patch.object(web, "_read_with_model", AsyncMock(return_value=ARTICLE)) as read,
            patch.object(web, "enforce_virtual_key_access") as access,
        ):
            response = await web.jina_extract(request)
        read.assert_awaited_once_with(request, read_model=READ_MODEL, url=URL, output_format="markdown")
        access.assert_called_once_with(request, READ_MODEL)
        if as_json:
            assert json.loads(response.body) == {
                "code": 200,
                "status": 20000,
                "data": {"title": "Article", "description": "", "url": URL, "content": "# Content"},
            }
            assert response.headers["content-type"].startswith("application/json")
        else:
            assert response.body.decode() == f"Title: Article\n\nURL Source: {URL}\n\nMarkdown Content:\n# Content"
            assert response.headers["content-type"] == "text/plain; charset=utf-8"
        await _send_response(request, response)
        services.accounting_service.commit.assert_awaited_once()
        event = services.accounting_service.commit.await_args.args[1]
        assert event.usage.cost == DEFAULT_OPERATION_COST_USD
        assert request.state.llmgateway_target_path == PATH
        assert operation_from_path(PATH) == "web_read"

    run_async(scenario())


@pytest.mark.parametrize("via_header", [False, True])
def test_jina_extract_selects_model_and_text_format(via_header):
    async def scenario():
        payload = {"url": URL, "model": READ_MODEL}
        if not via_header:
            payload["respondWith"] = "text"
        request, _ = _jina_request(payload, headers={"x-respond-with": "text"} if via_header else {})
        with patch.object(web, "_read_with_model", AsyncMock(return_value={**ARTICLE, "content": "Content"})) as read:
            response = await web.jina_extract(request)
        read.assert_awaited_once_with(request, read_model=READ_MODEL, url=URL, output_format="text")
        assert response.body == b"Content"

    run_async(scenario())


@pytest.mark.parametrize(
    "payload", [{}, {"url": "file:///etc/passwd"}, {"url": URL, "respondWith": "screenshot"}, {"url": URL, "model": "unknown"}],
)
def test_jina_extract_rejects_invalid_requests_without_reading_or_charge(payload):
    async def scenario():
        request, services = _jina_request(payload)
        with patch.object(web, "_read_with_model", AsyncMock()) as read:
            with pytest.raises(HTTPException):
                await web.jina_extract(request)
        read.assert_not_awaited()
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_jina_extract_requires_model_when_multiple_are_configured():
    async def scenario():
        request, services = _jina_request({"url": URL})
        request.state.runtime_snapshot.config_loader.operation_rules[web.WEB_READ_SECTION]["another-reader"] = {}
        with patch.object(web, "_read_with_model", AsyncMock()) as read:
            with pytest.raises(HTTPException, match="multiple"):
                await web.jina_extract(request)
        read.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_jina_extract_preserves_access_denial_and_reader_errors():
    async def scenario():
        for status in (403, 503):
            request, services = _jina_request({"url": URL})
            target = "enforce_virtual_key_access" if status == 403 else "_read_with_model"
            with patch.object(web, target, side_effect=HTTPException(status_code=status, detail="failure")):
                with pytest.raises(HTTPException) as exc:
                    await web.jina_extract(request)
            assert exc.value.status_code == status
            services.accounting_service.commit.assert_not_awaited()
            services.accounting_service.release.assert_awaited_once()

    run_async(scenario())
