from __future__ import annotations

import logging
import urllib.parse
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any, cast
from urllib.parse import urlsplit

import httpx
from fastapi import HTTPException, Request

from ...agents.web_research import WebResearchClient
from ...config.loader import ConfigLoader
from ...config.settings import settings
from ...services.accounting import (
    AccountingValidationError,
    BillingComponent,
    BillingComponentKind,
    BillingPolicy,
    BillingUnit,
)
from ...services.active_requests import update_active_request_from_state
from ...services.operation_accounting import (
    OperationTerminalObservation,
    build_token_model_component,
    parse_synthesized_operation_observation,
)
from ...services.request_handler import OperationDispatcher
from ...utils.api_keys import has_api_key, select_next_api_key
from ...utils.provider_error_redaction import redact_provider_error_text
from ...utils.usage_tracking import extract_tokens_usage, initialize_tokens_usage
from ...utils.zai_mcp import detect_zai_search_location, zai_mcp_tool_call
from .chat import _attempt_model_fallback_rule
from .operation_accounting import OperationTerminalOwner
from .web_content import (
    append_images_to_markdown as _append_images_to_markdown,
    content_with_images as _content_with_images,
    extract_images_from_markdown as _extract_images_from_markdown,
    markdown_to_plain_text as _markdown_to_plain_text,
    merge_image_items as _merge_image_items,
)
from .web_extraction import _cloakbrowser_fetch, _direct_http_fetch

if TYPE_CHECKING:
    from ...services.runtime_config import AppServices, RuntimeSnapshot

logger = logging.getLogger(__name__)

WEB_SEARCH_SECTION = "web_search"
WEB_READ_SECTION = "web_read"
WEB_RESEARCH_SECTION = "web_research"
WEB_DEEP_RESEARCH_SECTION = "web_deep_research"
WEB_SEARCH_OPERATION = "web_search"
WEB_READ_OPERATION = "web_read"
WEB_RESEARCH_OPERATION = "web_research"
WEB_DEEP_RESEARCH_OPERATION = "web_deep_research"

_BUILTIN_SEARCH_ADAPTERS: tuple[str, ...] = ("proxy", "tavily", "jina", "zai")
_BUILTIN_READ_ADAPTERS: tuple[str, ...] = ("proxy", "tavily", "jina", "zai")


class _UsageAccumulator:
    def __init__(self) -> None:
        self.usage = initialize_tokens_usage()

    def add(self, tokens_usage: dict[str, Any]) -> None:
        for key in (
            "prompt_tokens",
            "completion_tokens",
            "total_tokens",
            "reasoning_tokens",
            "cached_tokens",
        ):
            self.usage[key] = int(self.usage.get(key) or 0) + int(tokens_usage.get(key) or 0)
        for key in ("cost", "cost_saved"):
            if tokens_usage.get(key) is None:
                continue
            self.usage[key] = float(self.usage.get(key) or 0) + float(tokens_usage.get(key) or 0)
        self.usage["is_estimated"] = bool(self.usage.get("is_estimated")) or bool(tokens_usage.get("is_estimated"))


@dataclass(frozen=True, slots=True)
class _ObservedTextModelResult:
    text: str
    component: BillingComponent

    def __post_init__(self) -> None:
        if (
            not isinstance(self.text, str)
            or not self.text.strip()
            or not isinstance(self.component, BillingComponent)
            or self.component.component_kind is not BillingComponentKind.MODEL
        ):
            raise AccountingValidationError


@dataclass(frozen=True, slots=True)
class _ObservedWebToolResult:
    result: object
    components: tuple[BillingComponent, ...]

    def __post_init__(self) -> None:
        if (
            not isinstance(self.components, tuple)
            or not self.components
            or any(not isinstance(component, BillingComponent) for component in self.components)
            or self.components[0].component_kind is not BillingComponentKind.OPERATION
            or any(component.component_kind is not BillingComponentKind.MODEL for component in self.components[1:])
        ):
            raise AccountingValidationError


