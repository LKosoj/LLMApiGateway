from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from functools import wraps
from typing import TYPE_CHECKING, Any, cast

from fastapi import APIRouter, HTTPException, Request, Response

from ..accounting_http import AccountingHttpUse, accounting_error_response
from ...config.settings import settings
from ...services.access_control import enforce_virtual_key_access
from ...services.accounting import (
    AccountingError,
    AccountingValidationError,
)
from ...services.active_requests import update_active_request_from_state
from ...services.deep_research_process import (
    DeepResearchCallbacks,
    DeepResearchProcessError,
)
from ...services.deep_research_protocol import (
    DeepResearchJob,
)
from .deep_research_accounting import (
    DeepResearchTerminalOwner,
    take_deep_research_terminal_owner,
)
from .operation_proxy import (
    json_response,
    read_json_request_body,
    request_duration_ms,
)
from .operation_accounting import (
    finalize_buffered_operation,
    release_operation_if_open,
    take_operation_terminal_owner,
)
from .web_evidence import (
    EVIDENCE_MODE_APPLIED,
    build_evidence_matrix,
    filter_articles_for_passed_evidence,
    insufficient_evidence_output,
)
from .web_content import (
    merge_image_items as _merge_image_items,
)

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices, RuntimeSnapshot


from .web_adapters import (
    WEB_DEEP_RESEARCH_OPERATION,
    WEB_DEEP_RESEARCH_SECTION,
    WEB_READ_OPERATION,
    WEB_READ_SECTION,
    WEB_RESEARCH_OPERATION,
    WEB_RESEARCH_SECTION,
    WEB_SEARCH_OPERATION,
    WEB_SEARCH_SECTION,
    _UsageAccumulator,
    _clamp_int,
    _filter_search_results,
    _get_model_config,
    _get_operation_runtime,
    _parse_domain_list,
    _parse_web_terminal_observation,
    _read_with_model,
    _require_config_model,
    _require_config_text,
    _resolve_service_model,
    _search_with_model,
    _set_web_service_usage_state,
    _normalize_output_format,
    _validate_image_size,
)  # noqa: F401
from .web_research_orchestration import (
    _analyze_articles,
    _analyze_evidence_matrix,
    _article_source_payload,
    _articles_with_original_content,
    _build_evidence_matrix_from_articles,
    _capture_deep_research_callback_context,
    _deep_research_report_language,
    _deep_research_reported_cost,
    _format_generated_images,
    _plan_evidence_matrix,
    _prepare_relevant_articles,
    _rerank_articles,
    _research_language_query_counts,
    _research_output_language,
    _run_deep_research_process,
    _run_with_client_disconnect_cancellation,
)  # noqa: F401
from .web_safe_fetch import (
    _validate_http_url,
)  # noqa: F401
from . import web_adapters as _web_adapters_owner
from . import web_extraction as _web_extraction_owner
from . import web_research_orchestration as _web_research_owner
from . import web_safe_fetch as _web_safe_fetch_owner

_ObservedTextModelResult = _web_adapters_owner._ObservedTextModelResult
_ObservedWebToolResult = _web_adapters_owner._ObservedWebToolResult
_READ_ADAPTERS = _web_adapters_owner._READ_ADAPTERS
_SEARCH_ADAPTERS = _web_adapters_owner._SEARCH_ADAPTERS
_build_web_operation_component = _web_adapters_owner._build_web_operation_component
_call_internal_text_model = _web_adapters_owner._call_internal_text_model
_format_read_result = _web_adapters_owner._format_read_result
_generate_queries = _web_adapters_owner._generate_queries
_read_jina = _web_adapters_owner._read_jina
_read_proxy = _web_adapters_owner._read_proxy
_read_tavily = _web_adapters_owner._read_tavily
_read_with_model_observed = _web_adapters_owner._read_with_model_observed
_read_zai = _web_adapters_owner._read_zai
_search_adapter_enabled = _web_adapters_owner._search_adapter_enabled
_search_jina = _web_adapters_owner._search_jina
_search_proxy = _web_adapters_owner._search_proxy
_search_tavily = _web_adapters_owner._search_tavily
_search_with_model_observed = _web_adapters_owner._search_with_model_observed
_search_zai = _web_adapters_owner._search_zai
_attempt_model_fallback_rule = _web_adapters_owner._attempt_model_fallback_rule

