import copy
import functools
import json
import logging
import re
import time
from uuid import uuid4

import tiktoken
from fastapi import HTTPException

from ...config.loader import ConfigLoader
from ...services.model_policy import resolve_model_name
from ...utils.text_sanitize import sanitize_payload

ANTHROPIC_DEFAULT_MAX_TOKENS = 32768
TOKEN_COUNT_ENCODING_FALLBACKS = ("o200k_base", "cl100k_base")
ANTHROPIC_TOOL_NAME_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_-]*$")
_ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
}


def _extract_anthropic_text(content: object, detail_prefix: str = "Anthropic content") -> str:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        # If it's not a list or string, try to stringify it as a last resort
        return str(content) if content is not None else ""

    text_parts: list[str] = []
    for block in content:
        if not isinstance(block, dict):
            continue

        block_type = block.get("type")

        # Extract text from various known block types
        if block_type == "text":
            text_parts.append(block.get("text", ""))
        elif block_type == "thinking":
            text_parts.append(block.get("thinking", ""))
        elif block_type == "redacted_thinking":
            text_parts.append(block.get("data", ""))
        elif block_type == "tool_result":
            # Nested content in tool results
            inner_content = block.get("content")
            if inner_content:
                text_parts.append(_extract_anthropic_text(inner_content, "Nested tool_result"))

        # Silently skip image, document, and other non-textual blocks
        # to maintain maximum compatibility with complex payloads.

    return "".join(text_parts)


def _anthropic_image_source_to_openai_part(source: object) -> dict:
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="Anthropic image source must be an object")

    source_type = source.get("type")
    if source_type == "url":
        url = source.get("url")
        if not isinstance(url, str) or not url:
            raise HTTPException(status_code=400, detail="Anthropic image URL source requires non-empty 'url'")
        return {"type": "image_url", "image_url": {"url": url}}

    if source_type == "base64":
        data = source.get("data")
        media_type = source.get("media_type")
        if not isinstance(data, str) or not data:
            raise HTTPException(status_code=400, detail="Anthropic base64 image source requires non-empty 'data'")
        if not isinstance(media_type, str) or not media_type:
            raise HTTPException(status_code=400, detail="Anthropic base64 image source requires non-empty 'media_type'")
        return {"type": "image_url", "image_url": {"url": f"data:{media_type};base64,{data}"}}

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic image source type: {source_type}")


def _anthropic_document_source_to_openai_parts(source: object, block: dict) -> list[dict]:
    if not isinstance(source, dict):
        raise HTTPException(status_code=400, detail="Anthropic document source must be an object")

    source_type = source.get("type")
    translated_parts: list[dict] = []

    title = block.get("title")
    context = block.get("context")
    prefix_parts: list[str] = []
    if isinstance(title, str) and title:
        prefix_parts.append(f"Document title: {title}")
    if isinstance(context, str) and context:
        prefix_parts.append(f"Document context: {context}")
    if prefix_parts:
        translated_parts.append({"type": "text", "text": "\n".join(prefix_parts) + "\n"})

    if source_type == "text":
        data = source.get("data")
        if not isinstance(data, str):
            raise HTTPException(status_code=400, detail="Anthropic plain text document source requires string 'data'")
        translated_parts.append({"type": "text", "text": data})
        return translated_parts

    if source_type == "content":
        nested_content = source.get("content")
        if not isinstance(nested_content, list):
            raise HTTPException(status_code=400, detail="Anthropic content document source requires list 'content'")
        translated_parts.extend(_anthropic_content_blocks_to_openai_parts(nested_content))
        return translated_parts

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic document source type: {source_type}")


def _anthropic_content_blocks_to_openai_parts(blocks: object) -> list[dict]:
    if not isinstance(blocks, list):
        raise HTTPException(status_code=400, detail="Anthropic content must be a list of content blocks")

    translated_parts: list[dict] = []
    for block in blocks:
        if not isinstance(block, dict):
            raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")

        block_type = block.get("type")
        if block_type == "text":
            translated_parts.append({"type": "text", "text": block.get("text", "")})
            continue

        if block_type == "image":
            translated_parts.append(_anthropic_image_source_to_openai_part(block.get("source")))
            continue

        if block_type == "document":
            translated_parts.extend(_anthropic_document_source_to_openai_parts(block.get("source"), block))
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported Anthropic user content block type: {block_type}")

    return translated_parts


def _collapse_openai_parts_to_content(parts: list[dict]) -> object:
    if not parts:
        return ""

    if all(part.get("type") == "text" for part in parts):
        return "".join(part.get("text", "") for part in parts)

    return parts


def _anthropic_tool_result_to_openai_content(content: object) -> object:
    if isinstance(content, str):
        return content

    translated_parts = _anthropic_content_blocks_to_openai_parts(content)
    return _collapse_openai_parts_to_content(translated_parts)


