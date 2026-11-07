"""Fusion keepalive: bytes leave before the ensemble finishes, then one JSON document."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI, HTTPException, Request
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.gzip import GZipMiddleware
from starlette.routing import Route
from starlette.types import Message, Scope

from llm_gateway_core.middleware.response_observation import (
    ResponseFinalizer,
    ResponseObservation,
    ResponseObservationMiddleware,
    TerminalReason,
    publish_response_observation,
)
from llm_gateway_core.services.fusion_keepalive import (
    FusionKeepaliveJsonResponse,
    fusion_response_should_keepalive,
)
from tests._async_compat import run_async
from tests.runtime_test_support import make_app_services


def _request(path: str = "/v1/chat/completions") -> Request:
    app = FastAPI()
    return Request(
        {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode("ascii"),
            "query_string": b"",
            "root_path": "",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("testserver", 80),
            "app": app,
            "state": {"llmgateway_request_id": "request-1"},
        }
    )


def _messages(sent: list[dict]) -> tuple[dict, bytes]:
    start = next(message for message in sent if message["type"] == "http.response.start")
    body = b"".join(
        message.get("body", b"")
        for message in sent
        if message["type"] == "http.response.body"
    )
    return start, body


async def _drive(response: FusionKeepaliveJsonResponse, request: Request) -> list[dict]:
    sent: list[dict] = []

    async def send(message: dict) -> None:
        sent.append(dict(message))

    async def receive() -> dict:
        return {"type": "http.request", "body": b"", "more_body": False}

    await response(request.scope, receive, send)
    return sent


def test_keepalive_is_only_for_the_outer_chat_completion_route() -> None:
    assert fusion_response_should_keepalive(_request())
    assert fusion_response_should_keepalive(_request("/v1/v1/chat/completions"))
    delegated = _request()
    delegated.state.llmgateway_delegate_dispatch = True
    assert not fusion_response_should_keepalive(delegated)
    assert not fusion_response_should_keepalive(_request("/v1/responses"))
    assert not fusion_response_should_keepalive(_request("/v1/messages"))


def test_fast_fusion_keeps_the_real_status_and_one_json_body() -> None:
    async def scenario() -> None:
        request = _request()

        async def produce() -> dict:
            return {"choices": [{"message": {"content": "ok"}}]}

        sent = await _drive(FusionKeepaliveJsonResponse(request, produce), request)
        start, body = _messages(sent)
        headers = dict(start["headers"])
        assert start["status"] == 200
        assert b"x-llmgateway-json-keepalive" not in headers
        assert body.startswith(b"{")
        assert json.loads(body)["choices"][0]["message"]["content"] == "ok"

    run_async(scenario())


def test_fast_fusion_failure_stays_a_real_502() -> None:
    async def scenario() -> None:
        request = _request()

        async def produce() -> dict:
            raise HTTPException(status_code=502, detail="Fusion main model failed.")

        sent = await _drive(FusionKeepaliveJsonResponse(request, produce), request)
        start, body = _messages(sent)
        headers = dict(start["headers"])
        assert start["status"] == 502
        assert b"x-llmgateway-json-keepalive" not in headers
        payload = json.loads(body)
        assert payload["detail"] == "Fusion main model failed."
        assert payload["error"]["message"] == "Fusion main model failed."

    run_async(scenario())


def test_slow_fusion_sends_a_newline_before_the_answer_exists() -> None:
    async def scenario() -> None:
        request = _request()
        release = asyncio.Event()
        saw_byte = asyncio.Event()

        async def produce() -> dict:
            await release.wait()
            return {"choices": [{"message": {"content": "later"}}]}

        sent: list[dict] = []

        async def send(message: dict) -> None:
            sent.append(dict(message))
            if message.get("type") == "http.response.body" and message.get("body"):
                saw_byte.set()

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        response = FusionKeepaliveJsonResponse(
            request,
            produce,
            interval_seconds=0.05,
        )
        task = asyncio.create_task(response(request.scope, receive, send))
        await asyncio.wait_for(saw_byte.wait(), timeout=1)
        assert not release.is_set()
        start, _body = _messages(sent)
        headers = dict(start["headers"])
        assert start["status"] == 200
        assert headers[b"x-llmgateway-json-keepalive"] == b"1"
        assert headers[b"content-encoding"] == b"identity"
        assert b"content-length" not in headers
        assert sent[1]["body"] == b"\n"
        release.set()
        await asyncio.wait_for(task, timeout=1)
        payload = json.loads(
            b"".join(
                message.get("body", b"")
                for message in sent
                if message.get("type") == "http.response.body"
            )
        )
        assert payload["choices"][0]["message"]["content"] == "later"

    run_async(scenario())


def test_slow_fusion_failure_returns_an_error_object_without_choices() -> None:
    async def scenario() -> None:
        request = _request()
        release = asyncio.Event()

        async def produce() -> dict:
            await release.wait()
            raise HTTPException(status_code=502, detail="Fusion main model failed.")

        sent: list[dict] = []
        saw_byte = asyncio.Event()

        async def send(message: dict) -> None:
            sent.append(dict(message))
            if message.get("body") == b"\n":
                saw_byte.set()

        async def receive() -> dict:
            return {"type": "http.request", "body": b"", "more_body": False}

        response = FusionKeepaliveJsonResponse(
            request,
            produce,
            interval_seconds=0.05,
        )
        task = asyncio.create_task(response(request.scope, receive, send))
        await asyncio.wait_for(saw_byte.wait(), timeout=1)
        release.set()
        await asyncio.wait_for(task, timeout=1)
        start, body = _messages(sent)
        assert start["status"] == 200
        payload = json.loads(body)
        assert "choices" not in payload
        assert payload["error"]["message"] == "Fusion main model failed."

    run_async(scenario())


class _PassThrough(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        return await call_next(request)


def _stack_app(release: asyncio.Event):
    services = make_app_services()

    async def endpoint(request: Request):
        async def produce() -> dict:
            await release.wait()
            return {"answer": "ok"}

        return FusionKeepaliveJsonResponse(
            request,
            produce,
            interval_seconds=0.05,
        )

    app = Starlette(routes=[Route("/fusion", endpoint, methods=["POST"])])
    app.state.services = services

    def observation(services_):
        async def finalize(_signal):
            return None

        return ResponseObservation(
            finalizer=ResponseFinalizer(services_.task_supervisor, finalize),
            classify_json=lambda _start, _body: TerminalReason.COMPLETE,
            classify_sse=lambda _start, event: (
                TerminalReason.COMPLETE if event.done else None
            ),
            classify_opaque=lambda _start: TerminalReason.COMPLETE,
        )

    async def prepare(request: Request) -> None:
        publish_response_observation(request.scope, observation(services))

    # Two HTTP middlewares sit inside observation in the real gateway.
    app.add_middleware(_PassThrough)
    app.add_middleware(_PassThrough)
    app.add_middleware(ResponseObservationMiddleware, request_preparer=prepare)
    app.add_middleware(GZipMiddleware, minimum_size=1000)
    return app


def test_keepalive_byte_passes_gzip_and_json_observation_before_fusion_finishes() -> None:
    async def scenario() -> None:
        release = asyncio.Event()
        app = _stack_app(release)
        sent: list[Message] = []
        saw_byte = asyncio.Event()

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: Message) -> None:
            sent.append(dict(message))
            if message.get("type") == "http.response.body" and message.get("body"):
                saw_byte.set()

        scope: Scope = {
            "type": "http",
            "asgi": {"version": "3.0", "spec_version": "2.4"},
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": "/fusion",
            "raw_path": b"/fusion",
            "query_string": b"",
            "headers": [(b"accept-encoding", b"gzip")],
            "client": ("127.0.0.1", 1234),
            "server": ("test", 80),
            "app": app,
            "state": {},
        }
        task = asyncio.create_task(app(scope, receive, send))
        await asyncio.wait_for(saw_byte.wait(), timeout=1)
        assert not release.is_set()
        start = next(message for message in sent if message["type"] == "http.response.start")
        headers = {name.lower(): value for name, value in start["headers"]}
        assert start["status"] == 200
        assert headers[b"content-encoding"] == b"identity"
        assert headers[b"x-llmgateway-json-keepalive"] == b"1"
        assert sent[1]["body"] == b"\n"
        release.set()
        await asyncio.wait_for(task, timeout=1)
        body = b"".join(
            message.get("body", b"")
            for message in sent
            if message.get("type") == "http.response.body"
        )
        assert json.loads(body) == {"answer": "ok"}

    run_async(scenario())