FREEDIUM_MIRROR_PREFIX = _web_extraction_owner.FREEDIUM_MIRROR_PREFIX
_abort_blocked_cloakbrowser_request = _web_extraction_owner._abort_blocked_cloakbrowser_request
_cloakbrowser_fetch = _web_extraction_owner._cloakbrowser_fetch
_cloakbrowser_launch_args = _web_extraction_owner._cloakbrowser_launch_args
_cloakbrowser_render_sync = _web_extraction_owner._cloakbrowser_render_sync
_direct_fetch_url_candidates = _web_extraction_owner._direct_fetch_url_candidates
_direct_http_fetch = _web_extraction_owner._direct_http_fetch
_extract_cloakbrowser_markdown = _web_extraction_owner._extract_cloakbrowser_markdown
_extract_text_with_selectolax = _web_extraction_owner._extract_text_with_selectolax
_is_freedium_url = _web_extraction_owner._is_freedium_url
_is_medium_url = _web_extraction_owner._is_medium_url
_title_from_html = _web_extraction_owner._title_from_html
_trafilatura_markdown = _web_extraction_owner._trafilatura_markdown

CLIENT_CLOSED_REQUEST_STATUS_CODE = _web_research_owner.CLIENT_CLOSED_REQUEST_STATUS_CODE
CLIENT_DISCONNECT_POLL_SECONDS = _web_research_owner.CLIENT_DISCONNECT_POLL_SECONDS
ARTICLE_RELEVANCE_MAX_TOKENS = _web_research_owner.ARTICLE_RELEVANCE_MAX_TOKENS
ARTICLE_RELEVANCE_THRESHOLD_CHARS = _web_research_owner.ARTICLE_RELEVANCE_THRESHOLD_CHARS
ARTICLE_RERANK_DOCUMENT_MAX_CHARS = _web_research_owner.ARTICLE_RERANK_DOCUMENT_MAX_CHARS
_article_rerank_document = _web_research_owner._article_rerank_document
_build_article_relevance_prompt = _web_research_owner._build_article_relevance_prompt
_deep_research_image_alt_text = _web_research_owner._deep_research_image_alt_text
_extract_relevant_article_content = _web_research_owner._extract_relevant_article_content

WEB_FETCH_BLOCKED_HOST_DETAIL = _web_safe_fetch_owner.WEB_FETCH_BLOCKED_HOST_DETAIL
WEB_FETCH_MAX_REDIRECTS = _web_safe_fetch_owner.WEB_FETCH_MAX_REDIRECTS
_PinnedHostAsyncHTTPTransport = _web_safe_fetch_owner._PinnedHostAsyncHTTPTransport
_PinnedHostNetworkBackend = _web_safe_fetch_owner._PinnedHostNetworkBackend
_ValidatedFetchUrl = _web_safe_fetch_owner._ValidatedFetchUrl
_get_pinned_public_url = _web_safe_fetch_owner._get_pinned_public_url
_get_with_public_redirects = _web_safe_fetch_owner._get_with_public_redirects
_is_blocked_fetch_ip = _web_safe_fetch_owner._is_blocked_fetch_ip
_resolve_fetch_host = _web_safe_fetch_owner._resolve_fetch_host
_validate_public_fetch_host = _web_safe_fetch_owner._validate_public_fetch_host
_validated_fetch_url = _web_safe_fetch_owner._validated_fetch_url

_append_images_to_markdown = _web_adapters_owner._append_images_to_markdown
logger = logging.getLogger(__name__)

router = APIRouter()

DEFAULT_MAX_RESULTS = 10
DEFAULT_DEEP_RESEARCH_WORDS = 2500
MAX_SEARCH_RESULTS = 20
DEFAULT_MAX_RESULTS_PER_LANG = 10
MAX_RESULTS_PER_LANG = 20
RESEARCH_DEFAULT_ARTICLES_PER_LANGUAGE = 8
MAX_RESEARCH_ARTICLES_PER_LANGUAGE = 10
MAX_DEEP_RESEARCH_WORDS = 8000
MAX_DEEP_RESEARCH_BREADTH = 10
MAX_DEEP_RESEARCH_DEPTH = 5
MAX_DEEP_RESEARCH_CONCURRENCY = 10


def _with_web_terminal_accounting(
    *,
    release_only: bool = False,
) -> Callable[
    [Callable[[Request], Awaitable[object]]],
    Callable[[Request], Awaitable[object]],
]:
    def decorate(
        endpoint: Callable[[Request], Awaitable[object]],
    ) -> Callable[[Request], Awaitable[object]]:
        @wraps(endpoint)
        async def wrapped(request: Request) -> object:
            try:
                owner = take_operation_terminal_owner(request)
            except AccountingError as exc:
                return accounting_error_response(
                    request,
                    exc,
                    use=AccountingHttpUse.ADMISSION,
                )

            started_at = time.monotonic()
            try:
                response = await endpoint(request)
                if release_only:
                    await release_operation_if_open(owner)
                    return response
                if not isinstance(response, Response):
                    raise AccountingValidationError
                gateway_model = getattr(request.state, "llmgateway_gateway_model", None)
                if not isinstance(gateway_model, str) or not gateway_model:
                    raise AccountingValidationError
                observation = _parse_web_terminal_observation(
                    owner,
                    gateway_model=gateway_model,
                    duration_ms=request_duration_ms(started_at),
                )
                return await finalize_buffered_operation(owner, response, observation)
            except AccountingError as exc:
                await release_operation_if_open(owner, primary_error=exc)
                return accounting_error_response(
                    request,
                    exc,
                    use=AccountingHttpUse.ADMISSION,
                )
            except BaseException as exc:
                await release_operation_if_open(owner, primary_error=exc)
                raise

        return wrapped

    return decorate