def _anthropic_tools_to_openai(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="Anthropic tools must be a list")

    translated_tools = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Anthropic tools must be objects")
        name = tool.get("name")
        input_schema = tool.get("input_schema")
        if not name or not isinstance(input_schema, dict):
            raise HTTPException(status_code=400, detail="Anthropic tools require 'name' and object 'input_schema'")

        translated_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description"),
                    "parameters": input_schema,
                },
            }
        )

    return translated_tools


def _anthropic_tool_choice_to_openai(tool_choice: object) -> object:
    if isinstance(tool_choice, str):
        tool_choice = {"type": tool_choice}

    if not isinstance(tool_choice, dict):
        raise HTTPException(status_code=400, detail="Anthropic tool_choice must be an object or string")

    choice_type = tool_choice.get("type")
    if choice_type == "auto":
        return "auto"
    if choice_type == "any":
        return "required"
    if choice_type == "none":
        return "none"
    if choice_type == "tool":
        tool_name = tool_choice.get("name")
        if not tool_name:
            raise HTTPException(status_code=400, detail="Anthropic tool_choice type 'tool' requires 'name'")
        return {"type": "function", "function": {"name": tool_name}}

    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic tool_choice type: {choice_type}")


def _anthropic_message_to_openai_messages(message: dict) -> list[dict]:
    role = message.get("role")
    content = message.get("content", "")

    if role == "assistant":
        assistant_message: dict[str, object] = {"role": "assistant"}
        text_parts: list[str] = []
        tool_calls: list[dict] = []

        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if not isinstance(block, dict):
                    raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")
                block_type = block.get("type")
                if block_type == "text":
                    text_parts.append(block.get("text", ""))
                elif block_type == "thinking":
                    text_parts.append(block.get("thinking", ""))
                elif block_type == "redacted_thinking":
                    text_parts.append(block.get("data", ""))
                elif block_type == "tool_use":
                    tool_calls.append(
                        {
                            "id": block.get("id"),
                            "type": "function",
                            "function": {
                                "name": block.get("name"),
                                "arguments": json.dumps(block.get("input", {}), ensure_ascii=False, separators=(",", ":")),
                            },
                        }
                    )
                else:
                    raise HTTPException(status_code=400, detail=f"Unsupported Anthropic assistant content block type: {block_type}")
        else:
            raise HTTPException(status_code=400, detail="Anthropic assistant content must be a string or a list of content blocks")

        assistant_message["content"] = "".join(text_parts) if text_parts else None
        if tool_calls:
            assistant_message["tool_calls"] = tool_calls

        return [assistant_message]

    if role != "user":
        raise HTTPException(status_code=400, detail=f"Unsupported Anthropic role: {role}")

    if isinstance(content, str):
        return [{"role": "user", "content": content}]

    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="Anthropic user content must be a string or a list of content blocks")

    translated_messages: list[dict] = []
    user_content_parts: list[dict] = []
    for block in content:
        if not isinstance(block, dict):
            raise HTTPException(status_code=400, detail="Anthropic content blocks must be objects")
        block_type = block.get("type")
        if block_type == "tool_result":
            if user_content_parts:
                translated_messages.append({"role": "user", "content": _collapse_openai_parts_to_content(user_content_parts)})
                user_content_parts = []
            translated_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": block.get("tool_use_id"),
                    "content": _anthropic_tool_result_to_openai_content(block.get("content", "")),
                }
            )
        else:
            user_content_parts.extend(_anthropic_content_blocks_to_openai_parts([block]))

    if user_content_parts:
        translated_messages.append({"role": "user", "content": _collapse_openai_parts_to_content(user_content_parts)})

    if not translated_messages:
        translated_messages.append({"role": "user", "content": ""})

    return translated_messages


def _anthropic_request_to_openai_payload(anthropic_payload: dict) -> tuple[dict, str]:
    requested_model = anthropic_payload.get("model")
    if not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    anthropic_messages = anthropic_payload.get("messages")
    if not isinstance(anthropic_messages, list) or not anthropic_messages:
        raise HTTPException(status_code=400, detail="Anthropic request must contain a non-empty 'messages' list")

    openai_messages: list[dict] = []

    system_content = anthropic_payload.get("system")
    if system_content is not None:
        openai_messages.append(
            {
                "role": "system",
                "content": _extract_anthropic_text(system_content),
            }
        )

    for message in anthropic_messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Anthropic messages must be objects")
        openai_messages.extend(_anthropic_message_to_openai_messages(message))

    openai_payload = {
        "model": requested_model,
        "messages": openai_messages,
        "stream": anthropic_payload.get("stream", False),
    }

    if openai_payload["stream"]:
        openai_payload["stream_options"] = {"include_usage": True}

    # Passthrough only safe, standard OpenAI fields
    openai_safe_fields = {
        "max_tokens",
        "temperature",
        "top_p",
        "presence_penalty",
        "frequency_penalty",
        "logit_bias",
        "service_tier",
        "user",
    }
    for field in openai_safe_fields:
        if field in anthropic_payload:
            openai_payload[field] = copy.deepcopy(anthropic_payload[field])

    if "stop_sequences" in anthropic_payload:
        openai_payload["stop"] = anthropic_payload["stop_sequences"]

    if "tools" in anthropic_payload:
        openai_payload["tools"] = _anthropic_tools_to_openai(anthropic_payload["tools"])

    if "tool_choice" in anthropic_payload:
        openai_payload["tool_choice"] = _anthropic_tool_choice_to_openai(anthropic_payload["tool_choice"])

    output_config = anthropic_payload.get("output_config")
    if isinstance(output_config, dict):
        response_format = output_config.get("format")
        if isinstance(response_format, dict):
            openai_payload["response_format"] = copy.deepcopy(response_format)

    return openai_payload, requested_model


