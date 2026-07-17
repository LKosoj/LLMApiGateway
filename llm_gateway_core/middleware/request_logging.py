import time
import logging
import re
import uuid
from fastapi import Request
from typing import Callable
from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

# Use a logger specific to this module
logger = logging.getLogger(__name__)

MAX_REQUEST_ID_BYTES = 255
_REQUEST_ID_RE = re.compile(
    rf"[A-Za-z0-9][A-Za-z0-9._-]{{0,{MAX_REQUEST_ID_BYTES - 1}}}\Z"
)


def is_valid_request_id(value: object) -> bool:
    return isinstance(value, str) and _REQUEST_ID_RE.fullmatch(value) is not None


def _new_request_id() -> str | None:
    candidate = str(uuid.uuid4())
    return candidate if is_valid_request_id(candidate) else None

class RequestLoggingASGIMiddleware:
    """Attach a request ID without a BaseHTTPMiddleware task boundary."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(
        self,
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        start_time = time.time()
        request_id = _new_request_id()
        state = scope.setdefault("state", {})
        state["llmgateway_request_id"] = request_id
        path = str(scope.get("path") or "")
        if path == "/health":
            await self.app(scope, receive, send)
            return

        method = str(scope.get("method") or "")
        client = scope.get("client")
        client_host = client[0] if client else "Unknown"
        logger.info(
            "Incoming request | ID: %s | Method: %s | Path: %s | Client: %s",
            request_id,
            method,
            path,
            client_host,
        )

        async def send_with_request_id(message: Message) -> None:
            if message["type"] != "http.response.start":
                await send(message)
                return

            response_start = dict(message)
            response_start["headers"] = list(message.get("headers", []))
            response_request_id = state.get("llmgateway_request_id")
            if response_request_id == request_id and is_valid_request_id(
                response_request_id
            ):
                MutableHeaders(scope=response_start)["X-Request-ID"] = (
                    response_request_id
                )
            await send(response_start)
            duration = round((time.time() - start_time) * 1000, 2)
            logger.info(
                "Request completed | ID: %s | Status: %s | Duration: %sms",
                request_id,
                response_start.get("status"),
                duration,
            )

        try:
            await self.app(scope, receive, send_with_request_id)
        except Exception as exc:
            duration = round((time.time() - start_time) * 1000, 2)
            logger.error(
                "Request failed | ID: %s | Error: %s | Duration: %sms",
                request_id,
                str(exc),
                duration,
                exc_info=True,
            )
            raise

# Keep the functional style middleware as well if preferred, but class-based is often cleaner
async def log_middleware_functional(request: Request, call_next: Callable):
    start_time = time.time()
    request_id = str(uuid.uuid4())
    request.state.llmgateway_request_id = request_id

    if request.url.path == "/health":
        return await call_next(request)

    logger.info(f"Incoming request | ID: {request_id} | Method: {request.method} | Path: {request.url.path} | Client: {request.client.host if request.client else 'Unknown'}")

    try:
        response = await call_next(request)
        duration = round((time.time() - start_time) * 1000, 2)
        logger.info(f"Request completed | ID: {request_id} | Status: {response.status_code} | Duration: {duration}ms")
        # Add request ID header if it's a standard Response object
        if hasattr(response, "headers"):
             response.headers["X-Request-ID"] = request_id
    except Exception as e:
        duration = round((time.time() - start_time) * 1000, 2)
        logger.error(f"Request failed | ID: {request_id} | Error: {str(e)} | Duration: {duration}ms", exc_info=True)
        raise e

    return response
