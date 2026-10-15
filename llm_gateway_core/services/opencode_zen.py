"""OpenCode Zen's free streaming wire and gateway response adaptation."""

import copy
import json
import secrets
import string
import time
from collections.abc import Awaitable, Callable
from functools import lru_cache
from typing import Any
from uuid import uuid4

import httpx
from fastapi import Request

from .request_handler import replace_streaming_response_body
from .stream_observation import SSEFramer, parse_sse_json


@lru_cache(maxsize=128)
def _session_id(seed: str) -> str:
    timestamp = int(time.time() * 1000) * 4096 + 1
    prefix = ((~timestamp) & ((1 << 48) - 1)).to_bytes(6, "big").hex()
    alphabet = string.digits + string.ascii_uppercase + string.ascii_lowercase
    return "ses_" + prefix + "".join(secrets.choice(alphabet) for _ in range(14))


def _prepare_request(request: Request, headers: dict, payload: dict):
    identity_headers = {
        "http-referer", "referer", "x-opencode-request", "user-agent",
        "x-opencode-project", "x-opencode-client", "x-opencode-session",
        "x-opencode-session-id", "x-session-affinity", "x-session-id",
    }
    headers = {
        key: value for key, value in headers.items()
        if key.lower() not in identity_headers
    }
    seed = request.headers.get("x-session-id") or getattr(request.state, "zen_session_seed", None)
    if seed is None:
        seed = request.state.zen_session_seed = str(uuid4())
    session = _session_id(seed)
    headers.update({
        "User-Agent": "opencode/1.19.0",
        "x-opencode-project": "global",
        "x-opencode-client": "cli",
        "x-opencode-session": session,
        "x-opencode-session-id": session,
        "x-session-affinity": session,
        "X-Session-Id": session,
    })
    payload = copy.deepcopy(payload)
    tools = payload["tools"] = payload.get("tools") or []
    names = {tool.get("function", {}).get("name") for tool in tools}
    injected = set()
    for name, parameter in (("bash", "command"), ("read", "file_path")):
        if name in names:
            continue
        injected.add(name)
        tools.append({
            "type": "function",
            "function": {
                "name": name,
                "description": "Requirement placeholder from OpenCode Zen: this gateway cannot execute it.",
                "parameters": {
                    "type": "object",
                    "properties": {parameter: {"type": "string"}},
                    "required": [parameter],
                },
            },
        })
    payload["stream"] = True
    payload["stream_options"] = {**(payload.get("stream_options") or {}), "include_usage": True}
    return headers, payload, injected


async def _filtered_chunks(source, injected: set[str], max_event_bytes: int):
    framer = SSEFramer(max_event_bytes=max_event_bytes)
    calls = {}

    def filter_event(event):
        if event.done:
            return None
        chunk = parse_sse_json(event)
        for choice in chunk.get("choices", []):
            pending = calls.setdefault(choice.get("index", 0), {})
            delta = choice.get("delta", {})
            # Buffer tool fragments until their names are complete. This also
            # prevents an injected tool's id or arguments leaking to the client.
            for fragment in delta.pop("tool_calls", None) or []:
                index = fragment["index"]
                call = pending.setdefault(index, {"index": index, "function": {}})
                for key in ("id", "type"):
                    if fragment.get(key):
                        call[key] = fragment[key]
                for key, value in fragment.get("function", {}).items():
                    call["function"][key] = call["function"].get(key, "") + value
            if choice.get("finish_reason"):
                kept = [call for _, call in sorted(pending.items())
                        if call["function"].get("name") not in injected]
                if kept:
                    for index, call in enumerate(kept):
                        call["index"] = index
                    delta["tool_calls"] = kept
                elif pending and choice["finish_reason"] == "tool_calls":
                    choice["finish_reason"] = "stop"
                pending.clear()
        return chunk

    try:
        async for data in source:
            if isinstance(data, str):
                data = data.encode()
            for event in framer.feed(data).events:
                yield filter_event(event)
        for event in framer.finish().events:
            yield filter_event(event)
    finally:
        await source.aclose()


async def _sse_body(chunks):
    try:
        async for chunk in chunks:
            if chunk is None:
                yield b"data: [DONE]\n\n"
            else:
                yield ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()
    finally:
        await chunks.aclose()


async def _collect_completion(chunks):
    result = {"object": "chat.completion", "choices": []}
    choices = {}
    async for chunk in chunks:
        if chunk is None:
            continue
        if "error" in chunk:
            return None, json.dumps(chunk["error"])
        for key in ("id", "created", "model", "system_fingerprint", "usage"):
            if chunk.get(key) is not None:
                result[key] = chunk[key]
        for fragment in chunk.get("choices", []):
            index = fragment.get("index", 0)
            choice = choices.setdefault(index, {
                "index": index,
                "message": {"role": "assistant", "content": None},
                "finish_reason": None,
            })
            message = choice["message"]
            for key, value in fragment.get("delta", {}).items():
                if key == "tool_calls":
                    message[key] = [{k: v for k, v in call.items() if k != "index"}
                                    for call in value]
                elif value is not None:
                    message[key] = (message.get(key) or "") + value if key != "role" else value
            if fragment.get("finish_reason"):
                choice["finish_reason"] = fragment["finish_reason"]
    result["choices"] = [value for _, value in sorted(choices.items())]
    return result, None


async def make_opencode_zen_request(
    make_request: Callable[..., Awaitable[tuple[Any, Any]]], request: Request,
    client: httpx.AsyncClient, target_url: str, headers: dict,
    payload: dict, is_streaming: bool, **kwargs: Any,
) -> tuple[Any, Any]:
    headers, payload, injected = _prepare_request(request, headers, payload)
    response, error = await make_request(
        client, target_url, headers, payload, True, **kwargs,
    )
    if error is not None or response is None:
        return response, error
    chunks = _filtered_chunks(
        response.body_iterator, injected, kwargs["stream_event_max_bytes"],
    )
    if is_streaming:
        return replace_streaming_response_body(response, _sse_body(chunks)), None
    try:
        return await _collect_completion(chunks)
    finally:
        await chunks.aclose()