def _openai_content_to_plain_text(content: object) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                text = block.get("text")
                if isinstance(text, str):
                    parts.append(text)
        return "".join(parts)
    return ""


def _openai_user_content_to_anthropic_blocks(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if content is None:
        return [{"type": "text", "text": ""}]
    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="OpenAI user content must be string or a list of content blocks")

    blocks: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(status_code=400, detail="OpenAI content blocks must be objects")
        part_type = part.get("type")
        if part_type == "text":
            blocks.append({"type": "text", "text": part.get("text", "")})
        elif part_type == "image_url":
            image_url_field = part.get("image_url")
            url = image_url_field.get("url") if isinstance(image_url_field, dict) else image_url_field
            if not isinstance(url, str) or not url:
                raise HTTPException(status_code=400, detail="OpenAI image_url block requires a 'url' string")
            if url.startswith("data:"):
                try:
                    header, b64data = url.split(",", 1)
                except ValueError:
                    raise HTTPException(status_code=400, detail="Malformed data URL for image")
                media_type = header[len("data:"):].split(";", 1)[0] or "image/png"
                blocks.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64data},
                })
            else:
                blocks.append({
                    "type": "image",
                    "source": {"type": "url", "url": url},
                })
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported OpenAI user content block type: {part_type}")

    if not blocks:
        blocks.append({"type": "text", "text": ""})
    return blocks


def _openai_tool_content_to_anthropic(content: object) -> object:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        normalized: list[dict] = []
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                normalized.append({"type": "text", "text": block.get("text", "")})
            else:
                normalized.append({"type": "text", "text": json.dumps(block, ensure_ascii=False)})
        return normalized
    return json.dumps(content, ensure_ascii=False)


def _openai_tool_name_to_anthropic(name: str, tool_name_map: dict[str, str]) -> str:
    mapped_name = tool_name_map.get(name)
    if mapped_name is not None:
        return mapped_name

    used_names = set(tool_name_map.values())
    candidate = name
    if not ANTHROPIC_TOOL_NAME_RE.fullmatch(candidate):
        candidate = re.sub(r"[^A-Za-z0-9_-]", "_", candidate)
        if not candidate or not candidate[0].isalpha():
            candidate = f"tool_{candidate}"

    base_candidate = candidate or "tool"
    suffix = 2
    while candidate in used_names or not ANTHROPIC_TOOL_NAME_RE.fullmatch(candidate):
        candidate = f"{base_candidate}_{suffix}"
        suffix += 1

    tool_name_map[name] = candidate
    return candidate


def _anthropic_tool_name_to_openai(name: object, tool_name_reverse_map: dict[str, str]) -> str:
    if not isinstance(name, str):
        return ""
    return tool_name_reverse_map.get(name, name)


def _openai_tools_to_anthropic(tools: object, tool_name_map: dict[str, str] | None = None) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="'tools' must be a list")
    if tool_name_map is None:
        tool_name_map = {}
    translated: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Tool entries must be objects")
        function_payload = tool.get("function") if tool.get("type") == "function" else tool
        if not isinstance(function_payload, dict):
            raise HTTPException(status_code=400, detail="Tool entry must contain a 'function' object")
        name = function_payload.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=400, detail="Each tool requires a 'name'")
        entry: dict = {
            "name": _openai_tool_name_to_anthropic(name, tool_name_map),
            "input_schema": function_payload.get("parameters") or {"type": "object", "properties": {}},
        }
        description = function_payload.get("description")
        if isinstance(description, str) and description:
            entry["description"] = description
        translated.append(entry)
    return translated


