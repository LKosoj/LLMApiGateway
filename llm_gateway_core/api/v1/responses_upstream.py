"""Translate a model's Responses upstream into the gateway's chat format."""

import copy
import json
import time
from uuid import uuid4

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from ...services.request_handler import replace_streaming_response_body
from ...services.stream_observation import SSEFramer, parse_sse_json


def chat_request_to_responses(payload: dict) -> dict:
    result = copy.deepcopy(payload)
    items = []
    for message in result.pop("messages", []):
        role = message["role"]
        content = message.get("content")
        if role == "tool":
            items.append({"type": "function_call_output", "call_id": message["tool_call_id"], "output": content or ""})
            continue
        if content is not None:
            if isinstance(content, list):
                parts = []
                for part in content:
                    if part["type"] == "text":
                        parts.append(
                            {"type": "output_text" if role == "assistant" else "input_text", "text": part["text"]}
                        )
                    elif part["type"] == "image_url":
                        image = part["image_url"]
                        image_part = {
                            "type": "input_image",
                            "image_url": image if isinstance(image, str) else image["url"],
                        }
                        if isinstance(image, dict) and "detail" in image:
                            image_part["detail"] = image["detail"]
                        parts.append(image_part)
                    elif part["type"] == "refusal":
                        parts.append(copy.deepcopy(part))
                    else:
                        raise HTTPException(400, f"Responses upstream does not support content type '{part['type']}'.")
                content = parts
            items.append({"role": role, "content": content})
        for call in message.get("tool_calls", []):
            if call.get("type", "function") != "function":
                raise HTTPException(400, "Responses upstream supports function tool calls only.")
            items.append({"type": "function_call", "call_id": call["id"], **call["function"]})
    result["input"] = items
    if "max_completion_tokens" in result:
        result["max_output_tokens"] = result.pop("max_completion_tokens")
        result.pop("max_tokens", None)
    elif "max_tokens" in result:
        result["max_output_tokens"] = result.pop("max_tokens")
    result.pop("stream_options", None)
    if "n" in result and result.pop("n") != 1:
        raise HTTPException(400, "Responses upstream supports n=1 only.")
    for field in (
        "stop",
        "logprobs",
        "top_logprobs",
        "frequency_penalty",
        "presence_penalty",
        "logit_bias",
        "functions",
        "function_call",
    ):
        if field in result:
            raise HTTPException(400, f"Responses upstream does not support '{field}'.")
    if "reasoning_effort" in result:
        result["reasoning"] = {**result.get("reasoning", {}), "effort": result.pop("reasoning_effort")}
    if "response_format" in result:
        fmt = result.pop("response_format")
        if fmt.get("type") == "json_schema":
            fmt = {"type": "json_schema", **fmt["json_schema"]} if "json_schema" in fmt else fmt
        result["text"] = {**result.get("text", {}), "format": fmt}
    if "tools" in result:
        tools = []
        for tool in result["tools"]:
            if tool.get("type") != "function":
                raise HTTPException(400, "Responses upstream supports function tools only.")
            tools.append({"type": "function", "strict": False, **tool["function"]})
        result["tools"] = tools
    choice = result.get("tool_choice")
    if isinstance(choice, dict) and choice.get("type") == "function":
        if "function" in choice:
            result["tool_choice"] = {"type": "function", "name": choice["function"]["name"]}
    return result


def responses_usage_to_chat(usage: dict) -> dict:
    result = {
        key: value
        for key, value in usage.items()
        if key
        not in {
            "input_tokens",
            "output_tokens",
            "input_tokens_details",
            "output_tokens_details",
        }
    }
    result["prompt_tokens"] = usage.get("input_tokens", 0)
    result["completion_tokens"] = usage.get("output_tokens", 0)
    result["total_tokens"] = usage.get("total_tokens", result["prompt_tokens"] + result["completion_tokens"])
    if isinstance(usage.get("input_tokens_details"), dict):
        result["prompt_tokens_details"] = copy.deepcopy(usage["input_tokens_details"])
    if isinstance(usage.get("output_tokens_details"), dict):
        result["completion_tokens_details"] = copy.deepcopy(usage["output_tokens_details"])
    return result


