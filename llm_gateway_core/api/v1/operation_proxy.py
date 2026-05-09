import asyncio
import logging
import time
from collections.abc import Mapping, Sequence
from urllib.parse import urlsplit, urlunsplit

import httpx
import json
from fastapi import HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse

from ...utils.text_sanitize import sanitize_payload
from ...utils.usage_tracking import enrich_tokens_usage, extract_tokens_usage

logger = logging.getLogger(__name__)

RETRYABLE_OPERATION_STATUS_CODES = frozenset({408, 409, 429})


async def read_json_request_body(request: Request, endpoint_name: str) -> dict:
    try:
        request_body_json = json.loads((await request.body()).decode("utf-8"))
        if not isinstance(request_body_json, dict):
            raise ValueError("Request body must be a JSON object")
        return sanitize_payload(request_body_json)
    except Exception as exc:
        logger.error("Error reading request body for %s: %s", endpoint_name, exc, exc_info=True)
        raise HTTPException(status_code=400, detail=f"Error reading request body: {exc}") from exc


def extract_downstream_error_detail(response: httpx.Response) -> str:
    try:
        response_json = response.json()
    except ValueError:
        return response.text or f"Downstream request failed with status {response.status_code}."

    if isinstance(response_json, dict):
        detail = response_json.get("detail")
        if isinstance(detail, str) and detail:
            return detail

        error = response_json.get("error")
        if isinstance(error, dict):
            message = error.get("message") or error.get("detail")
            if message:
                return str(message)
            return str(error)
        if error:
            return str(error)

    return response.text or f"Downstream request failed with status {response.status_code}."


def sanitize_target_url_for_log(target_url: str) -> str:
    split_result = urlsplit(target_url)
    if split_result.hostname is None:
        return target_url

    netloc = split_result.hostname
    if split_result.port is not None:
        netloc = f"{netloc}:{split_result.port}"

    return urlunsplit(
        (
            split_result.scheme,
            netloc,
            split_result.path,
            split_result.query,
            split_result.fragment,
        )
    )


def _mark_usd_budget_reservation_finalized(request: Request) -> None:
    if getattr(request.state, "usd_budget_reserved", False):
        request.state.usd_budget_finalized = True


def _commit_usd_budget_reservation(request: Request, cost_usd: float) -> None:
    if not getattr(request.state, "usd_budget_reserved", False):
        return
    if getattr(request.state, "usd_budget_finalized", False):
        return
    key_id = getattr(request.state, "usd_budget_reserved_key_id", None)
    ledger = getattr(request.app.state, "usd_budget_ledger", None)
    if key_id is not None and ledger is not None:
        try:
            ledger.commit(int(key_id), float(cost_usd or 0.0))
        except Exception as exc:
            logger.error("Failed to commit USD budget reservation for key %s: %s", key_id, exc)
            return
    _mark_usd_budget_reservation_finalized(request)


def should_retry_operation_status(status_code: int) -> bool:
    return status_code in RETRYABLE_OPERATION_STATUS_CODES or status_code >= 500


def downstream_error_status_code(status_code: int) -> int:
    return 503 if should_retry_operation_status(status_code) else status_code


async def sleep_before_retry(
    sanitized_target_url: str,
    retry_delay: float,
    attempt_number: int,
    total_attempts: int,
    failure_reason: str,
) -> None:
    remaining_attempts = total_attempts - attempt_number
    logger.warning(
        "Retrying operation request to %s after %s. attempt=%s/%s remaining_retries=%s delay=%ss",
        sanitized_target_url,
        failure_reason,
        attempt_number,
        total_attempts,
        remaining_attempts,
        retry_delay,
    )
    if retry_delay > 0:
        await asyncio.sleep(retry_delay)