def _openai_tool_choice_to_anthropic(
    choice: object,
    tool_name_map: dict[str, str] | None = None,
) -> object | None:
    if tool_name_map is None:
        tool_name_map = {}
    if isinstance(choice, str):
        if choice == "auto":
            return {"type": "auto"}
        if choice == "required":
            return {"type": "any"}
        if choice == "none":
            return {"type": "none"}
        return None
    if isinstance(choice, dict):
        if choice.get("type") == "function":
            fn = choice.get("function") or {}
            tool_name = fn.get("name") if isinstance(fn, dict) else None
            if isinstance(tool_name, str) and tool_name:
                return {"type": "tool", "name": _openai_tool_name_to_anthropic(tool_name, tool_name_map)}
        return None
    return None


def _openai_request_to_anthropic_payload(
    openai_payload: dict,
    tool_name_map: dict[str, str] | None = None,
) -> dict:
    """Convert an OpenAI chat-completions payload into an Anthropic messages payload.

    This is the symmetric counterpart of ``_anthropic_request_to_openai_payload``
    and is used when the selected provider speaks the native Anthropic dialect.
    """
    messages = openai_payload.get("messages")
    if not isinstance(messages, list):
        raise HTTPException(status_code=400, detail="'messages' must be a list")
    if tool_name_map is None:
        tool_name_map = {}

    system_texts: list[str] = []
    anthropic_messages: list[dict] = []
    pending_user_blocks: list[dict] = []

    def _flush_pending_user() -> None:
        if pending_user_blocks:
            anthropic_messages.append({"role": "user", "content": list(pending_user_blocks)})
            pending_user_blocks.clear()

    for message in messages:
        if not isinstance(message, dict):
            raise HTTPException(status_code=400, detail="Each message must be an object")
        role = message.get("role")
        content = message.get("content")

        if role == "system":
            text = _openai_content_to_plain_text(content)
            if text:
                system_texts.append(text)
            continue

        if role == "user":
            _flush_pending_user()
            anthropic_messages.append({
                "role": "user",
                "content": _openai_user_content_to_anthropic_blocks(content),
            })
            continue

        if role == "assistant":
            _flush_pending_user()
            blocks: list[dict] = []
            if isinstance(content, str) and content:
                blocks.append({"type": "text", "text": content})
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        text = part.get("text")
                        if isinstance(text, str) and text:
                            blocks.append({"type": "text", "text": text})
            tool_calls = message.get("tool_calls") or []
            if isinstance(tool_calls, list):
                for tool_call in tool_calls:
                    if not isinstance(tool_call, dict):
                        continue
                    fn = tool_call.get("function") or {}
                    raw_args = fn.get("arguments", "{}") if isinstance(fn, dict) else "{}"
                    try:
                        parsed = json.loads(raw_args) if raw_args else {}
                    except Exception:
                        parsed = {"raw_arguments": raw_args}
                    if not isinstance(parsed, dict):
                        parsed = {"value": parsed}
                    tool_name = fn.get("name") if isinstance(fn, dict) else None
                    if isinstance(tool_name, str) and tool_name:
                        tool_name = _openai_tool_name_to_anthropic(tool_name, tool_name_map)
                    blocks.append({
                        "type": "tool_use",
                        "id": tool_call.get("id"),
                        "name": tool_name,
                        "input": parsed,
                    })
            if not blocks:
                blocks.append({"type": "text", "text": ""})
            anthropic_messages.append({"role": "assistant", "content": blocks})
            continue

        if role == "tool":
            pending_user_blocks.append({
                "type": "tool_result",
                "tool_use_id": message.get("tool_call_id"),
                "content": _openai_tool_content_to_anthropic(content),
            })
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported OpenAI message role: {role}")

    _flush_pending_user()

    anthropic_payload: dict = {
        "model": openai_payload.get("model"),
        "messages": anthropic_messages,
    }
    if system_texts:
        anthropic_payload["system"] = "\n\n".join(system_texts)

    max_tokens_value = openai_payload.get("max_tokens")
    if isinstance(max_tokens_value, bool) or not isinstance(max_tokens_value, (int, float)) or int(max_tokens_value) <= 0:
        anthropic_payload["max_tokens"] = ANTHROPIC_DEFAULT_MAX_TOKENS
    else:
        anthropic_payload["max_tokens"] = int(max_tokens_value)

    if openai_payload.get("stream"):
        anthropic_payload["stream"] = True

    for field_name in ("temperature", "top_p"):
        if field_name in openai_payload:
            anthropic_payload[field_name] = openai_payload[field_name]

    if "stop" in openai_payload:
        stop_value = openai_payload["stop"]
        if isinstance(stop_value, str):
            anthropic_payload["stop_sequences"] = [stop_value]
        elif isinstance(stop_value, list):
            anthropic_payload["stop_sequences"] = [s for s in stop_value if isinstance(s, str)]

    if "tools" in openai_payload:
        anthropic_payload["tools"] = _openai_tools_to_anthropic(
            openai_payload["tools"],
            tool_name_map,
        )

    if "tool_choice" in openai_payload:
        translated_choice = _openai_tool_choice_to_anthropic(
            openai_payload["tool_choice"],
            tool_name_map,
        )
        if translated_choice is not None:
            anthropic_payload["tool_choice"] = translated_choice

    user_identifier = openai_payload.get("user")
    if isinstance(user_identifier, str) and user_identifier:
        anthropic_payload["metadata"] = {"user_id": user_identifier}

    return anthropic_payload


