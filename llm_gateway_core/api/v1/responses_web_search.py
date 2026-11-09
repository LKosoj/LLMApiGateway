"""Run a Codex Responses web_search tool through the gateway search stack.

Codex sends a hosted tool (``web_search`` or ``web_search_preview``). When
``external_web_access`` is false the tool is dropped and no search request
goes out. Otherwise the gateway turns it into a normal chat function, calls
the same search used by ``/v1/web/search``, Jina and Tavily, then asks the
model once more.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from fastapi import HTTPException, Request
from fastapi.responses import StreamingResponse

_HOSTED_WEB_SEARCH_DESCRIPTION = (
    "Search the web for up-to-date information. "
    "Returns a list of results with url, title and snippet."
)
_CONTEXT_SIZE_RESULTS = {"low": 3, "medium": 5, "high": 8}
_DEFAULT_MAX_RESULTS = 5
_MAX_MODEL_ROUNDS = 3
_MISSING = object()


@dataclass(frozen=True, slots=True)
class ResponsesWebSearchBinding:
    """How one hosted Responses search tool maps onto gateway web_search."""

    function_name: str
    max_results: int
    include_domains: tuple[str, ...]
    external_web_access: bool | None


def responses_web_search_function_tool(tool: dict) -> dict:
    """Chat function schema offered to the model for one hosted search tool."""
    description = _HOSTED_WEB_SEARCH_DESCRIPTION
    location = _location_text(tool.get("user_location"))
    if location:
        description = f"{description} User location: {location}."
    return {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": description,
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "The search query."},
                },
                "required": ["query"],
            },
        },
    }


def external_web_access_allowed(tool: dict) -> bool:
    """Live search is allowed unless the client set external_web_access to false.

    A missing flag means allowed: that is the OpenAI default for this tool.
    """
    return tool.get("external_web_access") is not False


def responses_web_search_binding(tool: dict, function_name: str) -> ResponsesWebSearchBinding:
    """Read Codex search limits from the hosted tool object."""
    filters = tool.get("filters")
    domains: tuple[str, ...] = ()
    if isinstance(filters, dict) and "allowed_domains" in filters:
        from .web_adapters import _parse_domain_list

        domains = tuple(_parse_domain_list(filters.get("allowed_domains"), "allowed_domains"))
    flag = tool.get("external_web_access")
    return ResponsesWebSearchBinding(
        function_name=function_name,
        max_results=_max_results_for_context_size(tool.get("search_context_size")),
        include_domains=domains,
        external_web_access=flag if isinstance(flag, bool) else None,
    )


def parse_web_search_query(arguments: object) -> str | None:
    """Read a search string from the shapes Codex and chat models send."""
    if isinstance(arguments, str):
        text = arguments.strip()
        if not text:
            return None
        try:
            arguments = json.loads(text)
        except json.JSONDecodeError:
            return text
    if not isinstance(arguments, dict):
        return None

    for key in ("query", "q"):
        value = arguments.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()

    action = arguments.get("action")
    if not isinstance(action, dict):
        return None
    action_query = action.get("query")
    if isinstance(action_query, str) and action_query.strip():
        return action_query.strip()
    queries = action.get("queries")
    if isinstance(queries, list):
        for item in queries:
            if isinstance(item, str) and item.strip():
                return item.strip()
    return None


async def complete_responses_web_search(
    request: Request,
    openai_payload: dict,
    bindings: list[ResponsesWebSearchBinding],
    *,
    dispatch: Callable[..., Awaitable[Any]],
    search: Callable[..., Awaitable[str]] | None = None,
) -> dict:
    """Call the model with streaming off and run gateway search in between.

    A turn is searched only when every tool call is one of ``bindings``.
    A mix with client tools (shell, editor, and so on) is returned unchanged
    so Codex still sees a consistent tool-call list. At most three model
    calls are made. Search runs between them, not after the last one.
    """
    by_name = {binding.function_name: binding for binding in bindings}
    source_messages = openai_payload.get("messages")
    if not isinstance(source_messages, list):
        raise HTTPException(status_code=500, detail="Internal server error: expected chat messages")

    working = dict(openai_payload)
    messages = list(source_messages)
    working["messages"] = messages
    working["stream"] = False
    working.pop("stream_options", None)
    search_call = search or _default_gateway_search

    response_data: dict | None = None
    usages: list[object] = []
    for round_index in range(_MAX_MODEL_ROUNDS):
        dispatched = await dispatch(request, working, accounting_handoff=None)
        if not isinstance(dispatched, dict):
            raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
        response_data = dispatched
        usages.append(response_data.get("usage"))
        tool_calls = _message_tool_calls(response_data)
        if not tool_calls or not _only_bound_search(tool_calls, by_name):
            break
        if round_index == _MAX_MODEL_ROUNDS - 1:
            break
        snapshot = _snapshot_provider(request)
        try:
            await _append_search_results(request, messages, response_data, tool_calls, by_name, search_call)
        finally:
            _restore_provider(request, snapshot)

    if response_data is None:
        raise HTTPException(status_code=500, detail="Internal server error: expected JSON response")
    response_data["usage"] = _combine_usage(usages)
    return response_data


def publish_responses_chat_observation(request: Request, response_data: dict) -> None:
    """Bill the finished model turn once. Search rounds are already included in its usage."""
    from ...services.chat_accounting import build_direct_chat_terminal_observation
    from .chat_accounting import get_chat_terminal_handoff

    provider = getattr(request.state, "llmgateway_provider", None)
    model = getattr(request.state, "llmgateway_provider_model", None)
    if not isinstance(provider, str) or not provider or not isinstance(model, str) or not model:
        raise HTTPException(status_code=500, detail="Internal server error: chat provider was not recorded")
    observation = build_direct_chat_terminal_observation(
        response_data,
        provider=provider,
        model=model,
        cost_rate_registry=request.state.runtime_snapshot.cost_rate_registry,
    )
    get_chat_terminal_handoff(request).publish(observation)


def responses_chat_to_sse(response_data: dict, requested_model: str) -> StreamingResponse:
    """Synthetic Responses event stream for a finished chat dict.

    The internal model calls are not streamed. Codex still receives the same
    event names the streaming translator emits, including ``response.completed``
    and ``data: [DONE]``.
    """
    from .chat_dialects import _openai_response_to_responses
    from .chat_streaming import _format_responses_sse_event

    completed = _openai_response_to_responses(response_data, requested_model)
    response_id = completed.get("id")

    async def generate():
        for index, item in enumerate(completed.get("output") or []):
            if not isinstance(item, dict):
                continue
            if item.get("type") == "function_call":
                yield _format_responses_sse_event(
                    {
                        "type": "response.output_item.added",
                        "response_id": response_id,
                        "output_index": index,
                        "item": {
                            "id": item.get("id"),
                            "type": "function_call",
                            "call_id": item.get("call_id"),
                            "name": item.get("name"),
                            "arguments": "",
                            "status": "in_progress",
                        },
                    }
                )
                arguments = item.get("arguments") or ""
                if isinstance(arguments, str) and arguments:
                    yield _format_responses_sse_event(
                        {
                            "type": "response.function_call_arguments.delta",
                            "response_id": response_id,
                            "item_id": item.get("id"),
                            "output_index": index,
                            "delta": arguments,
                        }
                    )
            elif item.get("type") == "message":
                text, annotations = _message_delta(item)
                if text:
                    event = {
                        "type": "response.output_text.delta",
                        "response_id": response_id,
                        "output_index": index,
                        "content_index": 0,
                        "delta": text,
                    }
                    if annotations:
                        event["annotations"] = annotations
                    yield _format_responses_sse_event(event)
            yield _format_responses_sse_event(
                {
                    "type": "response.output_item.done",
                    "response_id": response_id,
                    "output_index": index,
                    "item": item,
                }
            )
        yield _format_responses_sse_event({"type": "response.completed", "response": completed})
        yield b"data: [DONE]\n\n"

    return StreamingResponse(
        generate(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


def _location_text(value: object) -> str | None:
    if isinstance(value, str) and value.strip():
        return value.strip()
    if not isinstance(value, dict):
        return None
    parts = [
        value[key].strip()
        for key in ("city", "region", "country", "timezone")
        if isinstance(value.get(key), str) and value[key].strip()
    ]
    return ", ".join(parts) or None


def _max_results_for_context_size(value: object) -> int:
    if not isinstance(value, str):
        return _DEFAULT_MAX_RESULTS
    return _CONTEXT_SIZE_RESULTS.get(value.strip().lower(), _DEFAULT_MAX_RESULTS)


def _message_tool_calls(response_data: dict) -> list[dict]:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return []
    message = choices[0].get("message")
    if not isinstance(message, dict):
        return []
    tool_calls = message.get("tool_calls")
    if not isinstance(tool_calls, list):
        return []
    return [call for call in tool_calls if isinstance(call, dict)]


def _call_name(tool_call: dict) -> str | None:
    function = tool_call.get("function")
    if not isinstance(function, dict):
        return None
    name = function.get("name")
    return name if isinstance(name, str) and name else None


def _only_bound_search(tool_calls: list[dict], by_name: dict[str, ResponsesWebSearchBinding]) -> bool:
    names = [_call_name(call) for call in tool_calls]
    return all(name in by_name for name in names)


def _choice_message(response_data: dict) -> dict:
    choices = response_data.get("choices")
    if not isinstance(choices, list) or not choices or not isinstance(choices[0], dict):
        return {}
    message = choices[0].get("message")
    return message if isinstance(message, dict) else {}


async def _append_search_results(
    request: Request,
    messages: list,
    response_data: dict,
    tool_calls: list[dict],
    by_name: dict[str, ResponsesWebSearchBinding],
    search: Callable[..., Awaitable[str]],
) -> None:
    message = _choice_message(response_data)
    content = message.get("content")
    if not isinstance(content, (str, list)):
        content = None
    history_calls: list[dict] = []
    for tool_call in tool_calls:
        prepared = dict(tool_call)
        if not isinstance(prepared.get("id"), str) or not prepared["id"]:
            prepared["id"] = f"call_{uuid4().hex}"
        prepared["type"] = "function"
        function = dict(prepared.get("function") or {})
        arguments = function.get("arguments")
        if not isinstance(arguments, str):
            function["arguments"] = json.dumps(arguments if arguments is not None else {}, ensure_ascii=False)
        prepared["function"] = function
        history_calls.append(prepared)

    messages.append({"role": "assistant", "content": content, "tool_calls": history_calls})
    for tool_call in history_calls:
        function = tool_call["function"]
        binding = by_name[function["name"]]
        query = parse_web_search_query(function.get("arguments"))
        if binding.external_web_access is False:
            result_text = "web_search error: external web access is disabled."
        elif query is None:
            result_text = "web_search error: 'query' is required."
        else:
            result_text = await _search_text(
                search,
                request,
                query=query,
                max_results=binding.max_results,
                include_domains=list(binding.include_domains),
            )
        messages.append({"role": "tool", "tool_call_id": tool_call["id"], "content": result_text})


async def _search_text(
    search: Callable[..., Awaitable[str]],
    request: Request,
    *,
    query: str,
    max_results: int,
    include_domains: list[str],
) -> str:
    try:
        text = await search(
            request,
            query=query,
            max_results=max_results,
            include_domains=include_domains,
        )
    except HTTPException as exc:
        return f"web_search error: {exc.detail}"
    except Exception as exc:
        logging.warning("Responses web_search failed: %s", exc)
        return f"web_search error: {exc}"
    return text if isinstance(text, str) else str(text)


async def _default_gateway_search(
    request: Request,
    *,
    query: str,
    max_results: int,
    include_domains: list[str],
) -> str:
    from .web_adapters import (
        WEB_SEARCH_SECTION,
        _UsageAccumulator,
        _get_operation_runtime,
        _resolve_service_model,
        _search_with_model,
    )

    _dispatcher, _http_client, config_loader, _proxy_clients = _get_operation_runtime(request)
    search_model = _resolve_service_model(
        config_loader,
        WEB_SEARCH_SECTION,
        None,
        field_name="model",
    )
    results = await _search_with_model(
        request,
        search_model=search_model,
        query=query,
        max_results=max_results,
        num_queries=1,
        language="en",
        usage_accumulator=_UsageAccumulator(),
        expand_query=False,
        include_domains=include_domains or None,
    )
    return _format_search_results(results)


def _format_search_results(results: object) -> str:
    if not isinstance(results, list) or not results:
        return "No results."
    lines: list[str] = []
    for index, item in enumerate(results, start=1):
        if not isinstance(item, dict):
            continue
        title = item.get("title") or "(no title)"
        url = item.get("url") or ""
        snippet = item.get("snippet") or ""
        lines.append(f"[{index}] {title}\n{url}\n{snippet}".strip())
    return "\n\n".join(lines) or "No results."


def _usage_int(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    return int(value)


def _combine_usage(usages: list[object]) -> dict:
    total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    cached = 0
    reasoning = 0
    saw_cached = False
    saw_reasoning = False
    cost = 0.0
    saw_cost = False
    for usage in usages:
        if not isinstance(usage, dict):
            continue
        total["prompt_tokens"] += _usage_int(usage.get("prompt_tokens"))
        total["completion_tokens"] += _usage_int(usage.get("completion_tokens"))
        total["total_tokens"] += _usage_int(usage.get("total_tokens"))
        prompt_details = usage.get("prompt_tokens_details")
        if isinstance(prompt_details, dict) and "cached_tokens" in prompt_details:
            saw_cached = True
            cached += _usage_int(prompt_details.get("cached_tokens"))
        completion_details = usage.get("completion_tokens_details")
        if isinstance(completion_details, dict) and "reasoning_tokens" in completion_details:
            saw_reasoning = True
            reasoning += _usage_int(completion_details.get("reasoning_tokens"))
        if isinstance(usage.get("cost"), (int, float)) and not isinstance(usage.get("cost"), bool):
            saw_cost = True
            cost += float(usage["cost"])
    if total["total_tokens"] == 0:
        total["total_tokens"] = total["prompt_tokens"] + total["completion_tokens"]
    if saw_cached:
        total["prompt_tokens_details"] = {"cached_tokens": cached}
    if saw_reasoning:
        total["completion_tokens_details"] = {"reasoning_tokens": reasoning}
    if saw_cost and cost > 0:
        total["cost"] = cost
    return total


def _snapshot_provider(request: Request) -> tuple[object, object, object]:
    return (
        getattr(request.state, "llmgateway_provider", _MISSING),
        getattr(request.state, "llmgateway_provider_model", _MISSING),
        getattr(request.state, "llmgateway_target_path", _MISSING),
    )


def _restore_provider(request: Request, snapshot: tuple[object, object, object]) -> None:
    for name, value in zip(
        ("llmgateway_provider", "llmgateway_provider_model", "llmgateway_target_path"),
        snapshot,
        strict=True,
    ):
        if value is _MISSING:
            if hasattr(request.state, name):
                delattr(request.state, name)
            continue
        setattr(request.state, name, value)
    if request.scope.get("app") is None:
        return
    from ...services.active_requests import update_active_request_from_state

    update_active_request_from_state(request)


def _message_delta(item: dict) -> tuple[str, list | None]:
    content = item.get("content")
    if not isinstance(content, list):
        return "", None
    texts: list[str] = []
    annotations = None
    for part in content:
        if not isinstance(part, dict) or part.get("type") != "output_text":
            continue
        text = part.get("text")
        if isinstance(text, str) and text:
            texts.append(text)
        if annotations is None and isinstance(part.get("annotations"), list) and part["annotations"]:
            annotations = part["annotations"]
    return "".join(texts), annotations
