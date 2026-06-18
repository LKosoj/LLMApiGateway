import copy
import httpx
import json
import logging
import codecs
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Dict, Any, Optional

from fastapi import Request
from fastapi.responses import StreamingResponse

from llm_gateway_core.config.loader import OperationRoute, ProviderDetails
from llm_gateway_core.services.active_requests import update_active_request_from_state
from llm_gateway_core.services.model_policy import resolve_model_name
from llm_gateway_core.services.payload_transform import apply_payload_transforms
from llm_gateway_core.utils.text_sanitize import sanitize_payload

DEFAULT_RETRY_COUNT = 0
DEFAULT_RETRY_DELAY_SECONDS = 0.0
# Upstream Retry-After hints beyond this threshold would deadlock the gateway waiting
# on a single slow provider. Clamp to the same 120s ceiling that bounds retry_delay.
MAX_RETRY_AFTER_SECONDS = 120.0


class RequestErrorDetail(str):
    """``str`` subclass carrying optional metadata alongside the error message.

    Wrapped as ``str`` so existing call sites (logging, comparisons,
    error classification) keep working without changes. The ``retry_after``
    attribute holds the upstream ``Retry-After`` hint (already clamped and
    converted to seconds) when available, and ``status_code`` is the HTTP
    status that produced the failure (if applicable).
    """

    retry_after: float | None
    status_code: int | None

    def __new__(
        cls,
        message: str,
        *,
        retry_after: float | None = None,
        status_code: int | None = None,
    ) -> "RequestErrorDetail":
        instance = super().__new__(cls, message)
        instance.retry_after = retry_after
        instance.status_code = status_code
        return instance


def parse_retry_after_header(value: object) -> float | None:
    """Parse an RFC 7231 ``Retry-After`` header value into seconds.

    Accepts either delta-seconds (``"15"``) or an HTTP-date
    (``"Wed, 21 Oct 2015 07:28:00 GMT"``). Returns ``None`` when the input
    cannot be parsed. The caller is responsible for clamping to a sane
    upper bound via :data:`MAX_RETRY_AFTER_SECONDS`.
    """
    if value is None:
        return None

    if not isinstance(value, str):
        value = str(value)

    value = value.strip()
    if not value:
        return None

    try:
        seconds = float(value)
        if seconds < 0:
            return None
        return seconds
    except ValueError:
        pass

    try:
        target = parsedate_to_datetime(value)
    except (TypeError, ValueError):
        return None
    if target is None:
        return None

    if target.tzinfo is None:
        target = target.replace(tzinfo=timezone.utc)
    delta = (target - datetime.now(tz=timezone.utc)).total_seconds()
    if delta < 0:
        return 0.0
    return delta


def _clamped_retry_after(value: float | None) -> float | None:
    if value is None:
        return None
    if value <= 0:
        return 0.0
    return min(value, MAX_RETRY_AFTER_SECONDS)
FORBIDDEN_CUSTOM_BODY_PARAM_KEYS = frozenset({"stream", "messages", "tool_choice", "tools", "model"})
SECURITY_HEADER_NAMES = frozenset({"authorization", "cookie", "x-api-key"})
PROTECTED_ROUTE_OVERRIDE_KEYS = frozenset({"prompt", "images", "mask"})
OPERATION_BODY_PARAM_ALLOWLIST = {
    "embeddings": frozenset({"dimensions", "encoding_format", "user"}),
    "rerank": frozenset({"top_n", "return_documents", "max_chunks_per_doc"}),
}
SUPPORTED_OPERATION_TYPES = frozenset(
    {
        "embeddings",
        "rerank",
        "images_generations",
        "images_edits",
        "audio_speech",
        "audio_transcriptions",
        "pdf_conversions",
        "web_search",
        "web_read",
        "web_research",
        "web_deep_research",
    }
)


