from __future__ import annotations

import asyncio
import json
from unittest.mock import AsyncMock, patch
from urllib.parse import urlencode

import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1 import web
from tests._async_compat import run_async
from tests.test_web_accounting import (
    READ_MODEL,
    SEARCH_MODEL,
    _install_successful_commit,
    _request,
    _send_response,
)


PATH = "/v1/jina/search"
URL = "https://example.com/article"
OTHER_URL = "https://other.test/page"
THIRD_URL = "https://example.com/third"
SEARCH_RESULTS = [
    {"title": "First", "url": URL, "snippet": "First description"},
    {"title": "Second", "url": OTHER_URL, "snippet": "Second description"},
]


def _jina_request(payload, *, headers=None, form=False):
    request, services = _request(PATH)
    request.scope["headers"] = [
        (b"content-type", b"application/x-www-form-urlencoded" if form else b"application/json"),
        *((key.encode(), value.encode()) for key, value in (headers or {}).items()),
    ]
    request._body = (urlencode(payload, doseq=True) if form else json.dumps(payload)).encode()
    _install_successful_commit(services.accounting_service)
    return request, services


def _search_mock(results=SEARCH_RESULTS):
    return AsyncMock(return_value=results)


@pytest.mark.parametrize("form", [False, True])
@pytest.mark.parametrize("as_json", [False, True])
def test_jina_search_returns_jina_json_or_text(form, as_json):
    async def scenario():
        request, services = _jina_request(
            {"q": "topic"},
            headers={"accept": "application/json"} if as_json else {},
            form=form,
        )
        search = _search_mock()
        read = AsyncMock(side_effect=lambda _request, *, url, **_kwargs: {
            "title": next(item["title"] for item in SEARCH_RESULTS if item["url"] == url),
            "url": url,
            "content": f"Content for {url}",
        })
        with (
            patch.object(web, "_search_with_model", search),
            patch.object(web, "_read_with_model", read),
            patch.object(web, "enforce_virtual_key_access") as access,
        ):
            response = await web.jina_search(request)

        assert search.await_args.kwargs["search_model"] == SEARCH_MODEL
        assert search.await_args.kwargs["query"] == "topic"
        assert search.await_args.kwargs["num_queries"] == 1
        assert search.await_args.kwargs["expand_query"] is False
        assert [call.kwargs["url"] for call in read.await_args_list] == [URL, OTHER_URL]
        assert access.call_args_list == [
            ((request, SEARCH_MODEL),),
            ((request, READ_MODEL),),
        ]
        if as_json:
            assert json.loads(response.body) == {
                "code": 200,
                "status": 20000,
                "data": [
                    {"title": "First", "url": URL, "description": "First description", "content": f"Content for {URL}"},
                    {"title": "Second", "url": OTHER_URL, "description": "Second description", "content": f"Content for {OTHER_URL}"},
                ],
            }
        else:
            assert response.body.decode() == (
                f"[1] Title: First\n[1] URL Source: {URL}\n[1] Description: First description\n\nContent for {URL}\n\n"
                f"[2] Title: Second\n[2] URL Source: {OTHER_URL}\n[2] Description: Second description\n\nContent for {OTHER_URL}\n"
            )
        await _send_response(request, response)
        services.accounting_service.commit.assert_awaited_once()

    run_async(scenario())


def test_jina_search_header_format_has_priority_and_explicit_models_are_used():
    async def scenario():
        request, _services = _jina_request(
            {"q": "topic", "model": SEARCH_MODEL, "read_model": READ_MODEL, "respondWith": "markdown"},
            headers={"x-respond-with": "text"},
        )
        search = _search_mock([SEARCH_RESULTS[0]])
        read = AsyncMock(return_value={"title": "First", "url": URL, "content": "plain"})
        with (
            patch.object(web, "_search_with_model", search),
            patch.object(web, "_read_with_model", read),
        ):
            response = await web.jina_search(request)
        assert read.await_args.kwargs["output_format"] == "text"
        assert response.body == b"[1] Title: First\n[1] URL Source: https://example.com/article\n[1] Description: First description\n\nplain\n"

    run_async(scenario())


@pytest.mark.parametrize("count_field", ["num", "count"])
@pytest.mark.parametrize("form", [False, True])
def test_jina_search_applies_count_and_site_filter_before_reading(count_field, form):
    async def scenario():
        site = ["example.com", "example.com"] if form else ["example.com"]
        payload = {"q": "topic", count_field: "2", "site": site}
        request, _services = _jina_request(payload, form=form)
        search = _search_mock([*SEARCH_RESULTS, {"title": "Third", "url": THIRD_URL, "snippet": "Third description"}])
        read = AsyncMock(return_value={"title": "First", "url": URL, "content": "content"})
        with patch.object(web, "_search_with_model", search), patch.object(web, "_read_with_model", read):
            await web.jina_search(request)
        assert search.await_args.kwargs["max_results"] > 1
        assert [call.kwargs["url"] for call in read.await_args_list] == [URL, THIRD_URL]

    run_async(scenario())


