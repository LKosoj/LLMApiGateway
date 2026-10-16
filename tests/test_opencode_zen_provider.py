import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest
import httpx
from fastapi.responses import StreamingResponse

from tests._async_compat import run_async

from llm_gateway_core.api.v1.chat import _attempt_model_fallback_rule
from llm_gateway_core.config.loader import ProviderDetails
from llm_gateway_core.services.provider_models import ProviderModelsService
from llm_gateway_core.services.upstream_routing_state import UpstreamRoutingState
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.services.accounting import (
    ACCOUNTING_EVENT_VERSION,
    AccountingEvent,
    AccountingEventKind,
    CostSource,
)
from llm_gateway_core.services.chat_accounting import build_direct_chat_terminal_observation
from llm_gateway_core.utils.usage_tracking import (
    build_model_cost_rate_registry,
    calculate_model_cost_usd,
)


@pytest.fixture
def provider():
    entries = json.loads(
        (Path(__file__).resolve().parents[1] / "providers.json").read_text()
    )
    matches = [entry["opencode-zen"] for entry in entries if "opencode-zen" in entry]
    assert len(matches) == 1
    return ProviderDetails.model_validate(matches[0])


@pytest.mark.parametrize("provider_name", ["opencode-zen", "other-provider"])
def test_upstream_catalog_is_filtered_by_free_suffix_only_for_zen(provider, provider_name):
    client = Mock()
    entries = [
        {"id": "new-model-free", "context_length": 100000},
        {"id": "paid-model"},
        {"id": "big-pickle"},
        {"id": "free-in-the-middle-model"},
        {"id": "another-free"},
    ]
    client.get = AsyncMock(return_value=httpx.Response(200, json={"data": entries}))
    service = ProviderModelsService()
    assert provider.available_models is None
    models = run_async(service.get_models(provider_name, provider, client))
    metadata = run_async(service.get_model_entries(provider_name, provider, client))

    expected = [entry for entry in entries if entry["id"].endswith("-free")] if provider_name == "opencode-zen" else entries
    assert models == [entry["id"] for entry in expected]
    assert metadata == {entry["id"]: entry for entry in expected}
    client.get.assert_awaited_once_with(
        "https://opencode.ai/inference/v1/models",
        headers={"Content-Type": "application/json", "Authorization": "Bearer public", "User-Agent": "opencode/1.19.0"},
    )


def test_zen_catalog_refresh_discovers_new_free_models(provider):
    now = [0]
    client = Mock()
    client.get = AsyncMock(side_effect=[
        httpx.Response(200, json={"data": [{"id": "old-free"}, {"id": "paid"}]}),
        httpx.Response(200, json={"data": [{"id": "new-free"}, {"id": "big-pickle"}]}),
    ])
    service = ProviderModelsService(time_func=lambda: now[0])
    assert run_async(service.get_models("opencode-zen", provider, client)) == ["old-free"]
    now[0] = service.ttl_seconds + 1
    assert run_async(service.get_models("opencode-zen", provider, client)) == ["new-free"]
    assert client.get.await_count == 2


def test_zen_catalog_error_is_not_replaced_with_model_prices(provider):
    client = Mock()
    client.get = AsyncMock(return_value=httpx.Response(503, text="unavailable"))
    with pytest.raises(ValueError, match="HTTP 503"):
        run_async(ProviderModelsService().get_models("opencode-zen", provider, client))


@pytest.mark.parametrize("stream", [False, True])
def test_chat_sends_public_key_and_opencode_user_agent(provider, stream):
    async def upstream_body():
        yield b'data: {"id":"zen-test","choices":[{"index":0,"delta":{"role":"assistant","content":"ok"},"finish_reason":"stop"}]}\n\ndata: [DONE]\n\n'

    make_request = AsyncMock(return_value=(StreamingResponse(upstream_body()), None))
    body = {
        "model": "big-pickle",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": stream,
    }
    with patch("llm_gateway_core.api.v1.chat.make_llm_request", new=make_request):
        response, error, _ = run_async(
            _attempt_model_fallback_rule(
                SimpleNamespace(state=SimpleNamespace(), headers={}),
                Mock(),
                {"opencode-zen": provider},
                "big-pickle",
                body,
                {"provider": "opencode-zen", "model": "big-pickle"},
                stream,
                proxy_http_clients={},
                upstream_routing_state=UpstreamRoutingState(),
                stream_event_max_bytes=16384,
            )
        )

    assert error is None
    if stream:
        async def collect():
            return b"".join([chunk async for chunk in response.body_iterator])
        assert b'"content": "ok"' in run_async(collect())
    else:
        assert response["choices"][0]["message"]["content"] == "ok"
    make_request.assert_awaited_once()
    args = make_request.await_args.args
    assert args[1] == "https://opencode.ai/inference/v1/chat/completions"
    assert args[2]["Authorization"] == "Bearer public"
    assert args[2]["User-Agent"] == "opencode/1.19.0"
    assert len({args[2][name] for name in (
        "x-opencode-session", "x-opencode-session-id", "x-session-affinity", "X-Session-Id",
    )}) == 1
    assert args[3]["model"] == "big-pickle"
    assert args[3]["stream"] is True
    assert {tool["function"]["name"] for tool in args[3]["tools"]} == {"bash", "read"}
    assert body["stream"] is stream
    assert "tools" not in body


def test_free_chat_model_rates_and_calculator_are_zero(provider):
    registry = build_model_cost_rate_registry({"opencode-zen": provider})
    assert provider.models
    for model in provider.models.keys() - {"jev-1.13-free"}:
        rates = registry[("opencode-zen", model)]
        assert (rates.input_rate, rates.output_rate) == (0, 0)
        assert calculate_model_cost_usd(
            prompt_tokens=1000000, completion_tokens=1000000, rates=rates,
        ) == 0


@pytest.mark.parametrize("upstream_cost", [None, 0, 0.123])
def test_free_model_usage_record_preserves_upstream_cost_priority(
    provider, upstream_cost, tmp_path, monkeypatch,
):
    usage = {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
    if upstream_cost is not None:
        usage["cost"] = upstream_cost
    observation = build_direct_chat_terminal_observation(
        {"usage": usage}, provider="opencode-zen", model="big-pickle",
        cost_rate_registry=build_model_cost_rate_registry({"opencode-zen": provider}),
    )
    expected_cost = 0 if upstream_cost is None else upstream_cost
    assert observation.usage.cost == expected_cost
    assert observation.components[0].cost_source is (
        CostSource.TOKEN_REGISTRY if upstream_cost is None else CostSource.UPSTREAM
    )
    event = AccountingEvent(
        version=ACCOUNTING_EVENT_VERSION, event_id="zen-usage",
        kind=AccountingEventKind.CHARGE, api_key_id=7, method="POST",
        route_template="/v1/chat/completions", operation="chat",
        gateway_model="big-pickle", provider="opencode-zen", model="big-pickle",
        usage=observation.usage, cost_source=CostSource.COMPONENT_SUM,
        components=observation.components,
        occurred_at=datetime(2026, 10, 15, tzinfo=timezone.utc),
    )
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    db = TokensUsageDB(db_filename="zen-usage.db")
    db.accept_accounting_event(event)
    assert db.get_accounting_event(event.event_id).event.usage.cost == expected_cost
    with sqlite3.connect(db.db_path) as connection:
        assert connection.execute("SELECT cost FROM tokens_usage").fetchone() == (expected_cost,)