def _with_deep_research_terminal_accounting(
    endpoint: Callable[[Request], Awaitable[object]],
) -> Callable[[Request], Awaitable[object]]:
    @wraps(endpoint)
    async def wrapped(request: Request) -> object:
        try:
            owner = take_deep_research_terminal_owner(request)
        except AccountingError as exc:
            return accounting_error_response(
                request,
                exc,
                use=AccountingHttpUse.ADMISSION,
            )
        request.state.llmgateway_deep_research_terminal_owner = owner
        try:
            response = await endpoint(request)
            if not isinstance(response, Response) or not owner.is_ready:
                raise AccountingValidationError
            return response
        except AccountingError as exc:
            return accounting_error_response(
                request,
                exc,
                use=AccountingHttpUse.ADMISSION,
            )

    return wrapped


def _deep_research_terminal_owner(request: Request) -> DeepResearchTerminalOwner:
    owner = getattr(
        request.state,
        "llmgateway_deep_research_terminal_owner",
        None,
    )
    if not isinstance(owner, DeepResearchTerminalOwner):
        raise AccountingValidationError
    return owner


def _require_text(payload: dict, field_name: str) -> str:
    value = payload.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"Missing '{field_name}' in request body")
    return value.strip()


def _require_model(payload: dict) -> str:
    return _require_text(payload, "model")


def _payload_text(payload: dict, field_name: str) -> str | None:
    value = payload.get(field_name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=400, detail=f"'{field_name}' must be a non-empty string.")
    return value.strip()


def _bool_option(payload: dict, field_name: str, default: bool) -> bool:
    value = payload.get(field_name)
    if value is None:
        return default
    if not isinstance(value, bool):
        raise HTTPException(status_code=400, detail=f"'{field_name}' must be a boolean.")
    return value


def _raw_content_format(payload: dict) -> str | None:
    value = payload.get("include_raw_content")
    if value is None or value is False:
        return None
    if value is True:
        return "markdown"
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"true", "markdown"}:
            return "markdown"
        if normalized == "text":
            return "text"
        if normalized in {"false", ""}:
            return None
    raise HTTPException(
        status_code=400,
        detail="'include_raw_content' must be boolean, 'markdown', or 'text'.",
    )