def _build_web_operation_component(
    request: Request,
    *,
    operation: str,
    gateway_model: str,
) -> BillingComponent:
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    observation = parse_synthesized_operation_observation(
        {},
        policy=BillingPolicy(operation=operation, unit=BillingUnit.OPERATION),
        gateway_model=gateway_model,
        provider=None,
        model=None,
        cost_rate_registry=runtime_snapshot.cost_rate_registry,
        operation_cost_calculator_registry=(runtime_snapshot.operation_cost_calculator_registry),
        occurred_at=datetime.now(timezone.utc),
    )
    return BillingComponent(
        provider=None,
        model=None,
        usage=observation.usage,
        cost_source=observation.cost_source,
        component_kind=BillingComponentKind.OPERATION,
        operation=operation,
        gateway_model=gateway_model,
    )


def _parse_web_terminal_observation(
    owner: OperationTerminalOwner,
    *,
    gateway_model: str,
    duration_ms: int,
) -> OperationTerminalObservation:
    return parse_synthesized_operation_observation(
        {},
        policy=owner.context.policy,
        gateway_model=gateway_model,
        provider="llmgateway",
        model=gateway_model,
        cost_rate_registry=owner.cost_rate_registry,
        operation_cost_calculator_registry=(owner.operation_cost_calculator_registry),
        occurred_at=datetime.now(timezone.utc),
        duration_ms=duration_ms,
    )


def _get_operation_runtime(
    request: Request,
) -> tuple[OperationDispatcher, httpx.AsyncClient, ConfigLoader, Mapping[str, httpx.AsyncClient]]:
    services = cast("AppServices", request.app.state.services)
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    return (
        runtime_snapshot.operation_dispatcher,
        services.http_client,
        runtime_snapshot.config_loader,
        runtime_snapshot.proxy_http_clients,
    )


def _clamp_int(value: object, default: int, minimum: int, maximum: int) -> int:
    if value is None:
        return default
    try:
        normalized = int(value)
    except (TypeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail=f"Expected integer value, got {value!r}") from exc
    return max(minimum, min(normalized, maximum))


def _normalize_output_format(value: object) -> str:
    normalized = str(value or "markdown").strip().lower()
    if normalized in {"markdown", "text"}:
        return normalized
    raise HTTPException(status_code=400, detail="'format' must be 'markdown' or 'text'.")


def _format_read_result(result: dict[str, Any], output_format: str) -> dict[str, Any]:
    normalized_format = _normalize_output_format(output_format)
    if normalized_format == "markdown":
        return result
    formatted = dict(result)
    formatted["content"] = _markdown_to_plain_text(str(formatted.get("content") or ""))
    return formatted


def _validate_image_size(value: str, service_model: str) -> str:
    parts = value.lower().split("x", 1)
    if len(parts) != 2:
        raise HTTPException(
            status_code=500,
            detail=f"Model '{service_model}' has invalid image_generation_size; expected WIDTHxHEIGHT.",
        )
    try:
        width = int(parts[0])
        height = int(parts[1])
    except ValueError as exc:
        raise HTTPException(
            status_code=500,
            detail=f"Model '{service_model}' has invalid image_generation_size; expected WIDTHxHEIGHT.",
        ) from exc
    if width <= 0 or height <= 0:
        raise HTTPException(
            status_code=500,
            detail=f"Model '{service_model}' has invalid image_generation_size; dimensions must be positive.",
        )
    return f"{width}x{height}"


def _get_model_config(config_loader: ConfigLoader, section_name: str, gateway_model: str) -> dict:
    section = config_loader.operation_rules.get(section_name, {})
    config = section.get(gateway_model)
    if not isinstance(config, dict):
        raise HTTPException(status_code=404, detail=f"No {section_name} route configured for model '{gateway_model}'.")
    return config


def _resolve_service_model(
    config_loader: ConfigLoader,
    section_name: str,
    explicit_model: str | None,
    *,
    field_name: str,
) -> str:
    if explicit_model:
        _get_model_config(config_loader, section_name, explicit_model)
        return explicit_model

    section = config_loader.operation_rules.get(section_name, {})
    if not isinstance(section, dict) or not section:
        raise HTTPException(status_code=503, detail=f"No models configured in '{section_name}'.")
    if len(section) == 1:
        return next(iter(section))
    raise HTTPException(
        status_code=400,
        detail=f"Missing '{field_name}' in request body because multiple '{section_name}' models are configured.",
    )


def _require_config_text(config: dict, field_name: str, service_model: str) -> str:
    value = config.get(field_name)
    if not isinstance(value, str) or not value.strip():
        raise HTTPException(status_code=500, detail=f"Model '{service_model}' has no {field_name}.")
    return value.strip()


