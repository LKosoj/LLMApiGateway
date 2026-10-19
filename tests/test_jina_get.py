from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import web
from llm_gateway_core.services.active_requests import operation_from_path
from tests._async_compat import run_async
from tests.test_web_accounting import (
    READ_MODEL,
    _install_successful_commit,
    _request,
    _send_response,
)


EXTRACT = "/v1/jina/extract"
SEARCH = "/v1/jina/search"
URL = "https://example.com/article"
ARTICLE = {"title": "Article", "url": URL, "content": "# Content"}
RESULT = {"title": "Article", "url": URL, "snippet": "Description"}


def _get_request(path, *, query=(), path_params=None, headers=None):
    request, services = _request(path, method="GET")
    header_values = {"accept": "application/json"}
    header_values.update({key.lower(): value for key, value in (headers or {}).items()})
    request.scope["headers"] = [
        (key.encode(), value.encode()) for key, value in header_values.items()
    ]
    request.scope["query_string"] = urlencode(query, doseq=True).encode()
    request.scope["path_params"] = path_params or {}
    _install_successful_commit(services.accounting_service)
    return request, services


def test_jina_extract_get_query_returns_json_and_charges_once():
    async def scenario():
        request, services = _get_request(EXTRACT, query=[("url", URL)])
        with patch.object(web, "_read_with_model", AsyncMock(return_value=ARTICLE)) as read:
            response = await web.jina_extract(request)
        read.assert_awaited_once_with(request, read_model=READ_MODEL, url=URL, output_format="markdown")
        assert json.loads(response.body)["data"] == {**ARTICLE, "description": ""}
        await _send_response(request, response)
        services.accounting_service.commit.assert_awaited_once()
        services.accounting_service.release.assert_not_awaited()

    run_async(scenario())


def test_jina_extract_get_path_preserves_signed_query_bytes():
    async def scenario():
        request, _services = _get_request(
            "/v1/jina/extract/{url}",
            path_params={"url": URL},
        )
        request.scope["query_string"] = b"sig=a%26b&part=%2F&sig=second&model=page-model"
        with patch.object(web, "_read_with_model", AsyncMock(return_value=ARTICLE)) as read:
            response = await web.jina_extract(request)
        read.assert_awaited_once_with(
            request,
            read_model=READ_MODEL,
            url=f"{URL}?sig=a%26b&part=%2F&sig=second&model=page-model",
            output_format="markdown",
        )
        assert response.status_code == 200

    run_async(scenario())


def test_jina_search_get_path_keeps_unicode_query_repeat_sites_and_text_format():
    async def scenario():
        request, services = _get_request(
            "/v1/jina/search/{query}",
            path_params={"query": "что такое API"},
            query=[("site", "example.com"), ("site", "other.test"), ("hl", "ru"), ("respondWith", "text")],
            headers={"accept": "text/plain"},
        )
        search = AsyncMock(return_value=[RESULT])
        read = AsyncMock(return_value={"title": "Article", "url": URL, "content": "Content"})
        with patch.object(web, "_search_with_model", search), patch.object(web, "_read_with_model", read):
            response = await web.jina_search(request)
        assert search.await_args.kwargs["query"] == "что такое API"
        assert search.await_args.kwargs["language"] == "ru"
        assert search.await_args.kwargs["include_domains"] == ["example.com", "other.test"]
        assert response.body == b"[1] Title: Article\n[1] URL Source: https://example.com/article\n[1] Description: Description\n\nContent\n"
        await _send_response(request, response)
        services.accounting_service.commit.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize(
    ("path", "path_params", "query", "endpoint", "reader"),
    [
        (EXTRACT, {}, [], web.jina_extract, "read"),
        ("/v1/jina/extract/{url}", {}, [], web.jina_extract, "read"),
        (SEARCH, {}, [], web.jina_search, "search"),
        ("/v1/jina/search/{query}", {}, [], web.jina_search, "search"),
    ],
)
def test_jina_get_missing_input_releases_without_work(path, path_params, query, endpoint, reader):
    async def scenario():
        request, services = _get_request(path, path_params=path_params, query=query)
        with (
            patch.object(web, "_read_with_model", AsyncMock()) as read,
            patch.object(web, "_search_with_model", AsyncMock()) as search,
            pytest.raises(HTTPException),
        ):
            await endpoint(request)
        (read if reader == "read" else search).assert_not_awaited()
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize(
    ("path", "path_params", "query", "endpoint", "expected_operation"),
    [
        ("/v1/jina/extract/{url}", {"url": URL}, [], web.jina_extract, "web_read"),
        ("/v1/jina/search/{query}", {"query": "topic"}, [], web.jina_search, "web_search"),
    ],
)
def test_jina_get_dynamic_path_maps_to_operation(path, path_params, query, endpoint, expected_operation):
    async def scenario():
        request, _services = _get_request(path, path_params=path_params, query=query)
        if expected_operation == "web_read":
            patcher = patch.object(web, "_read_with_model", AsyncMock(return_value=ARTICLE))
        else:
            patcher = patch.object(web, "_search_with_model", AsyncMock(return_value=[]))
        with patcher:
            await endpoint(request)
        actual_path = "/v1/jina/extract/https://example.com/article" if expected_operation == "web_read" else "/v1/jina/search/topic"
        assert operation_from_path(actual_path) == expected_operation

    run_async(scenario())