async def proxy_json_to_downstream(
    target_url: str,
    headers: dict,
    payload: dict,
    http_client: httpx.AsyncClient,
    retry_count: int = 0,
    retry_delay: float = 0.0,
):
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Proxying operation request to downstream %s", sanitized_target_url)

    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1

        try:
            if payload.get("stream"):
                stream_context = http_client.stream("POST", target_url, headers=headers, json=payload)
                response = await stream_context.__aenter__()
                logger.info(
                    "Operation downstream response status %s from %s",
                    response.status_code,
                    sanitized_target_url,
                )

                if response.status_code >= 400:
                    error_detail = (await response.aread()).decode("utf-8", errors="replace")
                    await stream_context.__aexit__(None, None, None)

                    if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                        await sleep_before_retry(
                            sanitized_target_url,
                            retry_delay,
                            attempt_number,
                            total_attempts,
                            f"retryable downstream status {response.status_code}",
                        )
                        continue

                    raise HTTPException(
                        status_code=downstream_error_status_code(response.status_code),
                        detail=error_detail or f"Downstream request failed with status {response.status_code}.",
                    )

                async def _stream_body():
                    try:
                        async for chunk in response.aiter_bytes():
                            yield chunk
                    finally:
                        await stream_context.__aexit__(None, None, None)

                return StreamingResponse(
                    _stream_body(),
                    media_type=response.headers.get("content-type", "text/event-stream"),
                    status_code=response.status_code,
                )

            response = await http_client.post(target_url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Operation downstream request to %s failed with network error: %s",
                sanitized_target_url,
                exc,
                exc_info=True,
            )
            if attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"network error {type(exc).__name__}",
                )
                continue
            raise HTTPException(status_code=503, detail=f"Downstream request failed: {exc}") from exc

        logger.info(
            "Operation downstream response status %s from %s",
            response.status_code,
            sanitized_target_url,
        )
        if response.status_code >= 400:
            detail = extract_downstream_error_detail(response)
            if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"retryable downstream status {response.status_code}",
                )
                continue
            raise HTTPException(status_code=downstream_error_status_code(response.status_code), detail=detail)

        try:
            return response.json(), response.status_code
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


async def proxy_json_raw_to_downstream(
    target_url: str,
    headers: dict,
    payload: dict,
    http_client: httpx.AsyncClient,
    retry_count: int = 0,
    retry_delay: float = 0.0,
) -> tuple[bytes, int, str | None]:
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Proxying raw JSON operation request to downstream %s", sanitized_target_url)

    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1

        try:
            response = await http_client.post(target_url, headers=headers, json=payload)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Raw JSON operation downstream request to %s failed with network error: %s",
                sanitized_target_url,
                exc,
                exc_info=True,
            )
            if attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"network error {type(exc).__name__}",
                )
                continue
            raise HTTPException(status_code=503, detail=f"Downstream request failed: {exc}") from exc

        logger.info(
            "Raw JSON operation downstream response status %s from %s",
            response.status_code,
            sanitized_target_url,
        )
        if response.status_code >= 400:
            detail = extract_downstream_error_detail(response)
            if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"retryable downstream status {response.status_code}",
                )
                continue
            raise HTTPException(status_code=downstream_error_status_code(response.status_code), detail=detail)

        return response.content, response.status_code, response.headers.get("content-type")

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


async def proxy_multipart_to_downstream(
    target_url: str,
    headers: dict,
    data: Mapping[str, object] | Sequence[tuple[str, object]],
    files: Sequence[tuple[str, tuple[str, bytes, str | None]]],
    http_client: httpx.AsyncClient,
    retry_count: int = 0,
    retry_delay: float = 0.0,
):
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Proxying multipart operation request to downstream %s", sanitized_target_url)
    normalized_data = _normalize_multipart_scalar_data(data)

    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1

        try:
            response = await http_client.post(target_url, headers=headers, data=normalized_data, files=files)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Multipart operation downstream request to %s failed with network error: %s",
                sanitized_target_url,
                exc,
                exc_info=True,
            )
            if attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"network error {type(exc).__name__}",
                )
                continue
            raise HTTPException(status_code=503, detail=f"Downstream request failed: {exc}") from exc

        logger.info(
            "Multipart operation downstream response status %s from %s",
            response.status_code,
            sanitized_target_url,
        )
        if response.status_code >= 400:
            detail = extract_downstream_error_detail(response)
            if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"retryable downstream status {response.status_code}",
                )
                continue
            raise HTTPException(
                status_code=downstream_error_status_code(response.status_code),
                detail=detail,
            )

        try:
            return response.json(), response.status_code
        except ValueError as exc:
            raise HTTPException(status_code=503, detail="Downstream request returned invalid JSON.") from exc

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