def _require_config_model(config: dict, field_name: str, service_model: str) -> str:
    return _require_config_text(config, field_name, service_model)


def _set_web_service_usage_state(
    request: Request,
    service_model: str,
    operation_name: str,
    *,
    target_path: str | None = None,
) -> None:
    request.state.llmgateway_gateway_model = service_model
    request.state.llmgateway_provider = "llmgateway"
    request.state.llmgateway_provider_model = service_model
    request.state.llmgateway_operation = f"web_{operation_name}"
    request.state.llmgateway_target_path = target_path or f"/v1/web/{operation_name}"
    update_active_request_from_state(request)


async def _call_internal_text_model(
    request: Request,
    config_loader: ConfigLoader,
    http_client: httpx.AsyncClient,
    *,
    model: str,
    messages: list[dict[str, str]],
    temperature: float,
    max_tokens: int,
    usage_accumulator: _UsageAccumulator,
    observe: bool = False,
) -> str | _ObservedTextModelResult:
    services = cast("AppServices", request.app.state.services)
    runtime_snapshot = cast("RuntimeSnapshot", request.state.runtime_snapshot)
    model_config = config_loader.fallback_rules.get(model)
    if not isinstance(model_config, dict):
        raise HTTPException(status_code=500, detail=f"Internal gateway model '{model}' is not configured.")

    fallback_models = model_config.get("fallback_models")
    if not isinstance(fallback_models, list) or not fallback_models:
        raise HTTPException(status_code=500, detail=f"Internal gateway model '{model}' has no fallback models.")

    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "stream": False,
    }
    last_error = "No providers were attempted."
    for fallback_rule in fallback_models:
        response_data, error_detail, _attempt_number = await _attempt_model_fallback_rule(
            request,
            http_client,
            config_loader.providers_config,
            model,
            payload,
            fallback_rule,
            False,
            proxy_http_clients=runtime_snapshot.proxy_http_clients,
            upstream_routing_state=services.upstream_routing_state,
        )
        if isinstance(response_data, dict) and error_detail is None:
            choices = response_data.get("choices")
            if isinstance(choices, list) and choices:
                message = choices[0].get("message") if isinstance(choices[0], dict) else None
                content = message.get("content") if isinstance(message, dict) else None
                if isinstance(content, str) and content.strip():
                    if observe:
                        provider = fallback_rule.get("provider")
                        provider_model = fallback_rule.get("model")
                        if not isinstance(provider, str) or not isinstance(provider_model, str):
                            raise AccountingValidationError
                        component = build_token_model_component(
                            response_data,
                            provider=provider,
                            model=provider_model,
                            cost_rate_registry=runtime_snapshot.cost_rate_registry,
                        )
                        usage_accumulator.add(extract_tokens_usage(response_data))
                        return _ObservedTextModelResult(
                            text=content,
                            component=component,
                        )
                    usage_accumulator.add(extract_tokens_usage(response_data))
                    return content
            usage_accumulator.add(extract_tokens_usage(response_data))
            provider = str(fallback_rule.get("provider") or "unknown")
            provider_model = str(fallback_rule.get("model") or "unknown")
            last_error = f"Provider '{provider}' model '{provider_model}' returned no text content."
            logger.warning(
                "Internal gateway model '%s' received no text content from provider '%s' model '%s'; "
                "trying next fallback.",
                model,
                provider,
                provider_model,
            )
            continue
        last_error = error_detail or last_error

    raise HTTPException(
        status_code=503,
        detail=f"Internal gateway model '{model}' failed: {redact_provider_error_text(last_error)}",
    )


