from __future__ import annotations

import json
import logging
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ZAI_MCP_BASE_URL = "https://api.z.ai/api/mcp"
ZAI_MCP_PROTOCOL_VERSION = "2024-11-05"


def zai_mcp_server_url(server_path: str) -> str:
    return f"{ZAI_MCP_BASE_URL}/{server_path}/mcp"


def _parse_sse_envelope(response_text: str) -> dict[str, Any]:
    """Return the first JSON-RPC envelope from a Streamable HTTP SSE response."""
    for raw_line in response_text.splitlines():
        line = raw_line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload:
            continue
        return json.loads(payload)
    raise RuntimeError("Z.AI MCP: пустой SSE-ответ")


def _extract_tool_payload(envelope: dict[str, Any]) -> Any:
    """Return the parsed text content from a tools/call envelope.

    The Z.AI MCP servers wrap the tool payload as a JSON string inside
    result.content[0].text — and in practice double-encode it (the inner
    string is itself JSON). Unwrap repeatedly while the value is a string
    that parses as JSON, capped at a handful of iterations.
    """
    if "error" in envelope:
        raise RuntimeError(f"Z.AI MCP tool error: {envelope['error']}")
    result = envelope.get("result") or {}
    if isinstance(result, dict) and result.get("isError"):
        raise RuntimeError(f"Z.AI MCP tool error: {result}")
    blocks = (result or {}).get("content") or []
    for block in blocks:
        if not isinstance(block, dict):
            continue
        text = block.get("text")
        if not isinstance(text, str) or not text.strip():
            continue
        value: Any = text
        for _ in range(3):
            if not isinstance(value, str):
                break
            try:
                value = json.loads(value)
            except json.JSONDecodeError:
                break
        return value
    return None


async def zai_mcp_tool_call(
    client: httpx.AsyncClient,
    *,
    api_key: str,
    server_path: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout: float = 60.0,
) -> Any:
    """Invoke a tool on a Z.AI Streamable HTTP MCP server.

    Performs the minimal MCP handshake (initialize -> notifications/initialized
    -> tools/call) over plain httpx without taking a dependency on the mcp
    SDK. Returns the JSON-decoded tool payload, or raises on any protocol/
    transport error.
    """
    url = zai_mcp_server_url(server_path)
    base_headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }

    init_resp = await client.post(
        url,
        headers=base_headers,
        json={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": ZAI_MCP_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "llmgateway", "version": "1.0"},
            },
        },
        timeout=timeout,
    )
    init_resp.raise_for_status()
    session_id = init_resp.headers.get("mcp-session-id") or init_resp.headers.get(
        "Mcp-Session-Id"
    )
    if not session_id:
        raise RuntimeError("Z.AI MCP: сервер не вернул mcp-session-id")

    session_headers = {**base_headers, "Mcp-Session-Id": session_id}

    notif_resp = await client.post(
        url,
        headers=session_headers,
        json={"jsonrpc": "2.0", "method": "notifications/initialized"},
        timeout=timeout,
    )
    notif_resp.raise_for_status()

    call_resp = await client.post(
        url,
        headers=session_headers,
        json={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": arguments},
        },
        timeout=timeout,
    )
    call_resp.raise_for_status()

    envelope = _parse_sse_envelope(call_resp.text)
    return _extract_tool_payload(envelope)


def detect_zai_search_location(query: str) -> str:
    """Pick Z.AI search region: cn for CJK queries, us otherwise.

    The default Z.AI region (cn) returns Chinese-only results for non-CJK
    queries, which is why the previous integration looked broken.
    """
    if any("一" <= ch <= "鿿" for ch in query or ""):
        return "cn"
    return "us"
