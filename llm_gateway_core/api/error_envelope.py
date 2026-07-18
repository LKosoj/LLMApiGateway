from __future__ import annotations

from typing import Any, Mapping

from fastapi import HTTPException, Request, status
from fastapi.responses import JSONResponse


class StructuredHTTPException(HTTPException):
    """``HTTPException`` that carries extra top-level error-envelope fields.

    ``extra_payload`` is merged into the JSON body by
    :func:`build_error_payload`/:func:`error_response` without overwriting
    the reserved ``detail``/``error``/``request_id`` keys.
    """

    def __init__(
        self,
        status_code: int,
        detail: Any = None,
        *,
        extra_payload: Mapping[str, Any] | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        super().__init__(status_code=status_code, detail=detail, headers=headers)
        self.extra_payload = extra_payload


def get_request_id(request: Request) -> str | None:
    request_id = getattr(request.state, "llmgateway_request_id", None)
    if isinstance(request_id, str) and request_id:
        return request_id

    active_request_id = getattr(request.state, "llmgateway_active_request_id", None)
    if isinstance(active_request_id, str) and active_request_id:
        return active_request_id

    return None


def error_type_for_status(status_code: int) -> str:
    if status_code == status.HTTP_401_UNAUTHORIZED:
        return "authentication_error"
    if status_code == status.HTTP_403_FORBIDDEN:
        return "permission_error"
    if status_code == status.HTTP_429_TOO_MANY_REQUESTS:
        return "rate_limit_error"
    if 400 <= status_code < 500:
        return "invalid_request_error"
    if 500 <= status_code < 600:
        return "internal_error"
    return "http_error"


def code_from_detail(detail: Any) -> str | None:
    if isinstance(detail, dict):
        code = detail.get("code")
        if isinstance(code, str):
            return code
    return None


def message_from_detail(detail: Any) -> str:
    if isinstance(detail, str):
        return detail
    if isinstance(detail, dict):
        message = detail.get("message") or detail.get("detail") or detail.get("error")
        if isinstance(message, str):
            return message
    return str(detail)


def build_error_payload(
    request: Request,
    *,
    status_code: int,
    detail: Any,
    error_type: str | None = None,
    code: str | None = None,
    extra: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "detail": detail,
        "error": {
            "message": message_from_detail(detail),
            "type": error_type or error_type_for_status(status_code),
            "code": code or f"http_{status_code}",
        },
    }

    request_id = get_request_id(request)
    if request_id:
        payload["request_id"] = request_id
        payload["error"]["request_id"] = request_id

    if extra:
        extra_fields = dict(extra)
        for reserved_key in ("detail", "error", "request_id"):
            extra_fields.pop(reserved_key, None)
        payload.update(extra_fields)

    return payload


def error_response(
    request: Request,
    *,
    status_code: int,
    detail: Any,
    error_type: str | None = None,
    code: str | None = None,
    headers: dict[str, str] | None = None,
    extra: Mapping[str, Any] | None = None,
) -> JSONResponse:
    response = JSONResponse(
        status_code=status_code,
        content=build_error_payload(
            request,
            status_code=status_code,
            detail=detail,
            error_type=error_type,
            code=code,
            extra=extra,
        ),
        headers=headers,
    )

    request_id = get_request_id(request)
    if request_id:
        response.headers["X-Request-ID"] = request_id

    return response