def _coerce_anthropic_usage_token_count(value: object) -> int:
    try:
        return max(int(value or 0), 0)
    except (TypeError, ValueError):
        return 0


def _anthropic_usage_to_openai_usage(usage_src: object) -> dict:
    if not isinstance(usage_src, dict):
        usage_src = {}

    input_tokens = _coerce_anthropic_usage_token_count(usage_src.get("input_tokens"))
    cache_creation_input_tokens = _coerce_anthropic_usage_token_count(
        usage_src.get("cache_creation_input_tokens")
    )
    cache_read_input_tokens = _coerce_anthropic_usage_token_count(
        usage_src.get("cache_read_input_tokens")
    )
    output_tokens = _coerce_anthropic_usage_token_count(usage_src.get("output_tokens"))

    prompt_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens
    usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": output_tokens,
        "total_tokens": prompt_tokens + output_tokens,
    }
    if cache_creation_input_tokens or cache_read_input_tokens:
        usage["prompt_tokens_details"] = {"cached_tokens": cache_read_input_tokens}

    return usage


def _anthropic_response_to_openai(
    response_data: dict,
    requested_model: str,
    tool_name_reverse_map: dict[str, str] | None = None,
) -> dict:
    """Convert a non-streaming Anthropic /v1/messages response to OpenAI chat.completion shape."""
    if not isinstance(response_data, dict):
        raise HTTPException(status_code=502, detail="Anthropic provider returned a non-object response")
    if tool_name_reverse_map is None:
        tool_name_reverse_map = {}

    content_blocks = response_data.get("content") or []
    text_parts: list[str] = []
    tool_calls: list[dict] = []
    for block in content_blocks:
        if not isinstance(block, dict):
            continue
        block_type = block.get("type")
        if block_type == "text":
            text = block.get("text")
            if isinstance(text, str) and text:
                text_parts.append(text)
        elif block_type == "tool_use":
            try:
                arguments_json = json.dumps(
                    block.get("input") or {},
                    ensure_ascii=False,
                    separators=(",", ":"),
                )
            except Exception:
                arguments_json = "{}"
            tool_calls.append({
                "id": block.get("id") or f"call_{uuid4().hex[:16]}",
                "type": "function",
                "function": {
                    "name": _anthropic_tool_name_to_openai(block.get("name"), tool_name_reverse_map),
                    "arguments": arguments_json,
                },
            })

    message: dict = {"role": "assistant"}
    message["content"] = "".join(text_parts) if text_parts else None
    if tool_calls:
        message["tool_calls"] = tool_calls

    finish_reason = _ANTHROPIC_STOP_REASON_TO_OPENAI_FINISH.get(
        response_data.get("stop_reason"),
        "stop",
    )

    return {
        "id": response_data.get("id") or f"chatcmpl-{uuid4().hex}",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": response_data.get("model") or requested_model,
        "choices": [
            {"index": 0, "message": message, "finish_reason": finish_reason},
        ],
        "usage": _anthropic_usage_to_openai_usage(response_data.get("usage")),
    }


def _resolve_model_name_for_token_count(config_loader_instance: ConfigLoader, requested_model: str) -> str:
    try:
        model_resolution = resolve_model_name(
            requested_model,
            getattr(config_loader_instance, "model_rules", None),
        )
    except ValueError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc

    model_config = config_loader_instance.fallback_rules.get(model_resolution.effective_model)
    if not model_config:
        if model_resolution.changed:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"Model policy resolved '{requested_model}' to "
                    f"'{model_resolution.effective_model}', but no chat route is configured for it."
                ),
            )
        return requested_model

    fallback_models = model_config.get("fallback_models") or []
    if not fallback_models:
        return requested_model

    first_rule = fallback_models[0]
    if not isinstance(first_rule, dict):
        return requested_model

    provider_model = first_rule.get("model")
    if isinstance(provider_model, str) and provider_model:
        return provider_model

    return requested_model


@functools.lru_cache(maxsize=64)
def _resolve_tiktoken_encoding(model_name: str):
    try:
        return tiktoken.encoding_for_model(model_name)
    except KeyError:
        for encoding_name in TOKEN_COUNT_ENCODING_FALLBACKS:
            try:
                return tiktoken.get_encoding(encoding_name)
            except ValueError:
                continue

    raise HTTPException(status_code=500, detail="Token counting is not configured correctly")


