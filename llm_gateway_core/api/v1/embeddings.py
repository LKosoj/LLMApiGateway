import asyncio  # noqa: F401
import logging
import time

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse

from . import operation_proxy
from ...config.loader import (
    ConfigLoader,
    OperationRoute,
    REQUEST_FORMAT_QUERY_PASSAGES,
    REQUEST_FORMAT_QUERY_TEXTS,
    RESPONSE_FORMAT_RANKINGS_LOGIT,
    RESPONSE_FORMAT_SCORES,
    RESPONSE_OUTPUT_FORMAT_JINA_RESULTS,
    resolve_provider_api_key,
)
from ...services.access_control import enforce_virtual_key_access
from ...services.request_handler import OperationDispatcher, normalize_retry_settings
from .operation_proxy import (
    json_response,
    proxy_json_to_downstream,
    read_json_request_body,
    record_operation_usage,
    request_duration_ms,
)

logger = logging.getLogger(__name__)

router = APIRouter()
RERANK_ALLOWED_CUSTOM_PARAMS = frozenset({"top_n", "return_documents", "max_chunks_per_doc"})
FORBIDDEN_OPERATION_CUSTOM_PARAMS = frozenset({"stream", "messages", "tool_choice", "tools", "model"})
QUERY_PASSAGES_ALLOWED_CUSTOM_PARAMS = frozenset({"max_chunks_per_doc"})
POSITION_FALLBACK_INDEX_KEY = "_llmgateway_position_fallback_index"


async def proxy_to_downstream(*args, **kwargs):
    original_logger = operation_proxy.logger
    operation_proxy.logger = logger
    try:
        return await proxy_json_to_downstream(*args, **kwargs)
    finally:
        operation_proxy.logger = original_logger


def _uses_query_passages_request_format(route: OperationRoute) -> bool:
    return route.request_format == REQUEST_FORMAT_QUERY_PASSAGES


def _uses_query_texts_request_format(route: OperationRoute) -> bool:
    return route.request_format == REQUEST_FORMAT_QUERY_TEXTS


def map_rerank_payload(query: str, documents: list[str], route: OperationRoute) -> dict:
    if _uses_query_passages_request_format(route):
        downstream_payload = {
            "model": route.model,
            "query": {"text": query},
            "passages": [{"text": document} for document in documents],
        }

        for param_name, param_value in route.custom_body_params.items():
            normalized_param_name = param_name.lower()
            if normalized_param_name in QUERY_PASSAGES_ALLOWED_CUSTOM_PARAMS:
                downstream_payload[param_name] = param_value

        return downstream_payload

    if _uses_query_texts_request_format(route):
        return {
            "model": route.model,
            "query": query,
            "texts": documents,
        }

    downstream_payload = {
        "model": route.model,
        "text_1": query,
        "text_2": documents,
    }

    for param_name, param_value in route.custom_body_params.items():
        normalized_param_name = param_name.lower()
        if normalized_param_name in FORBIDDEN_OPERATION_CUSTOM_PARAMS:
            continue
        if normalized_param_name not in RERANK_ALLOWED_CUSTOM_PARAMS:
            continue
        downstream_payload[param_name] = param_value

    return downstream_payload


