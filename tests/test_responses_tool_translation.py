import pytest
from fastapi import HTTPException

from llm_gateway_core.api.v1.chat_dialects import _responses_request_to_openai_payload


def test_namespace_tools_are_flattened_and_web_search_becomes_a_function():
    payload, model = _responses_request_to_openai_payload(
        {
            "model": "llmgateway/high",
            "input": "hi",
            "tools": [
                {
                    "type": "function",
                    "name": "exec_command",
                    "description": "Run a command",
                    "parameters": {"type": "object", "properties": {}},
                },
                {
                    "type": "namespace",
                    "name": "multi_agent_v1",
                    "description": "Agent tools",
                    "tools": [
                        {
                            "type": "function",
                            "name": "spawn_agent",
                            "description": "Start an agent",
                            "parameters": {"type": "object", "properties": {"prompt": {"type": "string"}}},
                        }
                    ],
                },
                {"type": "web_search", "external_web_access": True},
            ],
        }
    )

    assert model == "llmgateway/high"
    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "exec_command",
        "spawn_agent",
        "web_search",
    ]
    assert payload["tools"][1]["function"]["description"] == "Start an agent"
    assert payload["tools"][1]["function"]["parameters"]["properties"]["prompt"]["type"] == "string"
    assert payload["tools"][2]["function"]["parameters"]["required"] == ["query"]


def test_duplicate_namespace_tool_name_is_qualified():
    payload, _model = _responses_request_to_openai_payload(
        {
            "model": "m",
            "input": "hi",
            "tools": [
                {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                {
                    "type": "namespace",
                    "name": "mcp__codex_tui",
                    "tools": [
                        {"type": "function", "name": "exec_command", "parameters": {"type": "object"}},
                    ],
                },
            ],
        }
    )

    assert [tool["function"]["name"] for tool in payload["tools"]] == [
        "exec_command",
        "mcp__codex_tui__exec_command",
    ]


def test_unknown_responses_tool_type_is_still_rejected():
    with pytest.raises(HTTPException) as exc:
        _responses_request_to_openai_payload(
            {
                "model": "m",
                "input": "hi",
                "tools": [{"type": "computer", "name": "screen"}],
            }
        )
    assert exc.value.status_code == 400
    assert exc.value.detail == "Unsupported Responses tool type: computer"