@pytest.mark.parametrize(("payload", "expected_urls"), [
    ({"q": "topic", "num": "1", "count": "2"}, [URL]),
    ({"q": "topic", "count": "2"}, [URL, OTHER_URL]),
])
def test_jina_search_num_takes_priority_over_count(payload, expected_urls):
    async def scenario():
        request, _services = _jina_request(payload)
        search = _search_mock()
        read = AsyncMock(side_effect=lambda _request, *, url, **_kwargs: {"title": url, "url": url, "content": url})
        with patch.object(web, "_search_with_model", search), patch.object(web, "_read_with_model", read):
            await web.jina_search(request)
        assert [call.kwargs["url"] for call in read.await_args_list] == expected_urls

    run_async(scenario())


def test_jina_search_preserves_search_order_when_reads_finish_out_of_order():
    async def scenario():
        request, _services = _jina_request({"q": "topic"}, headers={"accept": "application/json"})
        async def read(_request, *, url, **_kwargs):
            await asyncio.sleep(0.01 if url == URL else 0)
            return {"title": url, "url": url, "content": url}

        with patch.object(web, "_search_with_model", _search_mock()), patch.object(web, "_read_with_model", read):
            response = await web.jina_search(request)
        assert [item["url"] for item in json.loads(response.body)["data"]] == [URL, OTHER_URL]

    run_async(scenario())


@pytest.mark.parametrize("payload", [{}, {"q": ""}])
def test_jina_search_requires_query_without_search_or_charge(payload):
    async def scenario():
        request, services = _jina_request(payload)
        search = AsyncMock()
        with patch.object(web, "_search_with_model", search), pytest.raises(HTTPException):
            await web.jina_search(request)
        search.assert_not_awaited()
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_jina_search_empty_results_returns_empty_jina_data():
    async def scenario():
        request, _services = _jina_request({"q": "nothing"}, headers={"accept": "application/json"})
        with patch.object(web, "_search_with_model", _search_mock([])), patch.object(web, "_read_with_model", AsyncMock()) as read:
            response = await web.jina_search(request)
        assert json.loads(response.body) == {"code": 200, "status": 20000, "data": []}
        read.assert_not_awaited()

    run_async(scenario())


@pytest.mark.parametrize("error", [HTTPException(status_code=503, detail="failed"), asyncio.CancelledError()])
def test_jina_search_search_error_releases_without_charge(error):
    async def scenario():
        request, services = _jina_request({"q": "topic"})
        with patch.object(web, "_search_with_model", AsyncMock(side_effect=error)), pytest.raises(type(error)):
            await web.jina_search(request)
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize("error", [HTTPException(status_code=503, detail="failed"), asyncio.CancelledError()])
def test_jina_search_read_error_releases_without_charge(error):
    async def scenario():
        request, services = _jina_request({"q": "topic"})
        with (
            patch.object(web, "_search_with_model", _search_mock([SEARCH_RESULTS[0]])),
            patch.object(web, "_read_with_model", AsyncMock(side_effect=error)),
            pytest.raises(type(error)),
        ):
            await web.jina_search(request)
        services.accounting_service.commit.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


def test_jina_search_acl_denial_is_preserved_for_search_and_read():
    async def scenario():
        for denied_model in (SEARCH_MODEL, READ_MODEL):
            request, services = _jina_request({"q": "topic"})
            calls = []

            def deny(_request, model):
                calls.append(model)
                if model == denied_model:
                    raise HTTPException(status_code=403, detail="denied")

            with patch.object(web, "enforce_virtual_key_access", side_effect=deny), patch.object(
                web, "_search_with_model", _search_mock([SEARCH_RESULTS[0]])
            ), patch.object(web, "_read_with_model", AsyncMock(return_value={"title": "First", "url": URL, "content": "content"})):
                with pytest.raises(HTTPException) as exc:
                    await web.jina_search(request)
            assert exc.value.status_code == 403
            assert calls == ([SEARCH_MODEL] if denied_model == SEARCH_MODEL else [SEARCH_MODEL, READ_MODEL])
            services.accounting_service.release.assert_awaited_once()

    run_async(scenario())


@pytest.mark.parametrize("section", [web.WEB_SEARCH_SECTION, web.WEB_READ_SECTION])
def test_jina_search_requires_explicit_models_when_multiple_services_configured(section):
    async def scenario():
        request, services = _jina_request({"q": "topic"})
        request.state.runtime_snapshot.config_loader.operation_rules[section]["another-model"] = {}
        with patch.object(web, "_search_with_model", AsyncMock()) as search, pytest.raises(HTTPException, match="multiple"):
            await web.jina_search(request)
        search.assert_not_awaited()
        services.accounting_service.release.assert_awaited_once()

    run_async(scenario())
