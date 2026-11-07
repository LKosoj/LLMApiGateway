"""Keep a non-streaming Fusion response alive until the ensemble finishes.

Cherry and the playground both ask for one JSON document (``stream: false``).
Fusion can run for many minutes and otherwise sends nothing until the end, so
those clients close the connection. After a short wait this response sends
whitespace bytes, then the same JSON document.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import Any

from fastapi import HTTPException, Request
from starlette.responses import JSONResponse, Response
from starlette.types import Receive, Scope, Send

from ..api.error_envelope import (
    StructuredHTTPException,
    code_from_detail,
    error_response,
    message_from_detail,
)

logger = logging.getLogger(__name__)

FUSION_KEEPALIVE_INTERVAL_SECONDS = 15.0
JSON_KEEPALIVE_HEADER = b"x-llmgateway-json-keepalive"
DELEGATE_DISPATCH_STATE_ATTR = "llmgateway_delegate_dispatch"
_CHAT_COMPLETION_PATHS = frozenset({
    "/v1/chat/completions",
    "/v1/v1/chat/completions",
})
_SLOW_HEADERS = (
    (b"content-type", b"application/json"),
    (b"content-encoding", b"identity"),
    (b"cache-control", b"no-transform"),
    (b"x-accel-buffering", b"no"),
    (JSON_KEEPALIVE_HEADER, b"1"),
)
_LATE_FAILURE_BODY = b'{"error":{"message":"Fusion request failed."}}'


def json_keepalive_enabled(headers: tuple[tuple[bytes, bytes], ...]) -> bool:
    for name, value in headers:
        if name.lower() == JSON_KEEPALIVE_HEADER and value.strip() == b"1":
            return True
    return False


def fusion_response_should_keepalive(request: Request) -> bool:
    """Outer chat-completions calls keep the connection alive. Nested delegates do not."""
    if getattr(request.state, DELEGATE_DISPATCH_STATE_ATTR, False):
        return False
    return request.scope.get("path") in _CHAT_COMPLETION_PATHS


class FusionKeepaliveJsonResponse(Response):
    """One JSON document, with whitespace on the wire while Fusion is still running."""

    def __init__(
        self,
        request: Request,
        producer: Callable[[], Awaitable[dict[str, Any]]],
        *,
        interval_seconds: float = FUSION_KEEPALIVE_INTERVAL_SECONDS,
    ) -> None:
        super().__init__(content=b"", media_type="application/json")
        self._request = request
        self._producer = producer
        self._interval = interval_seconds

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        task = asyncio.create_task(self._drive(), name="fusion-response")
        try:
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._interval)
            except TimeoutError:
                await self._send_slow(task, scope, receive, send)
            except asyncio.CancelledError:
                raise
            except BaseException:
                if not task.done():
                    raise
                await self._send_fast(task, scope, receive, send)
            else:
                await self._send_fast(task, scope, receive, send)
        finally:
            if not task.done():
                task.cancel()
                try:
                    await task
                except BaseException:
                    pass

    async def _drive(self) -> tuple[str, object]:
        try:
            payload = await self._producer()
        except HTTPException as exc:
            return ("http", exc)
        if not isinstance(payload, dict):
            raise RuntimeError("Fusion response must be a JSON object")
        return ("ok", payload)

    async def _send_fast(
        self,
        task: asyncio.Task[tuple[str, object]],
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        kind, value = self._outcome(task)
        if kind == "raise":
            assert isinstance(value, BaseException)
            raise value
        if kind == "http":
            assert isinstance(value, HTTPException)
            await self._http_exception_response(value)(scope, receive, send)
            return
        assert isinstance(value, dict)
        await JSONResponse(content=value)(scope, receive, send)

    async def _send_slow(
        self,
        task: asyncio.Task[tuple[str, object]],
        scope: Scope,
        receive: Receive,
        send: Send,
    ) -> None:
        if task.done():
            await self._send_fast(task, scope, receive, send)
            return
        await send(
            {
                "type": "http.response.start",
                "status": 200,
                "headers": list(_SLOW_HEADERS),
            }
        )
        while not task.done():
            await send(
                {
                    "type": "http.response.body",
                    "body": b"\n",
                    "more_body": True,
                }
            )
            try:
                await asyncio.wait_for(asyncio.shield(task), timeout=self._interval)
            except TimeoutError:
                continue
            except asyncio.CancelledError:
                raise
            except BaseException:
                if not task.done():
                    raise
                break
        await self._send_slow_body(task, send)

    async def _send_slow_body(
        self,
        task: asyncio.Task[tuple[str, object]],
        send: Send,
    ) -> None:
        kind, value = self._outcome(task)
        if kind == "ok":
            assert isinstance(value, dict)
            body = JSONResponse(content=value).body
        elif kind == "http":
            assert isinstance(value, HTTPException)
            logger.warning(
                "Fusion failed after keepalive started: %s",
                message_from_detail(value.detail),
            )
            body = _error_body(message_from_detail(value.detail))
        else:
            assert isinstance(value, BaseException)
            if isinstance(value, asyncio.CancelledError):
                raise value
            logger.warning(
                "Fusion failed after keepalive started: %s",
                type(value).__name__,
            )
            body = _LATE_FAILURE_BODY
        await send({"type": "http.response.body", "body": body, "more_body": False})

    def _http_exception_response(self, exc: HTTPException) -> JSONResponse:
        extra = exc.extra_payload if isinstance(exc, StructuredHTTPException) else None
        return error_response(
            self._request,
            status_code=exc.status_code,
            detail=exc.detail,
            code=code_from_detail(exc.detail),
            headers=exc.headers,
            extra=extra,
        )

    @staticmethod
    def _outcome(task: asyncio.Task[tuple[str, object]]) -> tuple[str, object]:
        if task.cancelled():
            raise asyncio.CancelledError
        exc = task.exception()
        if exc is not None:
            return ("raise", exc)
        return task.result()


def _error_body(message: str) -> bytes:
    return json.dumps(
        {"error": {"message": message}},
        ensure_ascii=False,
        allow_nan=False,
        separators=(",", ":"),
    ).encode("utf-8")