def extract_score(result: dict, response_format: str | None = None) -> float:
    score_keys = (
        ("logit",)
        if response_format == RESPONSE_FORMAT_RANKINGS_LOGIT
        else ("score", "relevance_score", "relevance", "similarity", "similarity_score")
    )

    for score_key in score_keys:
        if score_key not in result:
            continue

        score_value = result.get(score_key)
        try:
            return float(score_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid rerank score value for key '{score_key}': {score_value!r}") from exc

    raise ValueError("Downstream rerank result does not contain a supported score field.")


def extract_scores_response_score(score_value: object) -> float:
    try:
        return float(score_value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid rerank score value in 'scores': {score_value!r}") from exc


def extract_index(result: dict, original_index: int) -> int:
    for index_key in ("index", "document_index"):
        if index_key not in result:
            continue

        index_value = result.get(index_key)
        try:
            return int(index_value)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Invalid rerank index value for key '{index_key}': {index_value!r}") from exc

    return original_index


def _extract_document_value(result: dict) -> str | None:
    document_value = result.get("document")
    if isinstance(document_value, str):
        return document_value
    if isinstance(document_value, dict):
        for text_key in ("text", "content", "page_content"):
            text_value = document_value.get(text_key)
            if isinstance(text_value, str):
                return text_value

    for fallback_key in ("text", "document_text", "content"):
        fallback_value = result.get(fallback_key)
        if isinstance(fallback_value, str):
            return fallback_value

    return None


def normalize_rerank_response(
    downstream_response: dict,
    response_format: str | None = None,
    *,
    include_index_metadata: bool = False,
) -> dict:
    if not isinstance(downstream_response, dict):
        raise ValueError("Downstream rerank response must be a JSON object.")

    if response_format == RESPONSE_FORMAT_SCORES:
        raw_scores = downstream_response.get("scores")
        if raw_scores is None:
            raise ValueError("Downstream rerank response must contain 'scores'.")
        if not isinstance(raw_scores, list):
            raise ValueError("Downstream rerank response field 'scores' must be a list.")
        normalized_results = [
            {"index": original_index, "score": extract_scores_response_score(raw_score)}
            for original_index, raw_score in enumerate(raw_scores)
        ]
        normalized_results.sort(key=lambda item: item["score"], reverse=True)
        return {"data": normalized_results}

    if response_format == RESPONSE_FORMAT_RANKINGS_LOGIT:
        raw_results = downstream_response.get("rankings")
        if raw_results is None:
            raise ValueError("Downstream rerank response must contain 'rankings'.")
    elif "results" in downstream_response:
        raw_results = downstream_response.get("results")
    elif "data" in downstream_response:
        raw_results = downstream_response.get("data")
    else:
        raise ValueError("Downstream rerank response must contain 'results' or 'data'.")

    if not isinstance(raw_results, list):
        raise ValueError("Downstream rerank response field 'results'/'data' must be a list.")

    normalized_results = []
    for original_index, raw_result in enumerate(raw_results):
        if not isinstance(raw_result, dict):
            raise ValueError("Each downstream rerank result must be an object.")

        has_explicit_index = any(index_key in raw_result for index_key in ("index", "document_index"))
        normalized_result = {
            "index": extract_index(raw_result, original_index),
            "score": extract_score(raw_result, response_format),
        }
        if include_index_metadata and not has_explicit_index:
            normalized_result[POSITION_FALLBACK_INDEX_KEY] = True
        document_value = _extract_document_value(raw_result)
        if document_value is not None:
            normalized_result["document"] = document_value

        normalized_results.append(normalized_result)

    return {"data": normalized_results}


def apply_top_n(results: list, top_n: int | None) -> list:
    if top_n is None:
        return list(results)

    return list(results[:top_n])


def add_return_documents(results: list, original_documents: list[str], return_docs: bool) -> list:
    normalized_results = []

    for position, result in enumerate(results):
        if not isinstance(result, dict):
            raise ValueError("Each normalized rerank result must be an object.")

        result_without_document = {
            key: value for key, value in result.items() if key not in ("document", POSITION_FALLBACK_INDEX_KEY)
        }
        if return_docs:
            document_index = None
            if result.get(POSITION_FALLBACK_INDEX_KEY):
                downstream_document = result.get("document")
                if isinstance(downstream_document, str):
                    matching_indices = [
                        index
                        for index, original_document in enumerate(original_documents)
                        if original_document == downstream_document
                    ]
                    if len(matching_indices) == 1:
                        document_index = matching_indices[0]
                        result_without_document["index"] = document_index
            else:
                index_value = result_without_document.get("index", position)
                try:
                    document_index = int(index_value)
                except (TypeError, ValueError):
                    document_index = position

                if not 0 <= document_index < len(original_documents):
                    document_index = position

            if document_index is not None and 0 <= document_index < len(original_documents):
                result_without_document["document"] = original_documents[document_index]

        normalized_results.append(result_without_document)

    return normalized_results


def format_rerank_response(results: list[dict], response_output_format: str | None = None) -> dict:
    if response_output_format == RESPONSE_OUTPUT_FORMAT_JINA_RESULTS:
        formatted_results = []
        for result in results:
            formatted_result = {
                "index": result["index"],
                "relevance_score": result["score"],
            }
            if "document" in result:
                formatted_result["document"] = result["document"]
            formatted_results.append(formatted_result)
        return {"results": formatted_results}

    return {"data": list(results)}


def _get_operation_runtime(request: Request) -> tuple[OperationDispatcher, httpx.AsyncClient, ConfigLoader, dict]:
    dispatcher: OperationDispatcher | None = getattr(request.app.state, "operation_dispatcher", None)
    if dispatcher is None:
        logger.error("OperationDispatcher not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Operation dispatcher not available.")

    http_client: httpx.AsyncClient | None = getattr(request.app.state, "http_client", None)
    if http_client is None:
        logger.error("Shared httpx.AsyncClient not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Shared HTTP client not available.")

    config_loader_instance: ConfigLoader | None = getattr(request.app.state, "config_loader", None)
    if config_loader_instance is None:
        logger.error("ConfigLoader not found in application state.")
        raise HTTPException(status_code=500, detail="Internal server error: Core configuration not available.")

    proxy_http_clients: dict = getattr(request.app.state, "proxy_http_clients", {})

    return dispatcher, http_client, config_loader_instance, proxy_http_clients


def _should_try_next_route(exc: HTTPException, route_index: int, routes: list[OperationRoute]) -> bool:
    return exc.status_code == 503 and route_index < len(routes) - 1


def _log_route_fallback(
    operation: str,
    requested_model: str,
    route: OperationRoute,
    route_index: int,
    routes: list[OperationRoute],
    exc: HTTPException,
) -> None:
    logger.warning(
        "%s route failed for gateway model '%s'; falling back to next route. "
        "route=%s/%s provider=%s model=%s detail=%s",
        operation,
        requested_model,
        route_index + 1,
        len(routes),
        route.provider,
        route.model,
        exc.detail,
    )


@router.post("/embeddings")
async def create_embeddings(request: Request):
    request_body_json = await read_json_request_body(request, "embeddings endpoint")

    requested_model = request_body_json.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    input_value = request_body_json.get("input")
    if "input" not in request_body_json or input_value is None:
        raise HTTPException(status_code=400, detail="Missing 'input' in request body")

    enforce_virtual_key_access(request, requested_model)

    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    routes = dispatcher.lookup_routes("embeddings", requested_model)
    if not routes:
        raise HTTPException(status_code=404, detail=f"No embeddings route configured for model '{requested_model}'.")

    request_started_at = time.monotonic()

    for route_index, route in enumerate(routes):
        try:
            provider_config = config_loader_instance.providers_config.get(route.provider)
            if provider_config is None:
                logger.error(
                    "Provider '%s' for embeddings route '%s' is missing from providers_config.",
                    route.provider,
                    requested_model,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Provider configuration not available.",
                )

            provider_api_key = resolve_provider_api_key(provider_config.apikey)
            target_url = dispatcher.build_target_url(route, provider_config)
            headers = dispatcher.build_headers(route, provider_api_key)
            payload = dispatcher.build_payload(request_body_json, route, "embeddings")
            retry_count, retry_delay = normalize_retry_settings(route.retry_count, route.retry_delay)

            request.state.llmgateway_gateway_model = requested_model
            dispatcher.set_request_state(
                request=request,
                operation="embeddings",
                route=route,
                provider_name=route.provider,
                provider_model=route.model,
            )

            effective_client = proxy_http_clients.get(route.provider, http_client)
            downstream_result = await proxy_json_to_downstream(
                target_url,
                headers,
                payload,
                effective_client,
                retry_count=retry_count,
                retry_delay=retry_delay,
            )
            if isinstance(downstream_result, StreamingResponse):
                return downstream_result

            downstream_json, downstream_status_code = downstream_result
            await record_operation_usage(
                request,
                downstream_json,
                gateway_model=requested_model,
                operation="embeddings",
                duration_ms=request_duration_ms(request_started_at),
            )
            return json_response(downstream_json, downstream_status_code)
        except HTTPException as exc:
            if _should_try_next_route(exc, route_index, routes):
                _log_route_fallback("Embeddings", requested_model, route, route_index, routes, exc)
                continue
            raise

    raise HTTPException(status_code=503, detail="Embeddings request failed after exhausting fallback routes.")


@router.post("/rerank")
async def create_rerank(request: Request):
    request_body_json = await read_json_request_body(request, "rerank endpoint")

    requested_model = request_body_json.get("model")
    if not isinstance(requested_model, str) or not requested_model.strip():
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    query = request_body_json.get("query")
    if not isinstance(query, str) or not query.strip():
        raise HTTPException(status_code=400, detail="Missing 'query' in request body")

    top_n = request_body_json.get("top_n")
    if top_n is not None and (isinstance(top_n, bool) or not isinstance(top_n, int) or top_n < 0):
        raise HTTPException(status_code=400, detail="Invalid 'top_n' in request body; expected non-negative integer")

    return_documents = request_body_json.get("return_documents", False)
    if not isinstance(return_documents, bool):
        raise HTTPException(status_code=400, detail="Invalid 'return_documents' in request body; expected boolean")

    documents = request_body_json.get("documents")
    if (
        "documents" not in request_body_json
        or not isinstance(documents, list)
        or any(not isinstance(document, str) for document in documents)
    ):
        raise HTTPException(status_code=400, detail="Missing or invalid 'documents' in request body; expected list[str]")

    enforce_virtual_key_access(request, requested_model)

    dispatcher, http_client, config_loader_instance, proxy_http_clients = _get_operation_runtime(request)
    routes = dispatcher.lookup_routes("rerank", requested_model)
    if not routes:
        raise HTTPException(status_code=404, detail=f"No rerank route configured for model '{requested_model}'.")

    request_started_at = time.monotonic()

    for route_index, route in enumerate(routes):
        try:
            provider_config = config_loader_instance.providers_config.get(route.provider)
            if provider_config is None:
                logger.error(
                    "Provider '%s' for rerank route '%s' is missing from providers_config.",
                    route.provider,
                    requested_model,
                )
                raise HTTPException(
                    status_code=500,
                    detail="Internal server error: Provider configuration not available.",
                )

            provider_api_key = resolve_provider_api_key(provider_config.apikey)
            target_url = dispatcher.build_target_url(route, provider_config)
            headers = dispatcher.build_headers(route, provider_api_key)
            payload = map_rerank_payload(query, documents, route)
            retry_count, retry_delay = normalize_retry_settings(route.retry_count, route.retry_delay)

            request.state.llmgateway_gateway_model = requested_model
            dispatcher.set_request_state(
                request=request,
                operation="rerank",
                route=route,
                provider_name=route.provider,
                provider_model=route.model,
            )

            effective_client = proxy_http_clients.get(route.provider, http_client)
            downstream_result = await proxy_json_to_downstream(
                target_url,
                headers,
                payload,
                effective_client,
                retry_count=retry_count,
                retry_delay=retry_delay,
            )
            if isinstance(downstream_result, StreamingResponse):
                return downstream_result

            downstream_json, downstream_status_code = downstream_result
            try:
                normalized_rerank_response = normalize_rerank_response(
                    downstream_json,
                    route.response_format,
                    include_index_metadata=True,
                )
                normalized_results = apply_top_n(normalized_rerank_response["data"], top_n)
                normalized_results = add_return_documents(normalized_results, documents, return_documents)
            except ValueError as exc:
                raise HTTPException(
                    status_code=502,
                    detail=f"Invalid downstream rerank response: {exc}",
                ) from exc

            await record_operation_usage(
                request,
                downstream_json,
                gateway_model=requested_model,
                operation="rerank",
                duration_ms=request_duration_ms(request_started_at),
            )
            return json_response(
                format_rerank_response(normalized_results, route.response_output_format),
                downstream_status_code,
            )
        except HTTPException as exc:
            if _should_try_next_route(exc, route_index, routes):
                _log_route_fallback("Rerank", requested_model, route, route_index, routes, exc)
                continue
            raise

    raise HTTPException(status_code=503, detail="Rerank request failed after exhausting fallback routes.")