def _build_token_count_payload(openai_payload: dict) -> dict:
    token_count_payload = {"messages": openai_payload.get("messages", [])}

    for field in ("cache_control", "output_config", "thinking", "tool_choice", "tools"):
        if field in openai_payload:
            token_count_payload[field] = openai_payload[field]

    return token_count_payload


def _estimate_input_tokens(openai_payload: dict, model_name: str) -> int:
    encoding = _resolve_tiktoken_encoding(model_name)
    token_count_payload = _build_token_count_payload(openai_payload)
    serialized_payload = json.dumps(
        token_count_payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return len(encoding.encode(serialized_payload))


def _map_finish_reason_to_anthropic(finish_reason: str | None) -> str | None:
    mapping = {
        "stop": "end_turn",
        "length": "max_tokens",
        "tool_calls": "tool_use",
    }
    return mapping.get(finish_reason, finish_reason)


def _openai_tool_calls_to_anthropic_blocks(tool_calls: object) -> list[dict]:
    if not isinstance(tool_calls, list):
        return []

    anthropic_blocks = []
    for tool_call in tool_calls:
        if not isinstance(tool_call, dict):
            continue
        function = tool_call.get("function", {})
        raw_arguments = function.get("arguments", "{}")
        try:
            parsed_arguments = (
                sanitize_payload(json.loads(raw_arguments))
                if raw_arguments
                else {}
            )
        except Exception:
            parsed_arguments = {"raw_arguments": raw_arguments}

        if not isinstance(parsed_arguments, dict):
            parsed_arguments = {"value": parsed_arguments}

        anthropic_blocks.append(
            {
                "type": "tool_use",
                "id": tool_call.get("id"),
                "name": function.get("name"),
                "input": parsed_arguments,
            }
        )

    return anthropic_blocks


def _openai_response_to_anthropic(response_data: dict, requested_model: str) -> dict:
    choices = response_data.get("choices", [])
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {})
    usage = response_data.get("usage", {})

    content_blocks = []

    # Anthropic thinking blocks require provider signatures that the gateway
    # cannot synthesize from generic OpenAI-compatible reasoning fields.
    # Handle main text content
    text_content = message.get("content")
    if isinstance(text_content, str) and text_content:
        content_blocks.append({"type": "text", "text": text_content})
    elif isinstance(text_content, list):
        # Flatten OpenAI list content if present
        parts = []
        for block in text_content:
            if isinstance(block, dict) and block.get("type") == "text":
                parts.append(block.get("text", ""))
        if parts:
            content_blocks.append({"type": "text", "text": "".join(parts)})

    # Handle tools
    tool_use_blocks = _openai_tool_calls_to_anthropic_blocks(message.get("tool_calls"))
    content_blocks.extend(tool_use_blocks)

    if not content_blocks:
        content_blocks.append({"type": "text", "text": ""})

    stop_reason = _map_finish_reason_to_anthropic(first_choice.get("finish_reason"))
    if tool_use_blocks and stop_reason is None:
        stop_reason = "tool_use"

    return {
        "id": response_data.get("id", "msg_llmgateway"),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content_blocks,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0),
            "output_tokens": usage.get("completion_tokens", 0),
        },
    }


def _coerce_responses_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _responses_content_to_openai_content(content: object) -> object:
    if isinstance(content, str):
        return content

    if not isinstance(content, list):
        raise HTTPException(status_code=400, detail="Responses message content must be a string or a list")

    openai_parts: list[dict] = []
    for part in content:
        if not isinstance(part, dict):
            raise HTTPException(status_code=400, detail="Responses content parts must be objects")

        part_type = part.get("type")
        if part_type in {"input_text", "output_text", "text"}:
            openai_parts.append({"type": "text", "text": _coerce_responses_text(part.get("text", ""))})
            continue

        if part_type == "input_image":
            image_url = part.get("image_url")
            if isinstance(image_url, str) and image_url:
                openai_parts.append({"type": "image_url", "image_url": {"url": image_url}})
                continue
            if isinstance(image_url, dict):
                url = image_url.get("url")
                if isinstance(url, str) and url:
                    openai_parts.append({"type": "image_url", "image_url": {"url": url}})
                    continue
            raise HTTPException(status_code=400, detail="Responses input_image requires non-empty 'image_url'")

        raise HTTPException(status_code=400, detail=f"Unsupported Responses content part type: {part_type}")

    return _collapse_openai_parts_to_content(openai_parts)


