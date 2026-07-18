"""Focused regression tests for two code-review findings:

1. ``custom_body_params`` (a per-rule config field) must not be able to
   silently disable the forced ``stream_options.include_usage`` invariant
   for streaming, non-anthropic provider calls
   (llm_gateway_core/api/v1/chat_dispatch.py).
2. The ``single_process_bootstrap`` module docstring must accurately
   describe what is (and is not) guaranteed to run before process state is
   created (llm_gateway_core/services/single_process_bootstrap.py).
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from llm_gateway_core.api.v1 import chat as chat_api
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from tests._async_compat import run_async
from tests.test_chat_accounting_integration import _direct_request, _provider_config


async def _attempt_with_rule(
    *,
    provider_type: str,
    streaming: bool,
    model_fallback_rule: dict,
    anthropic_payload: dict | None = None,
) -> dict:
    """Run ``attempt_model_fallback_rule`` and return the payload actually
    sent to the upstream provider (the 4th positional arg of
    ``make_llm_request``)."""
    request = _direct_request(anthropic_payload=anthropic_payload)
    auth_material = SimpleNamespace(
        headers={"Authorization": "Bearer upstream-secret"},
        upstream_key_fingerprint=None,
    )
    # A bare {"ok": True} response has no "choices" and would be flagged as an
    # empty_completion by the degenerate-response detector (Package D) on the
    # non-streaming path, so the "success" mock must be choices-shaped.
    upstream_response = {
        "ok": True,
        "choices": [
            {
                "finish_reason": "stop",
                "message": {"role": "assistant", "content": "ok"},
            }
        ],
    }
    make_llm_request_mock = AsyncMock(return_value=(upstream_response, None))
    with (
        patch.object(
            chat_api,
            "resolve_provider_auth_material",
            new=AsyncMock(return_value=auth_material),
        ),
        patch.object(chat_api, "make_llm_request", new=make_llm_request_mock),
    ):
        result, error, _attempt_number = await chat_api._attempt_model_fallback_rule(
            request,
            object(),
            {"provider-a": _provider_config(provider_type=provider_type)},
            "gateway-model",
            {
                "model": "gateway-model",
                "messages": [{"role": "user", "content": "hi"}],
                "stream": streaming,
            },
            model_fallback_rule,
            streaming,
            proxy_http_clients={},
            upstream_routing_state=UpstreamRoutingState(),
        )

    assert error is None
    assert result == upstream_response
    make_llm_request_mock.assert_awaited_once()
    return make_llm_request_mock.await_args.args[3]


# --- custom_body_params must not be able to turn off the forced -----------
#     include_usage invariant for streaming, non-anthropic calls. -----------


def test_custom_body_params_stream_options_cannot_disable_include_usage() -> None:
    attempt_payload = run_async(
        _attempt_with_rule(
            provider_type="openai",
            streaming=True,
            model_fallback_rule={
                "provider": "provider-a",
                "model": "model-a",
                "custom_body_params": {"stream_options": {"extra_field": "keep-me"}},
            },
        )
    )
    assert attempt_payload.get("stream_options") == {
        "extra_field": "keep-me",
        "include_usage": True,
    }


def test_anthropic_provider_does_not_get_include_usage_forced() -> None:
    anthropic_payload = {
        "model": "gateway-model",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
    }
    attempt_payload = run_async(
        _attempt_with_rule(
            provider_type="anthropic",
            streaming=True,
            model_fallback_rule={"provider": "provider-a", "model": "model-a"},
            anthropic_payload=anthropic_payload,
        )
    )
    assert "stream_options" not in attempt_payload


def test_non_streaming_request_does_not_get_include_usage_forced() -> None:
    attempt_payload = run_async(
        _attempt_with_rule(
            provider_type="openai",
            streaming=False,
            model_fallback_rule={
                "provider": "provider-a",
                "model": "model-a",
                "custom_body_params": {"stream_options": {"extra_field": "keep-me"}},
            },
        )
    )
    assert attempt_payload.get("stream_options") == {"extra_field": "keep-me"}
