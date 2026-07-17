import json
from collections import deque

from starlette.types import ASGIApp, Message, Receive, Scope, Send


BODYLESS_HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
RUNTIME_INDEPENDENT_PATHS = frozenset({"/health", "/healthz"})
PUBLIC_BODYLESS_EXACT_PATHS = frozenset({"/", *RUNTIME_INDEPENDENT_PATHS})
PUBLIC_BODYLESS_PATH_PREFIXES = ("/static/",)
LOGIN_REQUEST_PATH = "/auth/login"
# Login accepts one small JSON object. Keep this independent of the much larger
# model request limit so unauthenticated traffic cannot retain a 100 MiB body.
LOGIN_MAX_REQUEST_BODY_BYTES = 64 * 1024
PRE_ROUTE_REJECTED_STATE_KEY = "llmgateway_pre_route_rejected"
UNMATCHED_ROUTE_STATE_KEY = "llmgateway_unmatched_route"


class RequestBodyTooLarge(Exception):
    pass


def contains_request_body_too_large(exc: BaseException) -> bool:
    if isinstance(exc, RequestBodyTooLarge):
        return True
    if isinstance(exc, BaseExceptionGroup):
        return any(contains_request_body_too_large(child) for child in exc.exceptions)
    return False


class ContentSizeLimitMiddleware:
    def __init__(self, app: ASGIApp, *, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if _is_unmatched_route(scope):
            await self.app(scope, receive, send)
            return

        max_body_size = _request_body_limit(scope, self.max_body_size)
        if max_body_size is None:
            await self.app(scope, receive, send)
            return
        if (
            max_body_size < self.max_body_size
            and _content_length_exceeds_limit(scope, max_body_size)
        ):
            _mark_pre_route_rejected(scope)
            await _send_too_large(send)
            return

        block_size = max(1, min(max_body_size, 64 * 1024))
        body_blocks: deque[bytearray] = deque()
        bytes_seen = 0
        terminal_request = False
        buffered_disconnect: Message | None = None
        while True:
            message = await receive()
            if message["type"] == "http.request":
                body = message.get("body", b"")
                bytes_seen += len(body)
                if bytes_seen > max_body_size:
                    del body, message
                    body_blocks.clear()
                    _mark_pre_route_rejected(scope)
                    await _send_too_large(send)
                    return
                self._append_body_blocks(body_blocks, body, block_size)
                more_body = bool(message.get("more_body", False))
                del body, message
                if more_body:
                    continue
                terminal_request = True
            else:
                buffered_disconnect = message
                del message
            break

        empty_terminal_pending = terminal_request and not body_blocks

        async def replay_receive() -> Message:
            nonlocal empty_terminal_pending, buffered_disconnect
            if body_blocks:
                body = bytes(body_blocks.popleft())
                return {
                    "type": "http.request",
                    "body": body,
                    "more_body": bool(body_blocks) or not terminal_request,
                }
            if empty_terminal_pending:
                empty_terminal_pending = False
                return {
                    "type": "http.request",
                    "body": b"",
                    "more_body": False,
                }
            if buffered_disconnect is not None:
                message = buffered_disconnect
                buffered_disconnect = None
                return message
            return await receive()

        await self.app(scope, replay_receive, send)

    @staticmethod
    def _append_body_blocks(
        blocks: deque[bytearray],
        body: bytes,
        block_size: int,
    ) -> None:
        offset = 0
        while offset < len(body):
            if not blocks or len(blocks[-1]) == block_size:
                blocks.append(bytearray())
            available = block_size - len(blocks[-1])
            next_offset = min(offset + available, len(body))
            blocks[-1].extend(body[offset:next_offset])
            offset = next_offset


class ContentLengthLimitMiddleware:
    """Reject a declared oversized HTTP body without consuming the request."""

    def __init__(self, app: ASGIApp, *, max_body_size: int) -> None:
        self.app = app
        self.max_body_size = max_body_size

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        if _content_length_exceeds_limit(scope, self.max_body_size):
            await _send_too_large(send)
            return
        await self.app(scope, receive, send)


def _content_length_exceeds_limit(scope: Scope, max_body_size: int) -> bool:
    for raw_name, raw_value in scope.get("headers") or []:
        if raw_name.lower() != b"content-length":
            continue
        try:
            content_length = int(raw_value.decode("latin-1"))
        except (TypeError, ValueError):
            continue
        if content_length > max_body_size:
            return True
    return False


def _request_body_limit(scope: Scope, default_limit: int) -> int | None:
    method = str(scope.get("method") or "").upper()
    path = str(scope.get("path") or "")
    if method in BODYLESS_HTTP_METHODS:
        return None
    if path in PUBLIC_BODYLESS_EXACT_PATHS:
        return None
    if any(
        path == prefix.rstrip("/") or path.startswith(prefix)
        for prefix in PUBLIC_BODYLESS_PATH_PREFIXES
    ):
        return None
    if path == LOGIN_REQUEST_PATH:
        return min(default_limit, LOGIN_MAX_REQUEST_BODY_BYTES)
    return default_limit


def _mark_pre_route_rejected(scope: Scope) -> None:
    scope.setdefault("state", {})[PRE_ROUTE_REJECTED_STATE_KEY] = True


def _is_unmatched_route(scope: Scope) -> bool:
    state = scope.get("state")
    return isinstance(state, dict) and bool(state.get(UNMATCHED_ROUTE_STATE_KEY))


async def _send_too_large(send: Send) -> None:
    body = json.dumps({"detail": "Request body too large"}).encode("utf-8")
    headers = [
        (b"content-type", b"application/json"),
        (b"content-length", str(len(body)).encode("ascii")),
    ]
    await send({"type": "http.response.start", "status": 413, "headers": headers})
    await send({"type": "http.response.body", "body": body})