def _responses_input_to_openai_messages(input_items: object) -> list[dict]:
    if isinstance(input_items, str):
        return [{"role": "user", "content": input_items}]

    if not isinstance(input_items, list):
        raise HTTPException(status_code=400, detail="Responses 'input' must be a string or a list")

    openai_messages: list[dict] = []
    for item in input_items:
        if not isinstance(item, dict):
            raise HTTPException(status_code=400, detail="Responses input items must be objects")

        item_type = item.get("type") or ("message" if item.get("role") else None)
        if item_type == "message":
            role = item.get("role")
            if role not in {"system", "developer", "user", "assistant"}:
                raise HTTPException(status_code=400, detail=f"Unsupported Responses message role: {role}")
            openai_messages.append(
                {
                    "role": role,
                    "content": _responses_content_to_openai_content(item.get("content", "")),
                }
            )
            continue

        if item_type == "function_call":
            tool_name = item.get("name")
            if not isinstance(tool_name, str) or not tool_name:
                raise HTTPException(status_code=400, detail="Responses function_call requires non-empty 'name'")
            raw_arguments = item.get("arguments", "")
            if not isinstance(raw_arguments, str):
                raw_arguments = json.dumps(raw_arguments, ensure_ascii=False, separators=(",", ":"))
            tool_call_id = item.get("call_id") or item.get("id") or f"call_{uuid4().hex}"
            openai_messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": tool_call_id,
                            "type": "function",
                            "function": {
                                "name": tool_name,
                                "arguments": raw_arguments,
                            },
                        }
                    ],
                }
            )
            continue

        if item_type == "function_call_output":
            tool_call_id = item.get("call_id") or item.get("tool_call_id")
            if not isinstance(tool_call_id, str) or not tool_call_id:
                raise HTTPException(status_code=400, detail="Responses function_call_output requires non-empty 'call_id'")
            output = item.get("output", "")
            if not isinstance(output, str):
                output = json.dumps(output, ensure_ascii=False, separators=(",", ":"))
            openai_messages.append(
                {
                    "role": "tool",
                    "tool_call_id": tool_call_id,
                    "content": output,
                }
            )
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported Responses input item type: {item_type}")

    return openai_messages


def _responses_tools_to_openai(tools: object) -> list[dict]:
    if not isinstance(tools, list):
        raise HTTPException(status_code=400, detail="Responses tools must be a list")

    translated_tools: list[dict] = []
    for tool in tools:
        if not isinstance(tool, dict):
            raise HTTPException(status_code=400, detail="Responses tools must be objects")

        tool_type = tool.get("type")
        if tool_type not in (None, "function"):
            raise HTTPException(status_code=400, detail=f"Unsupported Responses tool type: {tool_type}")

        function_payload = tool.get("function", {}) if isinstance(tool.get("function"), dict) else {}
        name = tool.get("name") or function_payload.get("name")
        if not isinstance(name, str) or not name:
            raise HTTPException(status_code=400, detail="Responses tools require a non-empty function name")

        parameters = tool.get("parameters")
        if parameters is None:
            parameters = function_payload.get("parameters") or tool.get("parametersJsonSchema")
        if not isinstance(parameters, dict):
            parameters = {"type": "object", "properties": {}}

        translated_tools.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description") or function_payload.get("description"),
                    "parameters": parameters,
                },
            }
        )

    return translated_tools


def _responses_request_to_openai_payload(responses_payload: dict) -> tuple[dict, str]:
    requested_model = responses_payload.get("model")
    if not isinstance(requested_model, str) or not requested_model:
        raise HTTPException(status_code=400, detail="Missing 'model' in request body")

    openai_messages: list[dict] = []

    instructions = responses_payload.get("instructions")
    instructions_text = _coerce_responses_text(instructions).strip()
    if instructions_text:
        openai_messages.append({"role": "system", "content": instructions_text})

    if "input" not in responses_payload:
        raise HTTPException(status_code=400, detail="Responses request must contain 'input'")

    openai_messages.extend(_responses_input_to_openai_messages(responses_payload.get("input")))

    openai_payload = {
        "model": requested_model,
        "messages": openai_messages,
        "stream": responses_payload.get("stream", False),
    }

    if openai_payload["stream"]:
        openai_payload["stream_options"] = {"include_usage": True}

    if "max_output_tokens" in responses_payload:
        openai_payload["max_tokens"] = responses_payload["max_output_tokens"]
    elif "max_tokens" in responses_payload:
        openai_payload["max_tokens"] = responses_payload["max_tokens"]

    for field in ("metadata", "parallel_tool_calls", "reasoning", "service_tier", "temperature", "top_p", "user"):
        if field in responses_payload:
            openai_payload[field] = copy.deepcopy(responses_payload[field])

    if "tools" in responses_payload:
        openai_payload["tools"] = _responses_tools_to_openai(responses_payload["tools"])

    if "tool_choice" in responses_payload:
        openai_payload["tool_choice"] = copy.deepcopy(responses_payload["tool_choice"])

    text_config = responses_payload.get("text")
    if isinstance(text_config, dict):
        text_format = text_config.get("format")
        if isinstance(text_format, dict) and text_format.get("type") not in (None, "text"):
            openai_payload["response_format"] = copy.deepcopy(text_format)

    if "response_format" in responses_payload and "response_format" not in openai_payload:
        openai_payload["response_format"] = copy.deepcopy(responses_payload["response_format"])

    return openai_payload, requested_model