class OperationDispatcher:
    """Dispatches operation requests for non-chat endpoints."""

    def __init__(
        self,
        providers_config: Dict[str, Any],
        operation_rules: Dict[str, Dict[str, Any]],
        http_client: httpx.AsyncClient,
        model_rules: Dict[str, Any] | None = None,
    ):
        """
        Initialize the dispatcher with providers, operation rules, and shared HTTP client.

        Args:
            providers_config: Dictionary mapping provider names to ProviderDetails
            operation_rules: Dictionary with operation sections,
                           each mapping gateway_model names to route configurations
            http_client: Shared httpx.AsyncClient from app.state, used for making requests
        """
        self._providers_config = providers_config
        self._operation_rules = operation_rules
        self._http_client = http_client
        self._model_rules = model_rules or {}

    def lookup_route(self, operation: str, gateway_model: str) -> Optional[OperationRoute]:
        """
        Look up the first configured route for a given operation type and gateway model.

        Endpoints that support ordered fallback should use ``lookup_routes`` instead.

        Args:
            operation: Operation type configured in models_operation_rules.json
            gateway_model: Gateway model name to look up

        Returns:
            The first OperationRoute for the model, or None if not found or operation unknown
        """
        routes = self.lookup_routes(operation, gateway_model)
        if not routes:
            return None
        return routes[0]

    def lookup_routes(self, operation: str, gateway_model: str) -> list[OperationRoute]:
        """
        Look up all configured routes for a given operation type and gateway model.

        The returned order matches models_operation_rules.json. Embeddings and rerank
        endpoints use this order as their fallback chain.
        """
        # For unknown operation types, return an empty route list.
        if operation not in SUPPORTED_OPERATION_TYPES:
            return []

        # Get the section for the operation type
        section = self._operation_rules.get(operation)
        if not section:
            return []

        try:
            model_resolution = resolve_model_name(gateway_model, self._model_rules)
        except ValueError:
            return []

        # Get routes for the gateway model
        model_config = section.get(model_resolution.effective_model)
        if not model_config:
            return []

        routes = model_config.get("routes")
        if not routes or not isinstance(routes, list):
            return []

        parsed_routes: list[OperationRoute] = []
        try:
            for route in routes:
                parsed_routes.append(OperationRoute(**route))
        except (ValueError, TypeError):
            return []

        return parsed_routes

    def build_target_url(self, route: OperationRoute, provider_config: ProviderDetails) -> str:
        """Build a downstream target URL from provider base URL and operation route path."""
        if route.target_path.startswith(("http://", "https://")):
            return route.target_path
        return f"{provider_config.baseUrl.rstrip('/')}/{route.target_path.lstrip('/')}"

    def build_headers(
        self,
        route: OperationRoute,
        provider_api_key: str | None = None,
        auth_headers: dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        """Build downstream headers for an operation request."""
        headers: Dict[str, Any] = {
            "Content-Type": "application/json",
        }
        if auth_headers:
            headers.update(auth_headers)
        elif provider_api_key:
            headers["Authorization"] = f"Bearer {provider_api_key}"

        headers.update(route.custom_headers)
        return headers

    def filter_custom_headers(self, custom_headers: dict, allowlist: list[str]) -> Dict[str, Any]:
        """Keep only allowed custom headers while blocking security-sensitive headers."""
        allowed_headers = {header.lower() for header in allowlist}
        filtered_headers: Dict[str, Any] = {}

        for header_name, header_value in custom_headers.items():
            normalized_header_name = header_name.lower()
            if normalized_header_name in SECURITY_HEADER_NAMES:
                continue
            if normalized_header_name in allowed_headers:
                filtered_headers[header_name] = header_value

        return filtered_headers

    def merge_custom_params(self, base_payload: dict, route: OperationRoute, operation: str) -> dict:
        """Merge only allowed custom body params into the payload without overriding model."""
        merged_payload = copy.deepcopy(base_payload)
        allowed_params = OPERATION_BODY_PARAM_ALLOWLIST.get(operation, frozenset())

        for param_name, param_value in route.custom_body_params.items():
            normalized_param_name = param_name.lower()
            if normalized_param_name in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                continue
            if normalized_param_name not in allowed_params:
                continue
            if normalized_param_name == "model":
                continue
            merged_payload[param_name] = param_value

        return apply_payload_transforms(merged_payload, route.payload_transforms)

    def merge_all_non_reserved_custom_params(self, base_payload: dict, route: OperationRoute) -> dict:
        """Merge route custom body params while only blocking reserved keys."""
        merged_payload = copy.deepcopy(base_payload)

        for param_name, param_value in route.custom_body_params.items():
            normalized_param_name = param_name.lower()
            if normalized_param_name in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                continue
            if normalized_param_name == "model":
                continue
            if normalized_param_name in PROTECTED_ROUTE_OVERRIDE_KEYS:
                continue
            merged_payload[param_name] = param_value

        return apply_payload_transforms(merged_payload, route.payload_transforms)

    def build_payload(self, request_body: dict, route: OperationRoute, operation: str) -> dict:
        """Build downstream payload by replacing model and merging only allowed custom params."""
        payload = copy.deepcopy(request_body)
        for key in list(payload.keys()):
            if key.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                payload.pop(key)

        payload["model"] = route.model
        return self.merge_custom_params(payload, route, operation)

    def build_payload_with_route_overrides(self, request_body: dict, route: OperationRoute) -> dict:
        """Build downstream payload by replacing model and merging all non-reserved custom params."""
        payload = copy.deepcopy(request_body)
        for key in list(payload.keys()):
            if key.lower() in FORBIDDEN_CUSTOM_BODY_PARAM_KEYS:
                payload.pop(key)

        payload["model"] = route.model
        return self.merge_all_non_reserved_custom_params(payload, route)

    def set_request_state(
        self,
        request: Request,
        operation: str,
        route: OperationRoute,
        provider_name: str,
        provider_model: str,
    ) -> None:
        """Populate request.state with dispatcher metadata before the downstream request."""
        gateway_model = getattr(request.state, "llmgateway_gateway_model", None)
        if not isinstance(gateway_model, str) or not gateway_model:
            raise ValueError("request.state.llmgateway_gateway_model must be set before dispatching an operation")

        request.state.llmgateway_gateway_model = gateway_model
        request.state.llmgateway_provider = provider_name
        request.state.llmgateway_provider_model = provider_model
        request.state.llmgateway_operation = operation
        request.state.llmgateway_target_path = route.target_path
        update_active_request_from_state(request)


def _split_sse_buffer(buffer: str, text: str) -> tuple[list[str], str]:
    buffer += text
    parts = buffer.split("\n\n")
    buffer = parts.pop() if not buffer.endswith("\n\n") else ""
    return parts, buffer


def _parse_stream_chunk_json(chunk_str: str):
    data_lines: list[str] = []
    for line in chunk_str.splitlines():
        if line.startswith("data:"):
            data_lines.append(line[len("data:"):].lstrip())

    if not data_lines:
        return None

    data = "\n".join(data_lines).strip()
    if data == "[DONE]":
        return "[DONE]"
    if not data.startswith("{"):
        return None

    return sanitize_payload(json.loads(data))


def _is_success_stream_code(code: object, message: object) -> bool:
    if code != 0 and not (isinstance(code, str) and code.strip() == "0"):
        return False
    if message is None:
        return True
    if isinstance(message, str):
        return message.strip().lower() in {"", "success"}
    return False


def _extract_stream_error_detail(chunk_json: object) -> str | None:
    if not isinstance(chunk_json, dict):
        return None

    if "error" in chunk_json:
        error = chunk_json.get("error")
        if isinstance(error, dict):
            return error.get("message") or str(error)
        return str(error)

    if chunk_json.get("type") == "error":
        message = chunk_json.get("message")
        if message:
            return str(message)
        return str(chunk_json)

    if "detail" in chunk_json:
        return str(chunk_json.get("detail"))

    if "code" in chunk_json:
        code = chunk_json.get("code")
        message = chunk_json.get("message")
        if _is_success_stream_code(code, message):
            return None
        return str(message or code)

    return None


def _normalize_non_negative_int(value: object, default: int, setting_name: str) -> int:
    if value is None:
        return default

    try:
        normalized_value = int(value)
    except (TypeError, ValueError):
        logging.warning(f"Invalid {setting_name}={value!r}. Falling back to default {default}.")
        return default

    if normalized_value < 0:
        logging.warning(f"Negative {setting_name}={normalized_value}. Falling back to default {default}.")
        return default

    return normalized_value


def _normalize_non_negative_float(value: object, default: float, setting_name: str) -> float:
    if value is None:
        return default

    try:
        normalized_value = float(value)
    except (TypeError, ValueError):
        logging.warning(f"Invalid {setting_name}={value!r}. Falling back to default {default}.")
        return default

    if normalized_value < 0:
        logging.warning(f"Negative {setting_name}={normalized_value}. Falling back to default {default}.")
        return default

    return normalized_value


def normalize_retry_settings(retry_count: object, retry_delay: object) -> tuple[int, float]:
    normalized_retry_count = _normalize_non_negative_int(retry_count, DEFAULT_RETRY_COUNT, "retry_count")
    normalized_retry_delay = _normalize_non_negative_float(retry_delay, DEFAULT_RETRY_DELAY_SECONDS, "retry_delay")
    return normalized_retry_count, normalized_retry_delay


def _format_request_error_detail(target_url: str, error: httpx.RequestError) -> str:
    error_type = error.__class__.__name__
    error_text = str(error).strip()
    if error_text:
        return f"{error_type} connecting to {target_url}: {error_text}"
    return f"{error_type} connecting to {target_url}"


def _stream_chunk_has_response_content(chunk_json: object) -> bool:
    if not isinstance(chunk_json, dict):
        return False

    choices = chunk_json.get("choices")
    if isinstance(choices, list):
        for choice in choices:
            if not isinstance(choice, dict):
                continue
            delta = choice.get("delta")
            if isinstance(delta, dict):
                for key in ("content", "reasoning", "reasoning_content", "function_call", "tool_calls"):
                    if delta.get(key):
                        return True
            message = choice.get("message")
            if isinstance(message, dict) and message.get("content"):
                return True

    delta = chunk_json.get("delta")
    if isinstance(delta, dict):
        for key in ("text", "partial_json", "thinking"):
            if delta.get(key):
                return True

    content = chunk_json.get("content")
    if isinstance(content, str):
        return bool(content)
    if isinstance(content, list):
        return bool(content)
    return False


async def _make_streaming_request(
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict,
    request_payload: dict,
):
    """Stream-mode branch of make_llm_request.

    Primes the upstream SSE stream until the first complete event to fail closed
    on empty/error streams, then hands an inner ``combined_generator`` to
    ``StreamingResponse`` that replays prefetched chunks and continues consuming
    the rest of the body. Extracted verbatim from make_llm_request() — no
    behavior change.
    """
    error_in_stream = False
    error_detail = None
    tokens_usage = None

    stream_context = client.stream("POST", target_url, headers=headers, json=request_payload)
    stream_open = False
    stream_handed_off = False
    try:
        response = await stream_context.__aenter__()
        stream_open = True
        body_iterator = response.aiter_bytes()
        prefetched_chunks: list[bytes] = []
        saw_real_data_chunk = False
        buffer = ""
        decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")

        if response.status_code >= 400:
            error_text = (await response.aread()).decode("utf-8")
            logging.error(f"Downstream error {response.status_code} from {target_url}: {error_text}")
            headers = getattr(response, "headers", None) or {}
            retry_after = _clamped_retry_after(
                parse_retry_after_header(headers.get("retry-after") if hasattr(headers, "get") else None)
            )
            return None, RequestErrorDetail(
                error_text,
                retry_after=retry_after,
                status_code=response.status_code,
            )

        # Prime until the first complete SSE event so empty/error streams fail closed.
        while True:
            try:
                chunk = await anext(body_iterator)
            except StopAsyncIteration:
                break

            if not chunk:
                logging.debug(f"Skipping empty chunk received from {target_url}")
                continue

            prefetched_chunks.append(chunk)
            try:
                text_chunk = decoder.decode(chunk)
                parts, buffer = _split_sse_buffer(buffer, text_chunk)
                for part in parts:
                    if not part:
                        continue

                    chunk_json = _parse_stream_chunk_json(part)

                    if chunk_json == "[DONE]":
                        error_detail = "Stream ended before any content chunks were received."
                        error_in_stream = True
                        break

                    if chunk_json is None:
                        continue

                    error_detail_candidate = _extract_stream_error_detail(chunk_json)
                    if error_detail_candidate:
                        error_detail = error_detail_candidate
                        error_in_stream = True
                        break

                    if not _stream_chunk_has_response_content(chunk_json):
                        continue
                    saw_real_data_chunk = True
                    if "usage" in chunk_json:
                        tokens_usage = chunk_json.get("usage")
                    break

                if error_in_stream or saw_real_data_chunk:
                    break
            except UnicodeDecodeError:
                continue
            except Exception as e:
                logging.warning(
                    "Priming stream chunk inspection failed for %s. Error=%s. Chunk=%s",
                    target_url,
                    e,
                    chunk[:4000],
                )
                break

        if error_in_stream:
            return None, error_detail

        if not prefetched_chunks or not saw_real_data_chunk:
            return None, "Stream ended before any content chunks were received."

        async def combined_generator():
            nonlocal error_in_stream, error_detail, tokens_usage, saw_real_data_chunk, stream_open
            try:
                inner_decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
                for prefetched_chunk in prefetched_chunks:
                    logging.debug(f"Yielding prefetched chunk from {target_url}: {prefetched_chunk[:1000]}...")
                    # Maintain decoder state even if we don't re-inspect prefetched chunks
                    inner_decoder.decode(prefetched_chunk)
                    yield prefetched_chunk

                buffer = ""
                async for chunk in body_iterator:
                    if not chunk:
                        logging.debug(f"Skipping empty chunk received from {target_url}")
                        continue

                    should_stop = False
                    try:
                        text_chunk = inner_decoder.decode(chunk)
                        parts, buffer = _split_sse_buffer(buffer, text_chunk)

                        for chunk_str in parts:
                            if not chunk_str:
                                continue

                            chunk_json = _parse_stream_chunk_json(chunk_str)

                            if chunk_json == "[DONE]":
                                if not saw_real_data_chunk:
                                    error_detail = "Stream ended before any content chunks were received."
                                    error_in_stream = True
                                should_stop = True
                                break

                            if chunk_json is None:
                                continue

                            error_detail_candidate = _extract_stream_error_detail(chunk_json)
                            if error_detail_candidate:
                                logging.warning(f"Error detected in stream chunk from {target_url}: {error_detail_candidate}")
                                error_in_stream = True
                                error_detail = error_detail_candidate
                                should_stop = True
                                break

                            if _stream_chunk_has_response_content(chunk_json):
                                saw_real_data_chunk = True
                            if "usage" in chunk_json:
                                tokens_usage = chunk_json.get("usage")
                    except Exception as e:
                        logging.warning(f"CombinedGenerator: Could not decode chunk. Skipping content check for this chunk. Error={e}. Chunk={chunk}")

                    logging.debug(f"Yielding chunk from {target_url}: {chunk[:1000]}...")
                    yield chunk

                    if should_stop:
                        break

                logging.info(f"Finished streaming from {target_url}. Token Usage: {tokens_usage if tokens_usage else ''}")
            finally:
                if stream_open:
                    stream_open = False
                    await stream_context.__aexit__(None, None, None)

        stream_handed_off = True
        return StreamingResponse(
            combined_generator(),
            media_type="text/event-stream",
            headers={"Transfer-Encoding": "chunked", "X-Accel-Buffering": "no"},
        ), error_detail
    finally:
        if stream_open and not stream_handed_off:
            stream_open = False
            await stream_context.__aexit__(None, None, None)


async def _make_json_request(
    client: httpx.AsyncClient,
    target_url: str,
    headers: dict,
    request_payload: dict,
):
    """Non-stream branch of make_llm_request.

    Returns ``(response_json, None)`` on success or ``(None, error_detail)``
    on any downstream failure. Extracted verbatim from make_llm_request() —
    no behavior change.
    """
    response = await client.post(target_url, headers=headers, json=request_payload)
    logging.debug(f"Response received from {target_url}")

    if response.status_code >= 400:
        error_text = response.text
        logging.warning(f"Downstream error {response.status_code} from {target_url}: {error_text}")
        headers = getattr(response, "headers", None) or {}
        retry_after = _clamped_retry_after(
            parse_retry_after_header(headers.get("retry-after") if hasattr(headers, "get") else None)
        )
        return None, RequestErrorDetail(
            error_text,
            retry_after=retry_after,
            status_code=response.status_code,
        )

    try:
        response_json = response.json()
    except ValueError as json_err:
        error_detail = f"Invalid JSON response from {target_url}. Error={json_err}. Response= {response.text[:1000]}..."
        logging.error(error_detail, exc_info=True)
        return None, error_detail

    if not isinstance(response_json, dict):
        error_detail = (
            f"Non-object JSON body from {target_url}: "
            f"type={type(response_json).__name__}. Response= {response.text[:1000]}..."
        )
        logging.error(error_detail)
        return None, error_detail

    if "error" in response_json or "detail" in response_json:
        if "error" in response_json:
            error = response_json.get("error")
            if isinstance(error, dict):
                error_detail = error.get("message")
            elif isinstance(error, str):
                error_detail = error
            else:
                error_detail = repr(error)
        else:
            error_detail = None
        error_detail = error_detail or response_json.get("detail")
        logging.warning(f"Error detected in non-stream response from {target_url}: {error_detail}")
        return None, error_detail
    return response_json, None


async def make_llm_request(client: httpx.AsyncClient, target_url: str, headers: dict, payload: dict, is_streaming: bool):
    """Makes the downstream request and handles streaming/non-streaming responses."""
    request_payload = sanitize_payload(payload)

    logging.debug(
        "make_llm_request(): Sending request for model '%s'. Payload: %s",
        request_payload.get("model"),
        {k: v for k, v in request_payload.items() if k != "messages"},
    )
    try:
        if is_streaming:
            return await _make_streaming_request(client, target_url, headers, request_payload)
        return await _make_json_request(client, target_url, headers, request_payload)
    except httpx.RequestError as e:
        error_detail = _format_request_error_detail(target_url, e)
        logging.error(error_detail, exc_info=True)
        return None, error_detail
    except Exception as e:
        error_detail = f"Unexpected error during request to {target_url}: {str(e)}"
        logging.error(error_detail, exc_info=True)
        return None, error_detail
