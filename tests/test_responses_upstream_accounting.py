import asyncio
import json
import sqlite3
from datetime import datetime, timezone

import pytest
from fastapi.responses import StreamingResponse

from llm_gateway_core.api.v1.chat_accounting import ChatStreamDialect
from llm_gateway_core.api.v1.chat_streaming import _DirectChatStreamObservationBuilder, _openai_stream_to_responses
from llm_gateway_core.api.v1.responses_upstream import responses_response_to_chat, responses_stream_to_chat
from llm_gateway_core.config.schemas import ProviderDetails
from llm_gateway_core.db.tokens_usage_db import TokensUsageDB
from llm_gateway_core.middleware.accounting_admission import AccountingRequestContext
from llm_gateway_core.services.accounting import AccountingReservation, classify_billing_policy
from llm_gateway_core.services.chat_accounting import (
    build_chat_accounting_event,
    build_direct_chat_terminal_observation,
)
from llm_gateway_core.services.stream_observation import SSEFramer
from llm_gateway_core.utils.usage_tracking import build_model_cost_rate_registry


@pytest.mark.parametrize("upstream_cost", [None, 0, 0.123])
def test_responses_usage_is_persisted_with_upstream_cost_or_model_rates(tmp_path, monkeypatch, upstream_cost):
    provider = ProviderDetails(
        baseUrl="https://provider.example/v1",
        apikey="key",
        models={"model": {"api_type": "responses", "input_rate": 2, "output_rate": 10}},
    )
    registry = build_model_cost_rate_registry({"provider": provider})
    assert registry[("provider", "model")].input_rate == 2
    assert registry[("provider", "model")].output_rate == 10
    usage = {
        "input_tokens": 10,
        "output_tokens": 5,
        "total_tokens": 15,
        "input_tokens_details": {"cached_tokens": 3},
        "output_tokens_details": {"reasoning_tokens": 2},
    }
    if upstream_cost is not None:
        usage["cost"] = upstream_cost
    payload = responses_response_to_chat(
        {
            "id": "response-1",
            "model": "model",
            "status": "completed",
            "output": [{"type": "message", "content": [{"type": "output_text", "text": "OK"}]}],
            "usage": usage,
        }
    )
    observation = build_direct_chat_terminal_observation(
        payload,
        provider="provider",
        model="model",
        cost_rate_registry=registry,
    )
    context = AccountingRequestContext(
        method="POST",
        route_template="/v1/chat/completions",
        policy=classify_billing_policy("POST", "/v1/chat/completions"),
        request_id="request-1",
        reservation=AccountingReservation(
            reservation_id="reservation-1",
            request_id="request-1",
            api_key_id=7,
            reserved_usd=1,
        ),
    )
    event = build_chat_accounting_event(
        context,
        observation,
        gateway_model="gateway/model",
        occurred_at=datetime(2026, 10, 20, tzinfo=timezone.utc),
    )
    monkeypatch.setenv("GATEWAY_DB_DIR", str(tmp_path))
    database = TokensUsageDB(db_filename="responses-usage.db")
    database.accept_accounting_event(event)
    with sqlite3.connect(database.db_path) as connection:
        row = connection.execute(
            "SELECT provider, model, prompt_tokens, completion_tokens, cached_tokens, reasoning_tokens, cost "
            "FROM tokens_usage"
        ).fetchone()
    assert row[:6] == ("provider", "model", 10, 5, 3, 2)
    assert row[6] == pytest.approx(upstream_cost if upstream_cost is not None else 0.00007)


@pytest.mark.parametrize("upstream_cost", [None, 0, 0.123])
def test_responses_client_stream_preserves_cost_for_accounting(upstream_cost):
    async def scenario():
        usage = {"input_tokens": 10, "output_tokens": 5, "total_tokens": 15}
        if upstream_cost is not None:
            usage["cost"] = upstream_cost
        terminal = {
            "type": "response.completed",
            "response": {"status": "completed", "output": [], "usage": usage},
        }

        async def body():
            yield b'data: {"type":"response.output_text.delta","delta":"OK"}\n\n'
            yield ("data: " + json.dumps(terminal) + "\n\n").encode()

        converted = responses_stream_to_chat(StreamingResponse(body()), "model")
        wire_response = _openai_stream_to_responses(converted, "gateway/model")
        provider = ProviderDetails(
            baseUrl="https://provider.example/v1",
            apikey="key",
            models={"model": {"api_type": "responses", "input_rate": 2, "output_rate": 10}},
        )
        observer = _DirectChatStreamObservationBuilder(
            dialect=ChatStreamDialect.RESPONSES,
            provider="provider",
            model="model",
            cost_rate_registry=build_model_cost_rate_registry({"provider": provider}),
        )
        observations = []
        framer = SSEFramer()
        async for chunk in wire_response.body_iterator:
            for event in framer.feed(chunk).events:
                observation = observer.observe(event)
                if observation is not None:
                    observations.append(observation)
        assert len(observations) == 1
        assert observations[0].usage.cost == pytest.approx(upstream_cost if upstream_cost is not None else 0.00007)

    asyncio.run(scenario())
