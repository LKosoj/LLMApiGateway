"""Record one route attempt for embeddings, images, audio, and rerank.

Chat already writes these rows. The failures page and the usage fallback tab
read the same table, so other operations show up once their attempts are stored.
"""

from __future__ import annotations

import logging

from fastapi import Request

from ...services.accounting import AccountingError
from ...services.error_classifier import classify_error
from ...utils.usage_tracking import extract_request_x_title
from .operation_proxy import request_duration_ms

logger = logging.getLogger(__name__)


def clear_route_key_fingerprint(request: Request) -> None:
    """Drop the previous route's key fingerprint before the next route starts."""
    try:
        request.state.llmgateway_route_key_fingerprint = None
    except Exception:
        return


def record_operation_route_attempt(
    request: Request,
    *,
    operation: str,
    gateway_model: str,
    provider: str,
    model: str,
    success: bool,
    error: BaseException | None = None,
    started_at: float,
    upstream_key_fingerprint: str | None = None,
    use_request_fingerprint: bool = True,
) -> None:
    """Store one attempt. Missing request id or database is a no-op.

    Parallel audio tasks share one request. They pass their own fingerprint and
    set use_request_fingerprint=False so they never read that shared state.
    """
    try:
        if not isinstance(operation, str) or not operation:
            return
        request_id = getattr(request.state, "llmgateway_request_id", None)
        if not isinstance(request_id, str) or not request_id:
            return
        services = request.app.state.services
        fallback_events_db = getattr(services, "fallback_events_db", None)
        if fallback_events_db is None:
            return

        counts = getattr(request.state, "llmgateway_operation_attempt_counts", None)
        if not isinstance(counts, dict):
            counts = {}
            request.state.llmgateway_operation_attempt_counts = counts
        attempt_number = int(counts.get(operation, 0)) + 1
        counts[operation] = attempt_number

        fingerprint = upstream_key_fingerprint
        if fingerprint is None and use_request_fingerprint:
            fingerprint = getattr(request.state, "llmgateway_route_key_fingerprint", None)
        if not isinstance(fingerprint, str) or not fingerprint:
            fingerprint = None

        api_key_id = getattr(request.state, "api_key_id", None)
        if isinstance(api_key_id, bool) or not isinstance(api_key_id, int):
            api_key_id = None

        fallback_events_db.insert_event(
            request_id=request_id,
            gateway_model=_known_name(gateway_model),
            attempt_number=attempt_number,
            provider=_known_name(provider),
            model=_known_name(model),
            success=success,
            error_type=None if success else classify_error(error),
            error_message=None if success else _error_text(error),
            duration_ms=request_duration_ms(started_at),
            operation=operation,
            api_key_id=api_key_id,
            upstream_key_fingerprint=fingerprint,
            x_title=extract_request_x_title(request),
        )
    except Exception as exc:
        logger.debug("Failed to record operation route attempt: %s", exc)


def _known_name(value: object) -> str:
    if isinstance(value, str):
        cleaned = value.strip()
        if cleaned:
            return cleaned
    return "unknown"


def _error_text(error: BaseException | None) -> str | None:
    if error is None:
        return None
    diagnostic_detail = getattr(error, "diagnostic_detail", None)
    if isinstance(diagnostic_detail, str) and diagnostic_detail.strip():
        return diagnostic_detail.strip()
    if isinstance(error, AccountingError):
        return error.code.value
    detail = getattr(error, "detail", None)
    if isinstance(detail, str) and detail.strip():
        return detail.strip()
    status_code = getattr(error, "status_code", None)
    if isinstance(status_code, int):
        return f"Request failed with status code {status_code}"
    return type(error).__name__