async def _generate_queries(
    request: Request,
    config_loader: ConfigLoader,
    http_client: httpx.AsyncClient,
    *,
    query_model: str | None,
    query: str,
    language: str,
    num_queries: int,
    usage_accumulator: _UsageAccumulator,
    component_accumulator: list[BillingComponent] | None = None,
) -> list[str]:
    if not query_model:
        return [query]

    async def _completion(messages: list[dict[str, str]], temperature: float, max_tokens: int) -> str:
        if component_accumulator is None:
            result = await _call_internal_text_model(
                request,
                config_loader,
                http_client,
                model=query_model,
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                usage_accumulator=usage_accumulator,
            )
            if not isinstance(result, str):
                raise AccountingValidationError
            return result
        observed = await _call_internal_text_model(
            request,
            config_loader,
            http_client,
            model=query_model,
            messages=messages,
            temperature=temperature,
            max_tokens=max_tokens,
            usage_accumulator=usage_accumulator,
            observe=True,
        )
        if not isinstance(observed, _ObservedTextModelResult):
            raise AccountingValidationError
        component_accumulator.append(observed.component)
        return observed.text

    client = WebResearchClient(
        research_model=query_model,
        text_completion=_completion,
        http_client=http_client,
    )
    queries = await client.generate_search_queries(query, language, num_queries)
    if not queries:
        raise HTTPException(status_code=503, detail=f"Internal query model '{query_model}' returned no search queries.")
    return queries


def _normalize_search_item(
    url: Any,
    title: Any = "",
    snippet: Any = "",
    *,
    images: Any = None,
) -> dict[str, Any] | None:
    if not isinstance(url, str) or not url.strip():
        return None
    item: dict[str, Any] = {
        "url": url.strip(),
        "title": str(title or ""),
        "snippet": str(snippet or ""),
    }
    normalized_images = _merge_image_items(images, _extract_images_from_markdown(item["snippet"], item["url"]))
    if normalized_images:
        item["images"] = normalized_images
    return item