async def _attach_raw_content_to_results(
    request: Request,
    *,
    results: list[dict[str, Any]],
    read_model: str,
    output_format: str,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    async def _read_result(result: dict[str, Any]) -> dict[str, Any]:
        enriched = dict(result)
        try:
            article = await _read_with_model(
                request,
                read_model=read_model,
                url=_validate_http_url(str(result.get("url") or "")),
                output_format=output_format,
            )
        except HTTPException as exc:
            logger.warning("Failed to attach raw content for %s: %s", result.get("url"), exc.detail)
            enriched["raw_content"] = None
            return enriched
        enriched["raw_content"] = article.get("content") or None
        if include_images:
            enriched["images"] = _merge_image_items(enriched.get("images"), article.get("images"))
        return enriched

    gathered = await asyncio.gather(
        *[_read_result(result) for result in results],
        return_exceptions=True,
    )
    enriched_results: list[dict[str, Any]] = []
    for result, item in zip(results, gathered):
        if isinstance(item, Exception):
            logger.warning(
                "Failed to attach raw content for %s: %s",
                result.get("url"),
                item,
            )
            fallback = dict(result)
            fallback["raw_content"] = None
            enriched_results.append(fallback)
            continue
        enriched_results.append(item)
    return enriched_results


def _tavily_rank_score(index: int) -> float:
    return max(0.0, round(1.0 - (index * 0.01), 6))


def _normalized_images(value: object) -> list[dict[str, str]]:
    return _merge_image_items(value)


def _with_images_field(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{**item, "images": _normalized_images(item.get("images"))} for item in items]


def _tavily_search_result(result: dict[str, Any], index: int, *, include_images: bool) -> dict[str, Any]:
    item = {
        "title": str(result.get("title") or ""),
        "url": str(result.get("url") or ""),
        "content": str(result.get("snippet") or result.get("content") or ""),
        "score": _tavily_rank_score(index),
        "raw_content": result.get("raw_content"),
    }
    if "favicon" in result:
        item["favicon"] = result.get("favicon")
    if include_images or "images" in result:
        item["images"] = _normalized_images(result.get("images"))
    return item


def _tavily_response_time(started_at: float) -> float:
    return round(time.monotonic() - started_at, 2)


def _collect_result_images(results: list[dict[str, Any]]) -> list[dict[str, str]]:
    images: list[Any] = []
    for result in results:
        images.extend(_normalized_images(result.get("images")))
    return _merge_image_items(images)


def _extract_tavily_urls(payload: dict) -> list[str]:
    value = payload.get("urls", payload.get("url"))
    if value is None:
        raise HTTPException(status_code=400, detail="Missing 'urls' in request body")
    if isinstance(value, str):
        urls = [value]
    elif isinstance(value, list):
        urls = value
    else:
        raise HTTPException(status_code=400, detail="'urls' must be a string or an array of strings.")
    if not urls:
        raise HTTPException(status_code=400, detail="'urls' must not be empty.")
    if len(urls) > 20:
        raise HTTPException(status_code=400, detail="'urls' must contain no more than 20 URLs.")
    normalized: list[str] = []
    for url in urls:
        if not isinstance(url, str):
            raise HTTPException(status_code=400, detail="'urls' must contain only strings.")
        normalized.append(_validate_http_url(url))
    return normalized


@router.post("/web/search")
@_with_web_terminal_accounting()
async def web_search(request: Request):
    payload = await read_json_request_body(request, "web search endpoint")
    requested_model = _require_model(payload)
    query = _require_text(payload, "query")
    max_results = _clamp_int(payload.get("max_results"), DEFAULT_MAX_RESULTS, 1, MAX_SEARCH_RESULTS)
    num_queries = _clamp_int(payload.get("num_queries"), 1, 1, 5)
    language = str(payload.get("language") or "ru")
    include_domains = _parse_domain_list(payload.get("include_domains"), "include_domains")
    exclude_domains = _parse_domain_list(payload.get("exclude_domains"), "exclude_domains")
    include_images = _bool_option(payload, "include_images", False)
    raw_content_format = _raw_content_format(payload)
    read_model = None

    enforce_virtual_key_access(request, requested_model)
    if raw_content_format is not None:
        _dispatcher, _http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
        read_model = _resolve_service_model(
            config_loader,
            WEB_READ_SECTION,
            _payload_text(payload, "read_model"),
            field_name="read_model",
        )
        enforce_virtual_key_access(request, read_model)

    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_SEARCH_OPERATION
    update_active_request_from_state(request)
    usage_accumulator = _UsageAccumulator()
    search_limit = MAX_SEARCH_RESULTS if include_domains or exclude_domains else max_results
    results = await _search_with_model(
        request,
        search_model=requested_model,
        query=query,
        max_results=search_limit,
        num_queries=num_queries,
        language=language,
        usage_accumulator=usage_accumulator,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        include_images=include_images,
    )
    results = _filter_search_results(
        results,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        max_results=max_results,
    )
    if raw_content_format is not None and read_model is not None:
        results = await _attach_raw_content_to_results(
            request,
            results=results,
            read_model=read_model,
            output_format=raw_content_format,
            include_images=include_images,
        )
    if include_images:
        results = _with_images_field(results)
    response_payload = {
        "object": "web_search",
        "model": requested_model,
        "query": query,
        "data": results,
        "usage": usage_accumulator.usage,
    }
    _set_web_service_usage_state(request, requested_model, "search")
    return json_response(response_payload, 200)


@router.post("/web/read")
@_with_web_terminal_accounting()
async def web_read(request: Request):
    payload = await read_json_request_body(request, "web read endpoint")
    requested_model = _require_model(payload)
    url = _validate_http_url(_require_text(payload, "url"))
    output_format = _normalize_output_format(payload.get("format"))
    include_images = _bool_option(payload, "include_images", False)

    enforce_virtual_key_access(request, requested_model)
    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_READ_OPERATION
    update_active_request_from_state(request)
    usage_accumulator = _UsageAccumulator()
    article = await _read_with_model(
        request,
        read_model=requested_model,
        url=url,
        output_format=output_format,
    )
    response_payload = {
        "object": "web_read",
        "model": requested_model,
        **article,
        "usage": usage_accumulator.usage,
    }
    if include_images:
        response_payload["images"] = _normalized_images(article.get("images"))
    else:
        response_payload.pop("images", None)
    _set_web_service_usage_state(request, requested_model, "read")
    return json_response(response_payload, 200)


@router.post("/tavily/search")
@_with_web_terminal_accounting()
async def tavily_search(request: Request):
    payload = await read_json_request_body(request, "Tavily-compatible search endpoint")
    query = _require_text(payload, "query")
    max_results = _clamp_int(payload.get("max_results"), 5, 1, MAX_SEARCH_RESULTS)
    language = str(payload.get("language") or "en")
    include_domains = _parse_domain_list(payload.get("include_domains"), "include_domains")
    exclude_domains = _parse_domain_list(payload.get("exclude_domains"), "exclude_domains")
    include_images = _bool_option(payload, "include_images", False)
    raw_content_format = _raw_content_format(payload)

    _dispatcher, _http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    requested_model = _resolve_service_model(
        config_loader,
        WEB_SEARCH_SECTION,
        _payload_text(payload, "model"),
        field_name="model",
    )
    read_model = None
    if raw_content_format is not None:
        read_model = _resolve_service_model(
            config_loader,
            WEB_READ_SECTION,
            _payload_text(payload, "read_model"),
            field_name="read_model",
        )

    enforce_virtual_key_access(request, requested_model)
    if read_model is not None:
        enforce_virtual_key_access(request, read_model)

    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_SEARCH_OPERATION
    update_active_request_from_state(request)
    usage_accumulator = _UsageAccumulator()
    started_at = time.monotonic()
    search_limit = MAX_SEARCH_RESULTS if include_domains or exclude_domains else max_results
    results = await _search_with_model(
        request,
        search_model=requested_model,
        query=query,
        max_results=search_limit,
        num_queries=1,
        language=language,
        usage_accumulator=usage_accumulator,
        expand_query=False,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        include_images=include_images,
    )
    results = _filter_search_results(
        results,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        max_results=max_results,
    )
    if raw_content_format is not None and read_model is not None:
        results = await _attach_raw_content_to_results(
            request,
            results=results,
            read_model=read_model,
            output_format=raw_content_format,
            include_images=include_images,
        )

    response_payload = {
        "query": query,
        "answer": None,
        "images": _collect_result_images(results) if include_images else [],
        "results": [
            _tavily_search_result(result, index, include_images=include_images) for index, result in enumerate(results)
        ],
        "failed_results": [],
        "response_time": _tavily_response_time(started_at),
        "usage": {"credits": 1 if results else 0},
        "request_id": str(uuid.uuid4()),
    }
    _set_web_service_usage_state(request, requested_model, "search", target_path="/v1/tavily/search")
    return json_response(response_payload, 200)


@router.post("/tavily/extract")
@_with_web_terminal_accounting()
async def tavily_extract(request: Request):
    payload = await read_json_request_body(request, "Tavily-compatible extract endpoint")
    urls = _extract_tavily_urls(payload)
    output_format = _normalize_output_format(payload.get("format"))
    include_images = _bool_option(payload, "include_images", False)

    _dispatcher, _http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    requested_model = _resolve_service_model(
        config_loader,
        WEB_READ_SECTION,
        _payload_text(payload, "model") or _payload_text(payload, "read_model"),
        field_name="model",
    )
    enforce_virtual_key_access(request, requested_model)

    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_READ_OPERATION
    update_active_request_from_state(request)
    started_at = time.monotonic()
    semaphore = asyncio.Semaphore(4)

    async def _extract_url(url: str) -> tuple[dict[str, Any] | None, dict[str, str] | None]:
        async with semaphore:
            try:
                article = await _read_with_model(
                    request,
                    read_model=requested_model,
                    url=url,
                    output_format=output_format,
                )
            except HTTPException as exc:
                logger.warning("Tavily-compatible extract failed for %s: %s", url, exc.detail)
                return None, {"url": url, "error": str(exc.detail)}
        item = {
            "url": article.get("url") or url,
            "raw_content": article.get("content") or "",
            "images": _normalized_images(article.get("images")) if include_images else [],
        }
        return item, None

    extracted = await asyncio.gather(
        *[_extract_url(url) for url in urls],
        return_exceptions=True,
    )
    results: list[dict[str, Any]] = []
    failed_results: list[dict[str, str]] = []
    for url, item in zip(urls, extracted):
        if isinstance(item, Exception):
            logger.warning("Tavily-compatible extract crashed for %s: %s", url, item)
            failed_results.append({"url": url, "error": str(item)})
            continue
        result, failed = item
        if result is not None:
            results.append(result)
        if failed is not None:
            failed_results.append(failed)
    response_payload = {
        "results": results,
        "failed_results": failed_results,
        "response_time": _tavily_response_time(started_at),
        "usage": {"credits": len(results)},
        "request_id": str(uuid.uuid4()),
    }
    _set_web_service_usage_state(request, requested_model, "read", target_path="/v1/tavily/extract")
    return json_response(response_payload, 200)


@router.post("/web/research")
@_with_web_terminal_accounting()
async def web_research(request: Request):
    payload = await read_json_request_body(request, "web research endpoint")
    requested_model = _require_model(payload)
    query = _require_text(payload, "query")
    max_results_per_lang_value = payload.get("max_results_per_lang", payload.get("max_results"))
    max_results_per_lang = _clamp_int(
        max_results_per_lang_value,
        DEFAULT_MAX_RESULTS_PER_LANG,
        1,
        MAX_RESULTS_PER_LANG,
    )
    max_articles_per_language = _clamp_int(
        payload.get("max_articles"),
        RESEARCH_DEFAULT_ARTICLES_PER_LANGUAGE,
        1,
        MAX_RESEARCH_ARTICLES_PER_LANGUAGE,
    )
    language_query_counts = _research_language_query_counts(payload.get("language"), payload.get("num_queries"))
    output_language = _research_output_language(payload.get("output_language"))
    output_format = _normalize_output_format(payload.get("format"))

    enforce_virtual_key_access(request, requested_model)
    _dispatcher, http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    research_config = _get_model_config(config_loader, WEB_RESEARCH_SECTION, requested_model)
    search_model = research_config.get("search_model")
    read_model = research_config.get("read_model")
    rerank_model = research_config.get("rerank_model")
    if not isinstance(search_model, str) or not search_model:
        raise HTTPException(status_code=500, detail=f"Research model '{requested_model}' has no search_model.")
    if not isinstance(read_model, str) or not read_model:
        raise HTTPException(status_code=500, detail=f"Research model '{requested_model}' has no read_model.")
    if not isinstance(rerank_model, str) or not rerank_model:
        raise HTTPException(status_code=500, detail=f"Research model '{requested_model}' has no rerank_model.")
    analysis_model = research_config.get("analysis_model")
    if not isinstance(analysis_model, str) or not analysis_model:
        raise HTTPException(status_code=500, detail=f"Research model '{requested_model}' has no analysis_model.")

    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_RESEARCH_OPERATION
    update_active_request_from_state(request)
    usage_accumulator = _UsageAccumulator()
    evidence_plan = await _run_with_client_disconnect_cancellation(
        request,
        WEB_RESEARCH_OPERATION,
        lambda: _plan_evidence_matrix(
            request,
            config_loader,
            http_client,
            analysis_model=analysis_model,
            query=query,
            usage_accumulator=usage_accumulator,
        ),
    )
    evidence_matrix: dict[str, Any] | None = None

    async def _search_languages() -> list[Any]:
        language_search_tasks = [
            _search_with_model(
                request,
                search_model=search_model,
                query=query,
                max_results=max_results_per_lang,
                num_queries=num_queries,
                language=language,
                usage_accumulator=usage_accumulator,
            )
            for language, num_queries in language_query_counts
        ]
        return await asyncio.gather(*language_search_tasks, return_exceptions=True)

    language_search_results = await _run_with_client_disconnect_cancellation(
        request,
        WEB_RESEARCH_OPERATION,
        _search_languages,
    )
    search_candidates: list[dict[str, Any]] = []
    first_search_error: Exception | None = None
    for (language, _num_queries), result in zip(language_query_counts, language_search_results):
        if isinstance(result, Exception):
            logger.warning("Web research search failed for language '%s': %s", language, result)
            if first_search_error is None:
                first_search_error = result
            continue
        search_candidates.extend({**item, "language": language} for item in result)
    if not search_candidates and first_search_error is not None:
        if isinstance(first_search_error, HTTPException):
            raise first_search_error
        raise HTTPException(status_code=503, detail=f"Web research search failed: {first_search_error}")

    async def _read_search_result(result: dict[str, Any]) -> dict[str, str] | None:
        try:
            article = await _read_with_model(
                request,
                read_model=read_model,
                url=_validate_http_url(str(result["url"])),
                output_format=output_format,
            )
            return {
                **article,
                "title": article.get("title") or str(result.get("title", "")),
                "language": str(result.get("language", "")),
                "snippet": str(result.get("snippet", "")),
            }
        except HTTPException as exc:
            logger.warning("Failed to read search result %s: %s", result.get("url"), exc.detail)
            return None

    if search_candidates:

        async def _read_articles() -> list[dict[str, str] | None | BaseException]:
            return await asyncio.gather(
                *[_read_search_result(result) for result in search_candidates],
                return_exceptions=True,
            )

        article_results = await _run_with_client_disconnect_cancellation(
            request,
            WEB_RESEARCH_OPERATION,
            _read_articles,
        )
        downloaded_articles: list[dict[str, str]] = []
        for candidate, article in zip(search_candidates, article_results):
            if isinstance(article, Exception):
                logger.warning(
                    "Failed to read search result %s: %s",
                    candidate.get("url"),
                    article,
                )
                continue
            if article is not None:
                downloaded_articles.append(article)
        if downloaded_articles:
            original_articles = downloaded_articles
            downloaded_articles = await _run_with_client_disconnect_cancellation(
                request,
                WEB_RESEARCH_OPERATION,
                lambda: _prepare_relevant_articles(
                    request,
                    config_loader,
                    http_client,
                    relevance_model=analysis_model,
                    query=query,
                    articles=downloaded_articles,
                    usage_accumulator=usage_accumulator,
                    fail_on_error=evidence_plan.get("mode") == EVIDENCE_MODE_APPLIED,
                ),
            )
        if evidence_plan.get("mode") == EVIDENCE_MODE_APPLIED:
            evidence_articles = _articles_with_original_content(
                downloaded_articles,
                original_articles if downloaded_articles else [],
            )
            evidence_matrix = await _run_with_client_disconnect_cancellation(
                request,
                WEB_RESEARCH_OPERATION,
                lambda: _build_evidence_matrix_from_articles(
                    request,
                    config_loader,
                    http_client,
                    analysis_model=analysis_model,
                    query=query,
                    evidence_plan=evidence_plan,
                    articles=evidence_articles,
                    usage_accumulator=usage_accumulator,
                ),
            )
            downloaded_articles = filter_articles_for_passed_evidence(downloaded_articles, evidence_matrix)
        articles_by_language: dict[str, list[dict[str, str]]] = {}
        for article in downloaded_articles:
            articles_by_language.setdefault(str(article.get("language", "")), []).append(article)

        async def _rerank_language_articles() -> list[list[dict[str, Any]] | BaseException]:
            rerank_tasks = []
            for language, _num_queries in language_query_counts:
                language_articles = articles_by_language.get(language, [])
                if not language_articles:
                    continue
                rerank_tasks.append(
                    _rerank_articles(
                        request,
                        rerank_model=rerank_model,
                        query=query,
                        articles=language_articles,
                        top_n=max_articles_per_language,
                        usage_accumulator=usage_accumulator,
                    )
                )
            return await asyncio.gather(*rerank_tasks, return_exceptions=True)

        articles: list[dict[str, Any]] = []
        reranked_article_groups = await _run_with_client_disconnect_cancellation(
            request,
            WEB_RESEARCH_OPERATION,
            _rerank_language_articles,
        )
        for result in reranked_article_groups:
            if isinstance(result, Exception):
                logger.warning("Web research rerank failed: %s", result)
                if isinstance(result, HTTPException):
                    raise result
                raise HTTPException(status_code=503, detail=f"Web research rerank failed: {result}")
            articles.extend(result)
        search_results = [_article_source_payload(article) for article in articles]
        if evidence_matrix is not None:
            analysis = await _run_with_client_disconnect_cancellation(
                request,
                WEB_RESEARCH_OPERATION,
                lambda: _analyze_evidence_matrix(
                    request,
                    config_loader,
                    http_client,
                    analysis_model=analysis_model,
                    query=query,
                    output_language=output_language,
                    evidence_matrix=evidence_matrix,
                    usage_accumulator=usage_accumulator,
                ),
            )
        else:
            analysis = await _run_with_client_disconnect_cancellation(
                request,
                WEB_RESEARCH_OPERATION,
                lambda: _analyze_articles(
                    request,
                    config_loader,
                    http_client,
                    analysis_model=analysis_model,
                    query=query,
                    output_language=output_language,
                    articles=articles,
                    usage_accumulator=usage_accumulator,
                ),
            )
    else:
        articles = []
        search_results = []
        if evidence_plan.get("mode") == EVIDENCE_MODE_APPLIED:
            evidence_matrix = build_evidence_matrix(evidence_plan, [])
            analysis = insufficient_evidence_output(output_language, evidence_matrix)
        else:
            analysis = "Не найдено релевантных статей по запросу."

    response_payload = {
        "object": "web_research",
        "model": requested_model,
        "query": query,
        "output_language": output_language,
        "sources": search_results,
        "articles": articles,
        "output": analysis,
        "usage": usage_accumulator.usage,
    }
    if evidence_matrix is not None:
        response_payload["evidence_matrix"] = evidence_matrix
    _set_web_service_usage_state(request, requested_model, "research")
    return json_response(response_payload, 200)


@router.post("/web/deep-research")
@_with_deep_research_terminal_accounting
async def web_deep_research(request: Request):
    payload = await read_json_request_body(request, "web deep research endpoint")
    requested_model = _require_model(payload)
    query = _require_text(payload, "query")
    max_words = _clamp_int(payload.get("max_words"), DEFAULT_DEEP_RESEARCH_WORDS, 200, MAX_DEEP_RESEARCH_WORDS)
    breadth = _clamp_int(payload.get("breadth"), 4, 1, MAX_DEEP_RESEARCH_BREADTH)
    depth = _clamp_int(payload.get("depth"), 2, 1, MAX_DEEP_RESEARCH_DEPTH)
    concurrency = _clamp_int(payload.get("concurrency"), 6, 1, MAX_DEEP_RESEARCH_CONCURRENCY)
    image_generation_enabled = _bool_option(payload, "image_generation", False)
    report_language = _deep_research_report_language(payload.get("language"))
    output_format = _normalize_output_format(payload.get("format"))
    services = cast("AppServices", request.app.state.services)
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)

    enforce_virtual_key_access(request, requested_model)
    _dispatcher, _http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    deep_research_config = _get_model_config(config_loader, WEB_DEEP_RESEARCH_SECTION, requested_model)
    search_model = _require_config_model(deep_research_config, "search_model", requested_model)
    read_model = _require_config_model(deep_research_config, "read_model", requested_model)
    fast_model = _require_config_model(deep_research_config, "fast_model", requested_model)
    smart_model = _require_config_model(deep_research_config, "smart_model", requested_model)
    strategic_model = _require_config_model(deep_research_config, "strategic_model", requested_model)
    embedding_model = deep_research_config.get("embedding_model")
    if embedding_model is not None and (not isinstance(embedding_model, str) or not embedding_model.strip()):
        raise HTTPException(status_code=500, detail=f"Model '{requested_model}' has invalid embedding_model.")
    image_generation_model = None
    image_generation_size = None
    if image_generation_enabled:
        image_generation_model = _require_config_model(
            deep_research_config,
            "image_generation_model",
            requested_model,
        )
        if image_generation_model not in config_loader.operation_rules.get("images_generations", {}):
            raise HTTPException(
                status_code=500,
                detail=(
                    f"Deep research model '{requested_model}' references image_generation_model "
                    f"'{image_generation_model}' which is not configured in images_generations."
                ),
            )
        image_generation_size = _validate_image_size(
            _require_config_text(deep_research_config, "image_generation_size", requested_model),
            requested_model,
        )
        enforce_virtual_key_access(request, image_generation_model)

    request.state.llmgateway_gateway_model = requested_model
    request.state.llmgateway_operation = WEB_DEEP_RESEARCH_OPERATION
    update_active_request_from_state(request)
    accounting_owner = _deep_research_terminal_owner(request)
    child_context_token = accounting_owner.begin(requested_model)
    job_id = uuid.uuid4().hex
    callback_context = _capture_deep_research_callback_context(
        request,
        services=services,
        runtime_snapshot=runtime_snapshot,
        accounting_owner=accounting_owner,
        job_id=job_id,
        child_context_token=child_context_token,
        search_model=search_model,
        read_model=read_model,
        output_format=output_format,
        image_generation_model=image_generation_model,
        image_generation_size=image_generation_size,
    )
    job = DeepResearchJob(
        job_id=job_id,
        query=query,
        fast_model=fast_model,
        smart_model=smart_model,
        strategic_model=strategic_model,
        embedding_model=embedding_model.strip() if isinstance(embedding_model, str) else None,
        gateway_base_url=f"http://127.0.0.1:{settings.gateway_port}/v1",
        gateway_api_key=child_context_token,
        max_words=max_words,
        breadth=breadth,
        depth=depth,
        concurrency=concurrency,
        language=report_language,
        image_generation_enabled=image_generation_enabled,
        image_generation_model=image_generation_model,
        image_generation_size=image_generation_size,
    )
    callbacks = DeepResearchCallbacks(handle=callback_context.handle)
    try:
        result = await _run_with_client_disconnect_cancellation(
            request,
            WEB_DEEP_RESEARCH_OPERATION,
            lambda: _run_deep_research_process(
                services.deep_research_process_runner,
                job,
                callbacks,
            ),
        )
    except HTTPException:
        raise
    except DeepResearchProcessError as exc:
        logger.warning(
            "GPT Researcher process failed for deep research model '%s': %s",
            requested_model,
            exc.code,
        )
        raise HTTPException(
            status_code=503,
            detail=f"GPT Researcher failed: {exc.code}",
        ) from exc
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    except Exception as exc:
        logger.exception("GPT Researcher failed for deep research model '%s'.", requested_model)
        raise HTTPException(
            status_code=503,
            detail="GPT Researcher failed: internal_error",
        ) from exc

    reported_cost = _deep_research_reported_cost(result.costs)
    seal = await accounting_owner.seal_for_response()
    aggregate_usage = seal.aggregate_usage
    if reported_cost is None:
        logger.warning("Deep research diagnostic cost is unavailable or invalid.")
    elif abs(reported_cost - aggregate_usage.cost) > 1e-9:
        logger.warning("Deep research diagnostic cost differs from captured child accounting.")
    response_usage = {
        "prompt_tokens": aggregate_usage.prompt_tokens,
        "completion_tokens": aggregate_usage.completion_tokens,
        "total_tokens": aggregate_usage.total_tokens,
        "reasoning_tokens": aggregate_usage.reasoning_tokens,
        "cached_tokens": aggregate_usage.cached_tokens,
        "cost": aggregate_usage.cost,
        "cost_saved": aggregate_usage.cost_saved,
        "is_estimated": aggregate_usage.is_estimated,
    }
    response_payload = {
        "object": "web_deep_research",
        "model": requested_model,
        "query": query,
        "output": result.report,
        "sources": list(result.sources),
        "source_urls": list(result.source_urls),
        "context": list(result.context),
        "research_result": result.research_result,
        "images": _format_generated_images(list(result.generated_images)),
        "usage": response_usage,
    }
    _set_web_service_usage_state(request, requested_model, "deep-research")
    return json_response(response_payload, 200)