def _build_openai_usage_to_responses_usage(usage: object) -> dict:
    if not isinstance(usage, dict):
        usage = {}

    prompt_tokens_details = usage.get("prompt_tokens_details")
    if not isinstance(prompt_tokens_details, dict):
        prompt_tokens_details = {}

    completion_tokens_details = usage.get("completion_tokens_details")
    if not isinstance(completion_tokens_details, dict):
        completion_tokens_details = {}

    return {
        "input_tokens": usage.get("prompt_tokens", 0),
        "input_tokens_details": {
            "cached_tokens": prompt_tokens_details.get("cached_tokens", 0),
        },
        "output_tokens": usage.get("completion_tokens", 0),
        "output_tokens_details": {
            "reasoning_tokens": completion_tokens_details.get("reasoning_tokens", 0),
        },
        "total_tokens": usage.get("total_tokens", 0),
    }


def _openai_message_content_to_responses_content(content: object) -> list[dict]:
    if isinstance(content, str):
        return [{"type": "output_text", "text": content, "annotations": []}]
    if content is None:
        return [{"type": "output_text", "text": "", "annotations": []}]
    if not isinstance(content, list):
        return [{"type": "output_text", "text": _coerce_responses_text(content), "annotations": []}]

    text_parts: list[str] = []
    media_parts: list[dict] = []
    for part in content:
        if isinstance(part, str):
            text_parts.append(part)
            continue
        if not isinstance(part, dict):
            logging.debug("Skipping unsupported OpenAI message content part in Responses conversion: %r", part)
            continue

        part_type = part.get("type")
        if part_type in {"text", "output_text"}:
            text_value = part.get("text")
            if isinstance(text_value, str):
                text_parts.append(text_value)
            elif text_value is not None:
                text_parts.append(_coerce_responses_text(text_value))
            continue

        if part_type in {"image_url", "output_image"}:
            image_url = part.get("image_url") or part.get("url")
            if isinstance(image_url, dict):
                image_url = image_url.get("url")
            if isinstance(image_url, str) and image_url:
                media_parts.append({"type": "output_image", "image_url": {"url": image_url}})
            else:
                logging.debug("Skipping OpenAI image content part without image URL in Responses conversion.")
            continue

        logging.debug("Skipping unsupported OpenAI message content part type in Responses conversion: %r", part_type)

    output_content: list[dict] = []
    if text_parts:
        output_content.append({"type": "output_text", "text": "".join(text_parts), "annotations": []})
    output_content.extend(media_parts)
    if not output_content:
        output_content.append({"type": "output_text", "text": "", "annotations": []})
    return output_content


def _openai_response_to_responses(response_data: dict, requested_model: str) -> dict:
    choices = response_data.get("choices", [])
    first_choice = choices[0] if choices else {}
    message = first_choice.get("message", {})
    finish_reason = first_choice.get("finish_reason")
    usage = _build_openai_usage_to_responses_usage(response_data.get("usage"))

    output: list[dict] = []
    tool_calls = message.get("tool_calls")
    if isinstance(tool_calls, list) and tool_calls:
        for tool_call in tool_calls:
            if not isinstance(tool_call, dict):
                continue
            function = tool_call.get("function", {})
            call_id = tool_call.get("id") or f"call_{uuid4().hex}"
            output.append(
                {
                    "id": f"fc_{uuid4().hex}",
                    "type": "function_call",
                    "call_id": call_id,
                    "name": function.get("name"),
                    "arguments": function.get("arguments", ""),
                    "status": "completed",
                }
            )
    else:
        # Handle reasoning
        reasoning = message.get("reasoning_content") or message.get("reasoning")
        if isinstance(reasoning, str) and reasoning:
            output.append(
                {
                    "id": f"msg_{uuid4().hex}",
                    "type": "message",
                    "role": "assistant",
                    "status": "completed",
                    "content": [
                        {
                            "type": "output_text",
                            "text": reasoning,
                            "annotations": ["thought"],
                        }
                    ],
                }
            )

        # Handle main content
        output.append(
            {
                "id": f"msg_{uuid4().hex}",
                "type": "message",
                "role": "assistant",
                "status": "completed",
                "content": _openai_message_content_to_responses_content(message.get("content")),
            }
        )

    response_status = "completed" if finish_reason != "length" else "incomplete"
    responses_response = {
        "id": response_data.get("id", f"resp_{uuid4().hex}"),
        "object": "response",
        "created_at": response_data.get("created"),
        "status": response_status,
        "model": requested_model,
        "output": output,
        "usage": usage,
    }
    if finish_reason == "length":
        responses_response["incomplete_details"] = {"reason": "max_output_tokens"}
    return responses_response
