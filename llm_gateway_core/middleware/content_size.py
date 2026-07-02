import json

from starlette.types import ASGIApp, Message, Receive, Scope, Send


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

        if self._content_length_exceeds_limit(scope):
            await self._send_too_large(send)
            return

        bytes_seen = 0
        response_started = False

        async def limited_receive() -> Message:
            nonlocal bytes_seen
            message = await receive()
            if message["type"] == "http.request":
                bytes_seen += len(message.get("body", b""))
                if bytes_seen > self.max_body_size:
                    raise RequestBodyTooLarge
            return message

        async def tracking_send(message: Message) -> None:
            nonlocal response_started
            if message["type"] == "http.response.start":
                response_started = True
            await send(message)

        try:
            await self.app(scope, limited_receive, tracking_send)
        except Exception as exc:
            if not contains_request_body_too_large(exc):
                raise
            if response_started:
                raise
            await self._send_too_large(send)

    def _content_length_exceeds_limit(self, scope: Scope) -> bool:
        for raw_name, raw_value in scope.get("headers") or []:
            if raw_name.lower() != b"content-length":
                continue
            try:
                return int(raw_value.decode("latin-1")) > self.max_body_size
            except (TypeError, ValueError):
                return False
        return False

    async def _send_too_large(self, send: Send) -> None:
        body = json.dumps({"detail": "Request body too large"}).encode("utf-8")
        headers = [
            (b"content-type", b"application/json"),
            (b"content-length", str(len(body)).encode("ascii")),
        ]
        await send({"type": "http.response.start", "status": 413, "headers": headers})
        await send({"type": "http.response.body", "body": body})