async def proxy_multipart_raw_to_downstream(
    target_url: str,
    headers: dict,
    data: Mapping[str, object] | Sequence[tuple[str, object]],
    files: Sequence[tuple[str, tuple[str, bytes, str | None]]],
    http_client: httpx.AsyncClient,
    retry_count: int = 0,
    retry_delay: float = 0.0,
) -> tuple[bytes, int, str | None]:
    sanitized_target_url = sanitize_target_url_for_log(target_url)
    logger.info("Proxying raw multipart operation request to downstream %s", sanitized_target_url)
    normalized_data = _normalize_multipart_scalar_data(data)

    total_attempts = retry_count + 1
    attempt_number = 0

    while attempt_number < total_attempts:
        attempt_number += 1

        try:
            response = await http_client.post(target_url, headers=headers, data=normalized_data, files=files)
        except (httpx.TimeoutException, httpx.RequestError) as exc:
            logger.warning(
                "Raw multipart operation downstream request to %s failed with network error: %s",
                sanitized_target_url,
                exc,
                exc_info=True,
            )
            if attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"network error {type(exc).__name__}",
                )
                continue
            raise HTTPException(status_code=503, detail=f"Downstream request failed: {exc}") from exc

        logger.info(
            "Raw multipart operation downstream response status %s from %s",
            response.status_code,
            sanitized_target_url,
        )
        if response.status_code >= 400:
            detail = extract_downstream_error_detail(response)
            if should_retry_operation_status(response.status_code) and attempt_number < total_attempts:
                await sleep_before_retry(
                    sanitized_target_url,
                    retry_delay,
                    attempt_number,
                    total_attempts,
                    f"retryable downstream status {response.status_code}",
                )
                continue
            raise HTTPException(
                status_code=downstream_error_status_code(response.status_code),
                detail=detail,
            )

        return response.content, response.status_code, response.headers.get("content-type")

    raise HTTPException(status_code=503, detail="Downstream request failed after exhausting retries.")


def _normalize_multipart_scalar_data(
    data: Mapping[str, object] | Sequence[tuple[str, object]],
) -> dict[str, object]:
    if isinstance(data, Mapping):
        return dict(data)

    normalized_data: dict[str, object] = {}
    for field_name, field_value in data:
        if field_name not in normalized_data:
            normalized_data[field_name] = field_value
            continue

        existing_value = normalized_data[field_name]
        if isinstance(existing_value, list):
            existing_value.append(field_value)
            continue

        normalized_data[field_name] = [existing_value, field_value]

    return normalized_data


async def record_operation_usage(
    request: Request,
    downstream_json: dict,
    *,
    gateway_model: str,
    operation: str,
    duration_ms: int | None = None,
) -> None:
    tokens_usage_db = getattr(request.app.state, "tokens_usage_db", None)

    tokens_usage = extract_tokens_usage(downstream_json)
    tokens_usage = enrich_tokens_usage(
        tokens_usage,
        request,
        gateway_model=gateway_model,
        operation=operation,
    )
    if duration_ms is not None:
        tokens_usage["duration_ms"] = duration_ms

    if tokens_usage_db is None:
        logger.warning("TokensUsageDB not found in application state; skipping %s usage persistence.", operation)
    else:
        try:
            tokens_usage_db.insert_usage(tokens_usage)
        except Exception as exc:
            logger.error("Failed to store %s usage statistics: %s", operation, exc, exc_info=True)

    key_id = tokens_usage.get("api_key_id")
    if key_id is None:
        _commit_usd_budget_reservation(request, float(tokens_usage.get("cost") or 0.0))
        return

    api_keys_db = getattr(request.app.state, "api_keys_db", None)
    if api_keys_db is not None:
        try:
            await asyncio.to_thread(
                api_keys_db.record_spent,
                int(key_id),
                float(tokens_usage.get("cost") or 0.0),
            )
        except Exception as exc:
            logger.error("Failed to update spent_usd for key %s after %s: %s", key_id, operation, exc)

    _commit_usd_budget_reservation(request, float(tokens_usage.get("cost") or 0.0))

    rate_limiter = getattr(request.app.state, "rate_limiter", None)
    if rate_limiter is not None:
        try:
            rate_limiter.add_tokens(int(key_id), int(tokens_usage.get("total_tokens") or 0))
        except Exception as exc:
            logger.error("Failed to attribute %s tokens to RateLimiter for key %s: %s", operation, key_id, exc)


def json_response(content: dict, status_code: int) -> JSONResponse:
    return JSONResponse(content=content, status_code=status_code)


def request_duration_ms(started_at: float) -> int:
    return int((time.monotonic() - started_at) * 1000)