def responses_response_to_chat(payload: dict) -> dict:
    if payload.get("error"):
        return {"error": payload["error"]}
    if payload.get("status") not in {"completed", "incomplete"}:
        return {"error": {"message": "Responses upstream did not complete the response."}}
    message = {"role": "assistant", "content": ""}
    tools = []
    for item in payload.get("output", []):
        if item.get("type") == "message":
            for part in item.get("content", []):
                if part.get("type") == "output_text":
                    message["content"] += part["text"]
                elif part.get("type") == "refusal":
                    message["refusal"] = message.get("refusal", "") + part["refusal"]
        elif item.get("type") == "function_call":
            tools.append(
                {
                    "id": item["call_id"],
                    "type": "function",
                    "function": {"name": item["name"], "arguments": item["arguments"]},
                }
            )
        elif item.get("type") == "reasoning":
            summary = "".join(part.get("text", "") for part in item.get("summary", []))
            if summary:
                message["reasoning_content"] = message.get("reasoning_content", "") + summary
    if tools:
        message["tool_calls"] = tools
        if not message["content"]:
            message["content"] = None
    reason = "tool_calls" if tools else "stop"
    if payload.get("status") == "incomplete":
        detail = (payload.get("incomplete_details") or {}).get("reason")
        reason = "length" if detail == "max_output_tokens" else "content_filter"
    result = {
        "id": payload.get("id", ""),
        "object": "chat.completion",
        "created": payload.get("created_at", int(time.time())),
        "model": payload.get("model", ""),
        "choices": [{"index": 0, "message": message, "finish_reason": reason}],
    }
    if isinstance(payload.get("usage"), dict):
        result["usage"] = responses_usage_to_chat(payload["usage"])
    return result


def responses_stream_to_chat(response: StreamingResponse, model: str) -> StreamingResponse:
    source = response.body_iterator

    async def generate():
        framer = SSEFramer()
        metadata = {
            "id": f"chatcmpl-{uuid4().hex}",
            "object": "chat.completion.chunk",
            "created": int(time.time()),
            "model": model,
        }
        tools = {}
        completed = False

        def wrap(delta: dict, *, finish_reason=None, usage=None) -> bytes:
            chunk = {**metadata, "choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]}
            if usage is not None:
                chunk["usage"] = usage
            return ("data: " + json.dumps(chunk, ensure_ascii=False) + "\n\n").encode()

        def convert(event):
            nonlocal completed
            if completed or event.done:
                return []
            data = parse_sse_json(event)
            kind = data.get("type", event.event)
            if kind == "response.created":
                info = data["response"]
                metadata.update(
                    id=info.get("id") or metadata["id"],
                    created=info.get("created_at", metadata["created"]),
                    model=info.get("model", model),
                )
                return [wrap({"role": "assistant", "content": ""})]
            if kind in {
                "response.output_text.delta",
                "response.refusal.delta",
                "response.reasoning_summary_text.delta",
            }:
                key = {
                    "response.output_text.delta": "content",
                    "response.refusal.delta": "refusal",
                    "response.reasoning_summary_text.delta": "reasoning_content",
                }[kind]
                return [wrap({key: data["delta"]})]
            if kind == "response.output_item.added" and data["item"].get("type") == "function_call":
                item = data["item"]
                index = len(tools)
                tools[data["output_index"]] = index
                return [
                    wrap(
                        {
                            "tool_calls": [
                                {
                                    "index": index,
                                    "id": item["call_id"],
                                    "type": "function",
                                    "function": {"name": item["name"], "arguments": item.get("arguments", "")},
                                }
                            ]
                        }
                    )
                ]
            if kind == "response.function_call_arguments.delta":
                return [
                    wrap(
                        {
                            "tool_calls": [
                                {"index": tools[data["output_index"]], "function": {"arguments": data["delta"]}}
                            ]
                        }
                    )
                ]
            if kind in {"response.completed", "response.incomplete"}:
                result = responses_response_to_chat(data["response"])
                if "error" in result:
                    completed = True
                    return [("data: " + json.dumps(result) + "\n\n").encode()]
                completed = True
                return [
                    wrap({}, finish_reason=result["choices"][0]["finish_reason"], usage=result.get("usage")),
                    b"data: [DONE]\n\n",
                ]
            if kind in {"error", "response.failed"}:
                completed = True
                error = (
                    data.get("error")
                    or data.get("response", {}).get("error")
                    or {"message": data.get("message", "Responses upstream failed.")}
                )
                return [("data: " + json.dumps({"error": error}) + "\n\n").encode()]
            return []

        try:
            async for chunk in source:
                raw = chunk.encode() if isinstance(chunk, str) else bytes(chunk)
                for event in framer.feed(raw).events:
                    for converted in convert(event):
                        yield converted
                if completed:
                    break
            if not completed:
                for event in framer.finish().events:
                    for converted in convert(event):
                        yield converted
            if not completed:
                yield b'data: {"error":{"message":"Responses upstream stream ended before completion."}}\n\n'
        finally:
            framer.abort()
            close = getattr(source, "aclose", None)
            if close is not None:
                await close()

    return replace_streaming_response_body(response, generate())