async def _search_proxy(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    proxy_url = (settings.proxy_url or "").strip()
    if not proxy_url:
        return []
    response = await client.get(
        f"{proxy_url.rstrip('/')}/zai/search",
        params={"q": query},
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json() or {}
    items = data.get("search_result", [])[:max_results]
    normalized = [
        _normalize_search_item(
            item.get("url") or item.get("link"),
            item.get("title"),
            item.get("content") or item.get("snippet") or item.get("description"),
            images=item.get("images") if include_images else None,
        )
        for item in items
        if isinstance(item, dict)
    ]
    return [item for item in normalized if item is not None]


async def _search_tavily(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    tavily_api_key = select_next_api_key(settings.tavily_api_key)
    if not tavily_api_key:
        return []
    response = await client.post(
        "https://api.tavily.com/search",
        json={
            "api_key": tavily_api_key,
            "query": query,
            "max_results": max_results,
            "include_images": include_images,
            "include_image_descriptions": include_images,
        },
        timeout=30.0,
    )
    response.raise_for_status()
    data = response.json() or {}
    items = data.get("results", [])[:max_results]
    normalized = [
        _normalize_search_item(
            item.get("url") or item.get("link"),
            item.get("title"),
            item.get("content") or item.get("snippet"),
            images=item.get("images") if include_images else None,
        )
        for item in items
        if isinstance(item, dict)
    ]
    return [item for item in normalized if item is not None]


async def _search_jina(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    jina_api_key = select_next_api_key(settings.jina_api_key)
    if not jina_api_key:
        return []
    url = f"https://s.jina.ai/?q={urllib.parse.quote(query)}"
    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {jina_api_key}",
    }
    if include_images:
        headers["X-Retain-Images"] = "all"
        headers["X-With-Images-Summary"] = "true"
        headers["X-With-Generated-Alt"] = "true"
    else:
        headers["X-Respond-With"] = "no-content"
    response = await client.get(url, headers=headers, timeout=30.0)
    response.raise_for_status()
    data = response.json() or {}
    if data.get("code") != 200:
        return []
    items = data.get("data", [])[:max_results]
    normalized = [
        _normalize_search_item(
            item.get("url"),
            item.get("title"),
            item.get("description") or item.get("content"),
            images=(item.get("images") or _extract_images_from_markdown(item.get("content"), item.get("url") or ""))
            if include_images
            else None,
        )
        for item in items
        if isinstance(item, dict)
    ]
    return [item for item in normalized if item is not None]


async def _search_zai(
    client: httpx.AsyncClient,
    query: str,
    max_results: int,
    *,
    include_images: bool = False,
) -> list[dict[str, Any]]:
    zai_api_key = select_next_api_key(settings.zai_api_key)
    if not zai_api_key:
        return []
    payload = await zai_mcp_tool_call(
        client,
        api_key=zai_api_key,
        server_path="web_search_prime",
        tool_name="web_search_prime",
        arguments={
            "search_query": query,
            "location": detect_zai_search_location(query),
        },
        timeout=60.0,
    )
    items = payload if isinstance(payload, list) else []
    normalized = [
        _normalize_search_item(
            item.get("link") or item.get("url"),
            item.get("title"),
            item.get("content") or item.get("snippet"),
            images=(item.get("images") or item.get("media")) if include_images else None,
        )
        for item in items[:max_results]
        if isinstance(item, dict)
    ]
    return [item for item in normalized if item is not None]


_SEARCH_ADAPTERS = {
    "proxy": _search_proxy,
    "tavily": _search_tavily,
    "jina": _search_jina,
    "zai": _search_zai,
}


def _search_adapter_enabled(name: str) -> bool:
    if name == "proxy":
        return bool((settings.proxy_url or "").strip())
    if name == "tavily":
        return has_api_key(settings.tavily_api_key)
    if name == "jina":
        return has_api_key(settings.jina_api_key)
    if name == "zai":
        return has_api_key(settings.zai_api_key)
    return False


async def _read_proxy(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    proxy_url = (settings.proxy_url or "").strip()
    if not proxy_url:
        return None
    response = await client.get(
        f"{proxy_url.rstrip('/')}/zai/read",
        params={
            "url": url,
            "return_format": "markdown",
            "retain_images": "true",
            "with_images_summary": "true",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    data = (response.json() or {}).get("reader_result") or {}
    content = (data.get("content") or "").strip()
    if not content:
        return None
    content, images = _content_with_images(content, data.get("images"), url)
    return {"url": url, "title": str(data.get("title") or ""), "content": content, "images": images}


async def _read_tavily(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    tavily_api_key = select_next_api_key(settings.tavily_api_key)
    if not tavily_api_key:
        return None
    response = await client.post(
        "https://api.tavily.com/extract",
        json={
            "api_key": tavily_api_key,
            "urls": [url],
            "include_images": True,
            "format": "markdown",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json() or {}
    results = payload.get("results") or []
    if not results or not isinstance(results[0], dict):
        return None
    item = results[0]
    content = (item.get("raw_content") or item.get("content") or "").strip()
    if not content:
        return None
    content, images = _content_with_images(content, item.get("images"), url)
    # Tavily Extract returns page images as a separate list rather than inline; surface them
    # in the markdown body so web read keeps image links in the text like the other adapters.
    content = _append_images_to_markdown(content, images)
    return {"url": url, "title": str(item.get("title") or ""), "content": content, "images": images}


async def _read_jina(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    jina_api_key = select_next_api_key(settings.jina_api_key)
    if not jina_api_key:
        return None
    response = await client.get(
        f"https://r.jina.ai/{url}",
        headers={
            "Accept": "application/json",
            "Authorization": f"Bearer {jina_api_key}",
            "X-Retain-Images": "all",
            "X-With-Images-Summary": "true",
            "X-With-Generated-Alt": "true",
        },
        timeout=30.0,
    )
    response.raise_for_status()
    payload = response.json() or {}
    data = payload.get("data") or {}
    content = (data.get("content") or data.get("text") or "").strip()
    if not content:
        return None
    content, images = _content_with_images(content, data.get("images"), url)
    return {"url": url, "title": str(data.get("title") or ""), "content": content, "images": images}


async def _read_zai(client: httpx.AsyncClient, url: str) -> dict[str, Any] | None:
    zai_api_key = select_next_api_key(settings.zai_api_key)
    if not zai_api_key:
        return None
    payload = await zai_mcp_tool_call(
        client,
        api_key=zai_api_key,
        server_path="web_reader",
        tool_name="webReader",
        arguments={
            "url": url,
            "return_format": "markdown",
            "retain_images": True,
            "with_images_summary": True,
            "keep_img_data_url": False,
            "timeout": 20,
        },
        timeout=60.0,
    )
    data = payload if isinstance(payload, dict) else {}
    content = (data.get("content") or "").strip()
    if not content:
        return None
    content, images = _content_with_images(content, data.get("images"), url)
    return {"url": url, "title": str(data.get("title") or ""), "content": content, "images": images}


_READ_ADAPTERS = {
    "proxy": _read_proxy,
    "tavily": _read_tavily,
    "jina": _read_jina,
    "zai": _read_zai,
}


def _read_adapter_enabled(name: str) -> bool:
    return _search_adapter_enabled(name)


async def _search_with_model(
    request: Request,
    *,
    search_model: str,
    query: str,
    max_results: int,
    num_queries: int,
    language: str,
    usage_accumulator: _UsageAccumulator,
    expand_query: bool = True,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    include_images: bool = False,
    component_accumulator: list[BillingComponent] | None = None,
) -> list[dict[str, Any]]:
    _dispatcher, http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    search_config = _get_model_config(config_loader, WEB_SEARCH_SECTION, search_model)
    enabled_adapters = [name for name in _BUILTIN_SEARCH_ADAPTERS if _search_adapter_enabled(name)]
    if not enabled_adapters:
        raise HTTPException(
            status_code=503,
            detail=(
                f"Web search '{search_model}' has no enabled adapters. "
                "Configure at least one of PROXY_URL / TAVILY_API_KEY / JINA_API_KEY / ZAI_API_KEY in .env."
            ),
        )
    request.state.llmgateway_provider = "llmgateway"
    request.state.llmgateway_provider_model = ",".join(enabled_adapters)
    request.state.llmgateway_target_path = "/v1/web/search"
    update_active_request_from_state(request)
    if expand_query:
        query_model = search_config.get("query_model")
        component_kwargs = {"component_accumulator": component_accumulator} if component_accumulator is not None else {}
        queries = await _generate_queries(
            request,
            config_loader,
            http_client,
            query_model=query_model if isinstance(query_model, str) else None,
            query=query,
            language=language,
            num_queries=num_queries,
            usage_accumulator=usage_accumulator,
            **component_kwargs,
        )
    else:
        queries = [query]

    collected: list[dict[str, Any]] = []
    seen: set[str] = set()
    per_query_limit = max(1, min(max_results, max_results // max(len(queries), 1) + 1))
    include_domains = include_domains or []
    exclude_domains = exclude_domains or []
    for search_query in queries:
        query_results: list[dict[str, Any]] = []
        for adapter_name in enabled_adapters:
            adapter = _SEARCH_ADAPTERS[adapter_name]
            try:
                query_results = await adapter(http_client, search_query, per_query_limit, include_images=include_images)
            except Exception as exc:
                logger.warning(
                    "Web search adapter '%s' failed for query '%s': %s",
                    adapter_name,
                    search_query,
                    exc,
                )
                continue
            if query_results:
                filtered_query_results = _filter_search_results(
                    query_results,
                    include_domains=include_domains,
                    exclude_domains=exclude_domains,
                    max_results=per_query_limit,
                )
                if include_domains or exclude_domains:
                    if not filtered_query_results:
                        logger.warning(
                            "Web search adapter '%s' returned %d results for '%s', "
                            "but none matched domain filters; trying next.",
                            adapter_name,
                            len(query_results),
                            search_query,
                        )
                        query_results = []
                        continue
                    query_results = filtered_query_results
                logger.info(
                    "Web search adapter '%s' returned %d results for '%s'.",
                    adapter_name,
                    len(query_results),
                    search_query,
                )
                break
            logger.warning(
                "Web search adapter '%s' returned no results for '%s'; trying next.",
                adapter_name,
                search_query,
            )
        for result in query_results:
            if result["url"] in seen:
                continue
            seen.add(result["url"])
            collected.append(result)
            if len(collected) >= max_results:
                return collected
    return collected


async def _search_with_model_observed(
    request: Request,
    *,
    search_model: str,
    query: str,
    max_results: int,
    num_queries: int,
    language: str,
    usage_accumulator: _UsageAccumulator,
    expand_query: bool = True,
    include_domains: list[str] | None = None,
    exclude_domains: list[str] | None = None,
    include_images: bool = False,
    _component_accumulator: list[BillingComponent] | None = None,
) -> _ObservedWebToolResult:
    internal_components = [] if _component_accumulator is None else _component_accumulator
    result = await _search_with_model(
        request,
        search_model=search_model,
        query=query,
        max_results=max_results,
        num_queries=num_queries,
        language=language,
        usage_accumulator=usage_accumulator,
        expand_query=expand_query,
        include_domains=include_domains,
        exclude_domains=exclude_domains,
        include_images=include_images,
        component_accumulator=internal_components,
    )
    operation_component = _build_web_operation_component(
        request,
        operation=WEB_SEARCH_OPERATION,
        gateway_model=search_model,
    )
    return _ObservedWebToolResult(
        result=result,
        components=(operation_component, *internal_components),
    )


async def _read_with_model(
    request: Request,
    *,
    read_model: str,
    url: str,
    output_format: str,
) -> dict[str, Any]:
    _dispatcher, http_client, config_loader, _proxy_http_clients = _get_operation_runtime(request)
    _get_model_config(config_loader, WEB_READ_SECTION, read_model)
    enabled_adapters = [name for name in _BUILTIN_READ_ADAPTERS if _read_adapter_enabled(name)]
    request.state.llmgateway_provider = "llmgateway"
    request.state.llmgateway_provider_model = ",".join(["direct", "cloakbrowser"] + enabled_adapters)
    request.state.llmgateway_target_path = "/v1/web/read"
    update_active_request_from_state(request)

    direct_result = await _direct_http_fetch(url)
    if direct_result is not None:
        return _format_read_result(direct_result, output_format)

    rendered_result = await _cloakbrowser_fetch(url)
    if rendered_result is not None:
        return _format_read_result(rendered_result, output_format)

    last_error: str | None = None
    for adapter_name in enabled_adapters:
        adapter = _READ_ADAPTERS[adapter_name]
        try:
            result = await adapter(http_client, url)
        except Exception as exc:
            logger.warning("Web read adapter '%s' failed for %s: %s", adapter_name, url, exc)
            last_error = f"{adapter_name}: {exc}"
            continue
        if result is not None and (result.get("content") or "").strip():
            logger.info("Web read adapter '%s' succeeded for %s.", adapter_name, url)
            return _format_read_result(result, output_format)
        logger.warning("Web read adapter '%s' returned empty content for %s; trying next.", adapter_name, url)
        last_error = f"{adapter_name}: empty content"
    raise HTTPException(
        status_code=503,
        detail=(
            f"Web read '{read_model}' failed to retrieve content for '{url}'."
            + (f" Last error: {last_error}" if last_error else " No adapters enabled.")
        ),
    )


async def _read_with_model_observed(
    request: Request,
    *,
    read_model: str,
    url: str,
    output_format: str,
) -> _ObservedWebToolResult:
    result = await _read_with_model(
        request,
        read_model=read_model,
        url=url,
        output_format=output_format,
    )
    operation_component = _build_web_operation_component(
        request,
        operation=WEB_READ_OPERATION,
        gateway_model=read_model,
    )
    return _ObservedWebToolResult(
        result=result,
        components=(operation_component,),
    )


def _parse_domain_list(value: object, field_name: str) -> list[str]:
    if value is None:
        return []
    raw_values: list[object]
    if isinstance(value, str):
        raw_values = [part.strip() for part in value.split(",")]
    elif isinstance(value, list):
        raw_values = value
    else:
        raise HTTPException(status_code=400, detail=f"'{field_name}' must be a string or an array of strings.")

    domains: list[str] = []
    for item in raw_values:
        if not isinstance(item, str):
            raise HTTPException(status_code=400, detail=f"'{field_name}' must contain only strings.")
        raw_domain = item.strip().lower()
        if not raw_domain:
            continue
        parsed = urlsplit(raw_domain if "://" in raw_domain else f"https://{raw_domain}")
        domain = parsed.netloc or parsed.path
        if ":" in domain:
            domain = domain.split(":", 1)[0]
        domain = domain.strip(".")
        if domain:
            domains.append(domain)
    return domains


def _domain_matches(url: str, domains: list[str]) -> bool:
    if not domains:
        return False
    hostname = (urlsplit(url).hostname or "").lower().strip(".")
    if not hostname:
        return False
    return any(hostname == domain or hostname.endswith(f".{domain}") for domain in domains)


def _filter_search_results(
    results: list[dict[str, Any]],
    *,
    include_domains: list[str],
    exclude_domains: list[str],
    max_results: int,
) -> list[dict[str, Any]]:
    filtered: list[dict[str, Any]] = []
    for result in results:
        url = str(result.get("url") or "")
        if include_domains and not _domain_matches(url, include_domains):
            continue
        if exclude_domains and _domain_matches(url, exclude_domains):
            continue
        filtered.append(result)
        if len(filtered) >= max_results:
            break
    return filtered
